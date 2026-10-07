"""Edge cases for tree centroid: the definition on random trees, the barycenter, order, labels and errors."""
import random
import unittest

import networkx as nx


def by_definition(T):
    n = len(T)
    out = set()
    for v in T:
        H = T.subgraph(set(T) - {v})
        if max((len(c) for c in nx.connected_components(H)), default=0) <= n / 2:
            out.add(v)
    return out


def random_tree(rng, n):
    T = nx.Graph()
    T.add_node(0)
    for v in range(1, n):
        T.add_edge(v, rng.randrange(v))
    return nx.relabel_nodes(T, dict(zip(range(n), rng.sample(range(n), n))))


class TreeCentroidEdgeTests(unittest.TestCase):
    def test_matches_the_definition_on_random_trees(self):
        rng = random.Random(2)
        for i in range(80):
            T = random_tree(rng, rng.randrange(1, 40))
            with self.subTest(i=i):
                found = nx.tree.centroid(T)
                self.assertEqual(len(found), len(set(found)))
                self.assertEqual(set(found), by_definition(T))

    def test_minimizes_the_total_distance(self):
        rng = random.Random(6)
        for i in range(30):
            T = random_tree(rng, rng.randrange(2, 30))
            total = {v: sum(nx.single_source_shortest_path_length(T, v).values()) for v in T}
            best = min(total.values())
            self.assertEqual(set(nx.tree.centroid(T)), {v for v, t in total.items() if t == best})

    def test_two_node_order_follows_the_first_node(self):
        self.assertEqual(nx.tree.centroid(nx.Graph([(3, 2), (2, 1), (1, 0)])), [2, 1])
        self.assertEqual(nx.tree.centroid(nx.Graph([('a', 'b')])), ['a', 'b'])

    def test_single_node_and_labels(self):
        T = nx.Graph()
        T.add_node('only')
        self.assertEqual(nx.tree.centroid(T), ['only'])
        T = nx.Graph([('hub', x) for x in 'abcde'] + [('e', 'f'), ('f', 'g')])
        self.assertEqual(nx.tree.centroid(T), ['hub'])

    def test_errors(self):
        with self.assertRaises(nx.NotATree):
            nx.tree.centroid(nx.Graph([(0, 1), (2, 3)]))  # a forest
        with self.assertRaises(nx.NetworkXPointlessConcept):
            nx.tree.centroid(nx.Graph())
        with self.assertRaises(nx.NetworkXNotImplemented):
            nx.tree.centroid(nx.DiGraph([(0, 1)]))


if __name__ == '__main__':
    unittest.main()
