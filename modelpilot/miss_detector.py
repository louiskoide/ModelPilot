"""Miss detector, step 1 ($0): can facts the host has after a run, before anyone accepts it, pick out the trials that
miss strictly? Item 8 found nothing known before a run that does; this asks the same of what the run left behind.

Every Sonnet/Opus miss on record reported success, so a detector can't trust the agent's word. The facts here are the
host's: the saved diff, the client's record of the conversation (tool calls and the outputs the client returned), the
proxy's accounting and how the session stopped. A few read the agent's final text (`text_*`), labelled as model text:
used only to flag work for a check, never to accept it.

Two splits, task-disjoint, fixed before any holdout fact is computed:

- dev: every run on the 29 earlier tuning tasks (September 29 to `bench-20261010-090944`): the place to look.
- holdout: item 8's labelling runs (seed 8; 38 other tasks), the sqlglot screen and the compaction runs excluded
  (their tasks overlap item 8's). `facts --split holdout` refuses without `--prereg`, a committed file whose SHA-256
  is recorded, so the holdout is read once, after the rule is fixed.

A trial counts as item 8's did: complete, eligible, strictly graded (`hidden_and_edge`) and ended for a model reason.

    python3 -m modelpilot.miss_detector facts --split dev --out runs/miss-detector-dev-<ts>.json
    python3 -m modelpilot.miss_detector facts --split holdout --prereg docs/<prereg>.md --out runs/...

$0: reads recorded files only, runs nothing and sends nothing.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

from . import bench_tasks, delegation, regrade

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT/'runs'
COUNTED_STOPS = ('success', 'turn_limit', 'budget_stop')  # item 8's: the session ended for a model reason
HOLDOUT_SEED = 8
DEV_LAST = 'bench-20261010-090944'
DEV_FIRST = 'bench-20260929-000001'
EDITS = ('Edit', 'Write', 'MultiEdit', 'NotebookEdit')
HEDGES = re.compile(r"\b(however|but note|note that|not (?:yet )?(?:tested|verified|sure)|couldn't|could not|"
                    r"unable|might|may need|assum\w*|TODO|limitation\w*|caveat\w*)\b", re.I)


def split_runs(split):
    """The run directories of a split, by the rule in the module docstring."""
    out = []
    for run in sorted(RUNS.glob('bench-2026*')):
        manifest = run/'manifest.json'
        if not manifest.exists():
            continue
        seed = json.loads(manifest.read_text()).get('seed')
        if split == 'dev' and DEV_FIRST <= run.name <= DEV_LAST and seed != HOLDOUT_SEED:
            out.append(run)
        elif split == 'holdout' and seed == HOLDOUT_SEED:
            out.append(run)
    return out


def counted(record):
    from .bench_report import ineligible_reason
    return (record.get('complete', True) and not ineligible_reason(record)
            and record.get('pass_rule') == 'hidden_and_edge'
            and (record.get('client') or {}).get('stop') in COUNTED_STOPS)


def conversation(path):
    """The client's final conversation from client.stdout.jsonl: its user and assistant messages, in order."""
    messages = []
    if not path.exists():
        return messages
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('type') in ('assistant', 'user') and isinstance(event.get('message'), dict):
            messages.append(event['message'])
    return messages


def diff_facts(text, test_dir):
    """Lines and files the saved diff changes, source apart from tests, and test functions it adds."""
    out = dict.fromkeys(('source_files', 'test_files', 'source_added', 'source_deleted', 'test_added',
                         'test_deleted', 'tests_defined'), 0)
    kind = None
    for line in text.splitlines():
        if line.startswith('diff --git '):
            path = line.split(' b/', 1)[-1]
            kind = 'test' if regrade.is_test_path(path, test_dir) else 'source'
            out[f'{kind}_files'] += 1
        elif kind and line.startswith('+') and not line.startswith('+++'):
            out[f'{kind}_added'] += 1
            out['tests_defined'] += bool(re.match(r'\+\s*(?:async\s+)?def test\w*\(', line))
        elif kind and line.startswith('-') and not line.startswith('---'):
            out[f'{kind}_deleted'] += 1
    out['source_lines'] = out['source_added'] + out['source_deleted']
    return out


def tool_facts(messages):
    """What the agent did, from its tool calls and the outputs the client returned: edits, reads, searches, shell
    calls, its test runs (delegation's parser), and whether a test run came after its last edit."""
    order, counts = [], {'edits': 0, 'reads': 0, 'searches': 0, 'bash': 0}
    for message in messages:
        content = message.get('content')
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get('type') == 'tool_use':
                name = block.get('name')
                if name in EDITS:
                    counts['edits'] += 1
                    order.append(('edit', block.get('id')))
                elif name == 'Read':
                    counts['reads'] += 1
                elif name in ('Grep', 'Glob'):
                    counts['searches'] += 1
                elif name == 'Bash':
                    counts['bash'] += 1
                    order.append(('bash', block.get('id')))
    runs = delegation.agent_test_results(messages)
    test_ids = {c for c, *_ in delegation.test_calls(messages)}
    last_edit = max((i for i, (k, _) in enumerate(order) if k == 'edit'), default=-1)
    tested_after = any(k == 'bash' and c in test_ids for k, c in order[last_edit + 1:])
    last = runs[-1]['failures'] if runs else None
    return dict(counts, test_calls=len(test_ids), readable_test_runs=len(runs),
                ever_failed_tests=any(r['failures'] for r in runs), last_test_failures=last,
                last_tests_passed=last == 0, tested_after_last_edit=tested_after)


def final_text(messages):
    for message in reversed(messages):
        if message.get('role') != 'assistant':
            continue
        content = message.get('content')
        texts = [b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text'] \
            if isinstance(content, list) else [content or '']
        text = '\n'.join(t for t in texts if t)
        if text.strip():
            return text
    return ''


def trial_facts(trial_dir, record, task):
    from .bench_report import cold_cost
    trial_dir = Path(trial_dir)
    diff = trial_dir/'agent.diff'
    messages = conversation(trial_dir/'client.stdout.jsonl')
    text = final_text(messages)
    accounting = record.get('accounting') or {}
    tokens = accounting.get('proxy_tokens') or {}
    return {'requests': accounting.get('requests'), 'output_tokens': tokens.get('output_tokens'),
            'cost_usd': cold_cost(record), 'wall_seconds': record.get('wall_seconds'),
            'turn_limit': (record.get('client') or {}).get('stop') == 'turn_limit',
            **diff_facts(diff.read_text() if diff.exists() else '', task['test_dir']),
            **tool_facts(messages),
            'text_chars': len(text), 'text_hedges': len(HEDGES.findall(text))}


def collect(runs, rates):
    """One row per counted trial: its identity, label (strict pass, and the hidden grader's alone) and facts."""
    from .bench_report import hidden_passed, load_run
    tasks = {t['id']: t for t in bench_tasks.load()}
    rows = []
    for run in runs:
        for record in load_run(run, rates)[1]:
            if not counted(record) or record['task'] not in tasks:
                continue
            trial_dir = run/record['task']/record['arm']/str(record['trial'])
            rows.append({'run': run.name, 'task': record['task'], 'arm': record['arm'], 'trial': record['trial'],
                         'model': record.get('model'), 'effort': record.get('effort'),
                         'strict_pass': bool(record['passed']), 'hidden_pass': hidden_passed(record),
                         'facts': trial_facts(trial_dir, record, tasks[record['task']])})
    return rows


def tests_hitting(files):
    """Every test id that ran a watched line, in one trace."""
    return {t for trace in files.values() for hits in (trace.get('hits') or {}).values() for t in hits if t}


def line_coverage(source, files, tree, only=None):
    """Changed executable source lines run inside a test (any test, or with only, a test that predicate accepts)."""
    executable = run = 0
    for path, change in source.items():
        trace = files.get(str((Path(tree)/path).resolve())) or {}
        mine = set(change['added']) & set(trace.get('executable') or ())
        hits = trace.get('hits') or {}
        executable += len(mine)
        run += sum(any(t and (only is None or only(t)) for t in hits.get(str(n), ())) for n in mine)
    return {'executable': executable, 'run_in_tests': run,
            'fraction': round(run / executable, 3) if executable else None}


def executed_checks(task, python, scratch, diff, repo=None):
    """The host's executed checks on one saved fix (module docstring of `checks`): the task's suite on the agent's
    tree, its own tests included, then on the agent's source with the base's tests, each traced over the changed
    source lines. Applying the diff and running the tests executes agent-written code, as grading does."""
    from . import exposure
    scratch = Path(scratch)
    tree, error = exposure.change_tree(task, scratch, diff, repo)
    if error:
        return {'error': error}
    lines = exposure.changed_lines(tree)
    source = {p: v for p, v in lines.items() if p.endswith('.py') and not v.get('deleted_file')
              and not regrade.is_test_path(p, task['test_dir'])}
    agent_tests = sorted(p for p in lines if regrade.is_test_path(p, task['test_dir']) and not lines[p].get('deleted_file'))
    watch = {p: set(v['added']) for p, v in source.items() if v['added']}
    out = {'source_files': sorted(source), 'agent_test_files': agent_tests}
    if not watch:
        out['error'] = 'no_source_lines'
        return out
    for name in ('whole', 'base'):
        (scratch/name).mkdir(parents=True, exist_ok=True)
    whole = exposure.trace(tree, task, python, scratch/'whole', watch)
    exposure.keep_source_only(tree, task)
    base = exposure.trace(tree, task, python, scratch/'base', watch)
    for name, traced in (('whole', whole), ('base', base)):
        if traced.get('error'):
            out[name] = {'error': traced['error']}
            continue
        files = traced.get('files') or {}
        out[name] = dict(traced['suite'], coverage=line_coverage(source, files, tree))
    if 'error' not in out['whole'] and 'error' not in out['base']:
        # The agent's tests: those that run a changed line with its test edits and not without them (new tests, or
        # existing ones it changed). Same source both times, so an unchanged test runs the same lines.
        mine = tests_hitting(whole.get('files') or {}) - tests_hitting(base.get('files') or {})
        out['whole']['agent_tests_hitting'] = len(mine)
        out['whole']['agent_test_coverage'] = line_coverage(source, whole.get('files') or {}, tree,
                                                            only=lambda t: t in mine)
    return out


def check_facts(result):
    """The flat facts a rule reads from executed_checks' result (None where a run failed to give them)."""
    whole, base = result.get('whole') or {}, result.get('base') or {}
    def ran(r):
        return r and 'error' not in r and r.get('tests_run')
    return {'suite_failed': None if not ran(whole) else bool(whole.get('tests_failed') or whole.get('exit_code')),
            'agent_tests_added_run': None if not (ran(whole) and ran(base)) else whole['tests_run'] - base['tests_run'],
            'base_suite_failed': None if not ran(base) else bool(base.get('tests_failed') or base.get('exit_code')),
            'changed_executable': (whole.get('coverage') or base.get('coverage') or {}).get('executable'),
            'coverage_all_tests': (whole.get('coverage') or {}).get('fraction'),
            'coverage_agent_tests': (whole.get('agent_test_coverage') or {}).get('fraction'),
            'coverage_base_tests': (base.get('coverage') or {}).get('fraction'),
            'check_error': result.get('error') or whole.get('error') or base.get('error')}


def run_checks(runs, rates, out, jobs=4):
    """executed_checks on every counted trial of these runs, in parallel, into out/checks.json (rows keyed as facts')."""
    from concurrent.futures import ThreadPoolExecutor
    import shutil
    import subprocess
    import time
    from .bench_report import load_run
    tasks = {t['id']: t for t in bench_tasks.load()}
    work = [(run, r) for run in runs for r in load_run(run, rates)[1] if counted(r) and r['task'] in tasks]
    python, scratch = bench_tasks.interpreter(), Path(out)/'scratch'

    def one(item):
        index, (run, record) = item
        trial = run/record['task']/record['arm']/str(record['trial'])
        try:
            return executed_checks(tasks[record['task']], python, scratch/str(index), trial/'agent.diff')
        except (subprocess.CalledProcessError, OSError) as error:
            return {'error': type(error).__name__}
        finally:
            shutil.rmtree(scratch/str(index), ignore_errors=True)
    started = time.monotonic()
    with ThreadPoolExecutor(max(1, jobs)) as pool:
        results = list(pool.map(one, enumerate(work)))
    shutil.rmtree(scratch, ignore_errors=True)
    return {'python': python, 'seconds': round(time.monotonic() - started, 1),
            'rows': [{'run': run.name, 'task': r['task'], 'arm': r['arm'], 'trial': r['trial'],
                      'strict_pass': bool(r['passed']), 'checks': check_facts(res), 'detail': res}
                     for (run, r), res in zip(work, results)]}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    facts = sub.add_parser('facts', help='post-run facts and strict labels of one split, $0')
    facts.add_argument('--split', choices=('dev', 'holdout'), required=True)
    facts.add_argument('--prereg', help='the committed pre-registration (required for the holdout)')
    facts.add_argument('--out', required=True)
    checks = sub.add_parser('checks', help='executed checks on each counted fix of one split, $0 (runs agent code)')
    checks.add_argument('--split', choices=('dev', 'holdout'), required=True)
    checks.add_argument('--prereg', help='the committed pre-registration (required for the holdout)')
    checks.add_argument('--out', required=True, help='a new directory')
    checks.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    out = Path(args.out)
    if out.exists():
        parser.error(f'{out} exists: use a new path')
    prereg = None
    if args.split == 'holdout':
        if not args.prereg:
            parser.error('the holdout is read once, after the rule is fixed: pass --prereg <committed file>')
        prereg = {'path': args.prereg, 'sha256': sha256(args.prereg)}
    from .bench import code_revision, rates
    runs = split_runs(args.split)
    if args.command == 'checks':
        out.mkdir(parents=True)
        report = run_checks(runs, rates(), out, args.jobs)
        report = dict(split=args.split, prereg=prereg, code=code_revision(), runs=[r.name for r in runs], **report)
        with (out/'checks.json').open('x') as f:
            json.dump(report, f, indent=1)
            f.write('\n')
        errors = sum(bool(r['checks']['check_error']) for r in report['rows'])
        print(f"{args.split}: {len(report['rows'])} fixes checked, {errors} with errors, {report['seconds']} s -> "
              f"{out/'checks.json'}")
        return
    rows = collect(runs, rates())
    out.write_text(json.dumps({'split': args.split, 'prereg': prereg, 'code': code_revision(),
                               'runs': [r.name for r in runs], 'trials': len(rows),
                               'misses': sum(not r['strict_pass'] for r in rows), 'rows': rows}, indent=1) + '\n')
    print(f"{args.split}: {len(rows)} counted trials, {sum(not r['strict_pass'] for r in rows)} strict misses -> {out}")


if __name__ == '__main__':
    main()
