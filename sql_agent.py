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
BASE DE DATOS FINANCIERA (PostgreSQL - schema silver)

TABLAS DISPONIBLES:

1. fact_bdp (balance de prueba)
- id
- customer_id
- codigo_cuenta_contable
- saldo_inicial
- movimiento_debito
- movimiento_credito
- saldo_final
- id_tiempo
- source_date 2025-12-01
- created_at

2. fact_venta (ventas)
- id
- customer_id
- invoice_id
- item_id
- code
- description
- quantity
- price
- total
- created_at

3. presupuesto_proyeccion
- id
- customer_id
- cuenta
- anio_mes
- mes
- saldo
- created_at

4. dim_customers
- id
- customer_id
- name
- city_name
- state_name
- phone
- contact_email

5. dim_accounts
- id
- customer_id
- nombre_cuenta
- nombre_grupo
- nombre_subcuenta
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
- SIEMPRE filtra por customer_id = '{customer_id}'
- Si es necesario y requerido filtrar por tiempo
- máximo 50 rows
- usa SOLO esta tabla a menos que se pida explícitamente join

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
