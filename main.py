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
    field_label_candidates: Optional[list[str]] = None  # variantes de field_label_text -- el
    # mismo dato se llama distinto segun la plataforma de origen del reporte (ej. "Balance Owed"
    # en SmartCredit vs "Saldo:"/"Balance:"/"Current Balance:" en otras). Se prueban TODAS: se
    # acepta un valor que este en la misma linea que CUALQUIERA de las etiquetas candidatas. Se
    # combina con field_label_text si ambos se proporcionan (no son mutuamente excluyentes).
    highlight_full_row: bool = False  # cuando el argumento no depende de aislar el valor de UN
    # buro especifico (ej. "esta cuenta aparece bajo mas de un nombre"), resalta toda la fila de
    # valores en vez de intentar desambiguar por columna de buro. Requiere field_label_text.
    # Mas simple y seguro quen que forzar una desambiguacion incierta entre varias coincidencias.


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

BUREAU_NAMES = ["TransUnion", "Experian", "Equifax"]

# Limite empirico para candidatos en la MISMA pagina (calibrado con 3 reportes reales:
# SmartCredit, IdentityIQ, MyFreeScoreNow). Una fila de tabla real no separa etiqueta y valor
# por mas de esto en ninguna de las 3 plataformas probadas.
SAME_PAGE_MAX_VDIST = 300

# Limite de respaldo para candidatos de PAGINA ANTERIOR cuando el ancla (account_name /
# account_number) NO es unica en todo el documento -- ver _is_unique_anchor. Se probo como
# limite fijo universal (valor que "resuelve" el caso ambiguo de IdentityIQ) pero un reporte de
# MyFreeScoreNow con una seccion de recap ("Payment Summary") antes de la tabla real empuja los
# valores legitimos de una cuenta que SI cruza el corte de pagina mas alla de cualquier limite
# fijo razonable -- no hay un numero que sirva para las 3 plataformas a la vez. Por eso ya no es
# el unico filtro: solo se aplica cuando el ancla es ambigua (ver mas abajo).
PREV_PAGE_MAX_Y = 300


def _is_unique_anchor(doc, anchor_text: str) -> bool:
    """
    True si anchor_text aparece EXACTAMENTE UNA VEZ en TODO el documento. Cuando esto es cierto,
    cualquier coincidencia de anchor_text en la pagina anterior es, por definicion, la cuenta
    correcta -- sin importar que tan lejos este el valor en la pagina siguiente (una cuenta puede
    legitimamente extenderse varios cientos de puntos si el layout de esa plataforma intercala
    tablas de resumen antes de los datos detallados). Cuando el ancla SI se repite en el
    documento (caso real: "NCB" tambien aparece dentro de un comentario "Vendido a: NCB
    MANAGEMENT" de OTRA cuenta), no hay forma de confirmar sin ambiguedad que la ocurrencia de la
    pagina anterior es la cuenta real -- ahi se vuelve a exigir el limite PREV_PAGE_MAX_Y como
    filtro de seguridad, y si aun asi no alcanza, se reporta como ambiguo (MEDIUM/REVIEW_REQUIRED)
    en vez de adivinar. Esto mantiene el principio del proyecto: preferir REVIEW_REQUIRED antes
    que una captura incorrecta, sin sacrificar los casos legitimos que SI tienen un ancla unica.

    NOTA: se probo tambien filtrar aqui las coincidencias que "parecen" mencion incidental
    (ej. contar cuantas palabras preceden al ancla en su linea, para distinguir un encabezado
    real de un comentario tipo "Vendido a: NCB MANAGEMENT") -- se descarto porque el mismo patron
    estructural (2 palabras + dos puntos antes del ancla) tambien aparece en etiquetas de campo
    completamente legitimas y comunes en los 3 reportes (ej. "Cuenta #: 42668417****"), asi que
    ese filtro rechazaba anclas reales tan seguido como rechazaba las incidentales -- no
    discrimina. Sin una señal geometrica/textual que sí discrimine de forma confiable, se cuentan
    TODAS las coincidencias literales; el efecto practico es que un ancla que colisiona con una
    mencion incidental dentro del documento (caso real: "NCB") no calificara como unica, y su
    candidato de pagina anterior queda sujeto al limite PREV_PAGE_MAX_Y como red de seguridad --
    en el peor caso, resulta en MEDIUM/REVIEW_REQUIRED en vez de HIGH, nunca en una captura
    incorrecta.
    """
    count = 0
    for page_num in range(len(doc)):
        count += len(doc[page_num].search_for(anchor_text))
        if count > 1:
            return False
    return count == 1


def _bureau_x_ranges_near(page, ref_y0: float, max_dist: float = 500):
    """
    Ubica el renglon de encabezados TransUnion / Experian / Equifax mas cercano hacia abajo de
    ref_y0 (tipicamente el nombre de la cuenta), agrupando por renglon (mismo y0, tolerancia
    3pt) y quedandose con el renglon que tenga MAS burós encontrados juntos (idealmente los 3).
    Devuelve {nombre_buro: (x_min, x_max)} calculando los limites de columna como el PUNTO MEDIO
    entre encabezados vecinos -- no un offset fijo en pixeles, que varia segun la plataforma de
    origen del reporte y en la practica no coincide con donde cae el valor real (confirmado con
    datos reales: el valor puede caer 40-50pt a la izquierda del inicio del texto del encabezado).

    Busca en AMBAS direcciones (arriba y abajo de ref_y0), no solo hacia abajo. ref_y0 es el y0
    del ancla usada (account_name o account_number segun cual se haya pasado) -- y cuando el
    ancla es account_number, su fila esta casi siempre POR DEBAJO del renglon de encabezados de
    buro (el encabezado "TransUnion/Experian/Equifax" viene primero, el numero de cuenta debajo),
    al reves de cuando el ancla es account_name (que tipicamente esta arriba del encabezado de
    buro). Restringir la busqueda a una sola direccion causaba que, con account_number como
    ancla, nunca se encontrara el renglon de encabezados -- caso real confirmado (SmartCredit,
    Capital One #515676: bureau_hint quedaba sin efecto y la deteccion caia a MEDIUM/LOW en vez
    de HIGH).
    """
    found = []
    for bureau in BUREAU_NAMES:
        for r in page.search_for(bureau):
            if abs(r.y0 - ref_y0) <= max_dist:
                found.append((bureau, r))
    if not found:
        return {}

    rows = {}
    for b, r in found:
        key = round(r.y0 / 3)
        rows.setdefault(key, []).append((b, r))
    best_row = max(rows.values(), key=len)
    best_row.sort(key=lambda br: br[1].x0)

    ranges = {}
    for i, (b, r) in enumerate(best_row):
        left = (best_row[i - 1][1].x1 + r.x0) / 2 if i > 0 else r.x0 - 80
        right = (best_row[i + 1][1].x0 + r.x1) / 2 if i < len(best_row) - 1 else r.x1 + 80
        ranges[b] = (left, right)
    return ranges


def find_account_value_pair(doc, account_name: str, target_value: str, bureau_hint: Optional[str] = None, account_number: Optional[str] = None, field_label_text: Optional[str] = None, field_label_candidates: Optional[list] = None):
    """
    Busca target_value en cada pagina, y busca el ancla en esa MISMA pagina o en la pagina
    ANTERIOR (las cuentas frecuentemente empiezan con su nombre/numero en una pagina y su tabla
    de detalle continua en la siguiente, sin repetir el nombre ahi -- caso real confirmado en
    produccion). El ancla es account_number cuando se proporciona (mas especifico y confiable
    que un nombre corto como "BCN"/"NCB" que puede repetirse muchas veces en una pagina con
    tablas de historial de pago), o account_name en su defecto.

    Si field_label_text y/o field_label_candidates se proporcionan (ej. "Saldo:", "Balance Owed"),
    se descartan los value_matches que no esten en la MISMA linea que AL MENOS UNA de esas
    etiquetas -- necesario cuando varios campos de la MISMA cuenta comparten el mismo valor (caso
    real confirmado: Saldo, Credito alto y Vencido pueden ser identicos dentro de una sola cuenta)
    y cuando la etiqueta exacta varia segun la plataforma de origen del reporte.
    """
    anchor_text = account_number if account_number else account_name
    anchor_is_unique = _is_unique_anchor(doc, anchor_text)
    label_variants = []
    if field_label_text:
        label_variants.append(field_label_text)
    if field_label_candidates:
        label_variants.extend(c for c in field_label_candidates if c not in label_variants)

    candidates = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        value_matches = page.search_for(target_value)
        if not value_matches:
            continue

        if label_variants:
            label_matches = []
            for lbl in label_variants:
                label_matches.extend(page.search_for(lbl))
            if label_matches:
                value_matches = [
                    v for v in value_matches
                    if any(abs(v.y0 - lm.y0) <= 4 for lm in label_matches)
                ]
            if not value_matches:
                continue

        name_matches_same_page = page.search_for(anchor_text)
        name_matches_prev_page = doc[page_num - 1].search_for(anchor_text) if page_num > 0 else []

        for value_rect in value_matches:
            # Preferencia 1: nombre en la misma pagina, arriba del valor (misma tabla)
            for name_rect in name_matches_same_page:
                vdist = value_rect.y0 - name_rect.y0
                if -20 <= vdist <= SAME_PAGE_MAX_VDIST:
                    in_bureau_column = False
                    if bureau_hint:
                        ranges = _bureau_x_ranges_near(page, name_rect.y0)
                        rng = ranges.get(bureau_hint)
                        in_bureau_column = rng is not None and rng[0] <= value_rect.x0 <= rng[1]
                    candidates.append((page_num, name_rect, value_rect, vdist, in_bureau_column, 'same_page'))
            # Preferencia 2: nombre en la pagina anterior (cuenta que cruza page break). Si el
            # ancla es UNICA en todo el documento, se acepta sin importar la distancia -- no hay
            # otra cuenta con la que se pueda estar confundiendo (caso real confirmado: cuentas de
            # MyFreeScoreNow con una tabla de resumen "Payment Summary" antes de los datos reales,
            # que empuja el valor cientos de puntos mas abajo en la pagina siguiente). Si el ancla
            # SI se repite en el documento, se exige que el valor este cerca del INICIO de esta
            # pagina como filtro de seguridad -- sin esto, un nombre que aparece de pasada en un
            # comentario de OTRA cuenta (ej. "Vendido a: NCB MANAGEMENT" dentro de los comentarios
            # de una cuenta Santander) se emparejaba con CUALQUIER valor que matcheara en la
            # pagina siguiente, sin importar que tan lejos estuviera -- caso real confirmado
            # (JPMCB/NCB, reporte IdentityIQ de Rangel Peñaranda).
            if name_matches_prev_page and (anchor_is_unique or value_rect.y0 <= PREV_PAGE_MAX_Y):
                for name_rect in name_matches_prev_page:
                    candidates.append((page_num, name_rect, value_rect, 99999, False, 'prev_page'))

    if not candidates:
        return None, None, None, 'NONE', 'No se encontro el nombre de cuenta y el valor juntos en la misma pagina ni en paginas consecutivas'

    if bureau_hint:
        in_column = [c for c in candidates if c[4]]
        # Igual que mas abajo: deduplicar por VALOR, no por par (ancla, valor). Cuando el ancla
        # (tipicamente account_number) se imprime identico bajo mas de una columna de buro en la
        # misma fila (caso real: TransUnion y Experian muestran el mismo numero de cuenta
        # enmascarado), un solo valor real en la columna correcta genera un candidato "in_column"
        # por cada copia del ancla -- sin deduplicar, len(in_column) nunca da 1 y un match
        # completamente inequivoco terminaba en MEDIUM (caso real confirmado: SmartCredit,
        # Capital One #515676, Balance Owed).
        distinct_in_column = {}
        for c in in_column:
            key = (round(c[2].x0, 1), round(c[2].y0, 1))
            distinct_in_column.setdefault(key, c)
        if len(distinct_in_column) == 1:
            page_num, name_rect, value_rect, _, _, source = next(iter(distinct_in_column.values()))
            return page_num, name_rect, value_rect, 'HIGH', None, (source == 'same_page')

    # Prioriza same_page sobre prev_page, y dentro de cada grupo la menor distancia vertical
    candidates.sort(key=lambda c: (0 if c[5] == 'same_page' else 1, c[3]))
    best_page, best_name_rect, best_value_rect, _, _, best_source = candidates[0]
    # Cuenta VALORES distintos en la pagina, no pares (ancla, valor). El mismo account_number
    # suele imprimirse identico bajo mas de una columna de buro (ej. TransUnion y Experian
    # muestran el mismo numero enmascarado) -- eso genera varios name_rect para UN solo
    # value_rect real, y contar pares infla la ambiguedad de forma artificial (caso real
    # confirmado: JPMCB, reporte IdentityIQ -- un unico valor candidato bajaba a MEDIUM solo
    # porque su ancla aparecia 2 veces en la pagina anterior).
    same_page_count = len({(round(c[2].x0, 1), round(c[2].y0, 1)) for c in candidates if c[0] == best_page})

    if same_page_count == 1:
        confidence = 'HIGH'
    elif same_page_count <= 3:
        confidence = 'MEDIUM'
    else:
        confidence = 'LOW'

    reason = None if confidence == 'HIGH' else f'{same_page_count} posibles ubicaciones en la pagina {best_page + 1}, no se pudo confirmar con certeza cual es la correcta' + (' (bureau_hint no ayudo a desambiguar)' if bureau_hint else ' (considera agregar bureau_hint)')
    return best_page, best_name_rect, best_value_rect, confidence, reason, (best_source == 'same_page')


def find_full_row_evidence(doc, account_name: str, field_label_text: str):
    """
    Modo mas simple y robusto para casos donde no hace falta aislar el valor de UN buro
    especifico (ej. discrepancias sobre identidad de la cuenta, no sobre un dato puntual).
    Busca account_name (misma pagina o pagina anterior, igual que el ancla normal), luego
    field_label_text en la pagina resultante, cercano al nombre. Devuelve la fila COMPLETA
    (todos los valores en esa linea) para resaltarla entera, sin desambiguar por columna.

    Igual que find_account_value_pair: revisa TODAS las paginas antes de decidir (no se
    detiene en la primera coincidencia) y solo confirma con HIGH si exactamente una pagina
    califica -- si mas de una pagina tiene una coincidencia valida, es ambiguo y se reporta
    como tal en vez de adivinar cual es la correcta.
    """
    anchor_is_unique = _is_unique_anchor(doc, account_name)
    page_candidates = []  # una entrada por pagina que tenga al menos un candidato valido

    for page_num in range(len(doc)):
        page = doc[page_num]
        label_matches = page.search_for(field_label_text)
        if not label_matches:
            continue

        name_matches_same_page = page.search_for(account_name)
        name_matches_prev_page = doc[page_num - 1].search_for(account_name) if page_num > 0 else []

        candidates = []
        for label_rect in label_matches:
            for name_rect in name_matches_same_page:
                vdist = label_rect.y0 - name_rect.y0
                if -20 <= vdist <= SAME_PAGE_MAX_VDIST:
                    candidates.append((label_rect, vdist, 'same_page'))
            if name_matches_prev_page and (anchor_is_unique or label_rect.y0 <= PREV_PAGE_MAX_Y):
                candidates.append((label_rect, 99999, 'prev_page'))

        if not candidates:
            continue

        candidates.sort(key=lambda c: (0 if c[2] == 'same_page' else 1, c[1]))
        best_label_rect, _, source = candidates[0]
        page_candidates.append((page_num, best_label_rect, source))

    if not page_candidates:
        return None, None, None, None, 'No se encontro el nombre de cuenta y la etiqueta de campo juntos en ninguna pagina'

    if len(page_candidates) > 1:
        pages_found = [p[0] + 1 for p in page_candidates]
        return None, None, None, None, f'La combinacion de nombre de cuenta + etiqueta aparece en mas de una pagina ({pages_found}) -- no se puede confirmar cual es la correcta sin ambiguedad'

    page_num, best_label_rect, source = page_candidates[0]
    page = doc[page_num]

    # Ancho real de la fila: el borde derecho del ultimo texto que comparte la misma linea
    # (mismo y0, tolerancia de 3pt), no el ancho completo de la pagina -- evita que el
    # recorte/highlight se corte visualmente en el borde de la pagina.
    words = page.get_text("words")
    row_right_edge = best_label_rect.x1
    for w in words:
        wx0, wy0, wx1, wy1 = w[0], w[1], w[2], w[3]
        if abs(wy0 - best_label_rect.y0) <= 3:
            row_right_edge = max(row_right_edge, wx1)
    row_right_edge = min(page.rect.width - 10, row_right_edge + 10)

    row_rect = fitz.Rect(best_label_rect.x0, best_label_rect.y0, row_right_edge, best_label_rect.y1)
    return page_num, best_label_rect, row_rect, (source == 'same_page'), None


def crop_full_row(doc, page_num, label_rect, row_rect, dpi=200):
    page = doc[page_num]
    highlight = page.add_highlight_annot(row_rect)
    highlight.set_colors(stroke=(1, 0.85, 0))
    highlight.update()

    clip = fitz.Rect(
        max(0, label_rect.x0 - 15),
        max(0, label_rect.y0 - 8),
        min(page.rect.width - 5, row_rect.x1 + 15),
        min(page.rect.height, label_rect.y1 + 20),
    )
    pix = page.get_pixmap(clip=clip, dpi=dpi)
    return pix


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
            if sub.highlight_full_row:
                if not sub.field_label_text:
                    sub_results.append(LocatedSubResult(
                        label=sub.label, located=False, confidence='NONE',
                        reason='highlight_full_row requiere field_label_text'
                    ))
                    continue
                page_num, label_rect, row_rect, same_page, fail_reason = find_full_row_evidence(
                    source_doc, sub.account_name, sub.field_label_text
                )
                if page_num is not None:
                    pix = crop_full_row(source_doc, page_num, label_rect, row_rect)
                    crops.append((pix, sub, page_num + 1))
                    sub_results.append(LocatedSubResult(
                        label=sub.label, located=True, page=page_num + 1, confidence='HIGH'
                    ))
                else:
                    sub_results.append(LocatedSubResult(
                        label=sub.label, located=False, confidence='NONE',
                        reason=fail_reason
                    ))
                continue

            page_num, name_rect, value_rect, confidence, reason, same_page = find_account_value_pair(
                source_doc, sub.account_name, sub.target_value, sub.bureau_hint, sub.account_number, sub.field_label_text, sub.field_label_candidates
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
                # Conversion correcta de pixeles a puntos: los recortes se capturan a 200 DPI
                # (ver dpi=200 en crop_evidence_region/crop_full_row), y 1 punto = 200/72 pixeles
                # a esa resolucion. Un factor fijo de 0.5 (usado antes) es incorrecto y sobre-
                # dimensiona la imagen ~40%, arriesgando que se salga de la pagina en recortes
                # anchos como el modo de fila completa.
                px_to_pt = 72.0 / 200.0
                img_w = pix.width * px_to_pt
                img_h = pix.height * px_to_pt
                img_rect = fitz.Rect(50, y_cursor, 50 + img_w, y_cursor + img_h)
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
