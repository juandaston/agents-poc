"""Extract executable SQL from LLM text responses."""

from __future__ import annotations

import re


def extract_sql_from_llm_response(raw: str) -> str:
    """
    Pull executable SQL from LLM output.
    Keeps WITH ... SELECT (CTEs); do not strip from inner SELECT only.
    """
    sql = raw.replace("```sql", "").replace("```", "").strip()
    start = re.search(r"\b(WITH|SELECT)\b", sql, re.I)
    if not start:
        raise ValueError("SQL inválido: no WITH ni SELECT")
    sql = sql[start.start() :]
    semi = sql.find(";")
    if semi != -1:
        sql = sql[: semi + 1]
    return sql.strip().rstrip(";")

