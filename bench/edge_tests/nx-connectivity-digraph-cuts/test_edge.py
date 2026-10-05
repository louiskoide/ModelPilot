"""Edge cases for node connectivity and node cuts: brute force on small graphs, every flow function, both orders."""
import itertools
import random
import unittest

import networkx as nx
from networkx.algorithms import flow
from networkx.algorithms.connectivity import minimum_st_node_cut

FLOWS = [flow.boykov_kolmogorov, flow.dinitz, flow.edmonds_karp, flow.preflow_push, flow.shortest_augmenting_path]


def connected(G):
    return nx.is_strongly_connected(G) if G.is_directed() else nx.is_connected(G)


def brute_connectivity(G):
    """Smallest number of nodes whose removal disconnects G; n - 1 if no removal does (complete graphs)."""
    nodes = list(G)
    simple = nx.DiGraph(G) if G.is_directed() else nx.Graph(G)
    simple.remove_edges_from(list(nx.selfloop_edges(simple)))
    if not connected(simple):
        return 0
    for k in range(1, len(nodes) - 1):
        for cut in itertools.combinations(nodes, k):
            H = simple.copy()
            H.remove_nodes_from(cut)
            if not connected(H):
                return k
    return len(nodes) - 1


def random_digraphs(seed, count, n=6, p=.45):
    rng = random.Random(seed)
    out = []
    while len(out) < count:
        G = nx.gnp_random_graph(n, p, seed=rng.randrange(10**6), directed=True)
        if nx.is_weakly_connected(G):
            out.append(G)
    return out


class ConnectivityEdgeTests(unittest.TestCase):
    def test_digraph_connectivity_matches_brute_force(self):
        for G in random_digraphs(1, 40):
            expected = brute_connectivity(G)
            for f in FLOWS:
                with self.subTest(edges=sorted(G.edges()), flow=f.__name__):
                    self.assertEqual(nx.node_connectivity(G, flow_func=f), expected)

    def test_digraph_minimum_node_cut_is_a_smallest_real_cut(self):
        for G in random_digraphs(2, 30):
            expected = brute_connectivity(G)
            if expected == len(G) - 1:
                continue  # complete digraph: no node cut exists
            for f in FLOWS:
                with self.subTest(edges=sorted(G.edges()), flow=f.__name__):
                    cut = nx.minimum_node_cut(G, flow_func=f)
                    self.assertEqual(len(cut), expected)
                    H = G.copy()
                    H.remove_nodes_from(cut)
                    self.assertFalse(nx.is_strongly_connected(H))

    def test_undirected_results_still_match_brute_force(self):
        rng = random.Random(3)
        checked = 0
        while checked < 25:
            G = nx.gnp_random_graph(7, .5, seed=rng.randrange(10**6))
            if not nx.is_connected(G):
                continue
            checked += 1
            self.assertEqual(nx.node_connectivity(G), brute_connectivity(G))

    def test_only_an_edge_from_source_to_target_makes_a_pair_inseparable(self):
        G = nx.DiGraph([(1, 0), (0, 2), (2, 1)])  # 1 -> 0 only, plus a path 0 -> 2 -> 1
        for f in FLOWS:
            self.assertEqual(minimum_st_node_cut(G, 0, 1, flow_func=f), {2})
            self.assertEqual(minimum_st_node_cut(G, 1, 0, flow_func=f), set())

    def test_parallel_edges_and_self_loops_change_nothing_in_digraphs(self):
        for G in random_digraphs(4, 15):
            M = nx.MultiDiGraph(G)
            M.add_edges_from(list(G.edges()))  # every edge doubled
            M.add_edges_from((u, u) for u in list(G)[:2])  # and two self-loops
            expected = brute_connectivity(G)
            with self.subTest(edges=sorted(G.edges())):
                self.assertEqual(nx.node_connectivity(M), expected)
                if expected < len(G) - 1:
                    cut = nx.minimum_node_cut(M)
                    self.assertEqual(len(cut), expected)
                    self.assertNotIn(None, cut)

    def test_complete_digraph_with_self_loops(self):
        D = nx.complete_graph(4, nx.DiGraph)
        D.add_edges_from((u, u) for u in D)
        self.assertEqual(nx.node_connectivity(D), 3)
        self.assertEqual(len(nx.minimum_node_cut(D)), 3)


if __name__ == '__main__':
    unittest.main()
