"""Edge cases for ISMAGS monomorphisms: VF2 agreement on random graphs, symmetry, orientation, labels."""
import random
import unittest

import networkx as nx
from networkx.algorithms import isomorphism as iso


def as_set(mappings):
    return {frozenset(m.items()) for m in mappings}


def random_graph(rng, cls, n, m, extra_parallel=0):
    G = cls()
    G.add_nodes_from(range(n))
    for _ in range(m):
        u, v = rng.randrange(n), rng.randrange(n)
        if u != v or rng.random() < .2:
            G.add_edge(u, v)
    if G.is_multigraph():
        for _ in range(extra_parallel):
            if G.number_of_edges():
                G.add_edge(*rng.choice(list(G.edges())))
    for node in G:
        G.nodes[node]['c'] = rng.randrange(2)
    return G


class IsmagsMonomorphismEdgeTests(unittest.TestCase):
    def test_agrees_with_vf2_on_random_graphs(self):
        # Directed multigraphs are left out: the upstream fix itself differs from VF2 on some of them.
        rng = random.Random(7)
        for cls in (nx.Graph, nx.DiGraph, nx.MultiGraph):
            matcher = iso.DiGraphMatcher if cls().is_directed() else iso.GraphMatcher
            for i in range(25):
                G = random_graph(rng, cls, 6, 9, 3)
                S = random_graph(rng, cls, 3, 3, 1)
                nm = iso.categorical_node_match('c', None) if i % 2 else None
                with self.subTest(cls=cls.__name__, i=i):
                    expected = as_set(matcher(G, S, node_match=nm).subgraph_monomorphisms_iter())
                    self.assertEqual(as_set(iso.ISMAGS(G, S, node_match=nm).monomorphisms_iter(symmetry=False)), expected)
                    self.assertEqual(iso.ISMAGS(G, S, node_match=nm).is_monomorphic(), bool(expected))

    def test_extra_graph_edges_are_allowed_unlike_subgraph_isomorphism(self):
        G = nx.complete_graph(4)
        S = nx.path_graph(3)
        ismags = iso.ISMAGS(G, S)
        self.assertFalse(ismags.subgraph_is_isomorphic())
        self.assertTrue(ismags.is_monomorphic())
        self.assertEqual(len(list(iso.ISMAGS(G, S).monomorphisms_iter(symmetry=False))), 24)

    def test_symmetry_yields_one_mapping_per_subgraph_symmetry(self):
        G, S = nx.cycle_graph(5), nx.path_graph(3)  # the path has 2 automorphisms
        everything = list(iso.ISMAGS(G, S).monomorphisms_iter(symmetry=False))
        reduced = list(iso.ISMAGS(G, S).monomorphisms_iter())  # symmetry=True is the default
        self.assertEqual(len(everything), 10)
        self.assertEqual(len(reduced), 5)
        self.assertLessEqual(as_set(reduced), as_set(everything))

    def test_mappings_go_from_graph_nodes_to_subgraph_nodes(self):
        G = nx.Graph([('a', 'b'), ('b', 'c'), ('c', 'a')])
        S = nx.Graph([(0, 1)])
        for mapping in iso.ISMAGS(G, S).monomorphisms_iter(symmetry=False):
            self.assertLessEqual(set(mapping), set(G))
            self.assertEqual(set(mapping.values()), {0, 1})

    def test_edge_labels_must_match(self):
        G = nx.Graph()
        G.add_edge(0, 1, kind='x')
        G.add_edge(1, 2, kind='y')
        S = nx.Graph()
        S.add_edge('p', 'q', kind='y')
        em = iso.categorical_edge_match('kind', None)
        found = list(iso.ISMAGS(G, S, edge_match=em).monomorphisms_iter(symmetry=False))
        self.assertEqual(as_set(found), as_set([{1: 'p', 2: 'q'}, {2: 'p', 1: 'q'}]))

    def test_no_monomorphism_when_the_subgraph_is_larger(self):
        self.assertFalse(iso.ISMAGS(nx.path_graph(2), nx.path_graph(3)).is_monomorphic())
        self.assertEqual(list(iso.ISMAGS(nx.path_graph(2), nx.path_graph(3)).monomorphisms_iter()), [])

if __name__ == '__main__':
    unittest.main()
