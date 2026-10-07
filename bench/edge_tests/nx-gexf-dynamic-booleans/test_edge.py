"""Edge cases for dynamic GEXF booleans: nodes and edges, mixed with other types, and round trips."""
import io
import math
import unittest

import networkx as nx


def write(G):
    buf = io.BytesIO()
    nx.write_gexf(G, buf)
    return buf.getvalue().decode()


def read(text):
    return nx.read_gexf(io.BytesIO(text.encode()))


class GexfDynamicBooleanEdgeTests(unittest.TestCase):
    def test_node_and_edge_booleans_are_lowercase_and_round_trip(self):
        G = nx.Graph(mode='dynamic')
        G.add_node('a', ok=[(True, 0, 2), (False, 2, 4)])
        G.add_node('b', ok=[(False, 0, 4)])
        G.add_edge('a', 'b', live=[(True, 1, 3)])
        text = write(G)
        self.assertNotIn('value="True"', text)
        self.assertNotIn('value="False"', text)
        self.assertIn('value="true"', text)
        self.assertIn('value="false"', text)
        H = read(text)
        self.assertEqual(H.nodes['a']['ok'], [(True, 0, 2), (False, 2, 4)])
        self.assertEqual(H.nodes['b']['ok'], [(False, 0, 4)])
        self.assertEqual(H.edges['a', 'b']['live'], [(True, 1, 3)])

    def test_static_booleans_unchanged(self):
        G = nx.Graph()
        G.add_node('x', flag=True)
        text = write(G)
        self.assertIn('value="true"', text)
        self.assertIs(read(text).nodes['x']['flag'], True)

    def test_float_specials_still_spelled_as_before(self):
        G = nx.Graph(mode='dynamic')
        G.add_node('f', x=[(math.inf, 0, 1), (-math.inf, 1, 2), (1.5, 2, 3)])
        text = write(G)
        self.assertIn('value="INF"', text)
        self.assertIn('value="-INF"', text)
        self.assertIn('value="1.5"', text)


if __name__ == '__main__':
    unittest.main()
