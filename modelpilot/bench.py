"""M6 benchmark harness: repository tasks × arms × trials under identical limits.

Each trial gets a fresh history-free checkout, an isolated HOME/TMPDIR/Claude config, the
same tools, turn limit and stop threshold, and ModelPilot's proxy for wire accounting. After
the client exits, the shared hidden grader runs without provider credentials. Order is
randomized with a recorded seed. Without --live nothing is sent. Agents may run arbitrary
commands inside their checkout: this is not an OS sandbox, so only use trusted task repos.

Session shapes: 'single' is one prompt. 'followup' resumes the same session with FOLLOW_UP
after --gap seconds (0 keeps the prompt cache warm; 330 lets it expire); other trials run
while one waits. Before any request, each task's reference is graded once ($0), and a trial
must pass as many hidden tests as its reference did. Costs are reported cold-equivalent
(see bench_report) next to measured. The client binary is pinned for the whole run.

Jev arms (see bench_jev) run Jev's own proxy for the whole trial in front of the trial's
ModelPilot proxy, with the jev-router sentinel instead of --model. Their dollars are provider
cost only: router (TypeSafe) cost is unpriced, so they are a lower bound.

The ModelPilot arm runs its active policy (active_policy; user-approved for this arm only)
through modelpilot_adapter. Its governor enforces the same per-session limit on wire cost; a
request it refuses never reaches the provider and ends the session.
"""
import argparse
from collections import Counter
import getpass
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from . import bench_jev, bench_report, bench_tasks, regrade, switch_policy
from .governed_session import client_env
from .cache_probe import ACCOUNT_MESSAGE
from .jev_route_check import PATCH, TOKEN_FIELDS, check_anthropic_key, parse_events
from .policy_actions import MODELS as POLICY_TIERS
from .proxy import ProxyServer

ROOT = Path(__file__).resolve().parents[1]
# A trial passes only if it also passes its task's edge suite, where it has one (user decision, October 4):
# tuning tasks only; the locked final tasks have none, so they are graded on their hidden tests alone.
EDGE_ROOT = regrade.EDGE
PASS_RULE = ('the hidden grader, and every test of the task\'s edge suite where it has one (tuning tasks; '
             'user decision, October 4); before October 4 runs recorded the hidden grader alone')
TOOLS = 'Read,Edit,Write,Bash,Glob,Grep'
PREAMBLE = ('You are working in a Python repository in the current directory. Complete the task below. '
            'Run the relevant tests with python3 before you finish.\n\nTask: ')
FOLLOW_UP = "Run the repository's full test suite and fix anything that fails because of your change."
SHAPES = ('single', 'followup')
ARMS = {
    'opus-5': {'kind': 'fixed', 'model': 'claude-opus-5'},
    'opus-5.5': {'kind': 'fixed', 'model': 'claude-opus-5-5'},  # the policy's top rung since September 26
    'sonnet-5': {'kind': 'fixed', 'model': 'claude-sonnet-5'},
    'sonnet-5.5': {'kind': 'fixed', 'model': 'claude-sonnet-5-5'},  # the policy's middle tier since September 28
    # A fixed arm with an effort passes it as --effort; without one the client picks (medium for both 5.5 models,
    # on the wire). Sonnet 5.5 low: a cheaper setting a router could move tasks to if it passes (October 3).
    'sonnet-5.5-low': {'kind': 'fixed', 'model': 'claude-sonnet-5-5', 'effort': 'low'},
    # A fixed arm with append_system_prompt passes that file (relative to the repository) as
    # --append-system-prompt-file. Sonnet 5.5 concise: medium Sonnet told to keep its fix, tests, tool output
    # and prose small, since everything it adds is written to the cache and re-read every step (October 3).
    'sonnet-5.5-concise': {'kind': 'fixed', 'model': 'claude-sonnet-5-5', 'append_system_prompt': 'bench/prompts/concise.md'},
    # Both levers at once (October 3): low effort cut output, the concise prompt cut what each step adds; each saved
    # about 12% at equal quality on the 23 tuning tasks, by different routes, so this asks whether they add up.
    'sonnet-5.5-low-concise': {'kind': 'fixed', 'model': 'claude-sonnet-5-5', 'effort': 'low',
                               'append_system_prompt': 'bench/prompts/concise.md'},
    # Long sessions (plan item 6): low concise with the cache lifetime set rather than left to the login (the client
    # writes 5m entries with an API key and 1h ones on a subscription; CLAUDE_CODE_PROMPT_CACHE_TTL sets it, 2.1.284,
    # checked at $0). The lifetime is what these two compare, so their rows are priced as sent on either login.
    'sonnet-5.5-low-concise-5m': {'kind': 'fixed', 'model': 'claude-sonnet-5-5', 'effort': 'low',
                                  'append_system_prompt': 'bench/prompts/concise.md', 'prompt_cache_ttl': '5m'},
    'sonnet-5.5-low-concise-1h': {'kind': 'fixed', 'model': 'claude-sonnet-5-5', 'effort': 'low',
                                  'append_system_prompt': 'bench/prompts/concise.md', 'prompt_cache_ttl': '1h'},
    'haiku-4.5': {'kind': 'fixed', 'model': 'claude-haiku-4-5-20251001'},  # kept so its earlier runs reproduce
    # Haiku 5.5 (October 8): the policy's cheapest tier in place of Haiku 4.5, and cheaper per token at any prompt length
    # than every other tier. Is it enough on the tuning tasks? Without an effort the client sends medium (2.1.284, checked
    # at $0); low concise mirrors the cheapest Sonnet arm. 2.1.284 has no price for it, so the client's own cost won't
    # match the proxy's (client_cost_matches); reports use the proxy's.
    'haiku-5.5': {'kind': 'fixed', 'model': 'claude-haiku-5-5'},
    'haiku-5.5-low-concise': {'kind': 'fixed', 'model': 'claude-haiku-5-5', 'effort': 'low',
                              'append_system_prompt': 'bench/prompts/concise.md'},
    # Jev picks the served model per turn; the client only sends the sentinel.
    'jev-stock': {'kind': 'jev', 'variant': 'stock', 'model': 'jev-router', 'checkout': 'work/jev-router-baseline',
                  'patch': None},
    'jev-compat': {'kind': 'jev', 'variant': 'compat', 'model': 'jev-router', 'checkout': 'work/jev-router-compat',
                   'patch': PATCH},
    # Model-constrained compat Jev (recorded separately): it discovers only the policy's tiers, so it routes among the
    # same models as the fixed and ModelPilot arms. Stock Jev cannot be aligned: it never routes this client and
    # its fallback is its static claude-opus-5.
    'jev-compat-o55': {'kind': 'jev', 'variant': 'compat', 'model': 'jev-router', 'checkout': 'work/jev-router-compat',
                       'patch': PATCH, 'models': POLICY_TIERS},
    # Jev (the compat checkout, advice only) predicts the model and effort; ModelPilot's proxy jumps there when the
    # expected cost says it pays (active_policy). The client asks for the fallback start (S0), used while Jev is
    # unavailable. Like jev-compat-o55 it discovers only the policy's models, so Jev's model question is the same.
    # Since October 6 the start is low concise, the cheapest fixed setting at equal strict passes (plan item 2): the
    # client runs at low effort with the concise prompt appended, which stays in every request whatever the setting.
    'modelpilot': {'kind': 'modelpilot', 'model': 'claude-sonnet-5-5', 'effort': 'low', 'advisor': 'jev',
                   'append_system_prompt': 'bench/prompts/concise.md',
                   'checkout': 'work/jev-router-compat', 'models': POLICY_TIERS,
                   'served_models': sorted({m for m, _ in switch_policy.settings(switch_policy.load())}),
                   'policy': 'docs/m6-modelpilot-policy.md'},
    # The same arm with delegation on (October 5; docs/m6-modelpilot-policy.md, "Delegation"): priced consults and
    # handoff notes, plus a consult made whatever its price when the agent ends its turn (the Stop hook holds the
    # finish once per turn), to measure what a review by a stronger setting is worth. Reported as its own arm.
    # From the low start (October 6) the forced review goes to Jev's setting when that is stronger, usually Sonnet 5.5
    # medium, where from medium it went to Opus 5.5 medium (switch_policy._consult_target).
    'modelpilot-delegate': {'kind': 'modelpilot', 'model': 'claude-sonnet-5-5', 'effort': 'low', 'advisor': 'jev',
                            'append_system_prompt': 'bench/prompts/concise.md',
                            'checkout': 'work/jev-router-compat', 'models': POLICY_TIERS,
                            'served_models': sorted({m for m, _ in switch_policy.settings(switch_policy.load())}),
                            'policy': 'docs/m6-modelpilot-policy.md',
                            'policy_overrides': {'delegation': {'consult': {'enabled': True, 'force': ['agent_finish']},
                                                                'handoff_note': {'enabled': True}}}},
    # Randomized mid-task switches, for measurement (user request, October 7; config exploration): low concise with no
    # advisor and no gate moves; about half the trials, by a draw from their task, arm and trial number, switch to
    # Opus 5.5 at the turn's effort at a random main-loop request from 2 to 5. Needs no TypeSafe key.
    'modelpilot-explore': {'kind': 'modelpilot', 'model': 'claude-sonnet-5-5', 'effort': 'low', 'advisor': None,
                           'append_system_prompt': 'bench/prompts/concise.md', 'models': POLICY_TIERS,
                           'served_models': ['claude-opus-5-5', 'claude-sonnet-5-5'],
                           'policy': 'docs/m6-modelpilot-policy.md',
                           'policy_overrides': {'exploration': {'enabled': True}}},
    # The modelpilot arm with the quality floor on (user decision, October 8; docs/m6-modelpilot-policy.md, "Quality
    # floor"): no worse than the user's own model, one arm per baseline, each compared with that baseline's fixed arm
    # (sonnet-5.5, opus-5.5). Each starts at the cheapest setting its floor allows on the October 8 evidence: low concise
    # against Sonnet 5.5 medium, Opus 5.5 medium (with the concise prompt) against Opus 5.5 medium.
    'modelpilot-for-sonnet': {'kind': 'modelpilot', 'model': 'claude-sonnet-5-5', 'effort': 'low', 'advisor': 'jev',
                              'append_system_prompt': 'bench/prompts/concise.md',
                              'checkout': 'work/jev-router-compat', 'models': POLICY_TIERS,
                              'served_models': sorted({m for m, _ in switch_policy.settings(switch_policy.load())}),
                              'policy': 'docs/m6-modelpilot-policy.md',
                              'policy_overrides': {'quality_floor': {'enabled': True,
                                                                     'baseline': 'claude-sonnet-5-5/medium'}}},
    'modelpilot-for-opus': {'kind': 'modelpilot', 'model': 'claude-opus-5-5', 'effort': 'medium', 'advisor': 'jev',
                            'append_system_prompt': 'bench/prompts/concise.md',
                            'checkout': 'work/jev-router-compat', 'models': POLICY_TIERS,
                            'served_models': sorted({m for m, _ in switch_policy.settings(switch_policy.load())}),
                            'policy': 'docs/m6-modelpilot-policy.md',
                            'policy_overrides': {'quality_floor': {'enabled': True,
                                                                   'baseline': 'claude-opus-5-5/medium'}}},
}
RUNNABLE = ('fixed', 'jev')
AUTHS = ('api_key', 'subscription')
# Fixed arms can run on a Claude subscription (claude setup-token) instead of an API key. The client then asks for 1h
# cache writes, so dollars are reported API-key equivalent (bench_report.api_key_equivalent), as-sent alongside.
SUBSCRIPTION_KINDS = ('fixed',)
IDLE_SECONDS = 130  # the proxy's upstream socket timeout (120 s) bounds any request still in flight
# Models the pinned client (2.1.284) has no price for, checked at $0 against the owned fixture: it charges them its
# unknown-model rate, Opus 5.5's, so its --max-budget-usd stop and its own cost are in those dollars (Haiku 5.5: about
# 40x the wire cost, so the default $1 stops at about $0.025 of real spend). A proxy move doesn't change the client's
# price: it prices every reply as the model it asked for, whatever model the reply names, so a ModelPilot session moved
# to Haiku 5.5 is charged at Sonnet 5.5's rates, about 13x (runs/client-proxy-move-price-20261010.json).
CLIENT_UNPRICED = ('claude-haiku-5-5',)
# Client result subtypes, pinned by the offline tests against Claude Code 2.1.281.
STOPS = {'error_max_turns': 'turn_limit', 'error_max_budget_usd': 'budget_stop'}
# ModelPilot arm: why its proxy refused a request, which ends the session.
REFUSALS = {'insufficient_budget': 'budget_stop', 'cost_unknown': 'cost_unknown_halt'}
# Files outside the restored test directory that can change which tests run, or how.
TEST_CONFIG = ('conftest.py', 'pytest.ini', 'tox.ini', 'setup.cfg', 'pyproject.toml', 'sitecustomize.py', 'usercustomize.py')


class ClientChanged(RuntimeError):
    """The Claude Code binary no longer reports the version the run started with."""


class PreflightError(RuntimeError):
    """A task's reference solution fails its grader in this environment."""


def rates():
    """Rates for every benchmark model: the 5-family table, the policy's tiers and M0's 4.6 entries. Haiku 5.5's are
    priced by prompt length (cache_probe.tier)."""
    merged = json.loads((ROOT/'configs/m0.json').read_text())['rates']
    merged.update(json.loads((ROOT/'configs/jev-rates.json').read_text())['rates'])
    merged.update(json.loads((ROOT/'configs/opus-5-5-rates.json').read_text())['rates'])
    merged.update(json.loads((ROOT/'configs/sonnet-5-5-rates.json').read_text())['rates'])
    merged.update(json.loads((ROOT/'configs/haiku-5-5-rates.json').read_text())['rates'])
    return merged


def model_eligibility(routing, models, catalog):
    """A model-constrained trial compares only if Jev saw exactly its model set and served nothing else."""
    routing['model_set'] = list(models)
    routing['outside_model_set'] = sorted(set(routing['served_models']) - set(models))
    reason = ('served_outside_model_set' if routing['outside_model_set'] else
              'catalog_incomplete' if not catalog or catalog.get('status') != 200 or set(catalog['models']) != set(models)
              else None)
    if reason:
        routing.update(benchmark_eligible=False, ineligible_reason=reason)
    return routing


def arm_tools(arm):
    """Whether the arm gets the R5 tools: only while delegation is on (October 6, plan item 3). Without it they were
    called in 3 of 28 trials and, with the channel declaration, cost about $0.0024 a task in the first request."""
    config = switch_policy.with_overrides(switch_policy.load(), ARMS[arm].get('policy_overrides'))
    return switch_policy.delegating(config)


def arm_adapter(arm, budget_usd, sessions, jev_key=None, advisor_stub=None, tools=None):
    """The ModelPilot arm's adapter: active policy with Jev as advisor, the R5 tools while delegation is on, and the
    per-session limit for each session. Without a TypeSafe key (or an offline stub) there is no advisor and the trial
    is ineligible. tools overrides arm_tools for offline tests that drive the policy through host test runs."""
    if ARMS[arm]['kind'] != 'modelpilot':
        return None
    from .advisor import JevAdvisor
    from .modelpilot_adapter import ModelPilotAdapter
    advisor = (JevAdvisor(ROOT/ARMS[arm]['checkout'], key=jev_key, stub=advisor_stub)
               if ARMS[arm].get('advisor') == 'jev' and (jev_key or advisor_stub is not None) else None)
    return ModelPilotAdapter(limit_usd=budget_usd * sessions, mode='active',
                             tools=arm_tools(arm) if tools is None else tools, arm_id=arm,
                             model=ARMS[arm]['model'], effort=ARMS[arm]['effort'], advisor=advisor,
                             models=ARMS[arm].get('models'), overrides=ARMS[arm].get('policy_overrides'))


def schedule(tasks, arms, trials, seed):
    order = [(t['id'], a, n) for t in tasks for a in arms for n in range(trials)]
    random.Random(seed).shuffle(order)
    return order


def accounting(rows, final, jev=False):
    """Wire accounting for one trial. Jev arms: provider cost only (router unpriced), and the
    client prices the jev-router sentinel with its own guess, so only its tokens are compared."""
    messages = [r for r in rows if r.get('kind') == 'messages']
    refused = Counter(r.get('refusal') for r in rows if r.get('kind') == 'refused')  # ModelPilot arm; never sent
    ok = [r for r in messages if r.get('http_status') == 200]
    # Sensitivity only: requests the API answered with an error, counted as free. A transport
    # failure or an unpriced success is never a rejection.
    answered = [r for r in messages if not (isinstance(r.get('http_status'), int) and r['http_status'] != 200)]
    if_free = sum(r['cost_usd'] for r in answered) if answered and all(r.get('cost_usd') is not None for r in answered) else None
    proxy_tokens = {wire: sum((r.get('usage') or {}).get(wire, 0) for r in ok) for wire, _ in TOKEN_FIELDS}
    usage = final.get('modelUsage') or {}
    client_tokens = {wire: sum(m.get(name, 0) for m in usage.values() if isinstance(m, dict)) for wire, name in TOKEN_FIELDS}
    unpriced = sum(r.get('cost_usd') is None for r in messages)
    known = sum(r['cost_usd'] for r in messages if r.get('cost_usd') is not None)
    client = final.get('total_cost_usd')
    # The ModelPilot policy's own consults and handoff notes: billed, but never seen by the client.
    side_rows = [r for r in rows if r.get('kind') == 'side_call']
    sides = [r for r in side_rows if r.get('status') != 'refused']
    side_unpriced = sum(r.get('cost_usd') is None for r in sides)
    side_known = sum(r['cost_usd'] for r in sides if r.get('cost_usd') is not None)
    out = {'requests': len(messages), 'http_statuses': [r.get('http_status') for r in messages],
           # Answered by the API with an error; a transport failure (no status) is not a rejection.
           'rejected_requests': sum(isinstance(r.get('http_status'), int) and r['http_status'] != 200 for r in messages),
           'transport_failures': sum(r.get('http_status') is None for r in messages),
           'refused_requests': sum(refused.values()), 'refusal_reasons': dict(refused),
           'count_tokens_requests': sum(r.get('kind') == 'count_tokens' for r in rows),  # free; never in requests
           'models': [r.get('model') for r in messages], 'unpriced_requests': unpriced,
           # Unknown cost stays unknown: a trial with any unpriced request has no dollar total.
           'cost_usd': known + side_known if unpriced == 0 and side_unpriced == 0 and messages else None,
           'known_cost_usd': known + side_known,
           'cost_if_rejected_free_usd': if_free + side_known if if_free is not None and side_unpriced == 0 else None,
           'rejected_assumption': 'API error responses counted as $0 (unconfirmed)',
           'cost_scope': 'provider_only_router_unpriced' if jev else 'complete',
           'proxy_tokens': proxy_tokens, 'client_tokens': client_tokens,
           'tokens_match': bool(usage) and proxy_tokens == client_tokens,
           'client_cost_usd': client,
           'client_cost_basis': 'sentinel_model_unknown_price' if jev else 'client_model_table',
           'client_cost_matches': None if jev else  # the client's own requests only
           isinstance(client, (int, float)) and unpriced == 0 and abs(client - known) < 1e-6,
           'first_byte_seconds': [r.get('first_byte_seconds') for r in messages]}
    if jev:
        out['router_cost_usd'] = None
    if side_rows:
        out.update(side_calls=len(sides), side_refused=len(side_rows) - len(sides), side_unpriced=side_unpriced,
                   side_purposes=dict(Counter(r.get('purpose') for r in sides)),
                   side_cost_usd=side_known if side_unpriced == 0 else None)
    return out


def subscription_accounting(rows, final, price_table, ttl=None):
    """A subscription trial: dollars as an API key would have been billed for its tokens (1h cache writes at the
    5m rate); the client comparison and the as-sent price use the rows as measured. An arm that sets its cache
    lifetime (ttl) gets the same writes with an API key, so its rows are priced as sent."""
    measured = accounting(rows, final)
    if ttl:
        fields = ('cost_usd', 'known_cost_usd', 'cost_if_rejected_free_usd')
        return dict(measured, cost_scope='complete', billing='subscription', as_sent={k: measured[k] for k in fields},
                    repricing=f'none: the arm sets {ttl} cache writes, which an API-key client sends alike')
    equivalent = accounting(bench_report.api_key_equivalent(rows, price_table), final)
    fields = ('cost_usd', 'known_cost_usd', 'cost_if_rejected_free_usd')
    return dict(measured, **{k: equivalent[k] for k in fields}, cost_scope='api_key_equivalent', billing='subscription',
                as_sent={k: measured[k] for k in fields},
                repricing='1h cache writes (subscription client) priced at the 5m rate an API-key client gets')


def rate_limited(final, rows):
    """A 429 ended the session: the account's rate or usage limit, not a model result."""
    return final.get('api_error_status') == 429 or any(r.get('kind') == 'messages' and r.get('http_status') == 429
                                                        for r in rows)


def stop_reason(status, final, rows):
    """How one client session ended."""
    if status == 'timeout':
        return 'timeout'
    subtype = final.get('subtype')
    if subtype == 'success' and not final.get('is_error'):
        return 'success'
    if subtype in STOPS:
        return STOPS[subtype]
    refusal = next((r.get('refusal') or '' for r in rows if r.get('kind') == 'refused'), None)
    if refusal is not None:
        return REFUSALS.get(refusal, 'policy_stop' if refusal.startswith('policy_stop') else 'policy_refused')
    if any(r.get('status') in ('transport_error', 'connection_closed') for r in rows):
        return 'transport_error'
    if rate_limited(final, rows):
        return 'rate_limited'  # a subscription's usage limit, or an API rate limit: not a model result
    if final.get('api_error_status') in (401, 403) or (
            final.get('api_error_status') and ACCOUNT_MESSAGE.search(str(final.get('result') or ''))):
        return 'account_error'  # the Anthropic account can't pay or authenticate: not a model result
    if final.get('api_error_status'):
        return 'api_error'
    return 'client_error'


def test_config_changes(paths, test_dir):
    """Changed paths the grader does not restore that can change how tests run (flagged for review)."""
    inside = test_dir.rstrip('/') + '/'
    return [p for p in paths if Path(p).name in TEST_CONFIG and not p.startswith(inside)]


def budget_text(usd):
    return ('%.10f' % usd).rstrip('0').rstrip('.')


def client_command(cli, prompt, model, max_turns, budget_usd, session, extra=(), effort=None, append_prompt=None):
    # The stop threshold applies per invocation: a resumed session gets its own. No model
    # (Jev arms) leaves the client on the sentinel the router's environment sets.
    return [str(cli), '-p', prompt, *(['--model', model] if model else []), *(['--effort', effort] if effort else []),
            *(['--append-system-prompt-file', str(append_prompt)] if append_prompt else []),
            '--output-format', 'stream-json', '--verbose',
            '--max-turns', str(max_turns), '--max-budget-usd', budget_text(budget_usd), '--setting-sources', '',
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--tools', TOOLS, '--allowedTools', TOOLS,
            *session, *extra]


def effort_check(rows, effort):
    """Whether a fixed arm's main-loop requests ran at the effort it asked for (a client could ignore --effort).
    None without any such request: the session's stop reason, not this check, says what happened."""
    sent = Counter(r.get('effective_effort') or r.get('effort') for r in rows
                   if r.get('kind') == 'messages' and r.get('tool_count'))
    return {'requested': effort, 'sent': {str(k): n for k, n in sorted(sent.items(), key=str)},
            'applied': set(sent) == {effort} if sent else None}


def ttl_check(rows, ttl):
    """Whether an arm that sets its cache lifetime got it: every main-loop request's breakpoints ask for that TTL,
    apart from compaction requests, which the client marks with the default 5m whatever the setting (2.1.284).
    None without any such request."""
    main = [r for r in rows if r.get('kind') == 'messages' and r.get('tool_count') and r.get('cache_ttl')]
    marked = Counter('/'.join(r['cache_ttl']) for r in main if not r.get('compaction'))
    return {'requested': ttl, 'marked': dict(sorted(marked.items())),
            'compaction_requests': sum(bool(r.get('compaction')) for r in main),
            'applied': set(marked) == {ttl} if marked else None}


def appended_prompt(arm):
    """An arm's appended system prompt (fixed arms, and the ModelPilot arms since October 6): its file, text and hash;
    None without one."""
    if not arm.get('append_system_prompt'):
        return None
    path = ROOT/arm['append_system_prompt']
    data = path.read_bytes()
    return {'path': arm['append_system_prompt'], 'file': path, 'text': data.decode().strip(),
            'sha256': hashlib.sha256(data).hexdigest()}


def prompt_check(rows, sha256):
    """Whether every main-loop request carried the arm's appended system prompt (the proxy's system_marker).
    None without any such request: the session's stop reason, not this check, says what happened."""
    seen = Counter(r.get('system_marker') for r in rows if r.get('kind') == 'messages' and r.get('tool_count'))
    return {'requested_sha256': sha256, 'present': seen[True], 'missing': sum(n for k, n in seen.items() if k is not True),
            'applied': set(seen) == {True} if seen else None}


def client_version(cli):
    return subprocess.run([str(cli), '--version'], capture_output=True, text=True, timeout=30).stdout.strip()


def client_problem(version):
    """Why this client can't run the benchmark, or None: every arm runs the version pinned in bench/environment.json."""
    env = json.loads((ROOT/'bench/environment.json').read_text())
    pinned = env.get('claude_code')
    if pinned and (version or '').split(' ')[0] != pinned:
        return (f'Claude Code {version} is not the pinned {pinned}; every arm must run the pinned client. '
                f'Install it with: {env["claude_code_install"]}, then put work/claude-client/node_modules/.bin first on '
                'PATH or pass --claude.')
    return None


def code_revision():
    """The commit the harness runs from, and whether its code differs from it (None outside a Git checkout)."""
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True)
    if head.returncode:
        return None
    changed = subprocess.run(['git', 'status', '--porcelain', '--', 'modelpilot', 'configs', 'bench'], cwd=ROOT,
                             capture_output=True, text=True).stdout
    return {'commit': head.stdout.strip(), 'uncommitted_changes': bool(changed.strip())}


def resolve_client(cli):
    """The real binary behind a CLI path and its version. The installed `claude` is a symlink
    the auto-updater moves, so a run holds on to the binary it started with."""
    path = Path(cli).resolve(strict=True)
    return path, client_version(path)


def inside(path, root):
    return path == root or path.startswith(root + os.sep)


def leftover_processes(directory):
    """PIDs, other than this process, whose working directory is inside directory.

    Claude Code's Bash tool starts commands in their own process groups, so killing the
    client's group misses background jobs. Their working directory still gives them away.
    """
    root, found = os.path.realpath(directory), set()
    if os.path.isdir('/proc/self'):
        for entry in os.listdir('/proc'):
            if entry.isdigit():
                try:
                    if inside(os.readlink(f'/proc/{entry}/cwd'), root):
                        found.add(int(entry))
                except OSError:
                    continue
    else:
        out = subprocess.run(['lsof', '-a', '-d', 'cwd', '-u', str(os.getuid()), '-F', 'pn'],
                             capture_output=True, text=True).stdout
        pid = None
        for line in out.splitlines():
            if line.startswith('p'):
                pid = int(line[1:])
            elif line.startswith('n') and pid is not None and inside(line[1:], root):
                found.add(pid)
    found.discard(os.getpid())
    return sorted(found)


def reap(directory):
    """Kill leftover processes working inside directory; returns their PIDs."""
    pids = leftover_processes(directory)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return pids


def signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def drain(stream, chunks):
    for chunk in iter(lambda: stream.read1(65536), b''):
        chunks.append(chunk)


def run_client(command, env, cwd, timeout, grace=10, reap_dir=None):
    """One client invocation in its own process group, output kept even on timeout.

    On timeout: SIGTERM, up to grace seconds, then SIGKILL. Afterwards nothing it started
    survives: its group, and any process working inside reap_dir, are killed.
    """
    proc = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    chunks = {'stdout': [], 'stderr': []}
    readers = [threading.Thread(target=drain, args=(getattr(proc, name), chunks[name]), daemon=True) for name in chunks]
    for reader in readers:
        reader.start()
    status = 'completed'
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            status = 'timeout'
            signal_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
    finally:
        signal_group(proc.pid, signal.SIGKILL)
        proc.wait()
        if reap_dir:
            reap(reap_dir)
        for reader, stream in zip(readers, (proc.stdout, proc.stderr)):
            reader.join(timeout=grace)
            if not reader.is_alive():  # a pipe some escaped process still holds stays with its reader
                stream.close()
    return {'status': status, 'returncode': proc.returncode,
            **{name: b''.join(parts).decode(errors='replace') for name, parts in chunks.items()}}


def python_version(python):
    return subprocess.run([python, '-c', 'import sys; print(sys.version.split()[0])'], capture_output=True,
                          text=True, check=True).stdout.strip()


def task_repo(task):
    return Path(task['repo_path']) if 'repo_path' in task else bench_tasks.REPOS/task['repo']


def read_rows(log):
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()] if log.exists() else []


def redact(text, key):
    return text.replace(key, '[REDACTED]') if key and len(key) >= 8 else text


class Trial:
    """One task × arm × trial: a first session, an optional follow-up, then grading.

    step() runs the next session and returns True while a follow-up is still due; the last
    step grades. trial.json is rewritten after every step, so a crash keeps the accounting.
    One ModelPilot proxy (and, for Jev arms, one Jev router) serves every session of the trial;
    close() releases them. jev_stub replaces the router's TypeSafe call (offline tests only).
    """
    def __init__(self, task, arm_id, trial_dir, cli, key, upstream, price_table, *, trial=0, python=None,
                 max_turns=30, budget_usd=1.0, timeout=900, grace=10, shape='single', gap=0,
                 expected_hidden_passed=None, client_version=None, jev_key=None, jev_stub=None, router=None,
                 adapter=None, auth='api_key', oauth_token=None):
        arm = ARMS[arm_id]
        if auth not in AUTHS:
            raise ValueError(f'unknown auth {auth}')
        if auth == 'subscription' and (arm['kind'] not in SUBSCRIPTION_KINDS or adapter is not None or not oauth_token):
            raise ValueError(f'{arm_id}: only fixed arms run on a subscription, and they need its token')
        if arm['kind'] not in RUNNABLE and adapter is None:
            raise NotImplementedError(f'{arm_id}: launcher not implemented yet (work item 4, docs/changelog.md)')
        if shape not in SHAPES:
            raise ValueError(f'unknown shape {shape}')
        if adapter is not None and adapter.arm_id != arm_id:
            raise ValueError('Adapter does not match requested arm')
        if adapter is not None and arm['kind'] == 'jev':
            raise ValueError('Jev arms run through bench_jev, not an adapter')
        self.adapter = adapter
        if adapter is not None:
            arm = dict(arm, model=adapter.model)
        self.task, self.arm = task, arm
        # Absolute: the client resolves its home, tmp and config paths against the workspace it runs in.
        self.dir = Path(trial_dir).absolute()
        self.cli, self.api_key, self.upstream, self.rates = cli, key, upstream, price_table
        self.python = python or bench_tasks.interpreter()
        self.limits = {'max_turns': max_turns, 'budget_usd': budget_usd, 'timeout_s': timeout, 'tools': TOOLS}
        self.grace, self.gap, self.expected, self.version = grace, gap, expected_hidden_passed, client_version
        self.prompts = [PREAMBLE + task['instruction']] + ([FOLLOW_UP] if shape == 'followup' else [])
        self.session_id = str(uuid.uuid4())
        self.sessions, self.final = [], {}
        self.started = self.finished = self.router_unavailable = self.account_error = self.rate_limited = False
        self.auth, self.oauth_token = auth, oauth_token
        self.proxy = self.proxy_thread = self.route = None
        self.router = (router or bench_jev.JevRouter(arm)) if arm['kind'] == 'jev' else None
        self.jev_key, self.jev_stub = jev_key, jev_stub
        self.appended = appended_prompt(arm)
        self.client_extra = []  # more client flags for every session (the sequence shape's --autocompact)
        self.record = {'task': task['id'], 'arm': arm_id, 'trial': trial, 'model': arm['model'], 'effort': arm.get('effort'),
                       'append_system_prompt': {k: self.appended[k] for k in ('path', 'sha256')} if self.appended else None,
                       'prompt_cache_ttl': arm.get('prompt_cache_ttl'),
                       'spec_sha256': bench_tasks.spec_hash(task), 'shape': shape,
                       'gap_requested_seconds': gap if shape == 'followup' else None,
                       'expected_hidden_passed': expected_hidden_passed, 'limits': self.limits,
                       'client_path': str(cli), 'client_version': client_version, 'auth': auth, 'complete': False}
        if self.router:
            self.record.update(self.router.describe())

    @property
    def log(self):
        return self.dir/'observations.jsonl'

    def known_cost(self):
        """Measured dollars (a subscription trial: as sent, API-key priced; its account is not billed per token)."""
        if self.finished:  # checked before every session of a run; a finished log no longer changes
            accounting = self.record['accounting']
            return (accounting.get('as_sent') or accounting)['known_cost_usd']
        return sum(r['cost_usd'] for r in read_rows(self.log) if r.get('cost_usd') is not None)

    def step(self):
        if self.version and client_version(self.cli) != self.version:
            raise ClientChanged(f'{self.cli} no longer reports {self.version}; not running more trials.')
        if self.router:
            self.router.verify()  # an agent could edit the checkout through --add-dir or Bash
            if self.started and not self.router.alive():
                raise bench_jev.JevRouterDown(f'Jev router for {self.dir} exited; a harness failure, not a model result.')
        if not self.started:
            self.setup()
        index = len(self.sessions)
        self.run_session(index)
        due = index + 1 < len(self.prompts)
        if due and self.sessions[-1]['stop'] != 'success':
            self.record['follow_up'] = 'skipped_after_' + self.sessions[-1]['stop']
            due = False
        if due:
            self.save('parked')
        else:
            self.finish()
        return due

    def next_gap(self):
        """Seconds to wait after the session that just ended before the next one starts."""
        return self.gap

    def setup(self):
        if self.adapter:
            self.adapter.verify()
        self.started = True
        self.dir.mkdir(mode=0o700, parents=True)
        paths = self.make_workspace()
        dirs = {name: self.dir/name for name in ('home', 'tmp', 'config')}
        for path in dirs.values():
            path.mkdir()
        env = client_env(os.environ, self.api_key or '', dirs, self.cli, {})
        if self.auth == 'subscription':  # the client's own login path for a subscription; no API key in its environment
            env.pop('ANTHROPIC_API_KEY')
            env['CLAUDE_CODE_OAUTH_TOKEN'] = self.oauth_token
        if self.arm.get('prompt_cache_ttl'):  # the main conversation's cache lifetime, whatever the login
            env['CLAUDE_CODE_PROMPT_CACHE_TTL'] = self.arm['prompt_cache_ttl']
        # The agent's python3/pip/pytest are the grader's interpreter, not whatever the system has.
        env['PATH'] = os.pathsep.join([str(Path(self.python).parent), env['PATH']])
        # Same import paths as the grader, as an editable install would give a developer.
        if paths:
            env['PYTHONPATH'] = os.pathsep.join(paths)
        self.record['python'] = python_version(self.python)
        if self.adapter and hasattr(self.adapter, 'setup'):
            self.adapter.setup(self)
        options = self.adapter.proxy_options() if self.adapter and hasattr(self.adapter, 'proxy_options') else {}
        if self.arm.get('models'):
            options['catalog'] = self.arm['models']
        if self.appended:
            options['system_marker'] = self.appended['text']
        self.proxy = ProxyServer(('127.0.0.1', 0), self.upstream, self.log, self.rates, **options)
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()
        proxy_url = f'http://127.0.0.1:{self.proxy.server_port}'
        if self.router:
            self.route = self.router.start(proxy_url, dirs, self.jev_key, self.dir/'jev.stderr.txt', stub=self.jev_stub)
            self.record['router'] = self.route.get('router')
            env.update(self.route['env'])  # the router's port and the jev-router sentinel
            if self.arm.get('models'):
                self.record['catalog'] = bench_jev.prefetch_catalog(self.route['env']['ANTHROPIC_BASE_URL'], self.api_key)
        else:
            env['ANTHROPIC_BASE_URL'] = proxy_url
            if self.adapter and hasattr(self.adapter, 'use_catalog'):
                # The ModelPilot arm's advisor describes the models from the (filtered) account catalog, as Jev does.
                catalog = bench_jev.prefetch_catalog(proxy_url, self.api_key, entries=True)
                self.adapter.use_catalog(catalog)
                self.record['catalog'] = {k: v for k, v in catalog.items() if k != 'entries'}
        self.env = env

    def make_workspace(self):
        """The agent's checkout (self.work) and its base commit; returns the grader's import paths for it."""
        self.repo = task_repo(self.task)
        self.work = bench_tasks.workspace(self.task, self.repo, self.dir/'workspace')
        # The agent may commit (Haiku did in 4a), so its fix is diffed against this commit, never against HEAD.
        self.base_commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.work, capture_output=True, text=True,
                                          check=True).stdout.strip()
        return bench_tasks.test_env_paths(self.task, self.work)

    def run_session(self, index):
        first_row = len(read_rows(self.log))
        # Sessions persist only in the trial's own config dir, so the follow-up can resume them.
        session = ['--session-id', self.session_id] if index == 0 else ['--resume', self.session_id]
        model, extra = (None, ['--add-dir', self.route['add_dir']]) if self.router else (self.arm['model'], [])
        extra = extra + self.client_extra
        fixed_effort = self.arm.get('effort') if self.arm['kind'] == 'fixed' else None  # the adapter sets its own
        command = client_command(self.cli, self.prompts[index], model, self.limits['max_turns'],
                                 self.limits['budget_usd'], session, extra, effort=fixed_effort,
                                 append_prompt=self.appended['file'] if self.appended else None)
        env = self.env
        if self.adapter:
            command = self.adapter.command(command, env['ANTHROPIC_BASE_URL'])
            env = self.adapter.environment(env)
        started_unix, started = time.time(), time.monotonic()
        try:
            result = run_client(command, env, self.work, self.limits['timeout_s'], grace=self.grace, reap_dir=self.dir)
        finally:
            wall = time.monotonic() - started
            # Every request the session started is logged before its rows are counted.
            idle = self.proxy.wait_idle(IDLE_SECONDS)
        for name, suffix in (('stdout', 'jsonl'), ('stderr', 'txt')):
            with (self.dir/f'client.{name}.{suffix}').open('a') as f:
                f.write(redact(redact(redact(result[name], self.adapter.key) if self.adapter else result[name],
                                      self.api_key), self.oauth_token))
        final = next((e for e in reversed(parse_events(result['stdout'])) if e.get('type') == 'result'), {})
        if final:
            self.final = final  # a resumed session's totals are cumulative for the whole session
        rows = read_rows(self.log)[first_row:]
        messages = sorted((r for r in rows if r.get('kind') == 'messages'), key=lambda r: r.get('started_unix', 0))
        first_read = next(((r.get('usage') or {}).get('cache_read_input_tokens') for r in messages
                           if r.get('http_status') == 200), None)
        record = {'prompt': 'task' if index == 0 else 'follow_up', 'status': result['status'],
                  'returncode': result['returncode'], 'stop': stop_reason(result['status'], final, rows),
                  'subtype': final.get('subtype'), 'num_turns': final.get('num_turns'), 'requests': len(messages),
                  'rows': [first_row, first_row + len(rows)], 'wall_seconds': round(wall, 3),
                  'started_unix': started_unix, 'ended_unix': time.time(), 'first_read_tokens': first_read,
                  'proxy_idle': idle}
        self.account_error = self.account_error or record['stop'] == 'account_error'
        self.rate_limited = self.rate_limited or record['stop'] == 'rate_limited'
        if self.router:
            record['router_pid'] = self.router.pid
            self.router.collect(self.dir/'tmp', self.dir/'decisions.json')
        if index:
            # Other trials may re-warm the shared system prompt during the gap; cost is repriced
            # cold-equivalent, but a physically warm follow-up's latency is not.
            record['physically_warm'] = bool(first_read)
            self.record['gap_seconds'] = round(started_unix - self.sessions[-1]['ended_unix'], 3)
        self.sessions.append(record)

    def save(self, phase):
        rows = read_rows(self.log)
        last = self.sessions[-1] if self.sessions else {}
        if self.router:
            decisions, stderr = self.dir/'decisions.json', self.dir/'jev.stderr.txt'
            routing = bench_jev.routing(json.loads(decisions.read_text()) if decisions.exists() else [],
                                        stderr.read_text(errors='replace') if stderr.exists() else '', rows, self.sessions)
            if self.arm.get('models'):
                model_eligibility(routing, self.arm['models'], self.record.get('catalog'))
            self.record['routing'] = routing
            self.router_unavailable = routing['auth_failure']
        self.record.update(
            phase=phase, sessions=self.sessions,
            status='timeout' if any(s['status'] == 'timeout' for s in self.sessions) else 'completed',
            wall_seconds=round(sum(s['wall_seconds'] for s in self.sessions), 3),
            client={'subtype': self.final.get('subtype'), 'is_error': self.final.get('is_error'),
                    'num_turns': self.final.get('num_turns'), 'stop': last.get('stop')},
            accounting=self.adapter.accounting(rows, self.final) if self.adapter else
            subscription_accounting(rows, self.final, self.rates, self.arm.get('prompt_cache_ttl'))
            if self.auth == 'subscription' else
            accounting(rows, self.final, jev=bool(self.router)),
            cache=bench_report.cache_attribution(bench_report.priced_rows(self.record, rows, self.rates), self.rates),
            path=bench_report.setting_path(rows),
            cost_components=bench_report.cost_components(bench_report.priced_rows(self.record, rows, self.rates), self.rates),
            write_sources=bench_report.write_sources(bench_report.priced_rows(self.record, rows, self.rates), self.rates))
        if self.arm['kind'] == 'fixed' and self.arm.get('effort'):
            self.record['effort_check'] = effort_check(rows, self.arm['effort'])
        if self.appended:
            self.record['prompt_check'] = prompt_check(rows, self.appended['sha256'])
        if self.arm.get('prompt_cache_ttl'):
            self.record['ttl_check'] = ttl_check(rows, self.arm['prompt_cache_ttl'])
        if self.auth == 'subscription':
            self.record['as_sent'] = {'cache': bench_report.cache_attribution(rows, self.rates),
                                      'cost_components': bench_report.cost_components(rows, self.rates)}
        if self.adapter and (self.dir/'tmp').exists():
            self.record['routing'] = self.adapter.evidence(self.dir)
            # The ModelPilot arm's advisor uses the same TypeSafe key as Jev's router: a rejected key stops the run
            # (jev_router_unavailable) rather than letting trials run on their fallback without advice.
            advisor = self.record['routing'].get('advisor') or {}
            self.router_unavailable = self.router_unavailable or bool(advisor.get('auth_failures'))
        (self.dir/'trial.json').write_text(json.dumps(self.record, indent=2) + '\n')

    def close(self):
        """Stop the trial's router and proxy (idempotent). Afterwards every request is logged."""
        if self.router:
            self.router.stop()  # first, so nothing new reaches the proxy
            stderr = self.dir/'jev.stderr.txt'
            if stderr.exists():
                stderr.write_text(redact(redact(stderr.read_text(errors='replace'), self.api_key), self.jev_key))
        if self.proxy:
            self.proxy.shutdown()
            self.proxy_thread.join()
            # Handler threads are daemons that server_close() does not join: without this wait a
            # request still in flight (billed upstream) could lose its row when the log closes.
            if not self.proxy.wait_idle(IDLE_SECONDS):
                self.record['proxy_busy_at_close'] = True
            self.proxy.server_close()
            self.proxy = None

    def finish(self, stopped=None):
        """Grade the tree as the sessions left it. stopped: the run ended before a due follow-up."""
        if stopped:
            self.record['stopped'] = stopped
        self.close()
        self.save('grading')
        self.record.update(grade_checkout(self.task, self.work, self.base_commit, self.repo, self.python, self.dir,
                                          self.expected))
        reap(self.dir)
        # Keep the diff and records; drop copies that only cost disk.
        for name in ('workspace', 'grade', 'edge-env', 'home', 'tmp'):
            shutil.rmtree(self.dir/name, ignore_errors=True)
        # A trial the account couldn't pay for is not a model result: counted as incomplete, never as a failure.
        self.record['complete'] = stopped is None and not self.account_error and not self.rate_limited
        if self.account_error:
            self.record['excluded_reason'] = 'anthropic_account_error'
        elif self.rate_limited:  # a usage or rate limit cut the session short: not a model result either
            self.record['excluded_reason'] = 'rate_limited'
        self.finished = True
        self.save('graded')
        return self.record


def checkout_diff(work, base_commit):
    """The agent's change against the task's base commit, new files included (the agent may commit, so never HEAD)."""
    subprocess.run(['git', 'add', '-A', '-N'], cwd=work, capture_output=True)  # include new files in the diff
    return subprocess.run(['git', 'diff', '--binary', base_commit], cwd=work, capture_output=True).stdout


def grade_checkout(task, work, base_commit, repo, python, out, expected=None, name='agent.diff'):
    """Grade one task's checkout as the agent left it: its diff (written to out/name), the hidden grader and the
    task's edge suite where it has one. Scratch trees go under out/grade and out/edge-env. Returns the record fields."""
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=work, capture_output=True, text=True).stdout.strip()
    (Path(out)/name).write_bytes(checkout_diff(work, base_commit))
    changed = subprocess.run(['git', 'diff', '--name-only', base_commit], cwd=work, capture_output=True,
                             text=True).stdout.splitlines()
    fields = {'agent_moved_head': head != base_commit,
              'test_config_changed': test_config_changes(changed, task['test_dir'])}
    graded = bench_tasks.grade(task, work, repo, python, Path(out)/'grade', expected_hidden_passed=expected)
    edge_passed = None
    if regrade.edge_files(task['id'], EDGE_ROOT):
        edge = regrade.run_edge(task, Path(out)/'grade'/'graded', python, Path(out)/'edge-env', EDGE_ROOT)
        fields['edge'] = edge
        edge_passed = regrade.edge_all_passed(edge)
    fields['grade'] = {'passed': graded['passed'], 'reason': graded['reason'], 'edge_passed': edge_passed,
                       'failing_tests': sorted(set(graded['hidden']['failing_tests'] + graded['suite']['failing_tests'])),
                       'hidden_exit': graded['hidden']['exit_code'], 'suite_exit': graded['suite']['exit_code'],
                       'hidden_passed': graded['hidden']['tests_passed'],
                       'hidden_skipped': graded['hidden']['tests_skipped']}
    fields['passed'] = graded['passed'] and edge_passed is not False
    fields['pass_rule'] = 'hidden' if edge_passed is None else 'hidden_and_edge'
    return fields


def run_trial(task, arm_id, trial_dir, cli, key, upstream, price_table, *, sleep=time.sleep, **options):
    """Run one trial start to finish, waiting out the follow-up gap."""
    trial = Trial(task, arm_id, trial_dir, cli, key, upstream, price_table, **options)
    try:
        while trial.step():
            sleep(trial.next_gap())
    finally:
        trial.close()
    return trial.record


def interleave(trials, gap, *, clock=time.monotonic, sleep=time.sleep, stop=lambda: None):
    """Run trials' sessions one at a time: new trials in the given order, and a parked trial
    as soon as gap seconds have passed since its last session ended. Sleeps only when nothing
    else can run. Returns stop()'s reason if it gave one before a session, else None."""
    pending, parked, sequence = list(trials), [], 0
    while pending or parked:
        parked.sort(key=lambda p: p[:2])
        now = clock()
        if parked and parked[0][0] <= now:
            source = parked
        elif pending:
            source = pending
        else:
            sleep(parked[0][0] - now)
            continue
        reason = stop()
        if reason:
            return reason
        trial = parked.pop(0)[2] if source is parked else pending.pop(0)
        if trial.step():
            wait = trial.next_gap() if hasattr(trial, 'next_gap') else None  # a sequence's gaps differ by turn
            parked.append((clock() + (gap if wait is None else wait), sequence, trial))
            sequence += 1
    return None


class LazyTrial:
    """Creates its trial on first use, so a stopped run never touches trials it did not start."""
    def __init__(self, make, executed):
        self.make, self.executed, self.trial = make, executed, None

    def step(self):
        self.trial = self.trial or self.make()
        more = self.trial.step()
        record = self.trial.record or {}
        self.executed.append([record.get('task'), record.get('arm'), record.get('trial'), len(record.get('sessions') or [])])
        if not more:
            print(record.get('task'), record.get('arm'), record.get('trial'), 'PASS' if record.get('passed') else 'FAIL',
                  (record.get('cache') or {}).get('cold_equivalent_cost_usd'), flush=True)
        return more

    def next_gap(self):
        wait = getattr(self.trial, 'next_gap', None)
        return wait() if wait else None


def preflight(tasks, python, scratch):
    """Grade each task's reference once in this environment ($0) before any billable request.

    Returns each task's hidden tests passed, the bar every trial must meet. A reference that
    fails (a changed environment, a flaky task) stops the run, and so does an edge suite the
    reference doesn't pass in full: trials are graded on it.
    """
    expected, failed = {}, []
    for task in tasks:
        base = Path(scratch)/task['id']
        repo = task_repo(task)
        tree = bench_tasks.task_tree(task, repo, task['reference'], base/'reference')
        graded = bench_tasks.grade(task, tree, repo, python, base/'grade')
        edge = (regrade.run_edge(task, base/'grade'/'graded', python, base/'edge-env', EDGE_ROOT)
                if regrade.edge_files(task['id'], EDGE_ROOT) else None)
        shutil.rmtree(base, ignore_errors=True)
        if edge is not None and not regrade.edge_all_passed(edge):
            failed.append(f"{task['id']} (edge suite: the reference passes {edge['tests_passed']}/{edge['tests_run']})")
        elif graded['passed']:
            expected[task['id']] = {'hidden_passed': graded['hidden']['tests_passed'],
                                    'edge_tests': edge['tests_run'] if edge else None,
                                    'seconds': round(graded['hidden']['seconds'] + graded['suite']['seconds'], 3)}
        else:
            tests = sorted(set(graded['hidden']['failing_tests'] + graded['suite']['failing_tests']))
            failed.append(f"{task['id']} ({graded['reason']}{': ' + ', '.join(tests) if tests else ''})")
    if failed:
        raise PreflightError('Reference solutions fail their graders here: ' + '; '.join(failed) + '. No requests sent.')
    return expected


def edge_manifest(tasks):
    """Each graded edge suite's files and their SHA-256, for the manifest; None when no task has one."""
    return {t['id']: {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in regrade.edge_files(t['id'], EDGE_ROOT)}
            for t in tasks if regrade.edge_files(t['id'], EDGE_ROOT)} or None


def jev_manifest(arms):
    """What a run's Jev arms ran, for the manifest; None without Jev arms."""
    jev = {a: bench_jev.JevRouter(ARMS[a]).describe() for a in arms if ARMS[a]['kind'] == 'jev'}
    return {'arms': jev, 'router_cost': 'unpriced (TypeSafe); Jev dollars are provider cost only, a lower bound',
            'stop_threshold_basis': "--max-budget-usd is priced by Claude Code at its own rate for the jev-router "
                                    'sentinel, not the wire cost of the served model',
            'add_dir': 'the Jev checkout, as the stock launcher passes it'} if jev else None


def modelpilot_manifest(arms, budget_usd, sessions):
    """What each ModelPilot arm ran, for the manifest; None without one."""
    from .active_policy import parameters
    return {a: {'mode': 'active (user-approved September 26, benchmark arm only)',
                'parameters': parameters(ARMS[a]['model'], ARMS[a]['effort'], overrides=ARMS[a].get('policy_overrides')),
                'policy_sha256': hashlib.sha256((ROOT/ARMS[a]['policy']).read_bytes()).hexdigest(),
                'r5_tools': arm_tools(a), 'channel_declared': arm_tools(a),  # both only while delegation is on
                'governor_limit_usd': budget_usd * sessions,
                'limit_basis': 'wire cost, admitted while measured spend is below the limit (the client budget-stop '
                               'rule); the client threshold also applies, priced by Claude Code as the start model it '
                               'asked for'}
            for a in arms if ARMS[a]['kind'] == 'modelpilot'} or None


def run_bench(tasks, arms, trials, seed, out, cli, key, upstream, price_table, *, client_version=None, shape='single',
              gap=0, run_budget=None, expected=None, jev_key=None, trial_factory=None, clock=time.monotonic,
              sleep=time.sleep, subscription_arms=(), oauth_token=None, compact='none', autocompact=None, **limits):
    """subscription_arms: fixed arms whose client logs in with a Claude subscription token (oauth_token) instead of
    the API key. Their tokens are not billed to the API account, so the run budget counts only API-key trials.
    shape 'sequence' (long_session): tasks are sequences ({'id', 'tasks'}), each run as one session of task turns,
    gap seconds apart, with compaction turns as compact says and the client's autocompact window."""
    from . import long_session
    sequences = tasks if shape == 'sequence' else None
    if sequences:
        tasks = [t for unit in sequences for t in unit['tasks']]
        unsupported = [a for a in arms if a in long_session.UNSUPPORTED_ARMS]
        if unsupported:
            raise ValueError(f'Not supported in sequences: {unsupported}')
    subscription_arms = tuple(subscription_arms)
    wrong = [a for a in subscription_arms if a not in arms or ARMS[a]['kind'] not in SUBSCRIPTION_KINDS]
    if wrong or (subscription_arms and not oauth_token):
        raise ValueError(f'Subscription arms must be fixed arms of this run, with a token: {wrong or subscription_arms}')
    out = Path(out)
    out.mkdir(mode=0o700, parents=True)
    order = schedule(sequences or tasks, arms, trials, seed)
    by_id = {t['id']: t for t in sequences or tasks}
    if client_version is None:
        cli, client_version = resolve_client(cli)
    gap = gap if shape in ('followup', 'sequence') else 0
    steps = len(long_session.plan(max(len(u['tasks']) for u in sequences), gap, compact)) if sequences else None
    sessions = steps or (2 if shape == 'followup' else 1)  # the most any trial runs
    manifest = {'seed': seed, 'order': order, 'arms': {a: ARMS[a] for a in arms}, 'trials': trials,
                'tasks': {t['id']: bench_tasks.spec_hash(t) for t in tasks}, 'client': str(cli),
                'client_version': client_version, 'code': code_revision(),
                'python': python_version(limits.get('python') or bench_tasks.interpreter()), 'limits': limits,
                'preamble': PREAMBLE, 'tools': TOOLS, 'shape': shape, 'gap_seconds': gap,
                'appended_prompts': {a: {'path': ARMS[a]['append_system_prompt'], 'text': p['text'], 'sha256': p['sha256']}
                                     for a in arms for p in [appended_prompt(ARMS[a])] if p} or None,
                'follow_up_prompt': FOLLOW_UP if shape == 'followup' else None, 'run_budget_usd': run_budget,
                'reference_preflight': expected, 'cost_basis': 'cold-equivalent; measured alongside',
                'pass_rule': PASS_RULE, 'edge_suites': edge_manifest(tasks),
                'jev': jev_manifest(arms),
                'auth': {a: 'subscription' if a in subscription_arms else 'api_key' for a in arms},
                'subscription_note': ('Subscription arms log in with a claude setup-token token. The client asks for 1h cache '
                                      'writes then (5m with an API key); dollars are API-key equivalent (1h writes at the 5m '
                                      'rate), as-sent alongside, and do not count toward the run budget.')
                if subscription_arms else None,
                'modelpilot': modelpilot_manifest(arms, limits.get('budget_usd', 1.0), sessions),
                'sequences': {u['id']: [t['id'] for t in u['tasks']] for u in sequences} if sequences else None,
                'sequence_preamble': long_session.PREAMBLE if sequences else None,
                'compact': compact if sequences else None, 'autocompact': autocompact if sequences else None,
                'prompt_cache_ttl': {a: ARMS[a]['prompt_cache_ttl'] for a in arms if ARMS[a].get('prompt_cache_ttl')} or None,
                'client_unpriced_arms': {a: ARMS[a]['model'] for a in arms if ARMS[a]['model'] in CLIENT_UNPRICED} or None,
                'note': 'Stop thresholds are not billing caps: per session, and the run threshold between sessions. No retries.'
                        + (' The client prices client_unpriced_arms\' models at its unknown-model rate (Opus 5.5\'s), so '
                           'their per-session stop falls at a fraction of the same dollars of real spend.'
                           if any(ARMS[a]['model'] in CLIENT_UNPRICED for a in arms) else '')}
    with (out/'manifest.json').open('x') as f:
        json.dump(manifest, f, indent=2)

    def default_factory(task, arm, trial_dir, n):
        subscription = arm in subscription_arms
        if sequences:
            count = len(long_session.plan(len(task['tasks']), gap, compact))
            return long_session.SequenceTrial(
                task, arm, trial_dir, cli, key, upstream, price_table, trial=n, gap=gap, compact=compact,
                autocompact=autocompact, client_version=client_version, jev_key=jev_key,
                adapter=arm_adapter(arm, limits.get('budget_usd', 1.0), count, jev_key),
                auth='subscription' if subscription else 'api_key', oauth_token=oauth_token if subscription else None,
                expected=expected, **limits)
        adapter = arm_adapter(arm, limits.get('budget_usd', 1.0), 2 if shape == 'followup' else 1, jev_key)
        return Trial(task, arm, trial_dir, cli, key, upstream, price_table, trial=n, shape=shape, gap=gap,
                     client_version=client_version, jev_key=jev_key, adapter=adapter,
                     auth='subscription' if subscription else 'api_key', oauth_token=oauth_token if subscription else None,
                     expected_hidden_passed=((expected or {}).get(task['id']) or {}).get('hidden_passed'), **limits)
    factory = trial_factory or default_factory
    executed = []
    lazy = [LazyTrial(lambda t=t, a=a, n=n: factory(by_id[t], a, out/t/a/str(n), n), executed) for t, a, n in order]

    def spend(billing='api_key'):
        return sum(item.trial.known_cost() for item in lazy
                   if item.trial and getattr(item.trial, 'auth', 'api_key') == billing)

    def stop():
        # A rejected TypeSafe key would make every later Jev trial fail open onto Opus.
        if any(item.trial.router_unavailable for item in lazy if item.trial):
            return 'jev_router_unavailable'
        # An Anthropic account that can't pay or authenticate fails every later trial the same way.
        if any(item.trial.account_error for item in lazy if item.trial):
            return 'anthropic_account_error'
        # A usage or rate limit would cut every later session short the same way; no retries.
        if any(getattr(item.trial, 'rate_limited', False) for item in lazy if item.trial):
            return 'rate_limited'
        return 'run_budget' if run_budget is not None and spend() >= run_budget else None
    reason = error = None
    try:
        reason = interleave(lazy, gap, clock=clock, sleep=sleep, stop=stop)
        for item in lazy:  # started trials whose follow-up the stop cut off
            if reason and item.trial and item.trial.started and not item.trial.finished:
                item.trial.finish(stopped=reason)
    except BaseException as e:
        error = type(e).__name__
        raise
    finally:
        for item in lazy:  # a crash must not leave a trial's proxy or router running
            if item.trial and not item.trial.finished:
                item.trial.close()
        records = [item.trial.record for item in lazy if item.trial and item.trial.finished]
        summary = bench_report.summarize(records, arms, seed=seed)
        summary.update(trials=len(records), complete=reason is None and error is None, stopped=reason, error=error,
                       known_spend_usd=spend(), executed=executed,
                       subscription_as_sent_usd=spend('subscription') if subscription_arms else None,
                       unknown_cost_trials=[f"{r['task']}/{r['arm']}/{r['trial']}" for r in records
                                            if (r.get('accounting') or {}).get('cost_usd') is None])
        (out/'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tasks', help='Comma-separated task IDs from bench/tasks')
    parser.add_argument('--sequences', help='Sequence shape: comma-separated IDs from bench/sequences.json, or "all"')
    parser.add_argument('--arms', required=True, help='Comma-separated arms: ' + ', '.join(ARMS))
    parser.add_argument('--trials', type=int, default=1)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--max-turns', type=int, default=30)
    parser.add_argument('--budget', type=float, default=1.0, help='Per-session client stop threshold, not a billing cap')
    parser.add_argument('--shape', choices=SHAPES + ('sequence',), default='single')
    parser.add_argument('--gap', type=float, default=0,
                        help='Seconds before the follow-up (followup shape: 0 warm, 330 cold), or between task turns '
                             '(sequence shape)')
    parser.add_argument('--compact', choices=('none', 'warm', 'cold'), default='none',
                        help="Sequence shape: a '/compact' turn after each task but the last, before the gap (warm) or "
                             'after it (cold)')
    parser.add_argument('--autocompact', type=int, help="Sequence shape: the client's auto-compaction window in tokens "
                                                        '(100000 to 1000000)')
    parser.add_argument('--run-budget', type=float, help='Stop starting sessions once known spend reaches this (required with --live)')
    parser.add_argument('--claude', type=Path)
    parser.add_argument('--live', action='store_true', help='Required to send billable requests')
    parser.add_argument('--final', action='store_true', help='Allow final-split tasks (only for the frozen evaluation)')
    parser.add_argument('--subscription-arms', default='',
                        help='Comma-separated fixed arms that run on a Claude subscription (a claude setup-token token, '
                             'asked for at a hidden prompt) instead of the API key; dollars are reported API-key equivalent')
    args = parser.parse_args()
    from . import long_session
    units = None
    if args.shape == 'sequence':
        if args.tasks or not args.sequences:
            raise SystemExit('--shape sequence takes --sequences, not --tasks.')
        try:
            units = long_session.load(None if args.sequences == 'all' else args.sequences.split(','))
        except ValueError as e:
            raise SystemExit(str(e))
        tasks = [t for u in units for t in u['tasks']]
    else:
        if args.sequences or not args.tasks or args.compact != 'none' or args.autocompact is not None:
            raise SystemExit('--tasks is required; --sequences, --compact and --autocompact need --shape sequence.')
        tasks = [t for i in args.tasks.split(',') for t in bench_tasks.load(i)]
        if len(tasks) != len(args.tasks.split(',')):
            raise SystemExit('Unknown task.')
    arms = args.arms.split(',')
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        raise SystemExit(f'Unknown arm. Arms: {", ".join(ARMS)}')
    if units and any(a in long_session.UNSUPPORTED_ARMS for a in arms):
        raise SystemExit(f'Not supported in sequences: {", ".join(long_session.UNSUPPORTED_ARMS)}.')
    if args.autocompact is not None and not 100_000 <= args.autocompact <= 1_000_000:
        raise SystemExit('--autocompact takes 100000 to 1000000 tokens.')
    if args.gap and args.shape == 'single':
        raise SystemExit('--gap applies only to --shape followup or sequence.')
    subscription_arms = [a for a in args.subscription_arms.split(',') if a]
    wrong = [a for a in subscription_arms if a not in arms or ARMS[a]['kind'] not in SUBSCRIPTION_KINDS]
    if wrong:
        raise SystemExit(f'--subscription-arms takes fixed arms of this run only: {", ".join(wrong)}.')
    api_arms = [a for a in arms if a not in subscription_arms]
    if bench_tasks.SPLITS.exists():
        final = [t['id'] for t in tasks if bench_tasks.split_of(t['id']) == 'final']
        if final and not args.final:
            raise SystemExit(f'Final-split tasks {final} need --final: they are reserved for the frozen evaluation.')
        changed = bench_tasks.check_lock(bench_tasks.load())
        if final and changed:
            raise SystemExit(f'Final task specs changed since the lock: {changed}. Not running.')
    blocked = [a for a in arms if ARMS[a]['kind'] not in RUNNABLE + ('modelpilot',)]
    if blocked:
        raise SystemExit(f'Not runnable yet: {", ".join(blocked)}.')
    jev_arms = [a for a in arms if ARMS[a]['kind'] == 'jev']
    table = rates()
    served = sorted({m for a in arms for m in (ARMS[a].get('models', bench_jev.JEV_MODELS) if a in jev_arms
                                                 else ARMS[a].get('served_models', [ARMS[a]['model']]))})
    missing = [m for m in served if m not in table]
    if missing:
        raise SystemExit(f'No configured rates for {missing}.')
    advised = [a for a in arms if ARMS[a].get('advisor') == 'jev']
    if (jev_arms or advised) and not shutil.which('node'):
        raise SystemExit('Jev arms and the ModelPilot arm (Jev as advisor) need Node.')
    problems = [p for p in (bench_jev.JevRouter(ARMS[a]).problem() for a in jev_arms) if p]
    problems += [p for p in (bench_jev.JevRouter(dict(ARMS[a], variant='compat', patch=PATCH)).problem() for a in advised) if p]
    if problems:
        raise SystemExit(' '.join(problems))
    if args.live and args.run_budget is None:
        raise SystemExit('--live needs --run-budget: known spend at which no further session starts.')
    python = bench_tasks.interpreter()
    print(f'Preflight: grading {len(tasks)} reference solution(s) with {python} ($0)...', flush=True)
    with tempfile.TemporaryDirectory(prefix='bench-preflight-') as scratch:
        try:
            expected = preflight(tasks, python, scratch)
        except PreflightError as e:
            raise SystemExit(str(e))
    print('Preflight passed: ' + ', '.join(f"{i} ({e['hidden_passed']} hidden)" for i, e in expected.items()), flush=True)
    runs = len(units or tasks) * len(arms) * args.trials
    plans = [long_session.plan(len(u['tasks']), args.gap, args.compact) for u in units] if units else None
    sessions = max(len(p) for p in plans) if plans else 2 if args.shape == 'followup' else 1
    session_total = sum(len(p) for p in plans) if plans else len(tasks) * sessions  # per arm and trial
    try:
        planned = client_problem(resolve_client(args.claude or shutil.which('claude') or 'claude')[1])
    except OSError:
        planned = 'No Claude Code client found.'
    if not args.live:
        if planned:
            print('Client: ' + planned + ' A live run would stop here.')
        shape = (f'follow-up after {args.gap:g} s' if args.shape == 'followup' else
                 f'sequences of {min(len(u["tasks"]) for u in units)}-{max(len(u["tasks"]) for u in units)} task turns '
                 f'{args.gap:g} s apart, compaction {args.compact}'
                 + (f', autocompact window {args.autocompact}' if args.autocompact else '') if units else 'single prompt')
        ceiling = session_total * len(api_arms) * args.trials * args.budget  # API-billed sessions only
        if args.run_budget is None:
            run_stop, worst = 'No run stop threshold yet (--live needs --run-budget).', ceiling
        else:
            # No new session starts at the run threshold, but the one running can still add its own.
            run_stop = f'Run stop threshold ${args.run_budget:.2f}: no session starts once known spend reaches it.'
            worst = min(ceiling, args.run_budget + args.budget)
        print(f'Prepared {runs} trials ({len(units or tasks)} {"sequences" if units else "tasks"} × {len(arms)} arms × '
              f'{args.trials}), {shape}, seed {args.seed}, '
              f'max {args.max_turns} turns and ${args.budget:.2f} stop threshold per session. {run_stop} Up to about '
              f'${worst:.2f} of API spend if sessions reach their thresholds. Thresholds are not billing caps. No retries. Add --live.')
        if jev_arms:
            print(f'Jev arms {", ".join(jev_arms)}: checkouts verified. TypeSafe routing is billed separately and unpriced, '
                  'so their dollars are a lower bound; the client stop threshold is priced on the jev-router sentinel. '
                  'Stock Jev is expected not to route on this client (run as Opus plus router overhead). '
                  '--live asks for a TypeSafe key; consider python3 -m modelpilot.jev_check --live first.')
        if subscription_arms:
            print(f'Subscription arms {", ".join(subscription_arms)}: the client logs in with a claude setup-token token '
                  '(hidden prompt with --live) and asks for 1h cache writes; their dollars are reported API-key '
                  'equivalent (1h writes at the 5m rate), as-sent alongside, and do not count toward the run budget. '
                  'A usage or rate limit (HTTP 429) excludes the trial and stops the run.')
        for arm in (a for a in arms if ARMS[a]['kind'] == 'modelpilot' and ARMS[a].get('advisor') != 'jev'):
            print(f'ModelPilot exploration arm {arm}: low concise with no advisor; each trial\'s draw from its task, arm '
                  'and trial number decides whether it switches to Opus 5.5 at the turn\'s effort and at which main-loop '
                  'request (configs/modelpilot-policy.json, exploration). Measurement only; no TypeSafe key needed.')
        for arm in (a for a in arms if ARMS[a]['kind'] == 'modelpilot' and ARMS[a].get('advisor') == 'jev'):
            print(f'ModelPilot arm {arm}: active policy (benchmark arm only). Jev (compat checkout, advice only) predicts '
                  'the model and effort at each turn start and on stuck evidence; ModelPilot jumps straight there when '
                  'the expected total cost says it pays, else stays (configs/modelpilot-policy.json). Stuck with nothing '
                  f'stronger, the task stops; any request is refused once wire spend reaches ${args.budget * sessions:.2f} '
                  'per trial or cost is unknown. TypeSafe advice is billed separately and unpriced, so its dollars are a '
                  'lower bound; --live asks for a TypeSafe key.')
            floor = (ARMS[arm].get('policy_overrides') or {}).get('quality_floor') or {}
            if floor.get('enabled'):
                print(f'  Quality floor: only settings no worse than {floor["baseline"]} on the measured tuning tasks, '
                      'or above the current one; a start below it moves to the cheapest allowed setting without Jev.')
        return
    cli, version = resolve_client(args.claude or shutil.which('claude') or 'claude')
    if client_problem(version):
        raise SystemExit(client_problem(version) + ' No billable requests sent.')
    try:
        bench_tasks.lock_benchmark_environment(python)
    except (OSError, ValueError) as e:
        raise SystemExit(f'Cannot lock benchmark dependencies: {e}')
    key = None
    if api_arms:
        key = os.environ.get('ANTHROPIC_API_KEY') or getpass.getpass('Anthropic API key (hidden): ').strip()
        problem = check_anthropic_key(key)
        if problem:
            raise SystemExit(problem + ' No billable requests sent.')
    oauth_token = None
    if subscription_arms:
        oauth_token = (os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') or
                       getpass.getpass('Claude subscription token from `claude setup-token` (hidden): ').strip())
        if not oauth_token or any(c.isspace() for c in oauth_token):
            raise SystemExit('Missing or malformed subscription token. No requests sent.')
    jev_key = None
    if jev_arms or advised:
        jev_key = (os.environ.get('JEV_API_KEY') or os.environ.get('TYPESAFE_API_KEY')
                   or getpass.getpass('TypeSafe/Jev API key (hidden): ').strip())
        if not jev_key or any(c.isspace() for c in jev_key):
            raise SystemExit('Missing or malformed TypeSafe key. No billable requests sent.')
    out = ROOT/'runs'/('bench-' + time.strftime('%Y%m%d-%H%M%S'))
    print(f'Running {runs} billable trials with {cli} ({version}); stop at ${args.run_budget:.2f} known spend; '
          f'results: {out}', flush=True)
    summary = run_bench(units or tasks, arms, args.trials, args.seed, out, cli, key, 'https://api.anthropic.com', table,
                        client_version=version, shape=args.shape, gap=args.gap, run_budget=args.run_budget,
                        compact=args.compact, autocompact=args.autocompact,
                        expected=expected, jev_key=jev_key, max_turns=args.max_turns, budget_usd=args.budget,
                        subscription_arms=subscription_arms, oauth_token=oauth_token)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
