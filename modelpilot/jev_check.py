"""Pinned Jev router-only credential preflight; does not invoke Claude."""
import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


def validate_result(data):
    result = data.get('result')
    return bool(isinstance(result, dict) and result.get('choice') in data.get('offered_models', [])
                and type(result.get('confidence')) in (int, float) and 0 <= result['confidence'] <= 1
                and isinstance(result.get('response'), dict) and result['response']
                and isinstance(result.get('request'), dict) and result['request'])


def clean_env(key, home, tmp):
    # Explicit environment: never pass Anthropic credentials to this routing-only check.
    names = ('PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NODE_EXTRA_CA_CERTS')
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update(JEV_API_KEY=key, HOME=str(home), TMPDIR=str(tmp))
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--diagnostic', action='store_true', help='15-second scoring timeout, no retries; not a baseline result')
    parser.add_argument('--jev-root', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    pin = json.loads((root / 'configs/jev-baseline.json').read_text())
    # Repo-local work/ first; the second path is the original handoff machine's layout.
    candidates = [args.jev_root] if args.jev_root else [root / 'work/jev-router-baseline', root.parent.parent / 'work/jev-router-baseline']
    jev = next((c for c in candidates if (c / '.git').exists()), candidates[0]).resolve()
    if not args.live:
        print('Plan: one TypeSafe routing decision; ' + ('15-second diagnostic, no retries' if args.diagnostic else 'stock three-second deadline, one retry possible') + '; no Claude/model requests. Router cost is unpriced. Add --live; key prompted privately if absent.')
        return
    if not shutil.which('node'):
        raise SystemExit('Node >=20.12 is required.')
    if not (jev / '.git').exists():
        raise SystemExit(f'No Jev checkout at {jev}. Clone the pinned commit there (see docs/jev-baseline-setup.md) or pass --jev-root.')
    revision = subprocess.run(['git', '-C', str(jev), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(['git', '-C', str(jev), 'status', '--porcelain', '--untracked-files=no'], capture_output=True, text=True, check=True).stdout.strip()
    if revision != pin['commit'] or dirty:
        raise SystemExit('Jev checkout must match the pinned revision with no tracked modifications.')
    if not (jev / 'node_modules/@typesafe-ai/sdk').is_dir():
        raise SystemExit('Run npm ci --ignore-scripts in the pinned Jev checkout first.')
    key = os.environ.get('JEV_API_KEY') or os.environ.get('TYPESAFE_API_KEY') or getpass.getpass('TypeSafe/Jev API key (hidden): ').strip()
    if not key or any(c.isspace() for c in key):
        raise SystemExit('Missing or malformed key. No requests sent.')
    run = root / 'runs' / ('jev-preflight-' + time.strftime('%Y%m%d-%H%M%S'))
    run.parent.mkdir(exist_ok=True)  # runs/ is gitignored, so absent in a fresh clone
    run.mkdir(mode=0o700)
    home = run / 'home'; home.mkdir(mode=0o700)
    tmp = run / 'tmp'; tmp.mkdir(mode=0o700)
    print('Running router-only preflight; results: ' + str(run), flush=True)
    report = {'status': 'failed', 'provider_calls': 0, 'router_cost_usd': None, 'cost_complete': False,
              'diagnostic': args.diagnostic, 'baseline_eligible': False,
              'baseline_commit': revision, 'scope': 'Credential/scoring check with constrained 4.6 catalog, not a stock Jev/Claude benchmark.'}
    try:
        result = subprocess.run(['node', str(root / 'modelpilot/jev_preflight.mjs'), str(jev)] + (['--diagnostic'] if args.diagnostic else []),
                                env=clean_env(key, home, tmp), cwd=home, text=True,
                                capture_output=True, timeout=25)
        stdout = result.stdout.replace(key, '[REDACTED]')
        stderr = result.stderr.replace(key, '[REDACTED]')
        (run / 'stdout.json').write_text(stdout)
        (run / 'stderr.txt').write_text(stderr)
        data = json.loads(stdout)
        passed = result.returncode == 0 and validate_result(data)
        report.update(status='passed' if passed else 'failed', returncode=result.returncode,
                      selected_model=(data.get('result') or {}).get('choice'), wall_ms=data.get('wall_ms'))
        if not passed:
            report['hint'] = ('Router deadline/timeout reached; key validity is unconfirmed.' if 'aborted' in stderr.lower() or 'timed out' in stderr.lower() else 'No verified router decision. Inspect isolated stderr; fallback is not a routing pass.')
    except (subprocess.TimeoutExpired, ValueError, OSError) as error:
        report['error_type'] = type(error).__name__
    (run / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if report['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
