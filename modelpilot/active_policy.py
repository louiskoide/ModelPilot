"""The ModelPilot benchmark arm's active policy (user-approved September 26, the benchmark arm only).

Everywhere else the governor stays dry-run. Two layers (user decision, September 28):
- Jev predicts where the task should run: a model and an effort, with probabilities (advisor.JevAdvisor).
  It is asked only at decision points: the start of a user turn, evidence from the stuck detector
  that the current setting isn't enough, and mid-task steps (the host-run test suite flipping between
  failing and passing, or spend reaching the last decision's forecast; config "step"). It never
  applies anything itself.
- ModelPilot decides whether moving there pays (switch_policy.decide): expected token, failure-recovery
  and cache-rewrite cost of staying against each direct move, with hysteresis and conservative
  downgrades. A move jumps straight to its target and is kept for the task revision; there is no
  ladder. Stuck with nothing stronger to move to, the task stops: the proxy refuses the client's next
  main-loop request and the trial is recorded as unfinished.

The client starts at the arm's fallback setting (S0, used when Jev is unavailable). Tools (R5) are
wired by the adapter. R6: every Messages request is admitted before it is sent, on measured spend
against the same per-task limit as the other arms; unknown cost halts, and a refused request is
answered by the proxy with an API-style error and never reaches the provider. A move also needs the
limit to cover its full rebuild. Delegation (config 'delegation', off in the modelpilot arm): the gate may
consult a stronger setting on a brief of host facts instead of switching, and a mid-task model switch may carry
a handoff note from the model being left; both are side requests the proxy sends (ProxyServer.side_call), and
their text is delivered into the conversation and kept in place (delegation.py). For measurement, consult.force can
make a consult at the agent's finish: the Stop hook holds it once per turn and the request that continues the turn
carries the review (finish_review). Haiku 5.5 is reached by proxy moves only, like every other setting, once the
config makes it a candidate (it needs per-message effort there, or the client's own effort message would hold); at
turn starts the measured settings are weighed beside Jev's pick (measured_candidates). Not implemented: worker drafts.
"""
import hashlib
import json
import threading
import time
from . import delegation, switch_policy
from .fixture_dispatch import Dispatcher, ProxyPolicy
from .policy_actions import effort_anchor, effort_message, escalation_proposal, transform_request
from .proxy import cache_ttls, request_effort

SIGNALS = {'repeated_error': 'the same error keeps repeating', 'edit_oscillation': 'edits keep going back and forth',
           'stalled_tests': 'the test suite stopped improving', 'retry_language': 'the agent keeps retrying'}
STEP_CAUSES = {'tests_now_pass': 'the test suite now passes', 'tests_now_fail': 'the test suite now fails',
               'spend_overrun': 'spending since the last decision has reached its forecast',
               'tests_pass': 'the test suite passes', 'agent_finish': 'the agent ended its turn'}
# The same causes read from the agent's own test runs through Bash (plan item 2): its commands, the client's output.
AGENT_STEP_CAUSES = {'tests_now_pass': "the agent's own test run now passes",
                     'tests_now_fail': "the agent's own test run now fails",
                     'tests_pass': "the agent's own test run passes"}


def reviews_at_finish(config):
    """Whether the policy consults at the agent's finish whatever the price (consult.force 'agent_finish'), so the
    Stop hook must hold the finish for it (hooks.REVIEW_AT_STOP)."""
    consult = switch_policy.delegation(config, 'consult')
    return bool(consult['enabled'] and 'agent_finish' in consult['force'] and 'step' in consult['at'])


def parameters(model, effort, config=None, overrides=None):
    """The arm's frozen parameters: its fallback start, where it decides, and the switch rule. overrides: the arm's
    changes to the config file (bench.ARMS 'policy_overrides'), recorded with it."""
    cfg = switch_policy.with_overrides(config or switch_policy.load(), overrides)
    return {'S0': [model, effort], 'advisor': 'Jev (compat checkout): its model question unchanged, plus one effort question',
            'decision_points': cfg['decision_points'], 'effort_changes_at': cfg['effort_changes_at'],
            'step': {k: cfg['step'][k] for k in ('enabled', 'min_requests_between', 'max_per_revision', 'overrun_factor')},
            'return_reuse': {k: cfg['return_reuse'][k] for k in ('enabled', 'max_positions')},
            'settings': ['/'.join(str(x) for x in s) for s in switch_policy.settings(cfg)],
            'switch_rule': 'jump directly to the setting with the lowest expected total cost (switch + P_ok x run + '
                           '(1 - P_ok) x recovery) when it beats staying by the hysteresis (downgrades: plus a multiple '
                           'of their rewrite, and enough confidence); never climbs. P_ok blends Jev with measured '
                           'pass rates when calibration is on; a failure is redone at the turn\'s effort. With the '
                           'quality floor on, only settings no worse than the baseline (or above the current one) are '
                           'weighed, and a setting below it moves to the cheapest allowed one',
            'stop_on': 'stuck with no stronger setting', 'stuck': 'm2 heuristic-v1 (score >= 3, window 6)',
            'config_sha256': hashlib.sha256(switch_policy.CONFIG.read_bytes()).hexdigest(), 'overrides': overrides or {},
            'admission': 'measured spend below the per-task limit; a move also needs the limit to cover its full '
                         'rebuild (request bytes/3 tokens at the dearest write rate), not its output allowance',
            'per_message_effort': {k: cfg['per_message_effort'][k] for k in ('enabled', 'placement', 'beta')},
            'calibration': dict({k: cfg['calibration'][k] for k in ('enabled', 'jev_weight', 'applies_at')},
                                outcomes=cfg['calibration']['outcomes']),
            'jev_gate': {'enabled': switch_policy.gate(cfg)['enabled']},
            'quality_floor': {k: v for k, v in switch_policy.quality_floor(cfg).items() if k in
                              ('enabled', 'baseline', 'min_shared_tasks')},
            'cost_model': {k: cfg[k] for k in ('defaults', 'effort_output_factor', 'effort_request_factor',
                                               'bytes_per_token')},
            'delegation': {kind: {k: v for k, v in switch_policy.delegation(cfg, kind).items() if k != 'about'}
                           for kind in ('consult', 'handoff_note')},
            'not_implemented': (['per-message effort'] if not cfg['per_message_effort']['enabled'] else [])
                               + ['worker drafts (lever 3)'],
            'measured_candidates': {k: v for k, v in (cfg.get('measured_candidates') or {}).items() if k != 'about'}}


def normalized(message):
    """A message as the prompt cache sees it, whatever form the client resends it in: Claude Code turns an earlier
    system note from a text block with a cache marker into a plain string."""
    content = message.get('content')
    if isinstance(content, list):
        content = [{k: v for k, v in b.items() if k != 'cache_control'} if isinstance(b, dict) else b for b in content]
        if len(content) == 1 and isinstance(content[0], dict) and set(content[0]) == {'type', 'text'} \
                and content[0]['type'] == 'text':
            content = content[0]['text']
    return {'role': message.get('role'), 'content': content}


def prefix_digest(messages):
    return hashlib.sha256(json.dumps([normalized(m) for m in messages], sort_keys=True).encode()).hexdigest()


def user_turns(request):
    """User messages that start a turn (text, not tool results)."""
    def starts(m):
        content = m.get('content')
        return m.get('role') == 'user' and (isinstance(content, str) or isinstance(content, list) and
                                            not any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content))
    messages = request.get('messages') or []
    last = next((m for m in reversed(messages) if m.get('role') != 'system'), None)
    return sum(starts(m) for m in messages), bool(last and starts(last))


def write_ttl(request):
    """'1h' when the client's breakpoints ask for an hour (a subscription, or CLAUDE_CODE_PROMPT_CACHE_TTL=1h), else
    None (the configured cache_write_ttl)."""
    return '1h' if '1h' in cache_ttls(request) else None


class ActiveDispatcher(Dispatcher):
    gate, reserve_output = 'spent', False


class ActivePolicy(ProxyPolicy):
    active, mode, gate, keep_kind, Dispatch = True, 'active', 'spent', 'policy_keep', ActiveDispatcher

    def __init__(self, client_model, owner, advisor=None, config=None):
        self.client_model, self.owner, self.advisor = client_model, owner, advisor
        self.config = config or switch_policy.load()
        self.catalog, self.prompt_prefix = [], ''  # set by the adapter once the trial is set up
        # task -> {setting: (unix time, prefix tokens, content positions)}: where each setting's cache entry ends
        self.last_sent, self.lock = {}, threading.Lock()
        # task -> [(index in the client's messages, effort)]: effort messages sent; task -> (message count, digest) of the
        # previous main-loop request, to see that the client only appended since
        self.effort_messages, self.history = {}, {}
        # task -> [((mode, index in the client's messages), text)]: consult advice and handoff notes delivered
        self.deliveries, self.briefs = {}, {}
        self.workspace = self.base = None  # the trial's workspace and base commit (set by the adapter), for briefs
        # task -> main-loop requests seen; the trial's exploration draw is keyed by exploration_key (set by the adapter)
        self.main_requests, self.exploration_key = {}, None

    def check_upstream(self, origin):
        pass  # ProxyServer accepts only direct Anthropic HTTPS or loopback HTTP (offline tests).

    def dispatcher(self, gov, rates):
        return ActiveDispatcher(gov, rates)

    def lifetime(self, ttl):
        """Seconds an entry written with this lifetime counts as warm: cache_lifetime_seconds, else cache_ttl_seconds."""
        return (self.config.get('cache_lifetime_seconds') or {}).get(ttl, self.config['cache_ttl_seconds'])

    def warm(self, task, setting, now):
        with self.lock:
            entry = self.last_sent.get(task, {}).get(tuple(setting))
        return bool(entry) and now - entry[0] <= self.lifetime(entry[3] if len(entry) > 3 else None)

    def entries(self, task, now, positions):
        """{'model/effort': prefix tokens} for this task's settings whose cache entry should still be warm and be
        within reach: the conversation (now `positions` long) has grown by at most return_reuse.max_positions content
        positions since that setting was last sent (runs/thinking-probe-returns-20260930-091128 read entries 25
        positions back, 4/4; farther is unmeasured)."""
        with self.lock:
            sent = dict(self.last_sent.get(task, {}))
        reach = self.config['return_reuse']['max_positions']
        # Returns count only within cache_ttl_seconds whatever the entry's lifetime: measured for 5m entries only.
        live = sorted(((at, s, tokens) for s, (at, tokens, then, *_) in sent.items()
                       if now - at <= self.config['cache_ttl_seconds'] and positions - then <= reach), key=lambda e: e[0])
        out = {switch_policy._label(s): tokens for _, s, tokens in live}
        for _, s, tokens in live:  # per-message effort: a model's cache doesn't depend on its effort ('model/*')
            if switch_policy.per_message(self.config, s[0]):
                out[f'{s[0]}/*'] = tokens  # the newest entry for that model wins
        return out

    def sent(self, task, setting, now, tokens, positions, ttl=None):
        """ttl: the lifetime the request's cache breakpoints asked for ('5m' or '1h'; None: cache_write_ttl)."""
        with self.lock:
            self.last_sent.setdefault(task, {})[tuple(setting)] = (now, tokens, positions, ttl or self.config['cache_write_ttl'])

    def decision_point(self, gov, task, request, revision, proposal):
        """(trigger, key, step facts or None), or None. The key journals the decision once per point."""
        review = self.finish_review(gov, task, revision)
        if review:  # first: the request that continues a held finish looks like a new user turn
            return review
        if proposal['action'] != 'hold':  # the stuck detector sees evidence the current setting isn't enough
            return 'stuck_evidence', f"{revision}/stuck/{proposal['level']}", None
        turns, starting = user_turns(request)
        if starting:
            return 'turn_start', f'{revision}/turn/{turns}', None
        return self.step(gov, task, revision, request)

    def since_last(self, gov, task, revision):
        """This revision's decisions, the step facts since the last one (the agent's requests, their measured spend and
        that decision's forecast) and whether any of that spend is unknown."""
        decided = [e for e in gov.journal('advisor_decision') if e['task'] == task and e['revision'] == revision]
        since = decided[-1]['created'] if decided else 0
        spend = gov.spend(task, since)
        # The policy's own consults and notes count as spend, not as the agent's requests.
        spend['requests'] -= sum(e['payload'].get('request_id') is not None and e['payload'].get('status') != 'refused'
                                 for e in gov.journal('delegation') if e['task'] == task and e['created'] >= since)
        facts = {'requests': spend['requests'], 'spent_usd': spend['spent_usd'],
                 'forecast_usd': decided[-1]['payload'].get('forecast_usd') if decided else None}
        return decided, facts, spend['unknown']

    def finish_review(self, gov, task, revision):
        """The review at the agent's finish (consult.force 'agent_finish'), from host facts only: the Stop hook held the
        agent's finish (hooks.review_block), and this is the first main-loop request since, the one that continues the
        turn. A step whatever the spacing and number of earlier steps; the consult cap still holds."""
        if not reviews_at_finish(self.config):
            return None
        held = [e for e in gov.journal('review_block') if e['task'] == task and e['revision'] == revision]
        if not held or any(e['task'] == task and e['revision'] == revision for e in gov.journal('policy_stop')):
            return None  # nothing held, or the task was stopped: its stop stands
        key = f"{revision}/step/finish/{held[-1]['seq']}"
        if any(e['payload'].get('point') == key for e in gov.journal('advisor_decision') if e['task'] == task):
            return None
        return 'step', key, dict(self.since_last(gov, task, revision)[1], cause='agent_finish')

    def step(self, gov, task, revision, request=None):
        """A mid-task decision point: the host-run test suite flipped between failing and passing since the last
        decision; else the agent's own test runs through Bash did (plan item 2: agents test through Bash, the host tool
        ran in 2 of 28 trials); else the task's measured spend since then reached that decision's forecast. The agent's
        runs are read from the request's conversation by their summary lines: evidence that a decision is due, never
        a verdict on the work."""
        cfg = self.config['step']
        if not cfg['enabled'] or 'step' not in self.config['decision_points']:
            return None
        decided, facts, unknown = self.since_last(gov, task, revision)
        if not decided:
            return None  # the turn start decides first
        if (sum(e['payload']['trigger'] == 'step' for e in decided) >= cfg['max_per_revision']
                or facts['requests'] < cfg['min_requests_between']):
            return None
        last = decided[-1]
        suite = gov.state.observations(task, revision, 'failures', 1000)
        new = bool(suite) and suite[0]['seq'] > last['payload'].get('suite_seq', 0)
        # The suite passes for the first time in this revision (a review point for a forced consult)
        first_pass = new and suite[0]['failures'] == 0 and all(s['failures'] != 0 for s in suite[1:])
        if new and len(suite) >= 2 and (suite[0]['failures'] == 0) != (suite[1]['failures'] == 0):
            cause = 'tests_now_pass' if suite[0]['failures'] == 0 else 'tests_now_fail'
            return 'step', f"{revision}/step/tests/{suite[0]['seq']}", dict(facts, cause=cause, first_pass=first_pass)
        runs = delegation.agent_test_results((request or {}).get('messages'))
        fresh = len(runs) > last['payload'].get('agent_runs', 0)
        agent_first = fresh and runs[-1]['failures'] == 0 and all(r['failures'] for r in runs[:-1])
        if fresh and len(runs) >= 2 and (runs[-1]['failures'] == 0) != (runs[-2]['failures'] == 0):
            cause = 'tests_now_pass' if runs[-1]['failures'] == 0 else 'tests_now_fail'
            return 'step', f"{revision}/step/agent_tests/{len(runs)}", dict(facts, cause=cause, first_pass=agent_first,
                                                                            source='agent')
        consult = switch_policy.delegation(self.config, 'consult')
        if first_pass and consult['enabled'] and 'tests_pass' in consult.get('force', ()):
            return 'step', f"{revision}/step/tests/{suite[0]['seq']}", dict(facts, cause='tests_pass', first_pass=True)
        if agent_first and consult['enabled'] and 'tests_pass' in consult.get('force', ()):
            return 'step', f"{revision}/step/agent_tests/{len(runs)}", dict(facts, cause='tests_pass', first_pass=True,
                                                                            source='agent')
        forecast = facts['forecast_usd']
        if not unknown and forecast and facts['spent_usd'] >= cfg['overrun_factor'] * forecast:
            return 'step', f"{revision}/step/spend/{last['seq']}", dict(facts, cause='spend_overrun')
        return None

    def evidence(self, gov, task, setting, trigger='stuck_evidence', facts=None):
        if trigger == 'step':
            forecast = facts['forecast_usd']
            causes = AGENT_STEP_CAUSES if facts.get('source') == 'agent' else STEP_CAUSES
            return (f"ModelPilot step check: {causes[facts['cause']]}; on {setting[0]} at {setting[1]} effort, "
                    f"{facts['requests']} requests and ${facts['spent_usd']:.4f} since the last decision" +
                    (f' (forecast ${forecast:.4f}).' if forecast else '.'))
        signals = gov.state.recommend(task).get('signals') or {}
        seen = [text for name, text in SIGNALS.items() if signals.get(name)]
        return (f'ModelPilot progress check: on {setting[0]} at {setting[1]} effort, ' +
                ('; '.join(seen) or 'progress has stalled') + '.')

    def decide(self, gov, rates, task, revision, point, request, current, setting, now):
        """Jev's advice and the switch decision for one decision point, journaled once and reused."""
        trigger, key, facts = point
        earlier = [e['payload'] for e in gov.journal('advisor_decision') if e['task'] == task and e['payload']['point'] == key]
        if earlier:
            return earlier[-1]
        reach = switch_policy.content_positions(current.get('messages') or [])
        prof = switch_policy.profile(self.config, current, self.warm(task, setting, now),
                                     entries=self.entries(task, now, reach), write_ttl=write_ttl(current))
        prof['history'] = any(m.get('role') == 'assistant' for m in request.get('messages') or [])
        advice, gate = None, None
        if self.advisor is not None and switch_policy.gate(self.config)['enabled']:
            gate = switch_policy.jev_gate(self.config, rates, setting, prof, trigger)
        if self.advisor is not None and (gate is None or gate['can_change']):
            evidence = self.evidence(gov, task, setting, trigger, facts) if trigger != 'turn_start' else None
            advice = self.advisor.ask(request, setting[0], self.catalog, self.config['effort_order'],
                                      self.prompt_prefix, evidence)
        usable = advice if advice and not advice.get('error') else None
        consult = switch_policy.delegation(self.config, 'consult')
        if consult['enabled'] and trigger in consult['at']:
            done = sum(e['payload']['kind'] == 'consult' for e in gov.journal('delegation')
                       if e['task'] == task and e['revision'] == revision)
            text = self.brief(gov, task, trigger, facts, setting, request.get('messages'))
            self.briefs[key] = text
            prof.update(consults_left=consult['max_per_revision'] - done,
                        brief_tokens=len(text.encode()) / self.config['bytes_per_token'],
                        force_consult=bool(facts) and (facts['cause'] in consult['force'] or
                                                       bool(facts.get('first_pass')) and 'tests_pass' in consult['force']))
        decision = switch_policy.decide(self.config, rates, usable, setting, prof, trigger)
        if gate is not None:
            decision['gate'] = gate
            # Jev wasn't asked: no answer it could give moves this point (a quality floor move keeps its own reason)
            if not gate['can_change'] and decision['reason'] == 'advice_unavailable':
                decision['reason'] = 'jev_cannot_change'
        if decision['action'] != 'consult':
            self.briefs.pop(key, None)
        suite = gov.state.observations(task, revision, 'failures', 1)
        decision.update(point=key, suite_seq=suite[0]['seq'] if suite else 0, step=facts,
                        agent_runs=len(delegation.agent_test_results(request.get('messages'))),
                        profile={k: prof[k] for k in ('prefix_tokens', 'messages_tokens', 'warm', 'warm_entries', 'history',
                                                      'consults_left', 'brief_tokens', 'force_consult') if k in prof},
                        advice=None if advice is None else
                        {k: advice.get(k) for k in ('model', 'effort', 'metrics', 'usage', 'router_model', 'ms', 'stub',
                                                    'error', 'status', 'auth', 'prompt_chars', 'prompt_sha256', 'models')})
        with gov.db:
            gov._journal('advisor_decision', decision, task, revision)
        return decision

    def plan(self, gov, rates, task, request, side=None):
        """The decision (_plan), then the body rewritten to carry the task's context: with per-message effort on, effort
        rides in effort-only system messages and the top-level effort stays the client's, so the cached conversation
        survives an effort change; consult advice and handoff notes are put back where they were first delivered. A
        deferral that must still carry them gets a 'forward' body. side: the proxy's side_call for this request (sends
        consults and notes); without it nothing is delegated."""
        result = self._plan(gov, rates, task, request, side)
        if result.get('status') in ('stop', 'not_main_loop'):
            return result
        admitted = result.get('status') == 'admitted'
        if admitted:
            ticket = result.get('proposal') if result.get('kind') == 'escalation' else result
            setting = (ticket['target_model'], ticket['target_effort'])
        else:
            setting = (request['model'], request_effort(request))
        body = self.carry(task, request, result['request'] if admitted else request, setting)
        if body is None:
            return result
        body, delivered = body
        extra = {'deliveries': delivered} if delivered else {}
        beta = self.config['per_message_effort']['beta']
        if beta:
            extra['beta'] = beta
        return dict(result, request=body, **extra) if admitted else dict(result, forward=body, **extra)

    def _per_message(self, request, setting):
        return (self.config['per_message_effort']['enabled'] and switch_policy.per_message(self.config, setting[0])
                and (request.get('thinking') or {}).get('type') == 'adaptive' and setting[1] is not None)

    def carry(self, task, request, body, setting):
        """(body, deliveries carried) with the task's effort messages (top-level effort set back to the client's; a new
        effort message at this request's frontier when the setting's effort differs from the one in effect, at a turn
        start) and its deliveries. None: nothing to change."""
        messages = request.get('messages') or []
        if len(body.get('messages') or []) != len(messages):
            return None  # a body that isn't this request's messages: leave it as it is
        effort, per_message = setting[1], self._per_message(request, setting)
        client_effort = request_effort(request)
        with self.lock:
            sent, seen = self.effort_messages.get(task, []), self.history.get(task)
            delivered = self.deliveries.get(task, [])
            if seen and (len(messages) < seen[0] or prefix_digest(messages[:seen[0]]) != seen[1]):
                sent, delivered = [], []  # the client rewrote its history (e.g. compaction): the cache is gone anyway
            self.history[task] = (len(messages), prefix_digest(messages))
            current = sent[-1][1] if sent else client_effort
            # Only at a turn start: inside a turn's tool loop an effort message does not take effect.
            if per_message and effort != current and user_turns(request)[1]:
                anchor = effort_anchor(messages, self.config['per_message_effort']['placement'])
                if sent and anchor <= sent[-1][0]:
                    sent = sent[:-1]  # a second change at the same frontier replaces the first
                if effort != (sent[-1][1] if sent else client_effort):
                    sent = sent + [(anchor, effort)]
            self.effort_messages[task], self.deliveries[task] = sent, delivered
        out = self.render(body, client_effort, sent if per_message else [], delivered)
        if out is None and delivered:  # the client's messages no longer hold a delivery where it went: stop carrying them
            with self.lock:
                self.deliveries[task] = []
            delivered = []
            out = self.render(body, client_effort, sent if per_message else [], [])
        return None if out is None else (out, len(delivered))

    @staticmethod
    def render(body, client_effort, sent, delivered):
        """body with effort messages and deliveries in place; None when there is nothing to add or a delivery can't be
        placed."""
        if not sent and not delivered:
            return None
        if sent:
            body = dict(body, output_config=dict(body.get('output_config') or {}, effort=client_effort))
        return delegation.apply(body, delivered, [(index, effort_message(e)) for index, e in sent])

    def rendered(self, task, request, body, setting):
        """What this request would be sent as at setting, from the state carried so far (no new effort message, no
        state change): the side request for a handoff note reads that cached prefix."""
        with self.lock:
            sent, delivered = list(self.effort_messages.get(task, [])), list(self.deliveries.get(task, []))
        out = self.render(body, request_effort(request), sent if self._per_message(request, setting) else [], delivered)
        return out or body

    def deliver(self, task, request, text):
        """Keep text in the conversation from this request on; False when the request has nowhere to put it."""
        where = delegation.frontier(request.get('messages') or [])
        if not where:
            return False
        with self.lock:
            self.deliveries[task] = self.deliveries.get(task, []) + [(where, text)]
        return True

    def brief(self, gov, task, trigger, facts, setting, messages):
        spec = switch_policy.delegation(self.config, 'consult')
        if trigger == 'stuck_evidence':
            signals = gov.state.recommend(task).get('signals') or {}
            why = (delegation.WHY['stuck_evidence'] + ': ' +
                   ('; '.join(text for name, text in SIGNALS.items() if signals.get(name)) or 'progress has stalled') + '.')
        else:
            texts = delegation.AGENT_WHY if facts.get('source') == 'agent' else delegation.WHY
            why = texts['tests_pass' if facts.get('first_pass') and facts['cause'] == 'tests_now_pass' else facts['cause']]
        return delegation.brief(gov, task, why, setting, facts, self.workspace, self.base, spec, messages)

    def route_brief(self, text, setting, target):
        """Jev's second role: who answers the brief. Its model and effort when that setting is stronger than the
        current one, else the gate's target. Returns (setting, Jev's answer or None)."""
        if self.advisor is None or not switch_policy.delegation(self.config, 'consult')['route_brief']:
            return target, None
        answer = self.advisor.ask({'messages': [{'role': 'user', 'content': text}]}, setting[0], self.catalog,
                                  self.config['effort_order'])
        if not answer or answer.get('error'):
            return target, answer
        model, effort = (answer.get('model') or {}).get('choice'), (answer.get('effort') or {}).get('choice')
        routed = (model, effort if model in self.config['models'] and self.config['models'][model]['efforts'] else None)
        if routed in switch_policy.settings(self.config) and routed != tuple(setting) and \
                switch_policy.at_least(self.config, routed, setting):
            return routed, answer
        return target, answer

    def consult(self, gov, task, revision, point, decision, request, setting, side):
        """Ask the consult setting about the brief, once per decision point, and deliver its advice."""
        spec = switch_policy.delegation(self.config, 'consult')
        if side is None or any(e['payload'].get('point') == point[1] for e in gov.journal('delegation') if e['task'] == task):
            return None
        text = self.briefs.pop(point[1], None) or self.brief(gov, task, point[0], point[2], setting,
                                                             request.get('messages'))
        who, answer = self.route_brief(text, setting, tuple(decision['consult']))
        result = side('consult', delegation.consult_request(text, who, spec), spec['timeout_seconds'])
        advice = delegation.reply_text(result.get('message'), spec['advice_max_bytes'])
        delivered = bool(advice) and self.deliver(task, request, delegation.framing(
            'consult', advice, gov.channel_code(), f'{who[0]} at {who[1]} effort'))
        entry = {'kind': 'consult', 'point': point[1], 'trigger': point[0], 'reason': decision['reason'],
                 'gate_setting': decision['consult'], 'setting': list(who), 'status': result.get('status'),
                 'http_status': result.get('http_status'), 'stop_reason': (result.get('message') or {}).get('stop_reason'),
                 'request_id': result.get('request_id'), 'cost_usd': result.get('cost_usd'), 'delivered': delivered,
                 'brief_bytes': len(text.encode()), 'brief': text, 'advice': advice,
                 'routing': None if answer is None else {k: answer.get(k) for k in ('model', 'effort', 'error', 'usage', 'ms')}}
        with gov.db:
            gov._journal('delegation', entry, task, revision)
        return entry

    def handoff_note(self, gov, task, revision, point, request, current, setting, target, side):
        """Before a model move with earlier work in the conversation: the model being left writes a note in a side
        request on its own cached prefix; the note is delivered with the move."""
        spec = switch_policy.delegation(self.config, 'handoff_note')
        if (not spec['enabled'] or side is None or setting[0] == target[0]
                or not any(m.get('role') == 'assistant' for m in request.get('messages') or [])):
            return None
        body = delegation.note_request(self.rendered(task, request, current, setting), spec)
        result = side('handoff_note', body, spec['timeout_seconds']) if body else {'status': 'no_frontier'}
        note = delegation.reply_text(result.get('message'), spec['note_max_bytes'])
        delivered = bool(note) and self.deliver(task, request, delegation.framing(
            'handoff_note', note, gov.channel_code(), f'{setting[0]} at {setting[1]} effort'))
        entry = {'kind': 'handoff_note', 'point': point[1], 'trigger': point[0], 'source': list(setting),
                 'target': list(target), 'status': result.get('status'), 'http_status': result.get('http_status'),
                 'stop_reason': (result.get('message') or {}).get('stop_reason'), 'request_id': result.get('request_id'),
                 'cost_usd': result.get('cost_usd'), 'delivered': delivered, 'note': note}
        with gov.db:
            gov._journal('delegation', entry, task, revision)
        return entry

    def explore(self, gov, rates, task, revision, request, current, setting, proposal, now):
        """The exploration arm's randomized switch (config exploration): at the request its draw names, move to the
        target model at the turn's effort whatever the gate says, journal it (kind exploration) with the switch cost
        the policy predicts, and return the ticket. None when nothing is due here, or when the move isn't admitted
        (the request then goes on as usual)."""
        spec = switch_policy.exploration(self.config)
        if not spec['enabled']:
            return None
        with self.lock:
            n = self.main_requests[task] = self.main_requests.get(task, 0) + 1
        plan = switch_policy.exploration_plan(self.config, self.exploration_key or task)
        if not plan['switch'] or n != plan['at_request']:
            return None
        target = (spec['target_model'], setting[1])  # inside a turn only the model can move (effort_changes_at)
        reach = switch_policy.content_positions(current.get('messages') or [])
        prof = switch_policy.profile(self.config, current, self.warm(task, setting, now),
                                     entries=self.entries(task, now, reach), write_ttl=write_ttl(current))
        entry = {'point': f'{revision}/explore/{n}', 'request': n, 'plan': plan, 'source': list(setting),
                 'target': list(target), 'prefix_tokens': prof['prefix_tokens'], 'warm': prof['warm'],
                 'predicted_switch_usd': switch_policy.switch_cost(self.config, rates, setting, target, prof, reuse=True)}
        ticket = None
        if target == tuple(setting) or target not in switch_policy.settings(self.config):
            entry['status'] = 'nothing_to_switch'
        else:
            jump = dict(proposal, action='jump', trigger='exploration', decision=entry['point'],
                        target_model=target[0], target_effort=target[1])
            try:
                ticket = self.dispatcher(gov, rates).begin(jump, self.owner, current)
            except ValueError as exc:
                ticket = {'status': 'deferred', 'reason': 'refused:' + str(exc)}
            entry.update(status=ticket['status'], reason=ticket.get('reason'))
        with gov.db:
            gov._journal('exploration', entry, task, revision)
        if ticket is None or ticket['status'] != 'admitted':
            return None
        tokens = len(json.dumps(current)) / self.config['bytes_per_token']
        self.sent(task, target, now, tokens, reach, write_ttl(current))
        return dict(ticket, kind='escalation')

    def _plan(self, gov, rates, task, request, side=None):
        """Returns a ticket for an applied request, a stop, or a deferral dict (forward the original)."""
        if request.get('model') != self.client_model or not request.get('tools'):
            return {'status': 'not_main_loop'}
        now = time.time()
        client = (request['model'], request_effort(request))
        row = gov.state.get(task)
        revision = row['revision']
        if row['ack_revision'] != revision:
            return {'status': 'deferred', 'reason': 'unacknowledged_revision'}
        setting = self.effective(gov, task, revision) or client
        try:
            current = transform_request(request, *setting) if setting != client else request
            proposal = escalation_proposal(gov.state, task, revision, self.owner, *setting)
        except ValueError as exc:
            return {'status': 'deferred', 'reason': 'refused:' + str(exc)}
        explored = self.explore(gov, rates, task, revision, request, current, setting, proposal, now)
        if explored is not None:
            return explored
        point = self.decision_point(gov, task, request, revision, proposal)
        tokens = len(json.dumps(current)) / self.config['bytes_per_token']  # what this request's cache entry covers
        positions = switch_policy.content_positions(current.get('messages') or [])
        decision = self.decide(gov, rates, task, revision, point, request, current, setting, now) if point else None
        if decision and decision['action'] == 'stop':
            stop = {'status': 'stop', 'reason': 'policy_stop:' + decision['reason'], 'level': proposal['level'],
                    'model': setting[0], 'effort': setting[1]}
            with gov.db:
                gov._journal('policy_stop', stop, task, revision)
            return stop  # the task ends unfinished, with no retry
        if decision and decision['action'] == 'consult':
            self.consult(gov, task, revision, point, decision, request, setting, side)
        deferral = None
        if decision and decision['action'] == 'jump' and tuple(decision['target']) != setting:
            jump = dict(proposal, action='jump', trigger=point[0], decision=point[1],
                        target_model=decision['target'][0], target_effort=decision['target'][1])
            try:
                ticket = self.dispatcher(gov, rates).begin(jump, self.owner, current)
            except ValueError as exc:
                ticket = {'status': 'deferred', 'reason': 'refused:' + str(exc)}
            if ticket['status'] == 'admitted':
                self.handoff_note(gov, task, revision, point, request, current, setting, tuple(decision['target']), side)
                self.sent(task, decision['target'], now, tokens, positions, write_ttl(current))
                return dict(ticket, kind='escalation')
            deferral = ticket
        self.sent(task, setting, now, tokens, positions, write_ttl(current))
        return self.keep(gov, rates, task, revision, client, setting, current, deferral)
