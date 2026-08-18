"""Assemble LLM prompts: fixed base template + optional persona from DB."""

from __future__ import annotations

import logging

logger = logging.getLogger("agents-poc.prompt_builder")

BASE_CUSTOMER_ANSWER = """
Eres un asistente financiero para clientes NO técnicos.

Tu tarea:
- resumir resultados en lenguaje simple
- máximo 2-3 líneas
- sin tecnicismos
- directo y claro
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


def _log_built_prompt(kind: str, agent: dict, prompt: str) -> None:
    persona = (agent.get("customer_answer_prompt_body") or "").strip()
    extra = (agent.get("system_prompt") or "").strip()
    logger.info(
        "prompt assembled kind=%s agent=%r model=%s persona_chars=%s extra_chars=%s total_chars=%s",
        kind,
        agent.get("name"),
        agent.get("model"),
        len(persona),
        len(extra),
        len(prompt),
    )
    if persona:
        preview = persona if len(persona) <= 240 else persona[:239] + "…"
        logger.info("prompt persona preview kind=%s: %s", kind, preview)
    logger.debug(
        "=== LLM PROMPT (%s) agent_id=%s ===\n%s\n=== END PROMPT (%s) ===",
        kind,
        agent.get("id"),
        prompt,
        kind,
    )


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
    prompt = "\n".join(p for p in parts if p)
    _log_built_prompt("customer_answer", agent, prompt)
    return prompt
