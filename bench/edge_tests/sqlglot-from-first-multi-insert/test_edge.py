"""Edge cases for FROM-first queries and multi-table INSERTs: other set operations, a filtered LATERAL VIEW query,
three branches, QUALIFY in a later branch, and Oracle's other conditional and unconditional forms."""
import unittest

from sqlglot import ErrorLevel, UnsupportedError, parse_one


def write(sql, read, dialect=None):
    return parse_one(sql, read=read).sql(dialect=dialect or read, unsupported_level=ErrorLevel.RAISE)


class FromFirstEdgeTests(unittest.TestCase):
    def test_other_set_operations_keep_their_from(self):
        self.assertEqual(write('FROM (FROM t1 SELECT a UNION SELECT b FROM t2)', 'duckdb'),
                         'SELECT * FROM (SELECT a FROM t1 UNION SELECT b FROM t2)')
        self.assertEqual(write('SELECT * FROM (FROM t1 SELECT a EXCEPT SELECT b FROM t2) AS s', 'duckdb'),
                         'SELECT * FROM (SELECT a FROM t1 EXCEPT SELECT b FROM t2) AS s')

    def test_a_filtered_lateral_view_query_in_parentheses(self):
        self.assertEqual(write('SELECT * FROM (FROM x LATERAL VIEW EXPLODE(y) t AS a SELECT a WHERE a > 0) AS q', 'hive'),
                         'SELECT * FROM (SELECT a FROM x LATERAL VIEW EXPLODE(y) t AS a WHERE a > 0) AS q')

    def test_three_branches_from_a_from_first_source(self):
        sql = ('FROM (FROM x LATERAL VIEW EXPLODE(arr) a AS c SELECT k, c) AS s INSERT OVERWRITE TABLE t SELECT s.c '
               'INSERT INTO u SELECT s.k INSERT INTO w SELECT s.k, s.c')
        self.assertEqual(write(sql, 'hive'),
                         'FROM (SELECT k, c FROM x LATERAL VIEW EXPLODE(arr) a AS c) AS s INSERT OVERWRITE TABLE t '
                         'SELECT s.c INSERT INTO u SELECT s.k INSERT INTO w SELECT s.k, s.c')

    def test_qualify_in_a_later_branch(self):
        sql = ('FROM x INSERT INTO t SELECT k INSERT INTO u SELECT v '
               'QUALIFY ROW_NUMBER() OVER (PARTITION BY k ORDER BY v) = 1')
        for dialect in ('hive', 'spark'):
            with self.subTest(dialect), self.assertRaises(UnsupportedError):
                write(sql, 'hive', dialect)
        self.assertEqual(write(sql, 'hive', 'databricks'), sql)

    def test_oracle_conditional_and_unconditional_forms(self):
        self.assertEqual(
            write('INSERT WHEN k > 1 THEN INTO t VALUES (k) WHEN k > 5 THEN INTO u VALUES (k) SELECT k FROM x', 'oracle'),
            'INSERT ALL WHEN k > 1 THEN INTO t VALUES (k) WHEN k > 5 THEN INTO u VALUES (k) SELECT k FROM x')
        for sql in ('INSERT FIRST WHEN k > 1 THEN INTO t VALUES (k) ELSE INTO u VALUES (k) SELECT k FROM x',
                    'INSERT ALL INTO t VALUES (k) INTO u VALUES (k) SELECT k FROM x'):
            with self.subTest(sql):
                self.assertEqual(write(sql, 'oracle'), sql)


if __name__ == '__main__':
    unittest.main()
