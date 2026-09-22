"""M6 paired fixed-model/effort smoke benchmark. Offline plan by default."""
import argparse
import hashlib
import json
import os
import getpass
from pathlib import Path
import time
from .cache_probe import send, cost, error_details

CORPUS_VERSION = 'm6-smoke-v1'
TASKS = [
    {'id': 'extract', 'prompt': 'Return the value of TARGET. Records: OLD=amber; TARGET=cobalt; OTHER=maple.', 'answer': 'cobalt'},
    {'id': 'absent', 'prompt': 'Return the value of TARGET, or null if absent. Records: OLD=amber; OTHER=maple.', 'answer': None},
    {'id': 'arithmetic', 'prompt': 'Return the integer sum of 17, 28 and -9.', 'answer': 36},
    {'id': 'test_report', 'prompt': 'A completed test run has exit_code=1, passed_tests=7, failed_tests=1. Return boolean true if the run succeeded, otherwise false.', 'answer': False},
]


def plan(config):
    arms = [{'id': model + '/' + effort, 'model': model, 'effort': effort}
            for model in config['models'] for effort in config['efforts']]
    if not arms or len({a['id'] for a in arms}) != len(arms):
        raise ValueError('Need unique model/effort arms')
    return [{'task': task, 'arm': arm} for i, task in enumerate(TASKS)
            for arm in arms[i % len(arms):] + arms[:i % len(arms)]]


def grade(response, expected):
    if response.get('stop_reason') != 'end_turn':
        return False
    try:
        text = ''.join(b['text'] for b in response['content'] if b['type'] == 'text')
        value = json.loads(text)
        return (isinstance(value, dict) and set(value) == {'answer'} and
                type(value['answer']) is type(expected) and value['answer'] == expected)
    except (ValueError, KeyError, TypeError):
        return False


def summarize(rows, config):
    result = []
    for arm in dict.fromkeys(item['arm']['id'] for item in plan(config)):
        completed = [r for r in rows if r['arm'] == arm]
        known = sum(r['cost_usd'] or 0 for r in completed)
        result.append({'arm': arm, 'completed': len(completed), 'expected': len(TASKS),
                       'complete': len(completed) == len(TASKS),
                       'passes': sum(r['passed'] for r in completed),
                       'pass_rate': sum(r['passed'] for r in completed) / len(completed) if completed else None,
                       'known_cost_usd': known,
                       'cost_complete': all(r['cost_usd'] is not None for r in completed),
                       'wall_seconds': sum(r['wall_seconds'] for r in completed)})
    return result


def run(config, destination, sender=send, stop_usd=1):
    if not isinstance(stop_usd, (float, int)) or isinstance(stop_usd, bool) or not 0 < stop_usd <= 1:
        raise ValueError('Stop threshold must be in (0, 1] USD')
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = {'corpus_version': CORPUS_VERSION, 'tasks': TASKS, 'config': config,
                'max_tokens': 1024, 'thinking': {'type': 'adaptive'},
                'stop_usd': stop_usd, 'limits': 'Post-request stop threshold, not a hard billing cap; no retries.'}
    encoded = json.dumps(manifest, sort_keys=True)
    (destination / 'manifest.json').write_text(encoded + '\n')
    rows = []
    started = time.monotonic()
    state = 'complete'
    for item in plan(config):
        task, arm = item['task'], item['arm']
        request = {'model': arm['model'], 'max_tokens': 1024, 'thinking': {'type': 'adaptive'},
                   'output_config': {'effort': arm['effort']},
                   'system': 'Answer the supplied task. Return only a JSON object with one key: answer. No markdown.',
                   'messages': [{'role': 'user', 'content': task['prompt']}]}
        tick = time.monotonic()
        row = {'task_id': task['id'], 'arm': arm['id'], 'passed': False, 'cost_usd': None}
        try:
            response, _ = sender(request, {})
            row.update(response=response, usage=response['usage'], passed=grade(response, task['answer']))
            if response.get('model') == arm['model']:
                row['cost_usd'] = cost(response['usage'], config['rates'][arm['model']], '5m')
        except Exception as exc:
            row.update(error_type=type(exc).__name__, **error_details(exc))
        row['wall_seconds'] = time.monotonic() - tick
        rows.append(row)
        with (destination / 'observations.jsonl').open('a') as log:
            log.write(json.dumps(row) + '\n')
        print(task['id'], arm['id'], 'ERROR' if 'error_type' in row else 'PASS' if row['passed'] else 'FAIL', flush=True)
        if 'error_hint' in row:
            print(row['error_hint'], 'Reason:', row.get('reason_type', row['error_type']), 'errno:', row.get('errno'), flush=True)
        if row['cost_usd'] is None:
            state = 'stopped_unknown_cost'
            break
        if sum(r['cost_usd'] for r in rows) >= stop_usd and len(rows) < len(plan(config)):
            state = 'stopped_budget'
            break
    report = {'status': state, 'corpus_version': CORPUS_VERSION,
              'manifest_sha256': hashlib.sha256(encoded.encode()).hexdigest(),
              'calls': len(rows), 'wall_seconds': time.monotonic() - started,
              'stop_error': {k: rows[-1][k] for k in ('error_type', 'error_category', 'error_hint', 'reason_type', 'errno') if k in rows[-1]} if rows else {},
              'arms': summarize(rows, config), 'routing_applied': False,
              'missing_comparisons': ['Integrated ModelPilot governor', 'jev-router repository/revision and adapter'],
              'scope': 'Four toy tasks; fixed-model direct-API smoke comparison only. No cache, coding-agent or savings claim.'}
    (destination / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / 'configs/m0.json').read_text())
    if not args.live:
        print(json.dumps({'mode': 'plan', 'calls': len(plan(config)), 'tasks_per_arm': len(TASKS),
                          'max_output_tokens_per_call': 1024, 'paid_calls_made': 0,
                          'missing': ['Integrated governor', 'jev-router baseline']}, indent=2))
        return
    key = os.environ.get('ANTHROPIC_API_KEY') or getpass.getpass('API key (hidden): ').strip()
    if not key.startswith('sk-ant-') or any(c.isspace() for c in key):
        raise SystemExit('Invalid key format; no requests sent.')
    os.environ['ANTHROPIC_API_KEY'] = key
    out = args.out or root / 'runs' / ('m6-baselines-' + time.strftime('%Y%m%d-%H%M%S'))
    print('Running fixed baseline smoke comparison; results: ' + str(out), flush=True)
    report = run(config, out)
    if report['status'] != 'complete':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
