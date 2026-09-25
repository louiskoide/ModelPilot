"""In-process synthetic dispatcher. No URLs, credentials or network transport accepted."""
import hashlib
import json
import math
from .cache_probe import priced_usage
from .policy_actions import prepare_action


class StrictFixture:
    """Checks the candidate request shape and returns fixed synthetic usage, not model output."""
    def __init__(self):self.calls=0

    def reply(self,request):
        self.calls+=1
        if request['model']!='claude-opus-5' and any(m['role']=='system' for m in request['messages']):
            raise ValueError('Unconverted system message')
        if request['model']=='claude-haiku-4-5-20251001' and ('thinking' in request or request.get('output_config',{}).get('effort')):
            raise ValueError('Unsupported Haiku setting')
        return {'model':request['model'],'usage':{'input_tokens':100,'output_tokens':4,
                'cache_creation_input_tokens':0,'cache_read_input_tokens':0,
                'service_tier':'standard','inference_geo':'global'}}


class FixtureDispatcher:
    def __init__(self,governor,rates,fixture):
        from .loopback_fixture import LoopbackFixture
        if type(fixture) not in (StrictFixture,LoopbackFixture):
            raise ValueError('Only owned in-process or loopback fixtures are accepted; no live transport')
        self.gov,self.rates,self.fixture=governor,rates,fixture

    def dispatch(self,proposal,owner,request):
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
        result={'request_id':request_id,'status':'unknown_outcome','applied':False,'fixture_only':True,
                'request_sha256':hashlib.sha256(json.dumps(prepared['request'],sort_keys=True).encode()).hexdigest()}
        actual=None
        try:
            response=self.fixture.reply(prepared['request'])
            actual=priced_usage(prepared['request'],response.get('model'),response.get('usage'),self.rates)
            if actual is not None and (not math.isfinite(actual) or actual<0):
                actual=None
            result['usage']=response.get('usage')
        except Exception as exc:
            result['error_type']=type(exc).__name__
        # A transport failure/unknown model never means zero cost. Persistent unknown spend halts admission.
        self.gov.settle(request_id,actual)
        result['cost_usd']=actual
        if actual is not None:
            try:
                self.gov.state.confirm_escalation(proposal['task'],proposal['revision'],owner,proposal['last_event'])
                result['status']='fixture_confirmed'
            except ValueError:
                result['status']='stale_after_send'
        with self.gov.db:
            self.gov._journal('fixture_dispatch',result,proposal['task'],proposal['revision'])
        return result
