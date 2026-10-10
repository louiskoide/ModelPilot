"""Edge cases for ClickHouse MODIFY COLUMN: other types, the parsed node, transpiling, IF EXISTS elsewhere."""
import unittest

from sqlglot import exp, parse_one, transpile


class ModifyColumnEdgeTests(unittest.TestCase):
    def test_other_types_are_type_changes(self):
        for sql, out in (("ALTER TABLE t MODIFY COLUMN c Nullable(String)", "ALTER TABLE t ALTER COLUMN c TYPE Nullable(String)"),
                         ("ALTER TABLE db.t MODIFY COLUMN c Array(UInt8)", "ALTER TABLE db.t ALTER COLUMN c TYPE Array(UInt8)")):
            with self.subTest(sql):
                self.assertEqual(parse_one(sql, read='clickhouse').sql(dialect='clickhouse'), out)

    def test_the_action_carries_the_column_and_its_type(self):
        alter = parse_one("ALTER TABLE t MODIFY COLUMN c Int64", read='clickhouse').assert_is(exp.Alter)
        action = alter.args['actions'][0].assert_is(exp.AlterColumn)
        self.assertEqual(action.this.name, 'c')
        self.assertIsInstance(action.args.get('dtype'), exp.DataType)

    def test_it_transpiles_like_alter_column(self):
        self.assertEqual(transpile("ALTER TABLE t MODIFY COLUMN c Int64", read='clickhouse', write='postgres')[0],
                         transpile("ALTER TABLE t ALTER COLUMN c TYPE Int64", read='clickhouse', write='postgres')[0])

    def test_if_exists_is_kept_on_the_node(self):
        alter = parse_one("ALTER TABLE t MODIFY COLUMN IF EXISTS c Int64", read='clickhouse')
        self.assertTrue(alter.args['actions'][0].args.get('exists'))


if __name__ == '__main__':
    unittest.main()
