"""Run a test command and record which of some source lines it executes, and under which test. Standard library
only, and imports nothing from modelpilot: it runs by path under the interpreter being tested, from the tree the
tests run in, in place of `python -m MODULE ARGS...`:

    python line_trace.py SPEC.json OUT.json -m pytest -q tests

SPEC.json: {"files": {"/abs/path.py": [line, ...]}}, the lines to watch. OUT.json gets, per watched file, its
executable lines (from its own compiled code, so this interpreter's line table), and for each watched line the
tests that ran it ("" for code run outside any test: imports, collection). The current test is pytest's
PYTEST_CURRENT_TEST, else the unittest test being run. Uses sys.monitoring where it exists (3.12+), sys.settrace
before. Subprocesses the tests start are not traced. The command's exit code is passed through.
"""
import json
import os
import runpy
import sys
import threading

_current = ['']


def current_test():
    name = os.environ.get('PYTEST_CURRENT_TEST')
    return name.rsplit(' (', 1)[0] if name else _current[0]


def _follow_unittest():
    import unittest.case
    run = unittest.case.TestCase.run

    def tracked(self, result=None):
        before, _current[0] = _current[0], self.id()
        try:
            return run(self, result)
        finally:
            _current[0] = before
    unittest.case.TestCase.run = tracked


def executable_lines(path):
    """Line numbers this interpreter's compiled code for the file can report."""
    try:
        code = compile(open(path, 'rb').read(), path, 'exec')
    except (OSError, SyntaxError, ValueError):
        return None
    lines, stack = set(), [code]
    while stack:
        c = stack.pop()
        lines.update(line for _, _, line in c.co_lines() if line is not None)
        stack.extend(k for k in c.co_consts if hasattr(k, 'co_lines'))
    return lines


def _watch(targets, hits):
    """Start recording; targets {absolute path: set(lines)}, hits {path: {line: set(test ids)}}."""
    known = {}

    def lines_of(filename):  # (path, watched lines) for a co_filename as the import system wrote it, resolved once
        if filename not in known:
            path = os.path.realpath(filename) if filename else None
            known[filename] = (path, targets[path]) if path in targets else None
        return known[filename]

    monitoring = getattr(sys, 'monitoring', None)
    if monitoring is not None:
        tool = monitoring.COVERAGE_ID
        monitoring.use_tool_id(tool, 'modelpilot-line-trace')

        def on_line(code, line):
            found = lines_of(code.co_filename)
            if found is None or line not in found[1]:
                return monitoring.DISABLE
            hits[found[0]].setdefault(line, set()).add(current_test())
        monitoring.register_callback(tool, monitoring.events.LINE, on_line)
        monitoring.set_events(tool, monitoring.events.LINE)
        return

    def local(frame, event, arg):
        if event == 'line':
            found = lines_of(frame.f_code.co_filename)
            if frame.f_lineno in found[1]:
                hits[found[0]].setdefault(frame.f_lineno, set()).add(current_test())
        return local

    def global_trace(frame, event, arg):
        return local if lines_of(frame.f_code.co_filename) is not None else None
    threading.settrace(global_trace)
    sys.settrace(global_trace)


def main(argv):
    spec_path, out_path, flag, module, *args = argv
    if flag != '-m':
        raise SystemExit('usage: line_trace.py SPEC.json OUT.json -m MODULE [ARGS...]')
    spec = json.loads(open(spec_path).read())
    targets = {os.path.realpath(p): set(lines) for p, lines in spec['files'].items()}
    hits = {p: {} for p in targets}
    sys.path[0] = os.getcwd()  # as `python -m` has it, not this file's directory
    sys.argv = [module, *args]
    _follow_unittest()
    _watch(targets, hits)
    code = 0
    try:
        runpy.run_module(module, run_name='__main__', alter_sys=True)
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else 0 if stop.code is None else 1
    finally:
        sys.settrace(None)
        threading.settrace(None)
        if getattr(sys, 'monitoring', None) is not None:
            sys.monitoring.set_events(sys.monitoring.COVERAGE_ID, 0)
        out = {'exit_code': code, 'files': {}}
        for path, lines in targets.items():
            executable = executable_lines(path)
            out['files'][path] = {'executable': None if executable is None else sorted(executable),
                                  'hits': {str(line): sorted(tests) for line, tests in sorted(hits[path].items())}}
        with open(out_path, 'w') as f:
            json.dump(out, f)
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
