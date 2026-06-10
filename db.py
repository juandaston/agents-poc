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


def get_customer_name(customer_id: str) -> str | None:
    """Resolve app.customers.name from UUID (for gold views filtered by nombre_cliente)."""
    logger.info("resolving customer name customer_id=%s", customer_id)
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT name FROM app.customers
            WHERE id = %s::uuid AND deleted_at IS NULL
            LIMIT 1
            """,
            (customer_id,),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            logger.warning("customer name not found customer_id=%s", customer_id)
            return None
        name = str(row[0]).strip()
        logger.info("customer name resolved customer_id=%s", customer_id)
        return name or None
    except Exception:
        logger.exception("failed to resolve customer name customer_id=%s", customer_id)
        raise
    finally:
        cur.close()
        conn.close()


def run_sql(query: str, schema: str, source: str | None = None):
    source_label = source or schema
    logger.info("executing sql source=%s search_path=%s", source_label, schema)
    logger.debug("sql query source=%s body=%s", source_label, query)
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
            "sql done source=%s rows=%s columns=%s elapsed_ms=%s",
            source_label,
            len(result),
            colnames,
            elapsed_ms,
        )
        if result:
            logger.debug("sql first row sample=%s", _sample_row(result[0]))
        return result
    except Exception:
        logger.exception("sql failed source=%s search_path=%s", source_label, schema)
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
