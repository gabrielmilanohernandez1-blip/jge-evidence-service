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
    account_name_alternatives: Optional[list[str]] = None  # variantes cortas/abreviadas de
    # account_name -- necesario porque el nombre que el analisis de IA usa (normalizado, a veces
    # el nombre legal completo del acreedor, ej. "NCB MANAGEMENT SERVICES") frecuentemente NO es
    # el texto literal que aparece impreso como encabezado de la seccion de esa cuenta en el PDF
    # (caso real confirmado, reporte de Rangel Peñaranda: el encabezado real es solo "NCB", igual
    # que "SANTANDER" en vez de "SANTANDER CONSUMER USA"). Se prueban TODAS como candidatas de
    # ancla, igual que target_value_alternatives. Ver tambien _is_reference_mention: el nombre
    # completo suele SI aparecer en el documento, pero como referencia dentro de OTRA cuenta
    # (ej. "NCB (Acreedor original: ... SANTANDER CONSUMER USA INC)"), nunca como encabezado
    # propio -- de ahi que haga falta la forma corta ademas del filtro de mencion-referencial.
    field_label_text: Optional[str] = None  # ej. "Saldo:" -- texto EXACTO de la etiqueta tal como
    # aparece impreso justo antes del valor. Necesario cuando el mismo valor se repite dentro de
    # la MISMA cuenta en varios campos distintos (caso real: Saldo, Credito alto y Vencido pueden
    # ser identicos). Cuando se da, solo se aceptan valores en la MISMA linea que esa etiqueta.
    field_label_candidates: Optional[list[str]] = None  # variantes de field_label_text -- el
    # mismo dato se llama distinto segun la plataforma de origen del reporte (ej. "Balance Owed"
    # en SmartCredit vs "Saldo:"/"Balance:"/"Current Balance:" en otras). Se prueban TODAS: se
    # acepta un valor que este en la misma linea que CUALQUIERA de las etiquetas candidatas. Se
    # combina con field_label_text si ambos se proporcionan (no son mutuamente excluyentes).
    target_value_alternatives: Optional[list[str]] = None  # variantes de formato de target_value
    # (ej. "0.00" cuando target_value es "$0.00") -- el mismo n8n manifest ya las genera pero
    # este modelo las descartaba en silencio (Pydantic ignora campos no declarados), asi que
    # nunca se usaban en la busqueda real. Ver find_account_value_pair.
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
    max_package_size_mb: Optional[float] = None  # limite de tamano POR ARCHIVO de salida. Si el
    # PDF combinado de todas las discrepancias confirmadas superaria este limite, se divide en
    # varios archivos ("paquetes"), cada uno por debajo del limite, sin perder ningun item -- ver
    # DEFAULT_MAX_PACKAGE_SIZE_MB y la logica de empaquetado en build_evidence_package. None o <=0
    # usa el default.


# Limite de tamano por defecto para CADA archivo PDF de evidencia generado, en MB. 9, no 10:
# margen de seguridad bajo el limite real de subida del portal de quejas de CFPB
# (portal.consumerfinance.gov), que rechaza archivos que superan 10MB. Confirmado con un caso
# real (24/08/2026): un paquete de ~8.78MB "en crudo" (suma simple de 4 archivos) fue rechazado
# por el portal citando ese limite de 10MB -- la explicacion mas probable es overhead de
# codificacion/transmision del propio portal (ej. base64 agrega ~33%) empujando el tamano
# efectivo por encima del limite aunque el archivo en si mida menos de 10MB. 9MB deja margen sin
# sacrificar practicamente nada de capacidad util. Enviado por n8n en cada request (parametro
# `max_package_size_mb`), asi que se puede ajustar sin tocar este archivo si CFPB cambia su
# limite.
DEFAULT_MAX_PACKAGE_SIZE_MB = 9.0


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


class PackageFile(BaseModel):
    package_number: int  # 1-indexado
    pdf_base64: str
    size_bytes: int
    items_included: list[str]  # discrepancy_id de cada item incluido en ESTE archivo especifico


class BuildPackageResponse(BaseModel):
    package_status: str
    packages: list[PackageFile] = []  # 1 o mas archivos PDF, cada uno <= max_package_size_mb
    # (salvo un item individual que por si solo ya supere el limite -- ver nota en el empaquetado;
    # eso nunca se parte a la mitad, se entrega completo en su propio archivo).
    package_count: int = 0
    pdf_base64: Optional[str] = None  # DEPRECATED: alias de packages[0].pdf_base64 (o None si no
    # hay ningun paquete), se mantiene solo por compatibilidad hacia atras mientras el llamador
    # (n8n) no este actualizado para leer 'packages'. El llamador nuevo debe usar 'packages'.
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


# ── Formato del PDF de salida (margenes, tipografia, paginacion) ────────
# Estandar de documento legal de 1 pulgada de margen en los 4 lados (612x792pt = carta US),
# titulos centrados en negrita, numeracion de pagina y encabezado repetido en cada pagina de
# evidencia -- antes todo el texto se insertaba a mano en x=50 fijo (menos de 1 pulgada), sin
# limite de ancho para las imagenes (podian salirse del margen derecho) y sin numeracion ni
# encabezado de continuidad -- reportado por Gabriel como "no esta justificado, no esta
# alineado" (Ronda 17).
PAGE_W, PAGE_H = 612, 792
MARGIN = 72  # 1 pulgada
CONTENT_W = PAGE_W - 2 * MARGIN
FOOTER_SPACE = 24


def _centered_x(text: str, fontsize: float, fontname: str = "helv", page_width: float = PAGE_W) -> float:
    """x0 para que `text` quede centrado horizontalmente en la pagina."""
    w = fitz.get_text_length(text, fontname=fontname, fontsize=fontsize)
    return max(MARGIN, (page_width - w) / 2)


def _new_evidence_page(output_doc, client_name: str, continuation: bool = False):
    """
    Crea una pagina de evidencia nueva con el encabezado repetido (cliente + nombre del
    paquete) en la esquina superior -- practica estandar en anexos legales de mas de 1 pagina,
    para que una pagina no quede huerfana/sin identificar si se separa del resto del paquete al
    imprimirse o archivarse por separado.
    """
    page = output_doc.new_page(width=PAGE_W, height=PAGE_H)
    suffix = " (cont.)" if continuation else ""
    header = f"{client_name} — CFPB Supporting Evidence{suffix}"
    page.insert_text((MARGIN, 40), header, fontsize=8, fontname="helv", color=(0.45, 0.45, 0.45))
    return page


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


# Frases que indican que el nombre encontrado justo despues es una REFERENCIA a otro acreedor
# (historial de venta/transferencia de deuda), no el encabezado de su propia seccion de cuenta.
# Caso real confirmado (Rangel Peñaranda, reporte IdentityIQ): "SANTANDER CONSUMER USA" aparece
# UNA sola vez en todo el documento, dentro del encabezado de la cuenta NCB: "NCB (Acreedor
# original: 14 SANTANDER CONSUMER USA INC)". Usar esa ocurrencia como ancla de Santander
# encontraba el Saldo: real de NCB (pagina siguiente) y lo entregaba como evidencia de Santander
# -- HIGH confidence, pagina y cuenta EQUIVOCADAS. Simetricamente, "NCB" aparece dentro de los
# comentarios de la propia cuenta Santander ("...Saldo impagado reportado como Vendido a: NCB
# MANAGEMENT") y por la misma razon podia capturar el Saldo: de OTRA cuenta (STARTAUTOF, que
# empieza mas abajo en esa misma pagina) etiquetado como si fuera de NCB.
# NOTA: ya se probo (ver docstring de _is_unique_anchor) un filtro estructural generico basado en
# "N palabras + dos puntos antes del ancla" y se descarto por rechazar anclas reales igual de
# seguido (ej. "Cuenta #:"). Esta lista es deliberadamente mas angosta -- frases especificas de
# venta/transferencia de deuda, no un patron estructural -- para no repetir ese problema.
REFERENCE_MENTION_MARKERS = [
    'acreedor original', 'original creditor', 'vendido a', 'sold to',
    'comprado por', 'purchased by', 'transferido a', 'assigned to',
]


def _is_reference_mention(page, rect) -> bool:
    """
    True si el texto inmediatamente antes de `rect` en la misma linea contiene una de las frases
    de REFERENCE_MENTION_MARKERS -- es decir, el nombre encontrado en `rect` esta siendo
    mencionado como el acreedor ORIGINAL/ANTERIOR de OTRA cuenta, no como el encabezado de su
    propia seccion. Se usa para descartar esas ocurrencias como ancla antes de emparejarlas con
    un valor/etiqueta cercano.
    """
    preceding_rect = fitz.Rect(max(0, rect.x0 - 220), rect.y0 - 2, rect.x0, rect.y1 + 2)
    preceding_text = page.get_textbox(preceding_rect).lower()
    return any(marker in preceding_text for marker in REFERENCE_MENTION_MARKERS)


def _name_candidates(account_name: str, account_name_alternatives: Optional[list] = None) -> list:
    names = [account_name]
    if account_name_alternatives:
        names.extend(n for n in account_name_alternatives if n and n not in names)
    return names


def _search_name_filtered(page, names: list) -> list:
    """search_for cada candidato de nombre en `page`, descartando menciones referenciales."""
    matches = []
    for name in names:
        for r in page.search_for(name):
            if not _is_reference_mention(page, r):
                matches.append(r)
    return matches


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


import re as _re


def _date_format_alternatives(value: str) -> list:
    """
    Si value tiene forma de fecha ISO (YYYY-MM-DD, el formato que usa el pipeline de analisis
    internamente), genera las variantes DD/MM/YYYY y MM/DD/YYYY -- el formato en que la fecha
    realmente aparece IMPRESA en el PDF original. Sin esto, target_value="2023-10-16" nunca
    hace match contra el texto real del reporte ("16/10/2023"), y CUALQUIER discrepancia de
    fecha de apertura (un tipo de disputa muy comun) queda sin evidencia localizable aunque el
    dato SI este presente y sea correcto. No inventa ni asume cual de las 2 variantes es la
    real -- prueba ambas como candidatas de busqueda, igual que target_value_alternatives.
    """
    m = _re.match(r'^(\d{4})-(\d{2})-(\d{2})$', str(value or '').strip())
    if not m:
        return []
    yyyy, mm, dd = m.group(1), m.group(2), m.group(3)
    return [f'{dd}/{mm}/{yyyy}', f'{mm}/{dd}/{yyyy}']


def _amount_format_alternatives(value: str) -> list:
    """
    Si value tiene forma de monto en formato US ("$1,234.56", "$0.00"), genera las variantes en
    formato europeo que estas plataformas usan en algunas secciones del MISMO PDF (separador de
    miles y decimales invertidos, simbolo de moneda al final -- ej. "1.234,56 $", "0,00 $", y la
    variante con espacio como separador de miles vista en este mismo reporte: "8 633,00 $"). Sin
    esto, un balance identico puede no encontrarse solo porque esa seccion en particular del PDF
    lo imprime con el formato "opuesto" al que usa el analyzer internamente (Leccion aprendida
    #2 del proyecto: "el mismo PDF puede usar formatos de numero distintos en secciones
    distintas").
    """
    m = _re.match(r'^\$?\s*([\d,]*\d)\.(\d{2})$', str(value or '').strip())
    if not m:
        return []
    whole, cents = m.group(1), m.group(2)
    return [
        f'{whole.replace(",", ".")},{cents} $',
        f'{whole.replace(",", " ")},{cents} $',
    ]


def find_account_value_pair(doc, account_name: str, target_value: str, bureau_hint: Optional[str] = None, account_number: Optional[str] = None, field_label_text: Optional[str] = None, field_label_candidates: Optional[list] = None, target_value_alternatives: Optional[list] = None, account_name_alternatives: Optional[list] = None):
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
    using_account_number = bool(account_number)
    anchor_text = account_number if using_account_number else account_name
    anchor_names = _name_candidates(account_name, account_name_alternatives)
    if using_account_number:
        anchor_is_unique = _is_unique_anchor(doc, anchor_text)
    else:
        # Unico si, sumando TODOS los nombres candidatos (account_name + alternativas) y
        # descartando menciones referenciales (ver _is_reference_mention), aparece una sola vez
        # en todo el documento.
        total = 0
        for pn in range(len(doc)):
            total += len(_search_name_filtered(doc[pn], anchor_names))
            if total > 1:
                break
        anchor_is_unique = total == 1
    label_variants = []
    if field_label_text:
        label_variants.append(field_label_text)
    if field_label_candidates:
        label_variants.extend(c for c in field_label_candidates if c not in label_variants)

    # Variantes de target_value a probar: el valor tal cual, las alternativas explicitas del
    # manifest (ej. formato de numero "$0.00" vs "0.00"), y -- si target_value tiene forma de
    # fecha ISO -- las variantes DD/MM/YYYY y MM/DD/YYYY (ver _date_format_alternatives). Se
    # prueban TODAS por pagina y se combinan los matches; no importa cual variante hizo match,
    # el rect encontrado es el mismo dato real impreso en el documento.
    value_variants = [target_value]
    if target_value_alternatives:
        value_variants.extend(v for v in target_value_alternatives if v not in value_variants)
    for alt in _date_format_alternatives(target_value) + _amount_format_alternatives(target_value):
        if alt not in value_variants:
            value_variants.append(alt)

    candidates = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        value_matches = []
        for variant in value_variants:
            for m in page.search_for(variant):
                value_matches.append(m)
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

        if using_account_number:
            name_matches_same_page = page.search_for(anchor_text)
            name_matches_prev_page = doc[page_num - 1].search_for(anchor_text) if page_num > 0 else []
        else:
            name_matches_same_page = _search_name_filtered(page, anchor_names)
            name_matches_prev_page = _search_name_filtered(doc[page_num - 1], anchor_names) if page_num > 0 else []

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
        return None, None, None, 'NONE', 'No se encontro el nombre de cuenta y el valor juntos en la misma pagina ni en paginas consecutivas', False

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


def find_full_row_evidence(doc, account_name: str, field_label_text: Optional[str] = None, field_label_candidates: Optional[list] = None, account_name_alternatives: Optional[list] = None, account_number: Optional[str] = None):
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

    Fix 31/08/2026: acepta account_number opcional y, cuando se da, lo usa como ANCLA PRINCIPAL
    en vez de account_name -- misma logica ya probada en find_account_value_pair. El modelo
    SubLocation ya aceptaba este campo y el llamador de n8n ya lo enviaba (Analyzer, casos
    'tardios'/'tipo_cuenta_inconsistente' desde el 27/08/2026; comparador, Ronda 2 CFPB desde el
    31/08/2026) pero esta funcion nunca lo recibia en su firma ni el dispatcher se lo pasaba --
    se ignoraba en silencio. Un nombre de acreedor corto o generico (ej. "JPMCB CARD SERVICES")
    puede aparecer en muchas paginas del mismo reporte (resumen, detalle por buro, historial de
    pagos, mencion como acreedor original de otra cuenta), y el numero de cuenta es mucho mas
    especifico. El caso de identidad ambigua entre 2+ nombres candidatos (ej. BCN/NCB)
    deliberadamente NO envia account_number porque pueden compartir el mismo numero enmascarado
    entre si (ver Construir Evidence Manifest.js) -- ese caso sigue sin account_number y por lo
    tanto sin cambio de comportamiento aqui.
    """
    label_variants = []
    if field_label_text:
        label_variants.append(field_label_text)
    if field_label_candidates:
        label_variants.extend(c for c in field_label_candidates if c not in label_variants)
    if not label_variants:
        return None, None, None, None, 'find_full_row_evidence requiere field_label_text o field_label_candidates'

    using_account_number = bool(account_number)
    anchor_names = _name_candidates(account_name, account_name_alternatives)
    if using_account_number:
        anchor_is_unique = _is_unique_anchor(doc, account_number)
    else:
        total = 0
        for pn in range(len(doc)):
            total += len(_search_name_filtered(doc[pn], anchor_names))
            if total > 1:
                break
        anchor_is_unique = total == 1
    page_candidates = []  # una entrada por pagina que tenga al menos un candidato valido

    for page_num in range(len(doc)):
        page = doc[page_num]
        label_matches = []
        for variant in label_variants:
            label_matches.extend(page.search_for(variant))
        if not label_matches:
            continue

        if using_account_number:
            name_matches_same_page = page.search_for(account_number)
            name_matches_prev_page = doc[page_num - 1].search_for(account_number) if page_num > 0 else []
        else:
            name_matches_same_page = _search_name_filtered(page, anchor_names)
            name_matches_prev_page = _search_name_filtered(doc[page_num - 1], anchor_names) if page_num > 0 else []

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
        anchor_desc = 'numero de cuenta' if using_account_number else 'nombre de cuenta'
        return None, None, None, None, f'No se encontro el {anchor_desc} y la etiqueta de campo juntos en ninguna pagina'

    # Prioriza same_page sobre prev_page, igual que find_account_value_pair -- una pagina con
    # ancla Y etiqueta juntas en la MISMA pagina es una senal mucho mas fuerte que una pagina que
    # solo califico por el fallback de "pagina anterior". Sin esto, un ancla unica en el
    # documento (ej. "BCN") puede fallar como "ambiguo" solo porque una etiqueta generica como
    # "Saldo:" tambien aparece cerca del inicio de la pagina SIGUIENTE perteneciendo en realidad
    # a OTRA cuenta (caso real confirmado: BCN en pagina 5, tabla de JPMCB continua en pagina 6
    # con su propio "Saldo:" cerca del encabezado) -- eso NO deberia invalidar el match directo y
    # correcto que ya existe en la misma pagina. Solo se recurre a candidatos prev_page cuando
    # NINGUNA pagina califico por same_page en todo el documento.
    same_page_candidates = [p for p in page_candidates if p[2] == 'same_page']
    effective_candidates = same_page_candidates if same_page_candidates else page_candidates

    if len(effective_candidates) > 1:
        pages_found = [p[0] + 1 for p in effective_candidates]
        anchor_desc = 'numero de cuenta' if using_account_number else 'nombre de cuenta'
        return None, None, None, None, f'La combinacion de {anchor_desc} + etiqueta aparece en mas de una pagina ({pages_found}) -- no se puede confirmar cual es la correcta sin ambiguedad'

    page_num, best_label_rect, source = effective_candidates[0]
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


def _row_extent(page, y0, tolerance=3.0):
    """
    Extremos horizontales reales del texto impreso en la fila de y0 (tolerancia en pt),
    escaneando todas las palabras de la pagina -- mismo principio ya usado para row_right_edge
    en find_full_row_evidence: nunca usar un margen fijo en puntos para delimitar una fila, no
    generaliza entre plataformas de origen del reporte ni entre columnas de distinto ancho.
    """
    words = page.get_text("words")
    xs0, xs1 = [], []
    for w in words:
        wx0, wy0, wx1, wy1 = w[0], w[1], w[2], w[3]
        if abs(wy0 - y0) <= tolerance:
            xs0.append(wx0)
            xs1.append(wx1)
    if not xs0:
        return None, None
    return min(xs0), max(xs1)


def _row_top_boundary(page, y0, lookback=40, margin=6):
    """
    Limite superior real para un recorte que empieza en la fila de y0: busca la fila de texto
    distinta INMEDIATAMENTE ANTERIOR (y1 < y0) dentro de `lookback` puntos hacia arriba y
    devuelve un punto justo debajo de ella -- evita que el recorte capture la mitad inferior de
    la fila anterior (caso real confirmado: "Fecha de apertura:" apareciendo cortada a la mitad
    arriba de "Saldo:" en el modo full-row, Ronda 16). Si no hay fila anterior cercana, usa el
    margen fijo pequeño de siempre.
    """
    words = page.get_text("words")
    prev_y1 = None
    for w in words:
        wy1 = w[3]
        if wy1 < y0 - 1 and (y0 - wy1) <= lookback:
            if prev_y1 is None or wy1 > prev_y1:
                prev_y1 = wy1
    if prev_y1 is not None:
        return min(y0, prev_y1 + margin)
    return max(0, y0 - 8)


def crop_full_row(doc, page_num, label_rect, row_rect, dpi=200):
    page = doc[page_num]
    highlight = page.add_highlight_annot(row_rect)
    highlight.set_colors(stroke=(1, 0.85, 0))
    highlight.update()

    clip = fitz.Rect(
        max(0, label_rect.x0 - 15),
        _row_top_boundary(page, label_rect.y0),
        min(page.rect.width - 5, row_rect.x1 + 15),
        min(page.rect.height, label_rect.y1 + 20),
    )
    pix = page.get_pixmap(clip=clip, dpi=dpi)
    return pix


def crop_evidence_region(doc, page_num, name_rect, value_rect, same_page=True, dpi=200, bureau_hint=None):
    page = doc[page_num]
    highlight = page.add_highlight_annot(value_rect)
    highlight.set_colors(stroke=(1, 0.85, 0))
    highlight.update()

    row_left, _ = _row_extent(page, value_rect.y0)

    if same_page:
        # nombre y valor en la misma pagina: recorte normal desde el nombre hasta el valor
        left_fixed = min(name_rect.x0, value_rect.x0) - 15
        right_default = max(name_rect.x1, value_rect.x1) + 260
        ref_y0 = name_rect.y0
        # mismo principio que _row_top_boundary en crop_full_row: un margen fijo de -8pt a veces
        # no alcanza a excluir la fila justo arriba (ej. el encabezado de columna "TransUnion" /
        # "Experian" impreso arriba del ancla) -- caso real confirmado, cuenta SANTANDER CONSUMER
        # USA, Ronda 16: ese encabezado quedaba cortado a la mitad en el borde superior del
        # recorte.
        top = _row_top_boundary(page, name_rect.y0)
    else:
        # el nombre de la cuenta esta en la pagina anterior (cuenta que cruza el page break) --
        # las coordenadas Y de paginas distintas no son comparables, asi que se recorta desde
        # el inicio de ESTA pagina (donde continua la tabla) hasta el valor, sin usar la
        # posicion vertical del nombre.
        left_fixed = value_rect.x0 - 200
        right_default = value_rect.x1 + 260
        ref_y0 = value_rect.y0
        top = 0

    # Extiende el borde izquierdo hasta el inicio REAL del texto de esa fila (la etiqueta de la
    # cuenta, ej. "Tipo de cuenta - Detalle:"), en vez de un margen fijo que a veces no alcanza a
    # cubrirla -- esto solo puede ampliar el recorte hacia la izquierda, nunca recortarlo mas de
    # lo que ya estaba (caso real confirmado: columnas Experian/Equifax mas a la derecha que
    # TransUnion cortaban la etiqueta a la mitad -- "jeta de credito" en vez de "Tarjeta de
    # credito" -- porque el margen fijo de 200pt no alcanzaba a llegar hasta ahi, Ronda 16).
    left = min(left_fixed, row_left) if row_left is not None else left_fixed

    # Limite derecho: si se conoce la columna real del buro (bureau_hint), se usa su borde
    # derecho real (mismo calculo _bureau_x_ranges_near ya usado para desambiguar la busqueda)
    # en vez de un margen fijo de +260pt -- ese margen fijo puede alcanzar la columna del buro
    # VECINO cuando las columnas son angostas, mostrando datos de OTRO buro dentro del recorte
    # de este (caso real confirmado: SANTANDER CONSUMER USA, columnas Experian y Equifax a menos
    # de 260pt de distancia -- el recorte de "Equifax" terminaba mostrando la misma tabla de
    # Experian en vez de datos propios, Ronda 16). Solo se usa para ACORTAR el recorte por
    # defecto, nunca para ampliarlo mas alla de +260pt -- si el calculo de columna no aplica o
    # no es consistente con el valor real, se mantiene el comportamiento de siempre.
    right = right_default
    if bureau_hint:
        ranges = _bureau_x_ranges_near(page, ref_y0)
        rng = ranges.get(bureau_hint)
        if rng is not None and rng[1] >= value_rect.x1:
            right = min(right_default, rng[1] + 20)

    clip = fitz.Rect(
        max(0, left),
        top,
        min(page.rect.width, right),
        min(page.rect.height, value_rect.y1 + 20),
    )
    pix = page.get_pixmap(clip=clip, dpi=dpi)
    return pix


# ── Endpoint principal: construir el paquete completo ───────────────────

def _write_cover_page(doc, client_name: str, report_date: str, total_items: int):
    """
    Portada con el mismo formato de documento legal (margenes de 1 pulgada, titulo centrado en
    negrita, bloque cliente/fecha/total alineado en una columna fija de valores -- Ronda 17). Se
    usa una vez por cada PAQUETE de salida (si el paquete de evidencia se divide en varios
    archivos por tamano, cada archivo lleva su propia portada identica, para que cada uno sea un
    documento legal completo y auto-contenido por si solo).
    """
    cover = doc.new_page(width=PAGE_W, height=PAGE_H)
    title = "CFPB SUPPORTING EVIDENCE"
    cover.insert_text((_centered_x(title, 18, "hebo"), 110), title, fontsize=18, fontname="hebo")

    value_x = MARGIN + 130
    y = 170
    for label, value in (
        ("Client:", client_name),
        ("Report Date:", report_date),
        ("Evidence Items:", str(total_items)),
    ):
        cover.insert_text((MARGIN, y), label, fontsize=11, fontname="hebo")
        cover.insert_text((value_x, y), value, fontsize=11, fontname="helv")
        y += 22

    y += 20
    cover.insert_text((MARGIN, y), "Supporting excerpts from the original credit report.", fontsize=10, fontname="helv")
    y += 15
    cover.insert_text((MARGIN, y), "Each highlighted value is reproduced exactly as printed in the original document.", fontsize=10, fontname="helv")
    return cover


def _write_item_pages(doc, client_name: str, item: "EvidenceItem", crops: list):
    """
    Escribe la(s) pagina(s) de evidencia de UN item confirmado dentro de `doc` -- encabezado
    repetido (via _new_evidence_page), titulo/campo centrados, y cada recorte escalado para no
    salirse del margen (Ronda 17), continuando en una pagina nueva marcada "(cont.)" si las
    imagenes no caben en lo que queda de la pagina actual. Aislado en su propia funcion para que
    el empaquetado por tamano (ver build_evidence_package) pueda construir cada item en un
    documento temporal separado, medir su tamano, y decidir en que paquete cae -- sin duplicar
    esta logica de layout entre el caso "cabe en el paquete actual" y el caso "no cabe, hay que
    revertir y abrir un paquete nuevo".
    """
    page = _new_evidence_page(doc, client_name)

    heading = f"EVIDENCE {item.discrepancy_id}"
    page.insert_text((_centered_x(heading, 14, "hebo"), 70), heading, fontsize=14, fontname="hebo")
    page.insert_text((_centered_x(item.title, 11, "helv"), 88), item.title, fontsize=11, fontname="helv")
    field_text = f"Field: {item.field_label}"
    page.insert_text((_centered_x(field_text, 9, "helv"), 103), field_text, fontsize=9, fontname="helv")

    y_cursor = 128
    max_img_w = CONTENT_W
    px_to_pt = 72.0 / 200.0  # los recortes se capturan a 200 DPI (ver crop_evidence_region/crop_full_row)
    for pix, sub, source_page in crops:
        img_w = pix.width * px_to_pt
        img_h = pix.height * px_to_pt
        if img_w > max_img_w:
            scale = max_img_w / img_w
            img_w *= scale
            img_h *= scale

        if y_cursor + 14 + img_h > PAGE_H - MARGIN - FOOTER_SPACE:
            page = _new_evidence_page(doc, client_name, continuation=True)
            y_cursor = 65

        src_text = f"Source: Original Credit Report - Page {source_page} ({sub.label})"
        page.insert_text((MARGIN, y_cursor), src_text, fontsize=8, fontname="helv")
        y_cursor += 14
        img_rect = fitz.Rect(MARGIN, y_cursor, MARGIN + img_w, y_cursor + img_h)
        page.insert_image(img_rect, pixmap=pix)
        y_cursor = img_rect.y1 + 22


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

    max_mb = req.max_package_size_mb if (req.max_package_size_mb and req.max_package_size_mb > 0) else DEFAULT_MAX_PACKAGE_SIZE_MB
    max_bytes = int(max_mb * 1024 * 1024)

    item_results: list[ItemResult] = []
    included_count = 0

    # ── Empaquetado por tamano (Ronda "paquete < 10MB") ──────────────────────
    # Cada item confirmado se escribe primero en un documento temporal aparte (con el layout
    # completo de Ronda 17: encabezado, titulo centrado, recortes escalados, paginas de
    # continuacion si hace falta) para poder MEDIR su tamano antes de decidir en que paquete cae.
    # Se intenta agregarlo al paquete que esta en progreso ("current_doc"); si al agregarlo el
    # paquete supera max_bytes Y ya tenia al menos otro item adentro, se revierte (se sacan las
    # paginas que se acaban de agregar), se cierra el paquete actual tal como estaba ANTES de
    # este item (con su propia numeracion de pagina final), y se abre un paquete nuevo -- con su
    # propia portada -- que empieza con este item. Un item nunca se descarta por tamano: si un
    # solo item ya supera max_bytes por si mismo, queda como unico ocupante de su propio paquete
    # aunque ese paquete individual exceda el limite -- partir un item a la mitad no tiene
    # sentido (es una sola discrepancia, debe llegar completa en un solo archivo) y perderlo por
    # completo violaria el principio del proyecto de nunca perder evidencia real en silencio.
    finished_packages = []  # list[{"doc_bytes": bytes, "item_ids": list[str]}]
    current_doc = fitz.open()
    current_item_ids: list[str] = []
    _write_cover_page(current_doc, req.client_name, req.report_date, len(req.items))

    def _finalize_current():
        if current_item_ids:
            # Numeracion de pagina ("Page X of Y") centrada al pie -- calculada por PAQUETE (no
            # globalmente), justo antes de cerrarlo, una vez que se sabe cuantas paginas tiene
            # ESE archivo especifico -- cada paquete es un documento legal completo por si solo.
            total_pages = current_doc.page_count
            for i, pg in enumerate(current_doc):
                footer = f"Page {i + 1} of {total_pages}"
                pg.insert_text((_centered_x(footer, 8, "helv"), PAGE_H - 36), footer, fontsize=8, fontname="helv", color=(0.45, 0.45, 0.45))
            finished_packages.append({
                # garbage=4 + deflate=True: sin esto, tobytes() no limpia objetos huerfanos que
                # queden despues de un delete_page() (ver rollback abajo) -- un item que se
                # revirtio del paquete podia seguir "pesando" en el tamano final aunque su pagina
                # ya no fuera visible, inflando el tamano real muy por encima de lo medido/
                # reportado (bug real encontrado al probar esto con datos reales antes de
                # publicar -- ver test_packing.py). deflate=True es compresion sin perdida
                # (zlib/Flate de los streams del PDF, no recompresion de imagen) -- verificado
                # byte a byte que los pixeles de las imagenes quedan identicos.
                "doc_bytes": current_doc.tobytes(garbage=4, deflate=True),
                "item_ids": list(current_item_ids),
            })
        current_doc.close()

    for item in req.items:
        sub_results = []
        crops = []
        # Guarda que ubicacion (pagina + posicion) ya se uso como evidencia dentro de ESTE item --
        # cuando 2 sub_locations de burós distintos resuelven al MISMO texto impreso (caso real
        # confirmado: Experian y Equifax reportando la misma fecha, sin columna propia de Equifax
        # en esa tabla -- reporte IdentityIQ de Rangel Peñaranda, Ronda 16), no se presenta la
        # segunda como una confirmacion HIGH independiente -- seria citar el mismo recorte 2 veces
        # bajo 2 nombres de buro distintos, exactamente el tipo de sobre-afirmacion que este
        # proyecto no puede permitirse en un documento legal real.
        seen_locations = set()

        for sub in item.sub_locations:
            if sub.highlight_full_row:
                if not sub.field_label_text and not sub.field_label_candidates:
                    sub_results.append(LocatedSubResult(
                        label=sub.label, located=False, confidence='NONE',
                        reason='highlight_full_row requiere field_label_text o field_label_candidates'
                    ))
                    continue
                page_num, label_rect, row_rect, same_page, fail_reason = find_full_row_evidence(
                    source_doc, sub.account_name, sub.field_label_text, sub.field_label_candidates, sub.account_name_alternatives, sub.account_number
                )
                if page_num is not None:
                    loc_key = (page_num, round(label_rect.x0, 1), round(label_rect.y0, 1))
                    if loc_key in seen_locations:
                        sub_results.append(LocatedSubResult(
                            label=sub.label, located=False, page=page_num + 1, confidence='DUPLICATE',
                            reason='Esta ubicacion del documento ya fue citada como evidencia de otro buro/etiqueta en este mismo item -- no hay una fila distinta impresa para confirmar esto por separado.'
                        ))
                        continue
                    seen_locations.add(loc_key)
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
                source_doc, sub.account_name, sub.target_value, sub.bureau_hint, sub.account_number, sub.field_label_text, sub.field_label_candidates, sub.target_value_alternatives, sub.account_name_alternatives
            )
            if page_num is not None and confidence == 'HIGH':
                loc_key = (page_num, round(value_rect.x0, 1), round(value_rect.y0, 1))
                if loc_key in seen_locations:
                    sub_results.append(LocatedSubResult(
                        label=sub.label, located=False, page=page_num + 1, confidence='DUPLICATE',
                        reason='Esta ubicacion del documento ya fue citada como evidencia de otro buro en este mismo item (el valor coincide y no hay una columna separada impresa para este buro en este campo) -- no se puede confirmar por separado.'
                    ))
                    continue
                seen_locations.add(loc_key)
                pix = crop_evidence_region(source_doc, page_num, name_rect, value_rect, same_page=same_page, bureau_hint=sub.bureau_hint)
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

        # Un sub_location marcado 'DUPLICATE' no es un hueco de evidencia -- es un hecho
        # estructural del documento (2 burós comparten el mismo dato impreso, sin columna propia
        # para uno de ellos). No debe bloquear la confirmacion del item completo como si fuera una
        # busqueda fallida; solo se exige que TODOS los sub_locations que SI representan una
        # busqueda real (no duplicada) hayan sido encontrados, y que haya al menos 1 confirmado.
        real_results = [sr for sr in sub_results if sr.confidence != 'DUPLICATE']
        all_located = len(real_results) > 0 and all(sr.located for sr in real_results)
        overall_confidence = 'HIGH' if all_located else 'LOW'

        item_results.append(ItemResult(
            discrepancy_id=item.discrepancy_id,
            title=item.title,
            evidence_located=all_located,
            confidence=overall_confidence,
            sub_results=sub_results,
        ))

        if not all_located:
            continue

        included_count += 1

        # Construir la(s) pagina(s) de este item (layout completo de Ronda 17) en un documento
        # temporal aparte, para poder medir su tamano antes de decidir en que paquete cae.
        temp_doc = fitz.open()
        _write_item_pages(temp_doc, req.client_name, item, crops)

        before_count = current_doc.page_count
        current_doc.insert_pdf(temp_doc)
        current_item_ids.append(item.discrepancy_id)
        pages_added = current_doc.page_count - before_count
        # Misma razon que en _finalize_current: medir con garbage=4+deflate=True, no con el
        # tamano "crudo" sin comprimir -- si no, la decision de si algo "cabe" se toma contra un
        # numero que no refleja el tamano real del archivo que se va a enviar.
        fits = len(current_doc.tobytes(garbage=4, deflate=True)) <= max_bytes
        is_only_item_so_far = (len(current_item_ids) == 1)

        if not fits and not is_only_item_so_far:
            # No cabe junto con lo que ya habia en este paquete -- revertir (sacar TODAS las
            # paginas que se acaban de agregar para este item -- puede ser mas de 1 si el item
            # necesito una pagina de continuacion), cerrar el paquete actual tal como estaba
            # antes de este item, y abrir un paquete nuevo -- con su propia portada -- que
            # arranca con este item.
            for _ in range(pages_added):
                current_doc.delete_page(current_doc.page_count - 1)
            current_item_ids.pop()
            _finalize_current()
            current_doc = fitz.open()
            current_item_ids = []
            _write_cover_page(current_doc, req.client_name, req.report_date, len(req.items))
            current_doc.insert_pdf(temp_doc)
            current_item_ids.append(item.discrepancy_id)

        temp_doc.close()

    _finalize_current()
    source_doc.close()

    package_status = 'READY' if all(ir.evidence_located for ir in item_results) and len(item_results) > 0 else 'REVIEW_REQUIRED'

    packages_out: list[PackageFile] = []
    for i, pkg in enumerate(finished_packages, start=1):
        packages_out.append(PackageFile(
            package_number=i,
            pdf_base64=base64.b64encode(pkg["doc_bytes"]).decode("utf-8"),
            size_bytes=len(pkg["doc_bytes"]),
            items_included=pkg["item_ids"],
        ))

    return BuildPackageResponse(
        package_status=package_status,
        packages=packages_out,
        package_count=len(packages_out),
        pdf_base64=(packages_out[0].pdf_base64 if packages_out else None),
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
