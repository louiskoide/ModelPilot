"""ModelPilot benchmark plumbing and conservative switch-cost proposals.

Observe-only by default. tools=True adds the R5 tools (bench_tools MCP server). A
fixture_policy (the owned fixtures.FixtureServer) applies ladder escalations offline; those
trials stay ineligible for comparisons. mode='active' runs the arm's policy
(active_policy.ActivePolicy, user-approved for the benchmark arm only) with Jev as its advisor;
only such trials with a live advisor and the complete catalog are benchmark-eligible. The active
arm declares the correction channel only while delegation is on: nothing else speaks to the agent.
"""
import hashlib
import json
import math
from pathlib import Path
import sys
from .cache_probe import tier
from .governor import Governor
from .governed_session import hook_settings, OWNER
from .hooks import REVIEW_AT_STOP, channel_declaration


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
    rates={m:tier(rates[m],prefix_tokens) for m in (source,target)}  # a model priced by prompt length
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


TOOLS_NOTE=('ModelPilot tools: run the test suite with mcp__modelpilot__run_tests and search the repository with '
            'mcp__modelpilot__search. Long output comes back as an excerpt plus a handle; page more with '
            'mcp__modelpilot__expand_output only when you need it.')


class ModelPilotAdapter:
    arm_id='modelpilot'
    model='claude-sonnet-5-5'
    key=''

    def __init__(self,limit_usd=1.,mode='dry-run',tools=False,threshold=8192,fixture_policy=None,
                 arm_id='modelpilot',model='claude-sonnet-5-5',effort='medium',advisor=None,models=None,overrides=None):
        from .policy_actions import MODELS,setting
        setting(model,effort)
        if model==MODELS[0]:
            raise ValueError('The arm starts on Sonnet 5.5 or Opus 5.5; Haiku 5.5 is reached by a proxy move (user decision, '
                             'October 8), never by starting the client on it')
        self.arm_id,self.model,self.effort=arm_id,model,effort
        if mode not in ('dry-run','active'):
            raise ValueError('Mode must be dry-run or active')
        if mode=='active' and fixture_policy is not None:
            raise ValueError('The active arm runs its own policy, never a fixture policy')
        if not math.isfinite(limit_usd) or limit_usd<=0:
            raise ValueError('Positive finite budget required')
        if isinstance(threshold,bool) or not isinstance(threshold,int) or threshold<256:
            raise ValueError('Excerpt threshold must be an integer of at least 256 bytes')
        self.policy,self.advisor,self.models,self.catalog,self.overrides=None,advisor,models,None,overrides
        self.channel=True  # the observer and fixture modes keep the declared channel
        if mode=='active':
            from . import switch_policy
            from .active_policy import ActivePolicy
            config=switch_policy.with_overrides(switch_policy.load(),overrides)
            self.policy=ActivePolicy(self.model,OWNER,advisor=advisor,config=config)
            self.channel=switch_policy.delegating(config)
        elif overrides:
            raise ValueError('Policy overrides apply to the active arm only')
        elif fixture_policy is not None:
            from .fixture_dispatch import ProxyPolicy
            self.policy=ProxyPolicy(fixture_policy,self.model,OWNER)  # refuses anything but the fixture
        self.limit,self.tools,self.threshold=limit_usd,tools,threshold
        self.binding={}

    def verify(self):
        problem=self.advisor.problem() if self.advisor is not None and self.advisor.live else None
        if problem:
            raise RuntimeError(problem)

    def use_catalog(self,catalog):
        """The account catalog prefetched through the trial's (filtered) proxy: Jev describes its models from it."""
        self.catalog={k:catalog.get(k) for k in ('status','models','error') if k in catalog}
        if self.policy is not None and hasattr(self.policy,'catalog'):
            self.policy.catalog=catalog.get('entries') or []

    def prefix(self):
        """What the adapter adds before the task prompt; Jev's advisor is given the prompt without it."""
        return ((channel_declaration(self.task,self.code)+'\n\n' if self.channel else '')+
                (TOOLS_NOTE+'\n\n' if self.tools else ''))

    def setup(self,trial):
        self.directory=trial.dir
        self.db=trial.dir/'governor.sqlite3'
        self.session='bench-'+trial.session_id
        self.python=trial.python
        gov=Governor(self.db,self.session,self.limit)
        try:
            self.task=gov.state.create('benchmark',trial.task['instruction'])['id']
            gov.state.claim(self.task,1,OWNER,seconds=3600)
            # Declared in the ledger only when the prompt declares it: hooks deliver nothing to an agent never told.
            self.code=gov.declare_channel() if self.channel else None
        finally:
            gov.close()
        self.settings=trial.dir/'modelpilot-settings.json'
        self.settings.write_text(json.dumps(hook_settings(self.python),indent=2)+'\n')
        if self.tools:
            from .bench_tools import mcp_config
            spec=trial.dir/'modelpilot-tools-task.json'
            spec.write_text(json.dumps({k:trial.task.get(k) for k in ('suite_command','pythonpath')})+'\n')
            self.mcp=trial.dir/'modelpilot-mcp.json'
            self.mcp.write_text(json.dumps(mcp_config(sys.executable,self.db,self.session,self.limit,self.task,trial.work,
                                                      spec,trial.python,trial.dir/'home',trial.dir/'tmp',
                                                      self.threshold),indent=2)+'\n')
        if self.policy is not None and hasattr(self.policy,'prompt_prefix'):
            self.policy.prompt_prefix=self.prefix()
        if self.policy is not None and hasattr(self.policy,'exploration_key'):  # the trial's own exploration draw
            self.policy.exploration_key=f"{trial.record.get('task')}/{self.arm_id}/{trial.record.get('trial')}"
        if self.policy is not None and hasattr(self.policy,'workspace'):  # consult briefs diff against the base commit
            self.policy.workspace,self.policy.base=trial.work,getattr(trial,'base_commit',None)
        self.binding={'MODELPILOT_DB':str(self.db),'MODELPILOT_SESSION':self.session,
                      'MODELPILOT_LIMIT_USD':str(self.limit),'MODELPILOT_TASK':self.task,
                      'MODELPILOT_OWNER':OWNER,'MODELPILOT_HOOK_ERRORS':str(trial.dir/'hook-errors.jsonl')}
        from .active_policy import reviews_at_finish
        if self.policy is not None and hasattr(self.policy,'config') and reviews_at_finish(self.policy.config):
            self.binding[REVIEW_AT_STOP]='1'  # the Stop hook holds the agent's finish once per turn for the review

    def proxy_options(self):
        options={'governor':{'db':self.db,'session':self.session,'limit_usd':self.limit,'task':self.task}}
        if self.policy is not None:
            options['policy']=self.policy
        return options

    def command(self,command,proxy_url):
        result=list(command)
        i=result.index('-p')+1
        prompt=self.prefix()
        if self.tools:
            from .bench_tools import TOOL_NAMES
            result[result.index('--mcp-config')+1]=str(self.mcp)
            j=result.index('--allowedTools')+1
            result[j]=','.join([result[j]]+TOOL_NAMES)
        result[i]=prompt+result[i]
        return result+['--effort',self.effort,'--settings',str(self.settings)]

    def environment(self,env):
        return dict(env,**self.binding)

    def accounting(self,rows,final):
        from .bench import accounting
        report=accounting(rows,final)
        report['cost_complete']=report['cost_usd'] is not None and report['tokens_match'] and not report['rejected_requests']
        if not report['cost_complete']:
            report['cost_usd']=None
        if self.advisor is not None and self.advisor.live:
            report['cost_scope']='provider_only_router_unpriced'  # Jev's TypeSafe calls are billed separately, unpriced
        return report

    def evidence(self,directory):
        gov=Governor(self.db,self.session,self.limit)
        try:
            policy=gov.policy()
            hooks=gov.journal('hook_event')
            tool_calls=[e['payload'] for e in gov.journal('bench_tool')]
            dispatch,keep=(self.policy.Dispatch.journal_kind,self.policy.keep_kind) if self.policy else (None,None)
            escalations=[{k:e['payload'].get(k) for k in ('action','status','target_model','target_effort','trigger')
                          if k in e['payload']} for e in gov.journal(dispatch)] if dispatch else []
            decisions=[{k:e['payload'].get(k) for k in ('point','trigger','action','reason','current','target',
                                                          'benefit_usd','required_usd','jev','profile')}
                       for e in gov.journal('advisor_decision')]
            advice=[e['payload'].get('advice') or {} for e in gov.journal('advisor_decision')]
            kept=len(gov.journal(keep)) if keep else 0
            delegated=[{k:e['payload'].get(k) for k in ('kind','trigger','reason','setting','source','target','status',
                                                        'stop_reason','cost_usd','delivered','brief_bytes')}
                       for e in gov.journal('delegation')]
            stops=[e['payload'] for e in gov.journal('policy_stop')]
            explored=[e['payload'] for e in gov.journal('exploration')]
            held=len(gov.journal('review_block'))  # finishes the Stop hook held for a review
            from .policy_actions import escalation_proposal
            try:
                row=gov.state.get(self.task)
                proposal=escalation_proposal(gov.state,self.task,row['revision'],OWNER,self.model,self.effort)
            except ValueError:
                proposal={'action':'defer','reason':'task_not_owned_or_acknowledged','applied':False}
        finally:
            gov.close()
        log=Path(directory)/'observations.jsonl'
        rows=[json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        messages=[r for r in rows if r.get('kind')=='messages']
        refused=[r for r in rows if r.get('kind')=='refused']
        # The policy's own consults and notes the governor admitted: their spend is in the governor's total too.
        sides=[r for r in rows if r.get('kind')=='side_call' and r.get('status')!='refused']
        settled=all(r.get('governor_status')=='settled' for r in messages+sides)
        known=sum(r.get('cost_usd') or 0 for r in messages+sides)
        policy_file=Path(__file__).resolve().parents[1]/'docs/m6-modelpilot-policy.md'
        mode=self.policy.mode if self.policy else 'dry-run'
        active=mode=='active'
        fixture={'escalations':escalations,'kept_requests':kept} if self.policy else None
        from .active_policy import parameters
        live=self.advisor is not None and self.advisor.live
        catalog_ok=bool(self.catalog) and self.catalog.get('status')==200 and set(self.catalog.get('models') or [])==set(self.models or ())
        from . import switch_policy
        exploring=bool(active and hasattr(self.policy,'config') and switch_policy.exploration(self.policy.config)['enabled'])
        # The exploration arm has no advisor by design: its moves are drawn at random, not advised.
        eligible=active and (live or exploring) and catalog_ok
        out={'kind':'modelpilot_policy','mode':mode,'applied':any(r.get('applied') for r in messages),
             'active_policy_implemented':active,'benchmark_eligible':eligible,
             'tools':{'enabled':self.tools,'threshold_bytes':self.threshold,'calls':tool_calls},
             'channel_declared':self.channel,
             'fixture_policy':None if active else fixture,
             'policy':dict(fixture,stops=stops,refusals=[r.get('refusal') for r in refused],decisions=decisions,
                           delegation=delegated,held_finishes=held,exploration=explored if exploring else None,
                           parameters=parameters(self.model,self.effort,overrides=self.overrides))
                 if active else None,
             'advisor':{'live':live,'calls':sum(bool(a) for a in advice),'failures':sum(bool(a.get('error')) for a in advice),
                        'auth_failures':sum(bool(a.get('auth')) for a in advice),
                        'router_usage':[a['usage'] for a in advice if a.get('usage')],'catalog':self.catalog}
                 if active else None,
             'policy_sha256':hashlib.sha256(policy_file.read_bytes()).hexdigest(),
             'governor':policy,'escalation_proposal':proposal,'hook_events':len(hooks),'all_requests_settled':bool(messages) and settled,
             'accounting_matches':bool(messages) and settled and policy['cost_complete'] and
                 all(r.get('cost_usd') is not None for r in messages+sides) and abs(known-policy['spent_usd'])<1e-9,
             # Requests the governor would have refused; policy tickets carry their own admission.
             'would_refuse':sum('governor' in r and not r['governor']['admitted'] for r in messages)}
        if not active:
            out['ineligible_reason']='fixture_policy' if self.policy else 'observer_only'
        elif not eligible:  # a stub advisor is never evidence of Jev's advice
            out['ineligible_reason']=('no_advisor' if self.advisor is None else 'advisor_stub' if not live
                                      else 'catalog_incomplete')
        return out
