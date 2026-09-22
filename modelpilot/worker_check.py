"""Bounded live M3 validation: persistent search and fixed-test worker sessions."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import time
import uuid
from .cache_probe import send, error_details
from .m2 import State
from .workers import WorkerPool


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    if not args.live:
        print('Plan: four file-search tasks and two fixed-test tasks; six Sonnet API calls. Add --live to run.')
        return
    key=os.environ.get('ANTHROPIC_API_KEY') or getpass.getpass('Paste API key (hidden): ').strip()
    if not key.startswith('sk-ant-') or any(c.isspace() for c in key):
        raise SystemExit('Invalid key format; no requests sent.')
    os.environ['ANTHROPIC_API_KEY']=key
    root=Path(__file__).resolve().parents[1]
    run=root/'runs'/('m3-workers-'+time.strftime('%Y%m%d-%H%M%S'));run.mkdir(mode=0o700)
    fixture=run/'fixture';fixture.mkdir()
    (fixture/'test_fixture.py').write_text('import os, unittest\nclass Fixture(unittest.TestCase):\n    def test_sum(self): self.assertEqual(2+2,4)\n    def test_no_provider_key(self): self.assertNotIn("ANTHROPIC_API_KEY",os.environ)\n')
    config=json.loads((root/'configs/m0.json').read_text())
    pool=WorkerPool(root=fixture,db=run/'state.sqlite3',model='claude-sonnet-4-6',rates=config['rates'],
                    test_command=[sys.executable,'-m','unittest','discover','-s','.','-p','test_fixture.py','-v'],
                    max_calls=4,budget_usd=.50)
    state=State(run/'state.sqlite3')
    report={'status':'running','cases':[],'automatic_routing':False,
            'limits':'Six one-call tasks; per-role $0.50 stop threshold, not a hard billing ceiling.'}
    print(f'Running M3 live check; results: {run}',flush=True)
    started=time.monotonic()
    try:
        for index in range(4):
            marker='TOKEN_'+uuid.uuid4().hex
            content='\n'.join(f'catalog record {i}: deterministic fixture line for persistent file-search testing' for i in range(120))
            (fixture/'catalog.txt').write_text(content+'\ncatalog TARGET='+marker+'\n')
            task=state.create('Search fixture '+str(index),'Find the TARGET verification token in the current search evidence and return only that exact token. Do not reuse an earlier task token.')
            result=pool.dispatch('file_search',task=task['id'],revision=1,files=['catalog.txt'],query='catalog')
            passed=marker in result['result']['digest']
            report['cases'].append({'name':'search_'+str(index),'passed':passed,**result})
            print('search_'+str(index),'PASS' if passed else 'FAIL',flush=True)
            if not passed: raise ValueError('Worker did not return current evidence token')
        for index in range(2):
            task=state.create('Test fixture '+str(index),'Report the exit code, number of tests, and PASS if the current approved test run succeeded. Otherwise report FAIL. Do not claim earlier results apply.')
            result=pool.dispatch('test_runner',task=task['id'],revision=1,files=['test_fixture.py'])
            passed=result['operation']['exit_code']==0 and 'PASS' in result['result']['digest']
            report['cases'].append({'name':'tests_'+str(index),'passed':passed,**result})
            print('tests_'+str(index),'PASS' if passed else 'FAIL',flush=True)
            if not passed: raise ValueError('Worker did not report successful current test evidence')
        assert [c['prior_messages'] for c in report['cases']]==[0,2,4,6,0,2]
        report['status']='passed'
    except Exception as error:
        report.update(status='failed',error_type=type(error).__name__,error_hint=error_details(error)['error_hint'])
    finally:
        report['wall_seconds']=time.monotonic()-started
        report['calls']=sum(w.calls for w in pool.workers.values())
        report['known_cost_usd']=sum(w.spent for w in pool.workers.values())
        report['cost_complete']=not any(w.unknown_cost for w in pool.workers.values())
        report['cache_read_tokens']=sum(c.get('usage',{}).get('cache_read_input_tokens',0) for c in report['cases'])
        report['cache_write_tokens']=sum(c.get('usage',{}).get('cache_creation_input_tokens',0) for c in report['cases'])
        if not report['cost_complete']:report['status']='failed'
        (run/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
        state.close()
        compact={k:v for k,v in report.items() if k!='cases'}
        print(json.dumps(compact,indent=2),flush=True)
    if report['status']!='passed': raise SystemExit(1)


if __name__=='__main__':main()
