import re
import os
import json
import logging
import time
from openai import OpenAI
from utils import has_real_data
from db import run_sql, get_agent, get_customer_name
from security import validate_sql
from data_privacy import (
    CUSTOMER_ID_PLACEHOLDER,
    LLM_SAFETY_INSTRUCTION,
    apply_customer_id_placeholder,
    build_privacy_secrets,
    customer_filter_sql,
    inject_customer_filter,
    sanitize_results_for_llm,
    sanitize_sql_for_llm,
    sanitize_text_for_llm,
)

from semantic_loader import (
    build_intents_list,
    build_kpi_question_pattern,
    build_router_entities_block,
    build_routing_rules_block,
    get_kpi_entity_key,
    get_table_map,
    load_schema_context_text,
    qualified_source,
    qualified_sources,
)

logger = logging.getLogger("agents-poc.sql_agent")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def _schema_context() -> str:
    return load_schema_context_text()


def _kpi_table() -> str:
    return get_kpi_entity_key()


def _table_map() -> dict[str, str]:
    return get_table_map()


def _kpi_question_re() -> re.Pattern[str]:
    return build_kpi_question_pattern()


def _maybe_route_to_kpis(question: str, route: dict) -> dict:
    """Fallback: KPI-style questions should use the pre-aggregated view."""
    kpi_table = _kpi_table()
    tables = route.get("tables") or []
    if kpi_table in tables:
        return route
    if any(t in tables for t in ("fact_venta", "presupuesto_proyeccion", "dim_customers")):
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


def _prefer_kpi_view(route: dict) -> dict:
    """If the router picked the KPI view, answer only from it (already aggregated)."""
    kpi_table = _kpi_table()
    tables = route.get("tables") or []
    if kpi_table in tables:
        logger.info("preferring %s over %s", kpi_table, tables)
        return {**route, "tables": [kpi_table], "intent": route.get("intent") or "kpis"}
    return route


def generate_customer_answer(question, results, agent):
    logger.info("generating customer answer model=%s", agent.get("model"))
    started = time.perf_counter()
    response = client.responses.create(
        model=agent["model"],
        temperature=agent["temperature"],
        top_p=agent["top_p"],
        max_output_tokens=agent["max_tokens"],
        input=f"""
Eres un asistente financiero para clientes NO técnicos.

Tu tarea:
- resumir resultados en lenguaje simple
- máximo 2-3 líneas
- sin tecnicismos
- directo y claro
{LLM_SAFETY_INSTRUCTION}

PREGUNTA:
{question}

RESULTADOS (sin datos identificables):
{results}

RESPONDE SOLO el mensaje final al cliente.
"""
    )

    text = response.output_text.strip()
    logger.info(
        "customer answer ready elapsed_ms=%s chars=%s",
        int((time.perf_counter() - started) * 1000),
        len(text),
    )
    return text

def route_question(question: str, agent: dict):

    logger.info("routing question model=%s", agent.get("model"))
    started = time.perf_counter()
    response = client.responses.create(
        model=agent["model"],
        temperature=agent["temperature"],
        top_p=agent["top_p"],
        max_output_tokens=agent["max_tokens"],
        input=f"""
Eres un router de consultas financieras.

Tablas / vistas disponibles:

{build_router_entities_block()}

REGLAS DE ENRUTAMIENTO:
{build_routing_rules_block()}
- Si implica filtro por fechas, mes, año o periodo, menciónalo en "reason"
  (vw_kpis_financiero → anio / anio_mes; fact_bdp → source_date o gold.dim_time;
  presupuesto → anio_mes; ventas → load_ts).

RESPONDE SOLO JSON así:

{{
  "intent": "{build_intents_list()}",
  "tables": ["{_kpi_table()}"],
  "reason": "..."
}}

Pregunta:
{question}
"""
    )

    text = response.output_text

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


def generate_sql(question, table, customer_id, schema, agent):

    source = qualified_source(table, schema)
    logger.info(
        "generating sql source=%s route_table=%s model=%s",
        source,
        table,
        agent.get("model"),
    )
    started = time.perf_counter()
    kpi_table = _kpi_table()
    table_map = _table_map()
    schema_ctx = _schema_context()

    if table == kpi_table:
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
- NO filtres por cliente ni incluyas nombre_cliente; el servidor aplica el alcance del cliente
- NO selecciones la columna nombre_cliente
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
  En fact_bdp puedes JOIN gold.dim_time t ON t.id_time = {schema}.fact_bdp.id_tiempo
  (califica tablas con schema {schema} para silver; gold.dim_time sin prefijo silver).
  En presupuesto_proyeccion usa anio_mes/mes y deleted_at IS NULL.
- Si la pregunta NO pide filtro temporal, no agregues condiciones de fecha.
- máximo 50 rows
- usa SOLO la tabla {table} más gold.dim_time si hace falta filtrar fechas en fact_bdp
- califica tablas silver como {schema}.nombre_tabla en el SQL

PREGUNTA:
{question}

Devuelve SOLO SQL.
"""

    response = client.responses.create(
        model=agent["model"],
        temperature=agent["temperature"],
        top_p=agent["top_p"],
        max_output_tokens=agent["max_tokens"],
        input=prompt,
    )
    
    sql = response.output_text.strip()
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

    if table == kpi_table:
        filter_clause = customer_filter_sql(customer_id, get_customer_name(customer_id))
        sql = inject_customer_filter(sql, filter_clause)
    else:
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
    route = _maybe_route_to_kpis(safe_question, route)
    route = _prefer_kpi_view(route)

    route_tables = route.get("tables") or []
    sources_planned = qualified_sources(route_tables, schema)
    logger.info(
        "sources to consult count=%s list=%s",
        len(sources_planned),
        sources_planned,
    )

    final_answer = None
    all_results = []
    all_sql = []
    sources_consulted: list[str] = []

    for table in route_tables:
        source = qualified_source(table, schema)
        logger.info("consulting source=%s route_table=%s", source, table)
        sql = generate_sql(safe_question, table, customer_id, schema, agent)

        data = run_sql(sql, schema, source=source)
        sources_consulted.append(source)
        if not has_real_data(data):
            logger.warning(
                "no real data source=%s route_table=%s customer_id=%s",
                source,
                table,
                customer_id,
            )
            return {
                "route": route,
                "sources_consulted": sources_consulted,
                "sql": [sql],
                "data": [],
                "answer": "No se encontraron datos relacionados con la consulta.",
                "customer_answer": "No encontramos información relacionada con tu consulta.",
            }

        all_sql.append(sql)
        all_results.append({
            "table": table,
            "sql": sql,
            "data": data,
        })
        logger.info("consulted source=%s rows=%s", source, len(data))

    llm_results = sanitize_results_for_llm(all_results, privacy_secrets)
    safe_sql_list = [sanitize_sql_for_llm(s, privacy_secrets) for s in all_sql]

    if customer_type == "ADMIN":
        logger.info("generating admin explanation")
        final_answer = explain_results(safe_question, safe_sql_list, llm_results)
    
    logger.info("generating customer-facing answer")
    customer_answer = generate_customer_answer(safe_question, llm_results, agent)

    logger.info(
        "pipeline done elapsed_ms=%s sources_consulted=%s sql_count=%s admin_answer=%s",
        int((time.perf_counter() - pipeline_started) * 1000),
        sources_consulted,
        len(all_sql),
        customer_type == "ADMIN",
    )

    return {
        "route": route,
        "sources_consulted": sources_consulted,
        "sql": all_sql,
        "data": all_results,
        "answer": final_answer,
        "customer_answer": customer_answer
    }

def explain_results(question, sql_list, results):

    logger.info("explain_results sql_count=%s", len(sql_list))
    started = time.perf_counter()
    response = client.responses.create(
        model="gpt-4.1-mini",
        input=f"""
Eres un analista financiero senior.
{LLM_SAFETY_INSTRUCTION}

Pregunta:
{question}

SQL ejecutados (identificadores redactados):
{sql_list}

Resultados (sin columnas identificables):
{results}

Explica de forma clara:
- qué pasó
- insights
- anomalías si existen
"""
    )

    text = response.output_text
    logger.info(
        "admin explanation ready elapsed_ms=%s chars=%s",
        int((time.perf_counter() - started) * 1000),
        len(text or ""),
    )
    return text
