"""Edge cases for the weighted dominating set greedy: the stated rule, weights, and a dominating result."""
import random
import unittest

import networkx as nx
from networkx.algorithms.approximation import min_weighted_dominating_set


def greedy(G, weight=None):
    uncovered, chosen = set(G), set()
    candidates = [v for v in G]
    while uncovered:
        best, best_cost = None, None
        for v in candidates:
            if v in chosen:
                continue
            new = len(({v} | set(G[v])) & uncovered)
            if not new:
                continue
            cost = G.nodes[v].get(weight, 1) / new
            if best_cost is None or cost < best_cost:
                best, best_cost = v, cost
        chosen.add(best)
        uncovered -= {best} | set(G[best])
    return chosen


class DominatingSetGreedyEdgeTests(unittest.TestCase):
    def test_follows_the_greedy_rule_on_random_graphs(self):
        rng = random.Random(16)
        for i in range(60):
            G = nx.gnp_random_graph(rng.randrange(1, 14), rng.choice([.15, .3, .5]), seed=rng.randrange(10**6))
            with self.subTest(i=i, edges=sorted(G.edges())):
                found = min_weighted_dominating_set(G)
                self.assertTrue(nx.is_dominating_set(G, found))
                self.assertEqual(found, greedy(G))

    def test_weights(self):
        rng = random.Random(17)
        for i in range(30):
            G = nx.gnp_random_graph(10, .3, seed=rng.randrange(10**6))
            for v in G:
                G.nodes[v]['cost'] = rng.choice([1, 2, 5])
            with self.subTest(i=i):
                found = min_weighted_dominating_set(G, weight='cost')
                self.assertTrue(nx.is_dominating_set(G, found))
                self.assertEqual(found, greedy(G, 'cost'))

    def test_clique_with_tail_variants(self):
        for k in (3, 4, 6):
            G = nx.complete_graph(k)
            nx.add_path(G, [k - 1, k, k + 1])
            found = min_weighted_dominating_set(G)
            self.assertTrue(nx.is_dominating_set(G, found))
            self.assertEqual(len(found), 2)

    def test_isolated_nodes_and_empty_graph(self):
        G = nx.empty_graph(3)
        self.assertEqual(min_weighted_dominating_set(G), {0, 1, 2})
        self.assertEqual(min_weighted_dominating_set(nx.Graph()), set())


if __name__ == '__main__':
    unittest.main()
