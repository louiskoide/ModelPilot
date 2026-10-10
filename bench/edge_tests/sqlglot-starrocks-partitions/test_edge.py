"""Edge cases for StarRocks and MySQL partitions: other names and values, several ranges, tuples and expressions."""
import unittest

from sqlglot import parse_one


def roundtrip(sql, dialect):
    return parse_one(sql, read=dialect).sql(dialect=dialect)


class PartitionEdgeTests(unittest.TestCase):
    def test_starrocks_ranges_and_lists(self):
        for sql in (
            "CREATE TABLE s (d DATE) PARTITION BY RANGE (d) (PARTITION a VALUES LESS THAN ('2024-01-01'), PARTITION b VALUES LESS THAN (MAXVALUE))",
            "CREATE TABLE s (c STRING) PARTITION BY LIST (c) (PARTITION x VALUES IN ('u', 'v'), PARTITION y VALUES IN ('w'))",
            "CREATE TABLE s (d DATE) PARTITION BY RANGE (d) (START ('2024-01-01') END ('2024-03-01') EVERY (INTERVAL 1 MONTH))",
        ):
            with self.subTest(sql):
                self.assertEqual(roundtrip(sql, 'starrocks'), sql)

    def test_starrocks_column_and_expression_partitions(self):
        for sql in ("CREATE TABLE s (a INT, b INT, c INT) PARTITION BY (a, b, c)",
                    "CREATE TABLE s (d DATE, k STRING) PARTITION BY DATE_TRUNC('MONTH', d), k"):
            with self.subTest(sql):
                self.assertEqual(roundtrip(sql, 'starrocks'), sql)

    def test_starrocks_multi_column_list_tuples(self):
        sql = "CREATE TABLE s (a INT, b STRING) PARTITION BY LIST (a, b) (PARTITION p1 VALUES IN ((1, 'x')), PARTITION p2 VALUES IN ((2, 'y'), (3, 'z')))"
        self.assertEqual(roundtrip(sql, 'starrocks'), sql)

    def test_mysql_ranges_and_lists(self):
        for sql in (
            "CREATE TABLE m (y INT) PARTITION BY RANGE (y) (PARTITION p0 VALUES LESS THAN (1990), PARTITION p1 VALUES LESS THAN (2000), PARTITION p2 VALUES LESS THAN (MAXVALUE))",
            "CREATE TABLE m (d DATE) PARTITION BY RANGE (MONTH(d)) (PARTITION h1 VALUES LESS THAN (7), PARTITION h2 VALUES LESS THAN (MAXVALUE))",
            "CREATE TABLE m (r INT) PARTITION BY LIST (r) (PARTITION a VALUES IN (1), PARTITION b VALUES IN (2, 3))",
        ):
            with self.subTest(sql):
                self.assertEqual(roundtrip(sql, 'mysql'), sql)


if __name__ == '__main__':
    unittest.main()
