"""Thinking history across setting changes, direct API. Planning is free; --live prompts for a local key.

Real Claude Code requests carry `thinking`, and Sonnet / Opus replies carry thinking blocks, so
the policy's ladder rewrites requests whose history holds another setting's thinking. This probe
sends exactly what `policy_actions.transform_request` would forward and records whether the API
accepts it, what the cache does and whether the target thinks. Blocks are always passed back
unchanged (the API reference warns that stripping them can fail). `capture-shape` records, at $0,
which thinking/context/beta fields the pinned client sends, so the probe can mirror them.
"""
import argparse
import copy
import getpass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import uuid
from . import cache_probe as probe
from .cache_replication import HAIKU_5_5 as H, OPUS_5_5, RATES, SONNET_5_5, SOURCE, Budget
from .policy_actions import (MODELS as POLICY_MODELS, PLACEMENTS, effort_anchor, effort_message, transform_request,
                             with_effort_messages)
from .switch_policy import content_positions

ROOT = Path(__file__).resolve().parents[1]
SHAPE_FIXTURE = ROOT/'tests/fixtures/claude-2.1.284-shape.json'
FAKE_KEY = 'sk-ant-offline-fixture-not-a-key'
SUITES = ('smoke', 'transitions', 'top-rung', 'opus-5-5-effort', 'sonnet-5-5', 'haiku-5-5', 'returns',
          'per-message-effort', 'haiku-effort')
SHAPES = ('tool_continuation', 'new_turn')
PREFIX_LINES, SEED_MAX_TOKENS, SWITCH_MAX_TOKENS = 260, 4096, 2048
PUZZLE = ('Find the smallest positive integer n that leaves remainder 3 when divided by 7, remainder 4 when '
          'divided by 11 and remainder 5 when divided by 13. Work it out carefully, then ')
PROMPTS = {'tool_continuation': PUZZLE + 'call record_answer with n. Do not state the answer in text.',
           'new_turn': PUZZLE + 'reply with only the number. Do not use any tool.'}
FOLLOW_UP = 'Now add 17 to that number. Reply with only the result.'
TOOL_RESULT = 'Recorded.'
SYSTEM_NOTE = 'Environment note: this conversation is a ModelPilot measurement.'
TOOL = {'name': 'record_answer', 'description': 'Record the final integer answer.',
        'input_schema': {'type': 'object', 'properties': {'value': {'type': 'integer'}}, 'required': ['value']}}
THINKING_TYPES = ('thinking', 'redacted_thinking')
# case: (source setting, target setting). Controls continue on the same setting; they check that the
# request shape itself is accepted, so a rejected transition can be attributed to the change.
# "sonnet", "opus" and "haiku" are the policy's tiers: Opus 5.5 since September 26 (the first transitions run used
# Opus 5), Sonnet 5.5 since September 28 (every run before then used Sonnet 5) and Haiku 5.5 since October 8 (every
# run before then planned Haiku 4.5, whose targets the transform refused for mid-history system messages).
S, TOP = SONNET_5_5, OPUS_5_5
CONTROLS = {'control/sonnet': ((S, 'medium'), (S, 'medium')), 'control/opus': ((TOP, 'medium'), (TOP, 'medium'))}
# Planned only in suites with a case that ends on Haiku.
HAIKU_CONTROL = {'control/haiku': ((H, 'medium'), (H, 'medium'))}
# The ladder's rungs, correction resets to the client's setting and R3 downgrades.
TRANSITIONS = {'effort_up/sonnet': ((S, 'medium'), (S, 'high')), 'effort_down/sonnet': ((S, 'high'), (S, 'medium')),
               'model_up': ((S, 'high'), (TOP, 'medium')), 'model_down': ((TOP, 'medium'), (S, 'medium')),
               'effort_up/opus': ((TOP, 'medium'), (TOP, 'high')),
               'to_haiku/sonnet': ((S, 'medium'), (H, 'medium')), 'to_haiku/opus': ((TOP, 'medium'), (H, 'medium')),
               'from_haiku': ((H, 'medium'), (S, 'medium')), 'effort_up/haiku': ((H, 'medium'), (H, 'high'))}
TOP_RUNG = ('model_up', 'model_down', 'effort_up/opus')
# What the Sonnet 5.5 ladder needs: its effort rung, its model rung and the Opus 5.5 correction reset (Opus 5.5
# effort is verified). The API docs say no other model reads Sonnet 5.5's thinking blocks.
SONNET_5_5_CASES = ('effort_up/sonnet', 'effort_down/sonnet', 'model_up', 'model_down')
# What a Haiku 5.5 tier needs before it can be a candidate: the move down from Sonnet 5.5, the move back up (a correction
# reset or an escalation) and its own effort rung. Unlike Haiku 4.5 it takes Claude Code's mid-history system messages.
HAIKU_5_5_CASES = ('to_haiku/sonnet', 'from_haiku', 'effort_up/haiku')
SUITE_TRANSITIONS = {'transitions': tuple(TRANSITIONS), 'top-rung': TOP_RUNG, 'sonnet-5-5': SONNET_5_5_CASES,
                     'haiku-5-5': HAIKU_5_5_CASES}
# Opus 5.5 is not a policy tier: this answers the replication's open effort/cache question with real thinking.
O55_CASES = {'o55/control_high': ((OPUS_5_5, 'high'), (OPUS_5_5, 'high')),
             'o55/high_to_low': ((OPUS_5_5, 'high'), (OPUS_5_5, 'low')),
             'o55/low_to_high': ((OPUS_5_5, 'low'), (OPUS_5_5, 'high'))}
CONTROL_CASES = set(CONTROLS) | set(HAIKU_CONTROL) | {'o55/control_high'}
# Returns to a warm setting (the policy's return_reuse): seed at home, `away` requests at another setting, then one
# request back home. The API's breakpoints look back at most 20 content positions for an earlier entry, and a
# Claude Code step adds about four, so a return reaches home's entry only if it comes soon, or if a breakpoint is
# put back on home's last cached block (anchored). case: (home, away setting, away requests, anchored)
RETURN_CASES = {'return/effort_near': ((S, 'medium'), (S, 'high'), 1, False),
                'return/effort_far': ((S, 'medium'), (S, 'high'), 6, False),
                'return/effort_far_anchored': ((S, 'medium'), (S, 'high'), 6, True),
                'return/model_near': ((S, 'medium'), (TOP, 'medium'), 1, False)}
NEXT_STEP = 'Recorded. Next, call record_answer with the previous value plus 17.'
CONTINUE = 'Call record_answer with the previous value plus 17.'
LOOKBACK_POSITIONS = 20
# Per-message effort (beta; Claude Code 2.1.284 already sends its older header spelling per-turn-control-2026-07-01): an
# effort-only system message changes effort "from the next user turn" without invalidating the cache, where a top-level
# change rewrites the messages. Claude Code 2.1.284 itself carries its --effort value in the system note after the prompt
# (output_config.effort), and these seeds do too, so each case sees the client's own effort message. Each group seeds at
# home, runs step 1 there (the baseline), changes effort at step 2 and continues at step 3; every step is a fresh puzzle so
# thinking can respond. Modes: control (no change; native_* run the client itself at that effort, the references), top
# (the top-level effort only, as the proxy does today), pm (an effort message at step 2), pm_start (an effort message
# from the seed on, as at a turn start). Placement: 'before_result' before the newest user message (between the tool call
# and its result, if the API allows it inside a tool loop), 'after_result' right after it; always after the client's
# own effort message. case: (home, mode, target effort, placement)
EFFORT_CASES = {'effort/control': ((S, 'medium'), 'control', 'medium', None),
                'effort/native_low': ((S, 'low'), 'control', 'low', None),
                'effort/native_xhigh': ((S, 'xhigh'), 'control', 'xhigh', None),
                'effort/top_xhigh': ((S, 'medium'), 'top', 'xhigh', None),
                'effort/pm_low': ((S, 'medium'), 'pm', 'low', 'before_result'),
                'effort/pm_high': ((S, 'medium'), 'pm', 'high', 'before_result'),
                'effort/pm_xhigh': ((S, 'medium'), 'pm', 'xhigh', 'before_result'),
                'effort/pm_xhigh_after': ((S, 'medium'), 'pm', 'xhigh', 'after_result'),
                'effort/pm_xhigh_at_start': ((S, 'medium'), 'pm_start', 'xhigh', 'before_result'),
                'effort/opus_pm_low': ((TOP, 'medium'), 'pm', 'low', 'before_result')}
EFFORT_STEPS, EFFORT_MAX_TOKENS = 3, 8192
# The proxy move that reaches Haiku 5.5 (user decision, October 8): the client runs Sonnet 5.5 at low (the low concise
# start) with its own effort message on the note after the prompt, as 2.1.284 sends it, and ActivePolicy forwards every
# request to Haiku 5.5, from the first one on. Modes: move_control (Haiku at the client's effort, the reference),
# move_top (the top-level effort set to medium; the client's effort message, later, is still there) and move_pm (the
# top level left at the client's and an effort message for medium after the client's: ActivePolicy.carry at a turn
# start). Thinking tokens by step show which effort holds; the policy needs move_pm to hold medium.
# case: (client setting, mode, Haiku's effort, placement)
HAIKU_EFFORT_CASES = {'haiku/move_control': ((S, 'low'), 'move_control', 'low', None),
                      'haiku/move_top': ((S, 'low'), 'move_top', 'medium', None),
                      'haiku/move_pm': ((S, 'low'), 'move_pm', 'medium', 'before_result')}
# The stronger check of the proxy move's effort (suite haiku-effort, user decision October 9): over 2 repeats in
# thinking-probe-haiku-5-5-20261009-124512, move_pm thought about 9% more than move_control and move_top about the same,
# with the repeats overlapping, so these puzzles barely separated Haiku's low from medium. It adds references (native_*:
# the client itself at that effort, its requests moved to Haiku, so the top level and the client's message agree) and
# move_pm at xhigh, whose wider gap shows whether the proxy's message sets Haiku's effort at all. move_effort_verdict
# decides, by a rule fixed before the run.
HAIKU_EFFORT_CHECK_CASES = {'haiku/move_control': HAIKU_EFFORT_CASES['haiku/move_control'],
                            'haiku/native_medium': ((S, 'medium'), 'move_control', 'medium', None),
                            'haiku/move_pm': HAIKU_EFFORT_CASES['haiku/move_pm'],
                            'haiku/native_xhigh': ((S, 'xhigh'), 'move_control', 'xhigh', None),
                            'haiku/move_pm_xhigh': ((S, 'low'), 'move_pm', 'xhigh', 'before_result')}
# move_effort_verdict's thresholds, on mean thinking per repeat against move_control's: the xhigh reference must reach
# XHIGH_SEPARATES for the probe to tell efforts apart at all; below MEDIUM_SEPARATES the medium reference is too close to
# low for move_pm's own reading to mean anything.
XHIGH_SEPARATES, MEDIUM_SEPARATES = 1.5, 1.15


def step_puzzle(step):
    """A fresh puzzle for each step, so the model has something to think about at every effort level."""
    a, b, c, d = (step * 3 + 1) % 7, (step * 5 + 2) % 11, (step * 7 + 3) % 13, (step * 11 + 4) % 17
    return (f'find the smallest positive integer n that leaves remainder {a} when divided by 7, {b} when divided by '
            f'11, {c} when divided by 13 and {d} when divided by 17. Work it out carefully, then call record_answer '
            'with n. Do not state the answer in text.')


def _blocks(content):
    if isinstance(content, str):
        return 'str'
    return [b.get('type') for b in content if isinstance(b, dict)]


def _cache_marks(content):
    return [i for i, b in enumerate(content if isinstance(content, list) else [])
            if isinstance(b, dict) and 'cache_control' in b]


def summarize_shape(bodies, betas, client_version):
    """Structure of the longest main-loop (tool-bearing) request per model; never text or IDs."""
    longest = {}
    for body, beta in zip(bodies, betas):
        best = longest.get(body['model'])
        if body.get('tools') and (best is None or len(body['messages']) >= len(best[0]['messages'])):
            longest[body['model']] = (body, beta)
    requests = {}
    for model, (body, beta) in longest.items():
        messages = []
        for m in body['messages']:
            row = {'role': m.get('role'), 'blocks': _blocks(m.get('content', []))}
            if _cache_marks(m.get('content')):
                row['cache_control'] = _cache_marks(m.get('content'))
            if isinstance(m.get('output_config'), dict):  # the client's own per-message effort (2.1.284 sends one)
                row['output_config'] = sorted(m['output_config'])
            messages.append(row)
        def followed_by_system(kind):
            users = [i for i, m in enumerate(messages) if m['role'] == 'user' and
                     (('tool_result' in m['blocks']) == (kind == 'tool_result'))]
            return bool(users) and all(i+1 < len(messages) and messages[i+1]['role'] == 'system' for i in users)
        system = body.get('system', [])
        requests[model] = {
            'top_level_keys': sorted(body),
            'thinking': body.get('thinking'),
            'effort': (body.get('output_config') or {}).get('effort'),
            'output_config_keys': sorted(body.get('output_config') or {}),
            'context_management': body.get('context_management'),
            'anthropic_beta': [b.strip() for b in (beta or '').split(',') if b.strip()],
            'stream': body.get('stream', False),
            'tool_names': [t.get('name') for t in body['tools']],
            'tool_cache_control': _cache_marks(body['tools']),
            'system_blocks': ('str' if isinstance(system, str) else
                              [dict({'type': b.get('type')}, **({'cache_control': b['cache_control']} if 'cache_control' in b else {}))
                               for b in system]),
            'messages': messages,
            'system_after_prompt': followed_by_system('prompt'),
            'system_after_tool_result': followed_by_system('tool_result'),
        }
    return {'client_version': client_version, 'requests': requests,
            'scope': 'Structure of what the client sent to an owned fixture upstream; no text, IDs or provider traffic.'}


def capture_shape(cli, models=(S, TOP), timeout=180):
    """Run the real client against the owned fixture ($0) once per model; return the structure."""
    from .fixtures import fixture_server
    from .governed_session import client_env
    bodies, betas, version = [], [], None
    for model in models:
        server = fixture_server()
        server.keep_bodies = True
        server.script = [{'tool': 'Bash', 'input': {'command': 'true', 'description': 'check'}}, {'text': 'DONE'}]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                dirs = {name: Path(tmp)/name for name in ('workspace', 'home', 'tmp', 'config')}
                for path in dirs.values():
                    path.mkdir()
                env = client_env(os.environ, FAKE_KEY, dirs, cli, {'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{server.server_port}'})
                version = subprocess.run([str(cli), '--version'], env=env, capture_output=True, text=True,
                                         timeout=30, stdin=subprocess.DEVNULL).stdout.strip()
                command = [str(cli), '-p', 'Run the check with Bash, then reply DONE.', '--model', model,
                           '--effort', 'medium', '--output-format', 'stream-json', '--verbose', '--max-turns', '4',
                           '--max-budget-usd', '1.00', '--no-session-persistence', '--setting-sources', '',
                           '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                           '--tools', 'Bash', '--allowedTools', 'Bash']
                subprocess.run(command, env=env, cwd=dirs['workspace'], capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        beta_by_hash = {r['sha256']: r.get('beta') for r in server.received if 'sha256' in r}
        for raw in server.bodies:
            bodies.append(json.loads(raw))
            betas.append(beta_by_hash.get(hashlib.sha256(raw).hexdigest()))
    shape = summarize_shape(bodies, betas, version)
    missing = [m for m in models if m not in shape['requests']]
    if missing:
        raise RuntimeError(f'No main-loop request captured for {missing}; see the client flags')
    return shape


def _append_system(messages):
    """Mirror the client: a system note after the user turn, carrying the moving cache marker."""
    for m in messages:
        if m.get('role') == 'system' and isinstance(m.get('content'), list):
            m['content'] = SYSTEM_NOTE
    messages.append({'role': 'system', 'content': [{'type': 'text', 'text': SYSTEM_NOTE,
                                                    'cache_control': {'type': 'ephemeral'}}]})


def seed_request(shape_name, model, effort, nonce, spec):
    # Same nonce-first prefix as M0, so independent groups never share a cached prefix.
    text = probe.payload({'prefix_lines': PREFIX_LINES, 'max_tokens': 1}, nonce, model, effort, layer='system')['system'][0]['text']
    p = {'model': model, 'max_tokens': SEED_MAX_TOKENS,
         'system': [{'type': 'text', 'text': text, 'cache_control': {'type': 'ephemeral'}}],
         'tools': [copy.deepcopy(TOOL)],
         'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': PROMPTS[shape_name]}]}]}
    if spec.get('thinking') is not None:
        p['thinking'] = copy.deepcopy(spec['thinking'])
    p['output_config'] = {'effort': effort}
    if spec.get('context_management') is not None:
        p['context_management'] = copy.deepcopy(spec['context_management'])
    if spec['system_after_prompt']:
        _append_system(p['messages'])
    return p


def _group(run_id, case, shape_name, repeat, source, target, client_shape, spec_model=None):
    spec = client_shape['requests'][spec_model or source[0]]
    name = f'{case}/{shape_name}/{repeat}'
    return {'name': name, 'case': case, 'shape': shape_name, 'repeat': repeat,
            'source': list(source), 'target': list(target),
            # The proxy forwards the client's headers unchanged, so the switch keeps the source's betas.
            'betas': list(spec['anthropic_beta']), 'system_after_prompt': spec['system_after_prompt'],
            'system_after_tool_result': spec['system_after_tool_result'],
            'request': seed_request(shape_name, source[0], source[1], f'{run_id}/{name}', spec),
            'steps': ['seed', 'switched']}


def _move_groups(run_id, cases, repeat, client_shape):
    """The proxy move's effort groups: the client's request, with its own effort message, forwarded to Haiku 5.5."""
    groups = []
    for case, (home, mode, effort, placement) in cases.items():
        g = _group(run_id, case, 'tool_continuation', repeat, home, (H, effort), client_shape)
        g['request']['messages'][-1]['output_config'] = {'effort': home[1]}  # the client's own effort message
        g.update(steps=['seed'] + [f'step{i}' for i in range(1, EFFORT_STEPS + 1)], effort_mode=mode, placement=placement)
        groups.append(g)
    return groups


def plan(run_id, suite, repeats, client_shape):
    if suite not in SUITES:
        raise ValueError(f'Unknown suite {suite!r}')
    if type(repeats) is not int or repeats < 1:
        raise ValueError('Repeats must be a positive integer')
    groups = []
    for repeat in range(repeats):
        if suite == 'opus-5-5-effort':
            for case, (source, target) in O55_CASES.items():
                groups.append(_group(run_id, case, 'new_turn', repeat, source, target, client_shape))
            continue
        if suite == 'per-message-effort':
            for case, (home, mode, effort, placement) in EFFORT_CASES.items():
                g = _group(run_id, case, 'tool_continuation', repeat, home, (home[0], effort), client_shape)
                # The client's own effort message, as Claude Code 2.1.284 puts it on the note after the prompt.
                g['request']['messages'][-1]['output_config'] = {'effort': home[1]}
                g.update(steps=['seed'] + [f'step{i}' for i in range(1, EFFORT_STEPS + 1)], effort_mode=mode,
                         placement=placement)
                groups.append(g)
            continue
        if suite == 'haiku-effort':
            groups.extend(_move_groups(run_id, HAIKU_EFFORT_CHECK_CASES, repeat, client_shape))
            continue
        if suite == 'returns':
            for case, (home, away, count, anchored) in RETURN_CASES.items():
                g = _group(run_id, case, 'tool_continuation', repeat, home, away, client_shape)
                g.update(steps=['seed'] + ['away']*count + ['return'], away_requests=count, anchored=anchored)
                groups.append(g)
            continue
        transitions = {case: TRANSITIONS[case] for case in SUITE_TRANSITIONS.get(suite, ())}
        # A transition is judged against the control on its target model, so a suite plans only those controls
        # (smoke, with no transitions, is the Sonnet and Opus controls alone).
        targets = {target[0] for _, target in transitions.values()}
        cases = {case: pair for case, pair in dict(CONTROLS, **HAIKU_CONTROL).items()
                 if (pair[1][0] in targets if transitions else case in CONTROLS)}
        cases.update(transitions)
        for shape_name in (('tool_continuation',) if suite == 'smoke' else SHAPES):
            for case, (source, target) in cases.items():
                # The arm's client is the Sonnet tier (S0): the top rung and Haiku are reached only by the proxy
                # rewriting its requests, which keeps the client's headers. So their seeds carry Sonnet's shape and betas.
                groups.append(_group(run_id, case, shape_name, repeat, source, target, client_shape,
                                     spec_model=S if source[0] in (TOP, H) else None))
        if suite == 'haiku-5-5':
            groups.extend(_move_groups(run_id, HAIKU_EFFORT_CASES, repeat, client_shape))
    for g in groups:
        if 'switched' in g['steps']:
            # A refusal that does not depend on the reply (e.g. mid-history system messages for Haiku)
            # is known now: send nothing for it rather than pay for a seed that cannot be continued.
            try:
                switched_request(g, _placeholder_reply(g['shape']))
            except ValueError as exc:
                g.update(steps=[], refused=f'transform: {exc}')
    return groups


def _placeholder_reply(shape_name):
    last = ({'type': 'tool_use', 'id': 'toolu_plan', 'name': TOOL['name'], 'input': {'value': 0}}
            if shape_name == 'tool_continuation' else {'type': 'text', 'text': '0'})
    return {'content': [{'type': 'thinking', 'thinking': '', 'signature': 'plan'}, last]}


def planned_calls(groups):
    return sum(len(g['steps']) for g in groups)


def switched_request(group, response):
    """The client's next request at the source setting, then exactly what the proxy would forward."""
    p = copy.deepcopy(group['request'])
    p['max_tokens'] = SWITCH_MAX_TOKENS
    content = copy.deepcopy(response['content'])  # thinking blocks and signatures passed back unchanged
    p['messages'].append({'role': 'assistant', 'content': content})
    if group['shape'] == 'tool_continuation':
        p['messages'].append({'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': b['id'], 'content': TOOL_RESULT} for b in content if b.get('type') == 'tool_use']})
        if group['system_after_tool_result']:
            _append_system(p['messages'])
    else:
        p['messages'].append({'role': 'user', 'content': [{'type': 'text', 'text': FOLLOW_UP}]})
        if group['system_after_prompt']:
            _append_system(p['messages'])
    model, effort = group['target']
    if model in POLICY_MODELS:
        return transform_request(p, model, effort, allow_thinking_history=True)
    p.update(model=model, output_config=dict(p.get('output_config', {}), effort=effort))
    return p


def continue_request(group, request, response):
    """The client's next request after a reply, still at the client's (home) setting: tool results when the reply
    called the tool, else the instruction as a new user turn, then the client's system note. Content passed back
    unchanged."""
    p = copy.deepcopy(request)
    p['max_tokens'] = SWITCH_MAX_TOKENS
    content = copy.deepcopy(response['content'])
    p['messages'].append({'role': 'assistant', 'content': content})
    uses = [b for b in content if b.get('type') == 'tool_use']
    if uses:
        p['messages'].append({'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': b['id'], 'content': NEXT_STEP} for b in uses]})
    else:
        p['messages'].append({'role': 'user', 'content': [{'type': 'text', 'text': CONTINUE}]})
    if group['system_after_tool_result' if uses else 'system_after_prompt']:
        _append_system(p['messages'])
    return p


def marker_index(messages):
    """The last message carrying a cache breakpoint: where the request's newest cache entry ends."""
    return max((i for i, m in enumerate(messages) if _cache_marks(m.get('content'))), default=None)


def positions_after(messages, index):
    """Content positions after messages[index], counted as the policy counts a return's reach."""
    return content_positions(messages[index + 1:])


def with_anchor(request, index):
    """Put a breakpoint back on messages[index] (home's last cached block), so the return can read that entry
    wherever it is. A string content becomes the same text as a block. At most 4 breakpoints."""
    p = copy.deepcopy(request)
    message = p['messages'][index]
    if isinstance(message['content'], str):
        message['content'] = [{'type': 'text', 'text': message['content']}]
    message['content'][-1]['cache_control'] = {'type': 'ephemeral'}
    marks = (len(_cache_marks(p.get('system'))) + len(_cache_marks(p.get('tools')))
             + sum(len(_cache_marks(m.get('content'))) for m in p['messages']))
    if marks > 4:
        raise ValueError('An anchor would exceed 4 cache breakpoints')
    return p


def puzzle_request(group, request, response, step):
    """The client's next request with the next puzzle: in the tool result, or as a user turn if the reply ended."""
    p = copy.deepcopy(request)
    p['max_tokens'] = EFFORT_MAX_TOKENS
    content = copy.deepcopy(response['content'])
    p['messages'].append({'role': 'assistant', 'content': content})
    uses = [b for b in content if b.get('type') == 'tool_use']
    text = 'Recorded. Next puzzle: ' + step_puzzle(step)
    if uses:
        p['messages'].append({'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': b['id'], 'content': text} for b in uses]})
    else:
        p['messages'].append({'role': 'user', 'content': [{'type': 'text', 'text': text}]})
    if group['system_after_tool_result' if uses else 'system_after_prompt']:
        _append_system(p['messages'])
    return p


def _thinking_tokens(row):
    return ((row.get('usage') or {}).get('output_tokens_details') or {}).get('thinking_tokens')


def _entry_tokens(usage):
    return (usage.get('cache_read_input_tokens') or 0) + (usage.get('cache_creation_input_tokens') or 0)


def return_reuse(home_entry, read):
    """entry_read: the return read home's whole entry; partial: only an earlier part of it (e.g. tools and system);
    none: nothing."""
    return 'entry_read' if read >= home_entry else 'partial' if read > 0 else 'none'


def _refusal_category(response):
    return (response.get('stop_details') or {}).get('category')


def seed_problem(shape_name, response):
    content = response.get('content') or []
    if response.get('stop_reason') == 'refusal':  # HTTP 200, but the model declined: nothing to continue
        return f'seed_refused:{_refusal_category(response)}'
    if not any(b.get('type') in THINKING_TYPES for b in content):
        return 'seed_without_thinking'
    want = 'tool_use' if shape_name == 'tool_continuation' else 'end_turn'
    if response.get('stop_reason') != want:
        return f"seed_stop_reason_{response.get('stop_reason')}"
    if shape_name == 'tool_continuation' and not any(b.get('type') == 'tool_use' and b.get('name') == TOOL['name'] for b in content):
        return 'seed_without_record_answer'
    return None


def describe(response):
    """Structure only: never thinking text, signatures, tool inputs or answers."""
    content = response.get('content') or []
    thinking = [b for b in content if b.get('type') in THINKING_TYPES]
    return {'content_types': [b.get('type') for b in content], 'thinking_blocks': len(thinking),
            'signature_present': bool(thinking) and all(b.get('signature') or b.get('data') for b in thinking),
            'thinking_chars': sum(len(b.get('thinking') or '') for b in thinking),
            'stop_reason': response.get('stop_reason'), 'tool_use': any(b.get('type') == 'tool_use' for b in content),
            'stop_details': _stop_details(response)}


def _stop_details(response):
    details = response.get('stop_details')
    if not isinstance(details, dict):
        return None
    return {'type': details.get('type'), 'category': details.get('category'),
            'explanation': probe.redact_message(str(details.get('explanation') or ''))[:300]}


def estimate(nbytes, max_tokens, model):
    # Conservative admission estimate, as in cache_replication: UTF-8 bytes as tokens, all written.
    rate = probe.tier(RATES[model])  # priced by prompt length: the dearest tier
    return ((nbytes + 1024)*rate['write_5m'] + max_tokens*rate['output'])/1e6


def max_reserve(groups):
    total = 0
    for g in groups:
        if not g['steps']:
            continue
        size = len(json.dumps(g['request']).encode())
        # A move group forwards every request, its seed included, to the target model (HAIKU_EFFORT_CASES).
        sent_to = g['target'][0] if g.get('effort_mode', '').startswith('move_') else g['request']['model']
        total += estimate(size, g['request']['max_tokens'], sent_to)
        if 'switched' in g['steps']:
            total += estimate(size + SEED_MAX_TOKENS*4, SWITCH_MAX_TOKENS, g['target'][0])
        # A return group: every later request carries all earlier replies at their maximum length.
        for step, role in enumerate(g['steps'][1:] if 'away_requests' in g else (), start=1):
            model = g['target'][0] if role == 'away' else g['source'][0]
            total += estimate(size + step*SEED_MAX_TOKENS*4, SWITCH_MAX_TOKENS, model)
        for step in range(1, len(g['steps']) if 'effort_mode' in g else 1):  # a per-message-effort group
            total += estimate(size + step*EFFORT_MAX_TOKENS*4, EFFORT_MAX_TOKENS, sent_to)
    return total


def verdicts(groups, outcomes):
    # A transition's control continues on its target model, in the same shape and repeat.
    controls = {(g['target'][0], g['shape'], g['repeat']): outcomes.get(g['name']) for g in groups if g['case'] in CONTROL_CASES}
    result = []
    for g in groups:
        v = {'case': g['case'], 'shape': g['shape'], 'repeat': g['repeat'], 'source': g['source'], 'target': g['target']}
        v.update(outcomes.get(g['name']) or {'verdict': 'not_run'})
        # A return group is its own control: each away request continues the previous one on the same setting.
        if ('away_requests' not in g and 'effort_mode' not in g and g['case'] not in CONTROL_CASES
                and v['verdict'] in ('accepted', 'rejected', 'refused')):
            model = g['target'][0]
            seen = controls.get((model, g['shape'], g['repeat']))
            if not seen or seen['verdict'] != 'accepted':
                v.update(verdict='inconclusive', reason='control_not_accepted', observed=v['verdict'])
        result.append(v)
    return result


def verified_transitions(summary):
    """(source, target) model pairs accepted in every case, shape and repeat of the suite's transitions. A case's
    pair comes from its recorded verdicts, so a run keeps its evidence after the probe's tiers are retargeted."""
    cases = SUITE_TRANSITIONS.get(summary.get('suite'), ())
    expected, accepted, bad = {}, set(), set()
    for case in cases:
        recorded = {(v['source'][0], v['target'][0]) for v in summary['verdicts'] if v['case'] == case}
        source, target = TRANSITIONS[case]
        for pair in recorded or {(source[0], target[0])}:
            expected.setdefault(pair, set()).update(
                (case, shape_name, repeat) for shape_name in SHAPES for repeat in range(summary['repeats']))
    for v in summary['verdicts']:
        if v['case'] in cases:
            pair = (v['source'][0], v['target'][0])
            if v['verdict'] == 'accepted':
                accepted.add((pair, (v['case'], v['shape'], v['repeat'])))
            else:
                bad.add(pair)
    return sorted([list(pair) for pair, keys in expected.items()
                   if pair not in bad and all((pair, key) in accepted for key in keys)])


def return_findings(verdict_rows):
    """Per return case: the repeats whose return read home's whole entry, and how many positions each return
    was from home's last breakpoint."""
    found = {}
    for v in verdict_rows:
        if v['case'] not in RETURN_CASES:
            continue
        f = found.setdefault(v['case'], {'repeats': 0, 'entry_read': 0, 'positions': [], 'verdicts': []})
        f['repeats'] += 1
        f['verdicts'].append(v['verdict'])
        f['entry_read'] += v.get('return_reuse') == 'entry_read'
        if 'positions_since_home_marker' in v:
            f['positions'].append(v['positions_since_home_marker'])
    for f in found.values():
        f['all_read'] = f['entry_read'] == f['repeats']
    return found


def effort_findings(verdict_rows):
    """Per effort case: accepted repeats, whether steps 2 and 3 kept the cache, and mean thinking tokens by step."""
    found = {}
    for v in verdict_rows:
        if v['case'] not in {**EFFORT_CASES, **HAIKU_EFFORT_CASES, **HAIKU_EFFORT_CHECK_CASES}:
            continue
        f = found.setdefault(v['case'], {'repeats': 0, 'accepted': 0, 'verdicts': [], 'step2_cache': [],
                                         'step3_cache': [], 'thinking': []})
        f['repeats'] += 1
        f['verdicts'].append(v['verdict'])
        f['accepted'] += v['verdict'] == 'accepted'
        for key in ('step2_cache', 'step3_cache'):
            if v.get(key):
                f[key].append(v[key])
        if v.get('thinking'):
            f['thinking'].append(v['thinking'])
    for f in found.values():
        rows = [t for t in f['thinking'] if len(t) == EFFORT_STEPS + 1 and all(x is not None for x in t)]
        f['mean_thinking_by_step'] = [sum(t[i] for t in rows) / len(rows) for i in range(EFFORT_STEPS + 1)] if rows else None
    return found


def move_effort_verdict(found):
    """The haiku-effort suite's decision on Haiku's per_message_effort, fixed before its run (October 9). T(case): the
    mean over repeats of thinking tokens summed over the seed and the three steps.
    - invalid: a case has a repeat not accepted, or a move_pm repeat rewrote the cache at step 2 or 3;
    - inconclusive: T(native_xhigh) < XHIGH_SEPARATES x T(move_control), so the probe can't tell Haiku's efforts apart;
    - does_not_hold: T(move_pm_xhigh) is not nearer T(native_xhigh) than T(move_control), or the medium reference
      separates from low (T(native_medium) >= MEDIUM_SEPARATES x T(move_control)) and T(move_pm) is not nearer it;
    - holds: otherwise. With the medium reference too close to low, the xhigh pair decides alone (medium_separates).
    per_message_effort may be set for Haiku only when the verdict is holds."""
    t = {}
    for case in HAIKU_EFFORT_CHECK_CASES:
        f = found.get(case) or {}
        rows = [r for r in f.get('thinking') or [] if len(r) == EFFORT_STEPS + 1 and None not in r]
        if not f.get('repeats') or f['accepted'] != f['repeats'] or len(rows) != f['repeats']:
            return {'verdict': 'invalid', 'reason': f'{case}: not every repeat accepted with its thinking'}
        if 'move_pm' in case and not (len(f['step2_cache']) == len(f['step3_cache']) == f['repeats']
                                       and set(f['step2_cache'] + f['step3_cache']) == {'kept'}):
            return {'verdict': 'invalid', 'reason': f'{case}: the cache was rewritten'}
        t[case] = sum(map(sum, rows)) / len(rows)
    low = t['haiku/move_control']
    out = {'totals': t, 'xhigh_ratio': t['haiku/native_xhigh'] / low if low else None,
           'medium_ratio': t['haiku/native_medium'] / low if low else None}

    def nearer(case, reference):
        return abs(t[case] - t[reference]) < abs(t[case] - low)
    if not low or out['xhigh_ratio'] < XHIGH_SEPARATES:
        return dict(out, verdict='inconclusive', reason='the xhigh reference does not separate from low')
    out['medium_separates'] = out['medium_ratio'] >= MEDIUM_SEPARATES
    if not nearer('haiku/move_pm_xhigh', 'haiku/native_xhigh'):
        return dict(out, verdict='does_not_hold', reason="move_pm_xhigh is not nearer the xhigh reference than low")
    if out['medium_separates'] and not nearer('haiku/move_pm', 'haiku/native_medium'):
        return dict(out, verdict='does_not_hold', reason="move_pm is not nearer the medium reference than low")
    return dict(out, verdict='holds', reason='move_pm_xhigh nearer the xhigh reference' + (
        ' and move_pm nearer the medium reference' if out['medium_separates'] else
        '; the medium reference too close to low to read move_pm on its own'))


def execute(groups, out, budget, suite, repeats, transport=probe.send):
    """Sequential, no retries or threads. A switched-request 400 is an outcome; anything else unexpected stops."""
    rows, outcomes = [], {}
    started = time.monotonic()
    state = {'status': 'running', 'error': None}

    def summary():
        sent = [r for r in rows if r['status'] != 'transform_refused']
        result = dict(status=state['status'], error=state['error'], suite=suite, repeats=repeats,
                      calls=len(sent), planned_calls=planned_calls(groups),
                      known_cost_usd=sum(r['cost_usd'] for r in sent if r.get('cost_usd') is not None),
                      budget_charged_usd=budget.spent,
                      cost_complete=all(r.get('cost_usd') is not None for r in sent),
                      rejected_requests=sum(r['status'] == 'rejected' for r in sent),
                      refusals=sum(r.get('stop_reason') == 'refusal' for r in sent),
                      transform_refused=sum(r['status'] == 'transform_refused' for r in rows),
                      wall_seconds=time.monotonic()-started, verdicts=verdicts(groups, outcomes),
                      scope='Direct API; thinking blocks passed back unchanged; rejected requests have unknown cost '
                            '(charged to the budget at their admission estimate); no Claude Code integration or savings claim.')
        result['verified_transitions'] = verified_transitions(result)
        if suite == 'returns':
            result['return_findings'] = return_findings(result['verdicts'])
        if suite in ('per-message-effort', 'haiku-5-5', 'haiku-effort'):
            result['effort_findings'] = effort_findings(result['verdicts'])
        if suite == 'haiku-effort':
            result['move_effort_verdict'] = move_effort_verdict(result['effort_findings'])
        tmp = out/'summary.tmp'
        tmp.write_text(json.dumps(result, indent=2)+'\n')
        tmp.replace(out/'summary.json')
        return result

    with (out/'observations.jsonl').open('x') as log:
        def record(row):
            rows.append(row)
            log.write(json.dumps(row)+'\n')
            log.flush()
            print(row['group'], row['role'], row['status'], row.get('observation', row.get('reason', '')), flush=True)

        def send(group, role, p):
            reserve = estimate(len(json.dumps(p).encode()), p['max_tokens'], p['model'])
            budget.reserve(reserve)
            row = dict(group=group['name'], case=group['case'], shape=group['shape'], repeat=group['repeat'], role=role,
                       model=p['model'], effort=(p.get('output_config') or {}).get('effort'), started_unix=time.time(),
                       request_sha256=hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest())
            now = time.monotonic()
            try:
                try:
                    response, rid = transport(p, {'anthropic_beta': ','.join(group['betas'])})
                except urllib.error.HTTPError as exc:
                    details = probe.error_details(exc)
                    details['error_hint'] = probe.redact_message(details.get('error_hint', ''))
                    row.update(http_status=exc.code, error_type='HTTPError', **details)
                    if getattr(exc, 'api_error_type', None):
                        row['api_error_type'] = exc.api_error_type
                    # A billing or authentication failure says nothing about the transition: stop, never a verdict.
                    if exc.code != 400 or role == 'seed' or probe.account_problem(exc):
                        row['status'] = 'error'  # a seed is our own construction: its rejection is a bug
                        raise
                    row.update(status='rejected', cost_usd=None, budget_charged_usd=reserve,
                               api_error=probe.redact_message(getattr(exc, 'safe_api_message', '') or ''))
                    budget.spent += reserve  # never assumed free
                    return None
                except Exception as exc:
                    row.update(status='error', error_type=type(exc).__name__, **probe.error_details(exc))
                    raise
                usage = response.get('usage')
                price = probe.priced_usage(p, response.get('model'), usage, RATES)
                row.update(status='ok', http_status=200, request_id=rid, returned_model=response.get('model'), usage=usage,
                           cost_usd=price, observation=probe.observe(usage) if isinstance(usage, dict) else 'unknown',
                           **describe(response))
                if price is None:
                    row['status'] = 'unpriced'
                    raise RuntimeError('Unpriced response; inspect metadata')
                budget.spent += price
                return response
            finally:
                row['wall_seconds'] = time.monotonic()-now
                record(row)

        def run_return(group):
            home, away = tuple(group['source']), tuple(group['target'])
            seed = send(group, 'seed', group['request'])
            outcome = {'seed_observation': rows[-1]['observation'], 'seed_thinking_blocks': rows[-1]['thinking_blocks'],
                       'away_requests': group['away_requests'], 'anchored': group['anchored']}
            problem = seed_problem(group['shape'], seed)
            if problem:
                return dict(outcome, verdict='inconclusive', reason=problem)
            home_entry = _entry_tokens(rows[-1]['usage'])
            anchor = marker_index(group['request']['messages'])
            request, reply, away_reads = group['request'], seed, []
            for step in range(1, group['away_requests'] + 1):
                request = continue_request(group, request, reply)
                reply = send(group, f'away{step}', transform_request(request, *away, allow_thinking_history=True))
                if reply is None:
                    return dict(outcome, verdict='rejected', reason=f'away{step}_rejected', api_error=rows[-1].get('api_error'))
                if reply.get('stop_reason') == 'refusal':
                    return dict(outcome, verdict='inconclusive', reason=f'away{step}_refused:{_refusal_category(reply)}')
                away_reads.append(rows[-1]['usage'].get('cache_read_input_tokens') or 0)
            request = transform_request(continue_request(group, request, reply), *home, allow_thinking_history=True)
            if group['anchored']:
                request = with_anchor(request, anchor)
            positions = positions_after(request['messages'], anchor)
            outcome.update(home_entry_tokens=home_entry, away_reads=away_reads, positions_since_home_marker=positions,
                           predicted_reachable=group['anchored'] or positions <= LOOKBACK_POSITIONS)
            reply = send(group, 'return', request)
            last = rows[-1]
            outcome.update(api_error=last.get('api_error'), return_observation=last.get('observation'),
                           return_thinking_blocks=last.get('thinking_blocks'))
            if reply is None:
                return dict(outcome, verdict='rejected')
            if reply.get('stop_reason') == 'refusal':
                return dict(outcome, verdict='refused', reason=f'refused:{_refusal_category(reply)}')
            read = last['usage'].get('cache_read_input_tokens') or 0
            return dict(outcome, verdict='accepted', return_read=read,
                        return_write=last['usage'].get('cache_creation_input_tokens') or 0,
                        return_reuse=return_reuse(home_entry, read))

        def run_effort(group):
            home, target = tuple(group['source']), group['target'][1]
            mode, placement = group['effort_mode'], group['placement']
            moving = mode.startswith('move_')  # every request forwarded to the target model (HAIKU_EFFORT_CASES)
            injections = []
            if mode in ('pm_start', 'move_pm'):  # from the first request on, as ModelPilot would at a turn start
                injections.append((effort_anchor(group['request']['messages'], placement), target))

            def moved(client):
                out = transform_request(client, group['target'][0], target if mode == 'move_top' else home[1],
                                        allow_thinking_history=True)
                return with_effort_messages(out, injections) if mode == 'move_pm' else out
            client = group['request']
            reply = send(group, 'seed', moved(client) if moving else with_effort_messages(client, injections))
            row = rows[-1]
            outcome = {'mode': mode, 'placement': placement, 'thinking': [_thinking_tokens(row)],
                       'reads': [row['usage'].get('cache_read_input_tokens') or 0], 'entries': [_entry_tokens(row['usage'])]}
            problem = seed_problem(group['shape'], reply)
            if problem and problem != 'seed_without_thinking':  # at low effort a seed may skip thinking; it still counts
                return dict(outcome, verdict='inconclusive', reason=problem)
            for step in range(1, EFFORT_STEPS + 1):
                client = puzzle_request(group, client, reply, step)
                forwarded = client
                if moving:
                    forwarded = moved(client)
                elif step >= 2 and mode == 'top':
                    forwarded = transform_request(client, home[0], target, allow_thinking_history=True)
                elif mode in ('pm', 'pm_start'):
                    if step == 2 and mode == 'pm':
                        injections.append((effort_anchor(client['messages'], placement), target))
                    forwarded = with_effort_messages(client, injections)
                reply = send(group, f'step{step}', forwarded)
                row = rows[-1]
                if reply is None:
                    return dict(outcome, verdict='rejected', reason=f'step{step}_rejected', api_error=row.get('api_error'))
                if reply.get('stop_reason') == 'refusal':
                    return dict(outcome, verdict='inconclusive', reason=f'step{step}_refused:{_refusal_category(reply)}')
                outcome['thinking'].append(_thinking_tokens(row))
                outcome['reads'].append(row['usage'].get('cache_read_input_tokens') or 0)
                outcome['entries'].append(_entry_tokens(row['usage']))
            reads, entries = outcome['reads'], outcome['entries']
            # Kept: the step read everything the previous request had cached; else part of it was rewritten.
            return dict(outcome, verdict='accepted', injections=[[i, e] for i, e in injections],
                        step2_cache='kept' if reads[2] >= entries[1] else 'rewritten',
                        step3_cache='kept' if reads[3] >= entries[2] else 'rewritten')

        try:
            for group in groups:
                if 'effort_mode' in group:
                    outcomes[group['name']] = run_effort(group)
                    summary()
                    continue
                if not group['steps']:
                    record(dict(group=group['name'], case=group['case'], shape=group['shape'], repeat=group['repeat'],
                                role='switched', status='transform_refused', reason=group['refused']))
                    outcomes[group['name']] = {'verdict': 'transform_refused', 'reason': group['refused']}
                    summary()
                    continue
                if 'away_requests' in group:
                    outcomes[group['name']] = run_return(group)
                    summary()
                    continue
                seed = send(group, 'seed', group['request'])
                outcome = {'seed_observation': rows[-1]['observation'], 'seed_thinking_blocks': rows[-1]['thinking_blocks']}
                problem = seed_problem(group['shape'], seed)
                if problem:
                    outcomes[group['name']] = dict(outcome, verdict='inconclusive', reason=problem)
                    summary()
                    continue
                try:
                    request = switched_request(group, seed)
                except ValueError as exc:
                    reason = f'transform: {exc}'
                    record(dict(group=group['name'], case=group['case'], shape=group['shape'], repeat=group['repeat'],
                                role='switched', status='transform_refused', reason=reason))
                    outcomes[group['name']] = dict(outcome, verdict='transform_refused', reason=reason)
                    summary()
                    continue
                reply = send(group, 'switched', request)
                last = rows[-1]
                outcome.update(api_error=last.get('api_error'), switched_observation=last.get('observation'),
                               switched_thinking_blocks=last.get('thinking_blocks'))
                if reply is None:
                    outcome['verdict'] = 'rejected'
                elif reply.get('stop_reason') == 'refusal':  # accepted by the API, declined by the model
                    outcome.update(verdict='refused', reason=f'refused:{_refusal_category(reply)}')
                else:
                    outcome['verdict'] = 'accepted'
                outcomes[group['name']] = outcome
                summary()
            state['status'] = 'complete'
        except (Exception, KeyboardInterrupt) as exc:
            state.update(status='stopped', error=type(exc).__name__)
        return summary()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['capture-shape']:
        parser = argparse.ArgumentParser(prog='python3 -m modelpilot.thinking_probe capture-shape',
                                         description='Record the pinned client request shape against the owned fixture ($0).')
        parser.add_argument('--claude', type=Path, default=ROOT/'work/claude-client/node_modules/.bin/claude')
        parser.add_argument('--out', type=Path, default=SHAPE_FIXTURE)
        args = parser.parse_args(argv[1:])
        shape = capture_shape(args.claude)
        args.out.write_text(json.dumps(shape, indent=1)+'\n')
        print(f"Captured {shape['client_version']}: {args.out}")
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=SUITES, default='transitions')
    parser.add_argument('--repeats', type=int, help='default: 1 for smoke, 2 otherwise')
    parser.add_argument('--budget', type=float, default=8)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args(argv)
    if not SHAPE_FIXTURE.exists():
        raise SystemExit(f'No captured client shape at {SHAPE_FIXTURE}; run capture-shape first. Nothing sent.')
    shape = json.loads(SHAPE_FIXTURE.read_text())
    repeats = args.repeats or (1 if args.suite == 'smoke' else 2)
    budget = Budget(args.budget)
    rid = str(uuid.uuid4())
    groups = plan(rid, args.suite, repeats, shape)
    out = args.out or ROOT/'runs'/f"thinking-probe-{args.suite}-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=False)
    calls = planned_calls(groups)
    manifest = dict(run_id=rid, suite=args.suite, repeats=repeats, live=args.live, calls=calls, budget_usd=args.budget,
                    max_reserve_usd=max_reserve(groups), rates=RATES, pricing_source=SOURCE,
                    pricing_checked='2026-10-08' if args.suite in ('haiku-5-5', 'haiku-effort') else '2026-09-24', shape=shape,
                    prompts=PROMPTS, follow_up=FOLLOW_UP, controls=dict(CONTROLS, **HAIKU_CONTROL), transitions=TRANSITIONS,
                    opus_5_5_cases=O55_CASES, return_cases=RETURN_CASES, next_step=NEXT_STEP, effort_cases=EFFORT_CASES,
                    haiku_effort_cases=HAIKU_EFFORT_CASES, haiku_effort_check_cases=HAIKU_EFFORT_CHECK_CASES,
                    move_effort_rule=dict(xhigh_separates=XHIGH_SEPARATES, medium_separates=MEDIUM_SEPARATES,
                                          rule=move_effort_verdict.__doc__),
                    step_puzzles=[step_puzzle(i) for i in range(1, EFFORT_STEPS + 1)], groups=groups,
                    method='Seed at the source setting; continue with its content passed back unchanged, transformed '
                           'by policy_actions.transform_request (Opus 5.5: effort edited directly). Switched requests '
                           'keep the source model\'s beta header, as the proxy would. No retries.')
    (out/'plan.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(f"Plan ({args.suite}, {repeats} repeat(s)): {calls} requests at most; admission estimates total "
          f"${manifest['max_reserve_usd']:.2f} (a conservative upper bound, not a forecast); ${args.budget:g} budget; "
          f"client shape {shape['client_version']}; results: {out}", flush=True)
    if not args.live:
        print('Dry-run only. Add --live to send paid requests.')
        return
    if os.environ.get('ANTHROPIC_BASE_URL'):
        raise SystemExit('Direct Anthropic only; unset ANTHROPIC_BASE_URL. Nothing sent.')
    if not os.environ.get('ANTHROPIC_API_KEY'):
        os.environ['ANTHROPIC_API_KEY'] = getpass.getpass('Anthropic API key (hidden): ').strip()
    from .jev_route_check import check_anthropic_key
    problem = check_anthropic_key(os.environ['ANTHROPIC_API_KEY'])
    if problem:
        raise SystemExit(problem)
    result = execute(groups, out, budget, args.suite, repeats)
    print(json.dumps({k: v for k, v in result.items() if k != 'verdicts'}, indent=2))
    if result['status'] != 'complete':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
