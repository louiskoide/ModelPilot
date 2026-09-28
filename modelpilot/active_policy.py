"""The ModelPilot benchmark arm's active policy (user-approved September 26, the benchmark arm only).

Everywhere else the governor stays dry-run. Two layers (user decision, September 28):
- Jev predicts where the task should run: a model and an effort, with probabilities (advisor.JevAdvisor).
  It is asked only at decision points: the start of a user turn, and evidence from the stuck detector
  that the current setting isn't enough. It never applies anything itself.
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
import threading
import time
from . import switch_policy
from .fixture_dispatch import Dispatcher, ProxyPolicy
from .policy_actions import escalation_proposal, transform_request
from .proxy import request_effort

SIGNALS = {'repeated_error': 'the same error keeps repeating', 'edit_oscillation': 'edits keep going back and forth',
           'stalled_tests': 'the test suite stopped improving', 'retry_language': 'the agent keeps retrying'}


def parameters(model, effort, config=None):
    """The arm's frozen parameters: its fallback start, where it decides, and the switch rule."""
    cfg = config or switch_policy.load()
    return {'S0': [model, effort], 'advisor': 'Jev (compat checkout): its model question unchanged, plus one effort question',
            'decision_points': cfg['decision_points'],
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
        self.last_sent, self.lock = {}, threading.Lock()  # task -> (setting, unix time): the cache it keeps warm

    def check_upstream(self, origin):
        pass  # ProxyServer accepts only direct Anthropic HTTPS or loopback HTTP (offline tests).

    def dispatcher(self, gov, rates):
        return ActiveDispatcher(gov, rates)

    def warm(self, task, setting, now):
        with self.lock:
            last = self.last_sent.get(task)
        return bool(last) and last[0] == setting and now - last[1] <= self.config['cache_ttl_seconds']

    def sent(self, task, setting, now):
        with self.lock:
            self.last_sent[task] = (tuple(setting), now)

    def decision_point(self, request, revision, proposal):
        if proposal['action'] != 'hold':  # the stuck detector sees evidence the current setting isn't enough
            return 'stuck_evidence', f"{revision}/stuck/{proposal['level']}"
        turns, starting = user_turns(request)
        return ('turn_start', f'{revision}/turn/{turns}') if starting else None

    def evidence(self, gov, task, setting):
        signals = gov.state.recommend(task).get('signals') or {}
        seen = [text for name, text in SIGNALS.items() if signals.get(name)]
        return (f'ModelPilot progress check: on {setting[0]} at {setting[1]} effort, ' +
                ('; '.join(seen) or 'progress has stalled') + '.')

    def decide(self, gov, rates, task, revision, point, request, current, setting, now):
        """Jev's advice and the switch decision for one decision point, journaled once and reused."""
        trigger, key = point
        earlier = [e['payload'] for e in gov.journal('advisor_decision') if e['task'] == task and e['payload']['point'] == key]
        if earlier:
            return earlier[-1]
        advice = None
        if self.advisor is not None:
            evidence = self.evidence(gov, task, setting) if trigger == 'stuck_evidence' else None
            advice = self.advisor.ask(request, setting[0], self.catalog, self.config['effort_order'],
                                      self.prompt_prefix, evidence)
        usable = advice if advice and not advice.get('error') else None
        prof = switch_policy.profile(self.config, current, self.warm(task, setting, now))
        decision = switch_policy.decide(self.config, rates, usable, setting, prof, trigger)
        decision.update(point=key, profile={k: prof[k] for k in ('prefix_tokens', 'messages_tokens', 'warm')},
                        advice=None if advice is None else
                        {k: advice.get(k) for k in ('model', 'effort', 'metrics', 'usage', 'router_model', 'ms', 'stub',
                                                    'error', 'prompt_chars', 'prompt_sha256', 'models')})
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
        point = self.decision_point(request, revision, proposal)
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
                self.sent(task, decision['target'], now)
                return dict(ticket, kind='escalation')
            deferral = ticket
        self.sent(task, setting, now)
        return self.keep(gov, rates, task, revision, client, setting, current, deferral)
