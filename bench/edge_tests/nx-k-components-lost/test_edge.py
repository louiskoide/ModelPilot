"""Edge cases for k_components: brute force on small graphs, other joined-block graphs, and the definition."""
import itertools
import random
import unittest

import networkx as nx


def brute(G):
    nodes = list(G)
    subsets = [frozenset(c) for r in range(2, len(nodes) + 1) for c in itertools.combinations(nodes, r)]
    conn = {}
    for s in subsets:
        H = G.subgraph(s)
        conn[s] = nx.node_connectivity(H) if nx.is_connected(H) else 0
    out = {}
    for k in range(1, max(conn.values(), default=0) + 1):
        good = [s for s in subsets if conn[s] >= k]
        out[k] = sorted(sorted(s) for s in good if not any(s < t for t in good))
    return out


def joined(m, block, links):
    """Two copies of block (nodes 0..m-1 and m..2m-1) and one connector node per link."""
    G = nx.union(block, nx.relabel_nodes(block, {i: i + m for i in range(m)}))
    for j, (a, b) in enumerate(links):
        G.add_edges_from([(2 * m + j, x) for x in a] + [(2 * m + j, m + y) for y in b])
    return G


def levels(result):
    return {k: sorted(sorted(c) for c in comps) for k, comps in result.items()}


class KComponentsEdgeTests(unittest.TestCase):
    def test_matches_brute_force_on_small_graphs(self):
        rng = random.Random(4)
        for i in range(40):
            G = nx.gnp_random_graph(rng.randrange(3, 8), rng.choice([.4, .6, .8]), seed=rng.randrange(10**6))
            with self.subTest(i=i, edges=sorted(G.edges())):
                self.assertEqual(levels(nx.k_components(G)), brute(G))

    def test_icosahedra_joined_through_other_connectors(self):
        ico = nx.icosahedral_graph()
        for links in ([((0, 1, 2), (3, 4)), ((3, 4, 5), (5, 6)), ((6, 7, 8), (7, 8)), ((9, 10), (10, 11))],
                      [((0, 1, 2), (0, 1)), ((3, 4, 5), (2, 3)), ((6, 7, 8), (4, 5)), ((9, 10), (8, 9)),
                       ((11, 0), (10, 11))]):
            G = joined(12, ico, links)
            self.assertEqual(nx.node_connectivity(G), 4)
            result = levels(nx.k_components(G))
            with self.subTest(links=links):
                self.assertEqual(result[5], [list(range(12)), list(range(12, 24))])
                for k in range(1, 5):
                    self.assertEqual(result[k], [sorted(G)])

    def test_every_reported_component_meets_the_definition(self):
        G = joined(12, nx.icosahedral_graph(), [((0, 1, 2), (0, 1)), ((3, 4, 5), (2, 3)), ((6, 7, 8), (4, 5))])
        G.add_edges_from([(40, 0), (40, 1), (41, 40), (41, 2)])
        result = nx.k_components(G)
        for k, comps in result.items():
            for c in comps:
                self.assertGreaterEqual(nx.node_connectivity(G.subgraph(c)), k, (k, sorted(c)))
            for a, b in itertools.permutations(comps, 2):
                self.assertFalse(set(a) < set(b), (k, sorted(a), sorted(b)))

    def test_highly_connected_random_graphs_are_one_component_per_level(self):
        for seed in (3, 17, 41):
            G = nx.gnp_random_graph(14, 0.7, seed=seed)
            c = nx.node_connectivity(G)
            result = levels(nx.k_components(G))
            for k in range(1, c + 1):
                self.assertEqual(result[k], [sorted(G)], (seed, k))


if __name__ == '__main__':
    unittest.main()
