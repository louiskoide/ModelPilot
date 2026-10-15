"""A session's timeline as one static HTML page (October 15, overnight cloud run; $0).

    python3 -m modelpilot.timeline <proxy log> [--governor DB] --out FILE.html

One row per logged request: its time and the gap since the previous one ended ("overlap" when it started first); model, effort and effective_effort, and the
request kind; tokens by class (uncached input, 5m write, 1h write, read, output); its cost, or "unknown"; the cache
lifetime its breakpoints asked for, whether it came after an idle boundary (m5.idle_boundary) and, for the main loop,
what its cache writes were (bench_report.write_sources's rule: cold start, switch, growth, rebuild after expiry or
after a change; a row with both shows each part, and one that wrote nothing shows none). With --governor, the ledger's plans, notices, corrections and acknowledgements are interleaved by
time (read-only). Totals keep unknown as unknown. No prompt or tool content: the log doesn't carry it, and nothing
here adds it. The page has no scripts, fonts or other external resources.
"""
import argparse
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import sqlite3
from .m5 import idle_boundary
from .proxy import read_log

TOKEN_CLASSES = ('uncached', 'write_5m', 'write_1h', 'read', 'output')
# The governor's journal kinds a reader of a session's timeline wants beside its requests.
EVENT_KINDS = {'plan_rebase': 'plan', 'acknowledge_rebase': 'acknowledged', 'deliver_correction': 'correction delivered',
               'stop_block': 'stop held for a correction', 'queue_change': 'change queued', 'stuck_notice': 'notice',
               'client_stop': 'turn ended', 'suite_result': 'suite result', 'suite_refused': 'suite refused',
               'review': 'review', 'fallback': 'fallback', 'recover': 'orphaned reservations'}


def tokens(row):
    """Tokens by class, None for a class the row's usage doesn't give."""
    usage = row.get('usage') or {}
    split = usage.get('cache_creation') if isinstance(usage.get('cache_creation'), dict) else None
    count = lambda key: usage.get(key) if isinstance(usage.get(key), int) else None
    writes = count('cache_creation_input_tokens')
    return {'uncached': count('input_tokens'),
            'write_5m': split.get('ephemeral_5m_input_tokens') if split else (0 if writes == 0 else None),
            'write_1h': split.get('ephemeral_1h_input_tokens') if split else (0 if writes == 0 else None),
            'read': count('cache_read_input_tokens'), 'output': count('output_tokens')}


def lifetime(row):
    ttls = row.get('cache_ttl') or ()
    return '1h' if '1h' in ttls else '5m' if ttls else None


def annotate(rows):
    """Each row with its gap, idle boundary and write cause (bench_report.write_sources's rule, per row)."""
    out, last_end, last_main, built = [], None, None, {}
    for row in sorted(rows, key=lambda r: r.get('started_unix') or 0):
        started = row.get('started_unix')
        gap = None if started is None or last_end is None else started - last_end
        entry = {'row': row, 'gap_seconds': gap, 'tokens': tokens(row), 'idle_boundary': False, 'cause': None}
        sent = billed(row)
        main = sent and row.get('kind') == 'messages' and bool(row.get('tool_count'))
        if main and last_main is not None and started is not None and last_main.get('started_unix') is not None:
            idle = started - (last_main['started_unix'] + (last_main.get('wall_seconds') or 0))
            entry['idle_boundary'] = idle >= idle_boundary(lifetime(last_main) or '5m')
        usage = row.get('usage') or {}
        read, write = usage.get('cache_read_input_tokens'), usage.get('cache_creation_input_tokens')
        measured = row.get('http_status') == 200 and isinstance(read, int) and isinstance(write, int)
        if sent and not main:
            entry['cause'] = 'side request'
        elif main and not measured:
            entry['cause'] = 'unknown'  # write_sources's unmeasured_requests: what it wrote is unknown
        elif main:
            key = (row.get('model'), row.get('effort'))
            if key not in built:
                parts = {'switch' if built else 'cold start': write}
            else:
                prefix, when, life = built[key]
                lost = min(write, max(0, prefix - read))
                parts = {'rebuild after expiry' if (started or 0) - when > life else 'rebuild after a change': lost,
                         'growth': write - lost}
            parts = {k: n for k, n in parts.items() if n}
            entry['cause'] = (next(iter(parts)) if len(parts) == 1 else
                              ', '.join(f'{k} {n:,}' for k, n in parts.items()) if parts else 'none')
            split = usage.get('cache_creation') if isinstance(usage.get('cache_creation'), dict) else {}
            hour = bool(split.get('ephemeral_1h_input_tokens') or row.get('repriced_1h_write_tokens'))  # as written
            built[key] = (read + write, started or 0, 3600 if hour else 300)
        if main:
            last_main = row
        if started is not None:
            last_end = started + (row.get('wall_seconds') or 0)
        out.append(entry)
    return out


def governor_events(db):
    """The ledger's journal entries worth a row, read-only: (time, label, detail), and corrections from M2."""
    connection = sqlite3.connect(f'file:{Path(db).resolve()}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        out = []
        for e in connection.execute('SELECT kind,payload,created,revision FROM gov_decisions ORDER BY seq'):
            if e['kind'] not in EVENT_KINDS:
                continue
            payload = json.loads(e['payload'])
            if e['kind'] == 'plan_rebase' and payload.get('action') != 'would_rebase':
                continue
            detail = {'plan_rebase': lambda p: f"{p.get('trigger')}: {', '.join(c['kind'] for c in p.get('changes') or [])}"
                                               + (f" (deferred {', '.join(p['deferred_changes'])})" if p.get('deferred_changes') else ''),
                      'acknowledge_rebase': lambda p: f"by {p.get('applied_by')}",
                      'deliver_correction': lambda p: f"revision {p.get('revision')} by {p.get('event')}",
                      'queue_change': lambda p: p.get('kind'),
                      'suite_result': lambda p: f"{p.get('suite')}: {p.get('outcome')}",
                      'suite_refused': lambda p: p.get('refusal'),
                      'review': lambda p: f"{p.get('action')} ({p.get('reason')})",
                      'fallback': lambda p: f"{p.get('final_action')}",
                      'recover': lambda p: f"{p.get('orphaned')} orphaned"}.get(e['kind'], lambda p: '')(payload)
            out.append((e['created'], EVENT_KINDS[e['kind']], str(detail)))
        try:
            for c in connection.execute("SELECT created,revision FROM events WHERE kind='corrected' ORDER BY seq"):
                out.append((c['created'], 'task corrected', f"revision {c['revision']}"))
        except sqlite3.OperationalError:
            pass
        return sorted(out, key=lambda e: e[0])
    finally:
        connection.close()


def billed(row):
    """A request someone is billed for: count_tokens, the model catalog and refusals aren't."""
    return row.get('kind') in ('messages', 'side_call') and row.get('status') != 'refused'


def total(values):
    return None if any(v is None for v in values) else sum(values)


def fmt(value, digits=6):
    if value is None:
        return 'unknown'
    return f'{value:.{digits}f}' if isinstance(value, float) else str(value)


def clock(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime('%H:%M:%S.%f')[:-3] if seconds is not None else ''


CSS = '''body{font:13px/1.4 system-ui,sans-serif;margin:16px;color:#1b1f24;background:#fff}
table{border-collapse:collapse;width:100%}th,td{padding:3px 6px;border-bottom:1px solid #e3e6ea;text-align:right;white-space:nowrap}
th{position:sticky;top:0;background:#f6f8fa}td.l,th.l{text-align:left}tr.event td{background:#f2f7ff;color:#24476b;text-align:left}
td.w{white-space:normal;min-width:14em}tr.boundary td{border-top:2px solid #b35900}.unknown{color:#b3261e;font-weight:600}tfoot td{font-weight:600}
@media (prefers-color-scheme: dark){body{color:#e6e6e6;background:#16181c}th{background:#22252b}td,th{border-color:#30343b}
tr.event td{background:#1d2a3a;color:#bcd4f0}.unknown{color:#ff8a80}}'''


def render(rows, events=(), title='Session timeline'):
    entries = annotate(rows)
    columns = ['time', 'gap s', 'kind', 'model', 'effort', 'effective', *TOKEN_CLASSES, 'cost $', 'lifetime', 'writes', 'status']
    body = []
    pending = list(events)

    def event_rows(until):
        while pending and (until is None or pending[0][0] <= until):
            when, label, detail = pending.pop(0)
            body.append(f'<tr class="event"><td>{html.escape(clock(when))}</td>'
                        f'<td colspan="{len(columns) - 1}">{html.escape(label)}: {html.escape(detail)}</td></tr>')
    for e in entries:
        r = e['row']
        event_rows(r.get('started_unix'))
        gap = e['gap_seconds']
        cells = [clock(r.get('started_unix')), '' if gap is None else 'overlap' if gap < 0 else f'{gap:.1f}', r.get('kind') or '', r.get('model') or '', r.get('effort') or '',
                 r.get('effective_effort') or '', *[fmt(e['tokens'][k]) if billed(r) else '' for k in TOKEN_CLASSES],
                 fmt(r.get('cost_usd')),
                 lifetime(r) or '', (e['cause'] or '') + (' (after an idle boundary)' if e['idle_boundary'] else ''),
                 r.get('status') or '']
        classes = ' class="boundary"' if e['idle_boundary'] else ''
        tds = ''.join(f'<td class="{"l " if i in (2, 3, 4, 5, 13, 14) else ""}{"w " if i == 13 else ""}'
                      f'{"unknown" if c == "unknown" else ""}">'
                      f'{html.escape(str(c))}</td>' for i, c in enumerate(cells))
        body.append(f'<tr{classes}>{tds}</tr>')
    event_rows(None)
    sent = [e for e in entries if billed(e['row'])]
    sums = {k: total([e['tokens'][k] for e in sent]) for k in TOKEN_CLASSES}
    cost = total([e['row'].get('cost_usd') for e in sent])
    foot = ['totals', '', f'{len(sent)} requests', '', '', '', *[fmt(sums[k]) for k in TOKEN_CLASSES], fmt(cost), '', '', '']
    head = ''.join(f'<th class="{"l" if c in ("kind", "model", "effort", "effective", "writes", "status") else ""}">'
                   f'{html.escape(c)}</th>' for c in columns)
    foot_cells = ''.join(f'<td class="{"unknown" if c == "unknown" else ""}">{html.escape(str(c))}</td>' for c in foot)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{CSS}</style></head>'
            f'<body><h1>{html.escape(title)}</h1><p>{len(entries)} logged requests; times UTC. Unknown stays unknown in '
            'the totals. No prompt or tool content.</p>'
            f'<table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody><tfoot><tr>{foot_cells}</tr></tfoot>'
            '</table></body></html>\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('log', type=Path, help="the proxy's observations.jsonl")
    parser.add_argument('--governor', type=Path, help='the ledger database, read-only')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    events = governor_events(args.governor) if args.governor else []
    args.out.write_text(render(read_log(args.log), events, title=f'Timeline: {args.log.parent.name}'))
    print(args.out)


if __name__ == '__main__':
    main()
