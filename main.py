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

def find_account_value_pair(doc, account_name: str, target_value: str, bureau_hint: Optional[str] = None):
    candidates = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        name_matches = page.search_for(account_name)
        value_matches = page.search_for(target_value)
        if not name_matches or not value_matches:
            continue
        bureau_x_range = None
        if bureau_hint:
            bureau_matches = page.search_for(bureau_hint)
            if len(bureau_matches) == 1:
                # columna del buro: desde su encabezado hasta un margen razonable a la derecha
                bx = bureau_matches[0]
                bureau_x_range = (bx.x0 - 10, bx.x0 + 140)
        for name_rect in name_matches:
            for value_rect in value_matches:
                vdist = value_rect.y0 - name_rect.y0
                if -20 <= vdist <= 500:
                    in_bureau_column = (
                        bureau_x_range is not None
                        and bureau_x_range[0] <= value_rect.x0 <= bureau_x_range[1]
                    )
                    candidates.append((page_num, name_rect, value_rect, vdist, in_bureau_column))

    if not candidates:
        return None, None, None, 'NONE', 'No se encontro el nombre de cuenta y el valor juntos en ninguna pagina'

    # Si se dio bureau_hint y exactamente un candidato cae dentro de esa columna, se usa ese
    # directamente con confianza HIGH -- esto resuelve el caso comun de un mismo valor repetido
    # en varias columnas de buro (ej. mismo balance en TU y EXP).
    if bureau_hint:
        in_column = [c for c in candidates if c[4]]
        if len(in_column) == 1:
            page_num, name_rect, value_rect, _, _ = in_column[0]
            return page_num, name_rect, value_rect, 'HIGH', None

    candidates.sort(key=lambda c: c[3])
    best_page, best_name_rect, best_value_rect, _, _ = candidates[0]
    same_page_count = sum(1 for c in candidates if c[0] == best_page)

    if same_page_count == 1:
        confidence = 'HIGH'
    elif same_page_count <= 3:
        confidence = 'MEDIUM'
    else:
        confidence = 'LOW'

    reason = None if confidence == 'HIGH' else f'{same_page_count} posibles ubicaciones en la pagina {best_page + 1}, no se pudo confirmar con certeza cual es la correcta' + (' (bureau_hint no ayudo a desambiguar)' if bureau_hint else ' (considera agregar bureau_hint)')
    return best_page, best_name_rect, best_value_rect, confidence, reason


def crop_evidence_region(doc, page_num, name_rect, value_rect, dpi=200):
    page = doc[page_num]
    highlight = page.add_highlight_annot(value_rect)
    highlight.set_colors(stroke=(1, 0.85, 0))
    highlight.update()

    clip = fitz.Rect(
        max(0, min(name_rect.x0, value_rect.x0) - 15),
        max(0, name_rect.y0 - 8),
        min(page.rect.width, max(name_rect.x1, value_rect.x1) + 260),
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
            page_num, name_rect, value_rect, confidence, reason = find_account_value_pair(
                source_doc, sub.account_name, sub.target_value, sub.bureau_hint
            )
            if page_num is not None and confidence == 'HIGH':
                pix = crop_evidence_region(source_doc, page_num, name_rect, value_rect)
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
