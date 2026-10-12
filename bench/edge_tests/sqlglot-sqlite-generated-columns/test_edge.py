"""Edge cases for SQLite generated columns: other expressions and types, VIRTUAL dropped, and SQLite itself accepting
what sqlglot writes, with the storage it was asked for."""
import sqlite3
import unittest

from sqlglot import exp, parse_one


def sqlite_sql(sql, read='sqlite'):
    return parse_one(sql, read=read).sql(dialect='sqlite')


class GeneratedColumnEdgeTests(unittest.TestCase):
    def test_other_expressions_and_types(self):
        for sql, expected in (
            ("CREATE TABLE t (a TEXT, b TEXT GENERATED ALWAYS AS (UPPER(a)) STORED)",
             "CREATE TABLE t (a TEXT, b TEXT AS (UPPER(a)) STORED)"),
            ("CREATE TABLE t (a INTEGER, b AS (a + 1) VIRTUAL)", "CREATE TABLE t (a INTEGER, b AS (a + 1))"),
            ("CREATE TABLE t (a REAL, b REAL AS (a / 2) STORED, c AS (a * a))", None),
        ):
            with self.subTest(sql):
                self.assertEqual(sqlite_sql(sql), expected or sql)

    def test_persisted_follows_the_storage_keyword(self):
        for sql, persisted in (("CREATE TABLE t (a INTEGER, b AS (a * 2) STORED)", True),
                               ("CREATE TABLE t (a INTEGER, b AS (a * 2) VIRTUAL)", False),
                               ("CREATE TABLE t (a INTEGER, b INTEGER GENERATED ALWAYS AS (a * 2) STORED)", True),
                               ("CREATE TABLE t (a INTEGER, b INTEGER GENERATED ALWAYS AS (a * 2))", False)):
            with self.subTest(sql):
                computed = parse_one(sql, read='sqlite').find(exp.ComputedColumnConstraint)
                self.assertIsNotNone(computed)
                self.assertEqual(bool(computed.args.get('persisted')), persisted)
                self.assertNotIn('AUTOINCREMENT', sqlite_sql(sql))

    def test_sqlite_runs_what_is_written_with_the_storage_asked_for(self):
        # PRAGMA table_xinfo: hidden is 2 for a VIRTUAL generated column, 3 for a STORED one.
        for sql, read, hidden in (
            ("CREATE TABLE t (a INTEGER, b INTEGER GENERATED ALWAYS AS (a * 2) STORED)", 'sqlite', 3),
            ("CREATE TABLE t (a INTEGER, b INTEGER GENERATED ALWAYS AS (a * 2) VIRTUAL)", 'sqlite', 2),
            ("CREATE TABLE t (a INTEGER, b AS (a * 2) STORED)", 'sqlite', 3),
            ("CREATE TABLE t (a INT, b AS (a * 2) PERSISTED)", 'tsql', 3),
        ):
            with self.subTest(sql):
                db = sqlite3.connect(':memory:')
                db.execute(sqlite_sql(sql, read))
                db.execute('INSERT INTO t (a) VALUES (21)')
                self.assertEqual(db.execute('SELECT b FROM t').fetchone()[0], 42)
                info = {row[1]: row[6] for row in db.execute('PRAGMA table_xinfo(t)')}
                self.assertEqual(info['b'], hidden)

    def test_identity_still_becomes_autoincrement_and_runs(self):
        out = sqlite_sql("CREATE TABLE t (a INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY, b TEXT)")
        self.assertEqual(out, "CREATE TABLE t (a INTEGER PRIMARY KEY AUTOINCREMENT, b TEXT)")
        db = sqlite3.connect(':memory:')
        db.execute(out)
        db.execute("INSERT INTO t (b) VALUES ('x')")
        self.assertEqual(db.execute('SELECT a FROM t').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
