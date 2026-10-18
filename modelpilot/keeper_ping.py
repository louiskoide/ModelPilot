"""Keeper ping check (October 17; paid behind --live, about $0.10, under $0.30 with --renewal): what a cache keeper's
ping costs, and whether the entry stays warm. The transcript replay's go (docs/governor.md, "Cache keeper: result")
priced a ping at 20 written tokens and 1 output token. A ping must repeat the last request's model, effort and
thinking, since changing them writes a new prefix, so at Opus 5.5 xhigh it may think before it stops: at 1.1 cents
more a ping the replay's best setting falls to 5%.

    python3 -m modelpilot.keeper_ping [--renewal] [--budget 1] [--out DIR] [--live]

Direct API with an API key (asked hidden when ANTHROPIC_API_KEY is unset; a subscription token is refused), Opus 5.5 at
effort xhigh with adaptive thinking, as the user's own sessions run, and a nonce-led system prefix of about 8,000
tokens cached for 1h (cache_probe.payload). One request a step, no retries:
1. seed: max_tokens 1024; writes the prefix.
2. ping: the same body at max_tokens 1, then 16, 64 and 256 while the API refuses one with a 400 (a refusal's cost is
   unknown). Any other error stops the run.
3. stop_check (when the ping was accepted below 16): the same body at max_tokens 16, right after the ping. The first
   run's 1-token ping ended `refusal` with no output (runs/keeper-ping-20261017-215955); this shows whether a ping
   that can produce a token stops normally. A 400 here is recorded and the run goes on.
4. again: the seed's body; reads the prefix back if the entry is warm.
With --renewal the ping waits 55 minutes after the seed and `again` 55 minutes after the ping (about 1 h 50 min in all),
so a whole read shows a ping keeping an entry past its hour.
"""
import argparse
import getpass
import json
import os
import time
import urllib.error
import uuid
from pathlib import Path
from . import bench
from .cache_probe import account_problem, cost, error_details, payload, send

ROOT = Path(__file__).resolve().parents[1]
MODEL, EFFORT = 'claude-opus-5-5', 'xhigh'
PREFIX_LINES = 400
SEED_MAX_TOKENS = 1024
PING_LADDER = (1, 16, 64, 256)
STOP_CHECK_MAX_TOKENS = 16
WAIT_SECONDS = 3300
# The replay's best setting, K_all(4h) (runs/keeper-replay-20261017.json): its net, its pings and the month's total.
REPLAY = {'net_usd': 71.69, 'pings': 1463, 'total_usd': 1110.01, 'go_share': 0.05}
PING_WRITE_TOKENS, PING_OUTPUT_TOKENS = 20, 1


def config():
    return {'prefix_lines': PREFIX_LINES, 'max_tokens': SEED_MAX_TOKENS, 'thinking': {'type': 'adaptive'}}


def steps(renewal):
    wait = f' after {WAIT_SECONDS // 60} minutes' if renewal else ''
    return ([('seed', SEED_MAX_TOKENS, 0)] + [('ping', m, WAIT_SECONDS if renewal and i == 0 else 0)
                                             for i, m in enumerate(PING_LADDER)]
            + [('stop_check', STOP_CHECK_MAX_TOKENS, 0), ('again', SEED_MAX_TOKENS, WAIT_SECONDS if renewal else 0)]), wait


def estimate_usd(body, max_tokens, rate):
    """Pessimistic: about 3 request bytes a token, all written at the 1h rate, and the whole max_tokens as output."""
    return (len(json.dumps(body)) / 3 * rate['write_1h'] + max_tokens * rate['output']) / 1e6


def summarize(rows, rate, renewal):
    seed = next((r for r in rows if r['step'] == 'seed' and r.get('usage')), None)
    ping = next((r for r in rows if r['step'] == 'ping' and r.get('usage')), None)
    again = next((r for r in rows if r['step'] == 'again' and r.get('usage')), None)
    check = next((r for r in rows if r['step'] == 'stop_check'), None)
    out = {'ping_accepted_max_tokens': ping and ping['max_tokens'], 'ping_stop_reason': ping and ping['stop_reason'],
           'stop_check': check and {'max_tokens': check['max_tokens'], 'http_status': check.get('http_status'),
                                    'stop_reason': check.get('stop_reason'), 'error': check.get('error'),
                                    'output_tokens': (check.get('usage') or {}).get('output_tokens'),
                                    'written': (check.get('usage') or {}).get('cache_creation_input_tokens'),
                                    'cost_usd': check.get('cost_usd')},
           'ping_refusals': [{'max_tokens': r['max_tokens'], 'message': r.get('error')} for r in rows
                             if r['step'] == 'ping' and r.get('http_status') == 400]}
    if seed and ping:
        written = seed['usage']['cache_creation_input_tokens']
        u = ping['usage']
        assumed = (u['cache_read_input_tokens'] * rate['read'] + PING_WRITE_TOKENS * rate['write_1h'] +
                   PING_OUTPUT_TOKENS * rate['output']) / 1e6
        extra = ping['cost_usd'] - assumed
        net = REPLAY['net_usd'] - REPLAY['pings'] * extra
        out.update(ping_output_tokens=u['output_tokens'], ping_written=u['cache_creation_input_tokens'],
                   ping_read_share=round(u['cache_read_input_tokens'] / written, 4) if written else None,
                   ping_usd=round(ping['cost_usd'], 6), ping_extra_usd=round(extra, 6),
                   replay_best_net_usd=round(net, 2), replay_best_share=round(net / REPLAY['total_usd'], 4),
                   replay_still_go=net / REPLAY['total_usd'] >= REPLAY['go_share'])
    if seed and again:
        written = seed['usage']['cache_creation_input_tokens']
        share = again['usage']['cache_read_input_tokens'] / written if written else None
        out['again_read_share'] = share and round(share, 4)
        if renewal:
            out['again_seconds_after_seed'] = round(again['started_unix'] - seed['ended_unix'])
            out['kept_past_hour'] = bool(share and share >= 0.95 and out['again_seconds_after_seed'] > 3600)
    return out


def execute(out, budget, renewal, transport=send, sleeper=time.sleep, clock=time.time):
    rate = bench.rates()[MODEL]
    c = config()
    body = payload(c, f'keeper-ping {uuid.uuid4()}', MODEL, EFFORT, layer='system', ttl='1h')
    plan, _ = steps(renewal)
    rows, spent, status, accepted, accepted_at = [], 0.0, 'complete', False, None
    log = out/'rows.jsonl'
    for name, max_tokens, wait in plan:
        if name == 'ping' and accepted:
            continue
        if name == 'stop_check' and (not accepted or accepted_at >= max_tokens):
            continue
        if name == 'again' and not accepted:
            status = 'no_ping_accepted'
            break
        if wait:
            sleeper(wait)
        p = dict(body, max_tokens=max_tokens)
        estimate = estimate_usd(p, max_tokens, rate)
        if spent + estimate > budget:
            status = 'budget_stop'
            break
        row = {'step': name, 'max_tokens': max_tokens, 'started_unix': clock()}
        try:
            response, request_id = transport(p, c)
        except urllib.error.HTTPError as error:
            row.update(http_status=error.code, error=getattr(error, 'safe_api_message', None),
                       error_type=getattr(error, 'api_error_type', None), cost_usd=None, ended_unix=clock())
            rows.append(row)
            log.open('a').write(json.dumps(row) + '\n')
            if name in ('ping', 'stop_check') and error.code == 400 and not account_problem(error):
                continue
            status = 'account_problem' if account_problem(error) else 'error'
            break
        except (urllib.error.URLError, OSError) as error:
            row.update(error_details(error), error_type=type(error).__name__, cost_usd=None, ended_unix=clock())
            rows.append(row)
            log.open('a').write(json.dumps(row) + '\n')
            status = 'error'
            break
        usage = response.get('usage') or {}
        row.update(http_status=200, request_id=request_id, stop_reason=response.get('stop_reason'), usage=usage,
                   cost_usd=cost(usage, rate, '1h', row['started_unix']), ended_unix=clock())
        spent += row['cost_usd']
        rows.append(row)
        log.open('a').write(json.dumps(row) + '\n')
        if name == 'ping' and not accepted:
            accepted, accepted_at = True, max_tokens
    result = {'status': status, 'model': MODEL, 'effort': EFFORT, 'renewal': renewal, 'spent_known_usd': round(spent, 6),
              'unknown_cost_requests': sum(1 for r in rows if r.get('cost_usd') is None), 'rates': rate,
              **summarize(rows, rate, renewal)}
    (out/'result.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--renewal', action='store_true', help='wait 55 minutes before the ping and again before `again`')
    parser.add_argument('--budget', type=float, default=1.0, help='stop threshold in USD (default 1)')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args(argv)
    plan, wait = steps(args.renewal)
    rate = bench.rates()[MODEL]
    body = payload(config(), 'keeper-ping plan', MODEL, EFFORT, layer='system', ttl='1h')
    print(f'Plan: {MODEL} at effort {EFFORT}, adaptive thinking, a ~{len(json.dumps(body)) // 4:,}-token system prefix '
          f'cached 1h. seed (max_tokens {SEED_MAX_TOKENS}); ping{wait} at max_tokens {", ".join(map(str, PING_LADDER))} '
          f'until one is accepted; then once at max_tokens {STOP_CHECK_MAX_TOKENS} to see its stop reason; again{wait} '
          f'(the seed\'s body). At most {len(plan)} requests, each admitted only '
          f'under ${args.budget:g} (pessimistic estimate for the seed ${estimate_usd(body, SEED_MAX_TOKENS, rate):.2f}). '
          'No retries.', flush=True)
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
    out = args.out or ROOT/'runs'/f"keeper-ping-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=False)
    result = execute(out, args.budget, args.renewal)
    print(json.dumps(result, indent=2))
    print(f'Results: {out}')
    if result['status'] != 'complete':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
