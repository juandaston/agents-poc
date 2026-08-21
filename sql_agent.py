import re
import json
import logging
import time
from utils import has_real_data
from db import run_sql, get_agent, get_customer_name, fetch_nombre_rubro_grupo_candidates
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


def _maybe_route_to_ventas_netas(question: str, route: dict) -> dict:
    """Ventas netas/brutas mensuales → vw_ventas_netas_mes."""
    view = "vw_ventas_netas_mes"
    tables = route.get("tables") or []
    if view in tables:
        return route
    if not _ventas_netas_question_re().search(question or ""):
        return route
    logger.info("ventas netas keywords detected; overriding route to %s", view)
    return {
        **route,
        "tables": [view],
        "intent": "ventas",
        "reason": (route.get("reason") or "") + f" [auto: ventas netas → {view}]",
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


def generate_customer_answer(question, results, agent):
    logger.info("generating customer answer model=%s", agent.get("model"))
    started = time.perf_counter()
    prompt = build_customer_answer_prompt(
        question, results, agent, LLM_SAFETY_INSTRUCTION
    )
    text = complete_text(agent, prompt, max_tokens=512, timeout_sec=18)
    logger.info(
        "customer answer ready elapsed_ms=%s chars=%s",
        int((time.perf_counter() - started) * 1000),
        len(text),
    )
    return text

def route_question(question: str, agent: dict):
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
  ventas netas/brutas mensuales → vw_ventas_netas_mes.anio_mes;
  ventas detalle → fact_venta.invoice_date).
- Si la intención es ambigua o pide montos contables, enruta a vw_fact_bdp_enriched.

RESPONDE SOLO JSON así:

{{
  "intent": "{build_intents_list()}",
  "tables": ["{_primary_table()}"],
  "reason": "..."
}}

Pregunta:
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


_RUBRO_KEYWORD_PATTERNS: list[tuple[tuple[str, ...], str]] = [
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
- Filtra con nombre_rubro_grupo = 'valor exacto de la lista' (respeta mayúsculas y singular/plural).
- NO inventes valores (ej. 'Gastos Financieros' si la lista dice 'Gasto Financiero').
- NO uses nodo_s1 ni nombre_grupo en WHERE para conceptos P&L/balance.
"""


def _resolve_rubro_hints(question: str, customer_id: str, *, broad: bool = False) -> str:
    patterns = None if broad else _rubro_search_patterns(question)
    if broad or not patterns:
        candidates = fetch_nombre_rubro_grupo_candidates(customer_id)
    else:
        candidates = fetch_nombre_rubro_grupo_candidates(
            customer_id, ilike_patterns=patterns
        )
    return _build_rubro_hints_block(candidates)


def generate_sql(
    question,
    table,
    customer_id,
    schema,
    agent,
    *,
    retry_note: str | None = None,
    rubro_hints: str | None = None,
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

    if table == "vw_dim_accounts":
        prompt = f"""
Eres un experto en SQL PostgreSQL financiero.

{schema_ctx}

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

PREGUNTA:
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
{rubro_block}
VISTA ACTUAL:
gold.vw_fact_bdp_enriched

DESCRIPCIÓN:
{table_map.get(table, "")}

REGLAS (OBLIGATORIAS):
- SOLO SELECT sobre gold.vw_fact_bdp_enriched (una sola vista, sin JOINs)
- SIEMPRE filtra por customer_id = '{CUSTOMER_ID_PLACEHOLDER}'
- Montos: mvto (movimiento), saldo_final, saldo_inicial, movimiento_debito, movimiento_credito
- Rubros tablero: nombre_rubro_grupo, nombre_rubro_clase, nodo_s1, nodo_s2, sub_nodo_s3, cod_nodo, uso
- Cuenta: id_auxiliar, codigo_cuenta_contable, nombre_auxiliar, nombre_cuenta
- FECHAS: anio_mes ('YYYY-MM'), anio (int), fecha, source_date
  - Mes: WHERE anio_mes = '2026-06'
  - Año: WHERE anio = 2026 OR anio_mes LIKE '2026-%'
  - Último periodo: ORDER BY anio DESC, anio_mes DESC LIMIT 1 (subconsulta o CTE si agregas)
- FILTROS TABLERO (CRÍTICO — igual que Power BI):
  - SIEMPRE filtra por nombre_rubro_grupo (valor exacto del catálogo del cliente)
  - Si hay lista de valores válidos arriba, usa nombre_rubro_grupo = 'valor exacto'
  - Si no hay lista, nombre_rubro_grupo ILIKE '%palabra%' (ej. '%financier%', '%admin%', '%ingres%')
  - NO uses nodo_s1 ni nombre_grupo en WHERE para conceptos P&L/balance
  - NO pluralices ni inventes labels (ej. 'Gasto Financiero', no 'Gastos Financieros')
  - Ejemplos típicos (verificar en catálogo): 'Gasto Financiero', 'Gasto Admon', 'Ingresos Operacionales'
- Para totales mensuales: SUM(mvto) GROUP BY anio_mes, nombre_rubro_grupo
  - NUNCA uses ABS(mvto) ni SUM(ABS(mvto)): mvto ya trae el signo contable; ABS distorsiona débitos/créditos y no cuadra con Power BI
  - NO incluyas fecha en GROUP BY si ya filtras un solo anio_mes
- NO uses vw_kpis_financiero ni fact_bdp directo; esta vista ya une BDP + rubros + tiempo
- Selecciona solo columnas relevantes; máximo 100 filas
{retry_block}
PREGUNTA:
{question}

Devuelve SOLO SQL.
"""
    elif table == kpi_table:
        prompt = f"""
Eres un experto en SQL PostgreSQL financiero.

{schema_ctx}

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

    # 🔥 limpiar markdown
    sql = sql.replace("```sql", "").replace("```", "").strip()

    # 🔥 extraer SOLO SELECT
    match = re.search(r"(SELECT.*)", sql, re.S | re.I)

    if not match:
        logger.error("invalid sql generated table=%s raw=%s", table, sql[:1000])
        raise Exception(f"SQL inválido generado: {sql}")

    sql = match.group(1).strip()

    sql = apply_customer_id_placeholder(sql, customer_id)

    validated = validate_sql(sql)
    logger.info(
        "sql generated table=%s elapsed_ms=%s len=%s",
        table,
        int((time.perf_counter() - started) * 1000),
        len(validated),
    )
    logger.debug("sql generated table=%s body=%s", table, validated)
    return validated


_ENRICHED_SQL_RETRY_NOTE = (
    "Usa SOLO nombre_rubro_grupo con un valor EXACTO de la lista del catálogo "
    "(no inventes plurales ni uses nodo_s1). GROUP BY anio_mes, nombre_rubro_grupo."
)


def _consult_with_silver_fallback(
    primary_table: str,
    safe_question: str,
    customer_id: str,
    schema: str,
    agent: dict,
) -> dict:
    """
    Try gold (or primary) first, then silver_fallback from catalog.
    Returns dict with keys: data (list|None), sql, table, sources, routed_from.
    """
    sources_consulted: list[str] = []
    candidates = query_candidates(primary_table)
    last_sql = ""

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
            rubro_hints = _resolve_rubro_hints(safe_question, customer_id)
        sql = generate_sql(
            safe_question,
            table,
            customer_id,
            schema,
            agent,
            rubro_hints=rubro_hints,
        )
        last_sql = sql
        data = run_sql(sql, schema, source=source)
        if source not in sources_consulted:
            sources_consulted.append(source)
        if not has_real_data(data) and table == "vw_fact_bdp_enriched" and not is_fallback:
            logger.info(
                "enriched query returned 0 rows; retrying with full nombre_rubro_grupo catalog"
            )
            retry_hints = _resolve_rubro_hints(safe_question, customer_id, broad=True)
            retry_sql = generate_sql(
                safe_question,
                table,
                customer_id,
                schema,
                agent,
                retry_note=_ENRICHED_SQL_RETRY_NOTE,
                rubro_hints=retry_hints,
            )
            retry_data = run_sql(retry_sql, schema, source=source)
            if has_real_data(retry_data):
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


def run_financial_query(question: str, customer_id: str, schema: str, customer_type: str, agent_id: str):

    pipeline_started = time.perf_counter()
    logger.info(
        "pipeline start agent_id=%s customer_id=%s customer_type=%s schema_param=%s question_len=%s",
        agent_id,
        customer_id,
        customer_type,
        schema,
        len(question or ""),
    )
    logger.debug("pipeline question=%r", question)

    agent = get_agent(agent_id)
    schema = agent["schema_name"]
    logger.info("using schema from agent schema_name=%s agent_name=%r", schema, agent.get("name"))

    customer_name = get_customer_name(customer_id)
    privacy_secrets = build_privacy_secrets(customer_id, customer_name)
    safe_question = sanitize_text_for_llm(question, privacy_secrets)

    route = route_question(safe_question, agent)
    route = _maybe_route_to_ventas_netas(safe_question, route)
    route = _maybe_route_to_tablero(safe_question, route)
    route = _maybe_route_to_kpis(safe_question, route)
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
            table, safe_question, customer_id, schema, agent
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
    customer_answer = generate_customer_answer(safe_question, llm_results, agent)

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
