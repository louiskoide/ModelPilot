"""Plan item 14, a Haiku 5.5 read/search explorer, step 1 (built October 14, $0; not yet run): would it pay?

Replays the recorded dev-split trials of the cheapest fixed arms (`sonnet-5.5-low-concise` and Sonnet 5.5 at medium,
`sonnet-5.5`) as if each read or search result the main loop received had come back as a k-token summary from a Haiku
5.5 explorer subagent instead. Cache writes are about 45% of cost, mostly context growth, and 80–90% of tool-result
bytes are source read or searched (cache-write-sources-20261003.json, tool-result-split-20261005.txt), so the question
is whether summaries shrink the growth by more than the explorer and its plumbing cost.

Per trial: the main-loop proxy rows (Messages, with tools, HTTP 200, no compaction) are matched one to one with the
client transcript's assistant turns; a trial whose counts differ is reported as unaligned and left out. A result of T
tokens (its bytes / BYTES_PER_TOKEN) replaced by k tokens saves T - k on every later request that carries it: at the
write rate on the request that first carries it (if that request wrote at least T), then at whatever that request paid
for its old content (read, else write, else uncached input), each request at its own model's rates on its day, 1h
writes at the 5m rate (API-key equivalent). A Read of a file the agent later edits is never replaced: Claude Code's
Edit needs the main loop to have read the file itself.

Costs added, as low and high bounds (BOUNDS): the explorer's Haiku 5.5 requests (steps x (overhead + T) input tokens,
summary x output factor), the main loop's delegation call (its output and its growth on later requests) and the agent
tool's definition in every request's prefix. Scopes: `tools` (Read, Grep, Glob, as the item asks) and `tools_and_bash`
(also read-only Bash commands, where most of the source reading happens). Selection: `all` such results, or `over_k`
(only those longer than k: an oracle a live agent doesn't have, so an upper bound).

Assumes the agent's behaviour is otherwise unchanged (same requests, same output), which a live arm would test.

    python3 -m modelpilot.explorer_replay --out runs/explorer-replay-<ts>.json

$0: recorded files only, no model request.
"""
import argparse
import json
from pathlib import Path
import re
import statistics
from .cache_probe import prompt_tokens, tier
from .proxy import read_log

ROOT = Path(__file__).resolve().parents[1]
ARMS = ('sonnet-5.5-low-concise', 'sonnet-5.5')
HELPER = 'claude-haiku-5-5'
BYTES_PER_TOKEN = 2.8  # the policy's cost model (configs/modelpilot-policy.json)
K_GRID = (50, 100, 200, 400, 800, 1600)
BOUNDS = {  # (optimistic for the explorer, pessimistic)
    'low': {'helper_steps': 1, 'helper_overhead': 2000, 'helper_output_factor': 1.0, 'call_tokens': 50,
            'definition_tokens': 1000},
    'high': {'helper_steps': 3, 'helper_overhead': 8000, 'helper_output_factor': 2.0, 'call_tokens': 150,
             'definition_tokens': 3000}}
SEARCH_TOOLS = ('Read', 'Grep', 'Glob')
EDITS = ('Edit', 'Write', 'MultiEdit', 'NotebookEdit')
READ_COMMANDS = ('cat', 'sed', 'head', 'tail', 'grep', 'rg', 'find', 'ls', 'nl', 'wc', 'awk', 'tree')


def bash_reads(command):
    """A read-only Bash command (cat, sed -n, grep, find, ...): no redirection into a file, no in-place sed. Leading
    `cd x &&` is skipped; a pipeline counts by its first command."""
    command = re.sub(r'^\s*cd\s+\S+\s*&&\s*', '', command or '')
    if re.search(r'(^|[^2&])>|\bsed\s+-i\b|\btee\b', command):
        return False
    first = command.strip().split()
    return bool(first) and first[0] in READ_COMMANDS


def transcript(path):
    """(turns, results) from client.stdout.jsonl: assistant turns in order, one per API response (events sharing a
    message id are one turn), each with its tool calls; and each tool result's text by tool_use_id."""
    turns, results, index = [], {}, {}
    if not Path(path).exists():
        return turns, results
    for line in Path(path).read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get('message') if isinstance(event, dict) else None
        if not isinstance(message, dict):
            continue
        content = message.get('content') if isinstance(message.get('content'), list) else []
        if event.get('type') == 'assistant':
            key = message.get('id') or f'anon-{len(turns)}'
            if key not in index:
                index[key] = len(turns)
                turns.append({'id': key, 'calls': []})
            turns[index[key]]['calls'] += [b for b in content if isinstance(b, dict) and b.get('type') == 'tool_use']
        elif event.get('type') == 'user':
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    body = block.get('content')
                    text = body if isinstance(body, str) else ''.join(
                        b.get('text', '') for b in body or [] if isinstance(b, dict) and b.get('type') == 'text')
                    results[block.get('tool_use_id')] = text
    return turns, results


def main_rows(rows):
    """The main loop's requests, in order: Messages with tools, answered 200, not a compaction."""
    picked = [r for r in rows if r.get('kind') == 'messages' and r.get('tool_count') and r.get('http_status') == 200
              and not r.get('compaction')]
    return sorted(picked, key=lambda r: r.get('started_unix') or 0)


def candidates(turns, results, scope):
    """Results an explorer could have returned instead: {'turn', 'tool', 'tokens', 'path'}, with Reads of files the
    agent edits later left out (`kept_for_edit` counts them)."""
    edits = {}
    for j, turn in enumerate(turns):
        for call in turn['calls']:
            if call.get('name') in EDITS:
                path = (call.get('input') or {}).get('file_path')
                edits.setdefault(path, []).append(j)
    out, kept = [], 0
    for j, turn in enumerate(turns):
        for call in turn['calls']:
            name, args = call.get('name'), call.get('input') or {}
            if name in SEARCH_TOOLS:
                pass
            elif not (scope == 'tools_and_bash' and name == 'Bash' and bash_reads(args.get('command'))):
                continue
            text = results.get(call.get('id'))
            if text is None:
                continue
            path = args.get('file_path') if name == 'Read' else None
            if path and any(e > j for e in edits.get(path, ())):
                kept += 1
                continue
            out.append({'turn': j, 'tool': name, 'tokens': len(text.encode()) / BYTES_PER_TOKEN, 'path': path})
    return out, kept


def rates_of(row, rates):
    return tier(rates[row['model']], prompt_tokens(row.get('usage') or {}), row.get('started_unix'))


def unit_rates(rows, rates):
    """Per main-loop request: its rate table and what a token of old content cost it (read, else write, else input)."""
    out = []
    for row in rows:
        rate, usage = rates_of(row, rates), row.get('usage') or {}
        old = rate['read'] if usage.get('cache_read_input_tokens') else \
            rate['write_5m'] if usage.get('cache_creation_input_tokens') else rate['input']
        out.append({'rate': rate, 'old': old, 'written': usage.get('cache_creation_input_tokens') or 0})
    return out


def carried(tokens, first, units):
    """Dollars a prompt of `tokens` extra tokens costs from request `first` on: written there (or uncached), then paid
    as each later request paid its old content."""
    if first >= len(units) or tokens <= 0:
        return 0.0
    head = units[first]
    total = tokens * (head['rate']['write_5m'] if head['written'] >= tokens else head['rate']['input'])
    return (total + tokens * sum(u['old'] for u in units[first + 1:])) / 1e6


def replay_trial(rows, turns, results, rates, k, scope, selection, bound):
    """One trial at one k: {'saving', 'helper', 'overhead', 'net', 'replaced'} in dollars, or None if unaligned."""
    rows = main_rows(rows)
    if not rows or len(rows) != len(turns):
        return None
    units = unit_rates(rows, rates)
    found, _ = candidates(turns, results, scope)
    chosen = [c for c in found if selection == 'all' or c['tokens'] > k]
    b = BOUNDS[bound]
    saving = helper = overhead = 0.0
    for c in chosen:
        saving += carried(c['tokens'] - k, c['turn'] + 1, units)
        if c['tokens'] < k:  # a summary longer than the result it replaces costs more
            saving -= carried(k - c['tokens'], c['turn'] + 1, units)
        helper_input = b['helper_steps'] * (b['helper_overhead'] + c['tokens'])
        hr = tier(rates[HELPER], helper_input / b['helper_steps'])
        helper += (helper_input * hr['input'] + k * b['helper_output_factor'] * hr['output']) / 1e6
        call_rate = units[c['turn']]['rate']
        overhead += b['call_tokens'] * call_rate['output'] / 1e6 + carried(b['call_tokens'], c['turn'] + 1, units)
    if chosen:  # the agent tool's definition sits in every request's prefix once the arm offers it
        overhead += carried(b['definition_tokens'], 0, units)
    return {'saving': saving, 'helper': helper, 'overhead': overhead, 'net': saving - helper - overhead,
            'replaced': len(chosen)}


def replay(trials, rates, k_grid=K_GRID):
    """Every trial ({'task', 'arm', 'rows', 'turns', 'results', 'cost'}) at every k, scope, selection and bound, and
    per arm: mean net per trial, the break-even k (the largest k whose mean net is still >= 0) and per-task means."""
    rows_out, unaligned, unpriced = [], [], []
    for t in trials:
        if len(main_rows(t['rows'])) != len(t['turns']) or not t['turns']:
            unaligned.append({k: t.get(k) for k in ('run', 'task', 'arm', 'trial')})
            continue
        if any(r.get('model') not in rates for r in main_rows(t['rows'])):
            unpriced.append({k: t.get(k) for k in ('run', 'task', 'arm', 'trial')})
            continue
        found = {s: candidates(t['turns'], t['results'], s) for s in ('tools', 'tools_and_bash')}
        row = {k: t.get(k) for k in ('run', 'task', 'arm', 'trial', 'cost')}
        row['candidates'] = {s: {'results': len(f), 'tokens': round(sum(c['tokens'] for c in f)), 'kept_for_edit': kept}
                             for s, (f, kept) in found.items()}
        row['grid'] = {f'{s}/{sel}/{b}/{k}': replay_trial(t['rows'], t['turns'], t['results'], rates, k, s, sel, b)
                       for s in ('tools', 'tools_and_bash') for sel in ('all', 'over_k') for b in BOUNDS for k in k_grid}
        rows_out.append(row)
    arms = {}
    for arm in sorted({r['arm'] for r in rows_out}):
        mine = [r for r in rows_out if r['arm'] == arm]
        costs = [r['cost'] for r in mine if r['cost'] is not None]
        summary = {'trials': len(mine), 'mean_trial_cost_usd': round(statistics.mean(costs), 5) if costs else None,
                   'unpriced_trials': len(mine) - len(costs),  # left out of the mean (October 15 audit)
                   'candidates': {s: {'mean_results': round(statistics.mean(r['candidates'][s]['results'] for r in mine), 2),
                                      'mean_tokens': round(statistics.mean(r['candidates'][s]['tokens'] for r in mine)),
                                      'kept_for_edit': sum(r['candidates'][s]['kept_for_edit'] for r in mine)}
                                  for s in ('tools', 'tools_and_bash')},
                   'readings': {}}
        for s in ('tools', 'tools_and_bash'):
            for sel in ('all', 'over_k'):
                for b in BOUNDS:
                    nets = {k: statistics.mean(r['grid'][f'{s}/{sel}/{b}/{k}']['net'] for r in mine) for k in k_grid}
                    paying = [k for k in k_grid if nets[k] >= 0]
                    tasks = sorted({r['task'] for r in mine})
                    summary['readings'][f'{s}/{sel}/{b}'] = {
                        'mean_net_usd_per_trial': {k: round(v, 5) for k, v in nets.items()},
                        'mean_saving_usd_per_trial': {k: round(statistics.mean(r['grid'][f'{s}/{sel}/{b}/{k}']['saving']
                                                                               for r in mine), 5) for k in k_grid},
                        'break_even_k': max(paying) if paying else None,
                        'net_per_task_at_200': {t: round(statistics.mean(r['grid'][f'{s}/{sel}/{b}/200']['net']
                                                                         for r in mine if r['task'] == t), 5)
                                                for t in tasks} if 200 in k_grid else None}
        arms[arm] = summary
    return {'k_grid': list(k_grid), 'bounds': BOUNDS, 'bytes_per_token': BYTES_PER_TOKEN, 'trials': len(rows_out),
            'unaligned': unaligned, 'unpriced': unpriced, 'arms': arms, 'rows': rows_out}


def dev_trials(rates, arms=ARMS):
    """The counted dev trials of these arms with their proxy rows, transcript and recorded cold-equivalent cost."""
    from . import miss_detector
    from .bench_report import cold_cost, load_run
    out = []
    for run in miss_detector.split_runs('dev'):
        for record in load_run(run, rates)[1]:
            if record['arm'] not in arms or not miss_detector.counted(record):
                continue
            trial = run/record['task']/record['arm']/str(record['trial'])
            log = trial/'observations.jsonl'
            rows = read_log(log)
            turns, results = transcript(trial/'client.stdout.jsonl')
            out.append({'run': run.name, 'task': record['task'], 'arm': record['arm'], 'trial': record['trial'],
                        'cost': cold_cost(record), 'rows': rows, 'turns': turns, 'results': results})
    return out


def main():
    from .bench import rates
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, required=True, help='a new JSON file')
    args = parser.parse_args()
    if args.out.exists():
        parser.error(f'{args.out} exists: use a new path')
    trials = dev_trials(rates())
    if not trials:
        raise SystemExit('No counted dev trials of these arms under runs/: the dev split\'s bench runs are needed.')
    report = replay(trials, rates())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as f:
        json.dump(report, f, indent=1)
        f.write('\n')
    for arm, s in report['arms'].items():
        print(arm, s['trials'], 'trials, mean cost', s['mean_trial_cost_usd'])
        for name in ('tools/all/low', 'tools/all/high', 'tools_and_bash/over_k/low'):
            r = s['readings'][name]
            print(f"  {name}: net per trial {r['mean_net_usd_per_trial']}, break-even k {r['break_even_k']}")
    print(f"unaligned: {len(report['unaligned'])} -> {args.out}")


if __name__ == '__main__':
    main()
