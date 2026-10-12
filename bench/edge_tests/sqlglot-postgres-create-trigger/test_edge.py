"""Edge cases for PostgreSQL CREATE TRIGGER: every part of a full constraint trigger in the tree, OR REPLACE with
several events, NOT DEFERRABLE, PROCEDURE with arguments, REFERENCING without AS, and other dialects' bodies."""
import logging
import unittest

from sqlglot import exp, parse_one


def postgres(sql):
    return parse_one(sql, read='postgres')


class CreateTriggerEdgeTests(unittest.TestCase):
    def test_a_full_constraint_trigger_has_every_part_in_its_tree(self):
        sql = ("CREATE CONSTRAINT TRIGGER audit AFTER UPDATE OF a, b ON s.t FROM s.ref DEFERRABLE INITIALLY DEFERRED "
               "REFERENCING OLD TABLE AS before_rows NEW TABLE AS after_rows FOR EACH STATEMENT WHEN (OLD.a <> NEW.a) "
               "EXECUTE FUNCTION CHECK_IT('x', 2)")
        tree = postgres(sql)
        self.assertIsInstance(tree, exp.Create)
        self.assertEqual((tree.args['kind'], tree.this.name), ('TRIGGER', 'audit'))
        props = tree.find(exp.TriggerProperties)
        self.assertIsNotNone(props)
        self.assertEqual((props.args['table'].sql(), props.args['referenced_table'].sql()), ('s.t', 's.ref'))
        self.assertEqual((props.args['timing'], props.args['for_each']), ('AFTER', 'STATEMENT'))
        self.assertTrue(props.args['constraint'])
        self.assertEqual((props.args['deferrable'], props.args['initially']), ('DEFERRABLE', 'DEFERRED'))
        (event,) = props.args['events']
        self.assertIsInstance(event, exp.TriggerEvent)
        self.assertEqual((event.this, [c.sql() for c in event.args['columns']]), ('UPDATE', ['a', 'b']))
        referencing = props.args['referencing']
        self.assertIsInstance(referencing, exp.TriggerReferencing)
        self.assertEqual((referencing.args['old'].name, referencing.args['new'].name), ('before_rows', 'after_rows'))
        self.assertIsInstance(props.args['when'], exp.NEQ)
        self.assertIsInstance(props.args['execute'], exp.TriggerExecute)
        self.assertEqual(props.args['execute'].this.sql(), "CHECK_IT('x', 2)")
        self.assertEqual(tree.sql(dialect='postgres'), sql)

    def test_or_replace_with_several_events(self):
        sql = 'CREATE OR REPLACE TRIGGER t BEFORE INSERT OR UPDATE OR DELETE ON s.u FOR EACH ROW EXECUTE FUNCTION F()'
        tree = postgres(sql)
        self.assertTrue(tree.args.get('replace'))
        props = tree.find(exp.TriggerProperties)
        self.assertEqual([e.this for e in props.args['events']], ['INSERT', 'UPDATE', 'DELETE'])
        self.assertFalse(props.args.get('constraint'))
        self.assertEqual(tree.sql(dialect='postgres'), sql)

    def test_not_deferrable_initially_immediate(self):
        sql = ('CREATE CONSTRAINT TRIGGER t AFTER DELETE ON x NOT DEFERRABLE INITIALLY IMMEDIATE FOR EACH ROW '
               'EXECUTE FUNCTION F()')
        props = postgres(sql).find(exp.TriggerProperties)
        self.assertEqual((props.args['deferrable'], props.args['initially']), ('NOT DEFERRABLE', 'IMMEDIATE'))
        self.assertEqual(postgres(sql).sql(dialect='postgres'), sql)

    def test_procedure_with_arguments_and_referencing_without_as(self):
        self.assertEqual(postgres("CREATE TRIGGER t AFTER INSERT ON x FOR EACH ROW EXECUTE PROCEDURE log_it('a', 1)")
                         .sql(dialect='postgres'),
                         "CREATE TRIGGER t AFTER INSERT ON x FOR EACH ROW EXECUTE FUNCTION LOG_IT('a', 1)")
        self.assertEqual(postgres('CREATE TRIGGER t AFTER UPDATE ON x REFERENCING NEW TABLE new_rows FOR EACH STATEMENT '
                                  'EXECUTE FUNCTION F()').sql(dialect='postgres'),
                         'CREATE TRIGGER t AFTER UPDATE ON x REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT '
                         'EXECUTE FUNCTION F()')

    def test_other_dialects_bodies_still_fall_back_to_a_command(self):
        for dialect, sql in (
            ('mysql', 'CREATE TRIGGER stamp BEFORE UPDATE ON t FOR EACH ROW BEGIN SET NEW.changed = NOW() END'),
            ('sqlite', "CREATE TRIGGER log AFTER DELETE ON t BEGIN INSERT INTO gone (id) VALUES (OLD.id) END"),
            ('tsql', 'CREATE TRIGGER audit ON orders AFTER DELETE AS BEGIN INSERT INTO gone SELECT id FROM deleted END'),
        ):
            with self.subTest(dialect), self.assertLogs('sqlglot', level=logging.WARNING) as logs:
                tree = parse_one(sql, read=dialect)
                self.assertIsInstance(tree, exp.Command)
                self.assertEqual(tree.sql(dialect=dialect), sql)
                self.assertTrue(any('contains unsupported syntax' in line for line in logs.output))


if __name__ == '__main__':
    unittest.main()
