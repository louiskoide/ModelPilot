"""Helper-mode report (October 21, $0, offline): what the plugin's helpers cost, from its log (plugin/hooks/
helper-log.sh, helper.sh) and Claude Code's own transcripts, beside the cache rewrite `/model` would have written.

  python3 -m modelpilot.helper_report --out runs/helper-report.json

Measured, at API rates: each helper's requests, nested helpers included; its startup (each helper's first request
without its output: system prompt, tools and brief); the session's side (the output of the request that wrote the
brief, and the turn that relayed the reply); the session's prompt at the launch. Estimated: the rewrite `/model` to
the helper's setting would have written at that prompt (`switch_quote.quote`). est_saving_usd is rewrite - startup -
session side. Not counted: what a helper re-read that the session already held (the estimate overstates the saving),
the new model's reads of the whole conversation and the rewrite on switching back (it understates it), and quality.
Unknown stays unknown: a helper whose transcript is gone (Claude Code deletes them after cleanupPeriodDays, 30 by
default) or whose usage can't be priced is listed with None.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from . import cache_probe, switch_policy as sp, switch_quote

LOG = Path.home()/'.claude/plugins/data/modelpilot-modelpilot/helper-log.jsonl'
NOT_COUNTED = ('re-reads the session already held (overstates the saving); the new model reading the whole '
               'conversation and the rewrite back (understate it); quality')


def read_jsonl(path):
    """A JSONL file's objects, skipping lines that aren't one; None when the file can't be read."""
    try:
        lines = Path(path).read_text().splitlines()
    except (OSError, TypeError):
        return None
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def when(entry):
    try:
        return datetime.fromisoformat(entry['timestamp'].replace('Z', '+00:00')).timestamp()
    except (KeyError, AttributeError, ValueError):
        return None


def requests(entries):
    """A transcript's model requests in order. The client writes one entry per content block, so a request's content
    is all of its entries' and its usage the last one's."""
    out = {}
    for e in entries:
        m = e.get('message') if e.get('type') == 'assistant' else None
        if not isinstance(m, dict) or not isinstance(m.get('usage'), dict):
            continue
        key = e.get('requestId') or m.get('id')
        req = out.setdefault(key, {'model': m.get('model'), 'when': when(e), 'content': []})
        req['usage'] = m['usage']
        req['content'] += [b for b in m.get('content') or [] if isinstance(b, dict)]
    return list(out.values())


def price(req, rates, output=True):
    """One request's dollars on its date, or None under cache_probe.priced_usage's rules; output=False leaves its
    output out."""
    usage = req['usage'] if output else dict(req['usage'], output_tokens=0)
    if cache_probe.priced_usage({'model': req['model']}, req['model'], usage, rates) is None:
        return None
    return cache_probe.cost(usage, rates[req['model']], '5m', req['when'])


def usd(value):
    return None if value is None else round(value, 6)


def total(values):
    values = list(values)
    return None if any(v is None for v in values) else sum(values)


def relay(entries, agent):
    """The session's entries in the turn a helper's task notification started, up to the next prompt."""
    out, on = [], False
    for e in entries:
        content = (e.get('message') or {}).get('content') if e.get('type') == 'user' else None
        if e.get('type') == 'user' and not (isinstance(content, list) and content and
                                            all(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content)):
            text = content if isinstance(content, str) else json.dumps(content)
            on = (e.get('origin') or {}).get('kind') == 'task-notification' and f'<task-id>{agent}</task-id>' in text
        elif on:
            out.append(e)
    return out


def tree(agent, launches):
    """A helper and the helpers it launched, at any depth."""
    out, todo = [], [agent]
    while todo:
        a = todo.pop(0)
        if a and a not in out:
            out.append(a)
            todo += [l.get('agent') for l in launches if l.get('caller') == a]
    return out


def helper_row(top, launches, finished, rates, cfg):
    main = read_jsonl(top.get('transcript'))
    folder = Path(top.get('transcript') or '').with_suffix('')/'subagents'
    agents = tree(top.get('agent'), launches)
    found = {a: read_jsonl(folder/f'agent-{a}.jsonl') for a in agents}
    reqs = {a: requests(entries or []) for a, entries in found.items()}
    missing = [a for a in agents if not reqs[a]]
    helper_usd = None if missing else total(price(r, rates) for a in agents for r in reqs[a])
    startup_usd = None if missing else total(price(reqs[a][0], rates, output=False) for a in agents)
    launch = next((r for r in requests(main or []) for b in r['content']
                   if b.get('type') == 'tool_use' and b.get('id') == top.get('tool_use_id')), None)
    prompt = cache_probe.prompt_tokens(launch['usage']) if launch else None
    full, bare = (price(launch, rates), price(launch, rates, output=False)) if launch else (None, None)
    brief_usd = None if full is None or bare is None else full - bare
    relay_usd = total(price(r, rates) for r in requests(relay(main or [], top.get('agent'))))
    side_usd = None if brief_usd is None or relay_usd is None else brief_usd + relay_usd
    effort = (top.get('helper') or '').rpartition('-')[2]
    current = (launch['model'] if launch else None, top.get('caller_effort') or None)
    target = (reqs[agents[0]][0]['model'] if agents and reqs[agents[0]] else None, effort)
    rewrite_usd = None
    if prompt is not None and {current[0], target[0]} <= set(cfg['models']) \
            and {current[1], target[1]} <= set(cfg['effort_order']):
        rewrite_usd = switch_quote.quote(current, target, prompt, cfg=cfg, rates=rates)['rewrite_usd']
    parts = (rewrite_usd, startup_usd, side_usd)
    return {'session': top.get('session'), 'agent': top.get('agent'), 'helper': top.get('helper'),
            'session_setting': list(current), 'helper_setting': list(target), 'finished': top.get('agent') in finished,
            'nested_helpers': len(agents) - 1, 'helper_requests': sum(len(r) for r in reqs.values()),
            'missing_transcripts': missing, 'session_prompt_tokens': prompt, 'helper_usd': usd(helper_usd),
            'startup_usd': usd(startup_usd), 'session_side_usd': usd(side_usd), 'rewrite_usd': usd(rewrite_usd),
            'est_saving_usd': None if None in parts else usd(rewrite_usd - startup_usd - side_usd)}


def report(log_path=LOG, rates=None, cfg=None):
    from . import bench  # late: bench imports hooks, which imports switch_quote
    rates, cfg = rates or bench.rates(), cfg or sp.load()
    log = read_jsonl(log_path) or []
    launches = [e for e in log if e.get('event') == 'launch']
    finished = {e.get('agent') for e in log if e.get('event') == 'finish'}
    rows = [helper_row(l, launches, finished, rates, cfg) for l in launches if not l.get('caller')]
    priced = [r for r in rows if r['est_saving_usd'] is not None]
    sums = {k: usd(sum(r[k] for r in priced)) for k in ('helper_usd', 'startup_usd', 'session_side_usd', 'rewrite_usd',
                                                   'est_saving_usd')}
    return {'log': str(log_path), 'sessions': len({e.get('session') for e in log}),
            'notes': sum(e.get('event') == 'note' for e in log), 'launches': len(rows),
            'nested_launches': len(launches) - len(rows), 'finished': sum(r['finished'] for r in rows),
            'priced': len(priced), 'totals_over_priced': sums, 'not_counted': NOT_COUNTED, 'helpers': rows}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--log', default=str(LOG))
    p.add_argument('--out')
    a = p.parse_args(argv)
    text = json.dumps(report(a.log), indent=2)
    if a.out:
        Path(a.out).write_text(text + '\n')
    print(text)


if __name__ == '__main__':
    sys.exit(main())
