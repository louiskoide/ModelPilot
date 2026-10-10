"""Edge cases for DuckDB MACRO overloads: three overloads, scalar overloads aren't tables, single macros unchanged."""
import unittest

from sqlglot import exp, parse_one


def roundtrip(sql):
    return parse_one(sql, read='duckdb').sql(dialect='duckdb')


class MacroOverloadEdgeTests(unittest.TestCase):
    def test_three_overloads(self):
        sql = "CREATE MACRO m (a) AS a * 2, (a, b) AS a * b, (a, b, c) AS a * b * c"
        node = parse_one(sql, read='duckdb')
        self.assertIsInstance(node.expression, exp.MacroOverloads)
        self.assertEqual(len(node.expression.expressions), 3)
        self.assertEqual(node.sql(dialect='duckdb'), sql)

    def test_scalar_overloads_are_not_tables(self):
        node = parse_one("CREATE MACRO m (a) AS a, (a, b) AS a + b", read='duckdb')
        self.assertFalse(any(o.args.get('is_table') for o in node.expression.expressions))

    def test_a_single_macro_is_not_an_overload_list(self):
        for sql in ("CREATE MACRO one(a) AS a + 1", "CREATE MACRO one_tbl(a) AS TABLE (SELECT a AS x)"):
            with self.subTest(sql):
                node = parse_one(sql, read='duckdb')
                self.assertNotIsInstance(node.expression, exp.MacroOverloads)
                self.assertEqual(node.sql(dialect='duckdb'), sql)

    def test_a_temporary_overloaded_macro(self):
        sql = "CREATE TEMPORARY MACRO m (a) AS a, (a, b) AS a + b"
        self.assertEqual(roundtrip(sql), sql)


if __name__ == '__main__':
    unittest.main()
