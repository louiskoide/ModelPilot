"""What randomized mid-task switches did ($0): the modelpilot-explore arm's trials, switched against not switched.

Each trial's draw (switch_policy.exploration_plan, from its task, arm and trial number) decided whether it switches
to Opus 5.5 at the turn's effort and at which main-loop request, independently of the task, so switched and
unswitched trials are comparable by design. Per trial: the draw, whether the switch happened (a trial that ended
before its drawn request never switched), the switch cost the policy predicted and the one measured, strict pass
and cold-equivalent dollars.

Measured switch cost: the first Opus request's cache writes, priced at Opus's write rate for their lifetime less
its read rate (the rewrite beyond the read it replaces, as switch_policy.switch_cost defines it). It includes that
request's own new tokens (the tool result it adds, typically under a thousand), so it runs slightly above the pure
rewrite.
Usage: python3 -m modelpilot.exploration runs/bench-<ts> [...] [--out runs/exploration-report-<ts>.json]
"""
import argparse
import json
from pathlib import Path
import statistics
from .cache_probe import prompt_tokens, tier

ARM = 'modelpilot-explore'


def measured_switch_usd(rows, target_model, rates):
    """The first answered main-loop request on the target model: its cache writes at (write - read), or None."""
    main = sorted((r for r in rows if r.get('kind') == 'messages' and r.get('tool_count') and r.get('http_status') == 200
                   and r.get('model') == target_model), key=lambda r: r.get('started_unix', 0))
    if not main:
        return None
    usage = main[0].get('usage') or {}
    rate = tier(rates.get(target_model), prompt_tokens(usage), main[0].get('started_unix'))
    split = usage.get('cache_creation') or {}
    if not rate or not split:
        return None
    return (split.get('ephemeral_5m_input_tokens', 0) * (rate['write_5m'] - rate['read']) +
            split.get('ephemeral_1h_input_tokens', 0) * (rate['write_1h'] - rate['read'])) / 1e6


def trial_view(record, rows, rates):
    policy = (record.get('routing') or {}).get('policy') or {}
    entries = policy.get('exploration') or []
    entry = entries[0] if entries else None
    confirmed = any(e.get('trigger') == 'exploration' and e.get('status') == 'confirmed'
                    for e in policy.get('escalations') or [])
    target = (entry or {}).get('target') or [None]
    return {'task': record['task'], 'trial': record.get('trial'), 'passed': record.get('passed'),
            'complete': record.get('complete'), 'eligible': (record.get('routing') or {}).get('benchmark_eligible'),
            'cold_equivalent_usd': (record.get('cache') or {}).get('cold_equivalent_cost_usd'),
            'requests': (record.get('accounting') or {}).get('requests'),
            'switched': bool(entry) and confirmed, 'at_request': (entry or {}).get('request'),
            'predicted_switch_usd': (entry or {}).get('predicted_switch_usd'),
            'measured_switch_usd': measured_switch_usd(rows, target[0], rates) if entry and confirmed else None}


def summarize(views):
    usable = [v for v in views if v['complete'] and v['eligible']]

    def group(vs):
        costs = [v['cold_equivalent_usd'] for v in vs if v['cold_equivalent_usd'] is not None]
        return {'trials': len(vs), 'passed': sum(bool(v['passed']) for v in vs),
                'pass_rate': sum(bool(v['passed']) for v in vs) / len(vs) if vs else None,
                'mean_cost_usd': statistics.fmean(costs) if costs else None, 'priced': len(costs)}
    switched = [v for v in usable if v['switched']]
    pairs = [(v['predicted_switch_usd'], v['measured_switch_usd']) for v in switched
             if v['predicted_switch_usd'] and v['measured_switch_usd'] is not None]
    ratios = [m / p for p, m in pairs]
    return {'switched': group(switched), 'not_switched': group([v for v in usable if not v['switched']]),
            'excluded': len(views) - len(usable),
            'prediction': {'pairs': len(pairs),
                           'median_measured_over_predicted': statistics.median(ratios) if ratios else None,
                           'range': [min(ratios), max(ratios)] if ratios else None,
                           'mean_predicted_usd': statistics.fmean(p for p, _ in pairs) if pairs else None,
                           'mean_measured_usd': statistics.fmean(m for _, m in pairs) if pairs else None},
            'note': 'Randomized within the arm: compare switched with not switched. Few trials fail, so a pass-rate '
                    'difference needs many trials; the switch-cost comparison needs few.'}


def load(run_dirs, rates):
    views = []
    for run in run_dirs:
        for path in sorted(Path(run).glob(f'*/{ARM}/*/trial.json')):
            record = json.loads(path.read_text())
            log = path.parent/'observations.jsonl'
            rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()] if log.exists() else []
            views.append(trial_view(record, rows, rates))
    return views


def main():
    from .bench import rates
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('runs', type=Path, nargs='+')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    views = load(args.runs, rates())
    out = dict(summarize(views), runs=[str(r) for r in args.runs], trials=views)
    if args.out:
        args.out.write_text(json.dumps(out, indent=1) + '\n')
    print(json.dumps({k: v for k, v in out.items() if k != 'trials'}, indent=1))


if __name__ == '__main__':
    main()
