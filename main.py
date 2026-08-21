"""
JGE CreditLab - Evidence Locator Microservice
Fase 4: endpoint consolidado que arma el CFPB Evidence Package completo en una sola llamada.

Estrategia de localizacion (para evitar marcar el dato equivocado cuando el mismo valor
aparece varias veces en el documento): se busca el NOMBRE DE LA CUENTA y el VALOR especifico
en la MISMA pagina, y se emparejan por cercania vertical (misma tabla). Nunca se confirma
una evidencia con baja confianza -- se marca REVIEW_REQUIRED y se reporta la razon exacta.
"""
import base64
from typing import Optional

import fitz  # PyMuPDF
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="JGE CreditLab Evidence Locator")


@app.get("/health")
def health():
    return {"status": "ok", "service": "evidence-locator", "pymupdf_version": fitz.version[0]}


# ── Modelos ──────────────────────────────────────────────────────────────

class SubLocation(BaseModel):
    label: str
    account_name: str
    target_value: str
    bureau_hint: Optional[str] = None  # ej. "TransUnion" -- desambigua cuando el mismo valor
    # aparece en varias columnas de buro (caso comun: mismo balance en 2-3 columnas)
    account_number: Optional[str] = None  # ej. "101099****" -- cuando se da, se usa como ancla
    # PRINCIPAL en vez de account_name. Mas confiable que el nombre cuando el nombre es corto
    # (ej. "BCN", "NCB") y aparece muchas veces en tablas de historial de pago de la misma pagina.
    field_label_text: Optional[str] = None  # ej. "Saldo:" -- texto EXACTO de la etiqueta tal como
    # aparece impreso justo antes del valor. Necesario cuando el mismo valor se repite dentro de
    # la MISMA cuenta en varios campos distintos (caso real: Saldo, Credito alto y Vencido pueden
    # ser identicos). Cuando se da, solo se aceptan valores en la MISMA linea que esa etiqueta.


class EvidenceItem(BaseModel):
    discrepancy_id: str
    title: str
    field_label: str
    sub_locations: list[SubLocation]


class BuildPackageRequest(BaseModel):
    pdf_base64: str
    client_name: str
    report_date: str = ""
    items: list[EvidenceItem]


class LocatedSubResult(BaseModel):
    label: str
    located: bool
    page: Optional[int] = None
    confidence: str
    reason: Optional[str] = None


class ItemResult(BaseModel):
    discrepancy_id: str
    title: str
    evidence_located: bool
    confidence: str
    sub_results: list[LocatedSubResult]


class BuildPackageResponse(BaseModel):
    package_status: str
    pdf_base64: Optional[str] = None
    items: list[ItemResult]
    evidence_items_included: int
    evidence_items_total: int


# ── Localizacion determinística ─────────────────────────────────────────

def find_account_value_pair(doc, account_name: str, target_value: str, bureau_hint: Optional[str] = None, account_number: Optional[str] = None, field_label_text: Optional[str] = None):
    """
    Busca target_value en cada pagina, y busca el ancla en esa MISMA pagina o en la pagina
    ANTERIOR (las cuentas frecuentemente empiezan con su nombre/numero en una pagina y su tabla
    de detalle continua en la siguiente, sin repetir el nombre ahi -- caso real confirmado en
    produccion). El ancla es account_number cuando se proporciona (mas especifico y confiable
    que un nombre corto como "BCN"/"NCB" que puede repetirse muchas veces en una pagina con
    tablas de historial de pago), o account_name en su defecto.

    Si field_label_text se proporciona (ej. "Saldo:"), se descartan los value_matches que no
    esten en la MISMA linea que esa etiqueta -- necesario cuando varios campos de la MISMA
    cuenta comparten el mismo valor (caso real confirmado: Saldo, Credito alto y Vencido pueden
    ser identicos dentro de una sola cuenta).
    """
    anchor_text = account_number if account_number else account_name
    candidates = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        value_matches = page.search_for(target_value)
        if not value_matches:
            continue

        if field_label_text:
            label_matches = page.search_for(field_label_text)
            if label_matches:
                value_matches = [
                    v for v in value_matches
                    if any(abs(v.y0 - lm.y0) <= 4 for lm in label_matches)
                ]
            if not value_matches:
                continue

        name_matches_same_page = page.search_for(anchor_text)
        name_matches_prev_page = doc[page_num - 1].search_for(anchor_text) if page_num > 0 else []

        bureau_x_range = None
        if bureau_hint:
            bureau_matches = page.search_for(bureau_hint)
            if len(bureau_matches) == 1:
                bx = bureau_matches[0]
                bureau_x_range = (bx.x0 - 10, bx.x0 + 140)


        for value_rect in value_matches:
            in_bureau_column = (
                bureau_x_range is not None
                and bureau_x_range[0] <= value_rect.x0 <= bureau_x_range[1]
            )
            # Preferencia 1: nombre en la misma pagina, arriba del valor (misma tabla)
            for name_rect in name_matches_same_page:
                vdist = value_rect.y0 - name_rect.y0
                if -20 <= vdist <= 500:
                    candidates.append((page_num, name_rect, value_rect, vdist, in_bureau_column, 'same_page'))
            # Preferencia 2: nombre en la pagina anterior (cuenta que cruza page break)
            if name_matches_prev_page:
                for name_rect in name_matches_prev_page:
                    candidates.append((page_num, name_rect, value_rect, 99999, in_bureau_column, 'prev_page'))

    if not candidates:
        return None, None, None, 'NONE', 'No se encontro el nombre de cuenta y el valor juntos en la misma pagina ni en paginas consecutivas'

    if bureau_hint:
        in_column = [c for c in candidates if c[4]]
        if len(in_column) == 1:
            page_num, name_rect, value_rect, _, _, source = in_column[0]
            return page_num, name_rect, value_rect, 'HIGH', None, (source == 'same_page')

    # Prioriza same_page sobre prev_page, y dentro de cada grupo la menor distancia vertical
    candidates.sort(key=lambda c: (0 if c[5] == 'same_page' else 1, c[3]))
    best_page, best_name_rect, best_value_rect, _, _, best_source = candidates[0]
    same_page_count = sum(1 for c in candidates if c[0] == best_page)

    if same_page_count == 1:
        confidence = 'HIGH'
    elif same_page_count <= 3:
        confidence = 'MEDIUM'
    else:
        confidence = 'LOW'

    reason = None if confidence == 'HIGH' else f'{same_page_count} posibles ubicaciones en la pagina {best_page + 1}, no se pudo confirmar con certeza cual es la correcta' + (' (bureau_hint no ayudo a desambiguar)' if bureau_hint else ' (considera agregar bureau_hint)')
    return best_page, best_name_rect, best_value_rect, confidence, reason, (best_source == 'same_page')


def crop_evidence_region(doc, page_num, name_rect, value_rect, same_page=True, dpi=200):
    page = doc[page_num]
    highlight = page.add_highlight_annot(value_rect)
    highlight.set_colors(stroke=(1, 0.85, 0))
    highlight.update()

    if same_page:
        # nombre y valor en la misma pagina: recorte normal desde el nombre hasta el valor
        clip = fitz.Rect(
            max(0, min(name_rect.x0, value_rect.x0) - 15),
            max(0, name_rect.y0 - 8),
            min(page.rect.width, max(name_rect.x1, value_rect.x1) + 260),
            min(page.rect.height, value_rect.y1 + 20),
        )
    else:
        # el nombre de la cuenta esta en la pagina anterior (cuenta que cruza el page break) --
        # las coordenadas Y de paginas distintas no son comparables, asi que se recorta desde
        # el inicio de ESTA pagina (donde continua la tabla) hasta el valor, sin usar la
        # posicion vertical del nombre.
        clip = fitz.Rect(
            max(0, value_rect.x0 - 200),
            0,
            min(page.rect.width, value_rect.x1 + 260),
            min(page.rect.height, value_rect.y1 + 20),
        )
    pix = page.get_pixmap(clip=clip, dpi=dpi)
    return pix


# ── Endpoint principal: construir el paquete completo ───────────────────

@app.post("/build_evidence_package", response_model=BuildPackageResponse)
def build_evidence_package(req: BuildPackageRequest):
    try:
        pdf_bytes = base64.b64decode(req.pdf_base64)
    except Exception:
        raise HTTPException(400, "pdf_base64 invalido")

    try:
        source_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise HTTPException(400, f"No se pudo abrir el PDF: {e}")

    output_doc = fitz.open()
    item_results: list[ItemResult] = []
    included_count = 0

    cover = output_doc.new_page(width=612, height=792)
    cover.insert_text((50, 60), "CFPB SUPPORTING EVIDENCE", fontsize=16, fontname="helv")
    cover.insert_text((50, 90), f"Client: {req.client_name}", fontsize=11, fontname="helv")
    cover.insert_text((50, 110), f"Report Date: {req.report_date}", fontsize=11, fontname="helv")
    cover.insert_text((50, 130), f"Evidence Items: {len(req.items)}", fontsize=11, fontname="helv")
    cover.insert_text((50, 160), "Supporting excerpts from the original credit report.", fontsize=10, fontname="helv")
    cover.insert_text((50, 175), "Each highlighted value is reproduced exactly as printed in the original document.", fontsize=10, fontname="helv")

    for item in req.items:
        sub_results = []
        crops = []

        for sub in item.sub_locations:
            page_num, name_rect, value_rect, confidence, reason, same_page = find_account_value_pair(
                source_doc, sub.account_name, sub.target_value, sub.bureau_hint, sub.account_number, sub.field_label_text
            )
            if page_num is not None and confidence == 'HIGH':
                pix = crop_evidence_region(source_doc, page_num, name_rect, value_rect, same_page=same_page)
                crops.append((pix, sub, page_num + 1))
                sub_results.append(LocatedSubResult(
                    label=sub.label, located=True, page=page_num + 1, confidence=confidence
                ))
            else:
                sub_results.append(LocatedSubResult(
                    label=sub.label, located=False,
                    page=(page_num + 1) if page_num is not None else None,
                    confidence=confidence, reason=reason
                ))

        all_located = all(sr.located for sr in sub_results) and len(sub_results) > 0
        overall_confidence = 'HIGH' if all_located else 'LOW'

        item_results.append(ItemResult(
            discrepancy_id=item.discrepancy_id,
            title=item.title,
            evidence_located=all_located,
            confidence=overall_confidence,
            sub_results=sub_results,
        ))

        if all_located:
            included_count += 1
            evidence_page = output_doc.new_page(width=612, height=792)
            evidence_page.insert_text((50, 50), f"EVIDENCE {item.discrepancy_id}", fontsize=13, fontname="helv")
            evidence_page.insert_text((50, 70), item.title, fontsize=11, fontname="helv")
            evidence_page.insert_text((50, 88), f"Field: {item.field_label}", fontsize=9, fontname="helv")

            y_cursor = 110
            for pix, sub, source_page in crops:
                evidence_page.insert_text((50, y_cursor), f"Source: Original Credit Report - Page {source_page} ({sub.label})", fontsize=8, fontname="helv")
                y_cursor += 12
                img_rect = fitz.Rect(50, y_cursor, 50 + pix.width * 0.5, y_cursor + pix.height * 0.5)
                evidence_page.insert_image(img_rect, pixmap=pix)
                y_cursor = img_rect.y1 + 20

    source_doc.close()

    package_status = 'READY' if all(ir.evidence_located for ir in item_results) and len(item_results) > 0 else 'REVIEW_REQUIRED'

    output_pdf_base64 = None
    if included_count > 0:
        output_bytes = output_doc.tobytes()
        output_pdf_base64 = base64.b64encode(output_bytes).decode("utf-8")
    output_doc.close()

    return BuildPackageResponse(
        package_status=package_status,
        pdf_base64=output_pdf_base64,
        items=item_results,
        evidence_items_included=included_count,
        evidence_items_total=len(req.items),
    )


class LocateRequest(BaseModel):
    pdf_base64: str
    search_text: str
    x_min: Optional[float] = None
    x_max: Optional[float] = None
    page_hint: Optional[int] = None


class Match(BaseModel):
    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class LocateResponse(BaseModel):
    query: str
    matches: list[Match]
    confidence: str


@app.post("/locate", response_model=LocateResponse)
def locate_text(req: LocateRequest):
    try:
        pdf_bytes = base64.b64decode(req.pdf_base64)
    except Exception:
        raise HTTPException(400, "pdf_base64 invalido")
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    matches: list[Match] = []
    pages_to_search = [req.page_hint - 1] if req.page_hint else range(len(doc))
    for page_num in pages_to_search:
        if page_num < 0 or page_num >= len(doc):
            continue
        page = doc[page_num]
        for rect in page.search_for(req.search_text):
            if req.x_min is not None and rect.x0 < req.x_min:
                continue
            if req.x_max is not None and rect.x0 > req.x_max:
                continue
            matches.append(Match(page=page_num + 1, x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1))
    doc.close()
    confidence = 'HIGH' if len(matches) == 1 else ('MEDIUM' if len(matches) <= 3 else 'LOW')
    return LocateResponse(query=req.search_text, matches=matches, confidence=confidence)


# ── Diagnostico: texto crudo de una pagina (para depurar layouts nuevos) ──

class PageTextRequest(BaseModel):
    pdf_base64: str
    page: int  # 1-indexed


@app.post("/page_text")
def page_text(req: PageTextRequest):
    try:
        pdf_bytes = base64.b64decode(req.pdf_base64)
    except Exception:
        raise HTTPException(400, "pdf_base64 invalido")
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if req.page < 1 or req.page > len(doc):
        doc.close()
        raise HTTPException(400, f"Pagina {req.page} fuera de rango (PDF tiene {len(doc)} paginas)")
    page = doc[req.page - 1]
    text = page.get_text("text")
    doc.close()
    return {"page": req.page, "text": text}
