"""Paid modules send nothing without --live (October 15, overnight cloud run). Each paid command in the registry
(modelpilot.commands) runs its plan-only arguments in this process with every outbound path trapped: sockets to
anything but loopback, http.client and urllib connections, launches of claude, node or npm, and credential prompts.
Nothing may be attempted, the run must end cleanly (exit 0, or a refusal the registry lists) with a plan, and no run
directory may appear under runs/. A --live run without its required stop threshold must be refused before anything
is sent. Credentials are removed from the environment first. $0, no network.

miss_checklist and spec_tests are left out (commands.GUARD_SKIPPED): another session was editing them."""
import ast
import builtins
import contextlib
import getpass
import http.client
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request
from modelpilot import commands

ROOT = Path(__file__).resolve().parents[1]
CLIENTS = ('claude', 'node', 'npm', 'npx')
CREDENTIALS = re.compile(r'^(ANTHROPIC_.*|CLAUDE_CODE_OAUTH_TOKEN|.*_API_KEY|TYPESAFE.*|JEV_.*)$')
# What each plan prints instead of '--live' where it prints no such hint.
PLAN_MARKERS = {'cache_probe': '"live": false', 'evaluate': '"paid_calls_made": 0', 'risk_predictor': '"live": false'}


class Blocked(Exception):
    pass


def loopback(address):
    if not isinstance(address, tuple):
        return True  # a Unix socket
    host = address[0]
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host.split('%')[0]).is_loopback
    except ValueError:
        return False


class Traps:
    """Every outbound path, patched to record the attempt and raise before anything leaves the process. The fake
    client's `--version` (a shell script that prints the pinned version) is the one launch let through, matched by
    real path: bench.resolve_client launches the resolved binary, and macOS's /var is /private/var."""
    def __init__(self, fake_client):
        self.fake = os.path.realpath(fake_client)
        self.attempts, self.version_checks = [], []

    def block(self, what):
        self.attempts.append(what)
        raise Blocked(what)

    @contextlib.contextmanager
    def active(self):
        traps = self
        connect, connect_ex, popen_init = socket.socket.connect, socket.socket.connect_ex, subprocess.Popen.__init__

        def guarded_connect(sock, address):
            if not loopback(address):
                traps.block(f'socket.connect {address!r}')
            return connect(sock, address)

        def guarded_connect_ex(sock, address):
            if not loopback(address):
                traps.block(f'socket.connect_ex {address!r}')
            return connect_ex(sock, address)

        def guarded_popen(popen, args, *rest, **kwargs):
            argv = shlex.split(args) if isinstance(args, str) else [str(a) for a in args]
            names = {Path(a).name for a in argv} if kwargs.get('shell') or isinstance(args, str) else {Path(argv[0]).name}
            if argv and os.path.realpath(argv[0]) == traps.fake and argv[1:] == ['--version']:
                traps.version_checks.append(argv)
            elif names & set(CLIENTS):
                traps.block(f'launch {argv[:3]!r}')
            return popen_init(popen, args, *rest, **kwargs)

        def os_trap(name):
            real = getattr(os, name)

            def trapped(*args, **kwargs):
                # Popen's own spawn (posix_spawn on some builds) was vetted by guarded_popen: let it through.
                if sys._getframe(1).f_globals.get('__name__') == 'subprocess':
                    return real(*args, **kwargs)
                traps.block(f'os.{name}')
            return trapped

        patches = [
            mock.patch.object(socket, 'create_connection', lambda address, *a, **k: traps.block(f'create_connection {address!r}')),
            mock.patch.object(socket.socket, 'connect', guarded_connect),
            mock.patch.object(socket.socket, 'connect_ex', guarded_connect_ex),
            mock.patch.object(http.client.HTTPConnection, 'connect', lambda c: traps.block(f'http.client {c.host}:{c.port}')),
            mock.patch.object(http.client.HTTPSConnection, 'connect', lambda c: traps.block(f'https {c.host}:{c.port}')),
            mock.patch.object(urllib.request, 'urlopen', lambda url, *a, **k: traps.block(f'urlopen {url!r}')),
            mock.patch.object(urllib.request.OpenerDirector, 'open', lambda o, url, *a, **k: traps.block(f'opener {url!r}')),
            mock.patch.object(subprocess.Popen, '__init__', guarded_popen),
            mock.patch.object(os, 'system', lambda command: traps.block(f'os.system {command!r}')),
            mock.patch.object(getpass, 'getpass', lambda *a, **k: traps.block('getpass: asked for a credential')),
            mock.patch.object(builtins, 'input', lambda *a, **k: traps.block('input')),
        ] + [mock.patch.object(os, name, os_trap(name))
             for name in ('posix_spawn', 'posix_spawnp', 'execv', 'execve', 'execvp', 'execvpe') if hasattr(os, name)]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            yield self


def run_main(name, argv, traps):
    """(exit code or message, stdout + stderr) of one in-process run of the command's main, with traps set."""
    module = commands.load(name)
    clean = {k: v for k, v in os.environ.items() if not CREDENTIALS.match(k)}
    out = io.StringIO()
    code = 0
    with mock.patch.dict(os.environ, clean, clear=True), mock.patch.object(sys, 'argv', [name, *argv]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), traps.active():
        try:
            module.main()
        except SystemExit as stop:
            code = stop.code if stop.code is not None else 0
    return code, out.getvalue()


def runs_listing():
    return sorted(p.name for p in (ROOT/'runs').iterdir()) if (ROOT/'runs').is_dir() else []


class PaidGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()  # as resolve_client sees it (macOS: /var is /private/var)
        self.fake = self.root/'bin'/'claude'
        self.fake.parent.mkdir()
        self.fake.write_text('#!/bin/sh\necho "2.1.284 (Claude Code)"\n')
        self.fake.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def fixtures(self, name, tmp):
        if name == 'late_fix':  # a prepared directory with nothing ready: the plan still prints
            (tmp/'late-fix').mkdir()
            (tmp/'late-fix'/'plan.json').write_text(json.dumps({'items': [], 'python': '3.12', 'counts': {}}))
        if name == 'governed_session':  # a declared suite for --suite
            (tmp/'suite.json').write_text(json.dumps({'name': 'unit', 'command': ['python3', '-m', 'pytest', '-q']}))

    def run_plan(self, row, plan, extra=()):
        tmp = Path(tempfile.mkdtemp(dir=self.root))
        self.fixtures(row.name, tmp)
        argv = commands.plan_args(plan, root=ROOT, tmp=tmp, claude=self.fake) + list(extra)
        traps = Traps(self.fake)
        before = runs_listing()
        # bench grades each task's reference fix before its plan ($0, local, needs work/bench/repos): stood in for here.
        # risk_predictor reads each task's base source the same way ($0, local, needs work/bench/repos): stood in too.
        source = ('', {'names': [], 'source_files': 0, 'files': [], 'bytes': 0})
        with mock.patch('modelpilot.bench.preflight', lambda tasks, *a: {t['id']: {'hidden_passed': 1} for t in tasks}), \
                mock.patch('modelpilot.risk_predictor.context', lambda task, repo=None: source):
            code, output = run_main(row.name, argv, traps)
        self.assertEqual(traps.attempts, [], f'{row.name} {argv}: {output[-1500:]}')
        self.assertEqual(runs_listing(), before, f'{row.name} made a run directory')
        return code, output

    def test_every_paid_plan_sends_nothing_and_prints_a_plan(self):
        for row in commands.paid():
            if row.name in commands.GUARD_SKIPPED:
                continue
            self.assertTrue(row.plans, row.name)
            for plan in row.plans:
                with self.subTest(command=row.name, plan=plan):
                    code, output = self.run_plan(row, plan)
                    if code == 0:
                        self.assertIn(PLAN_MARKERS.get(row.name, '--live'), output)
                    else:
                        self.assertTrue(isinstance(code, str) and any(r in code for r in row.refusals), (code, output))

    def test_every_suite_of_the_direct_api_probes_plans_without_sending(self):
        from modelpilot import cache_replication, thinking_probe
        suites = {'cache_probe': ('quick', 'ttl', 'check'), 'cache_replication': cache_replication.SUITES,
                  'thinking_probe': thinking_probe.SUITES}
        for name, names in suites.items():
            row = commands.find(name)
            for suite in names:
                with self.subTest(command=name, suite=suite):
                    code, output = self.run_plan(row, row.plans[0], extra=['--suite', suite])
                    self.assertEqual(code, 0, output[-1500:])
                    self.assertIn(PLAN_MARKERS.get(name, '--live'), output)

    def test_live_without_a_stop_threshold_is_refused_before_anything_is_sent(self):
        needing = [row for row in commands.paid() if row.needs]
        self.assertEqual({row.name for row in needing}, {'bench', 'late_fix'})
        for row in needing:
            with self.subTest(command=row.name):
                code, output = self.run_plan(row, row.plans[0], extra=['--live'])
                self.assertIsInstance(code, str, output)
                self.assertIn(row.needs, code)

    def test_the_traps_catch_what_they_trap(self):
        traps = Traps(self.fake)
        with mock.patch.dict(os.environ, {}, clear=False), traps.active():
            for attempt in (lambda: socket.create_connection(('192.0.2.1', 443), timeout=1),
                            lambda: http.client.HTTPSConnection('api.anthropic.com').request('GET', '/'),
                            lambda: urllib.request.urlopen('https://api.anthropic.com/v1/models'),
                            lambda: urllib.request.build_opener().open('https://api.anthropic.com/v1/models'),
                            lambda: subprocess.run(['node', '--version']),
                            lambda: subprocess.run('npm --version', shell=True),
                            lambda: subprocess.run([str(self.fake), '-p', 'hi']),
                            lambda: getpass.getpass('key: ')):
                with self.assertRaises(Blocked):
                    attempt()
            sock = socket.socket()
            with self.assertRaises(Blocked):
                sock.connect(('192.0.2.1', 443))
            sock.close()
            self.assertEqual(subprocess.run([str(self.fake), '--version'], capture_output=True, text=True).stdout.strip(),
                             '2.1.284 (Claude Code)')
        self.assertEqual(len(traps.attempts), 9)
        self.assertEqual(len(traps.version_checks), 1)

    def test_popens_own_spawn_passes_and_a_direct_one_is_blocked_by_name(self):
        """subprocess starts a child through os.posix_spawn on some builds (close_fds=False anywhere; with close_fds too
        on Python 3.13+ where glibc has posix_spawn_file_actions_addclosefrom_np, as on CI's 3.14): that is Popen's own
        call, already vetted by the Popen trap, and must go through. A module calling os.posix_spawn itself must not."""
        traps = Traps(self.fake)
        with traps.active():
            done = subprocess.run([str(self.fake), '--version'], capture_output=True, text=True, close_fds=False)
            self.assertEqual(done.stdout.strip(), '2.1.284 (Claude Code)')
            with self.assertRaises(Blocked) as blocked:
                os.posix_spawn(self.fake, [self.fake, '-p', 'hi'], {})
        self.assertEqual(str(blocked.exception), 'os.posix_spawn')
        self.assertEqual((traps.attempts, len(traps.version_checks)), (['os.posix_spawn'], 1))

    def test_the_fake_client_passes_by_its_real_path(self):
        """bench.resolve_client launches the client's real path. On macOS the temporary directory is under /var, a
        symlink to /private/var, so the fake given as /var/... ran as /private/var/... and was blocked as a client
        (October 15, the main machine's first run after #86-#100: 3 errors). A symlinked directory shows it anywhere."""
        link = self.root/'link'
        link.symlink_to(self.fake.parent, target_is_directory=True)
        traps = Traps(link/'claude')
        with traps.active():
            for path in (os.path.realpath(link/'claude'), link/'claude'):
                done = subprocess.run([str(path), '--version'], capture_output=True, text=True)
                self.assertEqual(done.stdout.strip(), '2.1.284 (Claude Code)')
            with self.assertRaises(Blocked):
                subprocess.run([os.path.realpath(link/'claude'), '-p', 'hi'])
        self.assertEqual((len(traps.attempts), len(traps.version_checks)), (1, 2))


def mains():
    """Every modelpilot/*.py module with a top-level main."""
    found = set()
    for path in sorted((ROOT/'modelpilot').glob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        if any(isinstance(n, ast.FunctionDef) and n.name == 'main' for n in tree.body):
            found.add(path.stem)
    return found


def claude_md_paid():
    """The module names on CLAUDE.md's 'Paid' line."""
    line = next(line for line in (ROOT/'CLAUDE.md').read_text().splitlines() if line.startswith('- Paid'))
    return set(re.findall(r'`([a-z_0-9]+)`', line.split(':')[0] + line.split(':', 1)[1].split('Limits')[0]))


class RegistryTests(unittest.TestCase):
    def test_every_module_with_a_main_is_registered_or_excluded_with_a_reason(self):
        names = [c.name for c in commands.COMMANDS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names) | set(commands.EXCLUDED), mains())
        self.assertFalse(set(names) & set(commands.EXCLUDED))
        for name, reason in commands.EXCLUDED.items():
            self.assertTrue(reason.strip(), name)
        for row in commands.COMMANDS:
            self.assertEqual(row.module, f'modelpilot.{row.name}')
            self.assertTrue(row.summary and '\n' not in row.summary, row.name)
            self.assertTrue(callable(getattr(commands.load(row.name), 'main', None)), row.name)
            self.assertTrue(row.paid or not row.plans and not row.needs, row.name)

    def test_the_paid_set_includes_claude_md_and_the_ones_found_in_code(self):
        listed = claude_md_paid()
        self.assertGreaterEqual(len(listed), 10)
        paid = {c.name for c in commands.paid()}
        self.assertLessEqual(listed, paid)
        self.assertLessEqual({'late_fix', 'cache_replication', 'miss_checklist', 'spec_tests', 'risk_predictor'}, paid)
        self.assertLessEqual(set(commands.GUARD_SKIPPED), paid)


if __name__ == '__main__':
    unittest.main()
