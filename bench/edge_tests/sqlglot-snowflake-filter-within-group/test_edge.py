"""Edge cases for FILTER with WITHIN GROUP in Snowflake: a descending percentile, MODE from PostgreSQL with a filter
and in a grouped query, LISTAGG with another separator, other functions refused, and plain FILTER as before."""
import unittest

from sqlglot import ErrorLevel, UnsupportedError, parse_one


def snowflake(sql, read):
    return parse_one(sql, read=read).sql(dialect='snowflake', unsupported_level=ErrorLevel.RAISE)


class FilterWithinGroupEdgeTests(unittest.TestCase):
    def test_a_descending_percentile_wraps_its_key(self):
        self.assertEqual(
            snowflake('SELECT PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY v DESC) FILTER (WHERE k = 1) FROM t', 'duckdb'),
            'SELECT PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY IFF(k = 1, v, NULL) DESC NULLS LAST) FROM t')

    def test_mode_from_postgres(self):
        self.assertEqual(snowflake('SELECT MODE() WITHIN GROUP (ORDER BY x) FILTER (WHERE y > 0) FROM t', 'postgres'),
                         'SELECT MODE(IFF(y > 0, x, NULL)) FROM t')
        self.assertEqual(snowflake('SELECT g, MODE() WITHIN GROUP (ORDER BY x) AS m FROM t GROUP BY g', 'postgres'),
                         'SELECT g, MODE(x) AS m FROM t GROUP BY g')

    def test_listagg_wraps_only_its_values(self):
        self.assertEqual(
            snowflake("SELECT g, LISTAGG(x, '|') WITHIN GROUP (ORDER BY y) FILTER (WHERE z > 0) AS s FROM t GROUP BY g",
                      'redshift'),
            "SELECT g, LISTAGG(IFF(z > 0, x, NULL), '|') WITHIN GROUP (ORDER BY y) AS s FROM t GROUP BY g")

    def test_other_functions_are_refused(self):
        for sql in ('SELECT BAR(x) WITHIN GROUP (ORDER BY y) FILTER(WHERE z > 0) FROM t',
                    'SELECT BAZ(x, 2) WITHIN GROUP (ORDER BY y DESC) FILTER(WHERE z) FROM t'):
            with self.subTest(sql), self.assertRaises(UnsupportedError):
                snowflake(sql, '')

    def test_filter_without_within_group_and_other_dialects_as_before(self):
        self.assertEqual(snowflake('SELECT SUM(x) FILTER (WHERE y > 0) FROM t', 'duckdb'),
                         'SELECT SUM(IFF(y > 0, x, NULL)) FROM t')
        self.assertEqual(snowflake('SELECT COUNT(DISTINCT x) FILTER (WHERE y > 0) FROM t', 'duckdb'),
                         'SELECT COUNT(DISTINCT IFF(y > 0, x, NULL)) FROM t')
        sql = 'SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY b) FILTER (WHERE a IS NOT NULL) FROM t'
        self.assertEqual(parse_one(sql, read='duckdb').sql(dialect='postgres'),
                         'SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY b) FILTER(WHERE NOT a IS NULL) FROM t')


if __name__ == '__main__':
    unittest.main()
