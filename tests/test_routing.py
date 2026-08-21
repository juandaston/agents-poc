from semantic_loader import try_heuristic_route
from llm_client import resolve_fast_model, DEFAULT_FAST_OPENAI_MODEL
from sql_agent import (
    _account_search_patterns,
    _maybe_route_to_ventas_enriched,
    _validate_sql_for_route,
)


def test_try_heuristic_route_devoluciones():
    route = try_heuristic_route("¿Cuáles fueron las devoluciones en enero 2026?")
    assert route is not None
    assert route["tables"] == ["vw_fact_bdp_enriched"]
    assert "devoluciones" in route["reason"].lower()


def test_account_search_skips_cuales():
    patterns = _account_search_patterns("¿Cuáles fueron las devoluciones en enero 2026?")
    assert "%cuales%" not in patterns
    assert "%devoluc%" in patterns


def test_validate_sql_for_route_rejects_silver():
    sql = (
        "SELECT SUM(e.mvto) FROM gold.vw_fact_bdp_enriched e "
        "JOIN silver.fact_nota_credito f ON f.customer_id = e.customer_id "
        "WHERE e.customer_id = 'x'"
    )
    try:
        _validate_sql_for_route(sql, "vw_fact_bdp_enriched")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "fact_nota_credito" in str(exc)


def test_validate_sql_for_route_accepts_enriched_view_name():
    sql = (
        "SELECT SUM(mvto) FROM gold.vw_fact_bdp_enriched "
        "WHERE customer_id = 'x' AND anio_mes = '2026-01'"
    )
    _validate_sql_for_route(sql, "vw_fact_bdp_enriched")


def test_maybe_route_ventas_skips_pure_devoluciones():
    route = {"tables": ["vw_fact_bdp_enriched"], "reason": "primary"}
    out = _maybe_route_to_ventas_enriched(
        "¿Cuáles fueron las devoluciones en enero 2026?",
        route,
    )
    assert out is route
    assert "Ingresos Operacionales" not in str(out)


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
    assert route["tables"] == ["vw_fact_bdp_enriched"]


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
