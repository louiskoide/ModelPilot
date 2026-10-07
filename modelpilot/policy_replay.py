"""Replay recorded ModelPilot decisions through the current switch policy ($0).

Each ModelPilot trial journals its advisor decisions in its governor database: Jev's whole answer, the
setting, the decision point and the conversation profile. This re-runs switch_policy.decide on them with
today's config and rates, and reports where the decision changes beside each trial's recorded outcome.

    python3 -m modelpilot.policy_replay runs/bench-<ts> [runs/bench-<ts2> ...] [--arm ARM] [--out FILE]

--arm replays through that arm's overrides of the config (bench.ARMS 'policy_overrides', e.g. modelpilot-delegate)
and from that arm's start (S0): while a trial is still on the start it recorded (every decision so far the same), a
decision is replayed from the arm's start instead, and a recorded stay there counts as a stay on the arm's start.
That asks what the gate would decide from a new start (low concise since October 6) at the points the trial reached.
Only recorded decision points replay: a point a newer config would add (a forced review at the agent's finish or
when the suite first passes) was never journaled, so it can't appear here.

The journal's token counts were estimated at the bytes_per_token of the recording: the trial's recorded parameters
give it since October 3 (2.8), and older trials, which don't, used 4 (--recorded-bytes-per-token). They are rescaled to
the current value. Before October 6 every trial was replayed at 4, which put the October 5-6 runs' prefixes 1.43x high.

A decision after one whose replay differs is marked path_diverged: the trial would not have reached it the same way.
Databases are opened read-only; nothing is called.

With the config's jev_gate on, each replayed decision also says whether the gate would have skipped Jev there
(replayed.gate); the decision itself is still replayed on the answer Jev gave. A skipped point whose recorded answer
replays to anything but a stay would mean the gate is wrong: summary gate.skipped_but_moved counts them.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
from . import switch_policy

RECORDED_BYTES_PER_TOKEN = 4  # configs/modelpilot-policy.json at every recorded run before October 3


def _parameters(record):
    return ((record.get('routing') or {}).get('policy') or {}).get('parameters') or {}


def recorded_decisions(db_path):
    """The advisor decisions journaled in one trial's governor database, in order."""
    db = sqlite3.connect(f'file:{Path(db_path).resolve()}?immutable=1', uri=True)
    try:
        return [json.loads(p) for (p,) in db.execute(
            "select payload from gov_decisions where kind = 'advisor_decision' order by seq")]
    finally:
        db.close()


def replay(payload, cfg, rates, recorded_bytes_per_token=RECORDED_BYTES_PER_TOKEN, current=None):
    """switch_policy.decide on one journaled decision, from current (default: the recorded setting); None when the
    journal lacks what it needs."""
    profile = payload.get('profile')
    current = current or payload.get('current')
    if not profile or not current or payload.get('trigger') not in switch_policy.TRIGGERS:
        return None
    scale = recorded_bytes_per_token / cfg['bytes_per_token']
    prof = dict(cfg['defaults'], prefix_tokens=profile['prefix_tokens'] * scale,
                messages_tokens=profile['messages_tokens'] * scale, warm=profile['warm'],
                warm_entries={k: v * scale for k, v in (profile.get('warm_entries') or {}).items()},
                # Earlier work in the conversation (for a handoff note's price): recorded since October 5, else
                # assumed after a turn start.
                history=profile.get('history', payload['trigger'] != 'turn_start'))
    if 'brief_tokens' in profile:
        prof['brief_tokens'] = profile['brief_tokens']
    advice = payload.get('advice')
    usable = advice if advice and not advice.get('error') else None  # as active_policy.decide
    decision = switch_policy.decide(cfg, rates, usable, current, prof, payload['trigger'])
    if switch_policy.gate(cfg)['enabled']:
        decision['gate'] = switch_policy.jev_gate(cfg, rates, current, prof, payload['trigger'])
    return decision


def _setting(decision):
    return '/'.join(str(x) for x in decision['target'])


def _p_current(decision):
    return (decision.get('candidates') or [{}])[0].get('p_ok')


def recorded_start(record, payloads):
    """The setting a trial started on: its arm's recorded S0, else its first decision's current setting."""
    start = _parameters(record).get('S0')
    return tuple(start or (payloads[0].get('current') if payloads else None) or ())


def replay_run(run_dir, cfg, rates, recorded_bytes_per_token=RECORDED_BYTES_PER_TOKEN, start=None):
    """One row per journaled decision of every ModelPilot trial in a run. recorded_bytes_per_token: for trials that
    don't record their own. start: replay from this setting while a trial is still on the start it recorded (see the
    module docstring)."""
    from .bench_report import cold_cost, load_run
    _, records = load_run(run_dir, rates)
    outcome = {(r['task'], r['arm'], r.get('trial', 0)): r for r in records}
    rows = []
    for db_path in sorted(Path(run_dir).glob('*/*/*/governor.sqlite3')):
        trial_dir = db_path.parent
        task, arm, trial = trial_dir.parts[-3], trial_dir.parts[-2], int(trial_dir.parts[-1])
        record = outcome.get((task, arm, trial)) or {}
        if (record.get('routing') or {}).get('kind') != 'modelpilot_policy':
            continue
        diverged = False
        per_token = (_parameters(record).get('cost_model') or {}).get('bytes_per_token') or recorded_bytes_per_token
        payloads = recorded_decisions(db_path)
        began = recorded_start(record, payloads)
        rebase = bool(start and began and tuple(start) != began)
        for payload in payloads:
            on_start = rebase and not diverged and tuple(payload.get('current') or ()) == began
            current = list(start) if on_start else payload.get('current')
            new = replay(payload, cfg, rates, per_token, current=current)
            row = {'run': Path(run_dir).name, 'task': task, 'arm': arm, 'trial': trial, 'point': payload.get('point'),
                   'trigger': payload.get('trigger'), 'current': '/'.join(str(x) for x in current or []),
                   'recorded': {'action': payload.get('action'), 'target': _setting(payload),
                                'p_ok_current': _p_current(payload)},
                   'trial_passed': record.get('passed'), 'trial_cost_usd': cold_cost(record) if record else None}
            baseline = row['recorded']
            if on_start:  # what the recorded decision was, read from the new start: a stay there is a stay here
                stayed = baseline['target'] == '/'.join(str(x) for x in began)
                baseline = dict(baseline, target=row['current'] if stayed else baseline['target'])
                row.update(recorded_current='/'.join(str(x) for x in began), baseline=baseline)
            if new is None:
                row['replayed'] = None
                row['status'] = 'not_replayable'
            else:
                row['replayed'] = {'action': new['action'], 'target': _setting(new), 'reason': new.get('reason'),
                                   'p_ok_current': _p_current(new), 'forecast_usd': new['forecast_usd'],
                                   'candidates': new.get('candidates')}
                if 'gate' in new:
                    row['replayed']['gate'] = dict(new['gate'], skip=not new['gate']['can_change'])
                changed = (new['action'], _setting(new)) != (baseline['action'], baseline['target'])
                row['status'] = 'path_diverged' if diverged else 'changed' if changed else 'same'
                diverged = diverged or changed
            rows.append(row)
    return rows


def summarize(rows):
    gated = [r for r in rows if (r['replayed'] or {}).get('gate')]
    gate = {'decisions': len(gated),
            'skipped': dict(Counter(r['trigger'] for r in gated if r['replayed']['gate']['skip'])),
            'asked': dict(Counter(f"{r['trigger']}:{r['replayed']['gate']['reason']}" for r in gated
                                  if not r['replayed']['gate']['skip'])),
            'skipped_but_moved': sum(r['replayed']['gate']['skip'] and r['replayed']['action'] != 'stay' for r in gated),
            'closest_skipped_usd': max((r['replayed']['gate']['closest_usd'] for r in gated if r['replayed']['gate']['skip']
                                        and r['replayed']['gate']['closest_usd'] is not None), default=None)}
    return {'decisions': len(rows), 'status': dict(Counter(r['status'] for r in rows)),
            **({'gate': gate} if gated else {}),
            'by_trigger': {t: dict(Counter(r['status'] for r in rows if r['trigger'] == t))
                           for t in sorted({r['trigger'] for r in rows if r['trigger']})},
            'rebased': sum('baseline' in r for r in rows),
            'moves': dict(Counter(f"{_baseline(r)['action']} {_baseline(r)['target']} -> "
                                  f"{r['replayed']['action']} {r['replayed']['target']}"
                                  for r in rows if r['status'] == 'changed'))}


def _baseline(row):
    """The recorded decision as compared: read from the new start when the row was rebased."""
    return row.get('baseline') or row['recorded']


def main():
    from .bench import rates
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('runs', type=Path, nargs='+')
    parser.add_argument('--recorded-bytes-per-token', type=float, default=RECORDED_BYTES_PER_TOKEN,
                        help="For trials that don't record theirs (before October 3)")
    parser.add_argument('--arm', help="Replay through this arm's policy overrides (bench.ARMS), from its start")
    parser.add_argument('--out', type=Path, help='Write the rows and summary here (must not exist)')
    args = parser.parse_args()
    from .bench import ARMS
    overrides = ARMS[args.arm].get('policy_overrides') if args.arm else None
    start = [ARMS[args.arm]['model'], ARMS[args.arm]['effort']] if args.arm else None
    cfg, table = switch_policy.with_overrides(switch_policy.load(), overrides), rates()
    rows = [row for run in args.runs for row in replay_run(run, cfg, table, args.recorded_bytes_per_token, start)]
    result = {'config_sha256': hashlib.sha256(switch_policy.CONFIG.read_bytes()).hexdigest(), 'arm': args.arm,
              'overrides': overrides, 'start': start,
              'recorded_bytes_per_token': args.recorded_bytes_per_token, 'summary': summarize(rows), 'rows': rows}
    for r in rows:
        new, was = r['replayed'] or {}, _baseline(r)
        print(f"{r['run'][6:]} {r['task']:30s} {r['trigger']:14s} {was['action']:4s} {was['target']:24s} "
              f"-> {new.get('action', '-'):4s} {new.get('target', '-'):24s} {r['status']:13s} "
              f"p(current) {r['recorded']['p_ok_current'] or 0:.2f} -> {new.get('p_ok_current') or 0:.2f}  "
              f"passed {r['trial_passed']}")
    print(json.dumps(result['summary'], indent=2))
    if args.out:
        with args.out.open('x') as f:  # never overwrite evidence
            json.dump(result, f, indent=2)
            f.write('\n')
        print('replay:', args.out)


if __name__ == '__main__':
    main()
