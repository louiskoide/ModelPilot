"""Edge cases for Postgres parameter modes: user-defined types, mixed lists, mode words as names, the mode on write."""
import unittest

from sqlglot import parse_one


def roundtrip(sql):
    return parse_one(sql, read='postgres').sql(dialect='postgres')


class ParameterModeEdgeTests(unittest.TestCase):
    def test_a_mode_before_a_name_and_a_user_defined_type(self):
        for sql in ("CREATE FUNCTION f(OUT x my_type)", "CREATE FUNCTION f(INOUT p user_point)"):
            with self.subTest(sql):
                self.assertEqual(roundtrip(sql), sql)

    def test_mixed_modes_and_defaults(self):
        sql = "CREATE FUNCTION f(VARIADIC nums INT[], OUT total BIGINT)"
        self.assertEqual(roundtrip(sql), sql)
        sql = "CREATE FUNCTION f(a TEXT DEFAULT 'x', INOUT b INT DEFAULT 1, OUT c BOOLEAN)"
        self.assertEqual(roundtrip(sql), sql)

    def test_a_mode_word_before_a_type_names_the_parameter(self):
        for sql in ("CREATE FUNCTION f(out TEXT)", "CREATE FUNCTION f(a INT, inout BIGINT)"):
            with self.subTest(sql):
                self.assertEqual(roundtrip(sql), sql)

    def test_the_mode_is_written_in_upper_case(self):
        self.assertEqual(roundtrip("CREATE FUNCTION f(out x INT, inout y TEXT)"),
                         "CREATE FUNCTION f(OUT x INT, INOUT y TEXT)")


if __name__ == '__main__':
    unittest.main()
