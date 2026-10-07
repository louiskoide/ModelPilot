"""ModelPilot's cost- and cache-aware execution policy: whether moving to Jev's recommendation pays.

Jev predicts where a task should run: a model and an effort, each with probabilities. This module
decides whether switching there is worth its cost, by comparing the expected total cost of staying
with that of each direct move:

    expected(c) = switch(current -> c) + P_ok(c) * run(c) + (1 - P_ok(c)) * recover(c)

- run(c): the remaining work on c: horizon requests, each reading the prefix, writing what it adds to
  the cache (its new input and the previous reply) and paying for its output; on a cold cache the first
  request also writes the prefix. Effort and model scale the number of requests and the output per
  request (measured factors in the config). The prefix grows by each request's input and output.
- switch: the one-time cache rewrite, beyond the read it replaces. A model change rewrites the whole
  prefix; an effort change rewrites what that model rewrites (config: full, messages or none); a
  cold cache costs nothing extra, since staying would write it too. With return_reuse enabled, a move
  back to a setting whose own entry is still warm, and within the measured reach of the conversation's
  newest breakpoint (return_reuse.max_positions content positions), writes only what that entry doesn't
  cover.
- candidates: staying and Jev's setting; when Jev is unsure of the model (or the effort), also Jev's
  effort (or model) alone, so a move can take the part Jev is sure of. Effort changes only where
  effort_changes_at allows (a turn start): inside a turn's tool loop it does not take effect, so at a
  mid-task step or on stuck evidence only the model moves, at the turn's effort.
- P_ok(c): Jev's model answer is the cheapest model that can finish in one pass, and its effort
  answer the lowest effort that can. So c is enough when both are at or below c's, treated as
  independent. With calibration, at the decision points it names, that estimate is blended with
  the pass rate measured on the tuning split for the strongest measured setting c is at least as
  strong as: (1 - jev_weight) x measured + jev_weight x Jev. On evidence that the current setting
  isn't enough, Jev's probabilities are conditioned on that, uncalibrated: the measured rates say
  nothing about which setting rescues a task the current one can't finish.
- recover(c): a failure wastes part of run(c), and the task is redone where Jev says it needs to be (its
  recommendation, when at least as strong as c), or else on the strongest setting a failure can reach.
  A failure shows inside the turn, where the effort can't change (unless effort_changes_at allows it on
  stuck evidence), so that is a stronger model at c's effort. That redo can fail too (then it is done
  again on the strongest), so staying on a setting likely to fail carries the same downstream risk as
  moving to Jev's setting now.

- delegation (config 'delegation', off by default): at the decision points consult.at lists, a consult
  candidate keeps the current setting and asks a stronger one (Jev's model at Jev's effort) once, on a
  bounded brief: consult + P_consult x run(current) + (1 - P_consult) x recover(current), with
  P_consult = P_ok(current) + effectiveness x (P_ok(target) - P_ok(current)). consult.force makes a consult at
  the step causes it lists whatever its price (measurement only). With handoff_note on, a model move
  whose request holds earlier work also pays for the note the model being left writes; its benefit isn't
  priced.

A move goes straight to its target and never climbs. It must beat staying by the hysteresis, scaled
by how plausible the current setting is: the margin protects a setting that may well be enough from
marginal moves, not one that is almost certain to fail. Downgrades also need a multiple of their
rewrite and enough confidence. Models, efforts, cache behaviour and the cost
model come from configs/modelpilot-policy.json and prices from the rate table; nothing here names
a model.
"""
import itertools
import json
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1]/'configs/modelpilot-policy.json'
TRIGGERS = ('turn_start', 'stuck_evidence', 'step')
REWRITES = ('full', 'messages', 'none')


def _count(value, floor):
    return type(value) is int and value >= floor


def _positive(value):
    return _at_least_number(value, 0) and value > 0


def load(path=CONFIG):
    cfg = json.loads(Path(path).read_text())
    order = cfg['effort_order']
    for name in ('effort_output_factor', 'effort_request_factor'):
        if set(cfg[name]) != set(order) or not all(_positive(v) for v in cfg[name].values()):
            raise ValueError(f'{name} must cover effort_order exactly, with positive numbers')
    if not all(_positive(spec.get(k, 1)) for spec in cfg['models'].values() for k in ('request_factor', 'output_factor')):
        raise ValueError('a model request_factor or output_factor must be a positive number')
    cal = cfg['calibration']
    measured = [k.split('/') for k in cal['outcomes']]
    if (type(cal['enabled']) is not bool or not (_at_least_number(cal['jev_weight'], 0) and cal['jev_weight'] <= 1)
            or not (isinstance(cal['applies_at'], list) and set(cal['applies_at']) <= {'turn_start', 'step'})
            or any(len(k) != 2 or k[0] not in cfg['models'] or k[1] not in order for k in measured)
            or not all(_count(o['trials'], 1) and _count(o['passed'], 0) and o['passed'] <= o['trials']
                       for o in cal['outcomes'].values())):
        raise ValueError('calibration: enabled is a boolean, jev_weight in [0, 1], applies_at among turn_start and step, outcomes '
                         "'model/effort' -> passed <= trials (integers, trials >= 1)")
    if not set(cfg['decision_points']) <= set(TRIGGERS):
        raise ValueError(f'decision_points must be among {TRIGGERS}')
    if type(gate(cfg)['enabled']) is not bool:
        raise ValueError('jev_gate: enabled is a boolean')
    step = cfg['step']
    if (type(step['enabled']) is not bool or not _count(step['min_requests_between'], 1)
            or not _count(step['max_per_revision'], 0) or not _at_least_number(step['overrun_factor'], 0)
            or not step['overrun_factor']):
        raise ValueError('step: enabled is a boolean, min_requests_between >= 1, max_per_revision >= 0, '
                         'overrun_factor > 0')
    if type(cfg['return_reuse']['enabled']) is not bool or not _count(cfg['return_reuse']['max_positions'], 0):
        raise ValueError('return_reuse: enabled is a boolean, max_positions an integer >= 0')
    if not (isinstance(cfg['effort_changes_at'], list) and set(cfg['effort_changes_at']) <= set(TRIGGERS)):
        raise ValueError(f'effort_changes_at must be a list of decision points among {TRIGGERS}')
    pme = cfg['per_message_effort']
    if (type(pme['enabled']) is not bool or pme['placement'] not in ('before_result', 'after_result')
            or not (pme['beta'] is None or isinstance(pme['beta'], str) and pme['beta'] and ',' not in pme['beta'])):
        raise ValueError('per_message_effort: enabled is a boolean, placement before_result or after_result, beta null '
                         'or one header value')
    _check_delegation(cfg)
    for model, spec in cfg['models'].items():
        if any(e not in order for e in spec['efforts']):
            raise ValueError(f'{model}: an effort is not in effort_order')
        if spec['effort_switch_rewrite'] not in REWRITES:
            raise ValueError(f'{model}: effort_switch_rewrite must be one of {REWRITES}')
    if not settings(cfg):
        raise ValueError('No candidate settings')
    return cfg


FORCE_CAUSES = ('tests_now_pass', 'tests_now_fail', 'spend_overrun', 'tests_pass', 'agent_finish')


def _check_delegation(cfg):
    consult, note = delegation(cfg, 'consult'), delegation(cfg, 'handoff_note')
    sizes = ('brief_max_bytes', 'test_output_bytes', 'advice_max_bytes', 'max_tokens', 'output_tokens', 'advice_tokens')
    if type(consult['enabled']) is not bool or (consult['enabled'] and not (
            isinstance(consult['at'], list) and set(consult['at']) <= {'step', 'stuck_evidence'}
            and isinstance(consult['force'], list) and set(consult['force']) <= set(FORCE_CAUSES)
            and _count(consult['max_per_revision'], 0) and all(_count(consult[k], 1) for k in sizes)
            and _at_least_number(consult['effectiveness'], 0) and consult['effectiveness'] <= 1
            and type(consult['route_brief']) is bool and _positive(consult['timeout_seconds']))):
        raise ValueError('delegation.consult: enabled is a boolean; when enabled, at among step and stuck_evidence, force among '
                         f'{FORCE_CAUSES}, max_per_revision >= 0, positive integer sizes, effectiveness in [0, 1], '
                         'route_brief a boolean, a positive timeout')
    if type(note['enabled']) is not bool or (note['enabled'] and not (
            all(_count(note[k], 1) for k in ('output_tokens', 'note_tokens', 'note_max_bytes', 'max_tokens'))
            and _positive(note['timeout_seconds']))):
        raise ValueError('delegation.handoff_note: enabled is a boolean; when enabled, positive integer sizes and timeout')


def with_overrides(cfg, overrides):
    """A copy of a loaded config with an arm's overrides merged in (nested dicts merge; other values replace), then
    checked as load() checks the file. Only keys the config already has can be overridden."""
    out = json.loads(json.dumps(cfg))

    def merge(into, update, where):
        for k, v in update.items():
            if k not in into:
                raise ValueError(f'Unknown policy setting {where}{k}')
            if isinstance(v, dict) and isinstance(into[k], dict):
                merge(into[k], v, f'{where}{k}.')
            else:
                into[k] = v
    merge(out, overrides or {}, '')
    _check_delegation(out)
    return out


def delegation(cfg, kind):
    """The consult or handoff_note settings; a config without them has delegation off."""
    return (cfg.get('delegation') or {}).get(kind) or {'enabled': False}


def gate(cfg):
    """The jev_gate settings; a config without them asks Jev at every decision point."""
    return cfg.get('jev_gate') or {'enabled': False}


def delegating(cfg):
    """Whether consults or handoff notes are on: only they speak to the agent, through the declared channel."""
    return any(delegation(cfg, kind)['enabled'] for kind in ('consult', 'handoff_note'))


def _rank(cfg, model):
    return cfg['models'][model]['rank']


def _effort_index(cfg, effort):
    return -1 if effort is None else cfg['effort_order'].index(effort)


def settings(cfg):
    """Every (model, effort) the policy may run: candidate models at each effort they accept."""
    models = sorted((m for m, spec in cfg['models'].items() if spec['candidate']), key=lambda m: _rank(cfg, m))
    return [(m, e) for m in models for e in (cfg['models'][m]['efforts'] or [None])]


def _smoothed(probabilities, labels, weight):
    known = {k: float(v) for k, v in (probabilities or {}).items()
             if k in labels and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0}
    total = sum(known.values())
    if total <= 0:
        return None
    return {k: (1 - weight) * known.get(k, 0) / total + weight / len(labels) for k in labels}


def sufficiency(cfg, advice):
    """P(model m is enough) and P(effort e is enough), from Jev's distributions (smoothed, so no setting is
    ever certain). Without an effort answer, effort doesn't enter the estimate."""
    models = sorted(cfg['models'], key=lambda m: _rank(cfg, m))
    pm = _smoothed((advice.get('model') or {}).get('probabilities'), models, cfg['jev_smoothing'])
    if pm is None:
        return None
    pe = _smoothed((advice.get('effort') or {}).get('probabilities'), cfg['effort_order'], cfg['jev_smoothing'])

    def model_ok(model):
        return min(1.0, sum(pm[m] for m in models if _rank(cfg, m) <= _rank(cfg, model)))

    def effort_ok(effort):
        if pe is None or effort is None:
            return 1.0
        return min(1.0, sum(pe[e] for e in cfg['effort_order'][:_effort_index(cfg, effort) + 1]))
    return model_ok, effort_ok


def content_positions(messages):
    """Content positions in messages, counted as the API's cache lookback counts them: each block is one, except
    that a run of tool_use blocks, or of tool_result blocks, is one; a string content is one block. A return's
    reach is the difference between the conversation's count now and when the setting was last sent."""
    count = 0
    for m in messages:
        content = m.get('content')
        blocks = [{'type': 'text'}] if isinstance(content, str) else content if isinstance(content, list) else []
        previous = None
        for b in blocks:
            kind = b.get('type') if isinstance(b, dict) else None
            if not (kind in ('tool_use', 'tool_result') and kind == previous):
                count += 1
            previous = kind
    return count


def profile(cfg, request, warm, observed=None, entries=None):
    """What the cost model needs about the conversation. Token counts from request bytes, as Jev estimates
    context; per-request input and output from observed usage when given, else the configured defaults.
    entries: {'model/effort': prefix tokens its still-warm, reachable cache entry covers}, for return_reuse."""
    per = cfg['bytes_per_token']
    out = dict(cfg['defaults'], **(observed or {}))
    out.update(prefix_tokens=len(json.dumps(request)) / per,
               messages_tokens=len(json.dumps(request.get('messages') or [])) / per, warm=bool(warm),
               warm_entries=dict(entries or {}))
    return out


def _scale(cfg, setting, prof):
    """(requests, output per request) on setting relative to the shape the profile describes (output_model at
    output_effort): measured effort and model factors from the config."""
    model, effort = setting
    requests = output = 1.0
    base_effort, base_model = prof.get('output_effort'), prof.get('output_model')
    if effort is not None and base_effort:
        requests = cfg['effort_request_factor'][effort] / cfg['effort_request_factor'][base_effort]
        output = cfg['effort_output_factor'][effort] / cfg['effort_output_factor'][base_effort]
    if base_model and base_model != model:
        spec, base = cfg['models'][model], cfg['models'][base_model]
        requests *= spec.get('request_factor', 1) / base.get('request_factor', 1)
        output *= spec.get('output_factor', 1) / base.get('output_factor', 1)
    return requests, output


def run_cost(cfg, rates, setting, prof):
    rate = rates[setting[0]]
    requests, output = _scale(cfg, setting, prof)
    horizon = max(1.0, prof['horizon_requests'] * requests)
    reply = prof['output_tokens'] * output
    growth = prof['new_input_tokens'] + reply  # written to the cache by the next request
    prefix = prof['prefix_tokens'] + (horizon - 1) / 2 * growth  # average prefix over the remaining requests
    write = rate['write_' + cfg['cache_write_ttl']]
    cold = 0.0 if prof['warm'] else prof['prefix_tokens'] * (write - rate['read'])  # the first request writes it
    return (horizon * (prefix * rate['read'] + growth * write + reply * rate['output']) + cold) / 1e6


def switch_cost(cfg, rates, current, target, prof, warm=None, reuse=False):
    """The rewrite a move pays beyond the read it replaces; nothing when the cache is cold or nothing changes.
    reuse: the move happens now, so a target entry that is still warm counts (when return_reuse is enabled);
    a later, hypothetical move (a failure's redo) never counts on one."""
    warm = prof['warm'] if warm is None else warm
    if not warm or tuple(current) == tuple(target):
        return 0.0
    rate = rates[target[0]]
    extra = rate['write_' + cfg['cache_write_ttl']] - rate['read']
    if current[0] != target[0]:
        tokens = prof['prefix_tokens']
    else:
        scope = 'none' if per_message(cfg, target[0]) else cfg['models'][target[0]]['effort_switch_rewrite']
        tokens = {'full': prof['prefix_tokens'], 'messages': prof['messages_tokens'], 'none': 0}[scope]
    covered = None
    if reuse and cfg['return_reuse']['enabled']:
        entries = prof.get('warm_entries', {})
        covered = entries.get(_label(target))
        if covered is None and per_message(cfg, target[0]):
            covered = entries.get(f'{target[0]}/*')  # any effort of that model: its cache doesn't depend on effort
    if covered is not None:
        tokens = min(tokens, max(0.0, prof['prefix_tokens'] - covered))
    return tokens * extra / 1e6


def _horizon(cfg, setting, prof):
    return max(1.0, prof['horizon_requests'] * _scale(cfg, setting, prof)[0])


def consult_cost(cfg, rates, current, target, prof):
    """One consult: the brief at the target's uncached input price, its output (thinking included) at the target's
    output price, scaled by effort, and the advice written into the main conversation once and read after."""
    spec, write = delegation(cfg, 'consult'), 'write_' + cfg['cache_write_ttl']
    brief = prof.get('brief_tokens') or spec['brief_max_bytes'] / cfg['bytes_per_token']
    base = prof.get('output_effort')
    output = spec['output_tokens'] * (cfg['effort_output_factor'][target[1]] / cfg['effort_output_factor'][base]
                                      if target[1] and base else 1.0)
    rt, rc = rates[target[0]], rates[current[0]]
    advice = spec['advice_tokens'] * (rc[write] + (_horizon(cfg, current, prof) - 1) * rc['read'])
    return (brief * rt['input'] + output * rt['output'] + advice) / 1e6


def note_cost(cfg, rates, current, target, prof):
    """A handoff note before a model move: the model being left reads its prefix and writes the note, which the
    target then writes into its conversation and reads after. Nothing for a move that keeps the model, or before any
    work (no earlier assistant turn), or with notes off."""
    spec = delegation(cfg, 'handoff_note')
    if not spec['enabled'] or current[0] == target[0] or not prof.get('history'):
        return 0.0
    rc, rt, write = rates[current[0]], rates[target[0]], 'write_' + cfg['cache_write_ttl']
    return (prof['prefix_tokens'] * rc['read'] + spec['output_tokens'] * rc['output'] +
            spec['note_tokens'] * (rt[write] + (_horizon(cfg, target, prof) - 1) * rt['read'])) / 1e6


def measured_ok(cfg, setting):
    """The tuning split's pass rate, with a uniform prior ((passed + 1) / (trials + 2)), of the strongest measured
    setting that setting is at least as strong as; None when no measured setting is that weak."""
    found = [(o['passed'] + 1) / (o['trials'] + 2) for key, o in cfg['calibration']['outcomes'].items()
             if at_least(cfg, tuple(setting), tuple(key.split('/')))]
    return max(found) if found else None


def per_message(cfg, model):
    """Effort changes on this model go through effort-only system messages, which rewrite nothing."""
    return cfg['per_message_effort']['enabled'] and bool(cfg['models'][model].get('per_message_effort'))


def is_downgrade(cfg, target, current):
    if _rank(cfg, target[0]) != _rank(cfg, current[0]):
        return _rank(cfg, target[0]) < _rank(cfg, current[0])
    return _effort_index(cfg, target[1]) < _effort_index(cfg, current[1])


def at_least(cfg, target, current):
    return (_rank(cfg, target[0]) >= _rank(cfg, current[0]) and
            _effort_index(cfg, target[1]) >= _effort_index(cfg, current[1]))


def _at_least_number(value, floor):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= floor


def _label(setting):
    return f'{setting[0]}/{setting[1]}'


def _consult_target(cfg, current, model, effort, forced=False):
    """Who a consult asks: Jev's model at Jev's effort (a separate request runs at any effort), when that is stronger
    than the current setting; for a forced consult, else the strongest model at that effort or the current one."""
    every = settings(cfg)
    strongest = max((m for m, _ in every), key=lambda m: _rank(cfg, m))
    for m, e in [(model, effort)] + ([(strongest, effort), (strongest, current[1])] if forced else []):
        if m not in cfg['models']:
            continue
        c = (m, e if cfg['models'][m]['efforts'] else None)
        if c in every and c != tuple(current) and at_least(cfg, c, current):
            return c
    return None


def decide(cfg, rates, advice, current, prof, trigger):
    """Stay, jump straight to a target, or stop (stuck with nothing stronger). Returns the decision with every
    candidate's numbers, so the journal shows why, and forecast_usd: run() on the setting it leaves the task on,
    which a later step compares measured spend against."""
    decision = _decide(cfg, rates, advice, current, prof, trigger)
    decision['forecast_usd'] = run_cost(cfg, rates, tuple(decision['target']), prof)
    return decision


def answer_shapes(cfg):
    """Every way Jev's answer can shape the candidates: each model and effort choice, sure (1) or unsure (0) of each,
    or no effort answer. Its probabilities don't matter here; jev_gate bounds them."""
    models, efforts = list(cfg['models']), cfg['effort_order']
    for choice, sure in itertools.product(models, (0.0, 1.0)):
        model = {'choice': choice, 'confidence': sure, 'probabilities': {m: float(m == choice) for m in models}}
        yield {'model': model}
        for e_choice, e_sure in itertools.product(efforts, (0.0, 1.0)):
            yield {'model': model, 'effort': {'choice': e_choice, 'confidence': e_sure,
                                              'probabilities': {e: float(e == e_choice) for e in efforts}}}


def jev_gate(cfg, rates, current, prof, trigger):
    """Whether some answer Jev could give changes the decision at this point (config jev_gate). Jev's choices and
    confidences pick the candidates and where a failure is redone; its probabilities only set each setting's
    estimate, which lies in [0, 1]. With a failure wasting its whole run (recovery.wasted_fraction 1), every expected
    cost falls as any P_ok rises, so staying is at its dearest, and its hysteresis lowest, with Jev's estimate 0
    everywhere, and a candidate at its cheapest with 1. If no candidate beats staying by the hysteresis even then, for
    every answer shape, the decision stays whatever Jev says. The bound ignores a downgrade's extra hysteresis and its
    confidence check, so the gate errs to asking. Returns {'can_change': bool, 'reason': ...}: True without
    calibration at this point (Jev's estimate is then the whole of P_ok), where a consult is possible (its target is
    Jev's setting), or where the bound doesn't hold (wasted_fraction below 1)."""
    cal, consult = cfg['calibration'], delegation(cfg, 'consult')
    if not (cal['enabled'] and trigger in cal['applies_at']):
        return {'can_change': True, 'reason': 'uncalibrated'}
    if consult['enabled'] and trigger in consult['at']:
        return {'can_change': True, 'reason': 'consult_possible'}
    if cfg['recovery']['wasted_fraction'] < 1:
        return {'can_change': True, 'reason': 'not_monotone'}
    closest, shapes = None, 0
    for advice in answer_shapes(cfg):
        shapes += 1
        low, high = (_decide(cfg, rates, advice, current, prof, trigger, jev_estimate=x)['candidates'] for x in (0.0, 1.0))
        stay, required = low[0]['expected_usd'], cfg['hysteresis_usd'] * low[0]['p_ok']
        for row in high[1:]:
            gap = stay - row['expected_usd'] - required
            closest = gap if closest is None else max(closest, gap)
            if gap > 0:
                return {'can_change': True, 'reason': 'a_move_can_pay', 'shapes': shapes, 'closest_usd': gap}
    return {'can_change': False, 'reason': 'every_answer_stays', 'shapes': shapes, 'closest_usd': closest}


def _decide(cfg, rates, advice, current, prof, trigger, jev_estimate=None):
    """jev_estimate: Jev's estimate for every setting, in place of its probabilities (jev_gate's bounds)."""
    if trigger not in TRIGGERS:
        raise ValueError(f'Unknown decision point {trigger!r}')
    current = tuple(current)
    out = {'trigger': trigger, 'current': list(current), 'target': list(current), 'applied': False}
    runnable = settings(cfg)
    # Effort is set per user turn: an effort change inside a turn's tool loop does not take effect (per-message-effort
    # probe, September 30), so elsewhere only the model can move, at the turn's effort.
    effort_fixed = trigger not in cfg['effort_changes_at']
    if effort_fixed:
        runnable = [c for c in runnable if c[1] == current[1] or c == current]
    probabilities = sufficiency(cfg, advice) if advice else None
    if probabilities is None:
        return dict(out, action='stay', reason='advice_unavailable')
    model_ok, effort_ok = probabilities
    if effort_fixed:  # every candidate runs at the turn's effort, which can't change here: compare the models only
        effort_ok = lambda effort: 1.0
    model_answer, effort_answer = advice.get('model') or {}, advice.get('effort') or {}
    out['jev'] = {'model': model_answer.get('choice'), 'model_confidence': model_answer.get('confidence'),
                  'effort': effort_answer.get('choice'), 'effort_confidence': effort_answer.get('confidence')}
    cal = cfg['calibration']
    calibrated = cal['enabled'] and trigger in cal['applies_at']
    if calibrated:
        out['calibration'] = {'jev_weight': cal['jev_weight']}

    def jev_ok(setting):
        return model_ok(setting[0]) * effort_ok(setting[1]) if jev_estimate is None else jev_estimate

    def p_ok(setting):
        measured = measured_ok(cfg, setting) if calibrated else None
        jev = jev_ok(setting)
        return jev if measured is None else (1 - cal['jev_weight']) * measured + cal['jev_weight'] * jev
    if trigger == 'stuck_evidence':  # never calibrated (see load): the measured rates can't say what rescues a task
        stronger = [c for c in runnable if c != current and at_least(cfg, c, current)]
        failed = jev_ok(current)
        if not stronger or failed >= 1:
            return dict(out, action='stop', reason='no_stronger_setting')

        def probability(setting):  # P(setting is enough | the current setting was not), under independence
            lower_model = min(setting[0], current[0], key=lambda m: _rank(cfg, m))
            lower_effort = min(setting[1], current[1], key=lambda e: _effort_index(cfg, e))
            both = model_ok(lower_model) * effort_ok(lower_effort)
            return max(0.0, jev_ok(setting) - both) / (1 - failed)
        candidates = [current] + stronger
    else:
        probability = p_ok
        candidates = [current]
    jm = model_answer.get('choice')
    je = effort_answer.get('choice') or current[1]
    sure = cfg['direct_jump_confidence']
    moves = [(jm, je)]  # Jev's setting; a partial move only in a dimension Jev is unsure of, or its effort at a step
    if not _at_least_number(effort_answer.get('confidence'), sure):
        moves.append((jm, current[1]))
    if trigger == 'step' or not _at_least_number(model_answer.get('confidence'), sure):
        moves.append((current[0], je))
    if effort_fixed:
        moves = [(m, current[1]) for m, _ in moves]
    moves = [(m, e if m in cfg['models'] and cfg['models'][m]['efforts'] else None) for m, e in moves]
    pick = moves[0] if moves[0] in runnable else None
    if trigger in ('turn_start', 'step'):
        candidates += [c for c in dict.fromkeys(moves) if c in runnable and c != current]

    wasted = cfg['recovery']['wasted_fraction']
    every = settings(cfg)

    def reachable(setting):  # where a failure, found inside the turn, can be redone: the turn's effort holds there
        if 'stuck_evidence' in cfg['effort_changes_at']:
            return every
        return [c for c in every if c[1] == setting[1]] or [setting]

    def strength(c):
        return _rank(cfg, c[0]), _effort_index(cfg, c[1])
    overall = max(every, key=strength)

    def redo(setting):  # where a failure of setting is redone: a stronger model inside the turn, else a new turn
        inside = max(reachable(setting), key=strength)
        if inside != setting:
            return inside
        if setting == overall:
            return None
        return pick if pick and pick != setting and at_least(cfg, pick, setting) else overall  # the effort can change

    def recovery(setting):  # where a failed setting's work is redone first
        if pick and pick != setting and at_least(cfg, pick, setting) and pick in reachable(setting):
            return pick
        return redo(setting) or setting

    def from_scratch(setting):  # the work done on setting alone, redone further up while it fails
        run, nxt = run_cost(cfg, rates, setting, prof), redo(setting)
        if nxt is None:
            return run
        p = probability(setting)
        return p * run + (1 - p) * (wasted * run + switch_cost(cfg, rates, setting, nxt, prof, warm=True) +
                                    from_scratch(nxt))
    rows = []
    for c in candidates:
        run = run_cost(cfg, rates, c, prof)
        r = recovery(c)
        recover = wasted * run + switch_cost(cfg, rates, c, r, prof, warm=True) + from_scratch(r)
        p, switch = probability(c), switch_cost(cfg, rates, current, c, prof, reuse=True)
        note = note_cost(cfg, rates, current, c, prof)  # the handoff note the model being left writes, if on
        row = {'setting': _label(c), 'p_ok': p, 'switch_usd': switch + note, 'run_usd': run, 'recover_usd': recover,
               'expected_usd': switch + note + p * run + (1 - p) * recover, '_setting': c}
        if note:
            row['note_usd'] = note
        if calibrated:
            row.update(p_jev=jev_ok(c), p_measured=measured_ok(cfg, c))
        rows.append(row)
    stay = rows[0]
    consult = delegation(cfg, 'consult')
    asked = (consult['enabled'] and trigger in consult['at']
             and prof.get('consults_left', consult.get('max_per_revision', 0)) > 0)
    forced = bool(asked and prof.get('force_consult'))
    adviser = _consult_target(cfg, current, jm, je, forced) if asked else None
    if adviser:  # stay, and ask a stronger setting once on a bounded brief
        p = stay['p_ok'] + consult['effectiveness'] * max(0.0, probability(adviser) - stay['p_ok'])
        cost = consult_cost(cfg, rates, current, adviser, prof)
        rows.append({'setting': 'consult:' + _label(adviser), 'p_ok': p, 'switch_usd': 0.0, 'consult_usd': cost,
                     'run_usd': stay['run_usd'], 'recover_usd': stay['recover_usd'],
                     'expected_usd': cost + p * stay['run_usd'] + (1 - p) * stay['recover_usd'], '_setting': current})
    out['candidates'] = [{k: v for k, v in r.items() if k != '_setting'} for r in rows]
    if forced and adviser:  # measurement only: consult.force names this step's cause
        return dict(out, action='consult', consult=list(adviser), reason='forced')
    best = min(rows, key=lambda r: r['expected_usd'])
    if best is stay:
        return dict(out, action='stay', reason='current_is_cheapest')
    if adviser and best is rows[-1]:
        benefit, required = stay['expected_usd'] - best['expected_usd'], cfg['hysteresis_usd'] * stay['p_ok']
        out.update(benefit_usd=benefit, required_usd=required, downgrade=False)
        if benefit <= required:
            return dict(out, action='stay', reason='not_worth_switching')
        return dict(out, action='consult', consult=list(adviser), reason='expected_cost_lower')
    target = best['_setting']
    downgrade = is_downgrade(cfg, target, current)
    if downgrade:
        confidence = (out['jev']['model_confidence'] if target[0] != current[0] else out['jev']['effort_confidence'])
        if not isinstance(confidence, (int, float)) or confidence < cfg['min_confidence_to_downgrade']:
            return dict(out, action='stay', reason='low_confidence_no_downgrade')
    benefit = stay['expected_usd'] - best['expected_usd']
    required = (cfg['hysteresis_usd'] * stay['p_ok'] +
                (cfg['downgrade_switch_multiplier'] - 1) * best['switch_usd'] * downgrade)
    out.update(benefit_usd=benefit, required_usd=required, downgrade=downgrade)
    if benefit <= required:
        return dict(out, action='stay', reason='not_worth_switching')
    return dict(out, action='jump', target=list(target), reason='expected_cost_lower')
