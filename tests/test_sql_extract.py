import unittest

from sql_extract import extract_sql_from_llm_response


class SqlExtractTests(unittest.TestCase):
    def test_plain_select(self):
        raw = "SELECT SUM(mvto) FROM gold.vw_fact_bdp_enriched WHERE customer_id = 'x'"
        sql = extract_sql_from_llm_response(raw)
        self.assertTrue(sql.upper().startswith("SELECT"))
        self.assertIn("SUM(mvto)", sql)

    def test_with_cte(self):
        raw = """
WITH junio AS (
    SELECT SUM(mvto) AS total FROM gold.vw_fact_bdp_enriched
    WHERE customer_id = 'x' AND anio_mes = '2026-06'
),
abril AS (
    SELECT SUM(mvto) AS total FROM gold.vw_fact_bdp_enriched
    WHERE customer_id = 'x' AND anio_mes = '2026-04'
)
SELECT junio.total, abril.total FROM junio, abril;
"""
        sql = extract_sql_from_llm_response(raw)
        self.assertTrue(sql.upper().startswith("WITH"))
        self.assertIn("junio AS", sql)
        self.assertIn("abril AS", sql)
        self.assertIn("FROM junio, abril", sql)

    def test_strips_markdown_and_trailing_text(self):
        raw = """```sql
SELECT 1 AS n;
```
Espero que esto ayude."""
        sql = extract_sql_from_llm_response(raw)
        self.assertEqual(sql, "SELECT 1 AS n")

    def test_does_not_strip_with_prefix(self):
        """Regression: inner SELECT must not replace full CTE query."""
        raw = """
WITH junio AS (
    SELECT SUM(mvto) FROM gold.vw_fact_bdp_enriched WHERE anio_mes = '2026-06'
)
SELECT * FROM junio
"""
        sql = extract_sql_from_llm_response(raw)
        self.assertNotIn(")\nSELECT", sql.split("WITH")[0])
        self.assertEqual(sql.count("SELECT"), 2)


if __name__ == "__main__":
    unittest.main()
