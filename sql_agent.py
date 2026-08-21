import re
import json
import logging
import time
from utils import has_real_data
from db import (
    run_sql,
    get_agent,
    get_customer_name,
    fetch_nombre_rubro_grupo_candidates,
    fetch_cuenta_candidates,
)
from security import validate_sql
from llm_client import complete_text, resolve_fast_model
from data_privacy import (
    CUSTOMER_ID_PLACEHOLDER,
    LLM_SAFETY_INSTRUCTION,
    apply_customer_id_placeholder,
    build_privacy_secrets,
    sanitize_results_for_llm,
    sanitize_text_for_llm,
)

from prompt_builder import build_customer_answer_prompt
from sql_extract import extract_sql_from_llm_response
from conversation_history import (
    history_for_prompts,
    normalize_messages,
    sanitize_messages,
)
from semantic_loader import (
    build_intents_list,
    build_kpi_question_pattern,
    build_tablero_question_pattern,
    build_ventas_netas_question_pattern,
    build_router_entities_block,
    build_routing_rules_block,
    get_kpi_entity_key,
    get_primary_entity_key,
    get_table_map,
    load_schema_context_text,
    qualified_source,
    qualified_sources,
    query_candidates,
    try_heuristic_route,
)

logger = logging.getLogger("agents-poc.sql_agent")


def _schema_context() -> str:
    return load_schema_context_text()


def _kpi_table() -> str:
    return get_kpi_entity_key()


def _primary_table() -> str:
    return get_primary_entity_key()


def _table_map() -> dict[str, str]:
    return get_table_map()


def _kpi_question_re() -> re.Pattern[str]:
    return build_kpi_question_pattern()


def _ventas_netas_question_re() -> re.Pattern[str]:
    return build_ventas_netas_question_pattern()


def _tablero_question_re() -> re.Pattern[str]:
    return build_tablero_question_pattern()


def _maybe_route_to_ventas_enriched(question: str, route: dict) -> dict:
    """Ventas / facturación → vw_fact_bdp_enriched (Ingresos Operacionales), igual que Power BI."""
    primary = _primary_table()
    tables = route.get("tables") or []
    if tables == ["vw_ventas_por_producto_mes"]:
        return route
    if primary in tables and "vw_ventas_netas_mes" not in tables:
        return route
    if not _ventas_netas_question_re().search(question or "") and not re.search(
        r"\b(ventas?|facturaci[oó]n)\b", question or "", re.I
    ):
        return route
    if _DEVOLUCIONES_RE.search(question or "") and not re.search(
        r"\b(ventas?|facturaci[oó]n)\b", question or "", re.I
    ):
        return route
    logger.info(
        "ventas keywords detected; overriding route to %s (Ingresos Operacionales)",
        primary,
    )
    return {
        **route,
        "tables": [primary],
        "intent": "ventas",
        "reason": (route.get("reason") or "")
        + f" [auto: ventas → {primary} / nombre_rubro_grupo Ingresos Operacionales]",
    }


def _maybe_route_to_kpis(question: str, route: dict) -> dict:
    """Fallback: ratio/KPI questions → vw_kpis_financiero (skip tablero / dim_accounts)."""
    kpi_table = _kpi_table()
    primary_table = _primary_table()
    tables = route.get("tables") or []
    if kpi_table in tables:
        return route
    if primary_table in tables:
        return route
    if any(t in tables for t in ("fact_venta", "fact_nota_credito", "presupuesto_proyeccion", "dim_customers")):
        return route
    if _ventas_netas_question_re().search(question or ""):
        return route
    if _tablero_question_re().search(question or ""):
        return route
    if not _kpi_question_re().search(question or ""):
        return route
    logger.info("kpi keywords detected; overriding route to %s", kpi_table)
    return {
        **route,
        "tables": [kpi_table],
        "intent": "kpis",
        "reason": (route.get("reason") or "") + f" [auto: KPI keywords → {kpi_table}]",
    }


def _maybe_route_to_tablero(question: str, route: dict) -> dict:
    """P&L / tablero con montos → vw_fact_bdp_enriched."""
    primary_table = _primary_table()
    tables = route.get("tables") or []
    if primary_table in tables:
        return route
    if tables == ["vw_dim_accounts"]:
        return route
    if not _tablero_question_re().search(question or ""):
        return route
    logger.info("tablero keywords detected; overriding route to %s", primary_table)
    return {
        **route,
        "tables": [primary_table],
        "intent": route.get("intent") or "mixto",
        "reason": (route.get("reason") or "") + f" [auto: tablero → {primary_table}]",
    }


def _prefer_kpi_view(route: dict) -> dict:
    """If the router picked the KPI view, answer only from it (already aggregated)."""
    kpi_table = _kpi_table()
    tables = route.get("tables") or []
    if kpi_table in tables:
        logger.info("preferring %s over %s", kpi_table, tables)
        return {**route, "tables": [kpi_table], "intent": route.get("intent") or "kpis"}
    return route


def _fast_model(agent: dict) -> str:
    return resolve_fast_model(agent)


def generate_customer_answer(question, results, agent, *, history_block: str = ""):
    logger.info("generating customer answer model=%s", agent.get("model"))
    started = time.perf_counter()
    prompt = build_customer_answer_prompt(
        question, results, agent, LLM_SAFETY_INSTRUCTION, history_block=history_block
    )
    text = complete_text(agent, prompt, max_tokens=512, timeout_sec=18)
    logger.info(
        "customer answer ready elapsed_ms=%s chars=%s",
        int((time.perf_counter() - started) * 1000),
        len(text),
    )
    return text

def route_question(question: str, agent: dict, *, history_block: str = ""):
    heuristic = try_heuristic_route(question)
    if heuristic:
        logger.info(
            "heuristic route intent=%s tables=%s reason=%s",
            heuristic.get("intent"),
            heuristic.get("tables"),
            heuristic.get("reason"),
        )
        return heuristic

    fast_model = _fast_model(agent)
    logger.info(
        "routing question fast_model=%s agent_model=%s",
        fast_model,
        agent.get("model"),
    )
    started = time.perf_counter()
    history_section = history_block or ""
    text = complete_text(
        agent,
        f"""
Eres un router de consultas financieras.

Tablas / vistas disponibles:

{build_router_entities_block()}

REGLAS DE ENRUTAMIENTO:
{build_routing_rules_block()}
- Si implica filtro por fechas, mes, año o periodo, menciónalo en "reason"
  (vw_fact_bdp_enriched → anio_mes / anio / source_date; vw_dim_accounts → sin montos ni tiempo;
  vw_kpis_financiero → anio / anio_mes;
  fact_bdp → source_date o gold.dim_time; presupuesto → anio_mes;
  ventas netas/brutas mensuales → vw_fact_bdp_enriched (nombre_rubro_grupo = Ingresos Operacionales);
  ventas detalle por producto → vw_ventas_por_producto_mes.anio_mes;
  ventas detalle → fact_venta.invoice_date).
- Si la intención es ambigua o pide montos contables, enruta a vw_fact_bdp_enriched.
{history_section}
RESPONDE SOLO JSON así:

{{
  "intent": "{build_intents_list()}",
  "tables": ["{_primary_table()}"],
  "reason": "..."
}}

PREGUNTA ACTUAL:
{question}
""",
        model=fast_model,
        max_tokens=400,
        timeout_sec=15,
        temperature=0,
    )

    # limpiar posibles ```json
    text = text.replace("```json", "").replace("```", "").strip()
    logger.debug("router raw response=%s", text)

    route = json.loads(text)
    tables = route.get("tables") or []
    logger.info(
        "route resolved elapsed_ms=%s intent=%s route_tables=%s reason=%s",
        int((time.perf_counter() - started) * 1000),
        route.get("intent"),
        tables,
        route.get("reason"),
    )
    return route


_BROAD_RUBRO_PHRASES = (
    "gastos administrativos",
    "gasto administrativo",
    "gastos de administración",
    "gastos financieros",
    "gasto financiero",
    "ingresos operacionales",
    "ingreso operacional",
    "costos de ventas",
    "costo de ventas",
    "utilidad bruta",
    "utilidad operacional",
    "utilidad neta",
    "activo corriente",
    "activo total",
    "pasivo corriente",
    "patrimonio total",
)

_QUESTION_STOPWORDS = frozenset({
    "cuanto", "cuánto", "total", "gasto", "gastos", "mes", "anio", "año",
    "junio", "enero", "febrero", "marzo", "abril", "mayo", "julio", "agosto",
    "septiembre", "octubre", "noviembre", "diciembre", "ultimo", "último",
    "periodo", "cliente", "empresa", "valor", "montos", "saldo", "movimiento",
    "fue", "son", "estan", "están", "cual", "cuál", "como", "cómo", "que", "qué",
    "del", "de", "la", "el", "los", "las", "en", "por", "para", "con", "sin",
    "administracion", "administración", "administrativos", "administrativo",
    "financieros", "financiero", "operacionales", "operacional", "ingresos",
    "ingreso", "costos", "costo", "ventas", "utilidad", "activo", "pasivo",
    "patrimonio", "gastamos", "gastó", "gasto", "hubo", "tuvo", "fueron",
    "cuanto", "cuánto", "dame", "dime", "mostrar", "mostrar", "consultar",
    "cuales", "cuáles", "habia", "había", "sido", "estuvo",
})

_DEVOLUCIONES_RE = re.compile(
    r"\b(devoluci[oó]n(?:es)?|notas?\s+cr[eé]dito)\b",
    re.IGNORECASE,
)

_ENRICHED_FORBIDDEN_IN_SQL = (
    "fact_nota_credito",
    "fact_venta",
    "fact_bdp",
    "vw_ventas_netas_mes",
    "vw_ventas_por_producto_mes",
    "presupuesto_proyeccion",
)

_ACCOUNT_PHRASE_PATTERNS: list[tuple[tuple[str, ...], str]] = [
    (("devoluciones", "devolución", "devolucion", "notas credito", "notas crédito"), "%devoluc%"),
    (("materia prima", "materias primas"), "%materia%"),
]

_RUBRO_KEYWORD_PATTERNS: list[tuple[tuple[str, ...], str]] = [
    (("ventas", "venta", "facturacion", "facturación", "facturacion neta"), "%ingres%"),
    (("gasto financiero", "gastos financieros", "financier"), "%financier%"),
    (("gasto admon", "gastos admin", "administrativ"), "%admin%"),
    (("ingreso operacional", "ingresos operacionales", "ingresos"), "%ingres%"),
    (("costo vent", "costos de vent", "costo de vent"), "%costo%"),
    (("utilidad bruta", "utilidad operacional", "utilidad neta"), "%utilidad%"),
    (("activo corriente",), "%activo corriente%"),
    (("activo no corriente", "activo fijo"), "%activo%"),
    (("pasivo corriente", "pasivo no corriente"), "%pasivo%"),
    (("patrimonio",), "%patrimonio%"),
]


def _account_search_patterns(question: str) -> list[str]:
    q = question.lower()
    patterns: list[str] = []
    seen: set[str] = set()
    for phrases, pattern in _ACCOUNT_PHRASE_PATTERNS:
        if any(phrase in q for phrase in phrases) and pattern not in seen:
            patterns.append(pattern)
            seen.add(pattern)
    words = re.findall(r"[a-záéíóúñü]+", q)
    for word in words:
        if len(word) < 4 or word in _QUESTION_STOPWORDS:
            continue
        pattern = f"%{word}%"
        if pattern not in seen:
            patterns.append(pattern)
            seen.add(pattern)
    return patterns[:5]


def _is_broad_rubro_only_question(question: str) -> bool:
    if _account_search_patterns(question):
        return False
    q = question.lower()
    return any(phrase in q for phrase in _BROAD_RUBRO_PHRASES)


def _rubro_search_patterns(question: str) -> list[str]:
    q = question.lower()
    patterns: list[str] = []
    seen: set[str] = set()
    for keywords, pattern in _RUBRO_KEYWORD_PATTERNS:
        if any(kw in q for kw in keywords) and pattern not in seen:
            patterns.append(pattern)
            seen.add(pattern)
    return patterns


def _build_rubro_hints_block(candidates: list[str]) -> str:
    if not candidates:
        return ""
    lines = "\n".join(f"  - '{name}'" for name in candidates)
    return f"""
VALORES nombre_rubro_grupo VÁLIDOS PARA ESTE CLIENTE (gold.vw_dim_accounts):
{lines}
- Usar SOLO cuando la pregunta pide el TOTAL de una línea amplia (ej. total gastos administrativos).
- Filtra con nombre_rubro_grupo = 'valor exacto de la lista' (respeta mayúsculas y singular/plural).
- NO inventes valores (ej. 'Gastos Financieros' si la lista dice 'Gasto Financiero').
"""


def _build_cuenta_hints_block(candidates: list[dict[str, str]]) -> str:
    if not candidates:
        return ""
    lines: list[str] = []
    for row in candidates:
        cuenta = row.get("nombre_cuenta") or "—"
        aux = row.get("nombre_auxiliar") or "—"
        sub = row.get("sub_nodo_s3") or "—"
        rubro = row.get("nombre_rubro_grupo") or "—"
        lines.append(
            f"  - nombre_cuenta='{cuenta}', sub_nodo_s3='{sub}', "
            f"nombre_auxiliar='{aux}' (rubro padre: {rubro})"
        )
    joined = "\n".join(lines)
    return f"""
CUENTAS/SUBCUENTAS COINCIDENTES PARA ESTE CLIENTE (gold.vw_dim_accounts):
{joined}
- La pregunta pide un concepto ESPECÍFICO → filtra por nombre_cuenta, sub_nodo_s3 o nombre_auxiliar.
- NO uses solo nombre_rubro_grupo (ej. 'Gasto Admon') si piden seguros, arrendamiento, honorarios, etc.
- Prefiere el valor exacto más específico: nombre_cuenta = 'Seguros' o nombre_auxiliar ILIKE '%Seguro%'.
- El rubro padre (nombre_rubro_grupo) es contexto; no agregues todo el rubro salvo que lo pidan.
"""


def _resolve_enriched_hints(question: str, customer_id: str, *, broad: bool = False) -> str:
    if not broad and not _is_broad_rubro_only_question(question):
        account_patterns = _account_search_patterns(question)
        if account_patterns:
            cuentas = fetch_cuenta_candidates(customer_id, account_patterns)
            cuenta_block = _build_cuenta_hints_block(cuentas)
            if cuenta_block:
                return cuenta_block

    patterns = None if broad else _rubro_search_patterns(question)
    if broad or not patterns:
        candidates = fetch_nombre_rubro_grupo_candidates(customer_id)
    else:
        candidates = fetch_nombre_rubro_grupo_candidates(
            customer_id, ilike_patterns=patterns
        )
    return _build_rubro_hints_block(candidates)


def _resolve_rubro_hints(question: str, customer_id: str, *, broad: bool = False) -> str:
    return _resolve_enriched_hints(question, customer_id, broad=broad)


def generate_sql(
    question,
    table,
    customer_id,
    schema,
    agent,
    *,
    retry_note: str | None = None,
    rubro_hints: str | None = None,
    history_block: str = "",
    temporal_hints: str = "",
):

    source = qualified_source(table, schema)
    fast_model = _fast_model(agent)
    logger.info(
        "generating sql source=%s route_table=%s fast_model=%s agent_model=%s",
        source,
        table,
        fast_model,
        agent.get("model"),
    )
    started = time.perf_counter()
    kpi_table = _kpi_table()
    table_map = _table_map()
    schema_ctx = _schema_context()
    history_section = history_block or ""
    temporal_section = temporal_hints or ""

    if table == "vw_dim_accounts":
        prompt = f"""
Eres un experto en SQL PostgreSQL financiero.

{schema_ctx}
{history_section}
VISTA ACTUAL:
gold.vw_dim_accounts

DESCRIPCIÓN:
{table_map.get(table, "")}

REGLAS (OBLIGATORIAS):
- SOLO SELECT sobre gold.vw_dim_accounts (catálogo / rubros, SIN montos — no tiene mvto ni saldo)
- SIEMPRE filtra por customer_id = '{CUSTOMER_ID_PLACEHOLDER}'
- Columnas: id_auxiliar, nombre_auxiliar, nombre_cuenta, nombre_grupo, nombre_clase,
  id_rubro, nombre_rubro_grupo, nombre_rubro_clase, uso, nodo_s1, nodo_s2, sub_nodo_s3, cod_nodo
- Usa esta vista SOLO para listar cuentas, rubros o jerarquía contable
- Si la pregunta pide montos/totales/saldos → NO uses esta vista (usa gold.vw_fact_bdp_enriched)
- Selecciona solo columnas relevantes (evita SELECT *)
- Ordena por id_auxiliar o nodo_s1 según la pregunta
- máximo 100 filas

PREGUNTA ACTUAL:
{question}

Devuelve SOLO SQL.
"""
    elif table == "vw_fact_bdp_enriched":
        retry_block = ""
        if retry_note:
            retry_block = f"\nREINTENTO (0 filas en consulta previa):\n{retry_note}\n"
        rubro_block = rubro_hints or ""
        prompt = f"""
Eres un experto en SQL PostgreSQL financiero.

{schema_ctx}
{history_section}
{temporal_section}
{rubro_block}
VISTA ACTUAL:
gold.vw_fact_bdp_enriched

DESCRIPCIÓN:
{table_map.get(table, "")}

REGLAS (OBLIGATORIAS):
- SOLO SELECT sobre gold.vw_fact_bdp_enriched (una sola vista, sin JOINs, sin otras tablas)
- PROHIBIDO: fact_nota_credito, fact_venta, fact_bdp, silver.*, vw_ventas_netas_mes
- SIEMPRE filtra por customer_id = '{CUSTOMER_ID_PLACEHOLDER}'
- Montos: mvto (movimiento), saldo_final, saldo_inicial, movimiento_debito, movimiento_credito
- Cuenta (jerarquía PUC): id_auxiliar, nombre_auxiliar, id_cuenta, nombre_cuenta,
  id_grupo, nombre_grupo, id_subcuenta, nombre_subcuenta
- Rubros KPI tablero: nombre_rubro_grupo, nombre_rubro_clase, uso, sub_nodo_s3, nodo_s1, nodo_s2, cod_nodo
- FECHAS (OBLIGATORIO cuando aplique):
  - Si hay PERIODOS DETECTADOS arriba → filtra con anio_mes en WHERE (no escanees toda la historia)
  - Si la pregunta o el historial mencionan mes/año → SIEMPRE AND anio_mes = 'YYYY-MM' o anio_mes IN (...)
  - Mes único: WHERE anio_mes = '2026-06' → SELECT SUM(mvto) (sin GROUP BY anio_mes)
  - Comparar jun 2026 vs jun 2025: WHERE anio_mes IN ('2026-06','2025-06') GROUP BY anio_mes
  - Año completo: WHERE anio = 2026 OR anio_mes LIKE '2026-%'
  - Tendencia / histórico / evolución completa: entonces GROUP BY anio_mes (con LIMIT o rango acotado)
  - Último periodo: ORDER BY anio DESC, anio_mes DESC LIMIT 1 (subconsulta o CTE si agregas)
- JERARQUÍA DE FILTROS (CRÍTICO — elige el nivel según la pregunta):
  1. Rubro KPI (nombre_rubro_grupo): SOLO cuando piden el TOTAL de una línea amplia
     (ej. "total gastos administrativos", "gasto financiero del mes", "ingresos operacionales")
  2. Sub-cuenta (nombre_cuenta, sub_nodo_s3, nombre_auxiliar): cuando piden un concepto ESPECÍFICO
     dentro del rubro (ej. "seguros", "arrendamiento", "honorarios")
     → nombre_cuenta = 'Seguros' OR nombre_auxiliar ILIKE '%Seguro%' (NO todo 'Gasto Admon')
  3. Si hay lista de cuentas coincidentes arriba, usa el valor exacto más específico
  4. NO confundas concepto específico con rubro padre (seguros ≠ sumar todo Gasto Admon)
  5. nombre_rubro_grupo: valor exacto del catálogo; no pluralizar ('Gasto Financiero', no 'Gastos Financieros')
  6. Ventas / facturación / ingresos operacionales (pregunta distinta a devoluciones) →
     nombre_rubro_grupo = 'Ingresos Operacionales' (SUM(mvto))
     NO uses vw_ventas_netas_mes salvo detalle facturación Siigo por producto
  7. Devoluciones / notas crédito (solo si la pregunta es sobre devoluciones, no ventas) →
     nombre_cuenta o nombre_auxiliar ILIKE '%devoluc%' (SUM(mvto))
     NO uses fact_nota_credito ni nombre_rubro_grupo = 'Ingresos Operacionales'
- Para totales: SUM(mvto) — NUNCA ABS(mvto) ni SUM(ABS(mvto))
  - SELECT típico agregado: anio_mes, SUM(mvto) AS total (y dimensiones del GROUP BY)
  - Si usas GROUP BY, en SELECT SOLO columnas del GROUP BY + funciones agregadas (SUM, COUNT, MAX…)
  - NUNCA mezcles GROUP BY con saldo_inicial, saldo_final, movimiento_debito, movimiento_credito sin agregar
  - Usa saldos solo si la pregunta pide saldo explícito y sin GROUP BY incompatible
  - Rubro amplio: GROUP BY anio_mes, nombre_rubro_grupo
  - Sub-cuenta: GROUP BY anio_mes, nombre_cuenta (o sin GROUP BY si es un solo total)
  - Comparar varios meses: preferir UNA consulta con GROUP BY anio_mes (o FILTER/WHERE anio_mes IN (...))
    en lugar de CTEs complejas, salvo que sea imprescindible
  - NO incluyas fecha en GROUP BY si ya filtras un solo anio_mes
- NO uses vw_kpis_financiero ni fact_bdp directo; esta vista ya une BDP + rubros + tiempo
- Selecciona solo columnas relevantes; máximo 100 filas
{retry_block}
PREGUNTA ACTUAL:
{question}

Devuelve SOLO SQL.
"""
    elif table == kpi_table:
        prompt = f"""
Eres un experto en SQL PostgreSQL financiero.

{schema_ctx}
{history_section}
VISTA ACTUAL:
gold.{kpi_table}

DESCRIPCIÓN:
{table_map.get(kpi_table, "")}

REGLAS (OBLIGATORIAS):
- SOLO SELECT
- Consulta ÚNICAMENTE gold.vw_kpis_financiero (schema gold, nombre completo)
- NO hagas JOIN con fact_bdp, dim_accounts ni otras tablas; los KPIs ya están calculados
- SIEMPRE filtra por customer_id = '{CUSTOMER_ID_PLACEHOLDER}'
- NO selecciones nombre_cliente ni customer_id en el SELECT salvo que la pregunta lo pida explícitamente
- FECHAS: si la pregunta menciona periodo, mes, año o "último periodo", filtra con
  anio_mes ('YYYY-MM') o anio (int). Para último periodo usa ORDER BY anio DESC, anio_mes DESC LIMIT 1.
- Si la pregunta NO pide filtro temporal, devuelve los periodos más recientes
  (ORDER BY anio DESC, anio_mes DESC LIMIT 12) salvo que pida un total/histórico explícito.
- Selecciona solo las columnas relevantes para la pregunta (no SELECT * salvo que pida resumen completo)
- máximo 50 filas

PREGUNTA:
{question}

Devuelve SOLO SQL.
"""
    elif table == "vw_ventas_netas_mes":
        prompt = f"""
Eres un experto en SQL PostgreSQL financiero.

{schema_ctx}

VISTA ACTUAL:
gold.vw_ventas_netas_mes

DESCRIPCIÓN:
{table_map.get(table, "")}

REGLAS (OBLIGATORIAS):
- SOLO SELECT sobre gold.vw_ventas_netas_mes (una sola vista, sin JOINs)
- SIEMPRE filtra por customer_id = '{CUSTOMER_ID_PLACEHOLDER}'
- Columnas: anio_mes, ventas_brutas, notas_credito, ventas_netas
- ventas_netas = ventas_brutas − notas_credito (ya calculado en la vista)
- FECHAS: usa anio_mes ('YYYY-MM')
  - Mes: WHERE anio_mes = '2026-06'
  - Año: WHERE anio_mes LIKE '2026-%'
  - Rango: WHERE anio_mes BETWEEN '2026-01' AND '2026-06'
- Para evolución mensual: ORDER BY anio_mes
- NO uses fact_venta ni fact_nota_credito; esta vista ya agrega ambas
- máximo 50 filas

PREGUNTA:
{question}

Devuelve SOLO SQL.
"""
    elif table == "vw_ventas_por_producto_mes":
        prompt = f"""
Eres un experto en SQL PostgreSQL financiero.

{schema_ctx}

VISTA ACTUAL:
gold.vw_ventas_por_producto_mes

DESCRIPCIÓN:
{table_map.get(table, "")}

REGLAS (OBLIGATORIAS):
- SOLO SELECT sobre gold.vw_ventas_por_producto_mes (sin JOINs)
- SIEMPRE filtra por customer_id = '{CUSTOMER_ID_PLACEHOLDER}'
- Columnas: anio_mes, product_name, product_code, total_ventas, cantidad, num_facturas
- FECHAS: anio_mes ('YYYY-MM')
- Para top productos: ORDER BY total_ventas DESC
- NO uses fact_venta; esta vista ya agrega por producto
- máximo 50 filas

PREGUNTA:
{question}

Devuelve SOLO SQL.
"""
    elif table == "vw_presupuesto_vs_real_mes":
        prompt = f"""
Eres un experto en SQL PostgreSQL financiero.

{schema_ctx}

VISTA ACTUAL:
gold.vw_presupuesto_vs_real_mes

DESCRIPCIÓN:
{table_map.get(table, "")}

REGLAS (OBLIGATORIAS):
- SOLO SELECT sobre gold.vw_presupuesto_vs_real_mes (sin JOINs)
- SIEMPRE filtra por customer_id = '{CUSTOMER_ID_PLACEHOLDER}'
- Columnas: anio_mes, cuenta, cuenta_contable, presupuesto, real_saldo, variacion, pct_cumplimiento,
  nombre_rubro_grupo, nombre_rubro_clase, uso, nodo_s1, nodo_s2, sub_nodo_s3, cod_nodo
- FECHAS: anio_mes ('YYYY-MM')
- NO JOIN presupuesto_proyeccion ni fact_bdp manualmente
- máximo 50 filas

PREGUNTA:
{question}

Devuelve SOLO SQL.
"""
    elif table == "vw_semaforos_cliente":
        prompt = f"""
Eres un experto en SQL PostgreSQL financiero.

{schema_ctx}

VISTA ACTUAL:
gold.vw_semaforos_cliente

DESCRIPCIÓN:
{table_map.get(table, "")}

REGLAS (OBLIGATORIAS):
- SOLO SELECT sobre gold.vw_semaforos_cliente (sin JOINs)
- SIEMPRE filtra por customer_id = '{CUSTOMER_ID_PLACEHOLDER}'
- Una fila por cliente (último periodo KPI). Columnas de semáforos y ratios.
- NO uses vw_kpis_financiero si solo pide semáforos actuales
- máximo 50 filas

PREGUNTA:
{question}

Devuelve SOLO SQL.
"""
    elif table == "vw_ultimo_periodo_cliente":
        prompt = f"""
Eres un experto en SQL PostgreSQL financiero.

{schema_ctx}

VISTA ACTUAL:
gold.vw_ultimo_periodo_cliente

DESCRIPCIÓN:
{table_map.get(table, "")}

REGLAS (OBLIGATORIAS):
- SOLO SELECT sobre gold.vw_ultimo_periodo_cliente (sin JOINs)
- SIEMPRE filtra por customer_id = '{CUSTOMER_ID_PLACEHOLDER}'
- Columnas: fuente (kpis|ventas|bdp|presupuesto), ultimo_anio_mes, ultima_fecha
- Opcional: AND fuente = 'kpis' si la pregunta es específica de KPIs
- máximo 50 filas

PREGUNTA:
{question}

Devuelve SOLO SQL.
"""
    elif table == "fact_venta":
        prompt = f"""
Eres un experto en SQL PostgreSQL financiero.

{schema_ctx}

TABLA ACTUAL:
{schema}.fact_venta

DESCRIPCIÓN:
{table_map.get(table, "")}

REGLAS (OBLIGATORIAS):
- SOLO SELECT sobre {schema}.fact_venta (una sola tabla, sin JOINs)
- SIEMPRE filtra por customer_id = '{CUSTOMER_ID_PLACEHOLDER}'
- PROHIBIDO: JOIN gold.dim_time_sales, gold.dim_time, id_tiempo u otras tablas
- FECHAS: usa SOLO columnas de fact_venta:
  - invoice_date (preferir para ventas por mes/año/rango)
  - load_ts::date solo si la pregunta es por fecha de carga del ETL
- Mes y año (ej. junio 2026):
  EXTRACT(YEAR FROM invoice_date) = 2026 AND EXTRACT(MONTH FROM invoice_date) = 6
  O invoice_date >= DATE '2026-06-01' AND invoice_date < DATE '2026-07-01'
- Rango: invoice_date BETWEEN DATE 'YYYY-MM-DD' AND DATE 'YYYY-MM-DD'
- NO uses SELECT * salvo que la pregunta pida detalle completo; preferir SUM(total), COUNT, etc. si agrega
- máximo 50 filas

PREGUNTA:
{question}

Devuelve SOLO SQL.
"""
    else:
        prompt = f"""
Eres un experto en SQL PostgreSQL financiero.

{schema_ctx}

CONTEXTO:
schema silver: {schema}

TABLA ACTUAL:
{table}

DESCRIPCIÓN:
{table_map.get(table, "")}

REGLAS:
- SOLO SELECT
- SIEMPRE filtra por customer_id = '{CUSTOMER_ID_PLACEHOLDER}' en tablas que tengan esa columna
- Si la pregunta es de KPIs agregados (márgenes, ROE, utilidad, semáforos), NO uses esta tabla;
  deberías usar gold.vw_kpis_financiero en su lugar.
- FECHAS: si la pregunta menciona periodo, mes, año, trimestre, rango o "último periodo",
  aplica filtro temporal según la sección FILTRADO POR FECHAS Y PERIODOS del contexto.
  fact_bdp: source_date O JOIN gold.dim_time t ON t.id_time = {schema}.fact_bdp.id_tiempo
  (id_tiempo SOLO existe en fact_bdp, nunca en fact_venta).
  fact_venta: SOLO columnas de {schema}.fact_venta — invoice_date (preferir) o load_ts::date.
  Mes/año: EXTRACT(YEAR FROM invoice_date) y EXTRACT(MONTH FROM invoice_date), o rango DATE 'YYYY-MM-01'.
  NO JOIN gold.dim_time_sales, gold.dim_time ni otras tablas gold para ventas.
  presupuesto_proyeccion: anio_mes/mes y deleted_at IS NULL.
- Si la pregunta NO pide filtro temporal, no agregues condiciones de fecha.
- máximo 50 rows
- califica tablas silver como {schema}.nombre_tabla en el SQL

PREGUNTA:
{question}

Devuelve SOLO SQL.
"""

    response_text = complete_text(
        agent,
        prompt,
        model=fast_model,
        max_tokens=1200,
        timeout_sec=18,
        temperature=0,
    )
    
    sql = response_text.strip()
    logger.info("sql llm raw response table=%s len=%s", table, len(sql))
    logger.debug("sql llm raw response table=%s:\n%s", table, sql)

    try:
        sql = extract_sql_from_llm_response(sql)
    except ValueError:
        logger.error("invalid sql generated table=%s raw=%s", table, sql[:1000])
        raise Exception(f"SQL inválido generado: {sql}") from None

    sql = apply_customer_id_placeholder(sql, customer_id)

    try:
        validated = validate_sql(sql)
        _validate_sql_for_route(validated, table)
    except ValueError as exc:
        if table == "vw_fact_bdp_enriched" and not retry_note:
            logger.warning(
                "sql route validation failed table=%s error=%s; retrying",
                table,
                exc,
            )
            return generate_sql(
                question,
                table,
                customer_id,
                schema,
                agent,
                retry_note=f"{_ENRICHED_SQL_WRONG_SOURCE_RETRY_NOTE} ({exc})",
                rubro_hints=rubro_hints,
                history_block=history_block,
                temporal_hints=temporal_hints,
            )
        raise Exception(f"SQL inválido para {table}: {exc}") from exc

    logger.info(
        "sql generated table=%s elapsed_ms=%s len=%s",
        table,
        int((time.perf_counter() - started) * 1000),
        len(validated),
    )
    logger.debug("sql generated table=%s body=%s", table, validated)
    return validated


_ENRICHED_SQL_RETRY_NOTE = (
    "0 filas: si la pregunta es concepto específico (seguros, arrendamiento…), filtra por "
    "nombre_cuenta / sub_nodo_s3 / nombre_auxiliar, NO solo nombre_rubro_grupo. "
    "Usa valores exactos del catálogo. SUM(mvto) sin ABS."
)

_ENRICHED_SQL_GROUPBY_RETRY_NOTE = (
    "Error GROUP BY: en SELECT usa SOLO columnas del GROUP BY más SUM(mvto) (o COUNT). "
    "No incluyas saldo_inicial, saldo_final, movimiento_debito ni movimiento_credito sin agregar."
)

_ENRICHED_SQL_WRONG_SOURCE_RETRY_NOTE = (
    "Debes consultar SOLO gold.vw_fact_bdp_enriched. "
    "PROHIBIDO fact_nota_credito, fact_venta y cualquier tabla silver. "
    "Devoluciones: FROM gold.vw_fact_bdp_enriched WHERE nombre_auxiliar ILIKE '%devoluc%' "
    "o nombre_cuenta ILIKE '%devoluc%'; filtro temporal con anio_mes; SUM(mvto)."
)


def _validate_sql_for_route(sql: str, table: str) -> None:
    norm = (sql or "").lower()
    if table != "vw_fact_bdp_enriched":
        return
    if "vw_fact_bdp_enriched" not in norm:
        raise ValueError("SQL debe usar gold.vw_fact_bdp_enriched")
    for forbidden in _ENRICHED_FORBIDDEN_IN_SQL:
        if forbidden in norm:
            raise ValueError(f"SQL no debe usar {forbidden}")


def _sql_error_kind(exc: Exception) -> str | None:
    msg = str(exc).lower()
    if "groupingerror" in msg or "must appear in the group by" in msg:
        return "groupby"
    if "syntaxerror" in msg or "syntax error" in msg:
        return "syntax"
    return None


def _try_run_sql(sql: str, schema: str, source: str) -> tuple[list | None, str | None]:
    try:
        return run_sql(sql, schema, source=source), None
    except Exception as exc:
        kind = _sql_error_kind(exc)
        if kind:
            logger.warning("sql execution failed kind=%s source=%s", kind, source)
            return None, kind
        raise


def _consult_with_silver_fallback(
    primary_table: str,
    safe_question: str,
    customer_id: str,
    schema: str,
    agent: dict,
    *,
    lookup_question: str | None = None,
    history_block: str = "",
    temporal_hints: str = "",
) -> dict:
    """
    Try gold (or primary) first, then silver_fallback from catalog.
    Returns dict with keys: data (list|None), sql, table, sources, routed_from.
    """
    sources_consulted: list[str] = []
    candidates = query_candidates(primary_table)
    last_sql = ""
    hint_question = lookup_question or safe_question

    for idx, table in enumerate(candidates):
        source = qualified_source(table, schema)
        is_fallback = idx > 0
        logger.info(
            "consulting source=%s table=%s primary=%s fallback=%s",
            source,
            table,
            primary_table,
            is_fallback,
        )
        rubro_hints = None
        if table == "vw_fact_bdp_enriched":
            rubro_hints = _resolve_enriched_hints(hint_question, customer_id)
        sql = generate_sql(
            safe_question,
            table,
            customer_id,
            schema,
            agent,
            rubro_hints=rubro_hints,
            history_block=history_block,
            temporal_hints=temporal_hints,
        )
        last_sql = sql
        data, sql_err = _try_run_sql(sql, schema, source)
        if source not in sources_consulted:
            sources_consulted.append(source)
        if (
            sql_err == "groupby"
            and table == "vw_fact_bdp_enriched"
            and not is_fallback
        ):
            logger.info("enriched query GROUP BY error; retrying with aggregate-only SELECT")
            retry_sql = generate_sql(
                safe_question,
                table,
                customer_id,
                schema,
                agent,
                retry_note=_ENRICHED_SQL_GROUPBY_RETRY_NOTE,
                rubro_hints=rubro_hints,
                history_block=history_block,
                temporal_hints=temporal_hints,
            )
            retry_data, retry_err = _try_run_sql(retry_sql, schema, source)
            if retry_data is not None and not retry_err:
                logger.info("enriched GROUP BY retry succeeded rows=%s", len(retry_data))
                return {
                    "data": retry_data,
                    "sql": retry_sql,
                    "table": table,
                    "sources": sources_consulted,
                    "routed_from": None,
                }
            last_sql = retry_sql
            data = retry_data
        elif sql_err:
            logger.warning(
                "sql execution aborted source=%s table=%s kind=%s",
                source,
                table,
                sql_err,
            )
            data = None
        if not has_real_data(data) and table == "vw_fact_bdp_enriched" and not is_fallback:
            logger.info(
                "enriched query returned 0 rows; retrying with full nombre_rubro_grupo catalog"
            )
            retry_hints = _resolve_enriched_hints(hint_question, customer_id, broad=True)
            retry_sql = generate_sql(
                safe_question,
                table,
                customer_id,
                schema,
                agent,
                retry_note=_ENRICHED_SQL_RETRY_NOTE,
                rubro_hints=retry_hints,
                history_block=history_block,
                temporal_hints=temporal_hints,
            )
            retry_data, retry_err = _try_run_sql(retry_sql, schema, source)
            if has_real_data(retry_data) and not retry_err:
                logger.info("enriched retry succeeded rows=%s", len(retry_data))
                return {
                    "data": retry_data,
                    "sql": retry_sql,
                    "table": table,
                    "sources": sources_consulted,
                    "routed_from": None,
                }
            last_sql = retry_sql
            data = retry_data
        if has_real_data(data):
            if is_fallback:
                logger.info(
                    "silver fallback succeeded primary=%s table=%s rows=%s",
                    primary_table,
                    table,
                    len(data),
                )
            return {
                "data": data,
                "sql": sql,
                "table": table,
                "sources": sources_consulted,
                "routed_from": primary_table if is_fallback else None,
            }
        logger.warning(
            "no real data source=%s table=%s primary=%s fallback=%s",
            source,
            table,
            primary_table,
            is_fallback,
        )

    return {
        "data": None,
        "sql": last_sql,
        "table": primary_table,
        "sources": sources_consulted,
        "routed_from": None,
    }


def run_financial_query(
    question: str,
    customer_id: str,
    schema: str,
    customer_type: str,
    agent_id: str,
    *,
    messages: list | None = None,
):

    pipeline_started = time.perf_counter()
    logger.info(
        "pipeline start agent_id=%s customer_id=%s customer_type=%s schema_param=%s "
        "question_len=%s history_messages=%s",
        agent_id,
        customer_id,
        customer_type,
        schema,
        len(question or ""),
        len(messages or []),
    )
    logger.debug("pipeline question=%r", question)

    agent = get_agent(agent_id)
    schema = agent["schema_name"]
    logger.info("using schema from agent schema_name=%s agent_name=%r", schema, agent.get("name"))

    customer_name = get_customer_name(customer_id)
    privacy_secrets = build_privacy_secrets(customer_id, customer_name)
    safe_question = sanitize_text_for_llm(question, privacy_secrets)
    normalized_history = normalize_messages(messages)
    safe_history = sanitize_messages(
        normalized_history,
        lambda text: sanitize_text_for_llm(text, privacy_secrets),
    )
    history_block, effective_question, temporal_hints = history_for_prompts(
        safe_question, safe_history
    )
    if safe_history:
        logger.info(
            "conversation context turns=%s follow_up_effective=%s periods_hint=%s",
            len(safe_history),
            effective_question != safe_question,
            bool(temporal_hints),
        )

    route = route_question(effective_question, agent, history_block=history_block)
    route = _maybe_route_to_ventas_enriched(effective_question, route)
    route = _maybe_route_to_tablero(effective_question, route)
    route = _maybe_route_to_kpis(effective_question, route)
    route = _prefer_kpi_view(route)

    route_tables = route.get("tables") or []
    sources_planned = qualified_sources(route_tables, schema)
    logger.info(
        "sources to consult count=%s list=%s",
        len(sources_planned),
        sources_planned,
    )

    all_results = []
    all_sql = []
    sources_consulted: list[str] = []

    for table in route_tables:
        hit = _consult_with_silver_fallback(
            table,
            safe_question,
            customer_id,
            schema,
            agent,
            lookup_question=effective_question,
            history_block=history_block,
            temporal_hints=temporal_hints,
        )
        if hit["data"] is None:
            logger.warning(
                "no data after gold+silver fallbacks primary=%s customer_id=%s",
                table,
                customer_id,
            )
            return {
                "route": route,
                "sources_consulted": hit["sources"],
                "sql": [hit["sql"]] if hit.get("sql") else [],
                "data": [],
                "answer": None,
                "customer_answer": "No encontramos información relacionada con tu consulta.",
            }

        for src in hit["sources"]:
            if src not in sources_consulted:
                sources_consulted.append(src)
        all_sql.append(hit["sql"])
        block = {
            "table": hit["table"],
            "sql": hit["sql"],
            "data": hit["data"],
        }
        if hit.get("routed_from"):
            block["routed_from"] = hit["routed_from"]
        all_results.append(block)
        logger.info(
            "consulted table=%s rows=%s routed_from=%s",
            hit["table"],
            len(hit["data"]),
            hit.get("routed_from"),
        )

    llm_results = sanitize_results_for_llm(all_results, privacy_secrets)

    logger.info("generating customer-facing answer customer_type=%s", customer_type)
    customer_answer = generate_customer_answer(
        safe_question, llm_results, agent, history_block=history_block
    )

    logger.info(
        "pipeline done elapsed_ms=%s sources_consulted=%s sql_count=%s",
        int((time.perf_counter() - pipeline_started) * 1000),
        sources_consulted,
        len(all_sql),
    )

    return {
        "route": route,
        "sources_consulted": sources_consulted,
        "sql": all_sql,
        "data": all_results,
        "answer": None,
        "customer_answer": customer_answer,
    }
