"""M2 local state: versioned tasks, observable stuck signals, reference outputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

LADDER = ('increase_effort', 'stronger_model', 're_diagnose', 'human_review')
# Signals journaled for evidence, kept out of the score: both read the agent's own test runs.
OBSERVED_ONLY = ('stalled_agent_tests', 'repeated_agent_failure')
# What changes between two runs of the same failure: directories of absolute paths (temporary ones differ every run),
# line numbers, memory addresses, clock times and a test runner's duration. Counts like '2 failed' and values in a
# message (a hex literal, '2 s') stay: a change there is a change in state (heuristic-v3; v2 replaced every 0x value
# and every number before 's', so 0x1f and 0x7f hashed alike).
VOLATILE = ((re.compile(r'(?<![\w.~-])/(?:[^\s/\'"`:,;()<>\[\]{}]+/)+'), ''),
            (re.compile(r'\bline \d+'), 'line #'),
            (re.compile(r'(\.[A-Za-z]\w*):\d+(?::\d+)?\b'), r'\1:#'),
            (re.compile(r'\bat 0x[0-9a-fA-F]+\b'), 'at 0x#'),  # an object's address in its repr
            (re.compile(r'\b0x[0-9a-fA-F]{8,}\b'), '0x#'),  # any other address
            (re.compile(r'\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?'), '<time>'),
            (re.compile(r'\bin \d+(?:\.\d+)?s\b(?: \(\d+:\d\d:\d\d\))?'), 'in #s'))
ANSI = re.compile(r'\x1b\[[0-9;]*m')
PYTEST_SUMMARY = re.compile(r'^(?:FAILED|ERROR) (?!\()\S.*$', re.M)  # pytest's short test summary lines
UNITTEST_FAILURE = re.compile(r'^(?:FAIL|ERROR): \S.*$', re.M)
RULE = re.compile(r'^(?:={10,}|-{10,})\s*$', re.M)


def normalize_error(text):
    """An error's text as hashed: whitespace collapsed and VOLATILE parts replaced, so a failure repeated with a new
    temporary path, line number, address, clock time or test duration hashes the same."""
    for pattern, replacement in VOLATILE:
        text = pattern.sub(replacement, text)
    return ' '.join(text.split())


def _exception(text):
    """The exception line a Python traceback ends on (the first unindented line after its frames), or None."""
    tail = text.rsplit('Traceback (most recent call last):', 1)
    if len(tail) < 2:
        return None
    return next((l.strip() for l in tail[1].splitlines() if l.strip() and not l[:1].isspace()), None)


def failure_signature(text):
    """What identifies a failure, so the same failure after an edit compares equal although the output around it
    changed (a traceback quotes the edited source; a check prints new values first): pytest's short summary lines
    (failing test ids with their messages), unittest's failing tests each with its exception line, or the exception a
    traceback ends on. Other text (a tool's own error, a crash with no traceback) is its own signature."""
    text = ANSI.sub('', text)
    summary = PYTEST_SUMMARY.findall(text)
    if summary:
        return '\n'.join(sorted(set(summary)))
    heads = list(UNITTEST_FAILURE.finditer(text))
    if heads:
        failures = []
        for head in heads:
            block = RULE.split(text[head.end():])
            body = block[1] if len(block) > 1 else block[0]  # header, rule, traceback, rule
            failures.append(f'{head[0]} {_exception(body) or body.strip()[-200:]}')
        return '\n'.join(sorted(set(failures)))
    return _exception(text) or text


def error_hash(text):
    """The hash the detector compares for a failure: of its normalized signature. None when nothing is left."""
    signature = normalize_error(failure_signature(text))
    return hashlib.sha256(signature.encode()).hexdigest() if signature else None


def assess(events, level=0):
    """Heuristic recommendations only. Inputs are ordered observations of one task revision.

    The agent's own test runs (agent_suite/agent_failures/agent_failure_hash: its Bash calls that run a test runner,
    observed by the hooks) are kept apart from a host suite's and are OBSERVED_ONLY: stalled_agent_tests (the host
    suite's non-improving rule) and repeated_agent_failure (its last three runs failing alike) are reported, never
    scored. On the 891 eligible recorded trials (runs/stuck-v3-replay-20261016.json) neither separates misses from
    passes: misses test as often and their last own run passes in 91% (runs/stuck-eval-20261016.json)."""
    if level not in range(5):
        raise ValueError('Escalation level must be 0..4')
    recent = events[-6:]
    # Explicit, verified progress starts a fresh window; retry text alone cannot escalate.
    last_progress = max((i for i,e in enumerate(recent) if e.get('progress') is True), default=-1)
    recent = recent[last_progress+1:]
    errors = [e.get('error_hash') for e in recent if e.get('error_hash')]
    repeated = len(errors) >= 3 and len(set(errors[-3:])) == 1
    snapshots = {}
    for e in recent:
        if e.get('file') and e.get('content_hash'):
            snapshots.setdefault(e['file'], []).append(e['content_hash'])
    oscillation = any(len(v) >= 4 and v[-4] == v[-2] and v[-3] == v[-1] and v[-1] != v[-2]
                      for v in snapshots.values())
    def stalls(suite_key, failures_key):
        series = {}
        for e in recent:
            if e.get(suite_key) and isinstance(e.get(failures_key), int):
                series.setdefault(e[suite_key], []).append(e[failures_key])
        return any(len(v) >= 3 and v[-1] > 0 and v[-3] <= v[-2] <= v[-1] for v in series.values())
    stalled, stalled_agent = stalls('suite', 'failures'), stalls('agent_suite', 'agent_failures')
    runs = [e for e in recent if isinstance(e.get('agent_failures'), int)][-3:]
    repeated_agent = (len(runs) == 3 and all(e['agent_failures'] > 0 and e.get('agent_failure_hash') for e in runs)
                      and len({e['agent_failure_hash'] for e in runs}) == 1)
    retry = sum(e.get('retry_language') is True for e in recent) >= 3
    signals = {'repeated_error': repeated, 'edit_oscillation': oscillation, 'stalled_tests': stalled,
               'stalled_agent_tests': stalled_agent, 'repeated_agent_failure': repeated_agent, 'retry_language': retry}
    score = 3*repeated + 3*oscillation + 3*stalled + retry
    return {'score': score, 'signals': signals, 'observed_only': list(OBSERVED_ONLY), 'window_events': len(recent),
            'recommendation': LADDER[level] if score >= 3 and level < 4 else 'hold',
            'applied': False, 'policy': 'heuristic-v3; threshold=3; window=6'}


class State:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Never open and close the file here: closing any descriptor to it drops every POSIX lock this process holds on
        # it, the SQLite connections' included, and a ledger is opened once per request beside hook processes writing
        # it (October 14: their sessions ended 'database disk image is malformed'). A new file is made private instead.
        created = not path.exists()
        self.db = sqlite3.connect(path, timeout=10)
        if created:
            os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, title TEXT NOT NULL, instruction TEXT NOT NULL,
          revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'pending',
          owner TEXT, lease_until REAL, ack_revision INTEGER,
          level INTEGER NOT NULL DEFAULT 0, last_escalation_event INTEGER NOT NULL DEFAULT 0,
          result TEXT);
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL, revision INTEGER NOT NULL,
          kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS outputs(
          handle TEXT PRIMARY KEY, content TEXT NOT NULL, bytes INTEGER NOT NULL, created REAL NOT NULL);
        ''')

    def close(self):
        self.db.close()

    def _event(self, task, rev, kind, payload):
        cursor = self.db.execute('INSERT INTO events(task,revision,kind,payload,created) VALUES(?,?,?,?,?)',
                                (task, rev, kind, json.dumps(payload), time.time()))
        return cursor.lastrowid

    def get(self, task):
        row = self.db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()
        if row is None:
            raise ValueError('Unknown task')
        return dict(row)

    def create(self, title, instruction):
        task = str(uuid.uuid4())
        with self.db:
            self.db.execute('INSERT INTO tasks(id,title,instruction) VALUES(?,?,?)', (task,title,instruction))
            self._event(task, 1, 'created', {})
        return self.get(task)

    def _owned(self, task, revision, owner):
        row = self.get(task)
        if (row['revision'] != revision or row['owner'] != owner or row['status'] != 'in_progress'
                or (row['lease_until'] or 0) <= time.time() or row['ack_revision'] != revision):
            raise ValueError('Stale revision, expired lease, unacknowledged correction or wrong owner')
        return row

    def claim(self, task, revision, owner, seconds=300):
        if not owner or not 1 <= seconds <= 3600:
            raise ValueError('Owner required; lease must be 1..3600 seconds')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.get(task)
            if row['revision'] != revision or row['status'] in ('done','cancelled'):
                raise ValueError('Task is stale or terminal')
            if row['status'] == 'in_progress' and row['lease_until'] > time.time():
                raise ValueError('Task already leased; use renew')
            new_revision = revision + (row['status'] == 'in_progress')
            self.db.execute("UPDATE tasks SET owner=?,lease_until=?,status='in_progress',revision=?,ack_revision=? WHERE id=?",
                            (owner,time.time()+seconds,new_revision,new_revision,task))
            if new_revision != revision:
                self.db.execute('UPDATE tasks SET level=0,last_escalation_event=0 WHERE id=?',(task,))
            self._event(task, new_revision, 'claimed', {'owner':owner})
        return self.get(task)

    def correct(self, task, revision, instruction):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.get(task)
            if row['revision'] != revision or row['status'] in ('done','cancelled'):
                raise ValueError('Task is stale or terminal')
            self.db.execute('UPDATE tasks SET instruction=?,revision=revision+1,ack_revision=NULL,level=0,last_escalation_event=0 WHERE id=?',
                            (instruction,task))
            self._event(task, revision+1, 'corrected', {})
        return self.get(task)

    def acknowledge(self, task, revision, owner):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.get(task)
            if row['revision'] != revision or row['owner'] != owner or row['status'] != 'in_progress' or row['lease_until'] <= time.time():
                raise ValueError('Cannot acknowledge stale or unowned task')
            self.db.execute('UPDATE tasks SET ack_revision=? WHERE id=?', (revision,task))
            self._event(task, revision, 'acknowledged', {'owner':owner})
        return self.get(task)

    def renew(self, task, revision, owner, seconds=300):
        if not 1 <= seconds <= 3600:
            raise ValueError('Lease must be 1..3600 seconds')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._owned(task,revision,owner)
            self.db.execute('UPDATE tasks SET lease_until=? WHERE id=?', (time.time()+seconds,task))
        return self.get(task)

    def complete(self, task, revision, owner, result):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._owned(task,revision,owner)
            self.db.execute("UPDATE tasks SET status='done',result=?,lease_until=NULL WHERE id=?",(result,task))
            self._event(task,revision,'completed',{})
        return self.get(task)

    def cancel(self, task, revision):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.get(task)
            if row['revision'] != revision or row['status'] in ('done','cancelled'):
                raise ValueError('Task is stale or terminal')
            self.db.execute("UPDATE tasks SET status='cancelled',revision=revision+1,lease_until=NULL,ack_revision=NULL WHERE id=?",(task,))
            self._event(task,revision+1,'cancelled',{})
        return self.get(task)

    def observe(self, task, revision, owner, observation):
        # suite_outcome and tree_hash (October 14): a host-run suite's verdict and the files it ran against
        # (modelpilot.suite_adapter); an unknown outcome carries no failures, so it never feeds stalled_tests.
        # agent_suite and agent_failures (October 16): the agent's own Bash test run, its test-running shell lines and
        # the failures its output reports (modelpilot.hooks); kept apart from a host suite's suite and failures.
        # agent_failure_hash (heuristic-v3): error_hash of a failing run's output, never its text.
        allowed = {'error','file','content_hash','suite','failures','retry_language','progress','suite_outcome','tree_hash',
                   'agent_suite','agent_failures','agent_failure_hash'}
        if set(observation)-allowed:
            raise ValueError('Unknown observation fields')
        payload = dict(observation)
        if 'suite_outcome' in payload and payload['suite_outcome'] not in ('pass','fail','unknown'):
            raise ValueError('suite_outcome must be pass, fail or unknown')
        if payload.get('suite_outcome') == 'unknown' and 'failures' in payload:
            raise ValueError('an unknown suite outcome has no failure count')
        for key in ('retry_language','progress'):
            if key in payload and type(payload[key]) is not bool:
                raise ValueError(f'{key} must be boolean')
        for key in ('failures','agent_failures'):
            if key in payload and (type(payload[key]) is not int or payload[key] < 0):
                raise ValueError(f'{key} must be a nonnegative integer')
        for key in ('error','file','content_hash','suite','tree_hash','agent_suite'):
            if key in payload and not isinstance(payload[key], str):
                raise ValueError(f'{key} must be a string')
        if 'agent_failure_hash' in payload and not (isinstance(payload['agent_failure_hash'], str)
                                                    and re.fullmatch('[0-9a-f]{64}', payload['agent_failure_hash'])):
            raise ValueError('agent_failure_hash must be a SHA-256 hex digest')
        if 'error' in payload:
            digest = error_hash(payload.pop('error'))
            if digest:
                payload['error_hash'] = digest
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._owned(task,revision,owner)
            self._event(task,revision,'observation',payload)
        return self.recommend(task)

    def observations(self, task, revision, field, limit=2):
        """The latest observations of one revision that carry `field`, newest first, with their event seq."""
        rows = self.db.execute("SELECT seq,payload FROM events WHERE task=? AND revision=? AND kind='observation' ORDER BY seq DESC",
                               (task,revision))
        found = []
        for row in rows:
            payload = json.loads(row['payload'])
            if field in payload:
                found.append(dict(payload, seq=row['seq']))
                if len(found) == limit:
                    break
        return found

    def file_hashes(self, task, revision):
        """Each file's newest observed content hash in one revision, {file: content_hash}."""
        hashes = {}
        for row in self.db.execute("SELECT payload FROM events WHERE task=? AND revision=? AND kind='observation' ORDER BY seq",
                                   (task,revision)):
            payload = json.loads(row['payload'])
            if 'file' in payload and 'content_hash' in payload:
                hashes[payload['file']] = payload['content_hash']
        return hashes

    def recommend(self, task):
        row = self.get(task)
        events = self.db.execute("SELECT seq,payload FROM events WHERE task=? AND revision=? AND kind='observation' AND seq>? ORDER BY seq DESC LIMIT 6",
                                 (task,row['revision'],row['last_escalation_event'])).fetchall()
        result = assess([json.loads(e['payload']) for e in reversed(events)],row['level'])
        if row['status'] != 'in_progress' or row['ack_revision'] != row['revision'] or (row['lease_until'] or 0) <= time.time():
            result['recommendation'] = 'hold'
        result.update(task=task,revision=row['revision'],level=row['level'],last_event=events[0]['seq'] if events else 0)
        return result

    def confirm_escalation(self, task, revision, owner, last_event):
        # This only records an external/manual action; never changes a model or effort itself.
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._owned(task,revision,owner)
            result = self.recommend(task)
            if result['recommendation']=='hold' or result['last_event']!=last_event:
                raise ValueError('Recommendation is stale or no escalation is indicated')
            self.db.execute('UPDATE tasks SET level=level+1,last_escalation_event=? WHERE id=?',(last_event,task))
            self._event(task,revision,'escalation_confirmed',{'action':result['recommendation']})
        return self.recommend(task)

    def store_output(self, content):
        data = content.encode('utf-8')
        if len(data) > 16*1024*1024:
            raise ValueError('Output exceeds 16 MiB limit')
        handle = hashlib.sha256(data).hexdigest()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO outputs VALUES(?,?,?,?)',(handle,content,len(data),time.time()))
        digest = content if len(content)<=1600 else content[:800]+'\n[… excerpt omitted; use expand_output …]\n'+content[-800:]
        return {'handle':handle,'bytes':len(data),'characters':len(content),'digest':digest,
                'digest_kind':'literal_excerpt','truncated':len(content)>1600}

    def expand(self, handle, offset=0, limit=8000):
        if not re.fullmatch('[0-9a-f]{64}',handle):
            raise ValueError('Invalid output handle')
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=32000:
            raise ValueError('offset must be nonnegative; limit must be 1..32000 characters')
        row = self.db.execute('SELECT content FROM outputs WHERE handle=?',(handle,)).fetchone()
        if row is None:
            raise ValueError('Unknown output handle')
        text = row['content']
        if hashlib.sha256(text.encode()).hexdigest()!=handle:
            raise ValueError('Stored output integrity failure')
        end = min(len(text), offset+limit)
        return {'handle':handle,'offset':offset,'text':text[offset:end],
                'next_offset':end if end<len(text) else None,'total_characters':len(text)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=Path('runs/m2/state.sqlite3'))
    parser.add_argument('operation',choices=['create','get','claim','correct','acknowledge','renew','complete','cancel','observe','recommend','confirm_escalation','store_output','expand'])
    parser.add_argument('--args',default='{}',help='JSON keyword arguments; output content can instead be read using --file')
    parser.add_argument('--file',type=Path)
    args=parser.parse_args(); values=json.loads(args.args)
    if args.file:
        if args.operation!='store_output':
            parser.error('--file only applies to store_output')
        values['content']=args.file.read_text()
    state=State(args.db)
    try:
        print(json.dumps(getattr(state,args.operation)(**values),indent=2))
    finally:
        state.close()


if __name__=='__main__':
    main()
