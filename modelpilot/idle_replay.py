"""Idle-boundary replay over recorded proxy logs (October 15, overnight cloud run; $0). Evidence for the pending
idle-plan decision (docs/governor.md, "Idle plans: options for making one free"), not the decision: it recommends
nothing.

    python3 -m modelpilot.idle_replay runs/<run or session dir> [...] [--follow 0,0.25,0.5,0.75,1] [--out FILE]

It reads bench runs (any shape: single, follow-up, sequence, split; */*/*/trial.json beside observations.jsonl) and
governed sessions (summary.json and observations.jsonl, split by its invocations). A trial's sessions (a follow-up,
the turns of a sequence or split) are where the client waited for the user; between two of them:

- idle boundary: the gap from the last answered main-loop request's end to the next session's first one's start is at
  least the earlier request's cache lifetime's boundary (its breakpoints' cache_ttl, 1h if any asked for it; m5.
  idle_boundary: 330 s for 5m, 3,630 s for 1h);
- the rewrite it paid: that first request's writes of the prefix built before the gap (bench_report.write_sources's
  rule: min(write, built - read)), in tokens and at the row's own write rate;
- other rebuilds by cause, from write_sources over the whole trial (switch, rebuild_changed, rebuild_expired).

What each option would have saved (P is the follow rate, a parameter), the rewritten prefix priced at the old
setting's write rate on Sonnet 5.5 and Opus 5.5 at 5m and 1h, as docs/governor.md prices it:
- (a) a conditional notice at turn end: P x the boundaries' rewrites, less, where a pause turned out short, P x that
  prefix at the same rate (a switch that threw a warm entry away); a model change's other rate isn't modelled;
- (b) a hold at the boundary: P x the boundaries' rewrites (each saving needs a resend);
- (c) active mode on the boundary request: the boundaries' rewrites, with no user action (P plays no part);
- (d) leave it: 0.
Every recorded transition counts as if a change had been queued there. Unknown stays unknown: a transition whose rows
lack usage or a start time is counted apart.
"""
import argparse
import json
from pathlib import Path
from . import bench_report
from .cache_probe import prompt_tokens, tier
from .m5 import idle_boundary
from .proxy import read_log

# Write rates (USD per million tokens) the options are priced at, from configs/sonnet-5-5-rates.json and
# configs/opus-5-5-rates.json (October 15).
WRITE_RATES = {'claude-sonnet-5-5': {'5m': 2.5, '1h': 4}, 'claude-opus-5-5': {'5m': 5, '1h': 8}}
FOLLOW = (0, .25, .5, .75, 1)


def main_rows(rows):
    """Answered main-loop requests in start order; the client's compaction request (it reads the old context to
    summarize it) is not one."""
    return sorted((r for r in rows if r.get('kind') == 'messages' and r.get('tool_count') and r.get('http_status') == 200
                   and not r.get('compaction')), key=lambda r: r.get('started_unix') or 0)


def lifetime(row):
    return '1h' if '1h' in (row.get('cache_ttl') or ()) else '5m'


def transitions(rows, sessions, rates):
    """Each pair of consecutive sessions: the gap, whether it crossed the earlier request's boundary, the prefix built
    before it and what the next session's first request rewrote of it."""
    out = []
    spans = [rows[a:b] for a, b in sessions]
    for before, after in zip(spans, spans[1:]):
        prev, nxt = (main_rows(before) or [None])[-1], (main_rows(after) or [None])[0]
        if prev is None or nxt is None:
            out.append({'known': False, 'reason': 'no_answered_main_loop_request'})
            continue
        usage_prev, usage_next = prev.get('usage') or {}, nxt.get('usage') or {}
        needed = ('cache_read_input_tokens', 'cache_creation_input_tokens')
        if not all(isinstance(u.get(k), int) for u in (usage_prev, usage_next) for k in needed) or \
                not all(isinstance(r.get('started_unix'), (int, float)) for r in (prev, nxt)):
            out.append({'known': False, 'reason': 'usage_or_time_missing'})
            continue
        ended = prev['started_unix'] + (prev.get('wall_seconds') or 0)
        gap = nxt['started_unix'] - ended
        ttl = lifetime(prev)
        built = usage_prev['cache_read_input_tokens'] + usage_prev['cache_creation_input_tokens']
        write, read = usage_next['cache_creation_input_tokens'], usage_next['cache_read_input_tokens']
        rewritten = min(write, max(0, built - read))
        same = (prev.get('model'), prev.get('effort')) == (nxt.get('model'), nxt.get('effort'))
        rate = tier(rates.get(nxt.get('model')), prompt_tokens(usage_next), nxt.get('started_unix')) if rates.get(nxt.get('model')) else None
        split = usage_next.get('cache_creation')
        per_token = None
        if rate and isinstance(split, dict) and write:
            per_token = (split.get('ephemeral_5m_input_tokens', 0) * rate['write_5m'] +
                         split.get('ephemeral_1h_input_tokens', 0) * rate['write_1h']) / 1e6 / write
        out.append({'known': True, 'gap_seconds': round(gap, 1), 'ttl': ttl, 'boundary_seconds': idle_boundary(ttl),
                    'idle_boundary': gap >= idle_boundary(ttl), 'same_setting': same, 'model': nxt.get('model'),
                    'prefix_tokens': built, 'rewritten_tokens': rewritten,
                    'rewritten_usd': (rewritten * per_token if per_token is not None else None) if rewritten else 0.0})
    return out


def trial_units(path):
    """[(name, rows, session row ranges)] for a bench run, a governed session or a single trial directory."""
    path = Path(path)
    units = []
    for record_path in sorted(path.glob('*/*/*/trial.json')):
        record = json.loads(record_path.read_text())
        sessions = [s['rows'] for s in record.get('sessions') or [] if isinstance(s.get('rows'), list)]
        units.append((f"{path.name}/{record.get('task')}/{record.get('arm')}/{record.get('trial', 0)}",
                      read_log(record_path.parent/'observations.jsonl'), sessions))
    summary = path/'summary.json'
    if not units and summary.exists():
        report = json.loads(summary.read_text())
        sessions = [[i['rows_from'], i['rows_to']] for i in report.get('invocations') or [] if 'rows_to' in i]
        rows = read_log(path/'observations.jsonl')
        units.append((path.name, rows, sessions or [[0, len(rows)]]))
    return units


def option_savings(boundaries, short_pauses, follow=FOLLOW):
    """Dollars each option would have saved over these transitions, by model, lifetime and follow rate."""
    rewritten = sum(t['rewritten_tokens'] for t in boundaries)
    exposed = sum(t['prefix_tokens'] for t in short_pauses)
    out = {}
    for model, rates in WRITE_RATES.items():
        for ttl, rate in rates.items():
            gross, early = rewritten * rate / 1e6, exposed * rate / 1e6
            out[f'{model}/{ttl}'] = {
                'a_conditional_notice': {str(p): {'saved': round(p * gross, 6), 'early_switch_cost': round(p * early, 6),
                                                  'net': round(p * (gross - early), 6)} for p in follow},
                'b_prompt_hold': {str(p): round(p * gross, 6) for p in follow},
                'c_active_boundary_request': round(gross, 6),
                'd_leave_it': 0.0}
    return out


def replay(paths, rates, follow=FOLLOW):
    rows_out, all_boundaries, all_short, unknown = [], [], [], 0
    causes = dict.fromkeys(bench_report.WRITE_SOURCES, 0)
    causes_usd = dict.fromkeys(bench_report.WRITE_SOURCES, 0.0)
    for path in paths:
        for name, rows, sessions in trial_units(path):
            found = transitions(rows, sessions, rates)
            sources = bench_report.write_sources(rows, rates)
            for source in bench_report.WRITE_SOURCES:
                causes[source] += sources['tokens'][source]
                causes_usd[source] = (None if causes_usd[source] is None or sources['usd'][source] is None
                                      else causes_usd[source] + sources['usd'][source])
            known = [t for t in found if t['known']]
            unknown += len(found) - len(known)
            all_boundaries += [t for t in known if t['idle_boundary']]
            all_short += [t for t in known if not t['idle_boundary']]
            rows_out.append({'unit': name, 'sessions': len(sessions), 'transitions': found,
                             'unmeasured_requests': sources['unmeasured_requests']})
    paid = [t['rewritten_usd'] for t in all_boundaries]
    return {'units': len(rows_out), 'transitions': len(all_boundaries) + len(all_short) + unknown,
            'idle_boundaries': len(all_boundaries), 'short_pauses': len(all_short), 'unknown_transitions': unknown,
            'boundary_rewrites': {'tokens': sum(t['rewritten_tokens'] for t in all_boundaries),
                                  'usd_as_recorded': None if None in paid else round(sum(paid), 6)},
            'rebuilds_by_cause': {'tokens': causes, 'usd': causes_usd},
            'follow_rates': list(follow), 'options': option_savings(all_boundaries, all_short, follow),
            'assumptions': ('Every transition between a trial\'s sessions counts as if a change had been queued there. '
                            '(a) nets a short pause\'s early switch at the same write rate. Unknown transitions are '
                            'counted, never priced. No recommendation.'),
            'rows': rows_out}


def main():
    from .bench import rates
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('runs', type=Path, nargs='+', help='bench run directories or governed session directories')
    parser.add_argument('--follow', default=','.join(map(str, FOLLOW)), help='follow rates, comma-separated, 0-1')
    parser.add_argument('--out', type=Path, help='write the report here (must not exist)')
    args = parser.parse_args()
    follow = tuple(float(p) for p in args.follow.split(','))
    if any(not 0 <= p <= 1 for p in follow):
        parser.error('follow rates are between 0 and 1')
    result = replay(args.runs, rates(), follow)
    text = json.dumps(result, indent=1)
    if args.out:
        with args.out.open('x') as f:
            f.write(text + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'rows'}, indent=1))


if __name__ == '__main__':
    main()
