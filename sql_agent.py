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

logger = logging.getLogger("agents-poc.sql_agent")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SCHEMA_CONTEXT = """
BASE DE DATOS FINANCIERA (PostgreSQL — schema silver, referencias gold)

REGLAS GLOBALES:
- Filtra SIEMPRE por customer_id en tablas silver que lo tengan (dim_accounts, fact_venta, fact_bdp, presupuesto_proyeccion).
- gold.vw_kpis_financiero NO tiene customer_id; filtra por nombre_cliente (nombre en app.customers).
- PREFERIR gold.vw_kpis_financiero cuando la pregunta sea de KPIs/ratios/estado de resultados agregado
  (utilidad, márgenes, ROE, ROA, liquidez, endeudamiento, semáforos, activo/pasivo/patrimonio totales).
- dim_customers.customer_id es varchar (identificador de negocio), NO confundir con uuid customer_id de hechos.
- fact_bdp.id_tiempo referencia gold.dim_time(id_time).
- presupuesto_proyeccion.anio_mes formato 'YYYY-MM' (CHECK ^\\d{4}-(0[1-9]|1[0-2])$); mes es columna generada (1-12).
- Tablas test_* son entornos de prueba; preferir tablas productivas salvo que se indique lo contrario.

──────────────────────────────────────────────────────────────────────────────
FILTRADO POR FECHAS Y PERIODOS (usar cuando la pregunta lo pida o implique tiempo)
──────────────────────────────────────────────────────────────────────────────
Detectar en la pregunta: fechas concretas, rangos ("entre enero y marzo"), mes, año,
trimestre, "último periodo", "periodo actual", "este mes", "año pasado", etc.

gold.dim_time — dimensión calendario (JOIN desde fact_bdp.id_tiempo)
- id_time       serial PK
- Date          date UNIQUE NOT NULL — fecha calendario
- Anio          int NOT NULL
- AnioMes       varchar(7) NOT NULL — 'YYYY-MM' (equivalente a presupuesto.anio_mes)
- AnioMesDia    varchar(10) NULL
- Dia           int NULL
- Mes           int NOT NULL (1-12)
- MesNumero     int NOT NULL
- Trimestre     int NULL
- SemanaAnno    int NULL

Índices útiles: Date, (Anio, Mes), AnioMes, (Anio, Trimestre)

Por tabla — columna / patrón recomendado:

| Tabla                  | Filtrar por                                                                 |
|------------------------|-----------------------------------------------------------------------------|
| fact_bdp               | source_date (date del extracto) O JOIN gold.dim_time vía id_tiempo          |
| vw_kpis_financiero     | anio (int), anio_mes ('YYYY-MM'), mes_corto; filtrar por nombre_cliente     |
| presupuesto_proyeccion | anio_mes ('YYYY-MM'), mes (1-12); siempre deleted_at IS NULL               |
| fact_venta             | load_ts::date (fecha de carga; no hay fecha de factura en el modelo)        |
| dim_accounts           | load_ts solo si pregunta por actualización del catálogo                     |
| dim_customers          | load_ts / metadata_last_updated solo si aplica                              |

fact_bdp — ejemplos de filtro temporal:
- Día exacto:     WHERE f.source_date = DATE '2025-12-01'
- Rango:          WHERE f.source_date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
- Mes/año:        JOIN gold.dim_time t ON t.id_time = f.id_tiempo
                  WHERE t.Anio = 2025 AND t.Mes = 12
- Por AnioMes:    JOIN gold.dim_time t ON t.id_time = f.id_tiempo
                  WHERE t.AnioMes = '2025-12'
- Último periodo: WHERE f.source_date = (
                    SELECT MAX(f2.source_date) FROM {schema}.fact_bdp f2
                    WHERE f2.customer_id = f.customer_id
                  )
  (alternativa: MAX(t.AnioMes) vía join)

presupuesto_proyeccion — ejemplos:
- Mes concreto:   WHERE anio_mes = '2025-03' AND deleted_at IS NULL
- Año completo:   WHERE anio_mes LIKE '2025-%' AND deleted_at IS NULL
- Por mes num:    WHERE mes = 3 AND anio_mes LIKE '2025-%' AND deleted_at IS NULL
- Último mes:     WHERE anio_mes = (
                    SELECT MAX(p.anio_mes) FROM {schema}.presupuesto_proyeccion p
                    WHERE p.customer_id = presupuesto_proyeccion.customer_id
                      AND p.deleted_at IS NULL
                  )

fact_venta — ejemplos:
- Rango carga:    WHERE load_ts::date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'
- Mes de carga:   WHERE DATE_TRUNC('month', load_ts) = DATE '2025-12-01'

Reglas de filtrado temporal:
- Si la pregunta NO menciona tiempo, NO agregues filtros de fecha (salvo deleted_at en presupuesto).
- Si menciona tiempo, SIEMPRE filtra; no devuelvas toda la historia del cliente.
- Para balance vs presupuesto en el mismo periodo, alinea fact_bdp (source_date o t.AnioMes)
  con presupuesto.anio_mes.
- Usa literales DATE 'YYYY-MM-DD' o anio_mes 'YYYY-MM'; evita funciones no deterministas innecesarias.

──────────────────────────────────────────────────────────────────────────────
1. silver.dim_accounts — plan de cuentas / auxiliares contables por cliente
──────────────────────────────────────────────────────────────────────────────
PK: id (serial4)
UNIQUE: (customer_id, id_auxiliar)

Columnas:
- id                  serial4 NOT NULL — PK
- integration_id      uuid NOT NULL
- customer_id         uuid NOT NULL — filtrar consultas por este campo
- id_auxiliar         varchar(150) NOT NULL
- nombre_auxiliar     varchar(255) NULL
- id_subcuenta        varchar(150) NULL
- nombre_subcuenta    varchar(255) NULL
- id_cuenta           varchar(150) NULL
- nombre_cuenta       varchar(255) NULL
- id_grupo            varchar(150) NULL
- nombre_grupo        varchar(255) NULL
- id_clase            varchar(150) NULL
- nombre_clase        varchar(255) NULL
- load_ts             timestamp DEFAULT CURRENT_TIMESTAMP NULL
- source_table        varchar(100) NULL

Índices:
- idx_dim_accounts_customer_id ON (customer_id)

Uso: joins por codigo/nombre de cuenta; jerarquía auxiliar → subcuenta → cuenta → grupo → clase.

──────────────────────────────────────────────────────────────────────────────
2. silver.dim_customers — maestro de clientes (dimension)
──────────────────────────────────────────────────────────────────────────────
PK: id (serial4)

Columnas:
- id                      serial4 NOT NULL — PK
- customer_id             varchar(50) NULL — id negocio (texto)
- type                    varchar(50) NULL
- person_type             varchar(50) NULL
- identification          varchar(100) NULL
- name                    varchar(255) NULL
- address                 varchar(255) NULL
- state_name              varchar(100) NULL
- city_name               varchar(100) NULL
- phone                   varchar(50) NULL
- contact_email           varchar(255) NULL
- metadata_created        varchar(50) NULL
- metadata_last_updated   varchar(50) NULL
- load_ts                 timestamp DEFAULT CURRENT_TIMESTAMP NULL

Uso: datos demográficos/contacto; NO tiene uuid customer_id de hechos.

──────────────────────────────────────────────────────────────────────────────
3. silver.fact_venta — líneas de facturación / ventas
──────────────────────────────────────────────────────────────────────────────
PK: id (serial4)

Columnas:
- id              serial4 NOT NULL — PK
- invoice_id      varchar(50) NULL
- item_id         varchar(50) NULL
- code            varchar(100) NULL
- description     text NULL
- quantity        numeric(15, 4) NULL
- price           numeric(15, 2) NULL
- total           numeric(15, 2) NULL
- taxes_raw       text NULL
- load_ts         timestamp DEFAULT CURRENT_TIMESTAMP NULL
- integration_id  uuid NULL
- customer_id     uuid NULL — filtrar consultas por este campo

Índices:
- idx_fact_venta_customer_id ON (customer_id)
- idx_fact_venta_integration_id ON (integration_id)

Uso: ventas por factura, producto, totales; agregaciones SUM(total), SUM(quantity).

──────────────────────────────────────────────────────────────────────────────
4. silver.presupuesto_proyeccion — presupuesto / proyección mensual por cuenta
──────────────────────────────────────────────────────────────────────────────
PK: id (uuid, default gen_random_uuid())
UNIQUE: (integration_id, cuenta, anio_mes)

Columnas:
- id              uuid NOT NULL — PK
- integration_id  uuid NOT NULL
- customer_id     uuid NULL — filtrar consultas por este campo
- cuenta          int8 NOT NULL — código numérico de cuenta
- cuenta_contable text NOT NULL — nombre/descripción cuenta
- anio_mes        varchar(7) NOT NULL — 'YYYY-MM'
- mes             int2 GENERATED (1-12) desde anio_mes
- saldo           numeric(18, 6) DEFAULT 0 NOT NULL
- created_at      timestamptz DEFAULT now() NULL
- updated_at      timestamptz DEFAULT now() NULL
- deleted_at      timestamptz NULL — excluir filas con deleted_at IS NOT NULL

Constraints:
- chk_presupuesto_anio_mes: anio_mes ~ '^\\d{4}-(0[1-9]|1[0-2])$'
- chk_presupuesto_mes: mes entre 1 y 12

Índices:
- idx_presupuesto_customer_id ON (customer_id) WHERE deleted_at IS NULL
- idx_presupuesto_integration_anio_mes ON (integration_id, anio_mes)
- idx_presupuesto_integration_cuenta ON (integration_id, cuenta)

Uso: comparar presupuesto vs real; filtrar por anio_mes o mes; SUM(saldo) por cuenta_contable.

──────────────────────────────────────────────────────────────────────────────
5. silver.fact_bdp — balance de prueba (movimientos y saldos contables)
──────────────────────────────────────────────────────────────────────────────
PK: id (serial4)
FK: id_tiempo → gold.dim_time(id_time)

Columnas:
- id                      serial4 NOT NULL — PK
- integration_id          uuid NOT NULL
- customer_id             uuid NULL — filtrar consultas por este campo
- codigo_cuenta_contable  varchar(150) NULL — join con dim_accounts / presupuesto
- saldo_inicial           numeric(15, 2) NULL
- movimiento_debito       numeric(15, 2) NULL
- movimiento_credito      numeric(15, 2) NULL
- saldo_final             numeric(15, 2) NULL
- mvto                    numeric(15, 2) NULL — movimiento neto
- id_tiempo               int4 NOT NULL — FK gold.dim_time
- source_date             date NULL — fecha origen del extracto
- nombre_archivo          varchar(255) NULL
- created_at              timestamp DEFAULT CURRENT_TIMESTAMP NULL

Índices:
- idx_fact_bdp_codigo_cuenta ON (codigo_cuenta_contable)
- idx_fact_bdp_customer_id ON (customer_id)
- idx_fact_bdp_id_tiempo ON (id_tiempo)
- idx_fact_bdp_integration_nombre_archivo ON (integration_id, nombre_archivo)
- idx_fact_bdp_nombre_archivo ON (nombre_archivo)
- idx_fact_bdp_source_date ON (source_date)

Uso: balance de prueba; saldo_final total; variaciones por cuenta y periodo (id_tiempo / source_date).

──────────────────────────────────────────────────────────────────────────────
6. gold.vw_kpis_financiero — KPIs financieros pre-calculados (PREFERIR para preguntas de KPIs)
──────────────────────────────────────────────────────────────────────────────
Vista en schema gold. Agrega fact_bdp + gold.vw_dim_accounts + gold.dim_time + app.customers.
Una fila por cliente, año y mes (anio_mes). NO requiere JOINs adicionales.

Dimensiones:
- anio              int — año calendario
- anio_mes          varchar(7) — 'YYYY-MM'
- mes_corto         varchar — nombre corto del mes
- nombre_cliente    varchar — nombre en app.customers (filtro de cliente)

Balance / estructura patrimonial:
- activo_corriente, activo_no_corriente, activo_total
- pasivo_corriente, pasivo_no_corriente, pasivo_total
- patrimonio_total

Estado de resultados:
- ingresos_operacionales, ingresos_no_operacionales
- costo_ventas_total, materia_prima, mano_obra_directa, costos_indirectos
- gastos_administrativos, gastos_ventas, gastos_financieros, impuesto_renta
- utilidad_bruta, utilidad_operacional, utilidad_antes_impuestos, utilidad_neta

Ratios y métricas:
- razon_corriente, capital_trabajo_neto, apalancamiento
- pct_endeudamiento_total, pct_endeudamiento_corto_plazo, pct_autonomia_financiera
- pct_margen_bruto, pct_margen_operacional, pct_margen_neto
- pct_roe, pct_roa
- pct_gastos_admin_sobre_ingresos, pct_gastos_ventas_sobre_ingresos, pct_costo_ventas_sobre_ingresos

Semáforos (VERDE | AMARILLO | ROJO):
- semaforo_liquidez, semaforo_endeudamiento, semaforo_margen_bruto, semaforo_utilidad_neta

Filtro de cliente:
  NO lo incluyas en el SQL; el servidor añade el filtro por nombre_cliente automáticamente.

Filtro temporal — ejemplos (solo periodo; sin filtro de cliente):
- Mes:        WHERE anio_mes = '2025-03'
- Año:        WHERE anio = 2025
- Rango mes:  WHERE anio_mes BETWEEN '2025-01' AND '2025-06'
- Último mes: ORDER BY anio DESC, anio_mes DESC LIMIT 1
  (o subconsulta MAX(anio_mes) sin filtrar por nombre de cliente)

Uso típico: margen bruto, utilidad neta, ROE, ROA, liquidez, endeudamiento, semáforos,
comparación de periodos, evolución de KPIs. NO usar fact_bdp si esta vista responde la pregunta.

──────────────────────────────────────────────────────────────────────────────
7. silver.test_dim_accounts — plan de cuentas (PRUEBAS, sin customer_id)
──────────────────────────────────────────────────────────────────────────────
PK: id (serial4)
UNIQUE: (integration_id, id_auxiliar)

Misma estructura jerárquica que dim_accounts (id_auxiliar … nombre_clase, load_ts, source_table)
pero keyed por integration_id; NO tiene customer_id.

──────────────────────────────────────────────────────────────────────────────
8. silver.test_fact_bdp — balance de prueba (PRUEBAS)
──────────────────────────────────────────────────────────────────────────────
PK: id (serial4)
FK: id_tiempo → gold.test_dim_time(id_time)

Columnas: integration_id, codigo_cuenta_contable, saldo_inicial, movimiento_debito,
movimiento_credito, saldo_final, mvto, id_tiempo, source_date (NOT NULL), nombre_archivo, created_at.
Sin customer_id.

──────────────────────────────────────────────────────────────────────────────
RELACIONES ÚTILES PARA JOINS
──────────────────────────────────────────────────────────────────────────────
- fact_bdp.codigo_cuenta_contable ↔ dim_accounts.id_auxiliar / id_cuenta (mismo customer_id)
- fact_bdp.id_tiempo ↔ gold.dim_time.id_time (fecha/periodo)
- presupuesto_proyeccion.cuenta / cuenta_contable ↔ fact_bdp.codigo_cuenta_contable (mismo customer_id + periodo)
- fact_venta.customer_id = dim_accounts.customer_id = fact_bdp.customer_id = presupuesto_proyeccion.customer_id
- vw_kpis_financiero.nombre_cliente ↔ app.customers.name (derivado de fact_bdp.customer_id)
"""

TABLE_MAP = {
    "fact_bdp": "balance de prueba",
    "fact_venta": "ventas",
    "presupuesto_proyeccion": "presupuesto",
    "dim_customers": "clientes",
    "dim_accounts": "plan de cuentas",
    "vw_kpis_financiero": "KPIs financieros pre-calculados (balance, P&L, ratios, semáforos)",
}

KPI_TABLE = "vw_kpis_financiero"

GOLD_SOURCES = frozenset({KPI_TABLE})


def qualified_source(table: str, schema: str) -> str:
    """Nombre calificado schema.tabla o schema.vista para logs."""
    if table in GOLD_SOURCES:
        return f"gold.{table}"
    return f"{schema}.{table}"


def qualified_sources(tables: list[str], schema: str) -> list[str]:
    return [qualified_source(t, schema) for t in tables]


KPI_QUESTION_RE = re.compile(
    r"\b(kpi|kpis|margen|utilidad|roe|roa|liquidez|endeudamiento|apalancamiento|"
    r"sem[aá]foro|raz[oó]n\s+corriente|capital\s+de\s+trabajo|"
    r"patrimonio|autonom[ií]a|ingresos\s+operacionales|"
    r"utilidad\s+neta|utilidad\s+bruta|utilidad\s+operacional|"
    r"costo\s+de\s+ventas|gastos\s+administrativos|estado\s+de\s+resultados)\b",
    re.IGNORECASE,
)


def _maybe_route_to_kpis(question: str, route: dict) -> dict:
    """Fallback: KPI-style questions should use the pre-aggregated view."""
    tables = route.get("tables") or []
    if KPI_TABLE in tables:
        return route
    if any(t in tables for t in ("fact_venta", "presupuesto_proyeccion", "dim_customers")):
        return route
    if not KPI_QUESTION_RE.search(question or ""):
        return route
    logger.info("kpi keywords detected; overriding route to %s", KPI_TABLE)
    return {
        **route,
        "tables": [KPI_TABLE],
        "intent": "kpis",
        "reason": (route.get("reason") or "") + " [auto: KPI keywords → vw_kpis_financiero]",
    }


def _prefer_kpi_view(route: dict) -> dict:
    """If the router picked the KPI view, answer only from it (already aggregated)."""
    tables = route.get("tables") or []
    if KPI_TABLE in tables:
        logger.info("preferring %s over %s", KPI_TABLE, tables)
        return {**route, "tables": [KPI_TABLE], "intent": route.get("intent") or "kpis"}
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

1. vw_kpis_financiero → KPIs financieros PRE-CALCULADOS (PREFERIR para preguntas de KPIs)
   Utilidad bruta/operacional/neta, márgenes %, ROE, ROA, liquidez (razón corriente),
   endeudamiento, apalancamiento, capital de trabajo, activo/pasivo/patrimonio totales,
   ingresos, costos, gastos agregados, semáforos (liquidez, endeudamiento, margen, utilidad).
2. fact_bdp → balance de prueba (movimientos contables por cuenta; detalle granular)
3. fact_venta → ventas
4. presupuesto_proyeccion → presupuesto
5. dim_customers → clientes
6. dim_accounts → plan de cuentas

REGLAS DE ENRUTAMIENTO:
- Si la pregunta se puede responder con KPIs agregados, ratios o semáforos → SOLO vw_kpis_financiero.
  NO combines fact_bdp ni dim_accounts si la vista basta.
- Usa fact_bdp solo para detalle por cuenta, movimientos, saldos por código contable.
- Si la pregunta requiere varias fuentes distintas (ej. ventas + presupuesto), devuelve varias tablas.
- Si implica filtro por fechas, mes, año o periodo, menciónalo en "reason"
  (vw_kpis_financiero → anio / anio_mes; fact_bdp → source_date o gold.dim_time;
  presupuesto → anio_mes; ventas → load_ts).

RESPONDE SOLO JSON así:

{{
  "intent": "kpis | ventas | balance | presupuesto | clientes | cuentas | mixto",
  "tables": ["vw_kpis_financiero"],
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

    if table == KPI_TABLE:
        prompt = f"""
Eres un experto en SQL PostgreSQL financiero.

{SCHEMA_CONTEXT}

VISTA ACTUAL:
gold.vw_kpis_financiero

DESCRIPCIÓN:
{TABLE_MAP[KPI_TABLE]}

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

{SCHEMA_CONTEXT}

CONTEXTO:
schema silver: {schema}

TABLA ACTUAL:
{table}

DESCRIPCIÓN:
{TABLE_MAP.get(table, "")}

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

    if table == KPI_TABLE:
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
