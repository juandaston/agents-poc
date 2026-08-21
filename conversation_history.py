"""Normalize and format chat history for the financial SQL agent."""

from __future__ import annotations

import re
from typing import Any

MAX_HISTORY_MESSAGES = 6
MAX_MESSAGE_CHARS = 600

_FOLLOWUP_START_RE = re.compile(
    r"^(y\b|y,|y en|y para|y el|y la|también|tambien|eso|lo mismo|igual|"
    r"ahora|otra vez|qué tal|que tal|cuánto fue|cuanto fue|cuando fue|"
    r"en el|para el|del mes|y cuánto|y cuanto|mismo periodo|mismo mes)\b",
    re.IGNORECASE,
)

_SPANISH_MONTHS = {
    "enero": "01",
    "febrero": "02",
    "marzo": "03",
    "abril": "04",
    "mayo": "05",
    "junio": "06",
    "julio": "07",
    "agosto": "08",
    "septiembre": "09",
    "setiembre": "09",
    "octubre": "10",
    "noviembre": "11",
    "diciembre": "12",
}

_MONTH_NAMES_PATTERN = "|".join(_SPANISH_MONTHS.keys())

_TREND_KEYWORDS = (
    "tendencia",
    "histórico",
    "historico",
    "evolución",
    "evolucion",
    "todos los meses",
    "serie completa",
    "últimos 12",
    "ultimos 12",
)


def wants_full_time_series(question: str) -> bool:
    q = (question or "").lower()
    return any(k in q for k in _TREND_KEYWORDS)


def extract_anio_mes_periods(*texts: str) -> list[str]:
    found: set[str] = set()
    month_re = re.compile(
        rf"\b({_MONTH_NAMES_PATTERN})\s+(?:de\s+)?(\d{{4}})\b",
        re.IGNORECASE,
    )
    iso_re = re.compile(r"\b(\d{4})-(\d{2})\b")
    for text in texts:
        if not text:
            continue
        for match in month_re.finditer(text):
            month_num = _SPANISH_MONTHS[match.group(1).lower()]
            found.add(f"{match.group(2)}-{month_num}")
        for match in iso_re.finditer(text):
            mm = int(match.group(2))
            if 1 <= mm <= 12:
                found.add(f"{match.group(1)}-{match.group(2)}")
    return sorted(found)


def build_temporal_hints_block(question: str, messages: list[dict[str, str]]) -> str:
    if wants_full_time_series(question):
        return ""
    texts = [question, *[msg.get("content", "") for msg in messages]]
    periods = extract_anio_mes_periods(*texts)
    if not periods:
        return ""
    lines = "\n".join(f"  - '{p}'" for p in periods)
    if len(periods) == 1:
        filter_sql = f"anio_mes = '{periods[0]}'"
    else:
        in_list = ", ".join(f"'{p}'" for p in periods)
        filter_sql = f"anio_mes IN ({in_list})"
    return f"""
PERIODOS DETECTADOS (pregunta o historial — filtra en WHERE):
{lines}
- OBLIGATORIO: AND {filter_sql}
- NO devuelvas toda la serie histórica si solo necesitas estos periodos.
- Comparación año vs año mismo mes: usa anio_mes IN (...), luego GROUP BY anio_mes.
- Un solo mes: WHERE anio_mes = 'YYYY-MM' y SUM(mvto) sin GROUP BY anio_mes innecesario.
"""


def _needs_history_topic(question: str, messages: list[dict[str, str]]) -> bool:
    if not messages:
        return False
    q = (question or "").strip()
    if extract_anio_mes_periods(q):
        return False
    if len(q.split()) > 14:
        return False
    return True


def normalize_messages(raw: list[Any] | None) -> list[dict[str, str]]:
    """Accept [{role, content}, ...]; drop empty and cap size."""
    if not raw or not isinstance(raw, list):
        return []

    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if len(content) > MAX_MESSAGE_CHARS:
            content = content[: MAX_MESSAGE_CHARS - 1] + "…"
        out.append({"role": role, "content": content})

    if len(out) > MAX_HISTORY_MESSAGES:
        out = out[-MAX_HISTORY_MESSAGES:]
    return out


def sanitize_messages(
    messages: list[dict[str, str]], sanitize_fn
) -> list[dict[str, str]]:
    return [
        {
            "role": msg["role"],
            "content": sanitize_fn(msg["content"]),
        }
        for msg in messages
    ]


def is_likely_followup(question: str) -> bool:
    q = (question or "").strip()
    if not q:
        return False
    if _FOLLOWUP_START_RE.search(q):
        return True
    words = q.split()
    return len(words) <= 6 and "?" in q


def last_user_message(messages: list[dict[str, str]]) -> str | None:
    for msg in reversed(messages):
        if msg.get("role") == "user" and msg.get("content"):
            return msg["content"].strip()
    return None


def resolve_effective_question(
    question: str, messages: list[dict[str, str]]
) -> str:
    """
    For short follow-ups ('y en mayo?'), combine with the prior user turn
    so routing and rubro/cuenta lookups keep the same topic.
    """
    q = (question or "").strip()
    if not q or not messages:
        return q
    if not is_likely_followup(q) and not _needs_history_topic(q, messages):
        return q
    prior = last_user_message(messages)
    if not prior:
        return q
    return f"{prior} — seguimiento: {q}"


def format_history_block(
    messages: list[dict[str, str]],
    *,
    exclude_last_user: bool = True,
) -> str:
    """Markdown-ish block for LLM prompts."""
    if not messages:
        return ""

    items = messages
    if exclude_last_user and items and items[-1].get("role") == "user":
        items = items[:-1]
    if not items:
        return ""

    lines: list[str] = []
    for msg in items:
        label = "Usuario" if msg["role"] == "user" else "Asistente"
        lines.append(f"{label}: {msg['content']}")

    body = "\n".join(lines)
    return f"""
HISTORIAL RECIENTE (solo contexto; interpreta la PREGUNTA ACTUAL en este hilo):
{body}
- Si la pregunta actual es un seguimiento breve ('y en mayo?', 'lo mismo para 2025'),
  mantén el mismo concepto/rubro/cuenta del historial y cambia solo periodo o detalle pedido.
- Si el historial menciona mes/año (ej. junio 2026), hereda ese periodo en el filtro SQL.
"""


def history_for_prompts(
    question: str, messages: list[dict[str, str]]
) -> tuple[str, str, str]:
    """
    Returns (history_block, effective_question, temporal_hints) for routing/SQL/answer.
    """
    block = format_history_block(messages, exclude_last_user=True)
    effective = resolve_effective_question(question, messages)
    temporal = build_temporal_hints_block(effective, messages)
    return block, effective, temporal
