"""
JGE CreditLab - Evidence Locator Microservice
Fase 2: MVP minimo. Recibe un PDF + texto a buscar, devuelve coordenadas.
n8n llama a este servicio via HTTP Request node (igual que ya llama a Anthropic).
"""
import base64
import io
from typing import Optional

import fitz  # PyMuPDF
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="JGE CreditLab Evidence Locator")


# ── Health check (para que Easypanel confirme que el contenedor esta vivo) ──
@app.get("/health")
def health():
    return {"status": "ok", "service": "evidence-locator", "pymupdf_version": fitz.version[0]}


# ── Modelos de entrada/salida ──
class LocateRequest(BaseModel):
    pdf_base64: str
    search_text: str
    # Opcional: restringe la busqueda a una franja horizontal (columna de un buro especifico)
    # util cuando el mismo texto aparece varias veces en distintas columnas
    x_min: Optional[float] = None
    x_max: Optional[float] = None
    page_hint: Optional[int] = None  # si ya sabemos en que pagina buscar (mas rapido, mas preciso)


class Match(BaseModel):
    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class LocateResponse(BaseModel):
    query: str
    matches: list[Match]
    confidence: str  # HIGH (1 match), MEDIUM (2-3 matches), LOW (4+ o ninguno)


# ── Endpoint principal: localizar texto en el PDF ──
@app.post("/locate", response_model=LocateResponse)
def locate_text(req: LocateRequest):
    try:
        pdf_bytes = base64.b64decode(req.pdf_base64)
    except Exception:
        raise HTTPException(400, "pdf_base64 invalido")

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise HTTPException(400, f"No se pudo abrir el PDF: {e}")

    matches: list[Match] = []
    pages_to_search = [req.page_hint - 1] if req.page_hint else range(len(doc))

    for page_num in pages_to_search:
        if page_num < 0 or page_num >= len(doc):
            continue
        page = doc[page_num]
        for rect in page.search_for(req.search_text):
            # Si se especifico una columna (x_min/x_max), descarta matches fuera de ella
            if req.x_min is not None and rect.x0 < req.x_min:
                continue
            if req.x_max is not None and rect.x0 > req.x_max:
                continue
            matches.append(Match(page=page_num + 1, x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1))

    doc.close()

    if len(matches) == 1:
        confidence = "HIGH"
    elif 1 < len(matches) <= 3:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return LocateResponse(query=req.search_text, matches=matches, confidence=confidence)


# ── Endpoint: recortar + resaltar una region y devolver PNG en base64 ──
class CropRequest(BaseModel):
    pdf_base64: str
    page: int  # 1-indexed
    highlight_x0: float
    highlight_y0: float
    highlight_x1: float
    highlight_y1: float
    crop_margin_top: float = 30
    crop_margin_bottom: float = 15
    crop_margin_left: float = 10
    crop_margin_right: float = 250  # suficiente para cubrir 3 columnas de buro
    dpi: int = 200


class CropResponse(BaseModel):
    image_base64: str
    width: int
    height: int


@app.post("/crop_and_highlight", response_model=CropResponse)
def crop_and_highlight(req: CropRequest):
    try:
        pdf_bytes = base64.b64decode(req.pdf_base64)
    except Exception:
        raise HTTPException(400, "pdf_base64 invalido")

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if req.page < 1 or req.page > len(doc):
        doc.close()
        raise HTTPException(400, f"Pagina {req.page} fuera de rango (PDF tiene {len(doc)} paginas)")

    page = doc[req.page - 1]

    # Resalta SOLO el valor especifico (nunca cubre el texto, solo lo marca)
    target_rect = fitz.Rect(req.highlight_x0, req.highlight_y0, req.highlight_x1, req.highlight_y1)
    highlight = page.add_highlight_annot(target_rect)
    highlight.set_colors(stroke=(1, 0.85, 0))
    highlight.update()

    # Recorta una region mas amplia alrededor del highlight para dar contexto
    clip = fitz.Rect(
        max(0, req.highlight_x0 - req.crop_margin_left),
        max(0, req.highlight_y0 - req.crop_margin_top),
        req.highlight_x1 + req.crop_margin_right,
        req.highlight_y1 + req.crop_margin_bottom,
    )
    pix = page.get_pixmap(clip=clip, dpi=req.dpi)
    png_bytes = pix.tobytes("png")
    doc.close()

    return CropResponse(
        image_base64=base64.b64encode(png_bytes).decode("utf-8"),
        width=pix.width,
        height=pix.height,
    )
