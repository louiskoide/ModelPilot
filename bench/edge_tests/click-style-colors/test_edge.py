"""Edge cases for style() colors: every 256-color index, boundaries, combined fg and bg, secho, more invalid values."""
import unittest

import click
from click.testing import CliRunner


class StyleColorsEdgeTests(unittest.TestCase):
    def test_every_palette_index(self):
        for i in range(256):
            with self.subTest(index=i):
                self.assertEqual(click.style('x', fg=i), f'\x1b[38;5;{i}mx\x1b[0m')
                self.assertEqual(click.style('x', bg=i), f'\x1b[48;5;{i}mx\x1b[0m')

    def test_zero_together_with_other_styles(self):
        self.assertEqual(click.style('x', fg=0, bg=0), '\x1b[38;5;0m\x1b[48;5;0mx\x1b[0m')
        self.assertEqual(click.style('x', fg=0, bg='red', bold=True), '\x1b[38;5;0m\x1b[41m\x1b[1mx\x1b[0m')
        self.assertEqual(click.style('x', fg=(0, 0, 0), bg=[255, 255, 255]),
                         '\x1b[38;2;0;0;0m\x1b[48;2;255;255;255mx\x1b[0m')
        self.assertEqual(click.style('x', fg=None, bg=None), 'x\x1b[0m')

    def test_more_invalid_values(self):
        invalid = ['Red', ' red', 'red ', 'bright-red', 'brightred', 'gray', -256, -1, 256, 1000, 2**64,
                   (255, 255, 256), [0, 0, -1], (1, 2), [1, 2, 3, 4], (1, 2, '3'), (1, 2, 3.0), (1, 2, None),
                   (1, (2,), 3), {1, 2, 3}, {'r': 1}, 1.5, b'\x00', True, (0, False, 0)]
        for param in ('fg', 'bg'):
            for value in invalid:
                with self.subTest(param=param, value=value):
                    with self.assertRaises(ValueError) as caught:
                        click.style('x', **{param: value})
                    self.assertIn('Unknown color', str(caught.exception))

    def test_secho_uses_the_same_rules(self):
        @click.command()
        def cli():
            click.secho('zero', fg=0)
            click.secho('black bg', bg=0)

        result = CliRunner().invoke(cli, color=True)
        self.assertEqual(result.output, '\x1b[38;5;0mzero\x1b[0m\n\x1b[48;5;0mblack bg\x1b[0m\n')
        with self.assertRaises(ValueError):
            click.secho('x', fg=300)
        with self.assertRaises(ValueError):
            click.secho('x', bg=False)

    def test_named_colors_for_both(self):
        names = ['black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white', 'reset']
        names += ['bright_' + n for n in names[:8]]
        for name in names:
            with self.subTest(name=name):
                fg, bg = click.style('x', fg=name), click.style('x', bg=name)
                self.assertTrue(fg.startswith('\x1b[') and bg.startswith('\x1b['))
                self.assertEqual(int(bg[2:bg.index('m')]), int(fg[2:fg.index('m')]) + 10)
                self.assertEqual(click.unstyle(fg), 'x')


if __name__ == '__main__':
    unittest.main()
