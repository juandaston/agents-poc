import re
import os
import json
from openai import OpenAI
from utils import has_real_data
from db import run_sql
from security import validate_sql
from db import get_agent

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SCHEMA_CONTEXT = """
BASE DE DATOS FINANCIERA (PostgreSQL — schema silver, referencias gold)

REGLAS GLOBALES:
- Filtra SIEMPRE por customer_id en tablas que lo tengan (dim_accounts, fact_venta, fact_bdp, presupuesto_proyeccion).
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
6. silver.test_dim_accounts — plan de cuentas (PRUEBAS, sin customer_id)
──────────────────────────────────────────────────────────────────────────────
PK: id (serial4)
UNIQUE: (integration_id, id_auxiliar)

Misma estructura jerárquica que dim_accounts (id_auxiliar … nombre_clase, load_ts, source_table)
pero keyed por integration_id; NO tiene customer_id.

──────────────────────────────────────────────────────────────────────────────
7. silver.test_fact_bdp — balance de prueba (PRUEBAS)
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
"""

def generate_customer_answer(question, results, agent):
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

PREGUNTA:
{question}

RESULTADOS:
{results}

RESPONDE SOLO el mensaje final al cliente.
"""
    )

    return response.output_text.strip()

def route_question(question: str, agent: dict):

    response = client.responses.create(
        model=agent["model"],
        temperature=agent["temperature"],
        top_p=agent["top_p"],
        max_output_tokens=agent["max_tokens"],
        input=f"""
Eres un router de consultas financieras.

Tablas disponibles:

1. fact_bdp → balance de prueba (movimientos contables)
2. fact_venta → ventas
3. presupuesto_proyeccion → presupuesto
4. dim_customers → clientes
5. dim_accounts → plan de cuentas

Si la pregunta requiere varias tablas, devuelve varias.
Si implica filtro por fechas, mes, año o periodo, menciónalo en "reason"
(fact_bdp → source_date o gold.dim_time; presupuesto → anio_mes; ventas → load_ts).

RESPONDE SOLO JSON así:

{{
  "intent": "ventas | balance | presupuesto | clientes | cuentas | mixto",
  "tables": ["fact_venta"],
  "reason": "..."
}}

Pregunta:
{question}
"""
    )

    text = response.output_text

    # limpiar posibles ```json
    text = text.replace("```json", "").replace("```", "").strip()

    return json.loads(text)


def generate_sql(question, table, customer_id, schema, agent):

    table_map = {
        "fact_bdp": "balance de prueba",
        "fact_venta": "ventas",
        "presupuesto_proyeccion": "presupuesto",
        "dim_customers": "clientes",
        "dim_accounts": "plan de cuentas"
    }

    response = client.responses.create(
        model=agent["model"],
        temperature=agent["temperature"],
        top_p=agent["top_p"],
        max_output_tokens=agent["max_tokens"],
        input=f"""
Eres un experto en SQL PostgreSQL financiero.

{SCHEMA_CONTEXT}

CONTEXTO:
schema: {schema}
customer_id: {customer_id}

TABLA ACTUAL:
{table}

DESCRIPCIÓN:
{table_map.get(table, "")}

REGLAS:
- SOLO SELECT
- SIEMPRE filtra por customer_id = '{customer_id}' en tablas que tengan esa columna
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
    )
    
    sql = response.output_text.strip()

    # 🔥 limpiar markdown
    sql = sql.replace("```sql", "").replace("```", "").strip()

    # 🔥 extraer SOLO SELECT
    match = re.search(r"(SELECT.*)", sql, re.S | re.I)

    if not match:
        raise Exception(f"SQL inválido generado: {sql}")

    sql = match.group(1).strip()

    return validate_sql(sql)

def run_financial_query(question: str, customer_id: str, schema: str, customer_type: str, agent_id: str):

    agent = get_agent(agent_id)
    schema = agent["schema_name"]

    route = route_question(question, agent)
    
    final_answer = None
    all_results = []
    all_sql = []

    for table in route["tables"]:

        sql = generate_sql(question, table, customer_id, schema, agent)

        data = run_sql(sql, schema)
        # ahorrar tokens si no hay data
        if not has_real_data(data):
            return {
                "route": route,
                "sql": [sql],
                "data": [],
                "answer": "No se encontraron datos relacionados con la consulta.",
                "customer_answer": "No encontramos información relacionada con tu consulta."
            }

        all_sql.append(sql)
        all_results.append({
            "table": table,
            "sql": sql,
            "data": data
        })

    if customer_type == "ADMIN":
        final_answer = explain_results(question, all_sql, all_results)
    
    # NUEVO: respuesta cliente
    customer_answer = generate_customer_answer(question, all_results, agent)

    return {
        "route": route,
        "sql": all_sql,
        "data": all_results,
        "answer": final_answer,
        "customer_answer": customer_answer
    }

def explain_results(question, sql_list, results):

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=f"""
Eres un analista financiero senior.

Pregunta:
{question}

SQL ejecutados:
{sql_list}

Resultados:
{results}

Explica de forma clara:
- qué pasó
- insights
- anomalías si existen
"""
    )

    return response.output_text
