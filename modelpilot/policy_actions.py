"""Offline policy proposals and request copies; no forwarding or escalation confirmation."""
from contextlib import nullcontext
import copy
import json
import math
from pathlib import Path
from .cache_probe import tier
from .proxy import effective_effort, reservation_estimate  # effective_effort: re-exported

# The policy's models, cheapest first, and what each accepts, from configs/modelpilot-policy.json so the tier set
# can change without code. Opus 5.5 replaced Opus 5 on September 26, Sonnet 5.5 replaced Sonnet 5 on September 28 and
# Haiku 5.5 replaced Haiku 4.5 on October 8 (docs/m6-modelpilot-policy.md, "Tier set").
POLICY_CONFIG=Path(__file__).resolve().parents[1]/'configs/modelpilot-policy.json'
_CONFIG=json.loads(POLICY_CONFIG.read_text())
MODELS=tuple(sorted(_CONFIG['models'],key=lambda m:_CONFIG['models'][m]['rank']))
EFFORTS=tuple(_CONFIG['effort_order'])
MODEL_EFFORTS={m:tuple(_CONFIG['models'][m]['efforts']) for m in MODELS}
MID_CONVERSATION_SYSTEM={m:_CONFIG['models'][m]['mid_conversation_system'] for m in MODELS}
LADDER_EFFORTS=('low','medium','high')  # the fixture-only ladder (ProxyPolicy); the active arm never climbs
# (source model, target model) changes the provider accepted with thinking history, from
# thinking_probe evidence only; the same model twice means an effort change (docs/thinking-history-probe.md).
# Opus 5.5 effort: 4/4 in runs/thinking-probe-top-rung-20260926-153723. Sonnet 5.5 effort and Sonnet 5.5 <-> Opus 5.5:
# 8/8, 4/4 and 4/4 in runs/thinking-probe-sonnet-5-5-20260928-133426 (the API drops the other model's thinking on a
# switch, unbilled). The Sonnet 5 pairs left with that tier. Sonnet 5.5 <-> Haiku 5.5 and Haiku's effort: accepted in
# both shapes in runs/thinking-probe-haiku-5-5-20261009-124512. Haiku 5.5 <-> Opus 5.5 is not probed, so it is refused.
# A pair is (a model whose thinking may be in the history, the target): not only the request's model, since a session
# that has moved holds every earlier model's thinking (history_models).
THINKING_HISTORY_VERIFIED=frozenset({('claude-opus-5-5','claude-opus-5-5'),('claude-sonnet-5-5','claude-sonnet-5-5'),
                                     ('claude-sonnet-5-5','claude-opus-5-5'),('claude-opus-5-5','claude-sonnet-5-5'),
                                     ('claude-sonnet-5-5','claude-haiku-5-5'),('claude-haiku-5-5','claude-sonnet-5-5'),
                                     ('claude-haiku-5-5','claude-haiku-5-5')})


def setting(model,effort):
    if model not in MODELS or (effort not in MODEL_EFFORTS[model] if MODEL_EFFORTS[model] else effort is not None):
        raise ValueError('Unsupported model/effort combination')


def has_thinking(messages):
    return any(block.get('type') in ('thinking','redacted_thinking')
               for message in messages for block in (message.get('content') if isinstance(message.get('content'),list) else [])
               if isinstance(block,dict))


def transform_request(request,model,effort,allow_thinking_history=False,history_models=(),returns_to=None):
    """allow_thinking_history is for thinking_probe only; the policy relies on THINKING_HISTORY_VERIFIED.
    history_models: models besides the request's whose thinking may be in its history (those a session's requests
    went to). returns_to: for a move, the model the session goes back to on a deferral or correction (the client's),
    which then gets the target's thinking; the pair back must be verified too, whenever the request can think."""
    setting(model,effort)
    if request.get('model') not in MODELS or any(m not in MODELS for m in history_models):
        raise ValueError('Unknown source model')
    source_effort=request.get('output_config',{}).get('effort')
    changed=request['model']!=model or source_effort!=effort
    messages=request.get('messages')
    if not isinstance(messages,list):raise ValueError('Messages must be an array')
    sources={request['model'],*history_models}
    gated=not (allow_thinking_history or all((m,model) in THINKING_HISTORY_VERIFIED for m in sources))
    if changed and gated and has_thinking(messages):
        raise ValueError('Thinking history across setting changes is not validated')
    thinks=(request.get('thinking') or {}).get('type') not in (None,'disabled') or has_thinking(messages)
    if (changed and returns_to is not None and returns_to!=model and thinks and not allow_thinking_history
            and (model,returns_to) not in THINKING_HISTORY_VERIFIED):
        raise ValueError('Thinking history across setting changes is not validated: no verified way back to '+returns_to)
    out=copy.deepcopy(request)
    out['model']=model
    if not MODEL_EFFORTS[model]:  # no effort or adaptive thinking: none of today's tiers (Haiku 4.5 until October 8)
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
    # Claude Code 2.1.284 itself sends system-role messages (after every user turn) to Sonnet 5.5, Opus 5.5 and Haiku 5.5,
    # as 2.1.282 did to Sonnet 5 and Opus 5, so they are kept. Haiku 4.5 rejected them (Jev finding): for a tier that
    # does, trailing ones are relocated, and only those.
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


# Per-message effort (beta): an effort-only system message changes effort from the next user turn without invalidating
# the prompt cache, where a top-level effort change rewrites the messages. It must stay where it was first sent.
PLACEMENTS=('before_result','after_result')


def effort_message(effort):
    return {'role':'system','content':[],'output_config':{'effort':effort}}


def effort_anchor(messages,placement):
    """Where an effort message goes in a request: before its newest user message, or right after it, and always after
    the client's own effort message. Claude Code 2.1.284 puts its --effort value in the system note after the prompt;
    a later effort message is the one that holds, so an earlier one would be overridden by the client's."""
    if placement not in PLACEMENTS:raise ValueError(f'Unknown placement {placement!r}')
    last_user=max(i for i,m in enumerate(messages) if m.get('role')=='user')
    clients=[i for i,m in enumerate(messages) if m.get('role')=='system' and isinstance(m.get('output_config'),dict)]
    return max(last_user if placement=='before_result' else last_user+1,(clients[-1]+1) if clients else 0)


def with_effort_messages(request,injections):
    """The request with each (index, effort, ...) effort message inserted at its index in the client's own message
    list, the one it was first sent at; the top-level effort is left as it is."""
    out=copy.deepcopy(request)
    for item in sorted(injections,key=lambda i:i[0],reverse=True):
        out['messages'].insert(item[0],effort_message(item[1]))
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
    transformed=transform_request(request,proposal['target_model'],proposal['target_effort'],
                                  history_models=tuple(proposal.get('history_models') or ()),returns_to=proposal.get('returns_to'))
    rate=tier(rates.get(transformed['model']))  # priced by prompt length: the dearest tier, as the reservation
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
