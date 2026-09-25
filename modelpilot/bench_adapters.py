"""Experimental benchmark adapters. Live CLI eligibility remains gated separately."""
import json
from pathlib import Path
from .jev_route_check import verify_checkout, DECISION, FAILURES

ROOT = Path(__file__).resolve().parents[1]

class JevAdapter:
    model = 'jev-router'

    def __init__(self, variant, checkout, node, key):
        if variant not in ('stock', 'compat') or not key:
            raise ValueError('Require stock/compat variant and a local Jev key')
        self.variant, self.checkout, self.node, self.key = variant, Path(checkout), Path(node), key
        self.arm_id = 'jev-' + variant

    def verify(self):
        config = json.loads((ROOT/'configs/jev-baseline.json').read_text())
        patch = ROOT/config['compat_variant']['patch'] if self.variant == 'compat' else None
        if not verify_checkout(self.checkout, config['commit'], patch):
            raise ValueError('Jev checkout does not match the pinned revision and variant patch')
        if not self.node.is_file():
            raise ValueError('Explicit Node executable required')

    def command(self, command, proxy_url):
        args = list(command[1:])
        if '--model' in args:
            i = args.index('--model')
            del args[i:i+2]
        return [str(self.node), str(ROOT/'modelpilot/jev_accounted_launch.mjs'),
                str(self.checkout), proxy_url, '--claude-bin', str(command[0]), '--', *args]

    def environment(self, env):
        result = {k:v for k,v in env.items() if not k.startswith('ANTHROPIC_DEFAULT_')
                  and k not in ('ANTHROPIC_MODEL','ANTHROPIC_SMALL_FAST_MODEL','TYPESAFE_API_KEY')}
        result.update(JEV_API_KEY=self.key, JEV_DEBUG='1', JEV_NO_STATUSLINE='1', CLAUDE_CODE_MAX_RETRIES='0')
        return result

    def accounting(self, rows, final):
        from .bench import accounting
        report = accounting(rows, final)
        provider = report['cost_usd'] if report['tokens_match'] and not report['rejected_requests'] else None
        report.update(provider_cost_usd=provider, router_cost_usd=None, cost_usd=None,
                      cost_complete=False, client_cost_matches=None,
                      client_cost_basis='sentinel_not_used_for_pricing')
        return report

    def evidence(self, directory):
        directory = Path(directory)
        decisions = []
        for path in sorted((directory/'tmp'/'jev-claude').glob('*.json')):
            data = json.loads(path.read_text())
            if isinstance(data, dict) and 'jev' in data:
                decisions.extend(d for d in data.get('history') or [data] if isinstance(d, dict))
        stderr = directory/'client.stderr.txt'
        text = stderr.read_text() if stderr.exists() else ''
        routed = DECISION.findall(text)
        evidence = dict(variant=self.variant, launcher='accounted-harness',
                        decision_count=len(decisions), logged_decision_count=len(routed),
                        decision_models=[d.get('model') for d in decisions],
                        fallback_markers=[f for f in FAILURES if f in text],
                        routing_observed=bool(decisions) and len(decisions)==len(routed),
                        router_cost_usd=None, total_cost_complete=False)
        # Preserve decision history before trial cleanup, never credentials.
        raw = json.dumps(decisions, indent=2).replace(self.key, '[REDACTED]')
        (directory/'jev-decisions.json').write_text(raw+'\n')
        return evidence
