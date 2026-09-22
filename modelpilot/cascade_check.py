"""Two paid cheap-model drafts, with M4 verification decisions remaining dry-run."""
import argparse
import getpass
import json
import os
from pathlib import Path
import time
import uuid
from .cache_probe import send,cost,error_details
from .m4 import cascade,sha


def verify_draft(response,evidence):
    if response.get('stop_reason')!='end_turn':
        return {'action':'would_escalate','reason':'incomplete_draft','applied':False}
    try:
        text=''.join(b['text'] for b in response['content'] if b['type']=='text')
        candidate=json.loads(text)
        if not isinstance(candidate,dict):raise ValueError()
        # Source identity is supplied by the host; model must supply its claimed span.
        candidate['source_sha256']=sha(evidence['text'])
        return cascade('read',candidate,evidence,1,1)
    except (KeyError,ValueError,TypeError):
        return {'action':'would_escalate','reason':'unparseable_draft','applied':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');args=parser.parse_args()
    if not args.live:
        print('Plan: two Sonnet drafts, dry-run verification; no escalation or shadow warming. Add --live to run.');return
    key=os.environ.get('ANTHROPIC_API_KEY') or getpass.getpass('Paste API key (hidden): ').strip()
    if not key.startswith('sk-ant-') or any(c.isspace() for c in key):raise SystemExit('Invalid API key format.')
    os.environ['ANTHROPIC_API_KEY']=key
    root=Path(__file__).resolve().parents[1];run=root/'runs'/('m4-cascade-'+time.strftime('%Y%m%d-%H%M%S'));run.mkdir(mode=0o700)
    rates=json.loads((root/'configs/m0.json').read_text())['rates'];model='claude-sonnet-4-6'
    report={'status':'running','cases':[],'escalations_executed':0,'shadow_requests':0}
    print('Running two draft checks; results: '+str(run),flush=True)
    try:
        for available in (True,False):
            token='TOKEN_'+uuid.uuid4().hex
            source=token+' is the verification token.' if available else 'There is no verification token in this source.'
            evidence={'revision':1,'text':source,'current_source_sha256':sha(source)}
            request={'model':model,'max_tokens':192,'output_config':{'effort':'low'},
                     'system':'Return only a JSON object with answer, start and end, identifying the TOKEN_ verification token in the supplied source. start/end are zero-based character offsets, end exclusive. If absent return {}. No markdown.',
                     'messages':[{'role':'user','content':source}]}
            started=time.monotonic();response,_=send(request,{})
            decision=verify_draft(response,evidence)
            expected='would_accept' if available else 'would_escalate'
            estimate=cost(response['usage'],rates[model],'5m') if response.get('model')==model else None
            case={'name':'supported' if available else 'unsupported','passed':decision['action']==expected and estimate is not None,
                  'decision':decision,'cost_usd':estimate,'usage':response['usage'],'wall_seconds':time.monotonic()-started}
            report['cases'].append(case);(run/(case['name']+'.json')).write_text(json.dumps({'source':source,'response':response},indent=2))
            print(case['name'],'PASS' if case['passed'] else 'FAIL',flush=True)
            if not case['passed']:break
        report['status']='passed' if len(report['cases'])==2 and all(c['passed'] for c in report['cases']) else 'failed'
    except Exception as error:
        report.update(status='failed',error_type=type(error).__name__,error_hint=error_details(error)['error_hint'])
    report['known_cost_usd']=sum(c['cost_usd'] or 0 for c in report['cases'])
    (run/'summary.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
    if report['status']!='passed':raise SystemExit(1)


if __name__=='__main__':main()
