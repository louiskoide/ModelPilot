"""Miss detector, next candidate (built offline October 14; not run): a requirements checklist read from the
instruction alone, then judged against each saved fix.

The misses on record are implied requirements that were never implemented (tomli's keyword construction, parse's
unseparated numbers, ISMAGS's multigraph-in-graph case), so neither the agents' tests nor the task's suite can show
them (miss_detector, October 13), and Opus's spec-written tests caught parse but not tomli or ISMAGS (spec_tests). This
asks one model to spell the requirements out as checks and a second request to say, check by check, whether the diff
implements them. Dev split only (miss_detector.split_runs('dev')): the holdout is the user's call.

    python3 -m modelpilot.miss_checklist plan [--out FILE]                              # $0: cost per model, dev split
    python3 -m modelpilot.miss_checklist list --model haiku --out runs/miss-checklist-<ts> [--live] [--limit-usd 1]
    python3 -m modelpilot.miss_checklist judge runs/miss-checklist-<ts> [--live] [--limit-usd 5]
    python3 -m modelpilot.miss_checklist score runs/miss-checklist-<ts>                 # $0

list: one request per dev task, given only the instruction and the repository's name: never the hidden tests, the
reference fix, the edge suite or any agent's diff. judge: one request per counted dev trial, given the instruction, the
task's checklist and the trial's saved agent.diff, never its grade. Without --live each writes its prompts and a plan
(requests, token limits, the most each can cost) and sends nothing. With --live (ANTHROPIC_API_KEY in the environment)
each request is sent once, with no retry and no fallback model, and none is sent once the next one's maximum cost would
take the total past --limit-usd (a stopping threshold, not a billing cap). Errors, refusals and replies without a valid
JSON block are recorded as they are. score ($0) reads the verdicts against the strict labels.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from . import bench_tasks, cache_probe

ROOT = Path(__file__).resolve().parents[1]
MODELS = {'haiku': ('claude-haiku-5-5', 'low'), 'sonnet': ('claude-sonnet-5-5', 'low')}
LIST_MAX_TOKENS, JUDGE_MAX_TOKENS, TIMEOUT = 6000, 6000, 300
MAX_CHECKS = 10
MAX_DIFF_BYTES = 100_000  # a larger diff is recorded as not judged rather than cut, so nothing it changed goes unseen
VERDICTS = ('met', 'not_met', 'unclear')
# Estimates only (plan): tokens per byte of prompt, and the output an answer is assumed to take, thinking included.
BYTES_PER_TOKEN = 3.0
EXPECTED_OUTPUT = {'list': 1500, 'judge': 1500}
# Item 7 (runs/late-fix-analysis-20261007.json): a miss noticed late was repaired 28/28 at $0.056 each (mean).
LATE_FIX_USD = 0.056
REDO_USD = 0.25  # an Opus 5.5 redo of the task (runs/redo-cost-20261004.json), for comparison

LIST_SYSTEM = (
    'You read a change request for a Python library and list what a correct implementation must do, as checks a '
    'reviewer can verify by reading the code change. Include what the request states and, above all, what it implies '
    'without spelling out: the inputs and edge cases it covers, the existing behaviour it must keep, and the public '
    'names, types, errors and messages it describes. Every check must follow from the request or from the library\'s '
    'established public behaviour; don\'t invent requirements. Give 3 to 10 checks, the most important first. Reply '
    'with one ```json block holding a list of objects with the keys "id" (R1, R2, ...), "requirement" (one sentence) '
    'and "check" (what to look for in the change).')
JUDGE_SYSTEM = (
    'You review a code change against a checklist of requirements. For each check, decide from the diff whether the '
    'change implements it: "met" when the diff shows code that does it, "not_met" when the diff doesn\'t do it or does '
    'something else, "unclear" when the diff can\'t tell you (for example, it depends on code the diff doesn\'t show). '
    'Tests the change adds show intent, not implementation. Judge the code, not its comments or any claim in the diff. '
    'Reply with one ```json block holding a list of objects with the keys "id", "verdict" and "evidence" (the diff '
    'lines that decide it, or why there are none), one per check, in the checklist\'s order.')


def list_prompt(task):
    return (f"Repository: {task['repo']} ({task['url']}).\n\nChange request:\n\n{task['instruction']}\n\n"
            'List the checks.')


def judge_prompt(task, checks, diff):
    return (f"Repository: {task['repo']} ({task['url']}).\n\nChange request:\n\n{task['instruction']}\n\n"
            f"Checklist:\n\n{json.dumps(checks, indent=1)}\n\nThe change, as a git diff:\n\n```diff\n{diff}\n```\n\n"
            'Judge each check.')


def request(model, system, prompt, max_tokens):
    """One request. Sonnet 5.5 and Haiku 5.5 think adaptively; effort is set, never left at the model's default."""
    name, effort = MODELS[model]
    return {'model': name, 'max_tokens': max_tokens, 'output_config': {'effort': effort}, 'system': system,
            'messages': [{'role': 'user', 'content': prompt}]}


def list_request(task, model):
    return request(model, LIST_SYSTEM, list_prompt(task), LIST_MAX_TOKENS)


def judge_request(task, checks, diff, model):
    return request(model, JUDGE_SYSTEM, judge_prompt(task, checks, diff), JUDGE_MAX_TOKENS)


def max_cost(payload, rates):
    """The most one request can cost: its bytes as tokens (at most one token a byte) and every output token."""
    rate = cache_probe.tier(rates[payload['model']])  # a model priced by prompt length: its dearest tier
    return (len(json.dumps(payload).encode()) * rate['input'] + payload['max_tokens'] * rate['output']) / 1e6


def expected_cost(payload, rates, output_tokens):
    """An estimate, not a bound: prompt bytes at BYTES_PER_TOKEN, and the output assumed."""
    prompt_tokens = len(json.dumps(payload).encode()) / BYTES_PER_TOKEN
    rate = cache_probe.tier(rates[payload['model']], prompt_tokens)
    return (prompt_tokens * rate['input'] + output_tokens * rate['output']) / 1e6


def reply_text(response):
    return ''.join(b.get('text', '') for b in response.get('content') or [] if isinstance(b, dict) and b.get('type') == 'text')


def json_block(text):
    """The reply's ```json block (else its first top-level JSON list), parsed; None when there is none."""
    found = re.search(r'```(?:json)?\s*\n(.*?)```', text, re.S)
    candidates = [found.group(1)] if found else []
    start = text.find('[')
    if start >= 0:
        candidates.append(text[start:text.rfind(']') + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def valid_checks(value):
    """A checklist as list asks for it (up to 10 objects with id, requirement and check, ids unique), or None."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CHECKS:
        return None
    keys = ('id', 'requirement', 'check')
    if not all(isinstance(c, dict) and all(isinstance(c.get(k), str) and c[k].strip() for k in keys) for c in value):
        return None
    if len({c['id'] for c in value}) != len(value):
        return None
    return [{k: c[k].strip() for k in keys} for c in value]


def verdicts_for(checks, value):
    """One verdict per check, in the checklist's order: the reply's where it gives a known verdict for that id, else
    'missing' (counted with unclear by score). None when the reply isn't a list."""
    if not isinstance(value, list):
        return None
    given = {}
    for v in value:
        if isinstance(v, dict) and isinstance(v.get('id'), str) and v.get('verdict') in VERDICTS:
            given.setdefault(v['id'].strip(), {'verdict': v['verdict'], 'evidence': str(v.get('evidence', ''))[:2000]})
    return [dict({'id': c['id']}, **given.get(c['id'], {'verdict': 'missing', 'evidence': ''})) for c in checks]


def send_all(items, out, rates, limit_usd, transport, parse):
    """Each (key, payload) sent once, in order, under the limit; replies saved whole under out/responses. parse(key,
    response) returns the fields a usable reply adds to its row, or None for an unusable one. Never retried."""
    results, spent = {}, 0.0
    (out/'responses').mkdir()
    for key, payload in items:
        if spent + max_cost(payload, rates) > limit_usd:
            results[key] = {'status': 'not_sent', 'reason': 'limit'}
            continue
        try:
            response, request_id = transport(payload)
        except Exception as error:  # recorded, never retried: a retry changes what was billed
            results[key] = dict(cache_probe.error_details(error), status='error', error_type=type(error).__name__)
            continue
        (out/'responses'/f'{key}.json').write_text(json.dumps(response, indent=2) + '\n')
        cost = cache_probe.priced_usage(payload, response.get('model'), response.get('usage'), rates)
        spent += cost if cost is not None else max_cost(payload, rates)  # unknown cost counts at its most
        row = {'request_id': request_id, 'stop_reason': response.get('stop_reason'), 'usage': response.get('usage'),
               'cost_usd': cost}
        parsed = parse(key, response)
        row.update(parsed if parsed is not None else {'status': 'unusable_reply'})
        results[key] = row
    sent = [r for r in results.values() if r['status'] != 'not_sent']
    # An error may still have been billed (a timeout after the API took the request): its cost is unknown.
    return results, {'spent_usd': round(spent, 6), 'cost_complete': all(r.get('cost_usd') is not None for r in sent)}


def default_transport():
    return lambda payload: cache_probe.send(payload, {}, timeout=TIMEOUT)


def list_checks(tasks, out, rates, model='haiku', live=False, limit_usd=1.0, transport=None):
    """Step 1: a checklist per task from its instruction alone, into out/checklists/<task>.json."""
    out = Path(out)
    out.mkdir(parents=True)  # a new directory: never overwrite evidence
    from .bench import code_revision
    plan = {'step': 'list', 'model': MODELS[model][0], 'model_key': model, 'effort': MODELS[model][1],
            'max_tokens': LIST_MAX_TOKENS, 'system': LIST_SYSTEM, 'live': live, 'limit_usd': limit_usd,
            'code': code_revision(), 'tasks': {}}
    (out/'prompts').mkdir()
    items = []
    for task in tasks:
        payload = list_request(task, model)
        (out/'prompts'/f"{task['id']}.json").write_text(json.dumps(payload, indent=2) + '\n')
        plan['tasks'][task['id']] = {'spec_sha256': bench_tasks.spec_hash(task),
                                     'max_cost_usd': round(max_cost(payload, rates), 4)}
        items.append((task['id'], payload))
    plan['max_cost_usd'] = round(sum(t['max_cost_usd'] for t in plan['tasks'].values()), 4)
    plan['results'] = {}
    if live:
        def parse(task_id, response):
            checks = valid_checks(json_block(reply_text(response)))
            if checks is None:
                return None
            (out/'checklists').mkdir(exist_ok=True)
            (out/'checklists'/f'{task_id}.json').write_text(json.dumps(checks, indent=1) + '\n')
            return {'status': 'listed', 'checks': len(checks)}
        plan['results'], totals = send_all(items, out, rates, limit_usd, transport or default_transport(), parse)
        plan.update(totals)
    (out/'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    return plan


def trial_key(row):
    return f"{row['run']}__{row['task']}__{row['arm']}__{row['trial']}"


def dev_trials(rates):
    """Every counted dev trial (miss_detector's rule) with its saved diff and strict label: the judge's inputs."""
    from . import miss_detector
    from .bench_report import load_run
    rows = []
    for run in miss_detector.split_runs('dev'):
        for record in load_run(run, rates)[1]:
            if miss_detector.counted(record):
                rows.append({'run': run.name, 'task': record['task'], 'arm': record['arm'], 'trial': record['trial'],
                             'strict_pass': bool(record['passed']),
                             'diff': str(run/record['task']/record['arm']/str(record['trial'])/'agent.diff')})
    return rows


def judge(out, trials, rates, live=False, limit_usd=5.0, transport=None):
    """Step 2: each trial's diff against its task's checklist, into out/judge/. trials: dev_trials()-shaped rows; their
    labels go to out/judge/trials.json for score, never into a request."""
    out = Path(out)
    plan = json.loads((out/'plan.json').read_text())
    model = plan['model_key']
    tasks = {t['id']: t for t in bench_tasks.load()}
    step = out/'judge'
    step.mkdir()  # once per checklist directory: never overwrite evidence
    (step/'prompts').mkdir()
    (step/'trials.json').write_text(json.dumps(trials, indent=1) + '\n')
    entries, items = {}, []
    for row in trials:
        key = trial_key(row)
        checklist = out/'checklists'/f"{row['task']}.json"
        diff = Path(row['diff'])
        text = diff.read_text(errors='replace') if diff.exists() else ''
        if not checklist.exists():
            entries[key] = {'status': 'no_checklist'}
        elif len(text.encode()) > MAX_DIFF_BYTES:
            entries[key] = {'status': 'diff_too_large', 'diff_bytes': len(text.encode())}
        else:
            payload = judge_request(tasks[row['task']], json.loads(checklist.read_text()), text, model)
            (step/'prompts'/f'{key}.json').write_text(json.dumps(payload, indent=2) + '\n')
            entries[key] = {'status': 'planned', 'diff_bytes': len(text.encode()),
                            'diff_sha256': hashlib.sha256(text.encode()).hexdigest(),
                            'max_cost_usd': round(max_cost(payload, rates), 4)}
            items.append((key, payload))
    report = {'step': 'judge', 'model': MODELS[model][0], 'effort': MODELS[model][1], 'max_tokens': JUDGE_MAX_TOKENS,
              'system': JUDGE_SYSTEM, 'live': live, 'limit_usd': limit_usd, 'trials': entries,
              'max_cost_usd': round(sum(e.get('max_cost_usd', 0) for e in entries.values()), 4), 'results': {}}
    if live:
        checklists = {t: json.loads((out/'checklists'/f'{t}.json').read_text())
                      for t in {r['task'] for r in trials} if (out/'checklists'/f'{t}.json').exists()}
        task_of = {trial_key(r): r['task'] for r in trials}

        def parse(key, response):
            verdicts = verdicts_for(checklists[task_of[key]], json_block(reply_text(response)))
            return None if verdicts is None else {'status': 'judged', 'verdicts': verdicts}
        report['results'], totals = send_all(items, step, rates, limit_usd, transport or default_transport(), parse)
        report.update(totals)
    (step/'plan.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


RULES = {f'{name}>={k}': (k, unclear) for k in (1, 2, 3)
         for name, unclear in (('not_met', False), ('not_met_or_unclear', True))}


def flagged(verdicts, k, unclear):
    """A trial is flagged when at least k checks are not met (and, with unclear, not clearly met)."""
    counted = ('not_met', 'unclear', 'missing') if unclear else ('not_met',)
    return sum(v['verdict'] in counted for v in verdicts) >= k


def share(rows, rule):
    k, unclear = RULES[rule]
    misses = [r for r in rows if not r['strict_pass']]
    passes = [r for r in rows if r['strict_pass']]
    return {'misses_flagged': sum(flagged(r['verdicts'], k, unclear) for r in misses), 'misses': len(misses),
            'passes_flagged': sum(flagged(r['verdicts'], k, unclear) for r in passes), 'passes': len(passes)}


def score(out):
    """$0: each rule's misses and passes flagged, overall, per task, and with each task left out (37 of the 40 dev
    misses are on 3 tasks, so the overall figure can rest on one of them)."""
    out = Path(out)
    trials = json.loads((out/'judge'/'trials.json').read_text())
    results = json.loads((out/'judge'/'plan.json').read_text())['results']
    rows = [dict(r, verdicts=results[trial_key(r)]['verdicts']) for r in trials
            if results.get(trial_key(r), {}).get('status') == 'judged']
    tasks = sorted({r['task'] for r in rows})
    report = {'trials': len(trials), 'judged': len(rows), 'unjudged': len(trials) - len(rows),
              'unjudged_misses': sum(not r['strict_pass'] for r in trials) - sum(not r['strict_pass'] for r in rows),
              'rules': {}}
    for rule in RULES:
        report['rules'][rule] = {
            'overall': share(rows, rule),
            'per_task': {t: share([r for r in rows if r['task'] == t], rule) for t in tasks},
            'leave_one_task_out': {t: share([r for r in rows if r['task'] != t], rule) for t in tasks}}
    with (out/'score.json').open('x') as f:  # never overwrite evidence
        json.dump(report, f, indent=1)
        f.write('\n')
    return report


SAMPLE_CHECKS = [{'id': f'R{i}', 'requirement': 'x' * 110, 'check': 'x' * 140} for i in range(1, MAX_CHECKS + 1)]


def estimate(tasks, trials, rates, model):
    """Dollars for both steps over these tasks and trials (dicts with 'task' and 'diff_bytes'): expected (prompt bytes
    at BYTES_PER_TOKEN, EXPECTED_OUTPUT assumed, a full 10-check list in every judge prompt) and the most possible."""
    by_id = {t['id']: t for t in tasks}
    lists = [list_request(t, model) for t in tasks]
    judges = [judge_request(by_id[r['task']], SAMPLE_CHECKS, 'x' * r['diff_bytes'], model) for r in trials]
    expected = {'list': sum(expected_cost(p, rates, EXPECTED_OUTPUT['list']) for p in lists),
                'judge': sum(expected_cost(p, rates, EXPECTED_OUTPUT['judge']) for p in judges)}
    most = {'list': sum(max_cost(p, rates) for p in lists), 'judge': sum(max_cost(p, rates) for p in judges)}
    return {'model': MODELS[model][0], 'effort': MODELS[model][1], 'tasks': len(lists), 'trials': len(judges),
            'expected_usd': {k: round(v, 4) for k, v in dict(expected, total=sum(expected.values())).items()},
            'max_usd': {k: round(v, 4) for k, v in dict(most, total=sum(most.values())).items()},
            'expected_per_trial_usd': round(sum(expected.values()) / len(judges), 5) if judges else None}


def break_even(per_trial_usd, miss_rate, recall, false_flag_rate, fix_usd=LATE_FIX_USD):
    """The damage per unnoticed miss above which running the check on every trial pays, if every flag costs one fix
    attempt at item 7's late-fix price: (check + flags × fix) / misses caught, per trial. None if it catches nothing."""
    caught = miss_rate * recall
    if caught <= 0:
        return None
    flags = caught + (1 - miss_rate) * false_flag_rate
    return (per_trial_usd + flags * fix_usd) / caught


def break_even_grid(per_trial_usd, miss_rate):
    return {f'recall {r}, false flags {f}': round(break_even(per_trial_usd, miss_rate, r, f), 3)
            for r in (0.5, 0.75, 1.0) for f in (0.05, 0.1, 0.25)}


def dev_tasks(trials):
    wanted = {r['task'] for r in trials}
    return [t for t in bench_tasks.load() if t['id'] in wanted]


def main():
    from .bench import rates
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='step', required=True)
    p = sub.add_parser('plan', help='$0: both steps\' cost for each model over the dev split')
    p.add_argument('--out', type=Path)
    s = sub.add_parser('list')
    s.add_argument('--model', choices=sorted(MODELS), default='haiku')
    s.add_argument('--out', type=Path, required=True)
    s.add_argument('--live', action='store_true')
    s.add_argument('--limit-usd', type=float, default=1.0)
    j = sub.add_parser('judge')
    j.add_argument('out', type=Path)
    j.add_argument('--live', action='store_true')
    j.add_argument('--limit-usd', type=float, default=5.0)
    c = sub.add_parser('score')
    c.add_argument('out', type=Path)
    args = parser.parse_args()
    if getattr(args, 'live', False) and not os.environ.get('ANTHROPIC_API_KEY'):
        raise SystemExit('Set ANTHROPIC_API_KEY in the environment; no requests sent.')
    if args.step == 'score':
        report = score(args.out)
        print(json.dumps({rule: r['overall'] for rule, r in report['rules'].items()}, indent=1))
        return
    trials = dev_trials(rates())
    if not trials:
        raise SystemExit('No counted dev trials under runs/: the dev split\'s bench runs are needed.')
    if args.step == 'plan':
        for row in trials:
            diff = Path(row['diff'])
            row['diff_bytes'] = diff.stat().st_size if diff.exists() else 0
        tasks = dev_tasks(trials)
        misses = sum(not r['strict_pass'] for r in trials)
        report = {'trials': len(trials), 'misses': misses, 'tasks': len(tasks),
                  'models': {m: estimate(tasks, trials, rates(), m) for m in MODELS},
                  'fix_usd': LATE_FIX_USD, 'redo_usd': REDO_USD}
        for m, e in report['models'].items():
            e['break_even_damage_usd'] = break_even_grid(e['expected_per_trial_usd'], misses / len(trials))
        text = json.dumps(report, indent=1) + '\n'
        if args.out:
            with Path(args.out).open('x') as f:
                f.write(text)
        print(text)
    elif args.step == 'list':
        plan = list_checks(dev_tasks(trials), args.out, rates(), args.model, args.live, args.limit_usd)
        print(json.dumps({k: plan.get(k) for k in ('live', 'max_cost_usd', 'limit_usd', 'spent_usd')}, indent=1))
    else:
        report = judge(args.out, trials, rates(), args.live, args.limit_usd)
        print(json.dumps({k: report.get(k) for k in ('live', 'max_cost_usd', 'limit_usd', 'spent_usd')}, indent=1))


if __name__ == '__main__':
    main()
