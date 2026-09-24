#!/usr/bin/env python3
"""Direct Anthropic Messages API cache experiments. Offline planning is default."""
import argparse
import copy
import hashlib
import json
import math
import os
import re
import socket
import ssl
from pathlib import Path
import time
import urllib.error
import urllib.request
import uuid


def validate(c):
    if len(c['models']) != 2 or len(set(c['models'])) != 2:
        raise ValueError('Choose two distinct, exact model IDs')
    if len(c['efforts']) != 2 or len(set(c['efforts'])) != 2:
        raise ValueError('Choose two distinct supported effort levels')
    if c['repeats'] < 3 or c['prefix_lines'] < 1:
        raise ValueError('Use at least three repeats and a positive prefix size')
    if c['max_tokens'] < 1:
        raise ValueError('Real requests need a positive output allowance')
    for ttl in c['ttls']:
        if ttl not in ('5m', '1h'):
            raise ValueError('TTL must be 5m or 1h')
    for model in c['models']:
        rates = c.get('rates', {}).get(model)
        if rates is not None:
            for key in ('input', 'write_5m', 'write_1h', 'read', 'output'):
                if not math.isfinite(rates[key]) or rates[key] < 0:
                    raise ValueError('Rates must be finite, nonnegative USD per million tokens')


def payload(c, nonce, model, effort, layer='messages', ttl='5m', max_tokens=None, tail='Reply OK.'):
    # The nonce comes FIRST so independent trials cannot reuse a long common prefix.
    prefix = nonce + '\n' + '\n'.join(
        f'Record {i}: cobalt river maple stone amber; item {i} is stable reference data.'
        for i in range(c['prefix_lines']))
    block = {'type': 'text', 'text': prefix,
             'cache_control': {'type': 'ephemeral', 'ttl': ttl}}
    p = {'model': model, 'max_tokens': c['max_tokens'] if max_tokens is None else max_tokens,
         'output_config': {'effort': effort}}
    if 'thinking' in c:
        p['thinking'] = copy.deepcopy(c['thinking'])
    if layer == 'system':
        p['system'] = [block]
        p['messages'] = [{'role': 'user', 'content': tail}]
    else:
        p['messages'] = [{'role': 'user', 'content': [block, {'type': 'text', 'text': tail}]}]
    return p


def experiments(c, suite, run_id):
    a, b = c['models']
    low, high = c['efforts']
    groups = []
    def add(name, specs):
        groups.append({'name': name, 'steps': specs})
    for repeat in range(c['repeats']):
        def p(name, model=a, effort=low, **kw):
            return payload(c, f'{run_id}/{repeat}/{name}', model, effort, **kw)
        if suite == 'quick':
            for model in (a, b):
                for layer in ('system', 'messages'):
                    name = f'effort/{repeat}/{model}/{layer}'
                    add(name, [(label, 0, p(name, model, effort, layer=layer))
                               for label, effort in [('cold', low), ('warm', low),
                                                     ('changed', high), ('changed_warm', high),
                                                     ('return', low)]])
            name = f'model/{repeat}'
            add(name, [(label, 0, p(name, model)) for label, model in
                       [('a_cold', a), ('a_warm', a), ('b_cold', b), ('b_warm', b), ('a_return', a)]])
            for model in (a, b):
                for layer in ('system', 'messages'):
                    for tokens in (0, 1):
                        name = f'shadow/{repeat}/{model}/{layer}/{tokens}'
                        add(name, [('prewarm', 0, p(name, model, layer=layer, max_tokens=tokens, tail='Warmup.')),
                                   ('refresh', 0, p(name, model, layer=layer, max_tokens=tokens, tail='Warmup.')),
                                   ('real', 0, p(name, model, layer=layer))])
                    name = f'no_shadow/{repeat}/{model}/{layer}'
                    add(name, [('real_cold', 0, p(name, model, layer=layer)),
                               ('real_warm', 0, p(name, model, layer=layer))])
        else:
            for model in (a, b):
                for ttl in c['ttls']:
                    seconds = 300 if ttl == '5m' else 3600
                    for kind, gaps in [('before', [0, seconds * .8]),
                                       ('after', [0, seconds + 30]),
                                       ('refresh', [0, seconds * .6, seconds * .6])]:
                        name = f'ttl/{repeat}/{model}/{ttl}/{kind}'
                        add(name, [(f'touch_{i}', gap, p(name, model, ttl=ttl))
                                   for i, gap in enumerate(gaps)])
    return groups


def cost(usage, rates, ttl):
    if rates is None:
        return None
    for key in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'):
        if key not in usage:
            raise ValueError(f'Missing usage field: {key}')
    creation = usage.get('cache_creation')
    total = usage['cache_creation_input_tokens']
    if creation is None:
        w5, w1 = (total, 0) if ttl == '5m' else (0, total)
    else:
        w5 = creation['ephemeral_5m_input_tokens']
        w1 = creation['ephemeral_1h_input_tokens']
        if w5 + w1 != total:
            raise ValueError('Cache write breakdown does not equal total')
    return (usage['input_tokens'] * rates['input'] +
            usage['output_tokens'] * rates['output'] +
            usage['cache_read_input_tokens'] * rates['read'] +
            w5 * rates['write_5m'] + w1 * rates['write_1h']) / 1e6


def priced_usage(request, model, usage, rates):
    """Measured cost of one response, or None when any pricing input is ambiguous.

    Shared by the proxy and governed transports so both apply one rule.
    """
    if not isinstance(usage, dict) or model != request.get('model'):
        return None  # aliases/fallback need explicit rate mapping, not guessing
    # "not_available": the model has no data-residency option (e.g. Haiku 4.5), so standard pricing applies.
    if usage.get('service_tier', 'standard') != 'standard' or usage.get('inference_geo', 'global') not in ('global', 'not_available'):
        return None
    if request.get('speed') == 'fast':
        return None
    # Missing TTL breakdown is ambiguous in real traffic; M0's single-TTL fallback is not used.
    if usage.get('cache_creation_input_tokens', 0) and 'cache_creation' not in usage:
        return None
    try:
        return cost(usage, rates.get(model), '5m')
    except (KeyError, TypeError, ValueError):
        return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def tls_context():
    context = ssl.create_default_context()
    # Preserve system/custom trust and supplement missing python.org macOS roots.
    try:
        import certifi
    except ImportError:
        pass
    else:
        context.load_verify_locations(cafile=certifi.where())
    return context


def error_details(error):
    reason = getattr(error, 'reason', error)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return {'error_category': 'tls_certificate',
                'error_hint': 'Python cannot verify the server certificate. Install certifi with python3 -m pip install certifi; keep TLS verification enabled.'}
    if isinstance(reason, socket.gaierror):
        return {'error_category': 'dns', 'error_hint': 'Hostname lookup failed. Check network access and DNS.'}
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return {'error_category': 'timeout', 'error_hint': 'Connection timed out; no automatic retry was made.'}
    if isinstance(error, urllib.error.HTTPError):
        hints = {400: 'API rejected the request configuration.',
                 401: 'API authentication failed. Check the locally configured key.',
                 403: 'API access was denied.', 404: 'Requested API resource or model was not found.',
                 429: 'API rate or usage limit reached.'}
        return {'error_category': 'http', 'error_hint': getattr(error, 'safe_api_message', hints.get(error.code, 'API returned an HTTP error.'))}
    if isinstance(error, urllib.error.URLError):
        # Do not print raw exceptions: proxy URLs can contain credentials.
        return {'error_category': 'connection',
                'reason_type': type(reason).__name__,
                'errno': getattr(reason, 'errno', None),
                'error_hint': 'HTTPS connection failed. Check internet access, VPN/proxy settings and firewall; no automatic retry was made.'}
    return {'error_category': 'other', 'error_hint': 'See error_type in the observation log.'}


def send(p, c):
    headers = {'x-api-key': os.environ['ANTHROPIC_API_KEY'],
               'anthropic-version': '2023-06-01', 'content-type': 'application/json'}
    if c.get('anthropic_beta'):
        headers['anthropic-beta'] = c['anthropic_beta']
    req = urllib.request.Request('https://api.anthropic.com/v1/messages',
                                 data=json.dumps(p).encode(), headers=headers)
    # No automatic retries: retries change both billing and cache state.
    try:
        with urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=tls_context())).open(req, timeout=120) as response:
            return json.load(response), response.headers.get('request-id')
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read(16384))
            message = body.get('error', {}).get('message', '')
            if isinstance(message, str) and message:
                error.safe_api_message = redact_message(message)
        except (ValueError, TypeError, AttributeError):
            pass
        finally:
            error.close()
        raise


def redact_message(message):
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    if key:
        message = message.replace(key, '[REDACTED]')
    message = re.sub(r'sk-ant-[A-Za-z0-9_-]+', '[REDACTED]', message)
    return ' '.join(message.split())[:1000]


def observe(usage):
    if not all(k in usage for k in ('cache_read_input_tokens', 'cache_creation_input_tokens')):
        return 'unknown'
    read, write = usage['cache_read_input_tokens'], usage['cache_creation_input_tokens']
    if read and write:
        return 'partial_hit_and_write'
    if read:
        return 'hit'
    if write:
        return 'write'
    return 'uncached_or_below_minimum'


def run(c, groups, path, limit, transport=send, sleeper=time.sleep):
    if sum(len(g['steps']) for g in groups) > limit:
        raise ValueError('Plan exceeds --max-calls; no requests sent')
    with path.open('x') as log:
        for group in groups:
            for label, delay, p in group['steps']:
                # Wait relative to the PREVIOUS RESPONSE, with no intervening touch.
                remaining = delay
                while remaining > 0:
                    chunk = min(remaining, 30)
                    sleeper(chunk)
                    remaining -= chunk
                start = time.monotonic()
                row = {'group': group['name'], 'step': label, 'model': p['model'],
                       'effort': p['output_config']['effort'], 'delay_seconds': delay,
                       'request_sha256': hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest(),
                       'started_unix': time.time(), 'max_tokens': p['max_tokens']}
                try:
                    response, rid = transport(p, c)
                    usage = response['usage']
                    block = p.get('system', p['messages'][0]['content'])[0]
                    row.update(status='ok', request_id=rid, usage=usage,
                               returned_model=response.get('model'), stop_reason=response.get('stop_reason'),
                               observation=observe(usage),
                               cost_usd=cost(usage, c.get('rates', {}).get(p['model']), block['cache_control']['ttl']))
                except Exception as e:
                    # Never log response bodies, authorization headers, or raw exceptions.
                    row.update(status='error', error_type=type(e).__name__,
                               http_status=getattr(e, 'code', None), **error_details(e))
                row['wall_seconds'] = time.monotonic() - start
                log.write(json.dumps(row) + '\n')
                log.flush()
                print(group['name'], label, row['status'], row.get('observation', ''), flush=True)
                if row['status'] == 'error':
                    raise RuntimeError(f"Probe stopped: {row['error_hint']} Log: {path}. No automatic retry.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--suite', choices=['quick', 'ttl', 'check'], default='quick')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--max-calls', type=int, default=200)
    args = parser.parse_args()
    c = json.loads(args.config.read_text())
    validate(c)
    rid = str(uuid.uuid4())
    groups = experiments(c, 'ttl' if args.suite == 'check' else args.suite, rid)
    if args.suite == 'check':
        groups = [dict(name=groups[0]['name'], steps=groups[0]['steps'][:1])]
    plan = {'run_id': rid, 'suite': args.suite, 'live': args.live, 'config': c,
            'calls': sum(len(g['steps']) for g in groups),
            'minimum_wait_seconds': sum(s[1] for g in groups for s in g['steps']),
            'groups': [{'name': g['name'], 'steps': [{'label': label, 'wait_seconds': wait,
                        'model': p['model'], 'effort': p['output_config']['effort'],
                        'max_tokens': p['max_tokens']} for label, wait, p in g['steps']]} for g in groups]}
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / 'plan.json').open('x') as f:
        json.dump(plan, f, indent=2)
    print(json.dumps({k: plan[k] for k in ('run_id', 'live', 'calls', 'minimum_wait_seconds')}, indent=2))
    if not args.live:
        return
    if not os.environ.get('ANTHROPIC_API_KEY'):
        raise SystemExit('Set ANTHROPIC_API_KEY in the environment; no requests sent.')
    if os.environ.get('ANTHROPIC_BASE_URL'):
        raise SystemExit('This adapter supports direct Anthropic only; refusing gateway mismatch.')
    if any('REPLACE' in model for model in c['models']):
        raise SystemExit('Configure exact model IDs before live execution.')
    run(c, groups, args.out / 'observations.jsonl', args.max_calls)


if __name__ == '__main__':
    main()
