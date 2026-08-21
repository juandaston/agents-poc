from semantic_loader import try_heuristic_route
from llm_client import resolve_fast_model, DEFAULT_FAST_OPENAI_MODEL


def test_try_heuristic_route_kpis():
    route = try_heuristic_route("¿Cuál es el ROE del último año?")
    assert route is not None
    assert route["tables"] == ["vw_kpis_financiero"]
    assert route["intent"] == "kpis"


def test_try_heuristic_route_tablero_ingresos():
    route = try_heuristic_route("dame ingresos operacionales")
    assert route is not None
    assert route["tables"] == ["vw_fact_bdp_enriched"]
    assert "tablero" in route["reason"] or "montos" in route["reason"]


def test_try_heuristic_route_dim_catalog():
    route = try_heuristic_route("muéstrame el plan de cuentas")
    assert route is not None
    assert route["tables"] == ["vw_dim_accounts"]


def test_try_heuristic_route_ventas_netas():
    route = try_heuristic_route("Resume las ventas netas por mes")
    assert route is not None
    assert route["tables"] == ["vw_ventas_netas_mes"]


def test_try_heuristic_route_balance():
    route = try_heuristic_route("¿Cuál es el saldo final total del balance de prueba?")
    assert route is not None
    assert route["tables"] == ["vw_fact_bdp_enriched"]
    assert route["intent"] == "balance"


def test_try_heuristic_route_unknown():
    assert try_heuristic_route("xyz abc random") is None


def test_resolve_fast_model_default():
    assert resolve_fast_model({"model": "claude-sonnet-4-6"}) == DEFAULT_FAST_OPENAI_MODEL


def test_resolve_fast_model_from_config():
    agent = {"model": "gpt-4o", "config": {"fast_model": "gpt-4o-mini"}}
    assert resolve_fast_model(agent) == "gpt-4o-mini"
