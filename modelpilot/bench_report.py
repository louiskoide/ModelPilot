"""M6 benchmark report: cache carry-over, cold-equivalent cost and a paired task-level bootstrap.

Trials run one after another, so a trial can read prompt cache that an earlier trial on the
same model wrote. Measured cost then depends on run order. Cold-equivalent cost reprices
those inherited reads as the cache writes an isolated trial would have paid; output is
unchanged by caching, so nothing else differs. Measured cost is always reported alongside.

A request the API rejected with an error is unpriced, so the trial's headline cost stays
unknown; a separately labeled sensitivity figure counts such rejections as free. Jev arms
report provider cost only (router cost unpriced): their dollars are a lower bound.

    python3 -m modelpilot.bench_report runs/bench-<ts> [--out FILE]

rebuilds the summary from a run's trial records (for example after a crash). Evidence is
never overwritten.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import random
import statistics

TTL_SECONDS = {'5m': 300, '1h': 3600}
MIN_TASKS = 10  # below this a percentile bootstrap interval is too narrow to support a claim
ASSUMPTIONS = ('Cache entries are per model and effort. A request can use an earlier in-trial prefix '
               '(cache read + write) started within that entry\'s TTL (300 s, or 3600 s for 1h writes); '
               'reads beyond it were inherited and are repriced as writes at the request\'s write TTL.')


def cache_attribution(rows, rates):
    """Inherited cache reads in one trial's proxy rows, and the cost had the trial started cold."""
    messages = sorted((r for r in rows if r.get('kind') == 'messages'), key=lambda r: r.get('started_unix', 0))
    entries, last_ttl = {}, {}
    carried_total, extra, unknown, start, rejected = 0, 0.0, None, None, 0
    for r in messages:
        if r.get('cost_usd') is None:
            if isinstance(r.get('http_status'), int) and r['http_status'] != 200:
                rejected += 1  # answered with an error: unconfirmed whether billed
            else:
                unknown = unknown or 'unpriced_request'
        usage = r.get('usage') or {}
        if r.get('http_status') != 200 or 'cache_read_input_tokens' not in usage:
            continue
        key = (r.get('model'), r.get('effort'))
        read, write = usage['cache_read_input_tokens'], usage.get('cache_creation_input_tokens', 0)
        split = usage.get('cache_creation')
        if split:
            five, hour = split['ephemeral_5m_input_tokens'], split['ephemeral_1h_input_tokens']
            ttl = 'mixed' if five and hour else '1h' if hour else '5m' if five else last_ttl.get(key, '5m')
        else:
            ttl = None if write else last_ttl.get(key, '5m')
        now = r.get('started_unix', 0)
        usable = max((prefix for prefix, started, life in entries.get(key, ()) if now - started <= life), default=0)
        carried = max(0, read - usable)
        carried_total += carried
        if start is None:
            start = {'first_read_tokens': read, 'carried_tokens': carried, 'warm': read > 0}
        rate = rates.get(r.get('model'))
        if ttl is None:
            unknown = unknown or 'no_cache_write_breakdown'
        elif carried and ttl == 'mixed':
            unknown = unknown or 'mixed_cache_ttl'
        elif carried and rate is None:
            unknown = unknown or 'no_rates'
        elif carried:
            extra += carried * (rate['write_' + ttl] - rate['read']) / 1e6
        entries.setdefault(key, []).append((read + write, now, TTL_SECONDS['1h' if ttl == '1h' else '5m']))
        if ttl in TTL_SECONDS:
            last_ttl[key] = ttl
    priced = [r['cost_usd'] for r in messages if r.get('cost_usd') is not None]
    measured = sum(priced) if messages and len(priced) == len(messages) else None
    return {'cache_start': start, 'carried_read_tokens': carried_total, 'measured_cost_usd': measured,
            'cold_equivalent_cost_usd': measured + extra if measured is not None and unknown is None else None,
            'cold_equivalent_unknown': unknown or ('rejected_request' if rejected else None),
            'rejected_requests': rejected,
            'cold_equivalent_if_rejected_free_usd': sum(priced) + extra if messages and unknown is None else None,
            'assumptions': ASSUMPTIONS}


def cold_cost(record):
    if (record.get('accounting') or {}).get('cost_complete') is False:
        return None  # an adapter reported its total incomplete: cache repricing cannot complete it
    return (record.get('cache') or {}).get('cold_equivalent_cost_usd')


def cold_cost_if_rejected_free(record):
    if (record.get('accounting') or {}).get('cost_complete') is False:
        return None
    return (record.get('cache') or {}).get('cold_equivalent_if_rejected_free_usd')


def cells(records, cost_of=cold_cost):
    """Per-task aggregates for one arm: trials, passes, cold-equivalent cost (None if any unknown), wall time."""
    out = {}
    for r in records:
        cell = out.setdefault(r['task'], {'n': 0, 'passes': 0, 'cost': 0.0, 'wall': 0.0})
        cell['n'] += 1
        cell['passes'] += bool(r.get('passed'))
        cell['wall'] += r.get('wall_seconds') or 0
        cost = cost_of(r)
        cell['cost'] = None if cost is None or cell['cost'] is None else cell['cost'] + cost
    return out


def metrics(chosen):
    """Pass rate, mean cost per trial, cost per pass and mean wall time over a list of cells.

    Dollar metrics use only fully priced cells; unknown cost is excluded, never priced at zero.
    """
    n = sum(c['n'] for c in chosen)
    priced = [c for c in chosen if c['cost'] is not None]
    priced_n, cost, passes = sum(c['n'] for c in priced), sum(c['cost'] for c in priced), sum(c['passes'] for c in priced)
    return {'pass_rate': sum(c['passes'] for c in chosen) / n if n else None,
            'mean_cost_usd': cost / priced_n if priced_n else None,
            'cost_per_pass_usd': cost / passes if passes else None,
            'mean_wall_seconds': sum(c['wall'] for c in chosen) / n if n else None}


def percentile(values, q):
    values = sorted(values)
    position = (len(values) - 1) * q
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def interval(values):
    return [percentile(values, .025), percentile(values, .975)] if values else None


def bootstrap(tasks, compute, rng, resamples):
    """Percentile intervals for each metric compute() returns, resampling tasks with replacement."""
    draws = {}
    for _ in range(resamples):
        sample = [rng.choice(tasks) for _ in tasks]
        for name, value in compute(sample).items():
            draws.setdefault(name, [])
            if value is not None:
                draws[name].append(value)
    return {name: {'ci95': interval(values), 'undefined_resamples': resamples - len(values)} for name, values in draws.items()}


def difference(estimate, drawn, tasks):
    ci = drawn['ci95']
    excludes_zero = ci is not None and (ci[0] > 0 or ci[1] < 0)
    out = {'estimate': estimate, 'ci95': ci, 'shows_difference': excludes_zero and tasks >= MIN_TASKS}
    if tasks < MIN_TASKS:
        out['note'] = f'too few tasks (fewer than {MIN_TASKS})'
    elif not excludes_zero:
        out['note'] = 'no difference shown'
    if drawn['undefined_resamples']:
        out['undefined_resamples'] = drawn['undefined_resamples']
    return out


def cost_scope(records):
    """'complete', 'provider_only_router_unpriced' (Jev), 'mixed', or None without records."""
    scopes = {(r.get('accounting') or {}).get('cost_scope', 'complete') for r in records}
    return scopes.pop() if len(scopes) == 1 else 'mixed' if scopes else None


def routing_summary(records):
    """Jev routing across an arm's trials, or None for arms without a router."""
    routes = [r['routing'] for r in records if r.get('routing')]
    if not routes:
        return None
    tokens = Counter()
    for usage in (u for x in routes for u in x.get('router_usage') or []):
        tokens.update({k: v for k, v in usage.items() if isinstance(v, (int, float)) and not isinstance(v, bool)})
    return {'trials': len(routes), 'routed_trials': sum(bool(x.get('routed')) for x in routes),
            'fail_open_trials': sum(bool(x.get('fail_open')) for x in routes),
            # 'stub' only in offline tests; a stub decision is never evidence of TypeSafe routing.
            'routers': dict(Counter(x.get('router') for x in routes)),
            'decisions': sum(x.get('decisions') or 0 for x in routes),
            'extra_decisions': sum(x.get('extra_decisions') or 0 for x in routes),
            'served_models': dict(Counter(m for x in routes for m in x.get('served_models') or [])),
            'router_tokens': dict(tokens)}


def arm_summary(arm, complete, incomplete, arm_cells, rng, resamples):
    point = metrics(list(arm_cells.values()))
    if_free = metrics(list(cells(complete, cold_cost_if_rejected_free).values()))
    first_bytes = [s for r in complete for s in (r.get('accounting') or {}).get('first_byte_seconds') or [] if s is not None]
    measured = [r['accounting']['cost_usd'] for r in complete if (r.get('accounting') or {}).get('cost_usd') is not None]
    passes = sum(bool(r.get('passed')) for r in complete)
    unknown = [f"{r['task']}/{r.get('trial', 0)}" for r in complete if cold_cost(r) is None]
    tasks = sorted(arm_cells)
    drawn = bootstrap(tasks, lambda sample: metrics([arm_cells[t] for t in sample]), rng, resamples) if tasks else {}
    out = {'arm': arm, 'trials': len(complete), 'incomplete_trials': len(incomplete), 'passes': passes,
           'pass_rate': point['pass_rate'] if complete else None, 'cost_scope': cost_scope(complete),
           'mean_cost_usd': point['mean_cost_usd'], 'cost_per_pass_usd': point['cost_per_pass_usd'],
           # Sensitivity, not a headline: requests the API rejected with an error counted as free.
           'mean_cost_if_rejected_free_usd': if_free['mean_cost_usd'],
           'cost_per_pass_if_rejected_free_usd': if_free['cost_per_pass_usd'],
           'rejected_requests': sum((r.get('accounting') or {}).get('rejected_requests') or 0 for r in complete),
           'mean_measured_cost_usd': sum(measured) / len(measured) if measured else None,
           'cost_usd_priced_trials': sum(measured), 'unpriced_trials': len(complete) - len(measured),
           'cold_equivalent_unknown_trials': len(unknown), 'unknown_cost': unknown,
           'mean_wall_seconds': point['mean_wall_seconds'], 'wall_seconds': sum(r.get('wall_seconds') or 0 for r in complete),
           'median_first_byte_seconds': statistics.median(first_bytes) if first_bytes else None,
           'warm_starts': sum(bool(((r.get('cache') or {}).get('cache_start') or {}).get('warm')) for r in complete),
           'stops': dict(Counter(s.get('stop') for r in complete for s in r.get('sessions') or [])),
           'grade_reasons': dict(Counter((r.get('grade') or {}).get('reason') for r in complete)),
           'test_config_changed': sum(bool(r.get('test_config_changed')) for r in complete),
           'ci95': {name: d['ci95'] for name, d in drawn.items()}}
    routing = routing_summary(complete)
    if routing:
        out['routing'] = routing
    return out


def pair_summary(a, b, cells_a, cells_b, rng, resamples, scopes=('complete', 'complete')):
    shared = sorted(set(cells_a) & set(cells_b))
    priced = [t for t in shared if cells_a[t]['cost'] is not None and cells_b[t]['cost'] is not None]

    def compute(sample, dollars):
        left, right = metrics([cells_a[t] for t in sample]), metrics([cells_b[t] for t in sample])
        names = ('mean_cost_usd', 'cost_per_pass_usd') if dollars else ('pass_rate', 'mean_wall_seconds')
        return {n: left[n] - right[n] if left[n] is not None and right[n] is not None else None for n in names}
    differences = {}
    for tasks, dollars in ((shared, False), (priced, True)):
        if not tasks:
            continue
        point = compute(tasks, dollars)
        drawn = bootstrap(tasks, lambda sample: compute(sample, dollars), rng, resamples)
        differences.update({n: difference(point[n], drawn[n], len(tasks)) for n in point})
    lower = [arm for arm, scope in zip((a, b), scopes) if scope not in ('complete', None)]
    return {'arms': [a, b], 'tasks': len(shared), 'dollar_tasks': len(priced),
            'excluded_unpriced_tasks': len(shared) - len(priced),
            'dollar_basis': f"lower bound for {', '.join(lower)}: router cost unpriced" if lower else 'complete',
            'differences': differences}


def summarize(records, arms, seed=0, resamples=10000):
    """Per-arm results and paired differences with task-level bootstrap 95% intervals.

    Only complete trials are analysed; incomplete ones (stopped or crashed) are counted.
    Arms are paired on the tasks both ran. An interval covering 0, or fewer than MIN_TASKS
    paired tasks, shows no difference.
    """
    excluded = [r for r in records if (r.get('routing') or {}).get('benchmark_eligible') is False]
    records = [r for r in records if (r.get('routing') or {}).get('benchmark_eligible') is not False]
    rng = random.Random(seed)
    complete = {arm: [r for r in records if r['arm'] == arm and r.get('complete', True)] for arm in arms}
    incomplete = {arm: [r for r in records if r['arm'] == arm and not r.get('complete', True)] for arm in arms}
    by_arm = {arm: cells(complete[arm]) for arm in arms}
    summaries = [arm_summary(arm, complete[arm], incomplete[arm], by_arm[arm], rng, resamples) for arm in arms]
    scope = {s['arm']: s['cost_scope'] for s in summaries}
    return {'excluded_ineligible_trials': [{'task': r['task'], 'arm': r['arm'],
                'reason': 'adapter_not_benchmark_eligible', 'cost_usd': (r.get('accounting') or {}).get('cost_usd')} for r in excluded],
            'arms': summaries,
            'paired': [pair_summary(a, b, by_arm[a], by_arm[b], rng, resamples, (scope[a], scope[b]))
                       for i, a in enumerate(arms) for b in arms[i + 1:]],
            'bootstrap': {'seed': seed, 'resamples': resamples, 'unit': 'task', 'interval': '95% percentile'},
            'cost_basis': 'cold-equivalent (inherited cache reads repriced as writes); measured cost alongside',
            'claims': 'No winner or savings claim when an interval covers 0, or from the tuning split.'}


def load_run(run_dir, rates):
    """A run's manifest and trial records; cache attribution is recomputed from the proxy log."""
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir/'manifest.json').read_text())
    records = []
    for path in sorted(run_dir.glob('*/*/*/trial.json')):
        record = json.loads(path.read_text())
        record.setdefault('trial', int(path.parent.name))
        record.setdefault('complete', record.get('phase', 'graded') == 'graded')
        log = path.parent/'observations.jsonl'
        if log.exists():
            rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
            record['cache'] = cache_attribution(rows, rates)
        records.append(record)
    return manifest, records


def write_summary(run_dir, rates, out, resamples=10000):
    manifest, records = load_run(run_dir, rates)
    summary = summarize(records, list(manifest['arms']), seed=manifest.get('seed', 0), resamples=resamples)
    summary.update(trials=len(records), rebuilt_from=str(run_dir))
    with Path(out).open('x') as f:  # never overwrite evidence
        json.dump(summary, f, indent=2)
        f.write('\n')
    return summary


def main():
    from .bench import rates
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('run', type=Path)
    parser.add_argument('--out', type=Path, help='Write here (must not exist); default prints the summary')
    parser.add_argument('--resamples', type=int, default=10000)
    args = parser.parse_args()
    if args.out:
        write_summary(args.run, rates(), args.out, args.resamples)
        print('summary:', args.out)
        return
    manifest, records = load_run(args.run, rates())
    summary = summarize(records, list(manifest['arms']), seed=manifest.get('seed', 0), resamples=args.resamples)
    print(json.dumps(dict(summary, trials=len(records)), indent=2))


if __name__ == '__main__':
    main()
