"""Edge cases for SQLite's ||, -> and ->> tier: meaning checked by SQLite itself, longer chains, other dialects."""
import sqlite3
import unittest

from sqlglot import exp, parse_one

DOC = """'{"a": {"b": 7, "c": "x"}, "k": "a"}'"""


class SQLiteArrowTierEdgeTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')

    def value(self, sql):
        return self.db.execute(sql).fetchone()[0]

    def test_rewritten_sql_means_the_same_to_sqlite(self):
        # Whatever sqlglot writes must evaluate, in SQLite, to what the input does.
        for sql in ("SELECT 2 * 3 || 4", "SELECT 1 + 2 || 3", "SELECT 7 - 1 || 2", "SELECT 10 / 2 || 5",
                    "SELECT 'x' || 1 + 2", f"SELECT {DOC} -> 'a' ->> 'b' + 1", f"SELECT 1 + {DOC} -> 'a' ->> 'b'",
                    f"SELECT {DOC} ->> 'k' || 'z'", f"SELECT 2 * {DOC} -> 'a' ->> 'b'",
                    "SELECT 'a' || 'b' COLLATE NOCASE || 'c'"):
            with self.subTest(sql):
                out = parse_one(sql, read='sqlite').sql(dialect='sqlite')
                self.assertEqual(self.value(out), self.value(sql), out)

    def test_a_mixed_chain_is_left_associative(self):
        item = parse_one('SELECT a || b ->> c ->> d', read='sqlite').selects[0]
        item.assert_is(exp.JSONExtractScalar)
        inner = item.this.assert_is(exp.JSONExtractScalar)
        inner.this.unnest().assert_is(exp.DPipe)  # parenthesized or not, the concatenation is innermost

    def test_more_arithmetic_around_the_tier(self):
        for sql, out in (("SELECT a ->> b * c FROM t", "SELECT (a ->> b) * c FROM t"),
                         ("SELECT a / b || c FROM t", "SELECT a / (b || c) FROM t"),
                         ("SELECT a * b -> c FROM t", "SELECT a * (b -> c) FROM t")):
            with self.subTest(sql):
                self.assertEqual(parse_one(sql, read='sqlite').sql(dialect='sqlite'), out)

    def test_other_dialects_keep_their_precedence(self):
        # Postgres's || binds looser than * and +.
        for sql in ("SELECT a || b * c FROM t", "SELECT 1 + 2 || 3"):
            with self.subTest(sql):
                self.assertEqual(parse_one(sql, read='postgres').sql(dialect='postgres'), sql)


if __name__ == '__main__':
    unittest.main()
