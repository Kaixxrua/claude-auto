import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import test_claude_accounts as base
import monitor as m

a = base.m

def windows(at):
    return {k:{'used_percentage':0,'resets_at':None if k=='five_hour' else at+86400,
               'observed_at':at,'source':'official_oauth_api'} for k in ('five_hour','seven_day')}

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.state=self.root/'state'
        self.registry={'version':1,'claude':'/bin/true','profiles':{n:str(self.root/n) for n in ('main','second','account07','account09')},'disabled':{'main':{'reason':'banned'}}}
        a.write_json(self.root/'accounts.json',self.registry)
        self.root_patch=patch.object(a,'ROOT',self.root);self.root_patch.start()
        self.at=time.time();self.start=self.at-.2;self.windows=windows(self.at)
        self.old={'blocked_until':self.at+86400,'recorded_at':self.at-3600}
        a.write_json(self.state/'quotas/second.json',self.old)
    def tearDown(self):self.root_patch.stop();self.tmp.cleanup()
    def recover(self,**kwargs):
        return a.record_quota_recovery(self.state,kwargs.get('name','second'),kwargs.get('windows',self.windows),kwargs.get('at',self.at),kwargs.get('start',self.start))
    def quota(self):return a.read_quota_state(self.state)['second']

    def test_complete_reset_replaces_future_weekly_block(self):
        self.assertTrue(self.recover());q=self.quota()
        self.assertEqual(q['source'],'official_usage_recovery')
        self.assertEqual(q['blocked_until'],self.start)
        self.assertEqual(q['previous_blocked_until'],self.old['blocked_until'])

    def test_old_running_controller_reads_elapsed_positive_timestamp(self):
        self.recover();q=self.quota()
        # Exact condition from the already-running 2026-10-08.3 controller.
        attempted={'second','account09'}
        attempted={n for n in attempted if not (0<({'second':q}.get(n,{}).get('blocked_until',0))<=time.time())}
        self.assertNotIn('second',attempted)

    def test_old_aggregate_limit_does_not_resurrect_recovered_block(self):
        a.write_json(self.state/'quota.json',{'second':self.old})
        self.recover();self.assertLess(self.quota()['blocked_until'],time.time())
        recovery=a.read_json(self.state/'quotas/second.json')
        self.assertEqual(a.merge_quota_record(recovery,self.old)['source'],'official_usage_recovery')

    def test_new_failure_wins_over_recovery_and_removes_priority(self):
        self.recover();a.record_quota(self.state,'second',time.time()+1000)
        self.assertGreater(self.quota()['blocked_until'],time.time())
        self.assertNotEqual(self.quota().get('source'),'official_usage_recovery')

    def test_concurrent_error_after_query_start_prevents_clear(self):
        a.write_json(self.state/'quotas/second.json',{**self.old,'recorded_at':self.at-.1})
        self.assertFalse(self.recover());self.assertGreater(self.quota()['blocked_until'],self.at)

    def test_later_legacy_failure_wins_over_recovery(self):
        self.recover()
        a.write_json(self.state/'quota.json',{'second':{'blocked_until':self.at+1000,'recorded_at':self.at+.2}})
        self.assertEqual(self.quota()['blocked_until'],self.at+1000)

    def test_disabled_account_or_sentinel_never_cleared(self):
        self.assertFalse(self.recover(name='main'))
        a.write_json(self.state/'quotas/second.json',{**self.old,'blocked_until':a.DISABLED_UNTIL})
        self.assertFalse(self.recover())
        self.assertEqual(self.quota()['blocked_until'],a.DISABLED_UNTIL)

    def test_partial_stale_unknown_or_exhausted_data_cannot_clear(self):
        cases=[]
        missing=copy.deepcopy(self.windows);missing.pop('five_hour');cases.append(missing)
        for update in ({'used_percentage':100},{'used_percentage':float('nan')},{'used_percentage':True},
                       {'source':'session_statusline'},{'observed_at':self.start-1},{'resets_at':self.at-1},
                       {'used_percentage':20,'resets_at':None}):
            w=copy.deepcopy(self.windows);w['five_hour'].update(update);cases.append(w)
        for w in cases:
            with self.subTest(window=w):self.assertFalse(self.recover(windows=w))
        self.assertFalse(self.recover(at=self.at-400,start=self.at-401))
        self.assertFalse(self.recover(at=self.at+400,start=self.start))
        self.assertEqual(self.quota(),self.old)

    def test_recovered_second_is_first_after_nine_and_six(self):
        self.recover();q=a.read_quota_state(self.state)
        names=['main','second','account06','account07','account09']
        for current in ('account09','account06'):
            candidates=a.selection_candidates(self.registry,names,current,{current},q,after=True)
            self.assertEqual(candidates[0],'second');self.assertNotIn('main',candidates)

    def test_account_running_a_delegated_job_comes_last(self):
        names=['main','second','account06','account07','account09']
        job={'status':'running','account':'account07','native_pid':os.getpid(),'native_start':a.process_identity(os.getpid())}
        a.write_json(self.state/'delegation/jobs'/('d'*32)/'job.json',job)
        c=a.selection_candidates(self.registry,names,'account06',{'account06'},a.read_quota_state(self.state),after=True)
        self.assertEqual(c,['account09','account07'])
        job['native_start']='0';a.write_json(self.state/'delegation/jobs'/('d'*32)/'job.json',job)
        c=a.selection_candidates(self.registry,names,'account06',{'account06'},a.read_quota_state(self.state),after=True)
        self.assertEqual(c,['account07','account09'])

    def test_recovery_priority_expires_but_does_not_reblock(self):
        self.recover();q=a.read_quota_state(self.state);q['second']['available_at']-=301
        names=['main','second','account06','account07','account09']
        c=a.selection_candidates(self.registry,names,'account06',{'account06'},q,after=True)
        self.assertEqual(c[0],'account07');self.assertIn('second',c)

    def test_monitor_query_pushes_recovery_to_live_launcher_cache(self):
        class Adapter:
            record_quota_recovery=staticmethod(a.record_quota_recovery)
            read_quota_state=staticmethod(a.read_quota_state)
        mon=m.Monitor(self.root,Adapter(),self.root)
        payload={'five_hour':{'utilization':0,'resets_at':None},'seven_day':{'utilization':0,'resets_at':'2099-01-01T00:00:00+00:00'}}
        mon.record_official_usage('second',{'email':'second@example.test','account_id':'a','organization_id':'b'},payload,self.at,self.start)
        self.assertTrue(mon.records['second']['launcher_synced'])
        self.assertLess(self.quota()['blocked_until'],time.time())

    def test_monitor_partial_query_does_not_merge_old_window_into_clearance(self):
        class Adapter:
            record_quota_recovery=staticmethod(a.record_quota_recovery)
            read_quota_state=staticmethod(a.read_quota_state)
        mon=m.Monitor(self.root,Adapter(),self.root)
        mon.records['second']={'email':'second@example.test','windows':self.windows}
        mon.record_official_usage('second',{'email':'second@example.test'},
            {'seven_day':{'utilization':0,'resets_at':'2099-01-01T00:00:00+00:00'}},self.at,self.start)
        self.assertFalse(mon.records['second']['launcher_synced'])
        self.assertEqual(self.quota(),self.old)

    def test_cached_monitor_sample_can_be_reconciled_by_new_launcher(self):
        a.write_json(self.state/'monitor/accounts.json',{'second':{'loggedIn':True,'windows':self.windows,'checked_at':self.at,'usage_query_started_at':self.start}})
        a.sync_monitored_recoveries(self.state)
        self.assertLess(self.quota()['blocked_until'],time.time())

    def test_pty_nine_exhausts_then_recovered_second_resumes_same_session(self):
        # An actual PTY runs the full controller/checkpoint/resume path. Only the
        # upstream Claude process is a fixture; no user session or card is used.
        injection='''
if name == 'account09':
    root=pathlib.Path(os.environ['CLAUDE_ACCOUNTS_HOME'])
    state=root/'state'; (state/'quotas').mkdir(parents=True,exist_ok=True)
    now=time.time()
    (state/'quotas/second.json').write_text(json.dumps({'blocked_until':now+86400,'recorded_at':now-3600}))
    (state/'monitor').mkdir(exist_ok=True)
    windows={k:{'used_percentage':0,'resets_at':None,'source':'official_oauth_api','observed_at':now} for k in ('five_hour','seven_day')}
    (state/'monitor/accounts.json').write_text(json.dumps({'second':{'loggedIn':True,'checked_at':now,'usage_query_started_at':now-.1,'windows':windows}}))
'''
        fake=base.FAKE.replace("if scenario == 'safety':",injection+"\nif scenario == 'safety':")
        # second is deliberately later than an unverified available account07.
        with patch.object(base,'FAKE',fake):
            runner=base.AccountsTests()
            code,trace,output=runner.run_fake('switch',names=('main','account07','account09','second'),initial='account09',disabled_profiles=('main',))
        self.assertEqual(code,0,output)
        self.assertEqual([x['account'] for x in trace],['account09','second'])
        self.assertEqual(trace[0]['session_id'],trace[1]['session_id'])
        self.assertIn('RESUME_OK',output)

if __name__=='__main__':unittest.main(verbosity=2)
