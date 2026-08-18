"""Assemble LLM prompts: fixed base template + optional persona from DB."""

from __future__ import annotations

BASE_CUSTOMER_ANSWER = """
Eres un asistente financiero para clientes NO técnicos.

Tu tarea:
- resumir resultados en lenguaje simple
- máximo 2-3 líneas
- sin tecnicismos
- directo y claro
""".strip()

BASE_ADMIN_EXPLAIN = """
Eres un analista financiero senior.
""".strip()


def _persona_block(agent: dict, body_key: str) -> str:
    text = (agent.get(body_key) or "").strip()
    if not text:
        return ""
    return f"\n\nPERFIL Y AUDIENCIA:\n{text}"


def _extra_block(agent: dict) -> str:
    """Legacy inline system_prompt on the agent (optional override)."""
    text = (agent.get("system_prompt") or "").strip()
    if not text:
        return ""
    return f"\n\nINSTRUCCIONES ADICIONALES:\n{text}"


def build_customer_answer_prompt(
    question: str,
    results,
    agent: dict,
    safety_instruction: str,
) -> str:
    parts = [
        BASE_CUSTOMER_ANSWER,
        _persona_block(agent, "customer_answer_prompt_body"),
        _extra_block(agent),
        safety_instruction,
        f"""

PREGUNTA:
{question}

RESULTADOS (sin datos identificables):
{results}

RESPONDE SOLO el mensaje final al cliente.
""".strip(),
    ]
    return "\n".join(p for p in parts if p)


def build_admin_explain_prompt(
    question: str,
    sql_list,
    results,
    agent: dict,
    safety_instruction: str,
) -> str:
    parts = [
        BASE_ADMIN_EXPLAIN,
        _persona_block(agent, "admin_explain_prompt_body"),
        _extra_block(agent),
        safety_instruction,
        f"""

Pregunta:
{question}

SQL ejecutados (identificadores redactados):
{sql_list}

Resultados (sin columnas identificables):
{results}

Explica de forma clara:
- qué pasó
- insights
- anomalías si existen
""".strip(),
    ]
    return "\n".join(p for p in parts if p)
