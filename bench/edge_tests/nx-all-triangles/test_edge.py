"""Edge cases for all_triangles: each triangle once against brute force, nbunch, multigraphs and self-loops."""
from collections import Counter
import itertools
import random
import unittest

import networkx as nx


def brute(G, nbunch=None):
    keep = set(G) if nbunch is None else set(nbunch)
    return Counter(frozenset(t) for t in itertools.combinations(G, 3)
                   if all(G.has_edge(a, b) for a, b in itertools.combinations(t, 2)) and keep & set(t))


class AllTrianglesEdgeTests(unittest.TestCase):
    def found(self, G, **kw):
        out = list(nx.all_triangles(G, **kw))
        for t in out:
            self.assertIsInstance(t, tuple)
            self.assertEqual(len(set(t)), 3)
        return Counter(frozenset(t) for t in out)

    def test_each_triangle_once_on_random_graphs(self):
        rng = random.Random(12)
        for i in range(40):
            G = nx.gnp_random_graph(rng.randrange(0, 12), rng.choice([.3, .6, .9]), seed=rng.randrange(10**6))
            with self.subTest(i=i):
                self.assertEqual(self.found(G), brute(G))
                self.assertEqual(sum(self.found(G).values()), sum(nx.triangles(G).values()) // 3)

    def test_nbunch_yields_each_touching_triangle_once(self):
        rng = random.Random(13)
        for i in range(30):
            G = nx.gnp_random_graph(10, .5, seed=rng.randrange(10**6))
            nbunch = rng.sample(range(10), rng.randrange(1, 5))
            with self.subTest(i=i, nbunch=nbunch):
                self.assertEqual(self.found(G, nbunch=nbunch), brute(G, nbunch))
        G = nx.complete_graph(4)
        self.assertEqual(self.found(G, nbunch=2), brute(G, [2]))  # a single node

    def test_multigraph_and_self_loops(self):
        G = nx.MultiGraph([(0, 1), (0, 1), (1, 2), (2, 0), (2, 0), (0, 0), (2, 3), (3, 3)])
        self.assertEqual(self.found(G), Counter({frozenset({0, 1, 2}): 1}))
        H = nx.Graph([(0, 1), (1, 2), (2, 0), (1, 1)])
        self.assertEqual(self.found(H), Counter({frozenset({0, 1, 2}): 1}))

    def test_is_lazy_and_refuses_directed(self):
        gen = nx.all_triangles(nx.complete_graph(5))
        self.assertEqual(len(set(next(gen))), 3)
        with self.assertRaises(nx.NetworkXNotImplemented):
            list(nx.all_triangles(nx.MultiDiGraph([(0, 1), (1, 2), (2, 0)])))


if __name__ == '__main__':
    unittest.main()
