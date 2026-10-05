"""Edge cases for VF2 isomorphisms_iter: random pairs, different sizes, reuse and the module functions."""
import itertools
import random
import unittest

import networkx as nx
from networkx.algorithms import isomorphism as iso


def matcher(G1, G2):
    return (iso.DiGraphMatcher if G1.is_directed() else iso.GraphMatcher)(G1, G2)


class Vf2IsomorphismsIterEdgeTests(unittest.TestCase):
    def test_iter_is_empty_exactly_when_not_isomorphic(self):
        rng = random.Random(5)
        for directed in (False, True):
            for _ in range(60):
                n1 = rng.randrange(1, 6)
                n2 = n1 if rng.random() < .5 else rng.randrange(1, 6)
                G1 = nx.gnp_random_graph(n1, .5, seed=rng.randrange(10**6), directed=directed)
                G2 = nx.gnp_random_graph(n2, .5, seed=rng.randrange(10**6), directed=directed)
                gm = matcher(G1, G2)
                with self.subTest(directed=directed, g1=sorted(G1.edges()), n1=n1, g2=sorted(G2.edges()), n2=n2):
                    found = list(gm.isomorphisms_iter())
                    self.assertEqual(bool(found), matcher(G1, G2).is_isomorphic())
                    self.assertEqual(bool(found), nx.is_isomorphic(G1, G2))
                    for mapping in found:
                        self.assertEqual(len(mapping), len(G2))

    def test_subgraph_iterators_still_yield_for_a_larger_first_graph(self):
        for cls in (nx.Graph, nx.DiGraph, nx.MultiGraph, nx.MultiDiGraph):
            G1 = nx.disjoint_union(nx.path_graph(3, create_using=cls), nx.empty_graph(2, create_using=cls))
            G2 = nx.path_graph(3, create_using=cls)
            gm = matcher(G1, G2)
            with self.subTest(cls=cls.__name__):
                self.assertEqual(list(gm.isomorphisms_iter()), [])
                self.assertTrue(list(matcher(G1, G2).subgraph_isomorphisms_iter()))
                self.assertTrue(list(matcher(G1, G2).subgraph_monomorphisms_iter()))

    def test_same_order_and_degrees_but_not_isomorphic(self):
        G1 = nx.disjoint_union(nx.cycle_graph(3), nx.cycle_graph(3))
        G2 = nx.cycle_graph(6)  # every node has degree 2 in both
        self.assertEqual(list(matcher(G1, G2).isomorphisms_iter()), [])
        self.assertFalse(matcher(G1, G2).is_isomorphic())

    def test_reuse_counts_every_isomorphism(self):
        for n in (3, 4, 5):
            G1, G2 = nx.complete_graph(n), nx.complete_graph(n)
            gm = matcher(G1, G2)
            self.assertTrue(gm.is_isomorphic())
            first = dict(gm.mapping)
            every = list(gm.isomorphisms_iter())
            self.assertEqual(len(every), len(list(itertools.permutations(range(n)))))
            self.assertIn(first, every)
            self.assertEqual(len(list(gm.isomorphisms_iter())), len(every))  # a third pass agrees


if __name__ == '__main__':
    unittest.main()
