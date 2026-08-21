from semantic_loader import try_heuristic_route
from llm_client import resolve_fast_model, DEFAULT_FAST_OPENAI_MODEL
from sql_agent import (
    _account_search_patterns,
    _concept_ilike_pattern,
    _extract_requested_concepts,
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
        "WHERE e.customer_id = 'x' AND e.uso = 'ER'"
    )
    try:
        _validate_sql_for_route(sql, "vw_fact_bdp_enriched")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "fact_nota_credito" in str(exc)


def test_validate_sql_for_route_accepts_enriched_view_name():
    sql = (
        "SELECT SUM(mvto) FROM gold.vw_fact_bdp_enriched "
        "WHERE customer_id = 'x' AND uso = 'ER' AND anio_mes = '2026-01'"
    )
    _validate_sql_for_route(sql, "vw_fact_bdp_enriched")


def test_validate_sql_for_route_requires_er_usage():
    sql = (
        "SELECT SUM(mvto) FROM gold.vw_fact_bdp_enriched "
        "WHERE customer_id = 'x' AND anio_mes = '2026-01'"
    )
    try:
        _validate_sql_for_route(sql, "vw_fact_bdp_enriched")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "uso = 'ER'" in str(exc)


def test_validate_sql_for_route_requires_er_in_where():
    sql = (
        "SELECT uso = 'ER' AS es_resultado, SUM(mvto) "
        "FROM gold.vw_fact_bdp_enriched WHERE customer_id = 'x' GROUP BY uso"
    )
    try:
        _validate_sql_for_route(sql, "vw_fact_bdp_enriched")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "uso = 'ER'" in str(exc)


def test_validate_sql_for_route_separates_multiple_concepts():
    question = (
        "Mano de obra directa, gastos de ventas, gasto de personal "
        "tráeme el valor de estos 3 de mayo 2026"
    )
    combined_sql = (
        "SELECT SUM(mvto) FROM gold.vw_fact_bdp_enriched "
        "WHERE customer_id = 'x' AND uso = 'ER' "
        "AND (nombre_cuenta = 'Mano de obra directa' "
        "OR nombre_rubro_grupo = 'Gastos de Ventas')"
    )
    try:
        _validate_sql_for_route(
            combined_sql, "vw_fact_bdp_enriched", question
        )
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "3 conceptos" in str(exc)


def test_extracts_and_normalizes_multiple_concepts():
    question = (
        "Mano de obra directa, gastos de ventas, gasto de personal "
        "tráeme el valor de estos 3 de mayo 2026"
    )
    assert _extract_requested_concepts(question) == [
        "Mano de obra directa",
        "gastos de ventas",
        "gasto de personal",
    ]
    assert _concept_ilike_pattern("Gastos de ventas") == "%gasto%venta%"
    assert _concept_ilike_pattern("Gasto de personal") == "%gasto%personal%"


def test_validate_sql_for_route_accepts_union_for_multiple_concepts():
    question = (
        "Mano de obra directa, gastos de ventas, gasto de personal "
        "tráeme el valor de estos 3 de mayo 2026"
    )
    sql = """
        SELECT 'Mano de obra directa' AS concepto, SUM(mvto) AS total
        FROM gold.vw_fact_bdp_enriched
        WHERE customer_id = 'x' AND uso = 'ER'
        UNION ALL
        SELECT 'Gastos de Ventas' AS concepto, SUM(mvto) AS total
        FROM gold.vw_fact_bdp_enriched
        WHERE customer_id = 'x' AND uso = 'ER'
        UNION ALL
        SELECT 'Gasto de Personal' AS concepto, SUM(mvto) AS total
        FROM gold.vw_fact_bdp_enriched
        WHERE customer_id = 'x' AND uso = 'ER'
    """
    _validate_sql_for_route(sql, "vw_fact_bdp_enriched", question)


def test_validate_sql_for_route_rejects_unverified_dimension_value():
    question = (
        "Mano de obra directa, gastos de ventas, gasto de personal "
        "tráeme el valor de estos 3 de mayo 2026"
    )
    hints = """
    - nombre_cuenta = 'Mano de obra directa'
    - nombre_rubro_grupo = 'Gasto Venta'
    - nombre_cuenta = 'Gastos de Personal'
    """
    sql = """
        SELECT 'Mano de obra directa' AS concepto, SUM(mvto) AS total
        FROM gold.vw_fact_bdp_enriched
        WHERE customer_id = 'x' AND uso = 'ER'
          AND nombre_cuenta = 'Mano de obra directa'
        UNION ALL
        SELECT 'Gastos de Ventas' AS concepto, SUM(mvto) AS total
        FROM gold.vw_fact_bdp_enriched
        WHERE customer_id = 'x' AND uso = 'ER'
          AND nombre_rubro_grupo = 'Gastos de Ventas'
        UNION ALL
        SELECT 'Gasto de Personal' AS concepto, SUM(mvto) AS total
        FROM gold.vw_fact_bdp_enriched
        WHERE customer_id = 'x' AND uso = 'ER'
          AND nombre_rubro_grupo = 'Gastos de Personal'
    """
    try:
        _validate_sql_for_route(
            sql, "vw_fact_bdp_enriched", question, hints
        )
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "no verificado por DISTINCT" in str(exc)


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
