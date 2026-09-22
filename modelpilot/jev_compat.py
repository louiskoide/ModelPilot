"""Offline check: can the pinned Jev route this Claude Code version's requests?

No keys, network or billing. Runs the given Claude Code CLI against Jev's real
proxy with a local fake Messages API and fake router (see jev_compat_probe.mjs).
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time
from .jev_route_check import PATCH, verify_checkout


def prepare_compat(root, source):
    """Build work/jev-router-compat: pinned Jev plus exactly the ModelPilot patch. Source is never modified."""
    commit = json.loads((root/'configs/jev-baseline.json').read_text())['commit']
    target = root/'work/jev-router-compat'
    if target.exists():
        if verify_checkout(target, commit, root/PATCH):
            return target
        raise SystemExit(f'{target} exists but is not pinned Jev plus {PATCH}; remove it and rerun.')
    if not verify_checkout(source, commit):
        raise SystemExit(f'Source {source} must be the clean pinned checkout.')
    subprocess.run(['git', 'clone', '-q', '--no-hardlinks', str(source), str(target)], check=True)
    subprocess.run(['git', '-C', str(target), 'checkout', '-q', commit], check=True)
    subprocess.run(['git', '-C', str(target), 'apply', '--whitespace=nowarn', str(root/PATCH)], check=True)
    subprocess.run(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], cwd=target, check=True)
    if not verify_checkout(target, commit, root/PATCH):
        raise SystemExit('Patched checkout does not match the recorded patch.')
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--claude', type=Path, help='Claude Code executable (default: claude on PATH)')
    parser.add_argument('--jev-root', type=Path)
    parser.add_argument('--prepare-compat', action='store_true', help='Build work/jev-router-compat from the pinned checkout and patch')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.prepare_compat:
        target = prepare_compat(root, root/'work/jev-router-baseline')
        print(f'Compat checkout ready: {target} (pinned + {PATCH}). Run upstream tests with npm test there.')
        return
    jev = (args.jev_root or root/'work/jev-router-baseline').resolve()
    cli = args.claude or Path(shutil.which('claude') or '')
    if not cli.name or not cli.exists():
        raise SystemExit('Claude Code CLI not found; pass --claude.')
    if not shutil.which('node') or not (jev/'src/proxy.mjs').exists():
        raise SystemExit(f'Node and a Jev checkout at {jev} are required.')
    version = subprocess.run([str(cli), '--version'], capture_output=True, text=True, timeout=30).stdout.strip()
    run = root/'runs'/('jev-compat-'+time.strftime('%Y%m%d-%H%M%S'))
    run.parent.mkdir(exist_ok=True)
    run.mkdir(mode=0o700)
    subprocess.run(['node', str(root/'modelpilot/jev_compat_probe.mjs'), str(jev), str(cli.resolve()), str(run/'isolated'),
                    str(run/'probe.json')], timeout=150, check=True)
    probe = json.loads((run/'probe.json').read_text())
    first = probe.get('first_agent_request') or {}
    summary = {'client_version': version, 'jev_root': str(jev), 'compatible': probe['compatible'],
               'router_calls': probe['router_calls'], 'continuation_seen': probe['continuation_seen'],
               'agent_models': probe['agent_models'], 'last_role': first.get('last_role'),
               'roles': first.get('roles'), 'jev_prompt_extracted': first.get('jev_prompt_extracted'),
               'network': 'loopback only', 'cost_usd': 0}
    (run/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary, indent=2))
    if not probe['compatible']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
