"""python3 -m modelpilot on the command registry (October 15, overnight cloud run): the listing, a run that passes its
arguments through, an unknown name, and pyproject.toml's description and scripts. $0: what runs is $0 commands."""
import contextlib
import importlib
import io
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from modelpilot import __main__ as cli
from modelpilot import commands

ROOT = Path(__file__).resolve().parents[1]


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def pyproject():
    """pyproject.toml's [project] description and [project.scripts], without tomllib (Python 3.10 has none)."""
    text = (ROOT/'pyproject.toml').read_text()
    description = re.search(r'^description = "(.*)"$', text, re.M).group(1)
    section = text.split('[project.scripts]', 1)[1].split('\n[', 1)[0]
    return description, dict(re.findall(r'^([\w-]+) = "([\w.:]+)"$', section, re.M))


class ListingTests(unittest.TestCase):
    def test_no_arguments_lists_every_command_and_marks_the_paid_ones(self):
        code, out, _ = run()
        self.assertEqual(code, 0)
        for row in commands.COMMANDS:
            line = next(line for line in out.splitlines() if line.split()[:1] == [row.name])
            self.assertIn(row.summary, line)
            self.assertEqual(line.endswith('[paid: --live]'), row.paid, row.name)
        self.assertEqual(out.count('[paid: --live]'), len(commands.paid()))
        self.assertEqual(run('--help')[1], out)

    def test_an_unknown_command_exits_2_and_runs_nothing(self):
        with mock.patch.object(commands, 'load', lambda name: self.fail('loaded')):
            code, out, err = run('no_such_command', '--live')
        self.assertEqual((code, out), (2, ''))
        self.assertIn("unknown command 'no_such_command'", err)


class RunTests(unittest.TestCase):
    def test_a_name_runs_that_main_with_the_rest_of_the_arguments(self):
        seen = []
        fake = type('Module', (), {'main': staticmethod(lambda: seen.append(list(sys.argv)))})
        argv = list(sys.argv)
        with mock.patch.object(commands, 'load', lambda name: fake if name == 'bench_report' else self.fail(name)):
            self.assertEqual(run('bench_report', 'runs/x', '--json')[0], 0)
        self.assertEqual(seen, [['python3 -m modelpilot bench_report', 'runs/x', '--json']])
        self.assertEqual(sys.argv, argv)  # restored

    def test_a_failing_command_exits_as_it_would_alone(self):
        fake = type('Module', (), {'main': staticmethod(lambda: sys.exit(1))})
        with mock.patch.object(commands, 'load', lambda name: fake), self.assertRaises(SystemExit) as raised:
            run('doctor')
        self.assertEqual(raised.exception.code, 1)

    def test_python_dash_m_modelpilot_runs_a_real_command(self):
        """A $0 command end to end, in a child process: governor's offline demo writes its report."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'demo.json'
            result = subprocess.run([sys.executable, '-m', 'modelpilot', 'governor', '--out', str(out)], cwd=ROOT,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertTrue(out.exists())
            listing = subprocess.run([sys.executable, '-m', 'modelpilot'], cwd=ROOT, capture_output=True, text=True,
                                     timeout=60)
            self.assertEqual((listing.returncode, listing.stdout), (0, cli.listing() + '\n'))


class PyprojectTests(unittest.TestCase):
    def test_the_description_is_the_readme_opening(self):
        description, _ = pyproject()
        opening = (ROOT/'README.md').read_text().split('\n\n')[1].split('. ')[0]  # its first sentence
        opening = re.sub(r'\[([^]]+)\]\([^)]+\)', r'\1', opening).removeprefix('ModelPilot is a ')
        self.assertEqual(description, 'A ' + opening)

    def test_every_script_names_a_registered_main(self):
        _, scripts = pyproject()
        self.assertEqual(scripts['modelpilot-doctor'], 'modelpilot.doctor:main')
        self.assertEqual(scripts['modelpilot-timeline'], 'modelpilot.timeline:main')
        for name, target in scripts.items():
            module, function = target.split(':')
            self.assertTrue(callable(getattr(importlib.import_module(module), function)), name)
            self.assertIsNotNone(commands.find(module.split('.', 1)[1]), name)


if __name__ == '__main__':
    unittest.main()
