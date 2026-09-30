"""ModelPilot's cost- and cache-aware execution policy: whether moving to Jev's recommendation pays.

Jev predicts where a task should run: a model and an effort, each with probabilities. This module
decides whether switching there is worth its cost, by comparing the expected total cost of staying
with that of each direct move:

    expected(c) = switch(current -> c) + P_ok(c) * run(c) + (1 - P_ok(c)) * recover(c)

- run(c): the remaining work on c, horizon x (prefix x read + new input x input + output x effort
  factor x output). The prefix grows by each request's input and output.
- switch: the one-time cache rewrite, beyond the read it replaces. A model change rewrites the whole
  prefix; an effort change rewrites what that model rewrites (config: full, messages or none); a
  cold cache costs nothing extra, since staying would write it too. With return_reuse enabled, a move
  back to a setting whose own entry is still warm, and within the measured reach of the conversation's
  newest breakpoint (return_reuse.max_positions content positions), writes only what that entry doesn't
  cover.
- candidates: staying and Jev's setting; when Jev is unsure of the model (or the effort), also Jev's
  effort (or model) alone, so a move can take the part Jev is sure of. When it is sure of both, nothing
  cheaper is tried first, except at a mid-task step, where Jev's effort on the current model is always
  weighed: an effort change rewrites less than a model change.
- P_ok(c): Jev's model answer is the cheapest model that can finish in one pass, and its effort
  answer the lowest effort that can. So c is enough when both are at or below c's, treated as
  independent. On evidence that the current setting isn't enough, the probabilities are
  conditioned on that.
- recover(c): a failure wastes part of run(c), and the task is redone where Jev says it needs to be (its
  recommendation, when at least as strong as c), or else on the strongest setting. That redo can fail
  too (then it is done again on the strongest setting), so staying on a setting likely to fail carries
  the same downstream risk as moving to Jev's setting now.

A move goes straight to its target and never climbs. It must beat staying by the hysteresis, scaled
by how plausible the current setting is: the margin protects a setting that may well be enough from
marginal moves, not one that is almost certain to fail. Downgrades also need a multiple of their
rewrite and enough confidence. Models, efforts, cache behaviour and the cost
model come from configs/modelpilot-policy.json and prices from the rate table; nothing here names
a model.
"""
import json
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1]/'configs/modelpilot-policy.json'
TRIGGERS = ('turn_start', 'stuck_evidence', 'step')
REWRITES = ('full', 'messages', 'none')


def _count(value, floor):
    return type(value) is int and value >= floor


def load(path=CONFIG):
    cfg = json.loads(Path(path).read_text())
    order = cfg['effort_order']
    if set(cfg['effort_output_factor']) != set(order):
        raise ValueError('effort_output_factor must cover effort_order exactly')
    if not set(cfg['decision_points']) <= set(TRIGGERS):
        raise ValueError(f'decision_points must be among {TRIGGERS}')
    step = cfg['step']
    if (type(step['enabled']) is not bool or not _count(step['min_requests_between'], 1)
            or not _count(step['max_per_revision'], 0) or not _at_least_number(step['overrun_factor'], 0)
            or not step['overrun_factor']):
        raise ValueError('step: enabled is a boolean, min_requests_between >= 1, max_per_revision >= 0, '
                         'overrun_factor > 0')
    if type(cfg['return_reuse']['enabled']) is not bool or not _count(cfg['return_reuse']['max_positions'], 0):
        raise ValueError('return_reuse: enabled is a boolean, max_positions an integer >= 0')
    pme = cfg['per_message_effort']
    if (type(pme['enabled']) is not bool or pme['placement'] not in ('before_result', 'after_result')
            or not (pme['beta'] is None or isinstance(pme['beta'], str) and pme['beta'] and ',' not in pme['beta'])):
        raise ValueError('per_message_effort: enabled is a boolean, placement before_result or after_result, beta null '
                         'or one header value')
    for model, spec in cfg['models'].items():
        if any(e not in order for e in spec['efforts']):
            raise ValueError(f'{model}: an effort is not in effort_order')
        if spec['effort_switch_rewrite'] not in REWRITES:
            raise ValueError(f'{model}: effort_switch_rewrite must be one of {REWRITES}')
    if not settings(cfg):
        raise ValueError('No candidate settings')
    return cfg


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


def run_cost(cfg, rates, setting, prof):
    model, effort = setting
    rate = rates[model]
    factor = 1.0
    if effort is not None and prof.get('output_effort'):
        factor = cfg['effort_output_factor'][effort] / cfg['effort_output_factor'][prof['output_effort']]
    horizon = prof['horizon_requests']
    growth = prof['new_input_tokens'] + prof['output_tokens'] * factor
    prefix = prof['prefix_tokens'] + (horizon - 1) / 2 * growth  # average prefix over the remaining requests
    return horizon * (prefix * rate['read'] + prof['new_input_tokens'] * rate['input'] +
                      prof['output_tokens'] * factor * rate['output']) / 1e6


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


def decide(cfg, rates, advice, current, prof, trigger):
    """Stay, jump straight to a target, or stop (stuck with nothing stronger). Returns the decision with every
    candidate's numbers, so the journal shows why, and forecast_usd: run() on the setting it leaves the task on,
    which a later step compares measured spend against."""
    decision = _decide(cfg, rates, advice, current, prof, trigger)
    decision['forecast_usd'] = run_cost(cfg, rates, tuple(decision['target']), prof)
    return decision


def _decide(cfg, rates, advice, current, prof, trigger):
    if trigger not in TRIGGERS:
        raise ValueError(f'Unknown decision point {trigger!r}')
    current = tuple(current)
    out = {'trigger': trigger, 'current': list(current), 'target': list(current), 'applied': False}
    runnable = settings(cfg)
    probabilities = sufficiency(cfg, advice) if advice else None
    if probabilities is None:
        return dict(out, action='stay', reason='advice_unavailable')
    model_ok, effort_ok = probabilities
    model_answer, effort_answer = advice.get('model') or {}, advice.get('effort') or {}
    out['jev'] = {'model': model_answer.get('choice'), 'model_confidence': model_answer.get('confidence'),
                  'effort': effort_answer.get('choice'), 'effort_confidence': effort_answer.get('confidence')}

    def p_ok(setting):
        return model_ok(setting[0]) * effort_ok(setting[1])
    if trigger == 'stuck_evidence':
        stronger = [c for c in runnable if c != current and at_least(cfg, c, current)]
        failed = p_ok(current)
        if not stronger or failed >= 1:
            return dict(out, action='stop', reason='no_stronger_setting')

        def probability(setting):  # P(setting is enough | the current setting was not), under independence
            lower_model = min(setting[0], current[0], key=lambda m: _rank(cfg, m))
            lower_effort = min(setting[1], current[1], key=lambda e: _effort_index(cfg, e))
            both = model_ok(lower_model) * effort_ok(lower_effort)
            return max(0.0, p_ok(setting) - both) / (1 - failed)
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
    moves = [(m, e if m in cfg['models'] and cfg['models'][m]['efforts'] else None) for m, e in moves]
    pick = moves[0] if moves[0] in runnable else None
    if trigger in ('turn_start', 'step'):
        candidates += [c for c in dict.fromkeys(moves) if c in runnable and c != current]
    strongest = max(runnable, key=lambda c: (_rank(cfg, c[0]), _effort_index(cfg, c[1])))

    wasted = cfg['recovery']['wasted_fraction']

    def recovery(setting):  # where a failed setting's work is redone
        return pick if pick and pick != setting and at_least(cfg, pick, setting) else strongest

    def from_scratch(setting):  # the work done on setting alone, redone on the strongest if it fails too
        run = run_cost(cfg, rates, setting, prof)
        if setting == strongest:
            return run
        p = probability(setting)
        return p * run + (1 - p) * (wasted * run + switch_cost(cfg, rates, setting, strongest, prof, warm=True) +
                                    run_cost(cfg, rates, strongest, prof))
    rows = []
    for c in candidates:
        run = run_cost(cfg, rates, c, prof)
        r = recovery(c)
        recover = wasted * run + switch_cost(cfg, rates, c, r, prof, warm=True) + from_scratch(r)
        p, switch = probability(c), switch_cost(cfg, rates, current, c, prof, reuse=True)
        rows.append({'setting': _label(c), 'p_ok': p, 'switch_usd': switch, 'run_usd': run, 'recover_usd': recover,
                     'expected_usd': switch + p * run + (1 - p) * recover, '_setting': c})
    out['candidates'] = [{k: v for k, v in r.items() if k != '_setting'} for r in rows]
    stay = rows[0]
    best = min(rows, key=lambda r: r['expected_usd'])
    if best is stay:
        return dict(out, action='stay', reason='current_is_cheapest')
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
