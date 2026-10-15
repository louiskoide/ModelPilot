"""modelpilot.doctor, the $0 preflight (October 15, overnight cloud run), on temporary directories: each check's pass,
warn and fail, credentials named but never shown, nothing written, and the exit status. No client, no network."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from modelpilot import doctor

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'sk-ant-doctor-test-value-that-must-not-print'


def script(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\n' + text + '\n')
    path.chmod(0o755)
    return path


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def snapshot(root):
    return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns, p.stat().st_size)
                  for p in Path(root).rglob('*') if p.is_file() and '.git' not in p.parts)


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root/'bench').mkdir()
        shutil.copy(ROOT/'bench/environment.json', self.root/'bench/environment.json')
        self.pinned = json.loads((self.root/'bench/environment.json').read_text())['claude_code']
        shutil.copytree(ROOT/'configs', self.root/'configs')

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, *dirs, **extra):
        return dict({'PATH': os.pathsep.join([*map(str, dirs), '/usr/bin', '/bin'])}, **extra)

    def test_python(self):
        self.assertEqual(doctor.python_check((3, 12, 3), '/x/python')['status'], 'pass')
        self.assertEqual(doctor.python_check((3, 9, 18), '/x/python')['status'], 'fail')

    def test_the_first_client_on_path_must_be_the_pinned_one_from_work(self):
        pinned = script(self.root/'work/claude-client/node_modules/.bin/claude', f'echo "{self.pinned} (Claude Code)"')
        self.assertEqual(doctor.client_check(self.root, self.env(pinned.parent))['status'], 'pass')
        other = script(self.root/'elsewhere/claude', f'echo "{self.pinned} (Claude Code)"')
        self.assertEqual(doctor.client_check(self.root, self.env(other.parent, pinned.parent))['status'], 'warn')
        newer = script(self.root/'newer/claude', 'echo "2.1.300 (Claude Code)"')
        failed = doctor.client_check(self.root, self.env(newer.parent, pinned.parent))
        self.assertEqual(failed['status'], 'fail')
        self.assertIn('2.1.300', failed['detail'])
        self.assertEqual(doctor.client_check(self.root, self.env())['status'], 'fail')  # none on PATH

    def test_the_client_is_asked_its_version_with_an_empty_home_and_nothing_else(self):
        probe = script(self.root/'probe/claude', f'env > "{self.root}/seen.txt"; echo "{self.pinned} (Claude Code)"')
        doctor.client_check(self.root, self.env(probe.parent, ANTHROPIC_API_KEY=SECRET))
        seen = dict(line.split('=', 1) for line in (self.root/'seen.txt').read_text().splitlines() if '=' in line)
        self.assertNotIn(SECRET, (self.root/'seen.txt').read_text())
        self.assertNotIn('ANTHROPIC_API_KEY', seen)
        self.assertEqual(os.listdir(seen['HOME']) if os.path.exists(seen['HOME']) else [], [])

    def test_the_grader_venv_matches_its_pins_and_is_locked(self):
        self.assertEqual(doctor.venv_check(self.root)[0]['status'], 'fail')  # none
        packages = json.loads((self.root/'bench/environment.json').read_text())['packages']
        python = self.root/'work/bench/py312/bin/python'
        for writable, extra, statuses in (([], [], ('pass', 'pass')), (['/site'], [], ('pass', 'warn')),
                                          ([], ['requests==2.0'], ('fail', 'pass'))):
            with self.subTest(writable=writable, extra=extra):
                report = {'python': '3.12.3', 'packages': packages + extra, 'writable': writable}
                script(python, f"echo '{json.dumps(report)}'")
                self.assertEqual(tuple(c['status'] for c in doctor.venv_check(self.root)), statuses)

    def test_the_final_split_lock(self):
        tasks = self.root/'bench/tasks'
        spec = {'id': 'synthetic-final', 'repo': 'r', 'instruction': 'x'}
        (tasks/'synthetic-final').mkdir(parents=True)
        (tasks/'synthetic-final'/'task.json').write_text(json.dumps(spec))
        from modelpilot.bench_tasks import spec_hash
        (self.root/'bench/splits.json').write_text(json.dumps({'tuning': {}, 'final': {'synthetic-final': spec_hash(spec)}}))
        self.assertEqual(doctor.lock_check(self.root)['status'], 'pass')
        (tasks/'synthetic-final'/'task.json').write_text(json.dumps(dict(spec, instruction='edited')))
        changed = doctor.lock_check(self.root)
        self.assertEqual((changed['status'], changed['detail']), ('fail', '1 final task specs changed since the lock'))

    def test_the_jev_checkouts(self):
        self.assertEqual([c['status'] for c in doctor.jev_checks(self.root)], ['warn', 'warn'])  # neither there
        baseline = self.root/'work/jev-router-baseline'
        baseline.mkdir(parents=True)
        git(baseline, 'init', '-q')
        (baseline/'a.txt').write_text('a\n')
        git(baseline, 'add', 'a.txt')
        git(baseline, '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '-m', 'pin')
        pin = json.loads((self.root/'configs/jev-baseline.json').read_text())
        (self.root/'configs/jev-baseline.json').write_text(json.dumps(dict(pin, commit=git(baseline, 'rev-parse', 'HEAD'))))
        script(self.root/'work/jev-router-compat/src/proxy.mjs', '')
        self.assertEqual([c['status'] for c in doctor.jev_checks(self.root)], ['pass', 'pass'])
        (baseline/'a.txt').write_text('edited\n')
        self.assertEqual(doctor.jev_checks(self.root)[0]['status'], 'fail')

    def test_runs_and_work_ignored_and_the_tree_clean(self):
        git(self.root, 'init', '-q')
        (self.root/'.gitignore').write_text('runs/\nwork/\n')
        git(self.root, 'add', '-A')
        git(self.root, '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '-m', 'x')
        self.assertEqual([c['status'] for c in doctor.git_checks(self.root)], ['pass', 'pass'])
        (self.root/'new.py').write_text('x = 1\n')
        self.assertEqual([c['status'] for c in doctor.git_checks(self.root)], ['pass', 'warn'])
        (self.root/'.gitignore').write_text('work/\n')
        self.assertEqual(doctor.git_checks(self.root)[0]['status'], 'fail')

    def test_every_model_an_arm_or_the_policy_can_send_has_rates(self):
        self.assertEqual(doctor.rates_check(ROOT)['status'], 'pass')
        self.assertIn('claude-sonnet-5-5', doctor.served_models())
        self.assertNotIn('jev-router', doctor.served_models())  # the Jev sentinel is never sent to the API
        with mock.patch.object(doctor, 'served_models', lambda: ['claude-unknown-9']):
            self.assertEqual(doctor.rates_check(ROOT)['status'], 'fail')
        (self.root/'configs/m0.json').write_text('{not json')
        self.assertEqual(doctor.rates_check(self.root)['status'], 'fail')

    def test_credentials_are_named_present_or_absent_never_shown(self):
        results = doctor.credential_checks({'ANTHROPIC_API_KEY': SECRET, 'ANTHROPIC_BASE_URL': 'http://x'})
        text = json.dumps(results)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(str(len(SECRET)), text)
        details = {r['check']: r['detail'] for r in results}
        self.assertEqual((details['credential ANTHROPIC_API_KEY'], details['credential CLAUDE_CODE_OAUTH_TOKEN']),
                         ('present', 'absent'))
        self.assertEqual([r['status'] for r in results if r['check'] == 'ANTHROPIC_BASE_URL'], ['warn'])

    def test_it_writes_nothing_and_exits_1_on_a_failure(self):
        before = snapshot(self.root)
        results = doctor.checks(self.root, self.env(ANTHROPIC_API_KEY=SECRET))
        self.assertEqual(snapshot(self.root), before)
        self.assertTrue(any(r['status'] == 'fail' for r in results))  # no client, no venv, no git here
        out = io.StringIO()
        with mock.patch.object(doctor, 'checks', lambda: results), contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as stop:
                doctor.main(['--json'])
        self.assertEqual(stop.exception.code, 1)
        printed = json.loads(out.getvalue())
        self.assertEqual(printed['failed'], sum(r['status'] == 'fail' for r in results))
        self.assertNotIn(SECRET, out.getvalue())
        with mock.patch.object(doctor, 'checks', lambda: [doctor.check('x', 'warn', 'y')]), \
                contextlib.redirect_stdout(io.StringIO()):
            doctor.main([])  # warnings alone exit 0


if __name__ == '__main__':
    unittest.main()
