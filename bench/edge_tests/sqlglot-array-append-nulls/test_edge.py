"""Edge cases for ARRAY_APPEND's NULL semantics: same-kind pairs untouched, qualified and computed arguments, array
literals, and no second guard once the SQL is DuckDB's own."""
import unittest

from sqlglot import parse_one


def write(sql, read, dialect):
    return parse_one(sql, read=read).sql(dialect=dialect)


class ArrayAppendEdgeTests(unittest.TestCase):
    def test_same_kind_pairs_are_written_as_is(self):
        for read, sql, dialect, expected in (
            ('snowflake', 'SELECT ARRAY_APPEND(arr, x) FROM t', 'spark', 'SELECT ARRAY_APPEND(arr, x) FROM t'),
            ('spark', 'SELECT ARRAY_APPEND(arr, x) FROM t', 'databricks', 'SELECT ARRAY_APPEND(arr, x) FROM t'),
            ('databricks', 'SELECT ARRAY_APPEND(arr, x) FROM t', 'snowflake', 'SELECT ARRAY_APPEND(arr, x) FROM t'),
            ('duckdb', 'SELECT LIST_APPEND(arr, x) FROM t', 'postgres', 'SELECT ARRAY_APPEND(arr, x) FROM t'),
            ('postgres', 'SELECT ARRAY_APPEND(arr, x) FROM t', 'duckdb', 'SELECT LIST_APPEND(arr, x) FROM t'),
        ):
            with self.subTest(f'{read} -> {dialect}'):
                self.assertEqual(write(sql, read, dialect), expected)

    def test_the_guard_uses_the_array_argument_as_written(self):
        self.assertEqual(write('SELECT ARRAY_APPEND(t.arr, x + 1) AS y FROM t', 'spark', 'duckdb'),
                         'SELECT CASE WHEN t.arr IS NULL THEN NULL ELSE LIST_APPEND(t.arr, x + 1) END AS y FROM t')
        self.assertEqual(write('SELECT ARRAY_APPEND(t.arr, 0) FROM t', 'databricks', 'postgres'),
                         'SELECT CASE WHEN t.arr IS NULL THEN NULL ELSE ARRAY_APPEND(t.arr, 0) END FROM t')

    def test_an_empty_array_replaces_null_for_array_literals_and_columns(self):
        self.assertEqual(write('SELECT ARRAY_APPEND(ARRAY[1, 2], 3)', 'postgres', 'snowflake'),
                         'SELECT ARRAY_APPEND(COALESCE([1, 2], []), 3)')
        self.assertEqual(write('SELECT LIST_APPEND(t.arr, y) AS z FROM t', 'duckdb', 'spark'),
                         'SELECT ARRAY_APPEND(COALESCE(t.arr, ARRAY()), y) AS z FROM t')

    def test_duckdb_sql_is_not_guarded_again(self):
        once = write('SELECT ARRAY_APPEND(arr, x) FROM t', 'snowflake', 'duckdb')
        self.assertEqual(once, 'SELECT CASE WHEN arr IS NULL THEN NULL ELSE LIST_APPEND(arr, x) END FROM t')
        self.assertEqual(write(once, 'duckdb', 'duckdb'), once)


if __name__ == '__main__':
    unittest.main()
