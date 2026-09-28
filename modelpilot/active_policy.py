"""The ModelPilot benchmark arm's active policy (user-approved September 26, the benchmark arm only).

Everywhere else the governor stays dry-run. This policy applies rules R1, R2, R5 and R6 of
docs/m6-modelpilot-policy.md in the trial's proxy:
- R1: the client starts at the arm's S0: Sonnet 5.5 medium (`modelpilot`) or Opus 5.5 medium
  (`modelpilot-o55`); the tuning split chooses between them.
- R2: the stuck ladder raises effort, then the model (Opus 5.5), one rung per window, each
  escalation admitted only if the limit covers its full rebuild, and kept for the task revision. A
  further stuck window (re_diagnose or human_review) stops the task: the proxy refuses the
  client's next main-loop request and the trial is recorded as unfinished.
- R5: tools (bench_tools) are wired by the adapter, not here.
- R6: every Messages request is admitted before it is sent, on measured spend against the
  same per-task limit as the other arms; unknown cost halts. A refused request is answered
  by the proxy with an API-style error and never reaches the provider.
Not implemented: R3/R4 cost-motivated switches (including every Haiku target) and worker drafts.
"""
from .fixture_dispatch import STOPS, Dispatcher, ProxyPolicy
from .m2 import LADDER
from .policy_actions import next_setting


def parameters(model,effort):
    """The arm's frozen parameters, with the ladder the stuck recommendations walk from its start setting."""
    ladder,setting=[f'{model}/{effort}'],(model,effort)
    for action in LADDER:
        action,*setting=next_setting(action,*setting)
        if action in STOPS:
            break
        ladder.append('/'.join(setting))
    return {'S0':[model,effort],'ladder':ladder,'stop_on':list(STOPS),'stuck':'m2 heuristic-v1 (score >= 3, window 6)',
            'admission':'measured spend below the per-task limit; an escalation also needs the limit to cover '
                        'its full rebuild (request bytes/3 tokens at the dearest write rate), not its output allowance',
            'rules':['R1','R2','R5','R6'],'not_implemented':['R3','R4','Haiku targets','worker drafts (lever 3)']}


class ActiveDispatcher(Dispatcher):
    gate,reserve_output='spent',False


class ActivePolicy(ProxyPolicy):
    active,mode,gate,keep_kind,Dispatch=True,'active','spent','policy_keep',ActiveDispatcher

    def __init__(self,client_model,owner):
        self.client_model,self.owner=client_model,owner

    def check_upstream(self,origin):
        pass  # ProxyServer accepts only direct Anthropic HTTPS or loopback HTTP (offline tests).

    def dispatcher(self,gov,rates):
        return ActiveDispatcher(gov,rates)
