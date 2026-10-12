"""Edge cases for Snowflake's CORR in DuckDB: inside larger expressions, several calls, framed windows, and no second
wrapper once the SQL is DuckDB's own."""
import unittest

from sqlglot import parse_one

WRAP = 'CASE WHEN ISNAN({0}) THEN NULL ELSE {0} END'


def to_duckdb(sql, read='snowflake'):
    return parse_one(sql, read=read).sql(dialect='duckdb')


class CorrEdgeTests(unittest.TestCase):
    def test_an_aliased_call_in_a_grouped_query(self):
        self.assertEqual(to_duckdb('SELECT g, CORR(x, y) AS r FROM t GROUP BY g'),
                         f"SELECT g, {WRAP.format('CORR(x, y)')} AS r FROM t GROUP BY g")

    def test_each_call_is_wrapped_once(self):
        out = to_duckdb('SELECT CORR(a, b) AS r1, CORR(c, d) AS r2 FROM t')
        self.assertEqual(out, f"SELECT {WRAP.format('CORR(a, b)')} AS r1, {WRAP.format('CORR(c, d)')} AS r2 FROM t")
        self.assertEqual(out.count('ISNAN('), 2)

    def test_a_framed_ordered_window_is_wrapped_whole(self):
        window = 'CORR(a, b) OVER (PARTITION BY c ORDER BY d ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)'
        self.assertEqual(to_duckdb(f'SELECT {window} FROM t'), f'SELECT {WRAP.format(window)} FROM t')

    def test_duckdb_sql_is_not_wrapped_again(self):
        once = to_duckdb('SELECT CORR(a, b) OVER (PARTITION BY c) FROM t')
        self.assertEqual(to_duckdb(once, read='duckdb'), once)
        for sql in ('SELECT CORR(a, b) FROM t', 'SELECT CORR(a, b) FILTER(WHERE c > 0) OVER (PARTITION BY d) FROM t'):
            with self.subTest(sql):
                self.assertEqual(to_duckdb(sql, read='duckdb'), sql)
                self.assertEqual(to_duckdb(sql, read='postgres'), sql)

    def test_snowflake_to_snowflake_is_unchanged(self):
        sql = 'SELECT g, CORR(x, y) AS r FROM t GROUP BY g'
        self.assertEqual(parse_one(sql, read='snowflake').sql(dialect='snowflake'), sql)


if __name__ == '__main__':
    unittest.main()
