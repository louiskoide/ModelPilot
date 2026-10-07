"""Edge cases for Floyd-Warshall and negative cycles: agreement with Bellman-Ford on random weighted digraphs."""
import random
import unittest

import networkx as nx

FUNCS = (nx.floyd_warshall, lambda G, **kw: nx.floyd_warshall_predecessor_and_distance(G, **kw)[1],
         lambda G, **kw: nx.floyd_warshall_tree(G, **kw)[1])


class FloydNegativeCycleEdgeTests(unittest.TestCase):
    def test_agrees_with_bellman_ford_on_random_digraphs(self):
        rng = random.Random(19)
        for i in range(60):
            G = nx.gnp_random_graph(rng.randrange(2, 9), .35, seed=rng.randrange(10**6), directed=True)
            for u, v in G.edges():
                G[u][v]['weight'] = rng.choice([-3, -1, 0, 1, 2, 4, 6])
            negative = nx.negative_edge_cycle(G)
            for f in FUNCS:
                with self.subTest(i=i, edges=sorted(G.edges(data='weight'))):
                    if negative:
                        with self.assertRaises(nx.NetworkXUnbounded):
                            f(G)
                    else:
                        dist = f(G)
                        expect = dict(nx.all_pairs_bellman_ford_path_length(G))
                        for u in G:
                            for v in G:
                                self.assertEqual(dist[u][v], expect[u].get(v, float('inf')))

    def test_negative_edge_in_undirected_graphs(self):
        for f in FUNCS:
            G = nx.path_graph(4)
            nx.set_edge_attributes(G, 1, 'weight')
            G[2][3]['weight'] = -0.5
            with self.assertRaises(nx.NetworkXUnbounded):
                f(G)
            G[2][3]['weight'] = 0  # zero is fine
            self.assertEqual(f(G)[0][3], 2)

    def test_other_weight_attribute_and_multigraph(self):
        G = nx.MultiDiGraph()
        G.add_edge(0, 1, w=2, cost=1)
        G.add_edge(1, 0, w=-3, cost=1)
        G.add_edge(1, 0, w=5, cost=1)
        for f in FUNCS:
            with self.assertRaises(nx.NetworkXUnbounded):
                f(G, weight='w')  # the cheaper parallel edge closes a negative cycle
            self.assertEqual(f(G, weight='cost')[0][1], 1)

    def test_negative_cycle_away_from_most_nodes(self):
        G = nx.path_graph(8, create_using=nx.DiGraph)
        G.add_edge(7, 6, weight=-2)
        for f in FUNCS:
            with self.assertRaises(nx.NetworkXUnbounded):
                f(G)


if __name__ == '__main__':
    unittest.main()
