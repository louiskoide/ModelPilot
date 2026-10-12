"""Edge cases for the DAX dialect: found by name, lower-case keywords, three nested FILTERs, grouped && and ||, other
comparisons, a bare column in ORDER BY, and another target dialect."""
import unittest

import sqlglot
from sqlglot import exp, parse_one


def dax(sql, dialect=None):
    return parse_one(sql, read='dax').sql(dialect=dialect)


class DAXEdgeTests(unittest.TestCase):
    def test_the_dialect_is_found_by_name(self):
        from sqlglot.dialects.dialect import Dialect, Dialects
        self.assertIsNotNone(Dialect.get_or_raise('dax'))
        self.assertEqual(Dialects.DAX.value, 'dax')
        self.assertEqual(sqlglot.transpile('EVALUATE Sales', read='dax', write='snowflake'), ['SELECT * FROM Sales'])
        self.assertIsInstance(parse_one('EVALUATE FILTER(Sales, Sales[Amount] > 100)', read='dax'), exp.Select)

    def test_keywords_are_case_insensitive(self):
        self.assertEqual(dax('evaluate Sales'), 'SELECT * FROM Sales')
        self.assertEqual(dax('Evaluate filter(Sales, Sales[Amount] > 1) order by Sales[Amount] desc'),
                         'SELECT * FROM Sales WHERE Sales."Amount" > 1 ORDER BY Sales."Amount" DESC')

    def test_three_nested_filters_are_all_anded(self):
        tree = parse_one('EVALUATE FILTER(FILTER(FILTER(Sales, Sales[A] > 1), Sales[B] < 2), Sales[C] = 3)', read='dax')
        where = tree.args['where']
        self.assertEqual(tree.find(exp.From).sql(), 'FROM Sales')
        self.assertEqual(sorted(c.sql() for c in where.find_all(exp.GT, exp.LT, exp.EQ)),
                         ['Sales."A" > 1', 'Sales."B" < 2', 'Sales."C" = 3'])
        self.assertIsNone(where.find(exp.Or))
        self.assertEqual(len(list(where.find_all(exp.And))), 2)

    def test_grouped_conditions_and_other_comparisons(self):
        self.assertEqual(dax('EVALUATE FILTER(Sales, (Sales[Amount] > 100 || Sales[Qty] < 5) && Sales[Region] = "West")'),
                         'SELECT * FROM Sales WHERE (Sales."Amount" > 100 OR Sales."Qty" < 5) AND Sales."Region" = \'West\'')
        self.assertEqual(dax('EVALUATE FILTER(Sales, Sales[Amount] >= 10.5)'),
                         'SELECT * FROM Sales WHERE Sales."Amount" >= 10.5')
        self.assertEqual(dax('EVALUATE FILTER(Sales, Sales[Name] <> "x")'), 'SELECT * FROM Sales WHERE Sales."Name" <> \'x\'')

    def test_a_bare_column_in_order_by_and_another_target(self):
        self.assertEqual(dax('EVALUATE Sales ORDER BY [Total Amount] DESC'), 'SELECT * FROM Sales ORDER BY "Total Amount" DESC')
        self.assertEqual(dax("EVALUATE FILTER('Sales Data', 'Sales Data'[Amount] > 100)", 'postgres'),
                         'SELECT * FROM "Sales Data" WHERE "Sales Data"."Amount" > 100')


if __name__ == '__main__':
    unittest.main()
