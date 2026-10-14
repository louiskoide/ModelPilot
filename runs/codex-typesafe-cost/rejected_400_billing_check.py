"""Plan item 13: is a request the API rejects with HTTP 400 billed? (Written October 14 overnight; NOT run.)

Jev arms lost requests to one rejection: Claude Code's trailing role "system" message, sent to a model that refuses
mid-conversation system messages (claude-haiku-4-5-20251001 in jev-route-check 20260922-151453). bench_report keeps a
Jev trial's cost unknown while it has such a rejection, until their billing is known.

The check: on an API key used for nothing else during the window, send REJECTED copies of that shape (each about
PROMPT_TOKENS input tokens) and one accepted CONTROL of the same prompt without the system message. Then read the
key's usage for the window (Console > Usage, filtered to this key and model, after about an hour; or the Admin API's
usage report grouped by api_key_id and model). Input tokens near the control's alone (about 1x) mean rejected 400s are
not billed; near (1 + REJECTED)x mean they are. The control proves the key's traffic shows up at all.

Offline by default: prints the plan and its cost. --live sends, with the key from ANTHROPIC_API_KEY or a hidden
prompt (never printed or written). Direct API only: refuses when ANTHROPIC_BASE_URL is set. No retries.

    python3.12 runs/codex-typesafe-cost/rejected_400_billing_check.py            # plan only
    python3.12 runs/codex-typesafe-cost/rejected_400_billing_check.py --live     # sends 1 count + 1 control + 10 rejected
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from modelpilot.cache_probe import redact_message, tls_context  # noqa: E402

MODEL = 'claude-haiku-4-5-20251001'
RATE = {'input': 1.0, 'output': 5.0}  # USD per million tokens, configs/jev-rates.json (Haiku 4.5)
REJECTED = 10
PROMPT_TOKENS = 2000  # planned size; the run measures the control's exact count first (token counting is free)
MARKER = 'rejected-400-billing-check'


def prompt():
    # About PROMPT_TOKENS tokens of plain text; numbered lines so nothing is cached between requests by accident.
    lines = [f'{MARKER} line {i}: the quick brown fox jumps over the lazy dog.' for i in range(100)]
    return '\n'.join(lines)


def control_body():
    return {'model': MODEL, 'max_tokens': 1, 'messages': [{'role': 'user', 'content': prompt()}]}


def rejected_body():
    # The shape Claude Code 2.1.278+ sends and Haiku 4.5 rejects: the user turn, then a trailing system message.
    return {'model': MODEL, 'max_tokens': 1, 'messages': [{'role': 'user', 'content': prompt()},
                                                         {'role': 'system', 'content': '# Environment\nplatform: test'}]}


def plan(prompt_tokens=PROMPT_TOKENS):
    control = (prompt_tokens * RATE['input'] + 1 * RATE['output']) / 1e6
    if_billed = REJECTED * prompt_tokens * RATE['input'] / 1e6
    return {'model': MODEL, 'requests': {'count_tokens': 1, 'control': 1, 'rejected': REJECTED},
            'prompt_tokens_each': prompt_tokens,
            'cost_usd': {'count_tokens': 0.0, 'control': round(control, 6),
                         'rejected_if_not_billed': 0.0, 'rejected_if_billed_at_input_rate': round(if_billed, 6),
                         'worst_case_total': round(control + if_billed, 6)},
            'auth': 'API key only (a subscription shows no per-request billing)',
            'wall_time': 'about 1 minute to send; read usage about 1 hour later',
            'readout': f'input tokens for {MODEL} on this key in the window: about {prompt_tokens} (1x) means rejected '
                       f'400s are free; about {(1 + REJECTED) * prompt_tokens} ({1 + REJECTED}x) means billed',
            'decision': 'whether bench_report may count rejected requests as $0 (today a sensitivity figure only, '
                        'cost_if_rejected_free_usd) and so whether Jev trials with a rejection get a headline cost'}


def post(path, body, key, timeout=60):
    request = urllib.request.Request('https://api.anthropic.com' + path, data=json.dumps(body).encode(),
                                     headers={'x-api-key': key, 'anthropic-version': '2023-06-01',
                                              'content-type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls_context()))
    started = time.time()
    try:
        with opener.open(request, timeout=timeout) as response:  # no retries: each attempt is one request
            return {'status': response.status, 'request_id': response.headers.get('request-id'),
                    'started_unix': started, 'body': json.load(response)}
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read(16384)).get('error', {})
        except ValueError:
            detail = {}
        finally:
            error.close()
        return {'status': error.code, 'request_id': error.headers.get('request-id'), 'started_unix': started,
                'error_type': detail.get('type'), 'error_message': redact_message(str(detail.get('message', '')))}


def live():
    if os.environ.get('ANTHROPIC_BASE_URL'):
        raise SystemExit('Direct API only: unset ANTHROPIC_BASE_URL. Nothing sent.')
    key = os.environ.get('ANTHROPIC_API_KEY') or getpass.getpass('Anthropic API key, used for nothing else now (hidden): ').strip()
    run = ROOT/'runs'/time.strftime('rejected-400-billing-%Y%m%d-%H%M%S')
    run.mkdir(parents=True)
    counted = post('/v1/messages/count_tokens', {'model': MODEL, 'messages': control_body()['messages']}, key)
    tokens = (counted.get('body') or {}).get('input_tokens')
    record = {'plan': plan(tokens or PROMPT_TOKENS), 'count_tokens': {k: v for k, v in counted.items() if k != 'body'},
              'control_input_tokens': tokens, 'requests': []}
    if counted['status'] != 200:
        record['stopped'] = 'token count failed; nothing billable sent'
    else:
        control = post('/v1/messages', control_body(), key)
        record['requests'].append(dict({k: v for k, v in control.items() if k != 'body'}, kind='control',
                                       usage=(control.get('body') or {}).get('usage')))
        for _ in range(REJECTED):
            sent = post('/v1/messages', rejected_body(), key)
            record['requests'].append(dict({k: v for k, v in sent.items() if k != 'body'}, kind='rejected'))
            if sent['status'] != 400:  # not the shape under test (accepted, or an account/rate problem): stop
                record['stopped'] = f"expected 400, got {sent['status']}"
                break
    record['window_unix'] = [min(r['started_unix'] for r in record['requests']) - 60, time.time() + 60] \
        if record['requests'] else None
    (run/'record.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({'run': str(run), 'statuses': [r['status'] for r in record['requests']],
                      'stopped': record.get('stopped'), 'readout': record['plan']['readout']}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--live', action='store_true', help='send the requests (billable)')
    if parser.parse_args().live:
        live()
    else:
        print(json.dumps(plan(), indent=2))


if __name__ == '__main__':
    main()
