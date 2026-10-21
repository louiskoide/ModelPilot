"""Switch quote (October 19, $0, dry-run): what moving the session to another model/effort costs, and whether a
delegated helper on that setting is the cheaper way to get the same work done. Never applies a switch.

  python3 -m modelpilot.switch_quote --from claude-sonnet-5-5/low --to claude-opus-5-5/high --prefix-tokens 120000

rewrite: the cache rewrite the switch pays beyond the read it replaces (`switch_policy.switch_cost`, target cold).
delegate: a helper on the target setting starts with no history: its own system prompt and tools, its brief and what it
re-reads are written once at the target's rate, plus its output; the main session also writes the brief and reads the
result. All token counts are guesses to override from the ledger (`LIMITS`), except the helper's base, measured on the
plugin's helper (October 20: about 30,000 characters of system prompt and tools on client 2.1.284, at about 4 a token);
the helper does not hold the session's conversation.
"""
import argparse
import json
import sys
from . import switch_policy as sp

# Guesses, not measurements. warn_usd: a rewrite above this is flagged. max_helper_tokens: a helper whose context
# would exceed this, its base aside, is not offered (it would re-read too much to stay cheap). margin: delegation must
# beat the rewrite by this fraction to be recommended.
LIMITS = {'warn_usd': 0.10, 'max_helper_tokens': 30000, 'margin': 0.25, 'helper_base_tokens': 7500,
          'brief_tokens': 1500, 'reread_tokens': 8000, 'helper_output_tokens': 1500, 'result_tokens': 600}


def quote(current, target, prefix_tokens, limits=None, cfg=None, rates=None):
    lim = dict(LIMITS, **(limits or {}))
    from . import bench  # late: bench imports hooks, which imports this module
    cfg = cfg or sp.load()
    rates = rates or bench.rates()
    prof = sp.profile(cfg, {}, True)
    prof.update(prefix_tokens=prefix_tokens, messages_tokens=prefix_tokens)
    rewrite = sp.switch_cost(cfg, rates, current, target, prof)
    helper_tokens = lim['brief_tokens'] + lim['reread_tokens']
    t, c = sp._rate(rates, target[0], helper_tokens), sp._rate(rates, current[0], prefix_tokens)
    scale = cfg['effort_output_factor'][target[1]] * cfg['models'][target[0]].get('output_factor', 1)
    delegate = ((lim['helper_base_tokens'] + helper_tokens) * t['write_5m'] + lim['helper_output_tokens'] * scale * t['output'] +
                lim['brief_tokens'] * c['output'] + lim['result_tokens'] * c['read']) / 1e6
    if rewrite <= lim['warn_usd']:
        action, why = 'switch', 'rewrite at or under the warning level'
    elif helper_tokens <= lim['max_helper_tokens'] and delegate <= rewrite * (1 - lim['margin']):
        action, why = 'delegate', 'a helper costs clearly less than the rewrite'
    else:
        action, why = 'warn', 'rewrite is expensive and a helper is not clearly cheaper or is over its size limit'
    return {'current': list(current), 'target': list(target), 'prefix_tokens': prefix_tokens, 'rewrite_usd': rewrite,
            'delegate_usd': delegate, 'action': action, 'reason': why, 'applied': False,
            'assumptions': 'target cold, 5m write, guessed helper sizes (LIMITS) beside its measured base; a helper has '
                           'no conversation history'}


def from_ledger(gov, changes, effort=None, limits=None):
    """Quote for a plan's model/effort changes, on the ledger's newest observed prefix; None when there is nothing to
    quote (no model/effort change, or no main-loop request seen yet). effort: the session's current effort."""
    ctx = gov.latest_context()
    want = {c['kind']: c['value'] for c in changes if c['kind'] in ('model', 'effort')}
    if not want or not ctx:
        return None
    current = (ctx['model'], effort or want.get('effort', 'medium'))
    target = (want.get('model', ctx['model']), want.get('effort', current[1]))
    cfg = sp.load()
    if not {current[0], target[0]} <= set(cfg['models']) or current[1] not in cfg['effort_order'] \
            or target[1] not in cfg['effort_order']:
        return None  # a setting the policy doesn't price (superseded model): no quote, never a guess
    return quote(current, target, ctx['prefix_tokens'], limits, cfg)


def describe(q):
    """One line for a hook notice."""
    head = f"Estimated switch cost: cache rewrite ${q['rewrite_usd']:.3f}, delegated helper ${q['delegate_usd']:.3f}."
    tail = {'switch': 'Cheap enough to switch.',
            'delegate': 'Expensive to switch: a helper on the new setting is much cheaper for a self-contained task '
                        '(it has no conversation history).',
            'warn': 'Expensive to switch, and a helper is not clearly cheaper.'}[q['action']]
    return f'{head} {tail}'


def setting(text):
    model, _, effort = text.partition('/')
    return model, effort or 'medium'


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--from', dest='src', required=True, help='MODEL/EFFORT')
    p.add_argument('--to', required=True, help='MODEL/EFFORT')
    p.add_argument('--prefix-tokens', type=int, required=True)
    for k in LIMITS:
        p.add_argument('--' + k.replace('_', '-'), type=float)
    a = p.parse_args(argv)
    limits = {k: getattr(a, k) for k in LIMITS if getattr(a, k) is not None}
    print(json.dumps(quote(setting(a.src), setting(a.to), a.prefix_tokens, limits), indent=2))


if __name__ == '__main__':
    sys.exit(main())
