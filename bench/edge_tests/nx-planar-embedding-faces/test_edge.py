"""Edge cases for PlanarEmbedding.faces(): half-edges once each, Euler per component, walks that close."""
from collections import Counter
import random
import unittest

import networkx as nx


def half_edges(embedding):
    return Counter((u, v) for u in embedding for v in embedding.neighbors_cw_order(u))


def face_half_edges(faces):
    return Counter((f[i], f[(i + 1) % len(f)]) for f in faces for i in range(len(f)))


class PlanarFacesEdgeTests(unittest.TestCase):
    def check(self, G):
        planar, embedding = nx.check_planarity(G)
        self.assertTrue(planar)
        faces = list(embedding.faces())
        self.assertEqual(face_half_edges(faces), half_edges(embedding))
        expected = 0
        for comp in nx.connected_components(G):
            H = G.subgraph(comp)
            if H.number_of_edges():
                expected += H.number_of_edges() - H.number_of_nodes() + 2
        self.assertEqual(len(faces), expected)
        for f in faces:
            walk = embedding.traverse_face(f[0], f[1])
            self.assertEqual(Counter(face_half_edges([walk])), Counter(face_half_edges([f])))
        return faces

    def test_random_planar_graphs(self):
        rng = random.Random(15)
        done = 0
        while done < 30:
            G = nx.gnm_random_graph(rng.randrange(2, 12), rng.randrange(1, 20), seed=rng.randrange(10**6))
            if nx.check_planarity(G)[0]:
                with self.subTest(edges=sorted(G.edges())):
                    self.check(G)
                done += 1

    def test_trees_and_stars_have_one_face_per_component(self):
        for G in (nx.star_graph(5), nx.balanced_tree(2, 3), nx.disjoint_union(nx.path_graph(3), nx.star_graph(3))):
            faces = self.check(G)
            self.assertTrue(all(len(f) == 2 * G.subgraph(nx.node_connected_component(G, f[0])).number_of_edges()
                                for f in faces))

    def test_grid_and_wheel(self):
        self.assertEqual(sorted(len(f) for f in self.check(nx.grid_2d_graph(3, 3))), [4, 4, 4, 4, 8])
        self.assertEqual(sorted(len(f) for f in self.check(nx.wheel_graph(6))), [3, 3, 3, 3, 3, 5])

    def test_is_a_generator_and_empty_embedding(self):
        _, embedding = nx.check_planarity(nx.cycle_graph(5))
        gen = embedding.faces()
        self.assertEqual(len(next(gen)), 5)
        self.assertEqual(list(nx.PlanarEmbedding().faces()), [])


if __name__ == '__main__':
    unittest.main()
