"""Edge cases for dominance: both functions against the definitions on random digraphs, and the start node."""
import random
import unittest

import networkx as nx


def dominators(G, start):
    """d dominates n when every path from start to n passes through d (brute force by deletion)."""
    reach = set(nx.descendants(G, start)) | {start}
    dom = {n: {n, start} for n in reach}
    for d in reach - {start}:
        H = G.subgraph(set(G) - {d})
        still = set(nx.descendants(H, start)) | {start}
        for n in reach - still:
            dom[n].add(d)
    return dom


def idoms(G, start):
    dom = dominators(G, start)
    out = {}
    for n, ds in dom.items():
        strict = ds - {n}
        if strict:  # start has none
            out[n] = next(d for d in strict if strict <= dom[d])  # dominated by all other strict dominators
    return out


def frontiers(G, start):
    dom = dominators(G, start)
    out = {x: set() for x in dom}
    for y in dom:
        for p in G.pred[y]:
            if p in dom:
                for x in dom[p]:
                    if not (x in dom[y] and x != y):
                        out[x].add(y)
    return out


def random_digraph(rng, n):
    G = nx.DiGraph()
    G.add_nodes_from(range(n))
    G.add_edges_from((u, v) for u in range(n) for v in range(n) if rng.random() < .25)
    return G


class DominanceEdgeTests(unittest.TestCase):
    def test_immediate_dominators_match_the_definition(self):
        rng = random.Random(8)
        for i in range(60):
            G = random_digraph(rng, rng.randrange(1, 8))
            with self.subTest(i=i, edges=sorted(G.edges())):
                self.assertEqual(nx.immediate_dominators(G, 0), idoms(G, 0))

    def test_dominance_frontiers_match_the_definition(self):
        rng = random.Random(9)
        for i in range(60):
            G = random_digraph(rng, rng.randrange(1, 8))
            with self.subTest(i=i, edges=sorted(G.edges())):
                self.assertEqual(nx.dominance_frontiers(G, 0), frontiers(G, 0))

    def test_start_never_has_an_immediate_dominator(self):
        G = nx.DiGraph([('s', 'a'), ('a', 's'), ('a', 'b'), ('b', 's')])
        idom = nx.immediate_dominators(G, 's')
        self.assertNotIn('s', idom)
        self.assertEqual(idom, {'a': 's', 'b': 'a'})
        self.assertEqual(nx.dominance_frontiers(G, 's'), {'s': {'s'}, 'a': {'s'}, 'b': {'s'}})

    def test_multidigraph_and_errors(self):
        G = nx.MultiDiGraph([(0, 1), (0, 1), (1, 0)])
        self.assertEqual(nx.immediate_dominators(G, 0), {1: 0})
        self.assertEqual(nx.dominance_frontiers(G, 0), {0: {0}, 1: {0}})
        for func in (nx.immediate_dominators, nx.dominance_frontiers):
            with self.assertRaises(nx.NetworkXNotImplemented):
                func(nx.Graph([(0, 1)]), 0)
            with self.assertRaises(nx.NetworkXError):
                func(nx.DiGraph([(0, 1)]), 7)


if __name__ == '__main__':
    unittest.main()
