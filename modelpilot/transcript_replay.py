"""Cache keeper replay over the client's own transcripts (October 17; $0, nothing sent, no message text kept). The test
is pre-registered in docs/governor.md, "Cache keeper: transcript replay, pre-registration": would a keeper, a request
that re-reads a prefix every 55 minutes so its 1h entry never expires, have saved 5% of the month's cost?

    python3 -m modelpilot.transcript_replay [--root ~/.claude/projects] [--out FILE]

From each transcript line only these are kept: the file (one session each; a file under subagents/ is a subagent's),
timestamps, isSidechain, and the response's model, ID and usage. A response the client wrote as several records is
one request, its record with the largest output; one copied into a later session's file (a resume) stays with the session
whose file ends first. Main-loop requests are a session file's records outside a sidechain; the others count in the
total only. A request starts at its first record and ends at its last.

Per request, what a keeper could have saved (tokens, each priced at its write rate less its read rate):
- a resume in the same session after a gap of at least 55 minutes: min(write, max(0, write - (prompt - previous
  prompt))), prompt being input plus reads plus writes; 0 when the prompt shrank (a compaction or clear);
- a session's first request at least 55 minutes after the last main-loop request anywhere: min(write, max(0, S -
  read)), S the nearest-in-time warm start's read (a first request reading at least half its prompt), from the same
  project within 7 days, else any project; scaled by the match rate, the share of first requests under 55 minutes
  after another session that started warm.
A keeper pings every 55 minutes until a request comes or N hours after the last one: each ping reads the kept prefix
(a session's last prompt; S for the shared one) and writes 20 tokens and 1 output token. K_shared(N) keeps the shared
prefix; K_all(N) also each session's context (both priced). Only 1h sessions are kept.

Precondition: at least 5 warm starts more than an hour after the newest earlier request, in any session, writing at
least half of what they read, with no gap of an hour or more between any two requests in between.
"""
import argparse
import json
import statistics
from collections import defaultdict, namedtuple
from datetime import datetime
from pathlib import Path
from . import bench
from .cache_probe import cost, prompt_tokens, tier

PING_SECONDS = 3300
LIFETIME_SECONDS = 3600
HOURS = (1, 2, 4, 8, 16, 24)
PING_WRITE_TOKENS, PING_OUTPUT_TOKENS = 20, 1
NEAR_SECONDS = 7 * 86400
RESUME_EXCESS_TOKENS = 30000
GO_SHARE = 0.05
PRECONDITION_COUNT = 5

Request = namedtuple('Request', 'session project model start end sidechain usage')


def stamp(text):
    try:
        return datetime.fromisoformat(str(text).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def load(root):
    """Requests from every transcript under root, and counts of what was left out."""
    root = Path(root).expanduser()
    found, file_end, skipped = defaultdict(dict), {}, defaultdict(int)
    for path in sorted(root.rglob('*.jsonl')):
        rel = path.relative_to(root)
        session, project, subagent = str(rel), rel.parts[0], 'subagents' in rel.parts
        with open(path, errors='replace') as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    skipped['unparsed_lines'] += 1
                    continue
                if not isinstance(d, dict):
                    continue
                t = stamp(d.get('timestamp')) if d.get('timestamp') else None
                if t is not None:
                    file_end[session] = max(t, file_end.get(session, t))
                m = d.get('message')
                if d.get('type') != 'assistant' or not isinstance(m, dict) or not isinstance(m.get('usage'), dict):
                    continue
                if m.get('model') == '<synthetic>':
                    skipped['synthetic'] += 1
                    continue
                key = m.get('id') or d.get('requestId')
                if t is None or not key:
                    skipped['no_timestamp_or_id'] += 1
                    continue
                seen = found[key].get(session)
                usage = m['usage']
                if seen is None:
                    found[key][session] = Request(session, project, m.get('model'), t, t,
                                                  subagent or bool(d.get('isSidechain')), usage)
                else:
                    best = usage if (usage.get('output_tokens') or 0) > (seen.usage.get('output_tokens') or 0) \
                        else seen.usage
                    found[key][session] = seen._replace(start=min(seen.start, t), end=max(seen.end, t), usage=best)
    requests = []
    for copies in found.values():
        skipped['copies'] += len(copies) - 1
        requests.append(min(copies.values(), key=lambda r: (file_end.get(r.session, r.end), r.session)))
    return sorted(requests, key=lambda r: r.start), dict(skipped)


def writes(usage):
    return usage.get('cache_creation_input_tokens') or 0


def reads(usage):
    return usage.get('cache_read_input_tokens') or 0


def prompt(usage):
    return (usage.get('input_tokens') or 0) + reads(usage) + writes(usage)


def lifetime_tokens(usage):
    creation = usage.get('cache_creation') or {}
    return creation.get('ephemeral_5m_input_tokens') or 0, creation.get('ephemeral_1h_input_tokens') or 0


def flat(rates, r):
    rate = rates.get(r.model)
    return None if rate is None else tier(rate, prompt_tokens(r.usage) if 'input_tokens' in r.usage else None, r.start)


def price(rates, r):
    rate = rates.get(r.model)
    try:
        return None if rate is None else cost(r.usage, rate, '1h', r.start)
    except ValueError:
        return None


def pings(gap, hours):
    """(pings sent, entry still alive) over a pause of gap seconds (None: no one came back) with a keeper that stops
    hours after the last request."""
    limit = hours * 3600
    count, k = 0, 1
    while (gap is None or PING_SECONDS * k < gap) and PING_SECONDS * k <= limit:
        count, k = count + 1, k + 1
    return count, gap is not None and gap <= PING_SECONDS * count + LIFETIME_SECONDS


def ping_usd(kept, rate):
    return (kept * rate['read'] + PING_WRITE_TOKENS * rate['write_1h'] + PING_OUTPUT_TOKENS * rate['output']) / 1e6


def replay(requests, rates):
    total = unpriced = 0
    for r in requests:
        c = price(rates, r)
        if c is None:
            unpriced += 1
        else:
            total += c
    main = [r for r in requests if not r.sidechain and flat(rates, r) is not None]
    sessions = defaultdict(list)
    for r in main:
        sessions[r.session].append(r)
    lifetimes = {}
    for s, rs in sessions.items():
        w5 = sum(lifetime_tokens(r.usage)[0] for r in rs)
        w1 = sum(lifetime_tokens(r.usage)[1] for r in rs)
        lifetimes[s] = '5m' if w5 > w1 else '1h'

    # Warm starts and the shared prefix's size near a time.
    firsts = [rs[0] for rs in sessions.values()]
    warm = [f for f in firsts if prompt(f.usage) and reads(f.usage) >= 0.5 * prompt(f.usage)]

    def shared_size(t, project):
        same = [f for f in warm if f.project == project and abs(f.start - t) <= NEAR_SECONDS]
        pool = same or warm
        return reads(min(pool, key=lambda f: abs(f.start - t)).usage) if pool else None

    # Resumes within a session, and each session's trailing pause (no one came back).
    resumes, trailing, five_minute = [], [], 0.0
    for s, rs in sessions.items():
        if lifetimes[s] == '5m':
            for a, b in zip(rs, rs[1:]):
                if b.start - a.end >= 330 and prompt(b.usage) >= prompt(a.usage):
                    rate = flat(rates, b)
                    avoid = min(writes(b.usage), max(0, writes(b.usage) - (prompt(b.usage) - prompt(a.usage))))
                    five_minute += avoid * (rate['write_5m'] - rate['read']) / 1e6
            continue
        for a, b in zip(rs, rs[1:]):
            gap = b.start - a.end
            if gap < PING_SECONDS:
                continue
            grown = prompt(b.usage) - prompt(a.usage)
            avoid = 0 if grown < 0 else min(writes(b.usage), max(0, writes(b.usage) - grown))
            rate = flat(rates, b)
            resumes.append({'gap': gap, 'avoid_tokens': avoid, 'saving_usd': avoid * (rate['write_1h'] - rate['read']) / 1e6,
                            'ping_usd': ping_usd(prompt(a.usage), flat(rates, a))})
        trailing.append({'gap': None, 'ping_usd': ping_usd(prompt(rs[-1].usage), flat(rates, rs[-1]))})

    # The shared prefix: global pauses between main-loop requests of 1h sessions, and session starts after them.
    timeline = sorted((r for r in main if lifetimes[r.session] == '1h'), key=lambda r: r.start)
    first_of = {rs[0].session: rs[0] for rs in sessions.values()}
    shared_gaps, starts, close, close_warm, last_end, last = [], [], 0, 0, None, None
    for r in timeline:
        if last_end is not None:
            gap = max(0.0, r.start - last_end)
            is_first = first_of.get(r.session) is r
            if is_first and gap < PING_SECONDS:
                close += 1
                close_warm += reads(r.usage) >= 0.5 * prompt(r.usage)
            if gap >= PING_SECONDS:
                size = shared_size(last.end, last.project)
                shared_gaps.append({'gap': gap, 'ping_usd': ping_usd(size or 0, flat(rates, last))})
                if is_first:
                    s = shared_size(r.start, r.project)
                    avoid = 0 if s is None else min(writes(r.usage), max(0, s - reads(r.usage)))
                    rate = flat(rates, r)
                    starts.append({'gap': gap, 'avoid_tokens': avoid, 'shared_unknown': s is None,
                                   'saving_usd': avoid * (rate['write_1h'] - rate['read']) / 1e6,
                                   'ping_usd': shared_gaps[-1]['ping_usd'],
                                   'remainder_usd': (writes(r.usage) - avoid) * (rate['write_1h'] - rate['read']) / 1e6,
                                   'prompt': prompt(r.usage)})
        last_end = r.end if last_end is None else max(last_end, r.end)
        last = r
    if last is not None:
        shared_gaps.append({'gap': None, 'ping_usd': ping_usd(shared_size(last.end, last.project) or 0, flat(rates, last))})
    match = close_warm / close if close else 0.0

    def run(items, hours, scale=1.0):
        saved = spent = 0.0
        for item in items:
            count, alive = pings(item['gap'], hours)
            spent += count * item['ping_usd']
            if alive and 'saving_usd' in item:
                saved += scale * item['saving_usd']
        return saved, spent

    settings = []
    for hours in HOURS:
        shared_ping = sum(pings(g['gap'], hours)[0] * g['ping_usd'] for g in shared_gaps)
        start_saved = sum(match * s['saving_usd'] for s in starts if pings(s['gap'], hours)[1])
        resume_saved, resume_spent = run(resumes + trailing, hours)
        for name, saved, spent in (('K_shared', start_saved, shared_ping),
                                   ('K_all', start_saved + resume_saved, shared_ping + resume_spent)):
            net = saved - spent
            settings.append({'setting': f'{name}({hours}h)', 'saved_usd': round(saved, 4), 'pings_usd': round(spent, 4),
                             'net_usd': round(net, 4), 'share': round(net / total, 4) if total else None,
                             'from_starts_usd': round(start_saved, 4),
                             'from_resumes_usd': round(saved - start_saved, 4)})
    best = max(settings, key=lambda s: s['net_usd'])

    def bound(item):
        count, _ = pings(item['gap'], 10 ** 6)
        return max(0.0, item['saving_usd'] - count * item['ping_usd'])
    upper = sum(bound(i) for i in resumes) + sum(bound(s) for s in starts)

    fresh = statistics.median(prompt(f.usage) for f in firsts) if firsts else 0
    large = [s for s in starts if s['prompt'] >= fresh + RESUME_EXCESS_TOKENS]

    # Precondition: warm starts past an hour after the newest request that could have written what they read.
    everything = sorted(requests, key=lambda r: r.start)
    held = []
    for f in warm:
        writer = max((r for r in everything if r.end < f.start and writes(r.usage) >= 0.5 * reads(f.usage)),
                      key=lambda r: r.end, default=None)
        if writer is None or f.start - writer.end <= LIFETIME_SECONDS:
            continue
        between = [r for r in everything if writer.end < r.start < f.start]
        marks = [writer.end] + [x for r in between for x in (r.start, r.end)] + [f.start]
        marks.sort()
        if all(b - a < LIFETIME_SECONDS for a, b in zip(marks, marks[1:])):
            held.append({'session': f.session, 'age_seconds': round(f.start - writer.end), 'read': reads(f.usage)})
    met = len(held) >= PRECONDITION_COUNT
    share = best['net_usd'] / total if total else 0.0
    return {
        'requests': len(requests), 'unpriced_requests': unpriced, 'main_loop_requests': len(main),
        'sessions': len(sessions), 'sessions_5m': sum(1 for v in lifetimes.values() if v == '5m'),
        'total_usd': round(total, 2),
        'span': [datetime.fromtimestamp(requests[0].start).isoformat(timespec='minutes'),
                 datetime.fromtimestamp(requests[-1].end).isoformat(timespec='minutes')] if requests else None,
        'precondition': {'warm_starts_past_an_hour': len(held), 'needed': PRECONDITION_COUNT, 'met': met,
                         'examples': sorted(held, key=lambda h: -h['age_seconds'])[:10]},
        'resumes_after_55_min': len(resumes), 'starts_after_55_min': len(starts),
        'starts_shared_unknown': sum(s['shared_unknown'] for s in starts),
        'match_rate': {'warm': close_warm, 'of': close, 'rate': round(match, 4)},
        'settings': settings, 'best': best, 'best_share': round(share, 4),
        'upper_bound_usd': round(upper, 4), 'upper_bound_share': round(upper / total, 4) if total else None,
        'large_first_prompts': {'median_first_prompt': fresh, 'count': len(large),
                                'remainder_up_to_usd': round(sum(s['remainder_usd'] for s in large), 4)},
        'five_minute_rewrites_usd': round(five_minute, 4),
        'decision': 'go' if met and share >= GO_SHARE else 'no-go',
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--root', default='~/.claude/projects')
    p.add_argument('--out')
    a = p.parse_args(argv)
    requests, skipped = load(a.root)
    result = replay(requests, bench.rates())
    result['skipped'] = skipped
    text = json.dumps(result, indent=2)
    if a.out:
        Path(a.out).write_text(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
