from llm_client import (
    _anthropic_sampling_kwargs,
    infer_provider_from_model,
    resolve_fast_model,
    resolve_model,
    resolve_provider,
)


def test_infer_provider_from_model():
    assert infer_provider_from_model("claude-sonnet-4-6") == "anthropic"
    assert infer_provider_from_model("gpt-4o") == "openai"
    assert infer_provider_from_model("") == "openai"


def test_resolve_model_retired_sonnet():
    assert resolve_model("claude-3-5-sonnet-20241022") == "claude-sonnet-4-6"


def test_resolve_model_current():
    assert resolve_model("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_resolve_provider_explicit():
    agent = {"provider": "anthropic", "model": "gpt-4o"}
    assert resolve_provider(agent) == "anthropic"


def test_resolve_provider_from_config():
    agent = {"model": "gpt-4o", "config": {"provider": "anthropic"}}
    assert resolve_provider(agent) == "anthropic"


def test_resolve_provider_from_model_prefix():
    agent = {"model": "claude-haiku-4-5"}
    assert resolve_provider(agent) == "anthropic"


def test_anthropic_sampling_prefers_temperature_by_default():
    assert _anthropic_sampling_kwargs(0.7, 1.0) == {"temperature": 0.7}


def test_anthropic_sampling_uses_top_p_when_tuned():
    assert _anthropic_sampling_kwargs(0.7, 0.9) == {"top_p": 0.9}


def test_resolve_fast_model_uses_env(monkeypatch):
    monkeypatch.setenv("FAST_LLM_MODEL", "gpt-4o-mini")
    assert resolve_fast_model({"model": "claude-sonnet-4-6"}) == "gpt-4o-mini"
