"""Normalize and format chat history for the financial SQL agent."""

from __future__ import annotations

import re
from typing import Any

MAX_HISTORY_MESSAGES = 6
MAX_MESSAGE_CHARS = 600

_FOLLOWUP_START_RE = re.compile(
    r"^(y\b|y,|y en|y para|y el|y la|también|tambien|eso|lo mismo|igual|"
    r"ahora|otra vez|qué tal|que tal|cuánto fue|cuanto fue|en el|para el|"
    r"del mes|y cuánto|y cuanto|mismo periodo|mismo mes)\b",
    re.IGNORECASE,
)


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
    if not q or not messages or not is_likely_followup(q):
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
"""


def history_for_prompts(
    question: str, messages: list[dict[str, str]]
) -> tuple[str, str]:
    """
    Returns (history_block, effective_question) for routing/SQL/answer.
    """
    block = format_history_block(messages, exclude_last_user=True)
    effective = resolve_effective_question(question, messages)
    return block, effective
