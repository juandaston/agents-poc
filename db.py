import logging
import os
import time

import psycopg2

logger = logging.getLogger("agents-poc.db")


def get_connection():
    logger.debug("opening database connection")
    return psycopg2.connect(
        os.getenv("DATABASE_URL"),
        sslmode="require",
        connect_timeout=5,
    )


def get_agent(agent_id: str):
    logger.info("loading agent agent_id=%s", agent_id)
    started = time.perf_counter()

    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(
            """
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
    """,
            (agent_id,),
        )

        row = cur.fetchone()

        if not row:
            logger.warning("agent not found agent_id=%s", agent_id)
            raise Exception("Agent not found")

        columns = [desc[0] for desc in cur.description]
        agent = dict(zip(columns, row))

        for field in [
            "temperature",
            "top_p",
            "frequency_penalty",
            "presence_penalty",
        ]:
            if agent.get(field) is not None:
                agent[field] = float(agent[field])

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "agent loaded agent_id=%s name=%r model=%s schema_name=%s elapsed_ms=%s",
            agent_id,
            agent.get("name"),
            agent.get("model"),
            agent.get("schema_name"),
            elapsed_ms,
        )
        return agent
    except Exception:
        logger.exception("failed to load agent agent_id=%s", agent_id)
        raise
    finally:
        cur.close()
        conn.close()


def run_sql(query: str, schema: str):
    logger.info("executing sql schema=%s", schema)
    logger.debug("sql query=%s", query)
    started = time.perf_counter()

    conn = get_connection()
    cur = conn.cursor()

    try:
        cur.execute(f"SET search_path TO {schema};")
        cur.execute(query)
        rows = cur.fetchall()
        colnames = [desc[0] for desc in cur.description] if cur.description else []
        result = [dict(zip(colnames, row)) for row in rows]
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "sql done schema=%s rows=%s columns=%s elapsed_ms=%s",
            schema,
            len(result),
            colnames,
            elapsed_ms,
        )
        if result:
            logger.debug("sql first row sample=%s", _sample_row(result[0]))
        return result
    except Exception:
        logger.exception("sql failed schema=%s query=%s", schema, query)
        raise
    finally:
        cur.close()
        conn.close()


def _sample_row(row: dict, max_len: int = 120) -> dict:
    out = {}
    for key, value in row.items():
        text = str(value)
        if len(text) > max_len:
            text = text[: max_len - 1] + "…"
        out[key] = text
    return out
