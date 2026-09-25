"""Observe-only ModelPilot benchmark plumbing and conservative switch-cost proposals.

This is not the active ModelPilot policy and is ineligible for savings comparisons.
"""
import hashlib
import json
import math
from pathlib import Path
from .governor import Governor
from .governed_session import hook_settings, OWNER
from .hooks import channel_declaration


def switch_decision(source, target, prefix_tokens, output_tokens, horizon, available_usd, rates,
                    margin=1.5, current_cold=False):
    result={'action':'defer','applied':False,'reason':'invalid_or_unknown_inputs'}
    values=(prefix_tokens,output_tokens,horizon,margin)
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in values):
        return result
    if prefix_tokens<0 or output_tokens<0 or horizon<1 or int(horizon)!=horizon or margin<1:
        return result
    if source not in rates or target not in rates or available_usd is None:
        return result
    if isinstance(available_usd,bool) or not math.isfinite(available_usd) or available_usd<0:
        return result
    for model in (source,target):
        if any(not isinstance(rates[model].get(k),(int,float)) or not math.isfinite(rates[model][k]) or rates[model][k]<0
               for k in ('read','write_5m','output')):
            return result
    a,b=rates[source],rates[target]
    write=prefix_tokens*b['write_5m']/1e6
    first=write+output_tokens*b['output']/1e6
    stay=(prefix_tokens*(a['write_5m'] if current_cold else a['read']) +
          prefix_tokens*a['read']*(horizon-1)+output_tokens*a['output']*horizon)/1e6
    target_cost=first+(horizon-1)*(prefix_tokens*b['read']+output_tokens*b['output'])/1e6
    # Warm-target evidence never discounts the first target write. Future reuse is a forecast,
    # not guaranteed: budget admission for each actual request still reserves the full write.
    savings=stay-target_cost
    result.update(target_write_usd=write,reserve_usd=first,stay_forecast_usd=stay,
                  target_forecast_usd=target_cost,savings_usd=savings)
    if first>available_usd:
        result['reason']='insufficient_write_reservation'
    elif source==target or savings <= (margin-1)*write:
        result.update(action='stay',reason='forecast_does_not_cover_rebuild_margin')
    else:
        result.update(action='would_switch',reason='cost_candidate_requires_quality_and_compatibility_checks')
    return result


class ModelPilotAdapter:
    arm_id='modelpilot'
    model='claude-sonnet-5'
    key=''

    def __init__(self,limit_usd=1.,mode='dry-run'):
        if mode!='dry-run':
            raise ValueError('Active ModelPilot benchmark policy is not validated')
        if not math.isfinite(limit_usd) or limit_usd<=0:
            raise ValueError('Positive finite budget required')
        self.limit=limit_usd
        self.binding={}

    def verify(self):
        pass  # No external router checkout or credential is used.

    def setup(self,trial):
        self.directory=trial.dir
        self.db=trial.dir/'governor.sqlite3'
        self.session='bench-'+trial.session_id
        self.python=trial.python
        gov=Governor(self.db,self.session,self.limit)
        try:
            self.task=gov.state.create('benchmark',trial.task['instruction'])['id']
            gov.state.claim(self.task,1,OWNER,seconds=3600)
            self.code=gov.declare_channel()
        finally:
            gov.close()
        self.settings=trial.dir/'modelpilot-settings.json'
        self.settings.write_text(json.dumps(hook_settings(self.python),indent=2)+'\n')
        self.binding={'MODELPILOT_DB':str(self.db),'MODELPILOT_SESSION':self.session,
                      'MODELPILOT_LIMIT_USD':str(self.limit),'MODELPILOT_TASK':self.task,
                      'MODELPILOT_OWNER':OWNER,'MODELPILOT_HOOK_ERRORS':str(trial.dir/'hook-errors.jsonl')}

    def proxy_options(self):
        return {'governor':{'db':self.db,'session':self.session,'limit_usd':self.limit,'task':self.task}}

    def command(self,command,proxy_url):
        result=list(command)
        i=result.index('-p')+1
        result[i]=channel_declaration(self.task,self.code)+'\n\n'+result[i]
        return result+['--effort','medium','--settings',str(self.settings)]

    def environment(self,env):
        return dict(env,**self.binding)

    def accounting(self,rows,final):
        from .bench import accounting
        report=accounting(rows,final)
        report['cost_complete']=report['cost_usd'] is not None and report['tokens_match'] and not report['rejected_requests']
        if not report['cost_complete']:
            report['cost_usd']=None
        return report

    def evidence(self,directory):
        gov=Governor(self.db,self.session,self.limit)
        try:
            policy=gov.policy()
            hooks=gov.journal('hook_event')
            from .policy_actions import escalation_proposal
            try:
                row=gov.state.get(self.task)
                proposal=escalation_proposal(gov.state,self.task,row['revision'],OWNER,self.model,'medium')
            except ValueError:
                proposal={'action':'defer','reason':'task_not_owned_or_acknowledged','applied':False}
        finally:
            gov.close()
        log=Path(directory)/'observations.jsonl'
        rows=[json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        messages=[r for r in rows if r.get('kind')=='messages']
        settled=all(r.get('governor_status')=='settled' for r in messages)
        known=sum(r.get('cost_usd') or 0 for r in messages)
        policy_file=Path(__file__).resolve().parents[1]/'docs/m6-modelpilot-policy.md'
        return {'mode':'dry-run','applied':False,'active_policy_implemented':False,'benchmark_eligible':False,
                'policy_sha256':hashlib.sha256(policy_file.read_bytes()).hexdigest(),
                'governor':policy,'escalation_proposal':proposal,'hook_events':len(hooks),'all_requests_settled':bool(messages) and settled,
                'accounting_matches':bool(messages) and settled and policy['cost_complete'] and
                    all(r.get('cost_usd') is not None for r in messages) and abs(known-policy['spent_usd'])<1e-9,
                'would_refuse':sum(not r.get('governor',{}).get('admitted',False) for r in messages)}
