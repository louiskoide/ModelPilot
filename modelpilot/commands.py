"""ModelPilot's command-line modules in one registry (October 15, overnight cloud run): every module with a `main`, a
one-line summary, whether it can send a billable request, and the arguments for a plan-only run.

paid: the module can send a request someone is billed for (Anthropic or TypeSafe), and only does so behind --live.
That is every name on CLAUDE.md's "Paid" line plus late_fix, cache_replication, miss_checklist and spec_tests, found by
reading their code. A module that only forwards what a client sends (proxy), drives the client against a loopback
fixture (jev_compat, thinking_probe's capture-shape) or runs tests on the host is not paid.

plans: argv lists for a plan-only run of a paid module (no --live), which sends nothing; tests/test_paid_guards.py runs
each with every outbound path trapped. Placeholders: {root} the repository, {tmp} a new empty directory and {claude} a
Claude Code executable, all the caller's. late_fix's plan reads a prepared directory: {tmp}/late-fix must hold a
plan.json (`late_fix prepare` writes one; an empty item list will do for a plan); governed_session's --suite plan reads a
suite declaration at {tmp}/suite.json (suite_adapter.load_spec).
needs: the stop threshold a --live run refuses to start without (checked before anything is sent).
refusals: messages a plan-only run may stop with instead of a plan, before anything is sent (a missing client).
"""
from collections import namedtuple
import importlib

Command = namedtuple('Command', 'name module summary paid plans needs refusals')


def command(name, summary, paid=False, plans=(), needs=None, refusals=()):
    return Command(name, f'modelpilot.{name}', summary, paid, tuple(tuple(p) for p in plans), needs, tuple(refusals))


COMMANDS = (
    command('bench', 'Benchmark harness: tasks × arms × trials, isolated, graded, wire-accounted', paid=True,
            plans=[['--tasks', 'parse-decimal-grouping', '--arms', 'sonnet-5.5-low-concise', '--claude', '{claude}']],
            needs='--run-budget'),
    command('bench_report', "A benchmark run's summary: cold-equivalent cost, paired bootstrap, router pricing ($0)"),
    command('bench_tasks', 'Mine, lock and validate benchmark tasks from cloned repositories ($0)'),
    command('bench_tools', "The ModelPilot arm's stdio MCP tools: run_tests, search, expand_output ($0)"),
    command('cache_probe', 'Direct Messages API cache experiments (M0)', paid=True,
            plans=[['--config', '{root}/configs/m0.json', '--out', '{tmp}/cache-probe']]),
    command('cache_replication', 'M0 replication on current models, and the one-hour lifetime probe', paid=True,
            plans=[['--out', '{tmp}/replication']]),
    command('cascade_check', 'Two cheap-model drafts under the dry-run M4 verifier; one governed fallback', paid=True,
            plans=[[], ['--execute-fallback']]),
    command('claude_check', 'A small real Claude Code check through the M1 proxy', paid=True, plans=[[]],
            refusals=['Claude Code is missing']),
    command('client_matrix', 'Client-version compatibility against loopback fixtures; a report, the pin stays ($0)'),
    command('doctor', 'A $0 preflight before live runs: read-only checks, each pass, warn or fail ($0)'),
    command('evaluate', 'Fixed model/effort smoke benchmark, four typed-answer tasks (M6)', paid=True, plans=[[]]),
    command('exploration', "The modelpilot-explore arm's report: switched against unswitched trials ($0)"),
    command('explorer_replay', 'Plan item 14: would a Haiku 5.5 read/search explorer pay, on recorded trials ($0)'),
    command('exposure', "Plan item 7: how exposed a change is should it turn out wrong ($0)"),
    command('governed_session', 'One isolated Claude Code session under the dry-run governor', paid=True,
            plans=[['--claude', '{claude}'], ['--claude', '{claude}', '--scenario', 'turn-end-rebuild'],
                   ['--claude', '{claude}', '--subscription', '--suite', '{tmp}/suite.json'],
                   ['--claude', '{claude}', '--scenario', 'turn-end-rebuild', '--subscription']]),
    command('governor', 'The dry-run governor: a demo report of admit, settle, review and rebase ($0)'),
    command('hooks', 'Claude Code hooks for the dry-run governor, and its coordinator commands ($0)'),
    command('idle_replay', 'Idle boundaries in recorded logs, and what each idle-plan option would have saved ($0)'),
    command('jev_check', 'Pinned Jev router-only credential preflight (TypeSafe)', paid=True, plans=[[]]),
    command('jev_compat', 'Can pinned Jev route this client: a loopback probe, or --prepare-compat ($0)'),
    command('jev_route_check', 'One-task end-to-end Jev routing preflight through the stock launcher', paid=True,
            plans=[[], ['--variant', 'compat', '--accounting', 'wire']]),
    command('late_fix', 'Plan item 7: what a miss costs to fix once noticed late', paid=True,
            plans=[['run', '{tmp}/late-fix']], needs='--limit-usd'),
    command('long_session', "Long sessions (plan item 6): sequences file and a run's per-turn report ($0)"),
    command('m2', 'M2 ledger: tasks, revisions, leases, corrections, observations ($0)'),
    command('m2_mcp', 'Read-only stdio MCP access to M2 outputs and task state ($0)'),
    command('m4', 'M4 dry-run cascade decisions and shadow-cache estimates ($0)'),
    command('m5', 'M5 controls offline: coalescing, budget and compaction checks ($0)'),
    command('midtask_compaction', "Mid-task compaction (plan item 6): a split-shape run's report ($0)"),
    command('miss_checklist', 'Miss detector: a requirements checklist, list/judge paid, score/plan $0', paid=True),
    command('miss_crosscheck', "Miss detector: other agents' tests run against each saved fix ($0)"),
    command('miss_detector', 'Miss detector: host facts and suite checks after a run ($0)'),
    command('policy_replay', "Replay recorded ModelPilot decisions through the current policy ($0)"),
    command('proxy', 'Loopback Messages API pass-through proxy with dry-run decisions; forwards, originates nothing'),
    command('proxy_overhead', 'What the proxy adds: first byte, per event, throughput, CPU, memory (loopback, $0)'),
    command('regrade', "Re-grade a finished run's saved fixes, edge suites included ($0)"),
    command('report', 'Summarize a proxy log: costs and latency ($0)'),
    command('soak', 'M1 soak against a local fake API ($0)'),
    command('spec_tests', 'Plan item 5: tests a stronger model writes from a spec; write paid, score $0', paid=True),
    command('thinking_probe', 'Thinking history across setting changes, direct API; capture-shape is $0', paid=True,
            plans=[['--out', '{tmp}/thinking-probe']]),
    command('timeline', "A session's requests and the governor's events as one static HTML page ($0)"),
    command('worker_check', 'Bounded live M3 check: search and fixed-test worker sessions', paid=True, plans=[[]]),
)

# Modules with a `main` that aren't commands, and why.
EXCLUDED = {
    '__main__': 'python3 -m modelpilot itself: it lists these commands and runs one',
    'line_trace': 'a tracer run by path with the bench interpreter (exposure.trace); its main takes argv, no CLI',
}

# Paid modules tests/test_paid_guards.py doesn't run: another session was editing them on October 15.
GUARD_SKIPPED = {
    'miss_checklist': 'the miss detector is out of scope tonight (another session edits it)',
    'spec_tests': 'the miss detector is out of scope tonight (another session edits it)',
}


def find(name):
    """The registry row for a command name, or None."""
    return next((c for c in COMMANDS if c.name == name), None)


def paid():
    return [c for c in COMMANDS if c.paid]


def plan_args(plan, **values):
    """A plan's argv with its placeholders ({root}, {tmp}, {claude}) filled in."""
    return [arg.format(**values) for arg in plan]


def load(name):
    """The command's module, imported."""
    return importlib.import_module(find(name).module)
