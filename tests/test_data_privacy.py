import unittest

from data_privacy import apply_customer_id_placeholder, inject_customer_filter


class InjectCustomerFilterTests(unittest.TestCase):
    def test_inserts_before_limit_on_newline(self):
        sql = """
SELECT pct_margen_bruto, anio_mes
FROM gold.vw_kpis_financiero
WHERE anio = 2025
ORDER BY anio DESC, anio_mes DESC
LIMIT 50
""".strip()
        result = inject_customer_filter(sql, "nombre_cliente = 'MESH'")
        self.assertIn("AND (nombre_cliente = 'MESH')", result)
        self.assertIn("ORDER BY anio DESC", result)
        self.assertRegex(result, r"AND \(nombre_cliente = 'MESH'\)\s+ORDER BY")
        self.assertRegex(result, r"LIMIT 50\s*$")

    def test_appends_and_when_where_exists_before_limit(self):
        sql = (
            "SELECT * FROM gold.vw_kpis_financiero WHERE anio = 2025\n"
            "LIMIT 50"
        )
        result = inject_customer_filter(sql, "nombre_cliente = 'MESH'")
        self.assertEqual(
            result,
            "SELECT * FROM gold.vw_kpis_financiero WHERE anio = 2025 "
            "AND (nombre_cliente = 'MESH') LIMIT 50",
        )

    def test_adds_where_when_missing(self):
        sql = "SELECT * FROM gold.vw_kpis_financiero ORDER BY anio DESC LIMIT 12"
        result = inject_customer_filter(sql, "nombre_cliente = 'MESH'")
        self.assertIn("WHERE (nombre_cliente = 'MESH')", result)
        self.assertRegex(result, r"WHERE \(nombre_cliente = 'MESH'\)\s+ORDER BY")

    def test_no_trailing_and_after_limit(self):
        sql = "SELECT * FROM gold.vw_kpis_financiero LIMIT 50"
        result = inject_customer_filter(sql, "nombre_cliente = 'MESH'")
        self.assertNotIn("LIMIT 50 AND", result)
        self.assertRegex(result, r"WHERE \(nombre_cliente = 'MESH'\)\s+LIMIT 50")


class ApplyCustomerIdPlaceholderTests(unittest.TestCase):
    def test_substitutes_uuid_in_kpi_query(self):
        sql = (
            "SELECT pct_margen_bruto, anio_mes FROM gold.vw_kpis_financiero "
            "WHERE customer_id = '__CUSTOMER_ID__' AND anio = 2025 LIMIT 12"
        )
        customer_id = "660e8400-e29b-41d4-a716-446655440000"
        result = apply_customer_id_placeholder(sql, customer_id)
        self.assertIn(f"customer_id = '{customer_id}'", result)
        self.assertNotIn("__CUSTOMER_ID__", result)


if __name__ == "__main__":
    unittest.main()
