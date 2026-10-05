"""Edge cases for bipartite butterflies: brute force on random bipartite graphs, nodes=, and the clustering link."""
import itertools
import random
import unittest

import networkx as nx
from networkx.algorithms import bipartite


def brute(G):
    counts = dict.fromkeys(G, 0)
    for a, b, c, d in itertools.combinations(G, 4):
        quad = (a, b, c, d)
        for x, y, z, w in ((a, b, c, d), (a, b, d, c), (a, c, b, d)):  # the three 4-cycles on four nodes
            if G.has_edge(x, y) and G.has_edge(y, z) and G.has_edge(z, w) and G.has_edge(w, x):
                for n in quad:
                    counts[n] += 1
    return counts


class ButterflyEdgeTests(unittest.TestCase):
    def test_matches_brute_force_on_random_bipartite_graphs(self):
        rng = random.Random(9)
        for i in range(30):
            G = bipartite.random_graph(rng.randrange(2, 6), rng.randrange(2, 6), rng.random(), seed=rng.randrange(10**6))
            with self.subTest(i=i, edges=sorted(G.edges())):
                self.assertEqual(bipartite.butterflies(G), brute(G))

    def test_unequal_sides(self):
        G = nx.complete_bipartite_graph(2, 4)  # C(2,2) * C(4,2) = 6 butterflies
        counts = bipartite.butterflies(G)
        self.assertEqual(sum(counts.values()), 24)
        self.assertEqual([counts[n] for n in (0, 1)], [6, 6])
        self.assertEqual({counts[n] for n in range(2, 6)}, {3})

    def test_nodes_keeps_whole_graph_counts_and_order_free(self):
        G = nx.complete_bipartite_graph(3, 3)
        self.assertEqual(bipartite.butterflies(G, nodes=[4, 0, 'missing']), {4: 6, 0: 6})
        self.assertEqual(bipartite.butterflies(G, nodes=[]), {})

    def test_string_nodes_and_bipartite_attribute_free_graphs(self):
        G = nx.Graph([('a', 'x'), ('a', 'y'), ('b', 'x'), ('b', 'y'), ('c', 'x')])
        self.assertEqual(bipartite.butterflies(G), {'a': 1, 'b': 1, 'x': 1, 'y': 1, 'c': 0})

    def test_robins_alexander_is_four_butterflies_over_three_paths(self):
        rng = random.Random(13)
        for i in range(15):
            G = bipartite.random_graph(4, 5, .6, seed=rng.randrange(10**6))
            if G.order() < 4 or G.size() < 3:
                continue
            three_paths = sum((G.degree(u) - 1) * (G.degree(v) - 1) for u, v in G.edges())  # no triangles
            expected = 0 if not three_paths else 4 * (sum(brute(G).values()) // 4) / three_paths
            with self.subTest(i=i, edges=sorted(G.edges())):
                self.assertAlmostEqual(bipartite.robins_alexander_clustering(G), expected, places=9)

if __name__ == '__main__':
    unittest.main()
