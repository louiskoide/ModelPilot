"""Option C of the October 18 review: would a forced Opus 5.5 note before the cheap agent's first edit name what its
misses miss? Each recorded Sonnet 5.5 transcript of the three miss tasks is cut where the agent first changes the code,
Opus writes the note and its checks from it, and the host runs the checks at $0.

    python3.12 -m modelpilot.pre_edit_note run --limit-usd 6
    python3 -m modelpilot.pre_edit_note write --out runs/pre-edit-note-<ts> [--live --limit-usd 6] [--jobs 4]
    python3 -m modelpilot.pre_edit_note score runs/pre-edit-note-<ts> [--jobs 4]

run: write --live into a new runs/pre-edit-note-<timestamp>, then score it. --live asks for the Anthropic key at a
hidden prompt when ANTHROPIC_API_KEY isn't set; it is never echoed or written, and run drops it before scoring.

Population, fixed in code: every counted trial (miss_detector.counted) of the three TASKS in the miss detector's dev
split, cut at its first change (an Edit/Write/MultiEdit/NotebookEdit call or a Bash command BASH_WRITE recognises, the
pattern of runs/first-edit-check-20261018.py), whose every response up to the cut came from Sonnet 5.5. The cut
message itself is left out: the note comes before any code. Item 8's holdout isn't read.

write: one request a trial to Opus 5.5 at medium effort, with no cache_control (nothing reads it again): the task as
the agent got it, then the agent's messages, tool calls and the results it saw before the cut, rendered as text. Never
the hidden tests, the reference fix, the edge suite, the label or anything after the cut. Without --live it writes the
prompts and the plan (an estimate and the most each request can cost) and sends nothing. --live needs
ANTHROPIC_API_KEY and --limit-usd. Each request is sent once, with no retry and no fallback model, misses first and the
tasks in turn. None is sent once the spend so far plus the most the in-flight and next requests can cost would pass
the limit (a stopping threshold, not a billing cap). A refusal, an error or a reply without a Python block is recorded
as it is; replies are saved whole, the Python block as suites/<id>/<task>/test_note.py.

score ($0, no model request): each note's checks on the task's reference fix and base, and on its own trial's saved
fix, with the grader's isolation; the reading pre-registered in docs/m6-benchmark-plan.md, "Pre-edit note probe
(option C): pre-registration". Applying a diff and running tests executes agent- and model-written code, as grading
does: not an OS sandbox, trusted task repositories only.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
from . import bench_tasks, cache_probe, miss_detector, regrade, spec_tests

ROOT = Path(__file__).resolve().parents[1]
TASKS = ('parse-decimal-grouping', 'tomli-decode-error-attrs', 'nx-ismags-monomorphism')
AGENT_MODEL = 'claude-sonnet-5-5'
MODEL, EFFORT, MAX_TOKENS, TIMEOUT = 'claude-opus-5-5', 'medium', 8000, 600
SUITE = 'test_note.py'
RESULT_CHARS = 20000  # one tool result's most, rendered; longer ones are cut and marked
OUTPUT_GUESS = 2500  # estimate only: the forced-review consults' median 1,702 output tokens plus the checks
BYTES_PER_TOKEN = 2.8  # configs/modelpilot-policy.json
EDITS = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit'}
BASH_WRITE = re.compile(
    r"\bsed\s+(?:-[a-zA-Z]*\s+)*-i|\bperl\s+-[a-zA-Z]*i|"
    r"(?<![0-9&>=-])>>?\s*(?![&=>]|/dev/|/tmp/|\$TMPDIR)['\"]?[\w.~/-]*\w|"
    r"\btee\s+(?:-a\s+)?(?!/dev/|/tmp/)\S|"
    r"open\([^)]*,\s*['\"][wa]|\.write_text\(|\.write_bytes\(|"
    r"\bpatch\b|\bgit\s+(?:apply|checkout\s+--|restore|stash)|"
    r"(?:^|[;&|]\s*)(?:mv|cp|rm|touch)\s")
# The pre-registered reading: a task passes when its own-fix catch rate over misses reaches its bar.
CATCH_BARS = {'parse-decimal-grouping': 0.75, 'tomli-decode-error-attrs': 0.5, 'nx-ismags-monomorphism': 0.6}
MISLEADING_MAX = 0.25  # notes with a check the reference fix fails
VOID_SHARE = 0.2  # population without a reading
SYSTEM = ("You advise a developer agent working on a change to a Python library. The agent has explored the repository "
          "and is about to make its first edit. You see the task and everything the agent has done so far: its "
          "messages, its tool calls and the results it saw. Write two things.\n\n"
          "1. A note for the agent, under 300 words: the plan; the requirements the task implies but doesn't spell "
          "out; and the conventions the change should match, the library's own and those of any API the task says it "
          "should resemble.\n\n"
          "2. The checks the finished change must pass: one file of at most 8 short unittest tests (standard library "
          "and the library itself only), each aimed at a requirement in your note, especially the implied ones. Every "
          "assertion must follow from the task or from the library's established public behaviour; don't rely on "
          "private names, internals or exact wording the task doesn't give. The tests run from the repository root "
          "with the library importable.\n\n"
          "Reply with the note, then the file in one ```python block.")


def bash_write(command):
    return bool(BASH_WRITE.search(command.replace('->', '')))


def conversation(trial_dir):
    """Assistant messages in order, each merged from its stream events (model, content), and tool results by id."""
    order, merged, results = [], {}, {}
    path = Path(trial_dir)/'client.stdout.jsonl'
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get('message')
        if not isinstance(message, dict):
            continue
        if event.get('type') == 'assistant':
            if message['id'] not in merged:
                order.append(message['id'])
                merged[message['id']] = {'model': message.get('model'), 'content': []}
            merged[message['id']]['content'] += message.get('content') or []
        elif event.get('type') == 'user' and isinstance(message.get('content'), list):
            for block in message['content']:
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    content = block.get('content')
                    results[block.get('tool_use_id')] = content if isinstance(content, str) else ''.join(
                        c.get('text', '') for c in content or [] if isinstance(c, dict))
    return [merged[m] for m in order], results


def cut(messages):
    """The first assistant message that changes the code, and the tool it does so with; (None, None) if none does."""
    for index, message in enumerate(messages):
        for block in message['content']:
            if block.get('type') != 'tool_use':
                continue
            if block.get('name') in EDITS:
                return index, block['name']
            if block.get('name') == 'Bash' and bash_write((block.get('input') or {}).get('command', '')):
                return index, 'Bash'
    return None, None


def render(messages, results, upto):
    """The agent's work before message `upto`, as text: its messages, its tool calls and the results it saw."""
    parts, cuts = [], 0
    for message in messages[:upto]:
        for block in message['content']:
            if block.get('type') == 'text' and block.get('text', '').strip():
                parts.append(f"[agent]\n{block['text'].strip()}")
            elif block.get('type') == 'tool_use':
                data = block.get('input') or {}
                shown = data['command'] if block.get('name') == 'Bash' and 'command' in data else json.dumps(data)
                result = results.get(block.get('id'), '')
                if len(result) > RESULT_CHARS:
                    result, cuts = result[:RESULT_CHARS] + f'\n[... {len(result) - RESULT_CHARS} more characters]', cuts + 1
                parts.append(f"[tool call: {block.get('name')}]\n{shown}\n[tool result]\n{result}")
    return '\n\n'.join(parts), cuts


def prompt(task, transcript):
    from .bench import PREAMBLE
    return (f"The task the agent was given:\n\n{PREAMBLE}{task['instruction']}\n\n"
            f"What the agent has done so far, in order:\n\n{transcript}\n\n"
            "The agent's next step is its first edit. Write the note and the checks.")


def request(task, transcript):
    """Opus 5.5 always thinks (adaptively); effort is set, not left at its default. No cache_control."""
    return {'model': MODEL, 'max_tokens': MAX_TOKENS, 'output_config': {'effort': EFFORT}, 'system': SYSTEM,
            'messages': [{'role': 'user', 'content': prompt(task, transcript)}]}


def estimate(payload, rates):
    rate = cache_probe.tier(rates[payload['model']])
    return (len(json.dumps(payload).encode())/BYTES_PER_TOKEN*rate['input'] + OUTPUT_GUESS*rate['output'])/1e6


def population(rates):
    """The trials the probe reads (fixed in code), and what was left out and why."""
    from .bench_report import load_run
    items, left_out = [], {}
    for run in miss_detector.split_runs('dev'):
        for record in load_run(run, rates)[1]:
            if record['task'] not in TASKS or not miss_detector.counted(record):
                continue
            trial_dir = run/record['task']/record['arm']/str(record['trial'])
            messages, results = conversation(trial_dir)
            index, tool = cut(messages)
            key = f"{run.name}__{record['task']}__{record['arm']}__{record['trial']}"
            if index is None:
                left_out[key] = 'no_change_found'
            elif any(m['model'] != AGENT_MODEL for m in messages[:index + 1]):
                left_out[key] = 'not_sonnet_before_the_cut'
            else:
                items.append({'id': key, 'run': run.name, 'task': record['task'], 'arm': record['arm'],
                              'trial': record['trial'], 'effort': record.get('effort'),
                              'strict_pass': bool(record['passed']), 'cut_at': index, 'cut_by': tool,
                              'requests': len(messages)})
    return items, left_out


def send_order(items):
    """Misses before passes, and within each the tasks in turn, so a limit stop leaves every task some misses."""
    def turns(group):
        queues = [[i for i in group if i['task'] == t] for t in TASKS]
        return [q[n] for n in range(max(map(len, queues), default=0)) for q in queues if n < len(q)]
    return turns([i for i in items if not i['strict_pass']]) + turns([i for i in items if i['strict_pass']])


def module_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def write(items, out, rates, live=False, limit_usd=None, jobs=4, transport=None, runs=None, left_out=None):
    out = Path(out)
    out.mkdir(parents=True)  # a new directory: never overwrite evidence
    from .bench import code_revision
    runs = Path(runs or ROOT/'runs')
    tasks = {t['id']: t for t in bench_tasks.load(','.join(TASKS))}
    (out/'prompts').mkdir()
    payloads = {}
    for item in items:
        messages, results = conversation(runs/item['run']/item['task']/item['arm']/str(item['trial']))
        transcript, cuts = render(messages, results, item['cut_at'])
        payloads[item['id']] = request(tasks[item['task']], transcript)
        (out/'prompts'/f"{item['id']}.json").write_text(json.dumps(payloads[item['id']], indent=2) + '\n')
        item.update(results_cut=cuts, estimate_usd=round(estimate(payloads[item['id']], rates), 4),
                    max_cost_usd=round(spec_tests.max_cost(payloads[item['id']], rates), 4))
    plan = {'model': MODEL, 'effort': EFFORT, 'max_tokens': MAX_TOKENS, 'system': SYSTEM, 'live': live,
            'limit_usd': limit_usd, 'code': code_revision(), 'module_sha256': module_sha256(),
            'notes': len(items), 'misses': sum(not i['strict_pass'] for i in items),
            'estimate_usd': round(sum(i['estimate_usd'] for i in items), 4),
            'max_cost_usd': round(sum(i['max_cost_usd'] for i in items), 4), 'population': items,
            'left_out': left_out or {}, 'results': {}}
    if live:
        transport = transport or (lambda p: cache_probe.send(p, {}, timeout=TIMEOUT))
        (out/'responses').mkdir()
        lock, state = threading.Lock(), {'spent': 0.0, 'reserved': 0.0}

        def one(item):
            payload, cap = payloads[item['id']], item['max_cost_usd']
            with lock:
                if state['spent'] + state['reserved'] + cap > limit_usd:
                    return {'status': 'not_sent', 'reason': 'limit'}
                state['reserved'] += cap
            try:
                response, request_id = transport(payload)
            except Exception as error:  # recorded, never retried: a retry changes what was billed
                with lock:
                    state['reserved'] -= cap
                    state['spent'] += cap  # an unknown cost counts at its most toward the limit
                return dict(cache_probe.error_details(error), status='error', error_type=type(error).__name__,
                            cost_usd=None)
            (out/'responses'/f"{item['id']}.json").write_text(json.dumps(response, indent=2) + '\n')
            cost = cache_probe.priced_usage(payload, response.get('model'), response.get('usage'), rates)
            with lock:
                state['reserved'] -= cap
                state['spent'] += cost if cost is not None else cap
            text = ''.join(b.get('text', '') for b in response.get('content') or [] if b.get('type') == 'text')
            code = spec_tests.code_block(text)
            row = {'status': 'written' if code else 'no_code_block', 'request_id': request_id,
                   'stop_reason': response.get('stop_reason'), 'usage': response.get('usage'), 'cost_usd': cost}
            if code:
                suite = out/'suites'/item['id']/item['task']
                suite.mkdir(parents=True)
                (suite/SUITE).write_text(code)
                row.update(sha256=hashlib.sha256(code.encode()).hexdigest(),
                           tests=len(re.findall(r'^\s*def test', code, re.M)))
            return row
        order = send_order(items)
        with ThreadPoolExecutor(max(1, jobs)) as pool:
            for item, row in zip(order, pool.map(one, order)):
                plan['results'][item['id']] = row
        plan['spent_usd'] = round(state['spent'], 6)
        plan['cost_complete'] = all(r.get('cost_usd') is not None for r in plan['results'].values()
                                    if r['status'] != 'not_sent')
    (out/'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    return plan


def reading(item, result, check, own):
    """One note's reading: None where the host has nothing to read (not sent, a transport error, its own fix not
    rebuilt); else whether it caught its own trial's fix and whether a check fails on the reference fix."""
    if result is None or result.get('status') in ('not_sent', 'error'):
        return None
    if result['status'] != 'written':  # a reply without checks: a note that catches nothing
        return {'caught': False, 'misleading': False, 'reference_tests': 0}
    if own is None or own.get('error'):
        return None
    reference = check['reference']
    invalid = set(reference.get('failing_tests') or [])
    misleading = bool(invalid) or (reference.get('exit_code') not in (0, None) and not reference.get('tests_run'))
    caught = spec_tests.readings(own, invalid)[1]
    return {'caught': bool(caught), 'misleading': misleading, 'reference_tests': reference.get('tests_run') or 0,
            'reference_failing': sorted(invalid), 'own_failing': own.get('failing_tests'),
            'base_failing': check['base'].get('failing_tests')}


def verdict(items, readings):
    """The pre-registered rule over every item of the population: void, pass or fail."""
    missing = sum(readings.get(i['id']) is None for i in items)
    read = [i for i in items if readings.get(i['id']) is not None]
    tasks = {}
    for task, bar in CATCH_BARS.items():
        misses = [i for i in read if i['task'] == task and not i['strict_pass']]
        caught = sum(readings[i['id']]['caught'] for i in misses)
        tasks[task] = {'misses_read': len(misses), 'caught': caught, 'bar': bar,
                       'share': round(caught/len(misses), 3) if misses else None,
                       'passes_read': sum(i['task'] == task and i['strict_pass'] for i in read),
                       'passes_flagged': sum(readings[i['id']]['caught'] for i in read
                                             if i['task'] == task and i['strict_pass'])}
    misleading = sum(readings[i['id']]['misleading'] for i in read)
    out = {'population': len(items), 'without_reading': missing, 'tasks': tasks,
           'misleading': misleading, 'misleading_share': round(misleading/len(read), 3) if read else None}
    if not items or missing > VOID_SHARE*len(items):
        out['verdict'] = 'void'
    elif all(t['share'] is not None and t['share'] >= t['bar'] for t in tasks.values()) \
            and out['misleading_share'] <= MISLEADING_MAX:
        out['verdict'] = 'pass'
    else:
        out['verdict'] = 'fail'
    return out


def score(out, python=None, jobs=4, runs=None):
    out = Path(out)
    plan = json.loads((out/'plan.json').read_text())
    runs = Path(runs or ROOT/'runs')
    python = python or bench_tasks.interpreter()
    tasks = {t['id']: t for t in bench_tasks.load(','.join(TASKS))}
    scratch = out/'scratch'
    items = plan['population']

    def one(pair):
        index, item = pair
        result = plan['results'].get(item['id'])
        root = out/'suites'/item['id']
        if not (root/item['task']/SUITE).exists():
            return reading(item, result, None, None), None
        task = tasks[item['task']]
        try:
            check = regrade.check_edge(task, bench_tasks.REPOS/task['repo'], python, scratch/str(index)/'check', root)
            own = spec_tests.run_on_fix(runs/item['run']/item['task']/item['arm']/str(item['trial']), task, python,
                                        scratch/str(index)/'own', root)
        finally:
            shutil.rmtree(scratch/str(index), ignore_errors=True)
        return reading(item, result, check, own), {'reference': check['reference'], 'base': check['base'], 'own': own}
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        done = list(pool.map(one, enumerate(items)))
    shutil.rmtree(scratch, ignore_errors=True)
    readings = {i['id']: r for i, (r, _) in zip(items, done)}
    report = {'plan_module_sha256': plan['module_sha256'], 'score_module_sha256': module_sha256(), 'python': python,
              **verdict(items, readings),
              'rows': [dict(i, reading=r, runs=raw) for i, (r, raw) in zip(items, done)]}
    with (out/'score.json').open('x') as f:  # never overwrite evidence
        json.dump(report, f, indent=2)
        f.write('\n')
    return report


def api_key():
    """The Anthropic key from the environment, else from a hidden prompt: never echoed, printed or written."""
    if not os.environ.get('ANTHROPIC_API_KEY'):
        import getpass
        key = getpass.getpass('Anthropic API key (hidden): ').strip()
        if not key:
            raise SystemExit('No key given; no requests sent.')
        os.environ['ANTHROPIC_API_KEY'] = key


def print_score(out, jobs):
    report = score(out, jobs=jobs)
    print(json.dumps({k: report[k] for k in ('verdict', 'population', 'without_reading', 'tasks',
                                             'misleading', 'misleading_share')}, indent=1))
    print(out/'score.json')


def main():
    from .bench import rates
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='step', required=True)
    w = sub.add_parser('write')
    w.add_argument('--out', type=Path, required=True)
    w.add_argument('--live', action='store_true')
    w.add_argument('--limit-usd', type=float)
    w.add_argument('--jobs', type=int, default=4)
    s = sub.add_parser('score')
    s.add_argument('out', type=Path)
    s.add_argument('--jobs', type=int, default=4)
    r = sub.add_parser('run', help='write --live into a new directory, then score it')
    r.add_argument('--limit-usd', type=float)
    r.add_argument('--out', type=Path)
    r.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    if args.step == 'score':
        print_score(args.out, args.jobs)
        return
    if args.step == 'run':
        from datetime import datetime
        args.live = True
        args.out = args.out or ROOT/'runs'/f"pre-edit-note-{datetime.now():%Y%m%d-%H%M%S}"
    if args.out.exists():
        parser.error(f'{args.out} exists: use a new path')
    if args.live and args.limit_usd is None:
        raise SystemExit('--live needs --limit-usd (a stopping threshold); no requests sent.')
    if args.live:
        api_key()
    items, left_out = population(rates())
    plan = write(items, args.out, rates(), args.live, args.limit_usd, args.jobs, left_out=left_out)
    summary = {k: plan[k] for k in ('live', 'notes', 'misses', 'estimate_usd', 'max_cost_usd', 'limit_usd')}
    summary['left_out'] = len(left_out)
    if args.live:
        summary.update(spent_usd=plan['spent_usd'], cost_complete=plan['cost_complete'],
                       results={s: sum(r['status'] == s for r in plan['results'].values())
                                for s in ('written', 'no_code_block', 'error', 'not_sent')})
    else:
        summary['sent'] = 'nothing; --live --limit-usd <usd> sends'
    print(json.dumps(summary, indent=1))
    if args.step == 'run':
        os.environ.pop('ANTHROPIC_API_KEY', None)  # scoring runs model-written code; it never needs the key
        print_score(args.out, args.jobs)


if __name__ == '__main__':
    main()
