"""Edge cases for nonisomorphic trees at small orders: counts against OEIS, the generator, and negative orders."""
import itertools
import unittest

import networkx as nx

A000055 = [1, 1, 1, 1, 2, 3, 6, 11, 23, 47, 106, 235, 551]  # OEIS, from order 0; networkx counts order 0 as 0


class NonisomorphicTreesEdgeTests(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(nx.number_of_nonisomorphic_trees(0), 0)
        for n in range(1, len(A000055)):
            self.assertEqual(nx.number_of_nonisomorphic_trees(n), A000055[n], n)

    def test_generator_agrees_and_yields_distinct_trees(self):
        for n in range(0, 9):
            trees = list(nx.nonisomorphic_trees(n))
            self.assertEqual(len(trees), nx.number_of_nonisomorphic_trees(n), n)
            for T in trees:
                self.assertEqual(len(T), n)
                self.assertTrue(nx.is_tree(T))
            for a, b in itertools.combinations(trees, 2):
                self.assertFalse(nx.is_isomorphic(a, b))

    def test_order_one_and_two(self):
        (one,) = list(nx.nonisomorphic_trees(1))
        self.assertEqual((sorted(one.nodes()), list(one.edges())), ([0], []))
        (two,) = list(nx.nonisomorphic_trees(2))
        self.assertEqual(two.number_of_edges(), 1)

    def test_negative_orders(self):
        for n in (-1, -5):
            with self.assertRaisesRegex(ValueError, 'non-negative'):
                nx.number_of_nonisomorphic_trees(n)
            gen = nx.nonisomorphic_trees(n)  # creating the generator is fine; iterating raises
            with self.assertRaisesRegex(ValueError, 'non-negative'):
                list(gen)


if __name__ == '__main__':
    unittest.main()
