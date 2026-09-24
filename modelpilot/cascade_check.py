"""Two paid cheap-model drafts, with M4 verification decisions remaining dry-run.

--execute-fallback additionally sends every draft through a governor and runs one Opus
fallback for an escalated draft, re-verified against the same source (never accepted unverified).
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import time
import uuid
from .cache_probe import send,cost,error_details
from .governor import Governor,execute_fallback,governed_transport
from .m4 import cascade,sha

FALLBACK_MODEL='claude-opus-4-6'


def parse_candidate(response,evidence):
    if response.get('stop_reason')!='end_turn':
        raise ValueError('incomplete draft')
    text=''.join(b['text'] for b in response['content'] if b['type']=='text')
    candidate=json.loads(text)
    if not isinstance(candidate,dict):raise ValueError('draft is not an object')
    # Source identity is supplied by the host; model must supply its claimed span.
    candidate['source_sha256']=sha(evidence['text'])
    return candidate


def verify_draft(response,evidence):
    if response.get('stop_reason')!='end_turn':
        return {'action':'would_escalate','reason':'incomplete_draft','applied':False}
    try:
        return cascade('read',parse_candidate(response,evidence),evidence,1,1)
    except (KeyError,ValueError,TypeError):
        return {'action':'would_escalate','reason':'unparseable_draft','applied':False}


def governed_fallback(gov,rates,request,response,evidence,transport=send):
    """Review the draft in the ledger, then run the fallback when the review escalates."""
    task=gov.state.create('cascade case','Identify the verification token')['id']
    rev=gov.state.claim(task,1,'cascade-check')['revision']
    evidence=dict(evidence,revision=rev)
    try:candidate=parse_candidate(response,evidence)
    except (KeyError,ValueError,TypeError):candidate={}
    review=gov.review(task,'read',candidate,evidence,rev,fallback_estimate=.02)
    return execute_fallback(gov,review,evidence,lambda d:dict(request,model=FALLBACK_MODEL),
                            lambda r:parse_candidate(r,evidence),transport,rates,allow_calls=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true')
    parser.add_argument('--execute-fallback',action='store_true',help='Also run one governed Opus fallback for an escalated draft')
    args=parser.parse_args()
    if not args.live:
        print('Plan: two Sonnet drafts, dry-run verification; '+
              ('one governed Opus fallback for the escalated draft, re-verified; ' if args.execute_fallback else 'no escalation; ')+
              'no shadow warming. Add --live to run.');return
    key=os.environ.get('ANTHROPIC_API_KEY') or getpass.getpass('Paste API key (hidden): ').strip()
    if not key.startswith('sk-ant-') or any(c.isspace() for c in key):raise SystemExit('Invalid API key format.')
    os.environ['ANTHROPIC_API_KEY']=key
    root=Path(__file__).resolve().parents[1];run=root/'runs'/('m4-cascade-'+time.strftime('%Y%m%d-%H%M%S'));run.mkdir(mode=0o700)
    rates=json.loads((root/'configs/m0.json').read_text())['rates'];model='claude-sonnet-4-6'
    report={'status':'running','cases':[],'escalations_executed':0,'shadow_requests':0}
    gov=Governor(run/'ledger.sqlite3','cascade',1.0) if args.execute_fallback else None
    transport=governed_transport(gov,send,rates,.02) if gov else send
    print('Running two draft checks; results: '+str(run),flush=True)
    try:
        for available in (True,False):
            token='TOKEN_'+uuid.uuid4().hex
            source=token+' is the verification token.' if available else 'There is no verification token in this source.'
            evidence={'revision':1,'text':source,'current_source_sha256':sha(source)}
            request={'model':model,'max_tokens':192,'output_config':{'effort':'low'},
                     'system':'Return only a JSON object with answer, start and end, identifying the TOKEN_ verification token in the supplied source. start/end are zero-based character offsets, end exclusive. If absent return {}. No markdown.',
                     'messages':[{'role':'user','content':source}]}
            started=time.monotonic();response,_=transport(request,{})
            decision=verify_draft(response,evidence)
            expected='would_accept' if available else 'would_escalate'
            estimate=cost(response['usage'],rates[model],'5m') if response.get('model')==model else None
            case={'name':'supported' if available else 'unsupported','passed':decision['action']==expected and estimate is not None,
                  'decision':decision,'cost_usd':estimate,'usage':response['usage'],'wall_seconds':time.monotonic()-started}
            if gov and decision['action']=='would_escalate':
                # No token exists here, so a correct fallback can only defer; acceptance would be a failure.
                fallback=governed_fallback(gov,rates,request,response,evidence)
                report['escalations_executed']+=fallback.get('fallback',{}).get('executed',False)
                case.update(fallback=fallback,passed=case['passed'] and fallback['action']=='would_defer'
                            and fallback.get('fallback',{}).get('cost_usd') is not None)
            report['cases'].append(case);(run/(case['name']+'.json')).write_text(json.dumps({'source':source,'response':response},indent=2))
            print(case['name'],'PASS' if case['passed'] else 'FAIL',flush=True)
            if not case['passed']:break
        report['status']='passed' if len(report['cases'])==2 and all(c['passed'] for c in report['cases']) else 'failed'
    except Exception as error:
        report.update(status='failed',error_type=type(error).__name__,error_hint=error_details(error)['error_hint'])
    report['known_cost_usd']=sum(c['cost_usd'] or 0 for c in report['cases'])+sum(
        (c.get('fallback',{}).get('fallback',{}).get('cost_usd') or 0) for c in report['cases'])
    if gov:
        report['governor_policy']=gov.policy();gov.close()
    (run/'summary.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
    if report['status']!='passed':raise SystemExit(1)


if __name__=='__main__':main()
