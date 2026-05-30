import os
import psycopg2
from sqlalchemy import text

def get_connection():
    return  psycopg2.connect(
        os.getenv("DATABASE_URL"),
        sslmode="require",
        connect_timeout=5
    )

def get_agent(agent_id: str):

    conn = get_connection()

    cur = conn.cursor()

    cur.execute("""
        SELECT
            id,
            tenant_id,
            customer_id,
            name,
            model,
            temperature,
            max_tokens,
            top_p,
            frequency_penalty,
            presence_penalty,
            system_prompt,
            schema_name,
            config
        FROM app.agents
        WHERE id = %s
          AND is_active = true
          AND deleted_at IS NULL
    """, (agent_id,))

    row = cur.fetchone()

    if not row:
        raise Exception("Agent not found")

    columns = [desc[0] for desc in cur.description]

    agent = dict(zip(columns, row))

    cur.close()
    conn.close()

    for field in [
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty"
    ]:
        if agent.get(field) is not None:
            agent[field] = float(agent[field])

    return agent

def run_sql(query: str, schema: str):

    conn = get_connection()
    cur = conn.cursor()

    # Neon: schema sigue funcionando normal si lo usas
    cur.execute(f"SET search_path TO {schema};")

    cur.execute(query)
    rows = cur.fetchall()

    colnames = [desc[0] for desc in cur.description]

    result = [dict(zip(colnames, row)) for row in rows]

    cur.close()
    conn.close()

    return result
