"""Edge cases for PIVOT literal aliases: more values, Spark and DuckDB alike, Snowflake itself untouched."""
import unittest

from sqlglot import transpile


def to(sql, dialect):
    return transpile(sql, read='snowflake', write=dialect)[0]


class PivotLiteralAliasEdgeTests(unittest.TestCase):
    def test_every_literal_gets_its_alias(self):
        sql = "SELECT * FROM t PIVOT(SUM(v) FOR k IN ('a', 'b', 'c'))"
        self.assertEqual(to(sql, 'duckdb'),
                         "SELECT * FROM t PIVOT(SUM(v) FOR k IN ('a' AS \"'a'\", 'b' AS \"'b'\", 'c' AS \"'c'\"))")
        self.assertEqual(to(sql, 'spark'),
                         "SELECT * FROM t PIVOT(SUM(v) FOR k IN ('a' AS `'a'`, 'b' AS `'b'`, 'c' AS `'c'`))")

    def test_the_alias_is_the_literal_text(self):
        out = to("SELECT * FROM t PIVOT(MAX(v) FOR k IN ('North', 'South Side'))", 'duckdb')
        self.assertIn("'North' AS \"'North'\"", out)
        self.assertIn("'South Side' AS \"'South Side'\"", out)

    def test_snowflake_output_is_unchanged(self):
        sql = "SELECT * FROM t PIVOT(SUM(v) FOR k IN ('a', 'b'))"
        self.assertEqual(transpile(sql, read='snowflake', write='snowflake')[0], sql)


if __name__ == '__main__':
    unittest.main()
