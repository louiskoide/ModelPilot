"""A $0 preflight before live runs (October 15, overnight cloud run): read-only checks, each pass, warn or fail.

    python3 -m modelpilot.doctor [--json]

Exits 1 if any check fails. It never fixes, installs or writes anything, sends nothing to a model and reads no
credential: a credential variable is reported present or absent, by name only, never its value or length. The client
is asked only for its --version, with an empty HOME and nothing else in its environment.

Checks: the Python running this; the first claude on PATH is work/claude-client's pinned version; work/bench/py312
matches bench/environment.json and its site-packages are read-only (CLAUDE.md: lock them before live runs); the final
split's lock holds (bench_tasks.check_lock; a count only); work/jev-router-baseline at its pinned commit with a clean
status, and work/jev-router-compat present; runs/ and work/ ignored by Git, and whether the tree is clean; the rate
files parse and every model the policy config or a bench arm can send has rates; the credential variables.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS = ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN', 'JEV_API_KEY', 'TYPESAFE_API_KEY')
SENTINELS = ('jev-router',)  # the Jev arms' placeholder model: never sent to the API, never priced


def check(name, status, detail):
    return {'check': name, 'status': status, 'detail': detail}


def run(command, **options):
    return subprocess.run(command, capture_output=True, text=True, timeout=60, **options)


def python_check(version=sys.version_info, executable=sys.executable):
    ok = tuple(version[:2]) >= (3, 10)
    return check('python', 'pass' if ok else 'fail',
                 f'{version[0]}.{version[1]}.{version[2]} at {executable}' + ('' if ok else '; needs 3.10 or later'))


def client_check(root, env):
    """The first claude on PATH must be work/claude-client's pinned version, checked with --version only."""
    pinned = json.loads((root/'bench/environment.json').read_text()).get('claude_code')
    expected = root/'work/claude-client/node_modules/.bin/claude'
    found = shutil.which('claude', path=env.get('PATH', ''))
    if not found:
        return check('client', 'fail', f'no claude on PATH; put {expected.parent} first')
    with tempfile.TemporaryDirectory() as home:
        try:
            out = run([found, '--version'], env={'HOME': home, 'PATH': env.get('PATH', '')}).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as error:
            return check('client', 'fail', f'{found} --version failed ({type(error).__name__})')
    version = out.split(' ')[0] if out else None
    where = 'work/claude-client' if Path(found).resolve() == expected.resolve() else found
    if version != pinned:
        return check('client', 'fail', f'first claude on PATH is {version} ({where}), not the pinned {pinned}')
    if where != 'work/claude-client':
        return check('client', 'warn', f'{pinned} on PATH, but from {found}, not work/claude-client')
    return check('client', 'pass', f'{pinned} from work/claude-client')


def venv_check(root):
    """work/bench/py312 has exactly bench/environment.json's packages, and its site-packages are read-only."""
    spec = json.loads((root/'bench/environment.json').read_text())
    python = root/'work/bench/py312/bin/python'
    if not python.exists():
        return [check('grader venv', 'fail', f'no {python.relative_to(root)}; recreate it with bench/environment.json')]
    script = ('import importlib.metadata as m, json, os, site, sys\n'
              'print(json.dumps({"python": "%d.%d.%d" % sys.version_info[:3], '
              '"packages": sorted(f"{d.metadata[\'Name\']}=={d.version}" for d in m.distributions() '
              'if d.metadata["Name"] not in ("pip", "setuptools")), '
              '"writable": [p for p in site.getsitepackages() if os.path.exists(p) and os.access(p, os.W_OK)]}))')
    try:
        seen = json.loads(run([str(python), '-I', '-c', script]).stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return [check('grader venv', 'fail', f'could not read it ({type(error).__name__})')]
    wanted = sorted(spec['packages'], key=str.lower)
    have = sorted(seen['packages'], key=str.lower)
    lower = lambda items: {p.lower() for p in items}
    out = [check('grader venv', 'pass' if lower(have) == lower(wanted) else 'fail',
                 f"Python {seen['python']} (environment.json names {spec.get('python')}); "
                 + ('packages match' if lower(have) == lower(wanted) else
                    f'missing {sorted(lower(wanted) - lower(have))}, extra {sorted(lower(have) - lower(wanted))}'))]
    out.append(check('grader site-packages', 'warn' if seen['writable'] else 'pass',
                     f"writable: {len(seen['writable'])} directories; lock them before a live run "
                     '(bench does it with lock_benchmark_environment)' if seen['writable'] else 'read-only'))
    return out


def lock_check(root):
    from . import bench_tasks
    try:
        tasks = [json.loads(p.read_text()) for p in sorted((root/'bench/tasks').glob('*/task.json'))]
        changed = bench_tasks.check_lock(tasks, json.loads((root/'bench/splits.json').read_text()))
    except (OSError, ValueError, KeyError) as error:
        return check('final split lock', 'fail', f'unreadable ({type(error).__name__})')
    return check('final split lock', 'fail' if changed else 'pass',
                 f'{len(changed)} final task specs changed since the lock' if changed else 'holds')


def jev_checks(root):
    pin = json.loads((root/'configs/jev-baseline.json').read_text())
    out = []
    baseline = root/'work/jev-router-baseline'
    if not (baseline/'.git').exists():
        out.append(check('jev baseline', 'warn', 'no work/jev-router-baseline (needed by Jev arms only)'))
    else:
        try:
            head = run(['git', '-C', str(baseline), 'rev-parse', 'HEAD']).stdout.strip()
            dirty = run(['git', '-C', str(baseline), 'status', '--porcelain', '--untracked-files=no']).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as error:
            head, dirty = None, type(error).__name__
        ok = head == pin['commit'] and not dirty
        out.append(check('jev baseline', 'pass' if ok else 'fail',
                         f'at {head[:12] if head else head}' + ('' if head == pin['commit'] else f", pinned {pin['commit'][:12]}")
                         + ('; tracked files changed' if dirty else '; clean')))
    compat = root/'work/jev-router-compat'
    out.append(check('jev compat', 'pass' if (compat/'src/proxy.mjs').exists() else 'warn',
                     'present' if (compat/'src/proxy.mjs').exists() else
                     'missing; python3 -m modelpilot.jev_compat --prepare-compat builds it (Jev arms only)'))
    return out


def git_checks(root):
    def ignored(name):  # a path inside it; a worktree whose runs/ is a symlink can only ask about the link
        code = run(['git', '-C', str(root), 'check-ignore', '-q', '--no-index', f'{name}/x']).returncode
        return (code if code != 128 else run(['git', '-C', str(root), 'check-ignore', '-q', '--no-index', name]).returncode) == 0
    try:
        both = all(ignored(name) for name in ('runs', 'work'))
        status = run(['git', '-C', str(root), 'status', '--porcelain']).stdout
    except (OSError, subprocess.TimeoutExpired) as error:
        return [check('git', 'warn', f'git unavailable ({type(error).__name__})')]
    changed = [line for line in status.splitlines() if line.strip()]
    return [check('runs/ and work/ ignored', 'pass' if both else 'fail',
                  'both ignored by Git' if both else 'not both ignored: run output could be committed'),
            check('working tree', 'pass' if not changed else 'warn',
                  'clean' if not changed else f'{len(changed)} paths changed or untracked; a run records the code it ran')]


def served_models():
    """Every model the policy config or a bench arm can send (the rule bench applies before a run)."""
    from . import bench, bench_jev, policy_actions
    models = set(policy_actions.MODELS)
    for arm in bench.ARMS.values():
        if arm['kind'] == 'jev':
            models |= set(arm.get('models', bench_jev.JEV_MODELS))
        else:
            models |= set(arm.get('served_models', [arm['model']]) if arm.get('model') or arm.get('served_models') else [])
        if bench.compaction_model(arm):
            models.add(bench.compaction_model(arm))
    return sorted(m for m in models if m and m not in SENTINELS)


def rates_check(root):
    from . import bench
    try:
        for path in sorted((root/'configs').glob('*rates*.json')) + [root/'configs/m0.json']:
            json.loads(path.read_text())
        table = bench.rates()
        missing = [m for m in served_models() if m not in table]
    except (OSError, ValueError, KeyError) as error:
        return check('rates', 'fail', f'unreadable ({type(error).__name__})')
    return check('rates', 'fail' if missing else 'pass',
                 f'no rates for {missing}' if missing else f'{len(served_models())} models the arms and policy send all have rates')


def credential_checks(env):
    out = [check(f'credential {name}', 'pass', 'present' if env.get(name) else 'absent') for name in CREDENTIALS]
    out.append(check('ANTHROPIC_BASE_URL', 'warn' if env.get('ANTHROPIC_BASE_URL') else 'pass',
                     'set: direct-API harnesses refuse a gateway, and the bench sets its own' if env.get('ANTHROPIC_BASE_URL')
                     else 'unset'))
    return out


def checks(root=ROOT, env=None):
    env = os.environ if env is None else env
    return ([python_check(), client_check(root, env)] + venv_check(root) + [lock_check(root)] + jev_checks(root)
            + git_checks(root) + [rates_check(root)] + credential_checks(env))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--json', action='store_true', help='print the checks as JSON')
    args = parser.parse_args(argv)
    results = checks()
    if args.json:
        print(json.dumps({'checks': results, 'failed': sum(r['status'] == 'fail' for r in results)}, indent=1))
    else:
        for r in results:
            print(f"{r['status'].upper():5} {r['check']}: {r['detail']}")
    if any(r['status'] == 'fail' for r in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
