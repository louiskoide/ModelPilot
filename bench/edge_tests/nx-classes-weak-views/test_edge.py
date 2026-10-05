"""Edge cases for weakly held graphs in cached views: every view, every class, temporaries, copies, pickles."""
import copy
import gc
import pickle
import unittest
import weakref

import networkx as nx

CLASSES = [nx.Graph, nx.DiGraph, nx.MultiGraph, nx.MultiDiGraph]
EDGES = [(0, 1), (1, 2), (2, 0), (2, 3)]


class WeakViewEdgeTests(unittest.TestCase):
    def test_each_view_alone_leaves_the_graph_to_reference_counting(self):
        names = ['edges', 'degree', 'in_edges', 'out_edges', 'in_degree', 'out_degree', 'nodes', 'adj']
        gc.disable()
        try:
            for cls in CLASSES:
                for name in names:
                    G = cls(EDGES)
                    if not hasattr(G, name):
                        continue
                    getattr(G, name)
                    view = getattr(G, name)  # read twice: the cached one
                    del view
                    ref = weakref.ref(G)
                    del G
                    with self.subTest(cls=cls.__name__, view=name):
                        self.assertIsNone(ref())
        finally:
            gc.enable()

    def test_views_of_temporary_directed_graphs_answer_calls(self):
        for cls in (nx.DiGraph, nx.MultiDiGraph):
            G = cls(EDGES)
            for name in ('in_degree', 'out_degree', 'degree'):
                with self.subTest(cls=cls.__name__, view=name):
                    self.assertEqual(dict(getattr(cls(EDGES), name)(weight='w')), dict(getattr(G, name)(weight='w')))
                    self.assertEqual(dict(getattr(cls(EDGES), name)([1, 2])), dict(getattr(G, name)([1, 2])))
            for name in ('in_edges', 'out_edges', 'edges'):
                with self.subTest(cls=cls.__name__, view=name):
                    self.assertEqual(list(getattr(cls(EDGES), name)([2])), list(getattr(G, name)([2])))

    def test_views_follow_changes_to_a_live_graph(self):
        for cls in CLASSES:
            G = cls(EDGES)
            ev, dv = G.edges, G.degree
            G.add_edge(3, 4)
            with self.subTest(cls=cls.__name__):
                self.assertIs(ev._graph, G)
                self.assertEqual(len(ev), len(EDGES) + 1)
                self.assertEqual(dv[4], 1)
                self.assertIs(G.edges, ev)  # still cached

    def test_deepcopy_and_pickle_give_each_copy_its_own_views(self):
        for cls in CLASSES:
            G = cls(EDGES)
            _ = (G.edges, G.degree)
            for D in (copy.deepcopy(G), pickle.loads(pickle.dumps(G, -1))):
                with self.subTest(cls=cls.__name__):
                    self.assertIs(D.edges._graph, D)
                    self.assertIs(D.degree._graph, D)
                    D.add_edge(5, 6)
                    self.assertEqual(D.degree(5), 1)
                    self.assertNotIn(5, G)

    def test_pickled_views_of_every_class_answer_calls(self):
        for cls in CLASSES:
            G = cls(EDGES)
            dv = pickle.loads(pickle.dumps(cls(EDGES).degree, -1))
            ev = pickle.loads(pickle.dumps(cls(EDGES).edges, -1))
            with self.subTest(cls=cls.__name__):
                self.assertEqual(dict(dv([1, 2], weight='w')), dict(G.degree([1, 2], weight='w')))
                self.assertEqual(sorted(ev(data=True, nbunch=[2])), sorted(G.edges(data=True, nbunch=[2])))
                pickle.loads(pickle.dumps(dv, -1))

    def test_nbunch_errors_are_unchanged(self):
        G = nx.Graph(EDGES)
        with self.assertRaisesRegex(nx.NetworkXError, 'Node 9 is not in the graph'):
            list(G.nbunch_iter(9))
        with self.assertRaisesRegex(nx.NetworkXError, r'Node \[1\] in sequence nbunch is not a valid node'):
            list(G.nbunch_iter([0, [1]]))
        self.assertEqual(list(G.nbunch_iter([0, 99])), [0])  # a missing node in a sequence is skipped
        self.assertEqual(list(G.edges([0, 99])), [(0, 1), (0, 2)])
        with self.assertRaisesRegex(nx.NetworkXError, 'Node 9 is not in the graph'):
            list(G.edges(9))


if __name__ == '__main__':
    unittest.main()
