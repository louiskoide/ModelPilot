"""Edge cases for generalized_petersen_graph: structure for many (n, k), labels, k = n/2 and bad arguments."""
import unittest

import networkx as nx


class GeneralizedPetersenEdgeTests(unittest.TestCase):
    def test_structure_for_many_parameters(self):
        for n in range(3, 13):
            for k in range(1, n // 2 + 1):
                G = nx.generalized_petersen_graph(n, k)
                with self.subTest(n=n, k=k):
                    self.assertEqual(sorted(G), list(range(2 * n)))
                    outer = {frozenset((i, (i + 1) % n)) for i in range(n)}
                    spokes = {frozenset((i, n + i)) for i in range(n)}
                    inner = {frozenset((n + i, n + (i + k) % n)) for i in range(n)}
                    self.assertEqual({frozenset(e) for e in G.edges()}, outer | spokes | inner)

    def test_k_half_of_n_is_allowed(self):
        G = nx.generalized_petersen_graph(6, 3)
        self.assertEqual(G.number_of_edges(), 6 + 6 + 3)  # each inner edge pairs two nodes once
        self.assertTrue(nx.is_isomorphic(nx.generalized_petersen_graph(4, 1), nx.hypercube_graph(3)))

    def test_bad_arguments(self):
        for n, k in ((2, 1), (0, 0), (5, 0), (5, 3), (7, -1)):
            with self.assertRaises(nx.NetworkXError, msg=(n, k)):
                nx.generalized_petersen_graph(n, k)

    def test_create_using(self):
        G = nx.generalized_petersen_graph(7, 2, create_using=nx.MultiGraph)
        self.assertIsInstance(G, nx.MultiGraph)
        self.assertEqual(G.number_of_edges(), 21)
        H = nx.Graph([('x', 'y')])
        out = nx.generalized_petersen_graph(5, 1, create_using=H)
        self.assertNotIn('x', out)  # an instance is cleared first
        with self.assertRaises(nx.NetworkXError):
            nx.generalized_petersen_graph(5, 2, create_using=nx.DiGraph())


if __name__ == '__main__':
    unittest.main()
