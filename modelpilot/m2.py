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


def assess(events, level=0):
    """Heuristic recommendations only. Inputs are ordered observations of one task revision."""
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
    suites = {}
    for e in recent:
        if e.get('suite') and isinstance(e.get('failures'), int):
            suites.setdefault(e['suite'], []).append(e['failures'])
    stalled = any(len(v) >= 3 and v[-1] > 0 and v[-3] <= v[-2] <= v[-1] for v in suites.values())
    retry = sum(e.get('retry_language') is True for e in recent) >= 3
    signals = {'repeated_error': repeated, 'edit_oscillation': oscillation,
               'stalled_tests': stalled, 'retry_language': retry}
    score = 3*repeated + 3*oscillation + 3*stalled + retry
    return {'score': score, 'signals': signals, 'window_events': len(recent),
            'recommendation': LADDER[level] if score >= 3 and level < 4 else 'hold',
            'applied': False, 'policy': 'heuristic-v1; threshold=3; window=6'}


class State:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, timeout=10)
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
        allowed = {'error','file','content_hash','suite','failures','retry_language','progress'}
        if set(observation)-allowed:
            raise ValueError('Unknown observation fields')
        payload = dict(observation)
        for key in ('retry_language','progress'):
            if key in payload and type(payload[key]) is not bool:
                raise ValueError(f'{key} must be boolean')
        if 'failures' in payload and (type(payload['failures']) is not int or payload['failures'] < 0):
            raise ValueError('failures must be a nonnegative integer')
        for key in ('error','file','content_hash','suite'):
            if key in payload and not isinstance(payload[key], str):
                raise ValueError(f'{key} must be a string')
        if 'error' in payload:
            error = ' '.join(payload.pop('error').split())
            if error:
                payload['error_hash'] = hashlib.sha256(error.encode()).hexdigest()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._owned(task,revision,owner)
            self._event(task,revision,'observation',payload)
        return self.recommend(task)

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
