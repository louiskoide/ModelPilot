"""Edge cases for antichain_width: brute force on random DAGs, node types, self-loops and the public name."""
import itertools
import random
import unittest

import networkx as nx


def brute(G):
    reach = {n: nx.descendants(G, n) for n in G}
    for k in range(len(G), 0, -1):
        for nodes in itertools.combinations(G, k):
            if all(b not in reach[a] and a not in reach[b] for a, b in itertools.combinations(nodes, 2)):
                return k
    return 0


class AntichainWidthEdgeTests(unittest.TestCase):
    def test_matches_brute_force_on_random_dags(self):
        rng = random.Random(21)
        for i in range(40):
            n = rng.randrange(1, 9)
            G = nx.DiGraph()
            G.add_nodes_from(range(n))
            G.add_edges_from((u, v) for u, v in itertools.combinations(range(n), 2) if rng.random() < .3)
            with self.subTest(i=i, edges=sorted(G.edges())):
                self.assertEqual(nx.dag.antichain_width(G), brute(G))

    def test_width_needs_reachability_not_just_edges(self):
        G = nx.DiGraph([(0, 1), (1, 2), (0, 3), (3, 4)])  # two chains below one root
        self.assertEqual(nx.dag.antichain_width(G), 2)
        G = nx.DiGraph([('a', 'b'), ('b', 'c'), ('x', 'y')])  # string nodes, two components
        self.assertEqual(nx.dag.antichain_width(G), 2)

    def test_a_self_loop_is_a_cycle(self):
        with self.assertRaises(nx.NetworkXUnfeasible):
            nx.dag.antichain_width(nx.DiGraph([(0, 1), (1, 1)]))

    def test_undirected_multigraph_is_refused(self):
        with self.assertRaises(nx.NetworkXNotImplemented):
            nx.dag.antichain_width(nx.MultiGraph([(0, 1)]))

    def test_input_graph_is_not_changed(self):
        G = nx.DiGraph([(0, 1), (1, 2)])
        before = (sorted(G.nodes()), sorted(G.edges()))
        nx.dag.antichain_width(G)
        self.assertEqual((sorted(G.nodes()), sorted(G.edges())), before)


if __name__ == '__main__':
    unittest.main()
