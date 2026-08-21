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


def _agent_select_sql(*, include_provider: bool, include_prompts: bool) -> str:
    provider_col = "a.provider,\n            " if include_provider else ""
    if include_prompts:
        prompt_cols = """
            cap.body AS customer_answer_prompt_body,
            aep.body AS admin_explain_prompt_body,
        """
        prompt_joins = """
        LEFT JOIN app.agent_prompts cap
            ON cap.id = a.customer_answer_prompt_id
           AND cap.deleted_at IS NULL
           AND cap.is_active = TRUE
        LEFT JOIN app.agent_prompts aep
            ON aep.id = a.admin_explain_prompt_id
           AND aep.deleted_at IS NULL
           AND aep.is_active = TRUE
        """
        from_clause = "FROM app.agents a"
    else:
        prompt_cols = ""
        prompt_joins = ""
        from_clause = "FROM app.agents"
    table_prefix = "a." if include_prompts else ""
    return f"""
        SELECT
            {table_prefix}id,
            {table_prefix}tenant_id,
            {table_prefix}customer_id,
            {table_prefix}name,
            {table_prefix}model,
            {provider_col}{table_prefix}temperature,
            {table_prefix}max_tokens,
            {table_prefix}top_p,
            {table_prefix}frequency_penalty,
            {table_prefix}presence_penalty,
            {table_prefix}system_prompt,
            {table_prefix}schema_name,
            {prompt_cols}
            {table_prefix}config
        {from_clause}
        {prompt_joins}
        WHERE {table_prefix}id = %s
          AND {table_prefix}is_active = true
          AND {table_prefix}deleted_at IS NULL
    """


def _load_agent_row(
    cur, agent_id: str, *, include_provider: bool, include_prompts: bool
) -> dict | None:
    cur.execute(
        _agent_select_sql(
            include_provider=include_provider,
            include_prompts=include_prompts,
        ),
        (agent_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    columns = [desc[0] for desc in cur.description]
    return dict(zip(columns, row))


def get_agent(agent_id: str):
    logger.info("loading agent agent_id=%s", agent_id)
    started = time.perf_counter()

    conn = get_connection()
    cur = conn.cursor()

    try:
        agent = None
        try:
            agent = _load_agent_row(
                cur, agent_id, include_provider=True, include_prompts=True
            )
        except psycopg2.errors.UndefinedColumn:
            conn.rollback()
            logger.warning(
                "agent prompt columns missing; loading agent without prompt joins"
            )
            try:
                agent = _load_agent_row(
                    cur, agent_id, include_provider=True, include_prompts=False
                )
            except psycopg2.errors.UndefinedColumn:
                conn.rollback()
                logger.warning(
                    "app.agents.provider column missing; infer provider from model/config"
                )
                agent = _load_agent_row(
                    cur, agent_id, include_provider=False, include_prompts=False
                )
        except psycopg2.errors.UndefinedTable:
            conn.rollback()
            logger.warning("app.agent_prompts missing; loading agent without prompts")
            agent = _load_agent_row(
                cur, agent_id, include_provider=True, include_prompts=False
            )

        if not agent:
            logger.warning("agent not found agent_id=%s", agent_id)
            raise Exception("Agent not found")

        for field in [
            "temperature",
            "top_p",
            "frequency_penalty",
            "presence_penalty",
        ]:
            if agent.get(field) is not None:
                agent[field] = float(agent[field])

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        customer_body = (agent.get("customer_answer_prompt_body") or "").strip()
        admin_body = (agent.get("admin_explain_prompt_body") or "").strip()
        system_prompt = (agent.get("system_prompt") or "").strip()
        logger.info(
            "agent loaded agent_id=%s name=%r model=%s provider=%s schema_name=%s "
            "customer_prompt_body=%s admin_prompt_body=%s system_prompt=%s elapsed_ms=%s",
            agent_id,
            agent.get("name"),
            agent.get("model"),
            agent.get("provider"),
            agent.get("schema_name"),
            f"{len(customer_body)} chars" if customer_body else "none",
            f"{len(admin_body)} chars" if admin_body else "none",
            f"{len(system_prompt)} chars" if system_prompt else "none",
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


def fetch_nombre_rubro_grupo_candidates(
    customer_id: str,
    ilike_patterns: list[str] | None = None,
    *,
    limit: int = 25,
) -> list[str]:
    """Distinct nombre_rubro_grupo from gold.vw_dim_accounts for SQL hint injection."""
    logger.info(
        "fetching nombre_rubro_grupo candidates customer_id=%s patterns=%s",
        customer_id,
        ilike_patterns,
    )
    conn = get_connection()
    cur = conn.cursor()
    try:
        if ilike_patterns:
            conditions = " OR ".join(
                "nombre_rubro_grupo ILIKE %s" for _ in ilike_patterns
            )
            params: list = [customer_id, *ilike_patterns, limit]
            cur.execute(
                f"""
                SELECT DISTINCT nombre_rubro_grupo
                FROM gold.vw_dim_accounts
                WHERE customer_id = %s::uuid
                  AND nombre_rubro_grupo IS NOT NULL
                  AND TRIM(nombre_rubro_grupo) <> ''
                  AND nombre_rubro_grupo <> 'Sin Clasificar'
                  AND ({conditions})
                ORDER BY 1
                LIMIT %s
                """,
                params,
            )
        else:
            cur.execute(
                """
                SELECT DISTINCT nombre_rubro_grupo
                FROM gold.vw_dim_accounts
                WHERE customer_id = %s::uuid
                  AND nombre_rubro_grupo IS NOT NULL
                  AND TRIM(nombre_rubro_grupo) <> ''
                  AND nombre_rubro_grupo <> 'Sin Clasificar'
                ORDER BY 1
                LIMIT %s
                """,
                (customer_id, limit),
            )
        names = [str(row[0]).strip() for row in cur.fetchall() if row and row[0]]
        logger.info(
            "nombre_rubro_grupo candidates customer_id=%s count=%s",
            customer_id,
            len(names),
        )
        return names
    except Exception:
        logger.exception(
            "failed to fetch nombre_rubro_grupo candidates customer_id=%s",
            customer_id,
        )
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
