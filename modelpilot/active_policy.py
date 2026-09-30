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
limit to cover its full rebuild. Not implemented: per-message effort, Haiku targets and worker drafts.
"""
import hashlib
import json
import threading
import time
from . import switch_policy
from .fixture_dispatch import Dispatcher, ProxyPolicy
from .policy_actions import escalation_proposal, transform_request
from .proxy import request_effort

SIGNALS = {'repeated_error': 'the same error keeps repeating', 'edit_oscillation': 'edits keep going back and forth',
           'stalled_tests': 'the test suite stopped improving', 'retry_language': 'the agent keeps retrying'}
STEP_CAUSES = {'tests_now_pass': 'the test suite now passes', 'tests_now_fail': 'the test suite now fails',
               'spend_overrun': 'spending since the last decision has reached its forecast'}


def parameters(model, effort, config=None):
    """The arm's frozen parameters: its fallback start, where it decides, and the switch rule."""
    cfg = config or switch_policy.load()
    return {'S0': [model, effort], 'advisor': 'Jev (compat checkout): its model question unchanged, plus one effort question',
            'decision_points': cfg['decision_points'],
            'step': {k: cfg['step'][k] for k in ('enabled', 'min_requests_between', 'max_per_revision', 'overrun_factor')},
            'return_reuse': cfg['return_reuse']['enabled'],
            'settings': ['/'.join(str(x) for x in s) for s in switch_policy.settings(cfg)],
            'switch_rule': 'jump directly to the setting with the lowest expected total cost (switch + P_ok x run + '
                           '(1 - P_ok) x recovery) when it beats staying by the hysteresis (downgrades: plus a multiple '
                           'of their rewrite, and enough confidence); never climbs',
            'stop_on': 'stuck with no stronger setting', 'stuck': 'm2 heuristic-v1 (score >= 3, window 6)',
            'config_sha256': hashlib.sha256(switch_policy.CONFIG.read_bytes()).hexdigest(),
            'admission': 'measured spend below the per-task limit; a move also needs the limit to cover its full '
                         'rebuild (request bytes/3 tokens at the dearest write rate), not its output allowance',
            'not_implemented': ['per-message effort', 'Haiku targets', 'worker drafts (lever 3)']}


def user_turns(request):
    """User messages that start a turn (text, not tool results)."""
    def starts(m):
        content = m.get('content')
        return m.get('role') == 'user' and (isinstance(content, str) or isinstance(content, list) and
                                            not any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content))
    messages = request.get('messages') or []
    last = next((m for m in reversed(messages) if m.get('role') != 'system'), None)
    return sum(starts(m) for m in messages), bool(last and starts(last))


class ActiveDispatcher(Dispatcher):
    gate, reserve_output = 'spent', False


class ActivePolicy(ProxyPolicy):
    active, mode, gate, keep_kind, Dispatch = True, 'active', 'spent', 'policy_keep', ActiveDispatcher

    def __init__(self, client_model, owner, advisor=None, config=None):
        self.client_model, self.owner, self.advisor = client_model, owner, advisor
        self.config = config or switch_policy.load()
        self.catalog, self.prompt_prefix = [], ''  # set by the adapter once the trial is set up
        self.last_sent, self.lock = {}, threading.Lock()  # task -> {setting: (unix time, prefix tokens)}: warm entries

    def check_upstream(self, origin):
        pass  # ProxyServer accepts only direct Anthropic HTTPS or loopback HTTP (offline tests).

    def dispatcher(self, gov, rates):
        return ActiveDispatcher(gov, rates)

    def warm(self, task, setting, now):
        with self.lock:
            entry = self.last_sent.get(task, {}).get(tuple(setting))
        return bool(entry) and now - entry[0] <= self.config['cache_ttl_seconds']

    def entries(self, task, now):
        """{'model/effort': prefix tokens} for this task's settings whose cache entry should still be warm."""
        with self.lock:
            sent = dict(self.last_sent.get(task, {}))
        return {switch_policy._label(s): tokens for s, (at, tokens) in sent.items()
                if now - at <= self.config['cache_ttl_seconds']}

    def sent(self, task, setting, now, tokens):
        with self.lock:
            self.last_sent.setdefault(task, {})[tuple(setting)] = (now, tokens)

    def decision_point(self, gov, task, request, revision, proposal):
        """(trigger, key, step facts or None), or None. The key journals the decision once per point."""
        if proposal['action'] != 'hold':  # the stuck detector sees evidence the current setting isn't enough
            return 'stuck_evidence', f"{revision}/stuck/{proposal['level']}", None
        turns, starting = user_turns(request)
        if starting:
            return 'turn_start', f'{revision}/turn/{turns}', None
        return self.step(gov, task, revision)

    def step(self, gov, task, revision):
        """A mid-task decision point, from host facts only: the test suite flipped between failing and passing
        since the last decision, or the task's measured spend since then reached that decision's forecast."""
        cfg = self.config['step']
        if not cfg['enabled'] or 'step' not in self.config['decision_points']:
            return None
        decided = [e for e in gov.journal('advisor_decision') if e['task'] == task and e['revision'] == revision]
        if not decided:
            return None  # the turn start decides first
        last = decided[-1]
        spend = gov.spend(task, last['created'])
        if (sum(e['payload']['trigger'] == 'step' for e in decided) >= cfg['max_per_revision']
                or spend['requests'] < cfg['min_requests_between']):
            return None
        facts = {'requests': spend['requests'], 'spent_usd': spend['spent_usd'],
                 'forecast_usd': last['payload'].get('forecast_usd')}
        suite = gov.state.observations(task, revision, 'failures')
        if (len(suite) == 2 and (suite[0]['failures'] == 0) != (suite[1]['failures'] == 0)
                and suite[0]['seq'] > last['payload'].get('suite_seq', 0)):
            cause = 'tests_now_pass' if suite[0]['failures'] == 0 else 'tests_now_fail'
            return 'step', f"{revision}/step/tests/{suite[0]['seq']}", dict(facts, cause=cause)
        forecast = facts['forecast_usd']
        if not spend['unknown'] and forecast and spend['spent_usd'] >= cfg['overrun_factor'] * forecast:
            return 'step', f"{revision}/step/spend/{last['seq']}", dict(facts, cause='spend_overrun')
        return None

    def evidence(self, gov, task, setting, trigger='stuck_evidence', facts=None):
        if trigger == 'step':
            forecast = facts['forecast_usd']
            return (f"ModelPilot step check: {STEP_CAUSES[facts['cause']]}; on {setting[0]} at {setting[1]} effort, "
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
        advice = None
        if self.advisor is not None:
            evidence = self.evidence(gov, task, setting, trigger, facts) if trigger != 'turn_start' else None
            advice = self.advisor.ask(request, setting[0], self.catalog, self.config['effort_order'],
                                      self.prompt_prefix, evidence)
        usable = advice if advice and not advice.get('error') else None
        prof = switch_policy.profile(self.config, current, self.warm(task, setting, now), entries=self.entries(task, now))
        decision = switch_policy.decide(self.config, rates, usable, setting, prof, trigger)
        suite = gov.state.observations(task, revision, 'failures', 1)
        decision.update(point=key, suite_seq=suite[0]['seq'] if suite else 0, step=facts,
                        profile={k: prof[k] for k in ('prefix_tokens', 'messages_tokens', 'warm', 'warm_entries')},
                        advice=None if advice is None else
                        {k: advice.get(k) for k in ('model', 'effort', 'metrics', 'usage', 'router_model', 'ms', 'stub',
                                                    'error', 'status', 'auth', 'prompt_chars', 'prompt_sha256', 'models')})
        with gov.db:
            gov._journal('advisor_decision', decision, task, revision)
        return decision

    def plan(self, gov, rates, task, request):
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
        point = self.decision_point(gov, task, request, revision, proposal)
        tokens = len(json.dumps(current)) / self.config['bytes_per_token']  # what this request's cache entry covers
        decision = self.decide(gov, rates, task, revision, point, request, current, setting, now) if point else None
        if decision and decision['action'] == 'stop':
            stop = {'status': 'stop', 'reason': 'policy_stop:' + decision['reason'], 'level': proposal['level'],
                    'model': setting[0], 'effort': setting[1]}
            with gov.db:
                gov._journal('policy_stop', stop, task, revision)
            return stop  # the task ends unfinished, with no retry
        deferral = None
        if decision and decision['action'] == 'jump' and tuple(decision['target']) != setting:
            jump = dict(proposal, action='jump', trigger=point[0], decision=point[1],
                        target_model=decision['target'][0], target_effort=decision['target'][1])
            try:
                ticket = self.dispatcher(gov, rates).begin(jump, self.owner, current)
            except ValueError as exc:
                ticket = {'status': 'deferred', 'reason': 'refused:' + str(exc)}
            if ticket['status'] == 'admitted':
                self.sent(task, decision['target'], now, tokens)
                return dict(ticket, kind='escalation')
            deferral = ticket
        self.sent(task, setting, now, tokens)
        return self.keep(gov, rates, task, revision, client, setting, current, deferral)
