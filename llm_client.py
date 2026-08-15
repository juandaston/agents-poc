"""Unified text completion for OpenAI and Anthropic models."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("agents-poc.llm_client")

PROVIDERS = frozenset({"openai", "anthropic"})

# Anthropic retired models -> current API IDs (see platform.claude.com/docs)
RETIRED_ANTHROPIC_MODELS: dict[str, str] = {
    "claude-3-5-sonnet-20241022": "claude-sonnet-4-6",
    "claude-3-5-sonnet-20240620": "claude-sonnet-4-6",
    "claude-3-7-sonnet-20250219": "claude-sonnet-4-6",
    "claude-3-5-haiku-20241022": "claude-haiku-4-5",
    "claude-3-opus-20240229": "claude-opus-4-6",
    "claude-3-sonnet-20240229": "claude-sonnet-4-6",
}


def resolve_model(model: str | None) -> str:
    name = (model or "").strip()
    if not name:
        return name
    mapped = RETIRED_ANTHROPIC_MODELS.get(name)
    if mapped and mapped != name:
        logger.warning("anthropic model remapped %s -> %s", name, mapped)
        return mapped
    return name


def infer_provider_from_model(model: str | None) -> str:
    name = (model or "").strip().lower()
    if name.startswith("claude"):
        return "anthropic"
    return "openai"


def resolve_provider(agent: dict[str, Any]) -> str:
    explicit = (agent.get("provider") or "").strip().lower()
    if explicit in PROVIDERS:
        return explicit

    config = agent.get("config") or {}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            config = {}
    if isinstance(config, dict):
        cfg_provider = (config.get("provider") or "").strip().lower()
        if cfg_provider in PROVIDERS:
            return cfg_provider

    return infer_provider_from_model(agent.get("model"))


def _openai_client():
    from openai import OpenAI

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    return OpenAI(api_key=api_key)


def _anthropic_client():
    import anthropic

    api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")
    return anthropic.Anthropic(api_key=api_key)


def _anthropic_sampling_kwargs(temperature: float, top_p: float) -> dict[str, float]:
    """Newer Claude models accept temperature OR top_p, not both."""
    if top_p < 1.0:
        return {"top_p": top_p}
    return {"temperature": temperature}


def complete_text(
    agent: dict[str, Any],
    prompt: str,
    *,
    model: str | None = None,
) -> str:
    provider = resolve_provider(agent)
    chosen_model = resolve_model(model or agent.get("model"))
    if not chosen_model:
        raise ValueError("model is required")

    temperature = float(agent.get("temperature", 0.7))
    top_p = float(agent.get("top_p", 1.0))
    max_tokens = int(agent.get("max_tokens", 4096))

    logger.info(
        "llm complete provider=%s model=%s prompt_chars=%s",
        provider,
        chosen_model,
        len(prompt or ""),
    )

    if provider == "anthropic":
        client = _anthropic_client()
        sampling = _anthropic_sampling_kwargs(temperature, top_p)
        response = client.messages.create(
            model=chosen_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            **sampling,
        )
        parts = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        text = "".join(parts).strip()
        if not text:
            raise RuntimeError("Anthropic returned an empty response")
        return text

    client = _openai_client()
    response = client.responses.create(
        model=chosen_model,
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_tokens,
        input=prompt,
    )
    text = (response.output_text or "").strip()
    if not text:
        raise RuntimeError("OpenAI returned an empty response")
    return text
