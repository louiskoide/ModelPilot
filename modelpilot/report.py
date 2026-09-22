"""Summarize observed costs and latency without declaring the M0 gate passed."""
import argparse
import json
from pathlib import Path
import statistics


def summarize(rows):
    groups = {}
    for row in rows:
        group = groups.setdefault(row['group'], [])
        group.append(row)
    results = []
    for name, records in groups.items():
        known_costs = [r['cost_usd'] for r in records if r.get('cost_usd') is not None]
        results.append({
            'group': name,
            'recorded_calls': len(records),
            'errors': sum(r['status'] != 'ok' for r in records),
            'known_cost_usd': sum(known_costs),
            'cost_complete_for_recorded_calls': len(known_costs) == len(records),
            'median_request_seconds': statistics.median(r['wall_seconds'] for r in records),
            'steps': [{'step': r['step'], 'status': r['status'],
                       'observation': r.get('observation', 'unknown'),
                       'read_tokens': r.get('usage', {}).get('cache_read_input_tokens'),
                       'write_tokens': r.get('usage', {}).get('cache_creation_input_tokens')}
                      for r in records],
        })
    return {'gate': 'requires_review',
            'note': 'Recorded observations only. Verify completeness against plan.json and warm controls before drawing conclusions. Costs exclude unreported failed-request billing.',
            'groups': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('observations', type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.observations.read_text().splitlines() if line.strip()]
    print(json.dumps(summarize(rows), indent=2))


if __name__ == '__main__':
    main()
