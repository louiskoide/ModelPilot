"""Pytest plugin and runtime for the behavioural vote's probes (`behavior_vote.py`). Standalone, stdlib and pytest only:
`behavior_vote` copies it into a scratch directory and the task's interpreter loads it with `-p vote_plugin`.

A probe is a peer's test with its expected values dropped (`behavior_vote.probe_source`): each assertion records the
values it would have compared, a raises check records what was raised (or 'none') and swallows it, a warns check records
the warning categories, and a `fail()` call records that it was reached. This plugin keeps only the selected tests
(VOTE_SELECT, a JSON list of [file, qualified name]), runs them, and writes to VOTE_OUT, for each probe (a pytest node
id, parameters included):

- `status`: 'ok'; 'skipped'; 'assertion' (an AssertionError raised outside the probe's own file, by a helper: it
  would rest on an expected value, so the probe can't be read on that tree); otherwise `<phase>:<exception type>`;
- `obs`: the ordered observations [site, kind, digest, prefix], site being the original line:column;
- `warnings`: the sorted warning categories the test call emitted (pytest's own left out).

Files that fail to collect are listed apart (`collect`) as 'collect:<exception type>'. Values are read by `canon`:
exact dicts by sorted items, sets sorted, floats to 10 significant digits, anything else by repr with memory addresses
and the tree's and temporary directories' paths replaced, so equal behaviour on two trees reads the same.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import warnings

import pytest

PREFIX = 120
DEPTH = 8
ADDRESS = re.compile(r' at 0x[0-9a-fA-F]+')
_state = {'current': None, 'probes': {}, 'collect': {}, 'replace': []}


def canon(value, depth=0):
    """A text for a value that two trees agree on when the value is the same (module docstring)."""
    if depth > DEPTH:
        return '...'
    kind = type(value)
    if value is None or kind is bool:
        return repr(value)
    if kind is float:
        return format(value, '.10g')
    if kind in (int, str, bytes, complex):
        return repr(value)
    if kind in (list, tuple):
        return f"{kind.__name__}[{', '.join(canon(v, depth + 1) for v in value)}]"
    if kind is dict:
        items = sorted((canon(k, depth + 1), canon(v, depth + 1)) for k, v in value.items())
        return 'dict{' + ', '.join(f'{k}: {v}' for k, v in items) + '}'
    if kind in (set, frozenset):
        return f"{kind.__name__}{{{', '.join(sorted(canon(v, depth + 1) for v in value))}}}"
    try:
        return repr(value)
    except Exception as error:  # a broken __repr__ is behaviour too
        return f'<repr raised {type_name(error)}>'


def unordered(value):
    """canon of an iterable's items in sorted order (assertCountEqual's comparison)."""
    return 'unordered[' + ', '.join(sorted(canon(v) for v in value)) + ']'


def type_name(error):
    return type(error).__qualname__


def _text(text):
    for old, new in _state['replace']:
        text = text.replace(old, new)
    return ADDRESS.sub(' at 0x?', text)


def _record(site, kind, text):
    probe = _state['probes'].get(_state['current'])
    if probe is None:
        return  # outside a selected test (module import, another test)
    text = _text(text)
    probe['obs'].append([site, kind, hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:16], text[:PREFIX]])


def _cbv_value(site, value, sort=False):
    try:
        text = unordered(value) if sort else canon(value)
    except Exception as error:  # iterating or comparing the value raised
        _record(site, 'raised', type_name(error))
        return
    _record(site, 'value', text)


def _cbv_raised(site, error):
    _record(site, 'raised', type_name(error))


def _cbv_reached(site, *args, **kwargs):
    _record(site, 'reached', 'fail')


class _Raises:
    """Stands in for pytest.raises' ExceptionInfo and assertRaises' context: records the exception (or 'none') and
    swallows it. Only Exception subclasses: pytest's skip and fail outcomes pass through."""

    def __init__(self, site):
        self.site = site
        self.type = self.value = self.exception = self.tb = self.typename = None

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        if kind is not None and not issubclass(kind, Exception):
            return False
        self.type, self.value, self.exception, self.tb = kind, value, value, tb
        self.typename = kind.__name__ if kind else None
        _record(self.site, 'raises', type_name(value) if value is not None else 'none')
        return True

    def match(self, *args, **kwargs):  # a message pattern is an expected value: dropped
        return True

    def errisinstance(self, kind):
        return self.value is not None and isinstance(self.value, kind)


def _cbv_raises(site):
    return _Raises(site)


def _cbv_raises_call(site, func, *args, **kwargs):
    info = _Raises(site)
    with info:
        func(*args, **kwargs)
    return info


class _Warns:
    """Stands in for pytest.warns' recorder and assertWarns' context: records the categories warned in the block (as
    both do, with every warning shown) and raises nothing."""

    def __init__(self, site):
        self.site = site
        self._context = None
        self.list = []

    def __enter__(self):
        self._context = warnings.catch_warnings(record=True)
        self.list = self._context.__enter__()
        warnings.simplefilter('always')
        return self

    def __exit__(self, *exc):
        self._context.__exit__(*exc)
        _record(self.site, 'warns', ','.join(sorted({w.category.__qualname__ for w in self.list})) or 'none')
        return False

    def __len__(self):
        return len(self.list)

    def __iter__(self):
        return iter(self.list)

    def __getitem__(self, index):
        return self.list[index]

    @property
    def warnings(self):
        return self.list

    @property
    def warning(self):
        return self.list[0].message if self.list else None

    @property
    def filename(self):
        return self.list[0].filename if self.list else None

    @property
    def lineno(self):
        return self.list[0].lineno if self.list else None

    def pop(self, kind=Warning):
        for index, item in enumerate(self.list):
            if issubclass(item.category, kind):
                return self.list.pop(index)
        return None

    def clear(self):
        self.list.clear()


def _cbv_warns(site):
    return _Warns(site)


def _cbv_warns_call(site, func, *args, **kwargs):
    with _Warns(site):
        return func(*args, **kwargs)


def key_of(nodeid):
    """(file, qualified name without parameters) of a node id, as behavior_vote selects tests."""
    path, _, rest = nodeid.partition('::')
    return path, rest.split('[', 1)[0]


def pytest_configure(config):
    select = os.environ.get('VOTE_SELECT')
    config._vote_select = {tuple(x) for x in json.loads(Path(select).read_text())} if select else None
    replace = []
    for variable, placeholder in (('VOTE_TREE', '<TREE>'), ('VOTE_TMP', '<TMP>')):
        for path in filter(None, os.environ.get(variable, '').split(os.pathsep)):
            replace += [(os.path.realpath(path), placeholder), (os.path.abspath(path), placeholder)]
    _state['replace'] = sorted(set(replace), key=lambda pair: -len(pair[0]))


def pytest_collection_modifyitems(session, config, items):
    select = getattr(config, '_vote_select', None)
    if select is None:
        return
    keep = [item for item in items if key_of(item.nodeid) in select]
    drop = [item for item in items if key_of(item.nodeid) not in select]
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


def _collect_error(report):
    found = re.findall(r'^E\s+([A-Za-z_][\w.]*)(?::|\s*$)', str(report.longrepr), re.M)
    return found[-1].rsplit('.', 1)[-1] if found else 'unknown'


def pytest_collectreport(report):
    if report.failed:
        _state['collect'][report.nodeid.split('::', 1)[0]] = 'collect:' + _collect_error(report)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _state['current'] = item.nodeid
    _state['probes'][item.nodeid] = {'key': list(key_of(item.nodeid)), 'status': 'ok', 'obs': [], 'warnings': []}
    yield
    _state['current'] = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    with warnings.catch_warnings(record=True) as caught:
        yield
    probe = _state['probes'].get(item.nodeid)
    if probe is not None:
        probe['warnings'] = sorted({w.category.__qualname__ for w in caught
                                    if not issubclass(w.category, pytest.PytestWarning)})


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    probe = _state['probes'].get(item.nodeid)
    if probe is None or probe['status'] != 'ok' or report.passed:
        return
    if report.skipped:
        probe['status'] = 'skipped'
        return
    error = call.excinfo.value if call.excinfo is not None else None
    if isinstance(error, AssertionError):
        probe['status'] = 'assertion'
    else:
        probe['status'] = f"{call.when}:{type_name(error) if error is not None else 'unknown'}"


def pytest_sessionfinish(session):
    out = os.environ.get('VOTE_OUT')
    if out:
        Path(out).write_text(json.dumps({'probes': _state['probes'], 'collect': _state['collect']}, indent=0) + '\n')
