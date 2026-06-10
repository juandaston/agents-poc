"""
Capa de privacidad: datos sensibles no deben enviarse a modelos LLM.

- Filtros de cliente se inyectan en el servidor (SQL), no los genera el modelo.
- customer_id se usa como placeholder en prompts y se sustituye tras la generación.
- Resultados y SQL se sanitizan antes de explain_results / generate_customer_answer.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("agents-poc.privacy")

CUSTOMER_ID_PLACEHOLDER = "__CUSTOMER_ID__"

# Columnas que no se envían al modelo (se omiten del payload).
SENSITIVE_COLUMN_NAMES = frozenset({
    "nombre_cliente",
    "name",
    "nombre",
    "customer_name",
    "nombre_auxiliar",
    "nombre_subcuenta",
    "nombre_cuenta",
    "nombre_grupo",
    "nombre_clase",
    "nombre_rubro_grupo",
    "nombre_rubro_clase",
    "email",
    "phone",
    "nit",
    "cod",
    "notes",
    "username",
    "nombre_archivo",
    "metadata_customer_name",
    "metadata_customer_email",
})

_BOUNDARY_TOKENS = (" order by ", " group by ", " limit ", " offset ", " fetch ")


def build_privacy_secrets(
    customer_id: str | None,
    customer_name: str | None,
    extra: list[str] | None = None,
) -> list[str]:
    secrets: list[str] = []
    for value in (customer_name, customer_id, *(extra or [])):
        if value and str(value).strip():
            secrets.append(str(value).strip())
    return secrets


def sanitize_text_for_llm(text: str, secrets: list[str]) -> str:
    """Redact known secrets from free text (pregunta del usuario, etc.)."""
    if not text or not secrets:
        return text
    out = text
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) < 2:
            continue
        out = re.sub(re.escape(secret), "[CLIENTE]", out, flags=re.IGNORECASE)
    return out


def sanitize_sql_for_llm(sql: str, secrets: list[str]) -> str:
    if not sql:
        return sql
    redacted = sanitize_text_for_llm(sql, secrets)
    # UUIDs embebidos en SQL
    redacted = re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "[UUID]",
        redacted,
        flags=re.IGNORECASE,
    )
    return redacted


def sanitize_rows_for_llm(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not rows:
        return []
    clean: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        clean.append({
            k: v
            for k, v in row.items()
            if str(k).lower() not in SENSITIVE_COLUMN_NAMES
        })
    return clean


def sanitize_results_for_llm(
    results: list[dict[str, Any]],
    secrets: list[str],
) -> list[dict[str, Any]]:
    """Copia de resultados del pipeline lista para prompts LLM."""
    out: list[dict[str, Any]] = []
    for block in results:
        data = block.get("data")
        rows = data if isinstance(data, list) else []
        out.append({
            "table": block.get("table"),
            "row_count": len(rows),
            "data": sanitize_rows_for_llm(rows),
        })
    if secrets:
        logger.debug(
            "sanitized %s result blocks for llm (secrets redacted=%s)",
            len(out),
            len(secrets),
        )
    return out


def customer_filter_sql(customer_id: str, customer_name: str | None = None) -> str:
    """Filtro de cliente para vistas gold por nombre_cliente (solo servidor)."""
    if customer_name:
        escaped = customer_name.replace("'", "''")
        return f"nombre_cliente = '{escaped}'"
    return (
        f"nombre_cliente = (SELECT name FROM app.customers "
        f"WHERE id = '{customer_id}'::uuid AND deleted_at IS NULL LIMIT 1)"
    )


def inject_customer_filter(sql: str, filter_clause: str) -> str:
    """Añade filtro de cliente al SQL generado (vw_kpis_financiero y similares)."""
    sql = sql.strip().rstrip(";")
    lowered = sql.lower()

    insert_at = len(sql)
    for token in _BOUNDARY_TOKENS:
        idx = lowered.find(token)
        if idx != -1:
            insert_at = min(insert_at, idx)

    head = sql[:insert_at].rstrip()
    tail = sql[insert_at:]

    if re.search(r"\bwhere\b", head, re.IGNORECASE):
        merged = f"{head} AND ({filter_clause}){tail}"
    else:
        merged = f"{head} WHERE ({filter_clause}){tail}"

    logger.info("injected server-side customer filter into sql")
    return merged


def apply_customer_id_placeholder(sql: str, customer_id: str) -> str:
    """Sustituye placeholder por UUID real antes de ejecutar (no va al LLM)."""
    if CUSTOMER_ID_PLACEHOLDER not in sql:
        return sql
    applied = sql.replace(CUSTOMER_ID_PLACEHOLDER, customer_id)
    logger.debug("applied customer_id placeholder in sql")
    return applied


LLM_SAFETY_INSTRUCTION = """
PRIVACIDAD:
- No menciones nombres de empresas, personas, correos, NIT ni archivos.
- Si necesitas referirte al sujeto, usa "tu empresa" o "el cliente".
- Los resultados ya vienen sin columnas identificables.
"""
