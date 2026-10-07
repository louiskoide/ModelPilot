"""Edge cases for is_perfect_graph: the definition on small random graphs, named graphs and refused graph types."""
import itertools
import random
import unittest

import networkx as nx


def colorable(G, k):
    nodes = sorted(G, key=G.degree, reverse=True)
    color = {}

    def place(i):
        if i == len(nodes):
            return True
        n = nodes[i]
        for c in range(k):
            if all(color.get(m) != c for m in G[n]):
                color[n] = c
                if place(i + 1):
                    return True
                del color[n]
        return False
    return place(0)


def chromatic(G):
    return next(k for k in range(len(G) + 1) if colorable(G, k))


def clique_number(G):
    return max((len(c) for c in nx.find_cliques(G)), default=0)


def perfect(G):
    for r in range(1, len(G) + 1):
        for nodes in itertools.combinations(G, r):
            H = G.subgraph(nodes)
            if chromatic(H) != clique_number(H):
                return False
    return True


class PerfectGraphEdgeTests(unittest.TestCase):
    def test_matches_the_definition_on_small_random_graphs(self):
        rng = random.Random(11)
        for i in range(45):
            G = nx.gnp_random_graph(rng.randrange(1, 8), rng.choice([.3, .5, .7]), seed=rng.randrange(10**6))
            with self.subTest(i=i, edges=sorted(G.edges())):
                self.assertEqual(nx.is_perfect_graph(G), perfect(G))

    def test_named_graphs(self):
        self.assertFalse(nx.is_perfect_graph(nx.petersen_graph()))
        self.assertTrue(nx.is_perfect_graph(nx.complete_bipartite_graph(3, 4)))
        self.assertTrue(nx.is_perfect_graph(nx.path_graph(9)))
        self.assertFalse(nx.is_perfect_graph(nx.cycle_graph(9)))
        self.assertFalse(nx.is_perfect_graph(nx.complement(nx.cycle_graph(9))))
        self.assertTrue(nx.is_perfect_graph(nx.complement(nx.cycle_graph(8))))
        W = nx.wheel_graph(6)  # hub plus a 5-cycle: the rim is an induced odd hole
        self.assertFalse(nx.is_perfect_graph(W))

    def test_tiny_and_empty_graphs(self):
        self.assertTrue(nx.is_perfect_graph(nx.Graph()))
        self.assertTrue(nx.is_perfect_graph(nx.empty_graph(4)))
        self.assertTrue(nx.is_perfect_graph(nx.Graph([('a', 'b')])))

    def test_odd_hole_hidden_among_other_nodes(self):
        G = nx.cycle_graph(5)
        G.add_edges_from([(0, 'x'), ('x', 'y'), ('y', 2), (5, 0), (5, 1)])
        self.assertFalse(nx.is_perfect_graph(G))

    def test_refused_graph_types(self):
        for G in (nx.DiGraph([(0, 1)]), nx.MultiGraph([(0, 1)]), nx.MultiDiGraph([(0, 1)])):
            with self.assertRaises(nx.NetworkXNotImplemented):
                nx.is_perfect_graph(G)


if __name__ == '__main__':
    unittest.main()
