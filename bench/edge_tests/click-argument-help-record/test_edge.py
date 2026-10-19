"""Edge cases for Argument.get_help_record: many argument shapes, every parameter of a command, help output unchanged."""
import unittest

import click
from click.testing import CliRunner


def shapes():
    yield click.Argument(['src'])
    yield click.Argument(['src'], help='')
    yield click.Argument(['src'], help='Source path')
    yield click.Argument(['files'], nargs=-1)
    yield click.Argument(['pair'], nargs=2, help='Two values')
    yield click.Argument(['name'], required=False)
    yield click.Argument(['name'], required=False, default='x', help='Name')
    yield click.Argument(['path'], metavar='PATH')
    yield click.Argument(['mode'], type=click.Choice(['a', 'b']))
    yield click.Argument(['count'], type=int, help='How many')


class ArgumentHelpRecordEdgeTests(unittest.TestCase):
    def test_always_a_metavar_and_help_pair(self):
        ctx = click.Context(click.Command('cli'))
        for arg in shapes():
            with self.subTest(name=arg.name, help=arg.help):
                record = arg.get_help_record(ctx)
                self.assertIsInstance(record, tuple)
                self.assertEqual(len(record), 2)
                self.assertEqual(record[0], arg.make_metavar(ctx))
                self.assertEqual(record[1], arg.help or '')
                self.assertIsInstance(record[1], str)

    def test_every_argument_of_a_command(self):
        @click.command()
        @click.option('--flag', is_flag=True, help='A flag')
        @click.option('--secret', hidden=True)
        @click.argument('src')
        @click.argument('dst', help='Where to')
        @click.argument('rest', nargs=-1)
        def cli(flag, secret, src, dst, rest):
            pass

        ctx = click.Context(cli)
        records = {p.name: p.get_help_record(ctx) for p in cli.get_params(ctx) if isinstance(p, click.Argument)}
        self.assertEqual(records, {'src': ('SRC', ''), 'dst': ('DST', 'Where to'), 'rest': ('[REST]...', '')})

    def test_help_output_lists_documented_and_undocumented_arguments(self):
        @click.command()
        @click.option('--secret', hidden=True)
        @click.argument('first')
        @click.argument('second', required=False)
        @click.argument('third', help='The third one')
        def cli(secret, first, second, third):
            pass

        result = CliRunner().invoke(cli, ['--help'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsNone(result.exception)
        lines = result.output.splitlines()
        start = lines.index('Positional arguments:')
        self.assertEqual([line.split() for line in lines[start + 1:start + 4]],
                         [['FIRST'], ['[SECOND]'], ['THIRD', 'The', 'third', 'one']])
        self.assertNotIn('--secret', result.output)

    def test_no_section_when_no_argument_has_help(self):
        @click.command()
        @click.argument('first')
        @click.argument('rest', nargs=-1)
        def cli(first, rest):
            pass

        result = CliRunner().invoke(cli, ['--help'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn('Positional arguments', result.output)


if __name__ == '__main__':
    unittest.main()
