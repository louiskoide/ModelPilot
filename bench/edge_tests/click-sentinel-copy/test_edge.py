"""Edge cases for Sentinel copies: every pickle protocol, nested structures, whole commands."""
import copy
import pickle
import unittest

import click
from click._utils import UNSET, Sentinel
from click.testing import CliRunner


class SentinelCopyEdgeTests(unittest.TestCase):
    def test_every_pickle_protocol(self):
        for member in Sentinel:
            for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
                with self.subTest(member=member.name, protocol=protocol):
                    self.assertIs(pickle.loads(pickle.dumps(member, protocol=protocol)), member)

    def test_nested_structures(self):
        members = list(Sentinel)
        data = {'list': [*members, (UNSET, [UNSET])], 'set': frozenset(members), UNSET: {'deep': [[UNSET]]}}
        copies = [copy.deepcopy(data)]
        copies += [pickle.loads(pickle.dumps(data, protocol=p)) for p in range(pickle.HIGHEST_PROTOCOL + 1)]
        for i, other in enumerate(copies):
            with self.subTest(copy=i):
                for got, member in zip(other['list'], members):
                    self.assertIs(got, member)
                self.assertIs(other['list'][-1][0], UNSET)
                self.assertIs(other['list'][-1][1][0], UNSET)
                self.assertEqual(other['set'], frozenset(members))
                self.assertIs(other[UNSET]['deep'][0][0], UNSET)
                self.assertIn(UNSET, other)

    def test_shared_references_stay_one_object(self):
        twice = [UNSET, UNSET, {'k': UNSET}]
        for dup in (copy.copy, copy.deepcopy, lambda v: pickle.loads(pickle.dumps(v))):
            with self.subTest(dup=dup):
                out = dup(twice)
                self.assertIs(out[0], UNSET)
                self.assertIs(out[1], UNSET)
                self.assertIs(out[2]['k'], UNSET)

    def test_copied_commands_keep_unset_defaults(self):
        cmd = click.Command('cli', params=[click.Option(['--name']), click.Option(['--count'], type=int),
                                           click.Argument(['src'], required=False)])
        for dup in (copy.copy, copy.deepcopy):
            with self.subTest(dup=dup):
                other = dup(cmd)
                for param in other.params:
                    self.assertIs(param.default, UNSET, param.name)
        clone = copy.deepcopy(cmd)
        result = CliRunner().invoke(clone, ['--help'])
        self.assertEqual(result.exit_code, 0, result.output)


if __name__ == '__main__':
    unittest.main()
