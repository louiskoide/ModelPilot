"""Durable integration surface joining M2 ledger, M4 verifier and M5 controls.

Dry-run only: every decision is journaled with applied=False. The caller (proxy,
worker dispatcher or Claude Code hook) owns any real model/effort/context change.
"""
import argparse
import json
from pathlib import Path
import tempfile
import time
import uuid
from .cache_probe import priced_usage
from .m2 import State
from .m4 import cascade, nonnegative, sha

KINDS = ('model', 'effort', 'prune', 'compact')


class BudgetRefused(ValueError):
    pass
SCHEMA = '''
CREATE TABLE IF NOT EXISTS gov_sessions(
  id TEXT PRIMARY KEY, limit_usd REAL NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS gov_reservations(
  session TEXT NOT NULL, request_id TEXT NOT NULL, task TEXT, revision INTEGER,
  estimate REAL NOT NULL, status TEXT NOT NULL, actual REAL, created REAL NOT NULL,
  expires REAL NOT NULL, settled REAL, reconciled INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(session, request_id));
CREATE TABLE IF NOT EXISTS gov_changes(
  session TEXT NOT NULL, kind TEXT NOT NULL, change_id TEXT NOT NULL, value TEXT NOT NULL,
  revision INTEGER NOT NULL, created REAL NOT NULL, PRIMARY KEY(session, kind));
CREATE TABLE IF NOT EXISTS gov_plans(
  id TEXT PRIMARY KEY, session TEXT NOT NULL, revision INTEGER NOT NULL, trigger TEXT NOT NULL,
  changes TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, acknowledged REAL);
CREATE TABLE IF NOT EXISTS gov_decisions(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, kind TEXT NOT NULL,
  task TEXT, revision INTEGER, payload TEXT NOT NULL, created REAL NOT NULL);
'''


class Governor:
    """One session's budget, rebase queue and decision journal in the ledger database.

    Several processes may open the same session; writes use IMMEDIATE transactions.
    A reservation that outlives its expiry is orphaned: the governor cannot know
    whether that request was billed, so cost becomes unknown and new work halts
    until settle() reconciles it with measured evidence.
    """
    def __init__(self, path, session, limit_usd, mode='dry-run', clock=time.time):
        if mode != 'dry-run':
            raise ValueError('Active governor actions are disabled until integration evidence exists')
        nonnegative(limit_usd, 'limit_usd')
        if not isinstance(session, str) or not session:
            raise ValueError('Session ID required')
        self.session = session
        self.clock = clock
        self.state = State(path)
        self.db = self.state.db
        try:
            self.db.executescript(SCHEMA)
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                self.db.execute('INSERT OR IGNORE INTO gov_sessions VALUES(?,?,?)', (session, limit_usd, self.clock()))
                stored = self.db.execute('SELECT limit_usd FROM gov_sessions WHERE id=?', (session,)).fetchone()[0]
            if stored != limit_usd:
                raise ValueError('Session budget limit differs from the recorded limit')
            self.limit = stored
        except Exception:
            self.state.close()
            raise

    def close(self):
        self.state.close()

    def _journal(self, kind, payload, task=None, revision=None):
        self.db.execute('INSERT INTO gov_decisions(session,kind,task,revision,payload,created) VALUES(?,?,?,?,?,?)',
                        (self.session, kind, task, revision, json.dumps(payload), self.clock()))

    def _recover(self):
        cursor = self.db.execute("UPDATE gov_reservations SET status='orphaned' WHERE session=? AND status='pending' AND expires<=?",
                                 (self.session, self.clock()))
        return cursor.rowcount

    def _totals(self):
        spent, unknown = self.db.execute(
            "SELECT COALESCE(SUM(actual),0), COALESCE(SUM(status='orphaned' OR (status='settled' AND actual IS NULL)),0) "
            'FROM gov_reservations WHERE session=?', (self.session,)).fetchone()
        reserved = self.db.execute("SELECT COALESCE(SUM(estimate),0) FROM gov_reservations WHERE session=? AND status='pending'",
                                   (self.session,)).fetchone()[0]
        return spent, reserved, bool(unknown)

    def _policy(self):
        spent, reserved, unknown = self._totals()
        remaining = max(0, self.limit - spent - reserved)
        fraction = remaining / self.limit if self.limit else 0
        mode = 'halt' if unknown or remaining == 0 else 'conserve' if fraction <= .2 else 'normal'
        return {'mode': mode, 'spent_usd': spent, 'reserved_usd': reserved, 'available_usd': remaining,
                'cost_complete': not unknown, 'optional_escalation_allowed': mode == 'normal',
                'prefer_output_digest': mode != 'normal', 'acceptance_floor_unchanged': True, 'applied': False}

    def recover(self):
        """Orphan expired reservations; callers need not call this, admission does."""
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            orphaned = self._recover()
            if orphaned:
                self._journal('recover', {'orphaned': orphaned, 'applied': False})
            return {'orphaned': orphaned, 'policy': self._policy()}

    def policy(self):
        return self.recover()['policy']

    def admit(self, request_id, estimate, task=None, revision=None, ttl=600, enforce=True):
        """Reserve before dispatch. Work for a stale or terminal task revision is refused.

        enforce=False is for observers that forward regardless (the dry-run proxy): the
        decision is journaled as would-admit/would-refuse, but the reservation is always
        recorded so that forwarded spend still counts.
        """
        nonnegative(estimate, 'estimate')
        if not isinstance(request_id, str) or not request_id:
            raise ValueError('Request ID required')
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 1 <= ttl <= 3600:
            raise ValueError('Reservation TTL must be 1..3600 seconds')
        if (task is None) != (revision is None):
            raise ValueError('Task and revision must be supplied together')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._recover()
            if self.db.execute('SELECT 1 FROM gov_reservations WHERE session=? AND request_id=?',
                               (self.session, request_id)).fetchone():
                raise ValueError('Duplicate request ID')
            reason = 'reserved'
            if task is not None:
                row = self.state.get(task)
                if row['revision'] != revision or row['status'] in ('done', 'cancelled'):
                    reason = 'stale_task'
            policy = self._policy()
            if reason == 'reserved':
                if not policy['cost_complete']:
                    reason = 'cost_unknown'
                elif policy['available_usd'] <= 0 or estimate > policy['available_usd']:
                    reason = 'insufficient_budget'
            admitted = reason == 'reserved'
            if admitted or not enforce:
                now = self.clock()
                self.db.execute("INSERT INTO gov_reservations(session,request_id,task,revision,estimate,status,created,expires) "
                                "VALUES(?,?,?,?,?,'pending',?,?)", (self.session, request_id, task, revision, estimate, now, now+ttl))
                policy = self._policy()
            result = {'request_id': request_id, 'admitted': admitted, 'reason': reason, 'enforced': enforce,
                      'reserved': admitted or not enforce, 'estimate_usd': estimate, 'policy': policy, 'applied': False}
            self._journal('admit', result, task, revision)
        return result

    def settle(self, request_id, actual):
        """Record measured cost (None when unknown). Also reconciles an orphaned reservation."""
        if actual is not None:
            nonnegative(actual, 'actual')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT status,actual FROM gov_reservations WHERE session=? AND request_id=?',
                                  (self.session, request_id)).fetchone()
            if row is None:
                raise ValueError('No reservation')
            if row['status'] == 'settled':
                if row['actual'] != actual:
                    raise ValueError('Conflicting settlement')
                return self._policy()
            reconciled = row['status'] == 'orphaned'
            if reconciled and actual is None:
                return self._policy()  # still unknown; nothing new learned
            self.db.execute("UPDATE gov_reservations SET status='settled',actual=?,settled=?,reconciled=? WHERE session=? AND request_id=?",
                            (actual, self.clock(), int(reconciled), self.session, request_id))
            policy = self._policy()
            self._journal('settle', {'request_id': request_id, 'actual_usd': actual, 'reconciled': reconciled,
                                     'policy': policy, 'applied': False})
        return policy

    def observe(self, task, revision, owner, observation):
        """Ledger observation plus stuck recommendation; never escalates by itself."""
        result = self.state.observe(task, revision, owner, observation)
        with self.db:
            self._journal('stuck', dict(result, applied=False), task, revision)
        return result

    def review(self, task, operation, candidate, evidence, task_revision, fallback_estimate):
        """Verify a cheap draft against host evidence and the current ledger revision.

        Unverified drafts escalate when the fallback fits the budget; otherwise the
        caller must defer or stop. Budget pressure never turns escalation into acceptance.
        """
        nonnegative(fallback_estimate, 'fallback_estimate')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._recover()
            row = self.state.get(task)
            decision = cascade(operation, candidate, evidence, task_revision, row['revision'])
            if decision['action'] == 'would_accept' and row['status'] in ('done', 'cancelled'):
                decision.update(action='reject_stale', reason='task_terminal')
            policy = self._policy()
            if decision['action'] == 'would_escalate':
                affordable = policy['cost_complete'] and fallback_estimate <= policy['available_usd']
                decision['escalation_affordable'] = affordable
                if not affordable:
                    decision.update(action='would_defer', escalation_reason=decision['reason'],
                                    reason='escalation_unaffordable_or_cost_unknown')
            decision.update(task=task, current_revision=row['revision'], fallback_estimate_usd=fallback_estimate,
                            budget_mode=policy['mode'], applied=False)
            self._journal('review', decision, task, row['revision'])
        return decision

    def queue_change(self, kind, value, revision):
        """Durably keep the latest cache-breaking change per kind."""
        if kind not in KINDS:
            raise ValueError('Unknown cache-breaking change')
        if type(revision) is not int or revision < 0:
            raise ValueError('Invalid task revision')
        change_id = uuid.uuid4().hex
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('INSERT OR REPLACE INTO gov_changes VALUES(?,?,?,?,?,?)',
                            (self.session, kind, change_id, json.dumps(value), revision, self.clock()))
            self._journal('queue_change', {'kind': kind, 'change_id': change_id, 'applied': False}, revision=revision)
        return change_id

    def pending_changes(self):
        rows = self.db.execute('SELECT kind,change_id,value,revision FROM gov_changes WHERE session=? ORDER BY kind',
                               (self.session,)).fetchall()
        return [{'kind': r['kind'], 'change_id': r['change_id'], 'value': json.loads(r['value']),
                 'revision': r['revision']} for r in rows]

    def plan_rebase(self, revision, *, idle_seconds=0, idle_threshold=300, cache_expired=False,
                    switch_justified=False, explicit_boundary=False, in_flight=0):
        """Same boundaries as M5 RebaseQueue.plan; a ready plan is recorded for acknowledgment."""
        nonnegative(idle_seconds, 'idle_seconds')
        nonnegative(idle_threshold, 'idle_threshold')
        if idle_threshold == 0 or type(in_flight) is not int or in_flight < 0:
            raise ValueError('Invalid rebase boundary')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            pending = self.pending_changes()
            eligible = [c for c in pending if c['revision'] == revision]
            stale = [c['kind'] for c in pending if c['revision'] != revision]
            trigger = ('explicit_boundary' if explicit_boundary else 'cache_expired' if cache_expired
                       else 'justified_switch' if switch_justified else 'idle' if idle_seconds >= idle_threshold else None)
            ready = bool(eligible and trigger and not in_flight)
            plan_id = None
            if ready:
                ids = sorted(c['change_id'] for c in eligible)
                for row in self.db.execute("SELECT id,changes FROM gov_plans WHERE session=? AND status='planned'", (self.session,)):
                    if sorted(c['change_id'] for c in json.loads(row['changes'])) == ids:
                        plan_id = row['id']  # identical outstanding plan: do not plan a second rebuild
                if plan_id is None:
                    plan_id = uuid.uuid4().hex
                    self.db.execute("INSERT INTO gov_plans VALUES(?,?,?,?,?,'planned',?,NULL)",
                                    (plan_id, self.session, revision, trigger, json.dumps(eligible), self.clock()))
            result = {'plan_id': plan_id, 'applied': False, 'action': 'would_rebase' if ready else 'defer',
                      'trigger': trigger, 'changes': eligible, 'stale_changes': stale,
                      'rebuilds_planned': int(ready), 'in_flight': in_flight}
            self._journal('plan_rebase', result, revision=revision)
        return result

    def acknowledge_rebase(self, plan_id):
        """Caller reports it performed the rebuild; drains only the planned change versions.

        A newer value queued after planning stays pending for the next boundary.
        """
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT * FROM gov_plans WHERE id=? AND session=?', (plan_id, self.session)).fetchone()
            if row is None:
                raise ValueError('Unknown rebase plan')
            if row['status'] == 'planned':
                drained = 0
                for change in json.loads(row['changes']):
                    drained += self.db.execute('DELETE FROM gov_changes WHERE session=? AND kind=? AND change_id=?',
                                               (self.session, change['kind'], change['change_id'])).rowcount
                self.db.execute("UPDATE gov_plans SET status='acknowledged',acknowledged=? WHERE id=?", (self.clock(), plan_id))
                # Other outstanding plans referenced now-drained versions; they cannot be acknowledged.
                self.db.execute("UPDATE gov_plans SET status='superseded' WHERE session=? AND status='planned'", (self.session,))
                self._journal('acknowledge_rebase', {'plan_id': plan_id, 'drained': drained, 'applied_by': 'caller'},
                              revision=row['revision'])
            elif row['status'] == 'superseded':
                raise ValueError('Rebase plan was superseded; plan again')
        return {'plan_id': plan_id, 'status': 'acknowledged', 'pending': self.pending_changes()}

    def journal(self, kind=None):
        query = 'SELECT seq,kind,task,revision,payload,created FROM gov_decisions WHERE session=?'
        args = [self.session]
        if kind:
            query += ' AND kind=?'
            args.append(kind)
        return [dict(r, payload=json.loads(r['payload'])) for r in self.db.execute(query + ' ORDER BY seq', args)]


def governed_transport(gov, inner, rates, estimate, ttl=600):
    """Wrap a Messages transport (e.g. a Worker's) so every call is admitted and settled.

    Refused admission raises before anything is sent. A raised transport error settles
    as unknown because the provider may already have billed it. Ambiguous usage
    (missing TTL breakdown, alias model) is also unknown, matching the proxy.
    """
    def transport(request, config):
        request_id = 'call-' + uuid.uuid4().hex
        decision = gov.admit(request_id, estimate(request) if callable(estimate) else estimate, ttl=ttl)
        if not decision['admitted']:
            raise BudgetRefused(decision['reason'])
        try:
            response, provider_id = inner(request, config)
        except BaseException:
            gov.settle(request_id, None)
            raise
        actual = None
        if isinstance(response, dict):
            actual = priced_usage(request, response.get('model'), response.get('usage'), rates)
        gov.settle(request_id, actual)
        return response, provider_id
    return transport


def reconcile_log(gov, log_path):
    """Bring the governor up to date from a proxy log (JSON lines) after crashes or governor errors.

    A row whose reservation is still open settles with the row's measured cost (None stays
    unknown). A row the proxy could not reserve ('untracked') is recorded now, so its spend
    counts. An orphan with no row at all, e.g. the proxy died mid-request, stays unknown.
    """
    rows = []
    with open(log_path) as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                if row.get('governor_request_id'):
                    rows.append(row)
    settled = untracked = 0
    for row in rows:
        request_id = row['governor_request_id']
        status = gov.db.execute('SELECT status FROM gov_reservations WHERE session=? AND request_id=?',
                                (gov.session, request_id)).fetchone()
        if status is None:
            if row.get('governor_status') != 'untracked':
                continue  # admitted elsewhere or foreign row: never invent a reservation
            gov.admit(request_id, 0, enforce=False)
            gov.settle(request_id, row.get('cost_usd'))
            untracked += 1
        elif status['status'] != 'settled' and row.get('cost_usd') is not None:
            gov.settle(request_id, row['cost_usd'])
            settled += 1
        elif status['status'] == 'pending':
            gov.settle(request_id, None)  # the proxy finished this request without a measured cost
    policy = gov.policy()
    ids = {row['governor_request_id'] for row in rows}
    orphans = [r['request_id'] for r in gov.db.execute(
        "SELECT request_id FROM gov_reservations WHERE session=? AND status='orphaned'", (gov.session,))]
    unknown = gov.db.execute("SELECT COUNT(*) FROM gov_reservations WHERE session=? AND "
                             "(status='orphaned' OR (status='settled' AND actual IS NULL))", (gov.session,)).fetchone()[0]
    return {'rows': len(rows), 'settled': settled, 'untracked_recorded': untracked, 'still_unknown': unknown,
            'orphans_without_rows': sum(o not in ids for o in orphans), 'policy': policy}


def demo(path):
    """Offline walk-through of one task: no provider requests, no applied actions."""
    gov = Governor(path, 'demo', 1)
    try:
        task = gov.state.create('demo', 'Find the verification token')['id']
        rev = gov.state.claim(task, 1, 'coordinator')['revision']
        admitted = gov.admit('draft-1', .002, task, rev)
        gov.settle('draft-1', .0015)
        text = 'The verification token is ALPHA.'
        evidence = {'revision': rev, 'text': text, 'current_source_sha256': sha(text)}
        start = text.index('ALPHA')
        good = {'start': start, 'end': start+5, 'answer': 'ALPHA', 'source_sha256': sha(text)}
        accepted = gov.review(task, 'read', good, evidence, rev, fallback_estimate=.02)
        fabricated = gov.review(task, 'read', dict(good, answer='BETA'), evidence, rev, fallback_estimate=.02)
        unaffordable = gov.review(task, 'read', dict(good, answer='BETA'), evidence, rev, fallback_estimate=5)
        gov.queue_change('model', 'target', rev)
        gov.queue_change('effort', 'high', rev)
        plan = gov.plan_rebase(rev, explicit_boundary=True)
        stale = gov.admit('stale-1', .001, task, rev+1)
        checks = {'admitted': admitted['admitted'], 'verified_read_accepted': accepted['action'] == 'would_accept',
                  'fabricated_read_escalates': fabricated['action'] == 'would_escalate',
                  'unaffordable_escalation_defers': unaffordable['action'] == 'would_defer',
                  'one_rebuild_planned': plan['rebuilds_planned'] == 1 and len(plan['changes']) == 2,
                  'stale_work_refused': stale['reason'] == 'stale_task',
                  'nothing_applied': all(e['payload'].get('applied') is not True for e in gov.journal())}
        return {'status': 'passed' if all(checks.values()) else 'failed', 'checks': checks, 'synthetic': True,
                'api_calls': 0, 'actual_api_cost_usd': 0, 'policy': gov.policy(), 'journal_entries': len(gov.journal())}
    finally:
        gov.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        result = demo(Path(tmp) / 'governor.sqlite3')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x') as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))
    if result['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
