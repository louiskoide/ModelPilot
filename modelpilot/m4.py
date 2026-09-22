"""M4 dry-run cascade decisions and shadow-cache lifecycle estimates."""
import argparse
import hashlib
import json
import math
from pathlib import Path


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def cascade(operation, candidate, evidence, task_revision, current_revision):
    """Host evidence is trusted; candidate confidence is never used for acceptance.

    Read candidates must name an exact source span. Test candidates report the
    observed exit code. Edit/destructive operations cannot be accepted here.
    """
    decision={'operation':operation,'applied':False,'mode':'dry-run','candidate_sha256':sha(json.dumps(candidate,sort_keys=True))}
    if operation=='destructive':
        return dict(decision,action='blocked',reason='destructive_operations_excluded')
    if task_revision!=current_revision:
        return dict(decision,action='reject_stale',reason='task_revision_changed')
    if operation not in ('read','test_report','edit'):
        return dict(decision,action='blocked',reason='unsupported_operation')
    if operation=='edit':
        return dict(decision,action='would_escalate',reason='independent_patch_validation_not_implemented')
    if not isinstance(evidence,dict) or evidence.get('revision')!=current_revision:
        return dict(decision,action='would_escalate',reason='missing_current_host_evidence')
    if operation=='read':
        text=evidence.get('text'); start=candidate.get('start'); end=candidate.get('end')
        valid=(isinstance(text,str) and type(start) is int and type(end) is int and
               0<=start<end<=len(text) and candidate.get('source_sha256')==sha(text) and
               evidence.get('current_source_sha256')==sha(text) and
               candidate.get('answer')==text[start:end])
        reason='source_span_verified' if valid else 'source_span_not_verified'
    else:
        valid=(evidence.get('completed') is True and type(evidence.get('exit_code')) is int and
               type(candidate.get('exit_code')) is int and candidate['exit_code']==evidence['exit_code'] and
               isinstance(evidence.get('run_id'),str) and bool(evidence['run_id']) and
               candidate.get('run_id')==evidence['run_id'] and
               candidate.get('passed') is (evidence['exit_code']==0))
        reason='test_result_verified' if valid else 'test_result_not_verified'
    return dict(decision,action='would_accept' if valid else 'would_escalate',reason=reason)


def nonnegative(value,name):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
        raise ValueError(name+' must be finite and nonnegative')
    return value


def shadow_plan(prefix_tokens, rates, switch_probability, refreshes=0, ttl='5m',
                latency_value_usd=0, configuration_matches=True, refresh_output_tokens=0):
    """Compare prewarm+maintenance with on-demand cold creation for one possible use.

    Assumes the shadow survives until the switch and the prefix stays unchanged.
    Prices exclude identical uncached suffix/output work in both alternatives.
    """
    nonnegative(prefix_tokens,'prefix_tokens'); nonnegative(switch_probability,'switch_probability')
    nonnegative(latency_value_usd,'latency_value_usd'); nonnegative(refresh_output_tokens,'refresh_output_tokens')
    if switch_probability>1 or type(refreshes) is not int or refreshes<0 or ttl not in ('5m','1h'):
        raise ValueError('Invalid probability, refresh count or TTL')
    for key in ('write_5m','write_1h','read','output'): nonnegative(rates[key],key)
    write=prefix_tokens*rates['write_'+ttl]/1e6
    read=prefix_tokens*rates['read']/1e6
    output=refresh_output_tokens*rates['output']/1e6
    cold=switch_probability*write
    warm=write+refreshes*read+(refreshes+1)*output+switch_probability*read
    benefit=switch_probability*latency_value_usd
    worthwhile=configuration_matches and prefix_tokens>0 and benefit>warm-cold
    return {'mode':'dry-run','applied':False,'warming_enabled':False,
            'would_consider_warming':worthwhile,'ttl':ttl,
            'expected_on_demand_usd':cold,'expected_shadow_usd':warm,
            'extra_api_cost_usd':warm-cold,'expected_latency_value_usd':benefit,
            'reason':'configuration_mismatch' if not configuration_matches else
                     ('latency_value_exceeds_extra_cost' if worthwhile else 'no_economic_case'),
            'assumptions':'same model/effort/prefix; cold target; survives until use; refresh hits; no prefix growth; same suffix and useful output costs excluded'}


def run_scenarios():
    text='The verification token is ALPHA.'
    evidence={'revision':1,'text':text,'current_source_sha256':sha(text)}
    start=text.index('ALPHA')
    good={'start':start,'end':start+5,'answer':'ALPHA','source_sha256':sha(text)}
    cases=[]
    def check(name,result,expected):
        passed=result['action']==expected and result['applied'] is False
        cases.append({'name':name,'passed':passed,'decision':result})
    check('verified_read',cascade('read',good,evidence,1,1),'would_accept')
    check('fabricated_read',cascade('read',dict(good,answer='invented'),evidence,1,1),'would_escalate')
    check('stale_task',cascade('read',good,evidence,1,2),'reject_stale')
    check('changed_file',cascade('read',good,dict(evidence,current_source_sha256='changed'),1,1),'would_escalate')
    test={'run_id':'fixture-1','exit_code':0,'passed':True}
    host={'revision':1,'run_id':'fixture-1','exit_code':0,'completed':True}
    check('verified_tests',cascade('test_report',test,host,1,1),'would_accept')
    check('false_test_success',cascade('test_report',test,dict(host,exit_code=1),1,1),'would_escalate')
    check('edit',cascade('edit',{}, {},1,1),'would_escalate')
    check('destructive',cascade('destructive',{}, {},1,1),'blocked')
    return cases


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('configs/m0.json'))
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    config=json.loads(args.config.read_text())
    cases=run_scenarios()
    plans={model:shadow_plan(14750,rates,1,refreshes=1) for model,rates in config['rates'].items()}
    report={'status':'passed' if all(c['passed'] for c in cases) else 'failed',
            'cascade_cases':cases,'shadow_plans':plans,'actual_api_cost_usd':0,'routing_applied':False}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    with args.out.open('x') as f:json.dump(report,f,indent=2)
    print(json.dumps(report,indent=2))
    if report['status']!='passed':raise SystemExit(1)


if __name__=='__main__':main()
