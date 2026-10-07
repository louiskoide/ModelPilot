"""M6 benchmark report: cache carry-over, cold-equivalent cost and a paired task-level bootstrap.

Trials run one after another, so a trial can read prompt cache that an earlier trial on the
same model wrote. Measured cost then depends on run order. Cold-equivalent cost reprices
those inherited reads as the cache writes an isolated trial would have paid; output is
unchanged by caching, so nothing else differs. Measured cost is always reported alongside.

A request the API rejected with an error is unpriced, so the trial's headline cost stays
unknown; a separately labeled sensitivity figure counts such rejections as free. Jev arms
report provider cost only (router cost unpriced): their dollars are a lower bound. Trials run
on a Claude subscription instead of an API key are priced as an API key would have been
billed for the same tokens (api_key_equivalent); their as-sent price is kept alongside.

    python3 -m modelpilot.bench_report runs/bench-<ts> [runs/bench-<ts2> ...] [--out FILE]

rebuilds the summary from a run's trial records (for example after a crash). Given several
runs, it pairs their arms by task; an arm in more than one run is labeled arm@<run>. Evidence
is never overwritten.

A trial passes on the hidden grader and, where its task has an edge suite, every edge test (user
decision, October 4). Runs graded before then carry the hidden verdict only; their edge results
come from the run's latest re-grade (runs/regrade-<run>-*/), and a trial with none is counted on
its hidden tests and labeled edge_missing. Hidden-test passes are reported alongside.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import random
import statistics
from . import regrade
from .cache_probe import cost

TTL_SECONDS = {'5m': 300, '1h': 3600}
EDGE_ROOT = regrade.EDGE
MIN_TASKS = 10  # below this a percentile bootstrap interval is too narrow to support a claim
ASSUMPTIONS = ('Cache entries are per model and effort. A request can use an earlier in-trial prefix '
               '(cache read + write) started within that entry\'s TTL (300 s, or 3600 s for 1h writes); '
               'reads beyond it were inherited and are repriced as writes at the request\'s write TTL.')


def api_key_equivalent(rows, rates):
    """A subscription trial's proxy rows as an API key would have been billed for them.

    Logged in with a subscription, Claude Code (2.1.284, checked at $0 against the owned fixture)
    marks every cache breakpoint ttl 1h; with an API key it uses the default 5 minutes. Nothing else
    in the request differs. So each 1h cache write moves to the 5m rate and its row is repriced;
    tokens and everything unpriced stay as measured."""
    out = []
    for r in rows:
        usage = r.get('usage') if isinstance(r.get('usage'), dict) else {}
        split = usage.get('cache_creation') if isinstance(usage.get('cache_creation'), dict) else {}
        hour = split.get('ephemeral_1h_input_tokens') or 0
        if r.get('kind') != 'messages' or not hour:
            out.append(r)
            continue
        usage = dict(usage, cache_creation=dict(split, ephemeral_1h_input_tokens=0,
                                                ephemeral_5m_input_tokens=(split.get('ephemeral_5m_input_tokens') or 0) + hour))
        row = dict(r, usage=usage, repriced_1h_write_tokens=hour)
        if r.get('cost_usd') is not None:
            try:
                row['cost_usd'] = cost(usage, rates.get(r.get('model')), '5m')
            except (KeyError, TypeError, ValueError):
                row['cost_usd'] = None
        out.append(row)
    return out


def priced_rows(record, rows, rates):
    """The rows a trial's dollar figures come from: as measured, or as an API key would have been billed. An arm
    that sets its cache lifetime (prompt_cache_ttl) sends the same writes with an API key, so it is priced as sent."""
    if record.get('auth') == 'subscription' and not record.get('prompt_cache_ttl'):
        return api_key_equivalent(rows, rates)
    return rows


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
    # The ModelPilot policy's consults and handoff notes, sent within the trial: counted as measured.
    sides = [r.get('cost_usd') for r in rows if r.get('kind') == 'side_call' and r.get('status') != 'refused']
    if None in sides:
        unknown = unknown or 'unpriced_side_call'
    priced += [c for c in sides if c is not None]
    measured = sum(priced) if messages and len(priced) == len(messages) + len(sides) else None
    return {'cache_start': start, 'carried_read_tokens': carried_total, 'measured_cost_usd': measured,
            'cold_equivalent_cost_usd': measured + extra if measured is not None and unknown is None else None,
            'cold_equivalent_unknown': unknown or ('rejected_request' if rejected else None),
            'rejected_requests': rejected,
            'cold_equivalent_if_rejected_free_usd': sum(priced) + extra if messages and unknown is None else None,
            'assumptions': ASSUMPTIONS}


def _add(total, cost):
    return None if total is None or cost is None else total + cost


# Policy deferrals that are the normal state, not a rung the policy wanted and could not apply.
IDLE_DEFERRALS = ('no_executable_escalation', 'unacknowledged_revision')


def setting_path(rows):
    """The model/effort settings one trial's main loop ran at, in order.

    Consecutive main-loop requests at one setting form a step. Requests without tools are Claude Code's
    own side calls, counted apart. A rung the policy wanted but did not apply is counted per request
    under its reason (the stuck window stays open, so it recurs until applied or the task ends).
    """
    messages = sorted((r for r in rows if r.get('kind') == 'messages'), key=lambda r: r.get('started_unix', 0))
    steps, side, deferred = [], {'requests': 0, 'cost_usd': 0.0}, Counter()
    for r in messages:
        policy = r.get('policy') or {}
        reason = policy.get('escalation_deferred') or (policy.get('reason') if policy.get('status') == 'deferred' else None)
        if reason and reason not in IDLE_DEFERRALS:
            deferred['thinking_history_unverified' if reason.startswith('refused:Thinking history') else reason] += 1
        if r.get('tool_count'):
            setting = [r.get('model'), r.get('effective_effort') or r.get('effort')]  # per-message effort, if set
            if not steps or steps[-1]['setting'] != setting:
                steps.append({'setting': setting, 'requests': 0, 'cost_usd': 0.0})
            bucket = steps[-1]
        else:
            bucket = side
        bucket['requests'] += 1
        bucket['cost_usd'] = _add(bucket['cost_usd'], r.get('cost_usd'))
    out = {'steps': steps, 'side': side, 'deferred_escalations': dict(deferred)}
    delegated = [{k: r.get(k) for k in ('purpose', 'model', 'effort', 'status', 'cost_usd')}
                 for r in sorted(rows, key=lambda r: r.get('started_unix', 0)) if r.get('kind') == 'side_call']
    if delegated:  # the ModelPilot policy's consults and handoff notes
        out['delegation'] = delegated
    return out


def cost_components(rows, rates):
    """Measured dollars by kind of token: uncached input, cache writes, cache reads and output.

    Priced requests only, with the rates they were priced at; None when a priced request can't be split.
    """
    out = {'input': 0.0, 'cache_write': 0.0, 'cache_read': 0.0, 'output': 0.0}
    for r in rows:
        if r.get('kind') not in ('messages', 'side_call') or r.get('cost_usd') is None:
            continue
        usage, rate = r.get('usage') or {}, rates.get(r.get('model'))
        split = usage.get('cache_creation') or ({'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 0}
                                                if not usage.get('cache_creation_input_tokens') else None)
        if rate is None or split is None:
            return None
        out['input'] += usage.get('input_tokens', 0) * rate['input'] / 1e6
        out['cache_write'] += (split['ephemeral_5m_input_tokens'] * rate['write_5m'] +
                               split['ephemeral_1h_input_tokens'] * rate['write_1h']) / 1e6
        out['cache_read'] += usage.get('cache_read_input_tokens', 0) * rate['read'] / 1e6
        out['output'] += usage.get('output_tokens', 0) * rate['output'] / 1e6
    return out


WRITE_SOURCES = ('cold_start', 'switch', 'growth', 'rebuild_expired', 'rebuild_changed', 'side')


def write_sources(rows, rates):
    """Where one trial's cache writes came from, in tokens and dollars.

    Per model and top-level effort (one cache entry each), a main-loop request is expected to read the whole
    prefix the previous request there built (its read + write). Its write is then growth: content added since,
    the earlier output and tool results included. Any shortfall was written again: rebuild_expired when that
    prefix was older than its TTL, else rebuild_changed (earlier content changed). The trial's first main-loop
    request is cold_start; the first at any later setting is switch. Requests without tools are Claude Code's
    own side calls. Dollars are None when a writing row has no write breakdown or no rates."""
    messages = sorted((r for r in rows if r.get('kind') in ('messages', 'side_call') and r.get('http_status') == 200
                       and 'cache_read_input_tokens' in (r.get('usage') or {})), key=lambda r: r.get('started_unix', 0))
    tokens, dollars, last = dict.fromkeys(WRITE_SOURCES, 0), dict.fromkeys(WRITE_SOURCES, 0.0), {}
    for r in messages:
        usage = r['usage']
        read, write = usage['cache_read_input_tokens'], usage.get('cache_creation_input_tokens') or 0
        split, rate = usage.get('cache_creation'), rates.get(r.get('model'))
        if not write:
            per_token = 0.0
        elif split and rate:
            per_token = (split['ephemeral_5m_input_tokens'] * rate['write_5m'] +
                         split['ephemeral_1h_input_tokens'] * rate['write_1h']) / 1e6 / write
        else:
            per_token = None
        key, now = (r.get('model'), r.get('effort')), r.get('started_unix', 0)
        if not r.get('tool_count') or r['kind'] == 'side_call':  # the policy's consults and notes count as side
            parts = {'side': write}
        elif key not in last:
            parts = {'switch' if last else 'cold_start': write}
        else:
            built, started, life = last[key]
            lost = min(write, max(0, built - read))
            parts = {'rebuild_expired' if now - started > life else 'rebuild_changed': lost, 'growth': write - lost}
        if r.get('tool_count'):
            hour = bool(split and split['ephemeral_1h_input_tokens'] or r.get('repriced_1h_write_tokens'))  # as sent
            last[key] = (read + write, now, TTL_SECONDS['1h' if hour else '5m'])
        for source, n in parts.items():
            tokens[source] += n
            dollars[source] = None if dollars[source] is None or (n and per_token is None) else dollars[source] + n * (per_token or 0)
    return {'tokens': tokens, 'usd': dollars}


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
    """'complete', 'provider_only_router_unpriced' (Jev), 'api_key_equivalent' (subscription), 'mixed', or None."""
    scopes = {(r.get('accounting') or {}).get('cost_scope', 'complete') for r in records}
    return scopes.pop() if len(scopes) == 1 else 'mixed' if scopes else None


def routing_summary(records):
    """Jev routing across an arm's trials, or None for arms without a router."""
    routes = [r['routing'] for r in records if r.get('routing') and r['routing'].get('kind') != 'modelpilot_policy']
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


def policy_summary(records):
    """The ModelPilot arm's policy actions across its trials, or None for other arms."""
    policies = [r['routing']['policy'] for r in records
                if (r.get('routing') or {}).get('kind') == 'modelpilot_policy' and r['routing'].get('policy')]
    if not policies:
        return None
    return {'trials': len(policies),
            'escalated_trials': sum(any(e['status'] == 'confirmed' for e in p['escalations']) for p in policies),
            'kept_requests': sum(p['kept_requests'] for p in policies),
            'policy_stops': sum(bool(p['stops']) for p in policies),
            'escalations': dict(Counter(f"{e['action']}:{e['status']}" for p in policies for e in p['escalations'])),
            'refusals': dict(Counter(x for p in policies for x in p['refusals'])),
            # Jev's advice at each decision point and what the switch policy did with it.
            'decisions': dict(Counter(f"{d.get('trigger')}:{d.get('action')}:{d.get('reason')}"
                                      for p in policies for d in p.get('decisions') or []))}


def outcome(record):
    """How a ModelPilot trial ended: passed or not, at its start setting or after climbing, or its session stop."""
    stop = next((s.get('stop') for s in reversed(record.get('sessions') or []) if s.get('stop')), None)
    if 'path' not in record:
        climbed = ''
    else:
        climbed = '_after_climbing' if len(record['path']['steps']) > 1 else '_at_start'
    if record.get('passed'):
        return 'passed' + climbed
    if stop and stop != 'success':
        return stop  # policy_stop, budget_stop, turn_limit, timeout, ...
    return 'finished_failing' + climbed  # the client ended normally but the hidden tests failed


def trial_view(record):
    """One ModelPilot trial: its setting path, what it spent on steps it later left, and its excerpts."""
    path = record.get('path') or {}
    steps = path.get('steps') or []
    left = steps[:-1]
    calls = ((record.get('routing') or {}).get('tools') or {}).get('calls') or []
    cost = 0.0
    for step in left:
        cost = _add(cost, step['cost_usd'])
    return {'task': record['task'], 'trial': record.get('trial', 0), 'passed': bool(record.get('passed')),
            'cost_usd': cold_cost(record), 'outcome': outcome(record),
            'path': [dict(s, setting='/'.join(str(x) for x in s['setting'])) for s in steps],
            'climb': {'steps_left': len(left), 'requests': sum(s['requests'] for s in left), 'cost_usd': cost} if left else None,
            'deferred_escalations': path.get('deferred_escalations') or {},
            'excerpts': {'cut_outputs': sum(bool(c.get('truncated')) for c in calls),
                         'cut_test_output': any(c.get('tool') == 'run_tests' and c.get('truncated') for c in calls),
                         'expanded': sum(c.get('tool') == 'expand_output' for c in calls)},
            'cost_components': record.get('cost_components')}


def modelpilot_breakdown(arm, trials, others):
    """Per-task results of one ModelPilot arm against every other arm on the same tasks.

    A task is lost when the arm failed it and another arm passed it; it is costlier when the arm passed
    but another arm passed it for less. Climbing cost is what trials spent on settings they later left.
    """
    views = [trial_view(r) for r in trials]
    mine = cells(trials)
    tasks = {}
    for task, cell in sorted(mine.items()):
        passing = {a: c[task] for a, c in others.items() if task in c and c[task]['passes'] == c[task]['n']}
        priced = {a: c['cost'] / c['n'] for a, c in passing.items() if c['cost'] is not None}
        cheapest = min(priced, key=priced.get) if priced else None
        tasks[task] = {'passed': cell['passes'] == cell['n'],
                       'mean_cost_usd': cell['cost'] / cell['n'] if cell['cost'] is not None else None,
                       'outcomes': [v['outcome'] for v in views if v['task'] == task],
                       'other_arms_passed': sorted(passing), 'cheapest_passing_arm': cheapest,
                       'cheapest_passing_cost_usd': priced.get(cheapest)}
    climbed = [v for v in views if v['climb']]
    climb_costs = [v['climb']['cost_usd'] for v in climbed]
    components = [v['cost_components'] for v in views if v['cost_components'] is not None]
    return {
        'arm': arm, 'trials': views, 'tasks': tasks,
        'outcomes': dict(Counter(v['outcome'] for v in views)),
        'lost_tasks': {t: v['outcomes'] for t, v in tasks.items() if not v['passed'] and v['other_arms_passed']},
        'costlier_tasks': {t: {'cost_usd': v['mean_cost_usd'], 'cheapest_passing_arm': v['cheapest_passing_arm'],
                               'cheapest_passing_cost_usd': v['cheapest_passing_cost_usd']}
                           for t, v in tasks.items() if v['passed'] and v['mean_cost_usd'] is not None
                           and v['cheapest_passing_cost_usd'] is not None
                           and v['mean_cost_usd'] > v['cheapest_passing_cost_usd']},
        'climbing': {'trials': len(climbed), 'requests_on_left_steps': sum(v['climb']['requests'] for v in climbed),
                     'cost_on_left_steps_usd': None if None in climb_costs else sum(climb_costs),
                     'deferred_escalation_requests': dict(sum((Counter(v['deferred_escalations']) for v in views), Counter()))},
        'excerpts': {'trials_with_cut_output': sum(bool(v['excerpts']['cut_outputs']) for v in views),
                     'trials_that_expanded': sum(bool(v['excerpts']['expanded']) for v in views),
                     'failed_after_cut_test_output': sum(v['excerpts']['cut_test_output'] and not v['passed'] for v in views)},
        'cost_components_usd': {k: sum(c[k] for c in components) for k in ('input', 'cache_write', 'cache_read', 'output')}
                               if components else None,
        'cost_components_trials': len(components)}


def start_point_ceiling(by_arm, candidates):
    """The most a perfect per-task choice among start points could gain over always using one.

    For each task it takes the candidate with the highest pass rate, then the lowest mean cost. The choice
    is made after seeing the outcome, and with one trial per task it also picks up run-to-run noise, so
    any real picker gains less. Only tasks every candidate ran with a known cost are compared.
    """
    shared = set.intersection(*(set(by_arm[a]) for a in candidates))
    tasks = sorted(t for t in shared if all(by_arm[a][t]['cost'] is not None for a in candidates))
    if not tasks:
        return None
    picks = {t: min(candidates, key=lambda a: (-by_arm[a][t]['passes'] / by_arm[a][t]['n'],
                                                by_arm[a][t]['cost'] / by_arm[a][t]['n'])) for t in tasks}

    def totals(choice):
        chosen = [by_arm[choice[t]][t] for t in tasks]
        n = sum(c['n'] for c in chosen)
        return {'passes': sum(c['passes'] for c in chosen), 'trials': n, 'mean_cost_usd': sum(c['cost'] for c in chosen) / n}
    best = totals(picks)
    always = {a: totals(dict.fromkeys(tasks, a)) for a in candidates}
    return {'candidates': list(candidates), 'tasks': len(tasks), 'excluded_tasks': len(set.union(*(set(by_arm[a]) for a in candidates))) - len(tasks),
            'picks': picks, 'best_per_task': best, 'always': always,
            'ceiling': {a: {'extra_passes': best['passes'] - f['passes'],
                            'mean_cost_saving_usd': f['mean_cost_usd'] - best['mean_cost_usd']} for a, f in always.items()},
            'note': 'Upper bound: chosen after the outcome; a negative saving means the extra passes cost more.'}


def write_source_totals(records):
    """An arm's cache writes by source over its trials (measured, not cold-equivalent: inherited reads are not
    writes), with each source's share of the written tokens. Dollars cover the trials that could all be priced."""
    found = [r['write_sources'] for r in records if r.get('write_sources')]
    if not found:
        return None
    tokens = {k: sum(w['tokens'][k] for w in found) for k in WRITE_SOURCES}
    priced = [w['usd'] for w in found if None not in w['usd'].values()]
    total = sum(tokens.values())
    return {'trials': len(found), 'tokens': tokens, 'share': {k: n / total for k, n in tokens.items()} if total else None,
            'tokens_per_trial': total / len(found), 'usd': {k: sum(u[k] for u in priced) for k in WRITE_SOURCES},
            'priced_trials': len(priced)}


def arm_summary(arm, complete, incomplete, arm_cells, rng, resamples):
    point = metrics(list(arm_cells.values()))
    if_free = metrics(list(cells(complete, cold_cost_if_rejected_free).values()))
    first_bytes = [s for r in complete for s in (r.get('accounting') or {}).get('first_byte_seconds') or [] if s is not None]
    measured = [r['accounting']['cost_usd'] for r in complete if (r.get('accounting') or {}).get('cost_usd') is not None]
    passes = sum(bool(r.get('passed')) for r in complete)
    hidden_passes = sum(hidden_passed(r) for r in complete)
    unknown = [f"{r['task']}/{r.get('trial', 0)}" for r in complete if cold_cost(r) is None]
    tasks = sorted(arm_cells)
    drawn = bootstrap(tasks, lambda sample: metrics([arm_cells[t] for t in sample]), rng, resamples) if tasks else {}
    out = {'arm': arm, 'trials': len(complete), 'incomplete_trials': len(incomplete), 'passes': passes,
           'hidden_passes': hidden_passes, 'pass_rules': dict(Counter(r.get('pass_rule', 'hidden') for r in complete)),
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
           'write_sources': write_source_totals(complete),
           'ci95': {name: d['ci95'] for name, d in drawn.items()}}
    routing = routing_summary(complete)
    if routing:
        out['routing'] = routing
    policy = policy_summary(complete)
    if policy:
        out['policy'] = policy
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
    lower = [arm for arm, scope in zip((a, b), scopes) if scope not in ('complete', 'api_key_equivalent', None)]
    equivalent = [arm for arm, scope in zip((a, b), scopes) if scope == 'api_key_equivalent']
    basis = ([f"lower bound for {', '.join(lower)}: router cost unpriced"] if lower else []) + (
        [f"API-key equivalent for {', '.join(equivalent)}: subscription trials, 1h cache writes priced at the 5m rate"]
        if equivalent else [])
    return {'arms': [a, b], 'tasks': len(shared), 'dollar_tasks': len(priced),
            'excluded_unpriced_tasks': len(shared) - len(priced),
            'dollar_basis': '; '.join(basis) or 'complete',
            'differences': differences}


def ineligible_reason(record):
    """Why a trial is excluded from comparisons, or None: its router or adapter says so, or a fixed arm's
    requested effort, appended system prompt or cache lifetime didn't reach the wire."""
    routing = record.get('routing') or {}
    if routing.get('benchmark_eligible') is False:
        return routing.get('ineligible_reason', 'adapter_not_benchmark_eligible')
    if (record.get('effort_check') or {}).get('applied') is False:
        return 'effort_not_applied'
    if (record.get('prompt_check') or {}).get('applied') is False:
        return 'prompt_not_applied'
    if (record.get('ttl_check') or {}).get('applied') is False:
        return 'ttl_not_applied'
    return None


def summarize(records, arms, seed=0, resamples=10000):
    """Per-arm results and paired differences with task-level bootstrap 95% intervals.

    Only complete trials are analysed; incomplete ones (stopped or crashed) are counted.
    Arms are paired on the tasks both ran. An interval covering 0, or fewer than MIN_TASKS
    paired tasks, shows no difference.
    """
    excluded = [r for r in records if ineligible_reason(r)]
    records = [r for r in records if not ineligible_reason(r)]
    rng = random.Random(seed)
    complete = {arm: [r for r in records if r['arm'] == arm and r.get('complete', True)] for arm in arms}
    incomplete = {arm: [r for r in records if r['arm'] == arm and not r.get('complete', True)] for arm in arms}
    by_arm = {arm: cells(complete[arm]) for arm in arms}
    summaries = [arm_summary(arm, complete[arm], incomplete[arm], by_arm[arm], rng, resamples) for arm in arms]
    scope = {s['arm']: s['cost_scope'] for s in summaries}
    policy_arms = [a for a in arms if any((r.get('routing') or {}).get('kind') == 'modelpilot_policy' for r in complete[a])]
    out = {'excluded_ineligible_trials': [{'task': r['task'], 'arm': r['arm'],
                'reason': ineligible_reason(r),
                'cost_usd': (r.get('accounting') or {}).get('cost_usd')} for r in excluded],
            'arms': summaries,
            'paired': [pair_summary(a, b, by_arm[a], by_arm[b], rng, resamples, (scope[a], scope[b]))
                       for i, a in enumerate(arms) for b in arms[i + 1:]],
            'bootstrap': {'seed': seed, 'resamples': resamples, 'unit': 'task', 'interval': '95% percentile'},
            'cost_basis': 'cold-equivalent (inherited cache reads repriced as writes); measured cost alongside',
            'claims': 'No winner or savings claim when an interval covers 0, or from the tuning split.'}
    if policy_arms:
        out['modelpilot'] = [modelpilot_breakdown(a, complete[a], {b: by_arm[b] for b in arms if b != a}) for a in policy_arms]
    if len(policy_arms) > 1:
        out['start_points'] = start_point_ceiling(by_arm, policy_arms)
    return out


def hidden_passed(record):
    return bool((record.get('grade') or {}).get('passed', record.get('passed')))


def latest_regrade(run_dir):
    found = sorted(Path(run_dir).parent.glob(f'regrade-{Path(run_dir).name}-*/results.json'))
    return found[-1] if found else None


def apply_pass_rule(record, edge=None):
    """passed = the hidden grader and, where the task has an edge suite, all of it (see the module docstring)."""
    if record.get('shape') == 'sequence':  # graded per subtask (long_session); passed means every subtask passed
        return record
    hidden = hidden_passed(record)
    record['grade'] = dict(record.get('grade') or {}, passed=hidden)  # older records kept it only as 'passed'
    edge = record.get('edge') or edge
    if not regrade.edge_files(record['task'], EDGE_ROOT):
        record['pass_rule'] = 'hidden'
    elif edge is None:
        record.update(pass_rule='edge_missing', passed=hidden)
    else:
        record.update(pass_rule='hidden_and_edge', edge=edge, passed=hidden and regrade.edge_all_passed(edge))
    return record


def load_run(run_dir, rates):
    """A run's manifest and trial records; cache attribution is recomputed from the proxy log."""
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir/'manifest.json').read_text())
    regraded = latest_regrade(run_dir)
    edges = {} if regraded is None else {(t['task'], t['arm'], t['trial']): t['edge']
                                         for t in json.loads(regraded.read_text())['trials'] if t.get('edge')}
    manifest['edge_from'] = str(regraded) if regraded else None
    records = []
    for path in sorted(run_dir.glob('*/*/*/trial.json')):
        record = json.loads(path.read_text())
        record.setdefault('trial', int(path.parent.name))
        record.setdefault('complete', record.get('phase', 'graded') == 'graded')
        log = path.parent/'observations.jsonl'
        if log.exists():
            rows = priced_rows(record, [json.loads(line) for line in log.read_text().splitlines() if line.strip()], rates)
            record.update(cache=cache_attribution(rows, rates), path=setting_path(rows),
                          cost_components=cost_components(rows, rates), write_sources=write_sources(rows, rates))
        records.append(apply_pass_rule(record, edges.get((record['task'], record['arm'], record['trial']))))
    return manifest, records


def load_runs(run_dirs, rates):
    """Several runs' trial records as one set of arms, paired by task.

    An arm that appears in more than one run is labeled arm@<run directory name>, so the same arm
    measured before and after a change stays apart. The runs ran at different times, possibly on
    different code, clients and auth; each run's manifest facts are listed with the arms it gave.
    The seed is the first run's."""
    loaded = [(Path(d),) + load_run(d, rates) for d in run_dirs]
    seen = Counter(a for _, manifest, _ in loaded for a in manifest['arms'])
    arms, records, runs = [], [], []
    for path, manifest, run_records in loaded:
        label = {a: f'{a}@{path.name}' if seen[a] > 1 else a for a in manifest['arms']}
        arms += label.values()
        records += [dict(r, arm=label[r['arm']]) for r in run_records if r['arm'] in label]
        runs.append({'run': str(path), 'arms': list(label.values()), 'code': manifest.get('code'),
                     'client_version': manifest.get('client_version'), 'auth': manifest.get('auth'),
                     'edge_from': manifest.get('edge_from')})
    return loaded[0][1].get('seed', 0), arms, records, runs


def summary_of(run_dirs, rates, resamples=10000):
    seed, arms, records, runs = load_runs(run_dirs, rates)
    summary = summarize(records, arms, seed=seed, resamples=resamples)
    summary.update(trials=len(records), rebuilt_from=str(run_dirs[0]) if len(run_dirs) == 1 else [r['run'] for r in runs],
                   pass_rule='hidden grader and, where the task has one, its whole edge suite (October 4); '
                             'hidden_passes alongside',
                   edge_from=[r['edge_from'] for r in runs])
    if len(run_dirs) > 1:
        summary.update(runs=runs, cross_run='Arms from different runs are paired by task; they ran at different '
                                            'times and possibly on different code, clients and auth (see runs).')
    return summary


def write_summary(run_dirs, rates, out, resamples=10000):
    """Rebuild one run's summary, or pair several runs' arms (see load_runs). Never overwrites."""
    run_dirs = [run_dirs] if isinstance(run_dirs, (str, Path)) else list(run_dirs)
    summary = summary_of(run_dirs, rates, resamples)
    with Path(out).open('x') as f:  # never overwrite evidence
        json.dump(summary, f, indent=2)
        f.write('\n')
    return summary


def main():
    from .bench import rates
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('runs', type=Path, nargs='+', help='One run, or several whose arms are paired by task')
    parser.add_argument('--out', type=Path, help='Write here (must not exist); default prints the summary')
    parser.add_argument('--resamples', type=int, default=10000)
    args = parser.parse_args()
    if args.out:
        write_summary(args.runs, rates(), args.out, args.resamples)
        print('summary:', args.out)
        return
    print(json.dumps(summary_of(args.runs, rates(), args.resamples), indent=2))


if __name__ == '__main__':
    main()
