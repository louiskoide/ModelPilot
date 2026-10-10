"""Edge cases for COLLATE in nested and CAST types: deeper nesting, structs, other collations and dialect pairs."""
import unittest

from sqlglot import parse_one, transpile


def roundtrip(sql, dialect):
    return parse_one(sql, read=dialect).sql(dialect=dialect)


class CollateTypeEdgeTests(unittest.TestCase):
    def test_deeper_nesting(self):
        for sql in ("CREATE TABLE foo (x ARRAY<ARRAY<STRING COLLATE UTF8_BINARY>>)",
                    "CREATE TABLE foo (m MAP<STRING COLLATE UTF8_LCASE, ARRAY<STRING COLLATE UTF8_BINARY>>)"):
            with self.subTest(sql):
                self.assertEqual(roundtrip(sql, 'databricks'), sql)

    def test_struct_fields(self):
        sql = "CREATE TABLE foo (s STRUCT<a: STRING COLLATE UTF8_LCASE, b: INT>)"
        self.assertEqual(roundtrip(sql, 'databricks'), sql)

    def test_other_collations_in_casts(self):
        self.assertEqual(roundtrip("SELECT CAST(x AS STRING COLLATE UNICODE_CI)", 'databricks'),
                         "SELECT CAST(x AS STRING COLLATE UNICODE_CI)")
        self.assertEqual(roundtrip('SELECT CAST(x AS VARCHAR(20) COLLATE "C")', 'postgres'),
                         'SELECT CAST(x AS VARCHAR(20) COLLATE "C")')

    def test_spark_keeps_the_collation_of_a_rewritten_char(self):
        self.assertEqual(roundtrip("SELECT CAST(x AS CHAR(3) COLLATE UNICODE)", 'spark'),
                         "SELECT CAST(x AS STRING COLLATE UNICODE)")
        self.assertEqual(transpile("SELECT CAST(x AS TEXT COLLATE bar)", read='postgres', write='spark')[0],
                         "SELECT CAST(x AS STRING COLLATE bar)")


if __name__ == '__main__':
    unittest.main()
