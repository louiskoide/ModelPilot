"""Synthetic policy dispatch: in-process fixtures, or a proxy whose upstream is the owned fixture server.

No URLs, credentials or live transport are accepted.
"""
import hashlib
import json
import math
import uuid
from .cache_probe import priced_usage
from .fixtures import FixtureServer
from .proxy import request_effort, reservation_estimate
from .policy_actions import escalation_proposal, prepare_action, transform_request


class StrictFixture:
    """Checks the candidate request shape and returns fixed synthetic usage, not model output."""
    def __init__(self):self.calls=0

    def reply(self,request):
        self.calls+=1
        if request['model']=='claude-haiku-4-5-20251001' and any(m['role']=='system' for m in request['messages']):
            raise ValueError('Unconverted system message')
        if request['model']=='claude-haiku-4-5-20251001' and ('thinking' in request or request.get('output_config',{}).get('effort')):
            raise ValueError('Unsupported Haiku setting')
        return {'model':request['model'],'usage':{'input_tokens':100,'output_tokens':4,
                'cache_creation_input_tokens':0,'cache_read_input_tokens':0,
                'service_tier':'standard','inference_geo':'global'}}


class FixtureDispatcher:
    def __init__(self,governor,rates,fixture):
        from .loopback_fixture import LoopbackFixture
        # FixtureServer only serves requests forwarded by a ProxyPolicy proxy (begin/finish).
        if type(fixture) not in (StrictFixture,LoopbackFixture,FixtureServer):
            raise ValueError('Only owned in-process or loopback fixtures are accepted; no live transport')
        self.gov,self.rates,self.fixture=governor,rates,fixture

    def dispatch(self,proposal,owner,request):
        ticket=self.begin(proposal,owner,request)
        if ticket['status']=='deferred':return ticket
        response=error=None
        try:response=self.fixture.reply(ticket['request'])
        except Exception as exc:error=type(exc).__name__
        return self.finish(ticket,response,error)

    def begin(self,proposal,owner,request):
        """Fence and reserve one escalation; returns the exact request to send, or a deferral."""
        # Bind inputs before validation so a caller cannot change the dispatched payload later.
        proposal=json.loads(json.dumps(proposal));request=json.loads(json.dumps(request))
        policy=self.gov.policy()
        prepared=prepare_action(self.gov.state,proposal,owner,request,
                                policy['available_usd'] if policy['cost_complete'] else None,self.rates)
        if prepared['action']!='prepared_offline':
            return {'status':'deferred','reason':prepared['reason'],'applied':False}
        # One attempt per escalation window, even if new observations arrive before completion.
        identity=[self.gov.session,proposal['task'],proposal['revision'],proposal['level']]
        request_id='fixture-'+hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        def fence():
            policy=self.gov._policy()
            checked=prepare_action(self.gov.state,proposal,owner,request,
                                    policy['available_usd'] if policy['cost_complete'] else None,self.rates)
            if checked!=prepared:raise ValueError('Admission changed after preparation')
        admitted=self.gov.admit(request_id,prepared['reserve_usd'],proposal['task'],proposal['revision'],fence=fence)
        if not admitted['admitted']:
            return {'status':'deferred','reason':admitted['reason'],'applied':False}
        return {'status':'admitted','request_id':request_id,'proposal':proposal,'owner':owner,
                'request':prepared['request'],'reserve_usd':prepared['reserve_usd']}

    def finish(self,ticket,response,error=None,applied=False):
        """Settle measured cost (None stays unknown) and confirm the rung only for a priced reply."""
        proposal,request=ticket['proposal'],ticket['request']
        result={'request_id':ticket['request_id'],'status':'unknown_outcome','applied':applied,'fixture_only':True,
                'target_model':proposal['target_model'],'target_effort':proposal['target_effort'],
                'action':proposal['action'],'level':proposal['level'],
                'request_sha256':hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()}
        actual=None
        if error is not None:
            result['error_type']=error
        elif response is not None:
            actual=priced_usage(request,response.get('model'),response.get('usage'),self.rates)
            if actual is not None and (not math.isfinite(actual) or actual<0):
                actual=None
            result['usage']=response.get('usage')
        # A transport failure/unknown model never means zero cost. Persistent unknown spend halts admission.
        self.gov.settle(ticket['request_id'],actual)
        result['cost_usd']=actual
        if actual is not None:
            try:
                self.gov.state.confirm_escalation(proposal['task'],proposal['revision'],ticket['owner'],proposal['last_event'])
                result['status']='fixture_confirmed'
            except ValueError:
                result['status']='stale_after_send'
        with self.gov.db:
            self.gov._journal('fixture_dispatch',result,proposal['task'],proposal['revision'])
        return result


EXECUTABLE=('increase_effort','stronger_model')


class ProxyPolicy:
    """Applies ladder escalations to a real client's requests inside the proxy, fixture upstream only.

    Only main-loop requests (the configured client model, with tools) are considered. An admitted
    escalation is sent once per window through FixtureDispatcher; once confirmed, later requests
    of the same task revision keep the escalated setting, each reserved with admission enforced.
    A correction (new revision) resets it with the ladder. Anything refused, stale or unknown is
    forwarded unchanged, as the dry-run proxy does; ModelPilot never blocks the client.
    """
    def __init__(self,upstream,client_model,owner):
        if type(upstream) is not FixtureServer:
            raise ValueError('Policy dispatch requires the owned in-process fixture upstream; no live transport')
        self.upstream,self.client_model,self.owner=upstream,client_model,owner

    def check_upstream(self,origin):
        if origin.scheme!='http' or origin.hostname!='127.0.0.1' or origin.port!=self.upstream.server_port:
            raise ValueError('Proxy upstream is not the owned fixture server')

    @staticmethod
    def effective(gov,task,revision):
        """Setting confirmed for this task revision, or None when the client's own applies."""
        confirmed=[e['payload'] for e in gov.journal('fixture_dispatch')
                   if e['task']==task and e['revision']==revision and e['payload']['status']=='fixture_confirmed']
        return (confirmed[-1]['target_model'],confirmed[-1]['target_effort']) if confirmed else None

    def plan(self,gov,rates,task,request):
        """Returns a ticket for an applied request, or a deferral dict (forward the original)."""
        if request.get('model')!=self.client_model or not request.get('tools'):
            return {'status':'not_main_loop'}
        client=(request['model'],request_effort(request))
        row=gov.state.get(task)
        revision=row['revision']
        if row['ack_revision']!=revision:
            return {'status':'deferred','reason':'unacknowledged_revision'}
        setting=self.effective(gov,task,revision) or client
        try:
            current=transform_request(request,*setting) if setting!=client else request
            proposal=escalation_proposal(gov.state,task,revision,self.owner,*setting)
        except ValueError as exc:
            return {'status':'deferred','reason':'refused:'+str(exc)}
        dispatcher=FixtureDispatcher(gov,rates,self.upstream)
        deferral=None
        if proposal['action'] in EXECUTABLE:
            try:
                ticket=dispatcher.begin(proposal,self.owner,current)
            except ValueError as exc:
                ticket={'status':'deferred','reason':'refused:'+str(exc)}
            if ticket['status']=='admitted':
                return dict(ticket,kind='escalation')
            deferral=ticket
        if setting==client:
            return deferral or {'status':'deferred','reason':'no_executable_escalation'}
        # Keep the confirmed setting: a fresh, enforced reservation fenced on the same revision.
        request_id='mp-'+uuid.uuid4().hex
        def fence():
            gov.state._owned(task,revision,self.owner)
            if self.effective(gov,task,revision)!=setting:raise ValueError('Escalated setting changed')
        try:
            admitted=gov.admit(request_id,reservation_estimate(json.dumps(current).encode(),current,rates),
                               task,revision,fence=fence)
        except ValueError as exc:
            return {'status':'deferred','reason':'refused:'+str(exc)}
        if not admitted['admitted']:
            return {'status':'deferred','reason':admitted['reason']}
        return {'status':'admitted','kind':'keep_escalated','request_id':request_id,'request':current,
                'target_model':setting[0],'target_effort':setting[1],'task':task,'revision':revision}

    def finish(self,gov,rates,ticket,cost,model,usage):
        if ticket['kind']=='escalation':
            dispatcher=FixtureDispatcher(gov,rates,self.upstream)
            response=None if cost is None else {'model':model,'usage':usage}
            error=None if cost is not None else 'UnpricedOrIncompleteResponse'
            return dispatcher.finish(ticket,response,error,applied=True)
        gov.settle(ticket['request_id'],cost)
        result={'request_id':ticket['request_id'],'status':'kept' if cost is not None else 'unknown_outcome',
                'applied':True,'fixture_only':True,'target_model':ticket['target_model'],
                'target_effort':ticket['target_effort'],'cost_usd':cost}
        with gov.db:
            gov._journal('fixture_keep_escalated',result,ticket['task'],ticket['revision'])
        return result
