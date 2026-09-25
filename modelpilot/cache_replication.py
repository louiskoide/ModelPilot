"""M0 replication on current models. Planning is free; --live prompts for a local key.

Suites: three-model (Haiku 4.5, Sonnet 5, Opus 5; run September 24) and opus-5-5.
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
HAIKU, OPUS_5_5 = 'claude-haiku-4-5-20251001', 'claude-opus-5-5'
MODELS = [HAIKU, 'claude-sonnet-5', 'claude-opus-5']
RATES = {m: dict(input=i, output=o, write_5m=i*1.25, write_1h=i*2, read=i*.1)
         for m, i, o in zip(MODELS, [1, 2, 5], [5, 10, 25])}
# Opus 5.5 reads are 0.05x input, not 0.1x; writes use the standard multipliers (derived; confirm at launch).
RATES[OPUS_5_5] = dict(input=4, output=20, write_5m=5, write_1h=8, read=.2)
SOURCE = 'https://platform.claude.com/docs/en/build-with-claude/prompt-caching'
SUITES = ('three-model', 'opus-5-5')


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
    if suite == 'opus-5-5':
        # Opus 5.5 is always "a", so every model group tests a return to Opus, which the
        # three-model order never did. `thinking` is omitted, as in the three-model suite.
        for repeat in range(3):
            for layer in ('system', 'messages'):
                add(f'effort/{repeat}/{OPUS_5_5}/{layer}',
                    [(label, 0, OPUS_5_5, effort) for label, effort in
                     [('cold','low'), ('warm','low'), ('changed','high'),
                      ('changed_warm','high'), ('return','low')]], layer)
                for b in MODELS:
                    add(f'model/{repeat}/{OPUS_5_5}/{b}/{layer}',
                        [(label, 0, model, 'low') for label, model in
                         [('a_cold',OPUS_5_5), ('a_warm',OPUS_5_5), ('b_cold',b), ('b_warm',b),
                          ('a_return',OPUS_5_5)]], layer)
            for kind, gaps in [('before',[0,240]), ('after',[0,330]), ('refresh',[0,180,180])]:
                add(f'ttl/{repeat}/{OPUS_5_5}/{kind}',
                    [(f'touch_{i}', gap, OPUS_5_5, 'low') for i, gap in enumerate(gaps)], 'messages')
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


def execute(groups, out, budget, transport=probe.send):
    """Interleave independent prefixes, serialize HTTP calls; no retries or background threads."""
    rows, queue = [], []
    started = time.monotonic()
    status, error = 'complete', None
    for i in range(len(groups)):
        heapq.heappush(queue, (started, i, 0, None))
    def summary():
        result = dict(status=status, calls=len(rows), planned_calls=sum(len(g['steps']) for g in groups),
                      known_cost_usd=budget.spent, cost_complete=all(r.get('cost_usd') is not None for r in rows),
                      wall_seconds=time.monotonic()-started, error=error,
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
                while due > time.monotonic():
                    time.sleep(min(30, due-time.monotonic()))
                group = groups[i]
                label, delay, p = group['steps'][step]
                rate = RATES[p['model']]
                # Conservative admission estimate: UTF-8 bytes as tokens plus framing allowance.
                # Estimated, not a provider-enforced billing ceiling.
                estimate = ((len(json.dumps(p).encode())+1024)*rate['write_5m'] + p['max_tokens']*rate['output'])/1e6
                budget.reserve(estimate)
                now = time.monotonic()
                row = dict(group=group['name'], step=label, model=p['model'],
                           effort=p.get('output_config',{}).get('effort'), started_unix=time.time(),
                           since_previous_start_seconds=None if previous is None else now-previous)
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
                    row['wall_seconds'] = time.monotonic()-now
                    rows.append(row)
                    log.write(json.dumps(row)+'\n'); log.flush()
                    print(group['name'], label, row['status'], row.get('observation',''), flush=True)
                if step+1 < len(group['steps']):
                    gap = group['steps'][step+1][1]
                    # TTL is measured from request start; record actual gaps, including scheduler delay.
                    due_next = max(time.monotonic(), now+gap) if group['name'].startswith('ttl/') else float('-inf')
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
                    rates=RATES, pricing_source=SOURCE, pricing_checked='2026-09-24', groups=groups,
                    thinking='The thinking parameter is omitted: Sonnet 5, Opus 5 and Opus 5.5 then run '
                             'adaptive thinking by default (Opus 5.5 cannot disable it); Haiku runs '
                             'without thinking and has no effort parameter.',
                    timing='Independent prefixes interleaved; TTL gaps from request start, actual gaps logged.')
    (out/'plan.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(f'Plan ({args.suite}): {calls} requests, three repeats, ${args.budget:g} admission budget; '
          f'results: {out}', flush=True)
    if not args.live:
        print('Dry-run only. Add --live to send paid requests. Allow roughly 10–20 minutes.')
        return
    if not os.environ.get('ANTHROPIC_API_KEY'):
        os.environ['ANTHROPIC_API_KEY'] = getpass.getpass('Anthropic API key (hidden): ').strip()
    from .jev_route_check import check_anthropic_key
    problem = check_anthropic_key(os.environ['ANTHROPIC_API_KEY'])
    if problem:
        raise SystemExit(problem)
    result = execute(groups, out, budget)
    print(json.dumps({k:v for k,v in result.items() if k != 'observations'}, indent=2))
    if result['status'] != 'complete':
        raise SystemExit(1)

if __name__ == '__main__':
    main()
