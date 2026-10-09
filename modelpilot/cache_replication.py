"""M0 replication on current models. Planning is free; --live prompts for a local key.

Suites: three-model (Haiku 4.5, Sonnet 5, Opus 5; run September 24), opus-5-5, haiku-5-5 (the same questions with
Haiku 5.5 at home: its effort changes, moves to Sonnet 5.5 and Opus 5.5 and back, its lifetime), and ttl-1h (plan
item 6): the one-hour lifetime on Sonnet 5.5 and Opus 5.5, and what a request marked with one lifetime does to an
entry written with the other (TTL_1H_KINDS).
"""
import argparse
import getpass
import heapq
import itertools
import json
import math
import os
from pathlib import Path
import time
import uuid
from . import cache_probe as probe

ROOT = Path(__file__).resolve().parents[1]
HAIKU, SONNET_5_5, OPUS_5_5 = 'claude-haiku-4-5-20251001', 'claude-sonnet-5-5', 'claude-opus-5-5'
HAIKU_5_5 = 'claude-haiku-5-5'
MODELS = [HAIKU, 'claude-sonnet-5', 'claude-opus-5']
RATES = {m: dict(input=i, output=o, write_5m=i*1.25, write_1h=i*2, read=i*.1)
         for m, i, o in zip(MODELS, [1, 2, 5], [5, 10, 25])}
# Opus 5.5 reads are 0.05x input, not 0.1x; writes use the standard multipliers (derived; confirm at launch).
RATES[OPUS_5_5] = dict(input=4, output=20, write_5m=5, write_1h=8, read=.2)
# Sonnet 5.5: Sonnet 5's prices at launch, cache reads $0.10 from October 7 (configs/sonnet-5-5-rates.json;
# cache_probe.dated).
RATES[SONNET_5_5] = json.loads((ROOT/'configs/sonnet-5-5-rates.json').read_text())['rates'][SONNET_5_5]
# Haiku 5.5: priced by prompt length (cache_probe.tier); every probe prompt here is far below its 100,000 tokens.
RATES[HAIKU_5_5] = json.loads((ROOT/'configs/haiku-5-5-rates.json').read_text())['rates'][HAIKU_5_5]
SOURCE = 'https://platform.claude.com/docs/en/build-with-claude/prompt-caching'
SUITES = ('three-model', 'opus-5-5', 'haiku-5-5', 'ttl-1h', 'ttl-1h-long')
# Suites with one model always "a", so every model group tests a return to it: (home, the models it moves to).
HOMES = {'opus-5-5': (OPUS_5_5, MODELS), 'haiku-5-5': (HAIKU_5_5, (SONNET_5_5, OPUS_5_5))}
# ttl-1h: each kind is an independent prefix, touched at these (lifetime marked, seconds after the previous request
# started) steps. The first request writes; the question is whether the last one reads.
TTL_1H_KINDS = {
    'past_5m': [('1h', 0), ('1h', 600)],  # an hour entry outlives five minutes: expect a read
    'before': [('1h', 0), ('1h', 3000)],  # 50 minutes: expect a read
    'after': [('1h', 0), ('1h', 3630)],  # 60.5 minutes: expect a write
    'refresh': [('1h', 0), ('1h', 2400), ('1h', 2400)],  # a read at 40 minutes keeps it to 80: expect reads
    'five_control': [('5m', 0), ('5m', 600)],  # a five-minute entry after 10 minutes: expect a write (M0: 330 s wrote)
    # Can a request marked 1h move a warm five-minute entry to the hour (a turn-end upgrade), and is that billed as a
    # read or as a write? Then 10 minutes idle, read with the default marker.
    'upgrade': [('5m', 0), ('1h', 60), ('5m', 600)],
    # Does a request marked 5m shorten an hour entry? Read at 60 s with the default marker, then 10 minutes idle.
    'downgrade': [('1h', 0), ('5m', 60), ('5m', 600)],
}
TTL_1H_MODELS = (SONNET_5_5, OPUS_5_5)
# ttl-1h-long: the three kinds that wait most of an hour, alone. The October 6 ttl-1h run measured the others; its
# long kinds were lost when the computer slept mid-wait (runs/m0-replication-ttl-1h-20261006-215100).
TTL_1H_LONG = ('before', 'after', 'refresh')


def plan(run_id, suite='three-model'):
    if suite not in SUITES:
        raise ValueError(f'Unknown suite {suite!r}')
    groups = []
    def add(name, steps, layer):
        requests = []
        for label, delay, model, effort in steps:
            p = probe.payload({'prefix_lines': 260, 'max_tokens': 32}, run_id+'/'+name,
                              model, effort, layer=layer)
            if model == HAIKU:
                p.pop('output_config')
            requests.append((label, delay, p))
        groups.append(dict(name=name, steps=requests))
    if suite in ('ttl-1h', 'ttl-1h-long'):
        kinds = {k: v for k, v in TTL_1H_KINDS.items() if suite == 'ttl-1h' or k in TTL_1H_LONG}
        for repeat in range(3):
            for model in TTL_1H_MODELS:
                for kind, steps in kinds.items():
                    name = f'ttl/1h/{repeat}/{model}/{kind}'
                    requests = [(f'touch_{i}_{ttl}', delay,
                                 probe.payload({'prefix_lines': 260, 'max_tokens': 32}, run_id+'/'+name, model, 'low',
                                               layer='messages', ttl=ttl))
                                for i, (ttl, delay) in enumerate(steps)]
                    groups.append(dict(name=name, steps=requests))
        return groups
    if suite in HOMES:
        # The home model is always "a", so every model group tests a return to it, which the
        # three-model order never did. `thinking` is omitted, as in the three-model suite.
        home, others = HOMES[suite]
        for repeat in range(3):
            for layer in ('system', 'messages'):
                add(f'effort/{repeat}/{home}/{layer}',
                    [(label, 0, home, effort) for label, effort in
                     [('cold','low'), ('warm','low'), ('changed','high'),
                      ('changed_warm','high'), ('return','low')]], layer)
                for b in others:
                    add(f'model/{repeat}/{home}/{b}/{layer}',
                        [(label, 0, model, 'low') for label, model in
                         [('a_cold',home), ('a_warm',home), ('b_cold',b), ('b_warm',b),
                          ('a_return',home)]], layer)
            for kind, gaps in [('before',[0,240]), ('after',[0,330]), ('refresh',[0,180,180])]:
                add(f'ttl/{repeat}/{home}/{kind}',
                    [(f'touch_{i}', gap, home, 'low') for i, gap in enumerate(gaps)], 'messages')
        return groups
    for repeat in range(3):
        for layer in ('system', 'messages'):
            for model in MODELS[1:]:
                add(f'effort/{repeat}/{model}/{layer}',
                    [(label, 0, model, effort) for label, effort in
                     [('cold','low'), ('warm','low'), ('changed','high'),
                      ('changed_warm','high'), ('return','low')]], layer)
            for a, b in itertools.combinations(MODELS, 2):
                add(f'model/{repeat}/{a}/{b}/{layer}',
                    [(label, 0, model, 'low') for label, model in
                     [('a_cold',a), ('a_warm',a), ('b_cold',b), ('b_warm',b), ('a_return',a)]], layer)
        for model in MODELS:
            for kind, gaps in [('before',[0,240]), ('after',[0,330]), ('refresh',[0,180,180])]:
                add(f'ttl/{repeat}/{model}/{kind}',
                    [(f'touch_{i}', gap, model, 'low') for i, gap in enumerate(gaps)], 'messages')
    return groups


class Budget:
    def __init__(self, limit):
        if not math.isfinite(limit) or limit <= 0:
            raise ValueError('Budget must be finite and positive')
        self.limit, self.spent = limit, 0

    def reserve(self, estimate):
        if self.spent + estimate > self.limit:
            raise RuntimeError('Budget admission stopped the run')


LATE_SECONDS = 60


def marked_ttl(p):
    """The lifetime a probe request's one breakpoint asks for."""
    block = (p.get('system') or p['messages'][0]['content'])[0]
    return block['cache_control'].get('ttl', '5m')


def ttl_summary(rows):
    """ttl-1h: per model and kind, each repeat's steps as (lifetime marked, seconds since the previous start,
    observation, 5m and 1h tokens written), and how many repeats read at their last step."""
    out = {}
    for r in rows:
        parts = r['group'].split('/')
        if parts[:2] != ['ttl', '1h']:
            continue
        repeat, model, kind = parts[2], parts[3], parts[4]
        split = (r.get('usage') or {}).get('cache_creation') or {}
        cell = out.setdefault(model, {}).setdefault(kind, {'repeats': {}})
        cell['repeats'].setdefault(repeat, []).append(
            [r.get('ttl'), None if r.get('since_previous_start_seconds') is None else round(r['since_previous_start_seconds']),
             r.get('observation'), split.get('ephemeral_5m_input_tokens'), split.get('ephemeral_1h_input_tokens'),
             bool(r.get('late'))])
    for kinds in out.values():
        for kind, cell in kinds.items():
            whole = [steps for steps in cell['repeats'].values() if len(steps) == len(TTL_1H_KINDS[kind])]
            complete = [steps for steps in whole if not any(step[5] for step in steps)]
            cell.update(complete=len(complete), late=len(whole) - len(complete), last_read=sum(steps[-1][2] in ('hit', 'partial_hit_and_write')
                                                              for steps in complete))
    return out


def execute(groups, out, budget, transport=probe.send):
    """Interleave independent prefixes, serialize HTTP calls; no retries or background threads."""
    rows, queue = [], []
    # Waits are scheduled on the wall clock: on macOS the monotonic clock stops while the computer sleeps, and a
    # wait counted on it then ends hours late in real time (the October 6 ttl-1h run). A step that starts more than
    # LATE_SECONDS after its planned gap is marked late, and ttl_summary leaves its repeat out.
    started = time.time()
    status, error = 'complete', None
    for i in range(len(groups)):
        heapq.heappush(queue, (started, i, 0, None))
    def summary():
        result = dict(status=status, calls=len(rows), planned_calls=sum(len(g['steps']) for g in groups),
                      known_cost_usd=budget.spent, cost_complete=all(r.get('cost_usd') is not None for r in rows),
                      wall_seconds=time.time()-started, error=error,
                      scope='Direct API cache observations; no Claude Code integration or savings claim.',
                      observations=[{k:r.get(k) for k in ('group','step','observation','since_previous_start_seconds')} for r in rows])
        tmp = out/'summary.tmp'
        tmp.write_text(json.dumps(result, indent=2)+'\n')
        tmp.replace(out/'summary.json')
        return result
    with (out/'observations.jsonl').open('x') as log:
        try:
            while queue:
                due, i, step, previous = heapq.heappop(queue)
                while due > time.time():
                    time.sleep(min(30, due-time.time()))
                group = groups[i]
                label, delay, p = group['steps'][step]
                rate = probe.tier(RATES[p['model']])  # priced by prompt length: the dearest tier
                # Conservative admission estimate: UTF-8 bytes as tokens plus framing allowance.
                # Estimated, not a provider-enforced billing ceiling.
                estimate = ((len(json.dumps(p).encode())+1024)*rate['write_5m'] + p['max_tokens']*rate['output'])/1e6
                budget.reserve(estimate)
                now, clock = time.time(), time.monotonic()
                row = dict(group=group['name'], step=label, model=p['model'], ttl=marked_ttl(p),
                           effort=p.get('output_config',{}).get('effort'), started_unix=time.time(),
                           since_previous_start_seconds=None if previous is None else now-previous,
                           planned_gap_seconds=None if previous is None else delay,
                           late=previous is not None and group['name'].startswith('ttl/') and now-previous > delay+LATE_SECONDS)
                try:
                    response, rid = transport(p, {})
                    usage = response['usage']
                    price = probe.priced_usage(p, response.get('model'), usage, RATES)
                    row.update(status='ok', request_id=rid, returned_model=response.get('model'), usage=usage,
                               cost_usd=price, observation=probe.observe(usage))
                    if price is not None:
                        budget.spent += price
                    if price is None or not (usage.get('cache_creation_input_tokens') or usage.get('cache_read_input_tokens')):
                        raise RuntimeError('Unpriced or uncached response; inspect metadata')
                except Exception as exc:
                    row.update(status='error', error_type=type(exc).__name__, **probe.error_details(exc))
                    raise
                finally:
                    row['wall_seconds'] = time.monotonic()-clock
                    rows.append(row)
                    log.write(json.dumps(row)+'\n'); log.flush()
                    print(group['name'], label, row['status'], row.get('observation',''), flush=True)
                if step+1 < len(group['steps']):
                    gap = group['steps'][step+1][1]
                    # TTL is measured from request start; record actual gaps, including scheduler delay.
                    due_next = max(time.time(), now+gap) if group['name'].startswith('ttl/') else float('-inf')
                    heapq.heappush(queue, (due_next, i, step+1, now))
                status = 'running'
                summary()
            status = 'complete'
        except (Exception, KeyboardInterrupt) as exc:
            status, error = 'stopped', type(exc).__name__
        return summary()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--suite', choices=SUITES, default='three-model')
    parser.add_argument('--budget', type=float, default=5)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    budget = Budget(args.budget)
    rid = str(uuid.uuid4())
    groups = plan(rid, args.suite)
    calls = sum(len(g['steps']) for g in groups)
    prefix = 'm0-replication-' + ('' if args.suite == 'three-model' else args.suite + '-')
    out = args.out or ROOT/'runs'/(prefix+time.strftime('%Y%m%d-%H%M%S'))
    out.mkdir(parents=True, exist_ok=False)
    manifest = dict(run_id=rid, suite=args.suite, live=args.live, calls=calls, budget_usd=args.budget,
                    rates=RATES, pricing_source=SOURCE,
                    pricing_checked='2026-10-08' if args.suite == 'haiku-5-5' else '2026-09-24', groups=groups,
                    thinking='The thinking parameter is omitted: Sonnet 5, Opus 5, Opus 5.5 and Haiku 5.5 then '
                             'run adaptive thinking by default (Opus 5.5 cannot disable it); Haiku 4.5 runs '
                             'without thinking and has no effort parameter.',
                    timing='Independent prefixes interleaved; TTL gaps from request start, actual gaps logged.')
    (out/'plan.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(f'Plan ({args.suite}): {calls} requests, three repeats, ${args.budget:g} admission budget; '
          f'results: {out}', flush=True)
    if not args.live:
        print('Dry-run only. Add --live to send paid requests. Allow roughly ' +
              ('90 minutes (the longest prefix waits 80 minutes; others run meanwhile). Keep the computer awake '
               '(caffeinate -i; a closed laptop lid still sleeps): a step that starts late is marked and left out.'
               if args.suite in ('ttl-1h', 'ttl-1h-long')
               else '10–20 minutes.'))
        return
    if not os.environ.get('ANTHROPIC_API_KEY'):
        os.environ['ANTHROPIC_API_KEY'] = getpass.getpass('Anthropic API key (hidden): ').strip()
    from .jev_route_check import check_anthropic_key
    problem = check_anthropic_key(os.environ['ANTHROPIC_API_KEY'])
    if problem:
        raise SystemExit(problem)
    result = execute(groups, out, budget)
    if args.suite in ('ttl-1h', 'ttl-1h-long'):
        rows = [json.loads(line) for line in (out/'observations.jsonl').read_text().splitlines() if line.strip()]
        (out/'ttl-summary.json').write_text(json.dumps(ttl_summary(rows), indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k != 'observations'}, indent=2))
    if result['status'] != 'complete':
        raise SystemExit(1)

if __name__ == '__main__':
    main()
