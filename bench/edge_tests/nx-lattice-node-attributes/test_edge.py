"""Edge cases for lattice attributes: sizes, periodic and positions for both generators, structure unchanged."""
import unittest

import networkx as nx

GENERATORS = (nx.hexagonal_lattice_graph, nx.triangular_lattice_graph)


class LatticeAttributesEdgeTests(unittest.TestCase):
    def test_no_attributes_without_positions(self):
        for gen in GENERATORS:
            for m, n, periodic in ((2, 2, False), (4, 6, True), (6, 6, True), (5, 3, False)):
                with self.subTest(gen=gen.__name__, m=m, n=n, periodic=periodic):
                    G = gen(m, n, periodic=periodic, with_positions=False)
                    self.assertTrue(all(d == {} for _, d in G.nodes(data=True)))
                    self.assertTrue(all(d == {} for _, _, d in G.edges(data=True)))

    def test_positions_only_and_always_when_asked(self):
        for gen in GENERATORS:
            for periodic in (False, True):
                G = gen(4, 6, periodic=periodic, with_positions=True)
                with self.subTest(gen=gen.__name__, periodic=periodic):
                    self.assertTrue(all(set(d) == {'pos'} for _, d in G.nodes(data=True)))
                    self.assertTrue(all(d == {} for _, _, d in G.edges(data=True)))

    def test_structure_is_the_same_either_way(self):
        for gen in GENERATORS:
            for periodic in (False, True):
                a = gen(4, 6, periodic=periodic, with_positions=True)
                b = gen(4, 6, periodic=periodic, with_positions=False)
                self.assertEqual(sorted(a.nodes()), sorted(b.nodes()))
                self.assertEqual({frozenset(e) for e in a.edges()}, {frozenset(e) for e in b.edges()})

    def test_periodic_hexagonal_lattice_is_cubic(self):
        G = nx.hexagonal_lattice_graph(4, 6, periodic=True, with_positions=False)
        self.assertEqual({d for _, d in G.degree()}, {3})
        T = nx.triangular_lattice_graph(4, 6, periodic=True, with_positions=False)
        self.assertEqual({d for _, d in T.degree()}, {6})


if __name__ == '__main__':
    unittest.main()
