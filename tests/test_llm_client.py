from llm_client import infer_provider_from_model, resolve_provider


def test_infer_provider_from_model():
    assert infer_provider_from_model("claude-3-5-sonnet-20241022") == "anthropic"
    assert infer_provider_from_model("gpt-4o") == "openai"
    assert infer_provider_from_model("") == "openai"


def test_resolve_provider_explicit():
    agent = {"provider": "anthropic", "model": "gpt-4o"}
    assert resolve_provider(agent) == "anthropic"


def test_resolve_provider_from_config():
    agent = {"model": "gpt-4o", "config": {"provider": "anthropic"}}
    assert resolve_provider(agent) == "anthropic"


def test_resolve_provider_from_model_prefix():
    agent = {"model": "claude-3-5-haiku-20241022"}
    assert resolve_provider(agent) == "anthropic"
