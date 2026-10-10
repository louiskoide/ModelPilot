"""Edge cases for option-only MySQL ALTER TABLE: order independence, single options, actions written first."""
import unittest

from sqlglot import exp, parse_one


def alter(sql):
    return parse_one(sql, read='mysql').assert_is(exp.Alter)


class AlterOptionsEdgeTests(unittest.TestCase):
    def test_the_parse_does_not_depend_on_option_order(self):
        for sql in ("ALTER TABLE t AUTO_INCREMENT=10, COMMENT='hi'", "ALTER TABLE t COMMENT='hi', AUTO_INCREMENT=10"):
            with self.subTest(sql):
                node = alter(sql)
                self.assertEqual((len(node.args['actions']), len(node.args['options'])), (0, 2))
                self.assertEqual(node.sql(dialect='mysql'), sql)

    def test_a_single_option_is_an_option(self):
        node = alter("ALTER TABLE t ENGINE=InnoDB")
        self.assertEqual((len(node.args['actions']), len(node.args['options'])), (0, 1))
        self.assertEqual(node.sql(dialect='mysql'), "ALTER TABLE t ENGINE=InnoDB")

    def test_actions_are_written_before_options(self):
        node = alter("ALTER TABLE t ENGINE=InnoDB, DROP COLUMN a")
        self.assertEqual((len(node.args['actions']), len(node.args['options'])), (1, 1))
        self.assertEqual(node.sql(dialect='mysql'), "ALTER TABLE t DROP COLUMN a, ENGINE=InnoDB")

    def test_actions_without_options_are_unchanged(self):
        node = alter("ALTER TABLE t ADD COLUMN c INT, DROP COLUMN d")
        self.assertEqual(len(node.args['actions']), 2)
        self.assertFalse(node.args.get('options'))


if __name__ == '__main__':
    unittest.main()
