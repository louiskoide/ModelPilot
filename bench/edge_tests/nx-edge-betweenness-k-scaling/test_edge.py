"""Edge cases for sampled edge betweenness: brute-force path counts from the sampled sources, both graph kinds."""
import random
import unittest

import networkx as nx


def raw_edge_counts(G, sources):
    """Sum over ordered pairs (s, t), s in sources, t != s, of the share of s-t shortest paths using each edge."""
    counts = {tuple(e): 0.0 for e in G.edges()}
    for s in sources:
        for t in G:
            if t == s or not nx.has_path(G, s, t):
                continue
            paths = list(nx.all_shortest_paths(G, s, t))
            for p in paths:
                for a, b in zip(p, p[1:]):
                    key = (a, b) if (a, b) in counts else (b, a)
                    counts[key] += 1 / len(paths)
    return counts


class EdgeBetweennessKEdgeTests(unittest.TestCase):
    def check(self, G, k, seed):
        n = len(G)
        sources = random.Random(seed).sample(list(G), k)  # the sample the function draws for this seed
        raw = raw_edge_counts(G, sources)
        half = 1 if G.is_directed() else 0.5
        for normalized, expect in ((False, {e: c * n / k * half for e, c in raw.items()}),
                                   (True, {e: c / (k * (n - 1)) for e, c in raw.items()})):
            got = nx.edge_betweenness_centrality(G, k=k, seed=seed, normalized=normalized)
            self.assertEqual(set(got), set(expect))
            for e in expect:
                self.assertAlmostEqual(got[e], expect[e], places=9, msg=(normalized, e, sources))

    def test_sampled_sources_on_random_graphs(self):
        rng = random.Random(14)
        for i in range(20):
            directed = i % 2 == 1
            G = nx.gnp_random_graph(rng.randrange(4, 9), .45, seed=rng.randrange(10**6), directed=directed)
            k = rng.randrange(1, len(G))
            with self.subTest(i=i, k=k, directed=directed):
                self.check(G, k, seed=rng.randrange(1000))

    def test_k_equal_to_n_matches_the_full_computation(self):
        G = nx.karate_club_graph()
        full = nx.edge_betweenness_centrality(G)
        sampled = nx.edge_betweenness_centrality(G, k=len(G), seed=1)
        for e in full:
            self.assertAlmostEqual(full[e], sampled[e], places=9)
        full = nx.edge_betweenness_centrality(G, normalized=False)
        sampled = nx.edge_betweenness_centrality(G, k=len(G), seed=1, normalized=False)
        for e in full:
            self.assertAlmostEqual(full[e], sampled[e], places=9)

    def test_node_betweenness_with_k_is_unchanged(self):
        G = nx.path_graph(5)
        b = nx.betweenness_centrality(G, k=5, seed=3)
        full = nx.betweenness_centrality(G)
        for v in G:
            self.assertAlmostEqual(b[v], full[v], places=9)

    def test_subset_with_every_node_matches_the_full_computation(self):
        for G in (nx.cycle_graph(7), nx.gnp_random_graph(9, .4, seed=5, directed=True)):
            node = nx.betweenness_centrality_subset(G, sources=list(G), targets=list(G), normalized=True)
            edge = nx.edge_betweenness_centrality_subset(G, sources=list(G), targets=list(G), normalized=True)
            for v, value in nx.betweenness_centrality(G).items():
                self.assertAlmostEqual(node[v], value, places=9)
            for e, value in nx.edge_betweenness_centrality(G).items():
                self.assertAlmostEqual(edge[e], value, places=9)


if __name__ == '__main__':
    unittest.main()
