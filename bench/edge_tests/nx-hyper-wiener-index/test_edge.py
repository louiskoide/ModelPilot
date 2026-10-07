"""Edge cases for hyper_wiener_index: the definition on random graphs, weights, tiny graphs and refusals."""
import itertools
import random
import unittest

import networkx as nx


def by_definition(G, weight=None):
    total = 0
    for u, v in itertools.permutations(G, 2):
        d = nx.shortest_path_length(G, u, v, weight=weight)
        total += d + d * d
    return total / 2


class HyperWienerEdgeTests(unittest.TestCase):
    def test_matches_the_definition(self):
        rng = random.Random(18)
        done = 0
        while done < 25:
            G = nx.gnp_random_graph(rng.randrange(2, 10), .4, seed=rng.randrange(10**6))
            if not nx.is_connected(G):
                continue
            for u, v in G.edges():
                G[u][v]['length'] = rng.choice([1, 2, 3.5])
            with self.subTest(edges=sorted(G.edges())):
                self.assertAlmostEqual(nx.hyper_wiener_index(G), by_definition(G))
                self.assertAlmostEqual(nx.hyper_wiener_index(G, weight='length'), by_definition(G, 'length'))
            done += 1

    def test_star_and_single_node(self):
        G = nx.star_graph(4)  # 4 leaf-hub pairs at 1, 6 leaf-leaf pairs at 2
        self.assertEqual(nx.hyper_wiener_index(G), 4 * 2 + 6 * 6)
        self.assertEqual(nx.hyper_wiener_index(nx.empty_graph(1)), 0)

    def test_disconnected_is_infinite(self):
        self.assertEqual(nx.hyper_wiener_index(nx.empty_graph(3)), float('inf'))

    def test_refuses_directed_and_multigraphs(self):
        for G in (nx.DiGraph([(0, 1), (1, 0)]), nx.MultiGraph([(0, 1)])):
            with self.assertRaises(nx.NetworkXNotImplemented):
                nx.hyper_wiener_index(G)


if __name__ == '__main__':
    unittest.main()
