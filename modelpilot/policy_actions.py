"""Offline policy proposals and request copies; no forwarding or escalation confirmation."""
from contextlib import nullcontext
import copy
import json
import math
from .proxy import reservation_estimate

MODELS=('claude-haiku-4-5-20251001','claude-sonnet-5','claude-opus-5')
EFFORTS=('low','medium','high')


def setting(model,effort):
    if model not in MODELS or (effort is not None if model==MODELS[0] else effort not in EFFORTS):
        raise ValueError('Unsupported model/effort combination')


def transform_request(request,model,effort):
    setting(model,effort)
    if request.get('model') not in MODELS:
        raise ValueError('Unknown source model')
    source_effort=request.get('output_config',{}).get('effort')
    changed=request['model']!=model or source_effort!=effort
    messages=request.get('messages')
    if not isinstance(messages,list):raise ValueError('Messages must be an array')
    if changed and any(block.get('type') in ('thinking','redacted_thinking')
            for message in messages for block in (message.get('content') if isinstance(message.get('content'),list) else [])
            if isinstance(block,dict)):
        raise ValueError('Thinking history across setting changes is not validated')
    out=copy.deepcopy(request)
    out['model']=model
    if model==MODELS[0]:
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
    # Claude Code 2.1.282 itself sends system-role messages (after every user turn) to Sonnet 5
    # and Opus 5, so they are kept. Haiku rejects them (Jev finding): relocate trailing ones only.
    if model==MODELS[0]:
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


def escalation_proposal(state,task,revision,owner,model,effort):
    setting(model,effort)
    nested=state.db.in_transaction
    with nullcontext() if nested else state.db:
        if not nested:
            state.db.execute('BEGIN IMMEDIATE')
        state._owned(task,revision,owner)
        recommendation=state.recommend(task)
        target,target_effort=model,effort
        action=recommendation['recommendation']
        if action=='increase_effort' and effort in EFFORTS and effort!='high':
            target_effort=EFFORTS[EFFORTS.index(effort)+1]
        elif action in ('increase_effort','stronger_model') and model!=MODELS[-1]:
            target=MODELS[MODELS.index(model)+1];target_effort='medium'
        elif action!='hold':
            action='human_review' if action=='human_review' else 're_diagnose'
        return {'task':task,'revision':revision,'last_event':recommendation['last_event'],
                'level':recommendation['level'],'source_model':model,'source_effort':effort,
                'target_model':target,'target_effort':target_effort,'action':action,'applied':False}


def prepare_action(state,proposal,owner,request,available_usd,rates):
    fresh=escalation_proposal(state,proposal['task'],proposal['revision'],owner,
                             proposal['source_model'],proposal['source_effort'])
    if fresh!=proposal:raise ValueError('Proposal no longer matches host ledger evidence')
    if request.get('model')!=proposal['source_model'] or request.get('output_config',{}).get('effort')!=proposal['source_effort']:
        raise ValueError('Request setting changed after proposal')
    result={'action':'defer','applied':False,'reason':'no_executable_escalation','proposal':proposal}
    if proposal['action'] not in ('increase_effort','stronger_model'):return result
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
    reserve=reservation_estimate(json.dumps(transformed).encode(),transformed,rates)
    result.update(reserve_usd=reserve,reason='insufficient_write_reservation')
    if reserve<=available_usd:
        result.update(action='prepared_offline',reason='requires_dispatch_time_fence_and_reservation',request=transformed)
    return result
