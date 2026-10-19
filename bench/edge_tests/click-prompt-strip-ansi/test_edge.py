"""Edge cases for styled prompts: the destination stream decides (stdout or stderr), hidden, choice and confirmation prompts."""
import io
import sys
import unittest
from unittest import mock

import click
from click import termui
from click.testing import CliRunner

STYLED = click.style('Name', fg='green', bold=True)


class TTY(io.StringIO):
    def isatty(self):
        return True


class Pipe(io.StringIO):
    def isatty(self):
        return False


def written_prompt(call, stdout, stderr, answer='x'):
    """Run call with the given streams, the prompt function writing its prompt to sys.stdout as input() does;
    return what reached each stream, however the prompt got there."""

    def visible(text=''):
        sys.stdout.write(text)
        return answer

    with mock.patch.object(termui, 'visible_prompt_func', visible), \
            mock.patch.object(sys, 'stdout', stdout), mock.patch.object(sys, 'stderr', stderr):
        call()
    return stdout.getvalue(), stderr.getvalue()


class PromptAnsiEdgeTests(unittest.TestCase):
    def test_outside_the_runner_the_destination_stream_decides(self):
        for name, call in (('prompt', lambda err: click.prompt(STYLED, err=err)),
                           ('confirm', lambda err: click.confirm(STYLED, err=err))):
            answer = 'y' if name == 'confirm' else 'x'
            cases = [
                (False, TTY(), Pipe(), True),   # prompt on stdout, a terminal: keep the codes
                (False, Pipe(), TTY(), False),  # prompt on stdout, a pipe: strip them
                (True, Pipe(), TTY(), True),    # prompt on stderr, a terminal: keep
                (True, TTY(), Pipe(), False),   # prompt on stderr, a pipe: strip
            ]
            for err, stdout, stderr, keep in cases:
                with self.subTest(call=name, err=err, keep=keep):
                    out, errout = written_prompt(lambda: call(err), stdout, stderr, answer)
                    shown, other = (errout, out) if err else (out, errout)
                    self.assertEqual('\x1b[' in shown, keep, repr(shown))
                    self.assertTrue(click.unstyle(shown).startswith('Name'), repr(shown))
                    self.assertNotIn('Name', other)

    def run_both(self, call, user_input):
        @click.command()
        def cli():
            call()

        plain = CliRunner().invoke(cli, input=user_input, color=False)
        colored = CliRunner().invoke(cli, input=user_input, color=True)
        self.assertIsNone(plain.exception, plain.output)
        self.assertIsNone(colored.exception, colored.output)
        return plain.output, colored.output

    def test_runner_prompt_kinds(self):
        calls = {
            'hidden': (lambda: click.prompt(STYLED, hide_input=True), 'secret\n'),
            'choice': (lambda: click.prompt(STYLED, type=click.Choice(['a', 'b']), default='a'), 'b\n'),
            'confirmation': (lambda: click.prompt(STYLED, confirmation_prompt=click.style('Again', fg='red')),
                             'v\nv\n'),
            'confirm default': (lambda: click.confirm(STYLED, default=True), '\n'),
            'stderr prompt': (lambda: click.prompt(STYLED, err=True), 'v\n'),
        }
        for name, (call, user_input) in calls.items():
            with self.subTest(kind=name):
                plain, colored = self.run_both(call, user_input)
                self.assertNotIn('\x1b', plain)
                self.assertIn('\x1b[', colored)
                self.assertEqual(click.unstyle(colored), plain)
                self.assertIn('Name', plain)

    def test_runner_confirmation_prompt_text(self):
        plain, _ = self.run_both(lambda: click.prompt(STYLED, confirmation_prompt=click.style('Again', fg='red')),
                                 'v\nv\n')
        self.assertEqual(plain, 'Name: v\nAgain: v\n')


if __name__ == '__main__':
    unittest.main()
