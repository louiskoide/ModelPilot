"""Offline policy proposals and request copies; no forwarding or escalation confirmation."""
from contextlib import nullcontext
import copy
import json
import math
from pathlib import Path
from .proxy import reservation_estimate

# The policy's models, cheapest first, and what each accepts, from configs/modelpilot-policy.json so the tier set
# can change without code. Opus 5.5 replaced Opus 5 on September 26 and Sonnet 5.5 replaced Sonnet 5 on
# September 28 (docs/m6-modelpilot-policy.md, "Tier set").
POLICY_CONFIG=Path(__file__).resolve().parents[1]/'configs/modelpilot-policy.json'
_CONFIG=json.loads(POLICY_CONFIG.read_text())
MODELS=tuple(sorted(_CONFIG['models'],key=lambda m:_CONFIG['models'][m]['rank']))
EFFORTS=tuple(_CONFIG['effort_order'])
MODEL_EFFORTS={m:tuple(_CONFIG['models'][m]['efforts']) for m in MODELS}
MID_CONVERSATION_SYSTEM={m:_CONFIG['models'][m]['mid_conversation_system'] for m in MODELS}
LADDER_EFFORTS=('low','medium','high')  # the fixture-only ladder (ProxyPolicy); the active arm never climbs
# (source model, target model) changes the provider accepted with thinking history, from
# thinking_probe evidence only; the same model twice means an effort change (docs/thinking-history-probe.md).
# Opus 5.5 effort: 4/4 in runs/thinking-probe-top-rung-20260926-153723. Its Sonnet 5 pairs (and Sonnet 5 effort,
# runs/thinking-probe-transitions-20260926-131953) left with the Sonnet 5 tier; no other model reads Sonnet 5.5's
# thinking blocks, so its pairs need their own probe run (suite sonnet-5-5) before its rungs and the Opus 5.5
# correction reset can rewrite requests whose history holds thinking. Haiku targets are refused by the transform itself.
THINKING_HISTORY_VERIFIED=frozenset({('claude-opus-5-5','claude-opus-5-5')})


def setting(model,effort):
    if model not in MODELS or (effort not in MODEL_EFFORTS[model] if MODEL_EFFORTS[model] else effort is not None):
        raise ValueError('Unsupported model/effort combination')


def transform_request(request,model,effort,allow_thinking_history=False):
    """allow_thinking_history is for thinking_probe only; the policy relies on THINKING_HISTORY_VERIFIED."""
    setting(model,effort)
    if request.get('model') not in MODELS:
        raise ValueError('Unknown source model')
    source_effort=request.get('output_config',{}).get('effort')
    changed=request['model']!=model or source_effort!=effort
    messages=request.get('messages')
    if not isinstance(messages,list):raise ValueError('Messages must be an array')
    gated=not (allow_thinking_history or (request['model'],model) in THINKING_HISTORY_VERIFIED)
    if changed and gated and any(block.get('type') in ('thinking','redacted_thinking')
            for message in messages for block in (message.get('content') if isinstance(message.get('content'),list) else [])
            if isinstance(block,dict)):
        raise ValueError('Thinking history across setting changes is not validated')
    out=copy.deepcopy(request)
    out['model']=model
    if not MODEL_EFFORTS[model]:  # no effort or adaptive thinking (Haiku 4.5)
        out.pop('thinking',None)
        if 'output_config' in out:
            out['output_config'].pop('effort',None)
            if not out['output_config']:out.pop('output_config')
        context=out.get('context_management',{})
        if 'edits' in context:
            context['edits']=[e for e in context['edits'] if 'thinking' not in e.get('type','').lower()]
            if not context['edits']:context.pop('edits')
            if not context:out.pop('context_management',None)
    else:
        out.setdefault('output_config',{})['effort']=effort
    # Claude Code 2.1.284 itself sends system-role messages (after every user turn) to Sonnet 5.5 and Opus 5.5,
    # as 2.1.282 did to Sonnet 5 and Opus 5, so they are kept. Haiku rejects them (Jev finding): relocate trailing ones only.
    if not MID_CONVERSATION_SYSTEM[model]:
        messages=out['messages'];tail=[]
        while messages and messages[-1].get('role')=='system':tail.insert(0,messages.pop())
        if any(m.get('role')=='system' for m in messages):
            raise ValueError('Only trailing system messages have an offline transformation')
        if tail:
            if not messages or messages[-1].get('role')!='user':
                raise ValueError('Cannot relocate trailing system context without a preceding user turn')
            content=messages[-1]['content']
            if isinstance(content,str):content=[{'type':'text','text':content}]
            if not isinstance(content,list):raise ValueError('Unsupported user content')
            for message in tail:
                blocks=message['content']
                if isinstance(blocks,str):blocks=[{'type':'text','text':blocks}]
                if not isinstance(blocks,list) or any(b.get('type')!='text' for b in blocks):
                    raise ValueError('Only text system context can be relocated')
                content.extend(blocks)
            messages[-1]['content']=content
    return out


def next_setting(action,model,effort):
    """The ladder's rule for one stuck recommendation: (action, model, effort) after it. An effort step,
    else the next model at medium; with no rung left the action becomes re_diagnose (or human_review)."""
    if action=='increase_effort' and effort in LADDER_EFFORTS and effort!=LADDER_EFFORTS[-1]:
        return action,model,LADDER_EFFORTS[LADDER_EFFORTS.index(effort)+1]
    if action in ('increase_effort','stronger_model') and model!=MODELS[-1]:
        return action,MODELS[MODELS.index(model)+1],'medium'
    if action=='hold':
        return action,model,effort
    return ('human_review' if action=='human_review' else 're_diagnose'),model,effort


def escalation_proposal(state,task,revision,owner,model,effort):
    setting(model,effort)
    nested=state.db.in_transaction
    with nullcontext() if nested else state.db:
        if not nested:
            state.db.execute('BEGIN IMMEDIATE')
        state._owned(task,revision,owner)
        recommendation=state.recommend(task)
        action,target,target_effort=next_setting(recommendation['recommendation'],model,effort)
        return {'task':task,'revision':revision,'last_event':recommendation['last_event'],
                'level':recommendation['level'],'source_model':model,'source_effort':effort,
                'target_model':target,'target_effort':target_effort,'action':action,'applied':False}


def prepare_action(state,proposal,owner,request,available_usd,rates,reserve_output=True):
    """reserve_output=False reserves the target's full rebuild (every request byte written at the
    dearest write rate) without the output allowance: the active arm's rule, since none of its
    requests reserves max_tokens (Claude Code sends 64000, $1.28 of Opus 5.5 output)."""
    fresh=escalation_proposal(state,proposal['task'],proposal['revision'],owner,
                             proposal['source_model'],proposal['source_effort'])
    if proposal.get('action')=='jump':
        # A direct move the switch policy chose (switch_policy.decide), fenced on the ledger state it was decided
        # on; a move made on stuck evidence also needs that evidence to still be there.
        keys=('task','revision','last_event','level','source_model','source_effort')
        if any(fresh[k]!=proposal[k] for k in keys) or (proposal.get('trigger')=='stuck_evidence' and fresh['action']=='hold'):
            raise ValueError('Proposal no longer matches host ledger evidence')
    elif fresh!=proposal:raise ValueError('Proposal no longer matches host ledger evidence')
    if request.get('model')!=proposal['source_model'] or request.get('output_config',{}).get('effort')!=proposal['source_effort']:
        raise ValueError('Request setting changed after proposal')
    result={'action':'defer','applied':False,'reason':'no_executable_escalation','proposal':proposal}
    if proposal['action'] not in ('increase_effort','stronger_model','jump'):return result
    if (available_usd is None or isinstance(available_usd,bool) or not isinstance(available_usd,(float,int))
            or not math.isfinite(available_usd) or available_usd<0):
        result['reason']='unknown_or_invalid_budget';return result
    transformed=transform_request(request,proposal['target_model'],proposal['target_effort'])
    rate=rates.get(transformed['model'])
    if not rate or any(isinstance(rate.get(k),bool) or not isinstance(rate.get(k),(float,int)) or not math.isfinite(rate[k]) or rate[k]<0
                       for k in ('input','write_5m','write_1h','output')):
        result['reason']='unknown_rates';return result
    if type(transformed.get('max_tokens')) is not int or transformed['max_tokens']<1:
        result['reason']='invalid_output_limit';return result
    reserve=reservation_estimate(json.dumps(transformed).encode(),
                                 transformed if reserve_output else dict(transformed,max_tokens=0),rates)
    result.update(reserve_usd=reserve,reason='insufficient_write_reservation')
    if reserve<=available_usd:
        result.update(action='prepared_offline',reason='requires_dispatch_time_fence_and_reservation',request=transformed)
    return result
