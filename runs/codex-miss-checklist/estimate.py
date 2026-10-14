"""$0 estimate of the requirements checklist over the dev split, without the dev runs (absent on the machine that
wrote it, October 14). Run with the codex/miss-checklist branch importable:

    PYTHONPATH=<checkout of codex/miss-checklist> python3.12 runs/codex-miss-checklist/estimate.py

Inputs: the 29 dev tasks' instructions (bench/tasks), 453 counted dev trials and 40 strict misses
(runs/miss-detector-dev-20261013-171447.json, as the changelog reports them), spread evenly over the tasks, and each
trial's diff taken as its task's upstream reference diff (reference-diff-sizes.json, from reference_diff_sizes.py),
at 1x and at 3x (agents add tests and context). On the main machine, `python3 -m modelpilot.miss_checklist plan` gives
the exact figure from the saved diffs instead.
"""
import json
from pathlib import Path
import sys
from modelpilot import bench, bench_tasks, miss_checklist as mc

here = Path(__file__).resolve().parent
sizes = json.loads((here/'reference-diff-sizes.json').read_text())
tasks = [t for t in bench_tasks.load() if t['id'] in sizes]
assert len(tasks) == 29, len(tasks)
TRIALS, MISSES = 453, 40
counts = {t['id']: TRIALS // 29 + (i < TRIALS % 29) for i, t in enumerate(tasks)}
rates = bench.rates()
report = {'inputs': {'trials': TRIALS, 'misses': MISSES, 'tasks': 29, 'trials_per_task': 'even (15-16)',
                     'diff_bytes': 'reference diff x 1 and x 3', 'bytes_per_token': mc.BYTES_PER_TOKEN,
                     'expected_output_tokens': mc.EXPECTED_OUTPUT, 'fix_usd': mc.LATE_FIX_USD, 'redo_usd': mc.REDO_USD},
          'estimates': {}}
for factor in (1, 3):
    trials = [{'task': t, 'diff_bytes': sizes[t]['diff_bytes'] * factor} for t, n in counts.items() for _ in range(n)]
    for model in mc.MODELS:
        e = mc.estimate(tasks, trials, rates, model)
        e['break_even_damage_usd'] = mc.break_even_grid(e['expected_per_trial_usd'], MISSES / TRIALS)
        report['estimates'][f'{model}, diffs x{factor}'] = e
json.dump(report, sys.stdout, indent=1)
print()
