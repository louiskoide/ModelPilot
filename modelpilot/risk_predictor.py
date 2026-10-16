"""Turn-start risk predictor (October 14, user request): can Haiku 5.5, reading a task's instruction and the source it
names, pick out the tasks the cheap setting misses?

Item 8 asked this of what a router knows at a turn start (Jev's answers, the instruction's length and stated
expectations) and failed. Since then the post-run detectors failed too (October 13–14): Opus's tests catch the misses
only when Opus writes them while solving. This is the last cheap try at a predictor, option (a) of the October 14
discussion: a Haiku 5.5 request per answer, at about $0.004, reads the instruction plus excerpts of the base commit's
source and rates the chance that the agent's change fails the maintainers' tests. Pre-registered before any request:
docs/m6-benchmark-plan.md, "Turn-start risk predictor: pre-registration".

Tasks: the 29 dev tuning tasks and item 8's 38 test tasks (the "item-8-labels" and "sqlglot-stage-1" batches), less
nx-generalized-petersen (a defective spec, October 11). Never a final-split task. The context comes from the base
commit only (`git grep`/`git show` on the task's repository under work/bench/repos): non-test Python files that
mention a code name from the instruction, ranked by how specific the names they mention are, whole or as excerpts
around those names, up to MAX_CONTEXT_BYTES. Nothing from the reference fix, the hidden tests or the edge suite.

    python3 -m modelpilot.risk_predictor rate --out runs/risk-predictor-<ts> [--live] [--limit-usd 2]
    python3 -m modelpilot.risk_predictor score runs/risk-predictor-<ts>          # $0

rate: SAMPLES requests per task. Without --live it writes the prompts and a plan (the most each request can cost)
and sends nothing. With --live (ANTHROPIC_API_KEY in the environment) each request is sent once, with no retry and no
fallback, and none once the next one's maximum cost would take the total past --limit-usd (a stopping threshold, not a
billing cap). score: labels as item 8's (a setting misses a task if any counted trial of it failed; M = low concise or
Sonnet 5.5 medium misses), from the dev split's runs for dev tasks and item 8's seed-8 runs for test tasks (task-level
pass/fail only, as item 8 published them; no diff or transcript is read). Then the pre-registered test.
"""
import argparse
from collections import defaultdict
import json
import keyword
import builtins
from math import comb, log
import os
from pathlib import Path
import re
import statistics as st
import subprocess

from . import bench_tasks, miss_checklist

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT/'runs'
MODEL, EFFORT = 'claude-haiku-5-5', 'low'
MAX_TOKENS, SAMPLES, TIMEOUT = 6000, 3, 300
MAX_CONTEXT_BYTES = 90_000  # about 30k tokens: inside Haiku 5.5's cheaper tier (under 100k prompt tokens)
EXCERPT_LINES = 30  # lines kept either side of a line naming a code name, when a file doesn't fit whole
TEST_BATCHES = ('item-8-labels', 'sqlglot-stage-1')  # item 8's 38 scored test tasks
EXCLUDED = {'nx-generalized-petersen': 'defective spec (October 11): every trial failed one hidden assertion'}
SETTINGS = {'sonnet-5.5-low-concise': 'cheap', 'sonnet-5.5': 'sonnet', 'opus-5.5': 'opus'}
ALPHA = 0.05
ITEM8_SCORE = RUNS/'item8-prereg-score-20261011.json'
NOT_SOURCE = {'test', 'tests', 'testing', 'doc', 'docs', 'benchmarks', 'examples', 'scripts'}
COMMON = set(keyword.kwlist) | set(dir(builtins)) | {'self', 'cls'}
NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')

SYSTEM = (
    'You estimate, before any work starts, how likely a coding agent is to get a change wrong. The agent is a capable, '
    'economical model working alone in the repository: it reads the code, makes the change, runs the tests it writes, '
    'and stops when it believes it is done. Its finished change is then graded by the maintainers\' own tests, which it '
    'never sees. Changes that fail them usually pass the agent\'s own tests: they miss something the request implies '
    'rather than states, a second code path that needs the same change, an edge case of the input, or an exact value or '
    'message. Read the request and the source below, and judge how easy those are to miss here. Reply with one ```json '
    'block holding an object with the keys "risk" (an integer from 0 to 100: the chance in percent that the agent\'s '
    'change fails the maintainers\' tests), "traps" (up to 5 short strings: what it would most likely miss) and '
    '"reason" (one sentence).')


def tasks():
    """The pre-registered tasks: (spec, part) for the dev tuning tasks and item 8's test tasks, less EXCLUDED."""
    splits = json.loads((ROOT/'bench'/'splits.json').read_text())
    tuning, final = set(splits['tuning']), set(splits['final'])
    out = []
    for spec in bench_tasks.load():
        if spec['id'] not in tuning or spec['id'] in final or spec['id'] in EXCLUDED:
            continue
        batch = spec.get('batch')
        if batch is None:
            out.append((spec, 'dev'))
        elif batch in TEST_BATCHES:
            out.append((spec, 'test'))
    return out


def code_names(instruction):
    """Names in the instruction that look like code: dotted names' parts, calls, names with an underscore or an inner
    capital, and anything in backticks; three characters or more, Python keywords and builtins left out."""
    found = set()
    for span in re.findall(r'`([^`]+)`', instruction):
        found.update(NAME.findall(span))
    for match in re.finditer(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+', instruction):
        found.update(match.group(0).split('.'))
    for match in NAME.finditer(instruction):
        word = match.group(0)
        following = instruction[match.end():match.end() + 1]
        if following == '(' or '_' in word.strip('_') or re.search(r'[a-z][A-Z]|[A-Z][a-z]+[A-Z]', word):
            found.add(word)
    return sorted(n for n in found if len(n) >= 3 and n not in COMMON)


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, check=True).stdout


def source_files(repo, base):
    """{path: bytes} of the base commit's non-test Python files."""
    files = {}
    for line in git(repo, 'ls-tree', '-r', '-l', base).splitlines():
        meta, path = line.split('\t', 1)
        parts = path.split('/')
        name = parts[-1]
        if (not path.endswith('.py') or NOT_SOURCE & set(parts[:-1]) or name.startswith('test_')
                or name.endswith('_test.py') or name == 'conftest.py'):
            continue
        files[path] = int(meta.split()[3])
    return files


def mentions(repo, base, name, paths):
    """The paths among these whose base-commit text has the name as a whole word."""
    result = subprocess.run(['git', '-C', str(repo), 'grep', '-l', '-w', '-F', '-e', name, base, '--', '*.py'],
                            capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip())
    found = {line.split(':', 1)[1] for line in result.stdout.splitlines() if ':' in line}
    return found & set(paths)


def excerpt(text, names, budget):
    """Lines within EXCERPT_LINES of a line naming one of the names, merged, in file order, up to budget bytes."""
    lines = text.splitlines()
    pattern = re.compile(r'\b(?:' + '|'.join(re.escape(n) for n in names) + r')\b')
    hits = [i for i, line in enumerate(lines) if pattern.search(line)]
    windows = []
    for i in hits:
        lo, hi = max(0, i - EXCERPT_LINES), min(len(lines), i + EXCERPT_LINES + 1)
        if windows and lo <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], hi)
        else:
            windows.append([lo, hi])
    parts, used = [], 0
    for lo, hi in windows:
        chunk = f'# lines {lo + 1}-{hi}\n' + '\n'.join(lines[lo:hi]) + '\n'
        if used + len(chunk.encode()) > budget:
            break
        parts.append(chunk)
        used += len(chunk.encode())
    return ''.join(parts)


def context(task, repo=None, max_bytes=MAX_CONTEXT_BYTES):
    """The source a host can give at the turn start: (text, record). Files that mention the instruction's code names,
    by the summed rarity of the names they mention (log of one more than the source files over the files mentioning
    the name, so a one-file repository still ranks), then smaller
    first, then path; whole while they fit, else as excerpts around the names."""
    repo = Path(repo) if repo else bench_tasks.REPOS/task['repo']
    names = code_names(task['instruction'])
    files = source_files(repo, task['base'])
    hits = {n: mentions(repo, task['base'], n, files) for n in names}
    weight = {n: log((len(files) + 1) / len(found)) for n, found in hits.items() if found}  # > 0 even in a one-file repo
    scores = defaultdict(float)
    matched = defaultdict(list)
    for n, found in hits.items():
        for path in found:
            scores[path] += weight[n]
            matched[path].append(n)
    ranked = sorted((p for p in scores if scores[p] > 0), key=lambda p: (-scores[p], files[p], p))
    blocks, record, used = [], [], 0
    for path in ranked:
        left = max_bytes - used
        if left < 500:
            break
        text = git(repo, 'show', f"{task['base']}:{path}")
        whole = len(text.encode()) + 200 <= left
        body = text if whole else excerpt(text, matched[path], left - 200)
        if not body.strip():
            continue
        block = f"=== {path} ({'whole file' if whole else 'excerpts'}) ===\n```python\n{body.rstrip()}\n```\n\n"
        blocks.append(block)
        used += len(block.encode())
        record.append({'path': path, 'whole': whole, 'bytes': len(block.encode()), 'names': sorted(matched[path])})
    return ''.join(blocks), {'names': names, 'source_files': len(files), 'files': record, 'bytes': used}


def prompt(task, source):
    head = (f"Repository: {task['repo']} ({task['url']}).\n\nChange request:\n\n{task['instruction']}\n\n")
    if not source:
        return head + 'No source file in the repository mentions a code name from the request.\n\nEstimate the risk.'
    return (head + 'Source at the commit the agent starts from: the files that mention code names from the request, '
            'the most specific first, whole or as excerpts around those names. Not every relevant file may be here.\n\n'
            + source + 'Estimate the risk.')


def request(task, source):
    """One request. Haiku 5.5 thinks adaptively; effort is set, never left at the model's default."""
    return {'model': MODEL, 'max_tokens': MAX_TOKENS, 'output_config': {'effort': EFFORT}, 'system': SYSTEM,
            'messages': [{'role': 'user', 'content': prompt(task, source)}]}


def parse_risk(text):
    """{'risk', 'traps', 'reason'} from the reply's JSON object, or None: risk must be a number from 0 to 100."""
    found = re.search(r'```(?:json)?\s*\n(.*?)```', text, re.S)
    candidates = [found.group(1)] if found else []
    if '{' in text:
        candidates.append(text[text.find('{'):text.rfind('}') + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        risk = value.get('risk') if isinstance(value, dict) else None
        if isinstance(risk, bool) or not isinstance(risk, (int, float)) or not 0 <= risk <= 100:
            return None
        traps = value.get('traps') if isinstance(value.get('traps'), list) else []
        return {'risk': risk, 'traps': [str(t)[:300] for t in traps[:5]], 'reason': str(value.get('reason', ''))[:500]}
    return None


def rate(out, chosen, rates, live=False, limit_usd=2.0, transport=None, repo_of=None):
    """SAMPLES requests per task, into out/ (a new directory). chosen: tasks()-shaped (spec, part) pairs."""
    out = Path(out)
    out.mkdir(parents=True)  # a new directory: never overwrite evidence
    from .bench import code_revision
    plan = {'model': MODEL, 'effort': EFFORT, 'max_tokens': MAX_TOKENS, 'samples': SAMPLES,
            'max_context_bytes': MAX_CONTEXT_BYTES, 'system': SYSTEM, 'live': live, 'limit_usd': limit_usd,
            'code': code_revision(), 'tasks': {}}
    (out/'prompts').mkdir()
    items = []
    for spec, part in chosen:
        source, record = context(spec, repo_of(spec) if repo_of else None)
        payload = request(spec, source)
        (out/'prompts'/f"{spec['id']}.json").write_text(json.dumps(payload, indent=2) + '\n')
        plan['tasks'][spec['id']] = dict(record, part=part, spec_sha256=bench_tasks.spec_hash(spec),
                                         max_cost_usd=round(SAMPLES * miss_checklist.max_cost(payload, rates), 4),
                                         expected_cost_usd=round(SAMPLES * miss_checklist.expected_cost(payload, rates, 1500), 4))
        items.extend((f"{spec['id']}#{i}", payload) for i in range(1, SAMPLES + 1))
    for key in ('max_cost_usd', 'expected_cost_usd'):
        plan[key] = round(sum(t[key] for t in plan['tasks'].values()), 4)
    plan['results'] = {}
    if live:
        def parse(key, response):
            answer = parse_risk(miss_checklist.reply_text(response))
            return None if answer is None else dict(answer, status='rated')
        plan['results'], totals = miss_checklist.send_all(
            items, out, rates, limit_usd, transport or miss_checklist.default_transport(), parse)
        plan.update(totals)
    (out/'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    return plan


def flag(scores):
    """Item 8's rule: the third of the scored tasks with the highest score, ties at the cut all flagged."""
    ranked = sorted((v for v in scores.values() if v is not None), reverse=True)
    if not ranked:
        return set()
    cut = ranked[round(len(ranked) / 3) - 1]
    return {t for t, v in scores.items() if v is not None and v >= cut}


def tail(n, m, draws, k):
    """P(K >= k) for K hypergeometric: draws from n tasks of which m are positive."""
    return sum(comb(m, i) * comb(n - m, draws - i) for i in range(k, min(m, draws) + 1)) / comb(n, draws)


def auc(scores, positive):
    pos = [v for t, v in scores.items() if v is not None and positive.get(t)]
    neg = [v for t, v in scores.items() if v is not None and positive.get(t) is False]
    if not pos or not neg:
        return None
    return round(sum(1 if p > q else .5 if p == q else 0 for p in pos for q in neg) / (len(pos) * len(neg)), 3)


def labels(chosen, table=None):
    """task -> item 8's labels and each setting's mean cold-equivalent cost: dev tasks from the dev split's runs, test
    tasks from item 8's seed-8 runs. Task-level pass/fail and cost only."""
    from . import miss_detector
    from .bench import rates
    from .bench_report import cold_cost, load_run
    table = table or rates()
    part = {spec['id']: p for spec, p in chosen}
    outcomes = defaultdict(list)
    for split, wanted in (('dev', 'dev'), ('holdout', 'test')):
        for run in miss_detector.split_runs(split):
            for r in load_run(run, table)[1]:
                if part.get(r['task']) == wanted and r['arm'] in SETTINGS and miss_detector.counted(r):
                    outcomes[r['task'], SETTINGS[r['arm']]].append((bool(r['passed']), cold_cost(r)))
    rows = {}
    for t in part:
        def misses(s):
            runs = outcomes.get((t, s))
            return None if not runs else not all(p for p, _ in runs)
        h, s, o = misses('cheap'), misses('sonnet'), misses('opus')
        costs = {x: [c for _, c in outcomes.get((t, x), []) if c is not None] for x in ('cheap', 'sonnet', 'opus')}
        rows[t] = {'part': part[t], 'M': None if h is None or s is None else h or s,
                   'Y_S': None if h is None or s is None else h and not s,
                   'Y_O': None if h is None or o is None else h and not o,
                   'trials': {x: f"{sum(p for p, _ in outcomes.get((t, x), []))}/{len(outcomes.get((t, x), []))}"
                              for x in ('cheap', 'sonnet', 'opus')},
                   'mean_usd': {x: round(sum(c) / len(c), 4) if c else None for x, c in costs.items()}}
    return rows


def routing(label, flagged, rating_usd_per_task):
    """For an Opus user: flagged tasks to Opus 5.5, the rest to low concise, against each alone; per task, over the
    tasks with both settings' mean cost; and the Y_O tasks the routing leaves on low concise."""
    priced = [t for t, v in label.items() if v['mean_usd']['cheap'] is not None and v['mean_usd']['opus'] is not None]
    if not priced:
        return None
    mean = lambda f: round(sum(f(t) for t in priced) / len(priced), 4)
    return {'tasks': len(priced),
            'routed_usd': round(mean(lambda t: label[t]['mean_usd']['opus' if t in flagged else 'cheap'])
                                + rating_usd_per_task, 4),
            'rating_usd_per_task': round(rating_usd_per_task, 4),
            'all_low_concise_usd': mean(lambda t: label[t]['mean_usd']['cheap']),
            'all_opus_usd': mean(lambda t: label[t]['mean_usd']['opus']),
            'oracle_usd': mean(lambda t: label[t]['mean_usd']['opus' if label[t]['Y_O'] else 'cheap']),
            'Y_O_left_on_low_concise': sorted(t for t in priced if label[t]['Y_O'] and t not in flagged),
            'Y_O': sum(bool(label[t]['Y_O']) for t in priced)}


def score(out, label=None):
    """$0: the pre-registered test over the labelled tasks with a score, then what is reported beside it."""
    out = Path(out)
    plan = json.loads((out/'plan.json').read_text())
    if label is None:
        label = labels([(bench_tasks.load(t)[0], v['part']) for t, v in plan['tasks'].items()])
    answers = defaultdict(list)
    for key, row in plan['results'].items():
        if row.get('status') == 'rated':
            answers[key.rsplit('#', 1)[0]].append(row['risk'])
    risk = {t: st.median(answers[t]) if answers[t] else None for t in plan['tasks']}
    labelled = [t for t in plan['tasks'] if label[t]['M'] is not None]
    scored = {t: risk[t] for t in labelled}  # a task without a usable answer is never flagged
    flagged = flag(scored)
    n, m = len(scored), sum(label[t]['M'] for t in scored)
    k = sum(label[t]['M'] for t in flagged)
    p = tail(n, m, len(flagged), k) if m else None
    verdict = ('inconclusive' if m < 3 else 'pass' if p < ALPHA and 2 * k >= m else 'fail')

    def reading(subset):
        sub = {t: scored[t] for t in subset}
        return {'tasks': len(sub), 'flagged': sum(t in flagged for t in sub),
                **{f'{y}_caught': f"{sum(bool(label[t][y]) for t in sub if t in flagged)}/{sum(bool(label[t][y]) for t in sub)}"
                   for y in ('M', 'Y_S', 'Y_O')},
                'auc_M': auc(sub, {t: label[t]['M'] for t in sub}),
                'auc_Y_O': auc(sub, {t: label[t]['Y_O'] for t in sub})}
    spent = plan.get('spent_usd') or 0.0
    report = {
        'verdict': verdict, 'tasks': n, 'positives_M': m, 'flagged': sorted(flagged), 'caught': k,
        'missed': sorted(t for t in scored if label[t]['M'] and t not in flagged),
        'p': None if p is None else round(p, 5),
        'unscored': sorted(t for t in plan['tasks'] if risk[t] is None),
        'unlabelled': sorted(t for t in plan['tasks'] if label[t]['M'] is None),
        'answers': {t: len(answers[t]) for t in plan['tasks']},
        'readings': {'all': reading(labelled), 'dev': reading([t for t in labelled if label[t]['part'] == 'dev']),
                     'test': reading([t for t in labelled if label[t]['part'] == 'test'])},
        'routing_for_an_opus_user': routing({t: label[t] for t in labelled}, flagged, spent / max(len(plan['tasks']), 1)),
        'spent_usd': plan.get('spent_usd'), 'cost_complete': plan.get('cost_complete'),
        'item8_labels_match': item8_match(label),
        'per_task': {t: dict(label[t], risk=risk[t], answers=answers[t], flagged=t in flagged) for t in plan['tasks']}}
    with (out/'score.json').open('x') as f:  # never overwrite evidence
        json.dump(report, f, indent=1)
        f.write('\n')
    return report


def item8_match(label, path=ITEM8_SCORE):
    """The test tasks' labels against item 8's published ones: None without the file, else the differences."""
    if not Path(path).exists():
        return None
    published = json.loads(Path(path).read_text())['per_task']
    return sorted(t for t, v in label.items() if v['part'] == 'test' and t in published
                  and any(v[y] != published[t][y] for y in ('M', 'Y_S', 'Y_O')))


def main():
    from .bench import rates
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='step', required=True)
    r = sub.add_parser('rate')
    r.add_argument('--out', type=Path, required=True)
    r.add_argument('--live', action='store_true')
    r.add_argument('--limit-usd', type=float, default=2.0)
    s = sub.add_parser('score')
    s.add_argument('out', type=Path)
    args = parser.parse_args()
    if args.step == 'score':
        report = score(args.out)
        print(json.dumps({k: report[k] for k in ('verdict', 'tasks', 'positives_M', 'caught', 'p', 'missed',
                                                  'unscored', 'readings', 'routing_for_an_opus_user')}, indent=1))
        return
    if args.live and not os.environ.get('ANTHROPIC_API_KEY'):
        raise SystemExit('Set ANTHROPIC_API_KEY in the environment; no requests sent.')
    plan = rate(args.out, tasks(), rates(), args.live, args.limit_usd)
    print(json.dumps({k: plan.get(k) for k in ('live', 'expected_cost_usd', 'max_cost_usd', 'limit_usd', 'spent_usd',
                                               'cost_complete')}, indent=1))


if __name__ == '__main__':
    main()
