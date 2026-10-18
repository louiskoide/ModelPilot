"""Cache keeper (October 18): keep an idle own session's 1h cache entry warm with 1-token pings.

The design and its evidence: docs/cache-keeper-design.md. Opt-in: only `own_sessions --live --keep` starts one, inside
that session's proxy; benchmarks and every other proxy never do (shadow warming stays off by default, CLAUDE.md).

While the client is idle, its last main-loop request's entry expires an hour after it was last read, and the next
prompt rewrites the whole conversation at the write rate. The keeper holds that request in memory and, 55 minutes after
the entry was last read, sends it again with max_tokens 1 and nothing in the prompt changed, which reads the entry back
and so renews it. It repeats every 55 minutes until whichever comes first:
- a new main-loop request (the newer one is held instead, and its window starts);
- 4 hours after the held request ended (`window_end`);
- the client exiting (`client_exit`);
- the timer firing more than 58 minutes after the last read, as after the Mac slept (`late`): the entry may be gone, and
  a ping then would rewrite it at the write rate, the cost the keeper exists to avoid;
- the day's pings reaching the cap, $3 at API rates across every own session (`daily_cap`).
Then nothing is kept until the next main-loop request. And for the rest of the client process, with no retry ever:
a ping that isn't an answered 200 (`ping_error`), one the governor couldn't reserve (`refused`), one of unknown cost
(`cost_unknown`), or one that read less than 95% of the held prompt (`ping_missed`).

A ping goes out through ProxyServer.side_call: reserved and settled by the dry-run governor like the client's requests,
and logged as kind keeper_ping. The governor's spend isn't the gate: every own session so far has rows of unknown cost
(a 429 at start, broken pipes), which would refuse every ping; the ping's own unknown cost is. A stop is logged as kind
keeper. The held request carries the user's code, and its headers the login: both stay in this object's memory only,
never written or logged, and are dropped when keeping stops. While a request is held the keeper holds off idle sleep
(`caffeinate -i`, macOS) so a ping can go out; closing the lid still sleeps the Mac, which `late` covers.

Not kept: a compaction, a request whose breakpoints aren't all 1h, thinking with a fixed budget (max_tokens 1 is below
it: a 400), an unpriced model, or a reply that wasn't a complete 200 of known cost.
"""
import copy
import datetime
import os
import shutil
import sqlite3
import subprocess
import threading
import time

from .cache_probe import cost, prompt_tokens, tier
from . import m5

EVERY_SECONDS = 55 * 60
LATE_SECONDS = 58 * 60
WINDOW_SECONDS = 4 * 3600
CAP_USD = 3.0  # a day, API rates, across every own session (user decision, October 18)
MIN_READ = .95
PING_TIMEOUT = 120
POLL_SECONDS = 30  # wall-clock checks: a sleeping Mac stops the monotonic clock that timed waits run on
PING_PREFIX = 'mp-keep-'
# Stops after which this client process is never kept again.
TERMINAL = ('ping_error', 'refused', 'cost_unknown', 'ping_missed', 'keeper_error')


def settings(overrides=None):
    """The keeper's settings as a session records them: the defaults, or a test's numeric overrides of Keeper's own."""
    o = overrides or {}
    return {'every_seconds': o.get('every', EVERY_SECONDS), 'late_seconds': o.get('late', LATE_SECONDS),
            'window_seconds': o.get('window', WINDOW_SECONDS), 'cap_usd_per_day': o.get('cap_usd', CAP_USD),
            'min_read': MIN_READ, 'ping_max_tokens': 1}


def keepable(request, row, rates):
    """None if this answered main-loop request can be kept, else why not."""
    if row.get('compaction'):
        return 'compaction'
    ttls = row.get('cache_ttl') or []
    if not ttls or any(t != '1h' for t in ttls):
        return 'not_1h'
    thinking = request.get('thinking')
    if isinstance(thinking, dict) and thinking.get('type') not in (None, 'adaptive', 'disabled'):
        return 'thinking_budget'
    if row.get('model') not in rates:
        return 'unpriced_model'
    if row.get('cost_usd') is None or not row.get('usage_complete') or not prompt_tokens(row.get('usage') or {}):
        return 'usage_unknown'
    return None


def ping_body(request):
    """The held request with output capped: max_tokens 1, not streamed (side_call reads one JSON reply). Neither is
    part of the prompt, so the ping reads the entry the request wrote (keeper_ping: 100% read at max_tokens 1)."""
    body = copy.deepcopy(request)
    body['max_tokens'] = 1
    body['stream'] = False
    return body


def ping_estimate(model, prompt, rates):
    """A ping's dollars if it reads the whole held prompt: the read plus one output token."""
    return cost({'input_tokens': 0, 'output_tokens': 1, 'cache_creation_input_tokens': 0,
                 'cache_read_input_tokens': prompt}, rates[model], '1h')


def day_start(now):
    """Local midnight before `now` (unix seconds)."""
    return datetime.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def ledger_spent(db, since):
    """Pings' dollars since `since` in the shared ledger, every session's: measured where settled, the reservation's
    estimate where not. Checked before each ping, so parallel sessions can overshoot the cap by a ping each."""
    if not os.path.exists(db):
        return 0.0
    connection = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=10)
    try:
        return connection.execute('SELECT COALESCE(SUM(COALESCE(actual, estimate)), 0) FROM gov_reservations '
                                  'WHERE request_id LIKE ? AND created >= ?', (PING_PREFIX + '%', since)).fetchone()[0]
    finally:
        connection.close()


class AwakeHold:
    """Holds off idle sleep while it lives: `caffeinate -i`, tied to this process so it can't outlive it. Without
    caffeinate (not macOS) it does nothing."""
    def __init__(self):
        path = shutil.which('caffeinate')
        self.process = None if path is None else subprocess.Popen(
            [path, '-i', '-w', str(os.getpid())], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

    def release(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None


class Keeper:
    """One client process's keeper, owned by its ProxyServer. The proxy reports each Messages request's start and end;
    a thread calls tick() every POLL_SECONDS, which sends a ping when one is due."""
    def __init__(self, proxy, every=EVERY_SECONDS, late=LATE_SECONDS, window=WINDOW_SECONDS, cap_usd=CAP_USD,
                 clock=time.time, spent_since=None, awake=AwakeHold, poll=POLL_SECONDS):
        if not proxy.governor or proxy.policy is not None:
            raise ValueError('The keeper needs a dry-run governor and no benchmark policy')
        if not 0 < every < late < m5.idle_boundary('1h') or window < every or cap_usd < 0:
            raise ValueError('Keeper timing must ping before the late limit, inside the 1h lifetime')
        self.proxy, self.every, self.late, self.window, self.cap = proxy, every, late, window, cap_usd
        self.clock, self.awake, self.poll = clock, awake, poll
        self.spent_since = spent_since or (lambda since: ledger_spent(proxy.governor['db'], since))
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.held = None  # {'body', 'headers', 'model', 'prompt', 'ended', 'refreshed', 'pings'}
        self.hold = None
        self.in_flight = 0
        self.pinging = False
        self.disabled = None
        self.closed = False
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name='cache-keeper', daemon=True)
        self.thread.start()

    def run(self):
        while not self.wake.wait(self.poll):
            try:
                self.tick()
            except Exception as e:
                self.stop('keeper_error', detail=type(e).__name__)

    def request_started(self):
        with self.lock:
            self.in_flight += 1

    def request_finished(self, request, headers, row):
        """After the proxy logged a client's Messages request. An answered main-loop request is held (or, if it can't
        be kept, ends keeping); anything else leaves the held one as it was."""
        with self.lock:
            self.in_flight -= 1
            if (self.closed or self.disabled or not row.get('tool_count') or row.get('status') != 'ok'
                    or row.get('http_status') != 200):
                return
            try:
                why = keepable(request, row, self.proxy.rates)
                if why is not None:
                    self._stop(f'not_keepable:{why}')
                    return
                self.held = {'body': ping_body(request), 'headers': list(headers), 'model': row['model'],
                             'prompt': prompt_tokens(row['usage']), 'refreshed': row['started_unix'],
                             'ended': row['started_unix'] + (row.get('wall_seconds') or 0), 'pings': 0}
                if self.hold is None:
                    self.hold = self.awake()
            except Exception as e:
                self._stop('keeper_error', detail=type(e).__name__)

    def tick(self):
        """Send a ping if one is due. Returns what happened (None: nothing due)."""
        with self.lock:
            held = self.held
            if self.closed or self.disabled or held is None or self.in_flight or self.pinging:
                return None
            now = self.clock()
            if now >= held['ended'] + self.window:
                return self._stop('window_end')
            if now < held['refreshed'] + self.every:
                return None
            if now - held['refreshed'] > self.late:
                return self._stop('late', detail=round(now - held['refreshed']))
            spent = self.spent_since(day_start(now))
            if spent + ping_estimate(held['model'], held['prompt'], self.proxy.rates) > self.cap:
                return self._stop('daily_cap', detail=round(spent, 6))
            self.pinging = True
        try:
            sent = self.clock()
            out = self.proxy.side_call(held['headers'], 'cache_keeper', held['body'], PING_TIMEOUT, kind='keeper_ping',
                                       mode='keeper', id_prefix=PING_PREFIX)
        except Exception as e:
            out = {'status': 'keeper_error', 'error': type(e).__name__}
        with self.lock:
            self.pinging = False
            read = (((out.get('message') or {}).get('usage') or {}).get('cache_read_input_tokens') or 0)
            if out.get('status') == 'refused':
                return self._stop('refused')
            if out.get('status') != 'ok' or out.get('http_status') != 200:
                return self._stop('ping_error', detail=out.get('http_status') or out.get('error'))
            if out.get('cost_usd') is None:
                return self._stop('cost_unknown')
            if read < MIN_READ * held['prompt']:
                return self._stop('ping_missed', detail=read)
            held['pings'] += 1
            held['refreshed'] = sent  # if a newer request is held by now, its own read renewed the entry
            return 'pinged'

    def stop(self, reason, detail=None):
        with self.lock:
            return self._stop(reason, detail)

    def _stop(self, reason, detail=None):
        """Drop the held request (and the login with it), release the hold on sleep, log why. Under self.lock."""
        held, self.held = self.held, None
        if reason in TERMINAL:
            self.disabled = reason
        if self.hold is not None:
            self.hold.release()
            self.hold = None
        if held is None and reason not in TERMINAL:
            return reason  # nothing was kept: nothing to log
        now = self.clock()
        row = {'started_unix': now, 'kind': 'keeper', 'event': 'disabled' if reason in TERMINAL else 'stopped',
               'reason': reason, 'pings': held['pings'] if held else 0}
        if held is not None:
            row.update(model=held['model'], prompt_tokens=held['prompt'], idle_seconds=round(now - held['ended'], 1))
        if detail is not None:
            row['detail'] = detail
        try:
            self.proxy.record(row)
        except Exception:
            pass  # the log is closed: nothing left to tell
        return reason

    def close(self):
        """The client exited: stop the timer (a ping in flight finishes and is logged first) and drop what's held."""
        self.wake.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(PING_TIMEOUT + 10)
        with self.lock:
            self.closed = True
            self._stop('client_exit')


def summary(rows, rates):
    """What keeping did in one own session's log: pings and their cost, why keeping stopped, and the returns that read
    an entry a ping kept: a main-loop request after an idle gap past its 1h boundary, with an answered ping in the gap.
    What such a return read, up to the previous prompt, it would otherwise have written: priced at the 1h write rate
    less the read rate, it is the rewrite avoided. Measured, not modelled: no read, no saving."""
    pings = [r for r in rows if r.get('kind') == 'keeper_ping']
    answered = [r for r in pings if r.get('status') == 'ok']
    known = [r['cost_usd'] for r in pings if r.get('cost_usd') is not None]
    stops = {}
    for r in rows:
        if r.get('kind') == 'keeper':
            stops[r.get('reason')] = stops.get(r.get('reason'), 0) + 1
    main = sorted((r for r in rows if r.get('kind') == 'messages' and r.get('tool_count') and r.get('status') == 'ok'
                   and r.get('started_unix') is not None), key=lambda r: r['started_unix'])
    returns, avoided, priced = [], 0.0, True
    for before, after in zip(main, main[1:]):
        ended = before['started_unix'] + (before.get('wall_seconds') or 0)
        if (before.get('cache_ttl') or [None])[-1] != '1h' or after['started_unix'] - ended < m5.idle_boundary('1h'):
            continue
        kept = [p for p in answered if ended <= p['started_unix'] <= after['started_unix']]
        if not kept:
            continue
        usage = after.get('usage') or {}
        read = min(usage.get('cache_read_input_tokens') or 0, prompt_tokens(before.get('usage') or {}))
        rate = (rates or {}).get(after.get('model'))
        dollars = None
        if rate is None:
            priced = False
        else:
            r = tier(rate, prompt_tokens(usage), after['started_unix'])
            dollars = read * (r['write_1h'] - r['read']) / 1e6
            avoided += dollars
        returns.append({'gap_seconds': round(after['started_unix'] - ended, 1), 'pings': len(kept),
                        'read_tokens': read, 'avoided_usd': None if dollars is None else round(dollars, 6)})
    spent = sum(known)
    return {'pings': len(pings), 'answered': len(answered), 'ping_cost_usd': round(spent, 6),
            'unknown_cost_pings': len(pings) - len(known), 'stops': stops, 'kept_returns': returns,
            'avoided_rewrite_usd': round(avoided, 6) if priced else None,
            'net_usd': round(avoided - spent, 6) if priced and len(known) == len(pings) else None}
