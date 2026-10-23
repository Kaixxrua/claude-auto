import contextlib
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import _support
import monitor as m

UTC=timezone.utc
SAMPLE='''Current session
0% 0% used
Resets 9:59pm (UTC)
Current week (all models)
14% 14% used
Resets Oct 13, 4:59pm (UTC)
What's contributing to your limits usage?
70% of your usage came from sessions active for 8+ hours
'''

class Adapter:
    def auth_status(self,registry,name):
        return {'loggedIn':True,'authMethod':'claude.ai','email':name+'@example.test','subscriptionType':'pro'}
    def eligible(self,status): return status.get('loggedIn') is True
    def inherit_preferences(self,*_): pass
    def profile_env(self,*_): return {}
    def read_quota_state(self,state_dir): return m.load_json(state_dir/'quota.json',{})

class MonitorTests(unittest.TestCase):
    def setUp(self):
        patch=mock.patch.object(m,'PROBE_RETRY_SECONDS',0)
        patch.start();self.addCleanup(patch.stop)
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        m.save_json(self.root/'accounts.json',{'version':1,'claude':'/bin/true','profiles':{'main':str(self.root/'main')}})
        self.mon=m.Monitor(self.root,Adapter(),self.root)
        self.mon.records={'main':{'loggedIn':True,'email':'main@example.test','checked_at':time.time(),
                                  'windows':{'five_hour':{'used_percentage':80,'resets_at':time.time()+3600,'observed_at':time.time(),'source':'official_cli_usage'},
                                             'seven_day':{'used_percentage':11,'resets_at':time.time()+86400,'observed_at':time.time(),'source':'official_cli_usage'}}}}

    def tearDown(self):
        self.mon.stop.set();self.mon.wake.set()
        if self.mon.worker:self.mon.worker.join(timeout=3)
        self.temp.cleanup()

    def test_parse_real_native_output_without_treating_analytics_as_quota(self):
        now=datetime(2026,10,7,17,tzinfo=UTC).timestamp()
        result=m.parse_usage('\x1b[32m'+SAMPLE+'\x1b[0m',now)
        self.assertEqual(result['five_hour']['used_percentage'],0)
        self.assertEqual(result['seven_day']['used_percentage'],14)
        self.assertEqual(result['seven_day']['resets_at'],datetime(2026,10,13,16,59,tzinfo=UTC).timestamp())

    def test_disabled_account_is_displayed_and_never_polled(self):
        registry=m.load_json(self.root/'accounts.json')
        registry['disabled']={'main':{'reason':'用户确认封禁'}}
        m.save_json(self.root/'accounts.json',registry)
        with mock.patch.object(self.mon.adapter,'auth_status',side_effect=AssertionError('must not probe disabled account')):
            self.mon.poll(registry,'main')
            self.mon.refresh()
            self.assertNotIn('main',self.mon.requested)
            with self.assertRaises(ValueError):self.mon.refresh('main')
            a=self.mon.snapshot()['accounts'][0]
        self.assertEqual(a['state'],'disabled')
        self.assertEqual(a['disabled_reason'],'用户确认封禁')
        self.assertTrue(self.mon.snapshot()['initial_sync_complete'])

    def test_midnight_year_boundary_and_unknown_format(self):
        now=datetime(2026,12,31,23,tzinfo=UTC).timestamp()
        self.assertEqual(m.parse_reset('Resets 1:30am (UTC)',now,'five_hour'),datetime(2027,1,1,1,30,tzinfo=UTC).timestamp())
        self.assertEqual(m.parse_reset('Resets Jan 2, 4pm (UTC)',now,'seven_day'),datetime(2027,1,2,16,tzinfo=UTC).timestamp())
        self.assertIsNone(m.parse_reset('Resets whenever',now,'five_hour'))
        self.assertIsNone(m.parse_reset('Resets 9pm (Mars)',now,'five_hour'))

    def test_invalid_percent_does_not_look_like_full_allowance(self):
        self.assertEqual(m.parse_usage('Current session\n150% used\n'),{})
        self.assertEqual(m.parse_usage('Current session\nloading…\n'),{})

    def test_no_periodic_quota_poll_for_idle_accounts(self):
        with mock.patch.object(m,'probe_usage',return_value=self.mon.records['main']['windows']) as probe:
            self.mon.start()
            deadline=time.time()+2
            while not self.mon.initialized and time.time()<deadline:time.sleep(.02)
            time.sleep(2.2)
            self.assertEqual(probe.call_count,1)
            for _ in range(4):self.mon.snapshot()
            self.assertEqual(probe.call_count,1)

    def test_new_profile_is_queried_once(self):
        with mock.patch.object(m,'probe_usage',return_value=self.mon.records['main']['windows']) as probe:
            self.mon.start()
            deadline=time.time()+2
            while probe.call_count<1 and time.time()<deadline:time.sleep(.02)
            m.save_json(self.root/'accounts.json',{'version':1,'claude':'/bin/true','profiles':{'main':str(self.root/'main'),'second':str(self.root/'second')}})
            self.mon.wake.set()
            deadline=time.time()+3
            while probe.call_count<2 and time.time()<deadline:time.sleep(.02)
            self.assertEqual(probe.call_count,2)

    def test_live_partial_status_keeps_last_known_window(self):
        m.save_json(self.root/'state/runs/example/main-limits.json',{'account':'main','at':time.time()+1,'limits':{'seven_day':{'used_percentage':13,'resets_at':time.time()+86400}}})
        with mock.patch.object(self.mon,'active_accounts',return_value={'main':1}):
            a=self.mon.snapshot()['accounts'][0]
        self.assertEqual(a['windows']['five_hour']['used_percentage'],80)
        self.assertEqual(a['windows']['seven_day']['used_percentage'],13)
        self.assertTrue(a['live'])

    def test_partial_usage_refresh_keeps_five_hour_and_reports_missing_window(self):
        old=self.mon.records['main']['windows']['five_hour'].copy()
        self.mon.records['main']['windows']['five_hour']['used_percentage']=100
        weekly={'seven_day':{'used_percentage':78,'resets_at':time.time()+86400,
                            'observed_at':time.time(),'source':'official_cli_usage'}}
        with mock.patch.object(m,'probe_usage',return_value=weekly):
            self.mon.poll(self.mon.registry,'main')
        a=self.mon.snapshot()['accounts'][0]
        self.assertEqual(a['windows']['five_hour']['used_percentage'],100)
        self.assertEqual(a['windows']['five_hour']['observed_at'],old['observed_at'])
        self.assertEqual(a['windows']['seven_day']['used_percentage'],78)
        self.assertEqual(a['missing_windows'],['five_hour'])
        self.assertIn('5 小时',a['error'])
        self.assertEqual(a['state'],'limited')

    def test_partial_refresh_cannot_hide_older_statusline_for_missing_window(self):
        self.mon.records['main']['windows'].pop('five_hour')
        self.mon.records['main']['checked_at']=time.time()+5
        m.save_json(self.root/'state/runs/example/main-limits.json',{'account':'main','at':time.time(),
                    'limits':{'five_hour':{'used_percentage':100,'resets_at':time.time()+3600}}})
        a=self.mon.snapshot()['accounts'][0]
        self.assertEqual(a['windows']['five_hour']['used_percentage'],100)
        self.assertEqual(a['state'],'limited')

    def test_identity_change_drops_old_windows_and_old_statusline(self):
        self.mon.records['main']['email']='previous@example.test'
        m.save_json(self.root/'state/runs/example/main-limits.json',{'account':'main','at':time.time()-10,
                    'limits':{'five_hour':{'used_percentage':100,'resets_at':time.time()+3600}}})
        weekly={'seven_day':{'used_percentage':5,'resets_at':time.time()+86400,'observed_at':time.time()}}
        with mock.patch.object(m,'probe_usage',return_value=weekly):
            self.mon.poll(self.mon.registry,'main')
        a=self.mon.snapshot()['accounts'][0]
        self.assertNotIn('five_hour',a['windows'])
        self.assertEqual(a['state'],'unknown')

    def test_official_limit_receipt_is_visible_even_when_usage_window_missing(self):
        self.mon.records['main']['windows'].pop('five_hour')
        until=time.time()+3600
        m.save_json(self.root/'state/quota.json',{'main':{'blocked_until':until,'recorded_at':time.time()}})
        with mock.patch.object(self.mon,'active_accounts',return_value={'main':1}):
            data=self.mon.snapshot();a=data['accounts'][0]
        self.assertEqual(a['state'],'limited')
        self.assertTrue(a['live'])
        self.assertTrue(a['quota_limit']['active'])
        self.assertNotIn('five_hour',a['windows'])
        self.assertEqual(data['availability']['available'],0)
        self.assertEqual(data['availability']['limited'],1)
        self.assertEqual(data['availability']['next_reset_at'],until)

    def test_expired_receipt_does_not_claim_limit_or_replenishment(self):
        self.mon.records['main']['windows'].pop('five_hour')
        m.save_json(self.root/'state/quota.json',{'main':{'blocked_until':time.time()-1,'recorded_at':time.time()-3600}})
        data=self.mon.snapshot();a=data['accounts'][0]
        self.assertFalse(a['quota_limit']['active'])
        self.assertEqual(a['state'],'unknown')
        self.assertEqual(data['availability']['unconfirmed'],1)

    def test_new_complete_positive_reading_supersedes_old_error(self):
        m.save_json(self.root/'state/quota.json',{'main':{'blocked_until':time.time()+3600,'recorded_at':time.time()-60}})
        a=self.mon.snapshot()['accounts'][0]
        self.assertFalse(a['quota_limit']['active'])
        self.assertEqual(a['state'],'available')

    def test_recovery_estimate_waits_for_both_exhausted_windows(self):
        core=self.mon.records['main']['windows']
        for w in core.values():w['used_percentage']=100
        data=self.mon.snapshot()
        self.assertEqual(data['availability']['next_reset_at'],core['seven_day']['resets_at'])

    def test_restarting_monitor_does_not_repeat_initial_scan(self):
        m.save_json(self.mon.state_dir/'accounts.json',self.mon.records)
        resumed=m.Monitor(self.root,Adapter(),self.root)
        self.assertIn('main',resumed.initialized)

    def test_expired_window_is_unknown_not_replenished(self):
        window=self.mon.records['main']['windows']['five_hour']
        window.update(used_percentage=100,resets_at=time.time()-1)
        a=self.mon.snapshot()['accounts'][0]
        self.assertTrue(a['windows']['five_hour']['expired'])
        self.assertEqual(a['windows']['five_hour']['used_percentage'],100)
        self.assertNotEqual(a['state'],'available')

    def test_reset_card_unknown_expiry_and_account_identity(self):
        self.assertEqual(self.mon.snapshot()['accounts'][0]['reset_card']['status'],'unknown')
        self.mon.set_card({'profile':'main','status':'available','expires_at':time.time()-1})
        self.assertEqual(self.mon.snapshot()['accounts'][0]['reset_card']['status'],'expired')
        self.mon.records['main']['email']='different@example.test'
        self.assertEqual(self.mon.snapshot()['accounts'][0]['reset_card']['status'],'unknown')

    def test_plan_period_ends_at_the_next_anniversary(self):
        t=lambda *a:datetime(*a,tzinfo=UTC).timestamp()
        plan={'type':'claude_pro','billing':'apple_subscription','status':None,'period':'monthly','started_at':t(2026,9,29,4,13,34)}
        view=m.plan_view(plan,t(2026,10,21))
        self.assertEqual((view['ends_at'],view['renewals']),(t(2026,10,29,4,13,34),0))
        self.assertEqual(m.plan_view(plan,t(2026,10,29,4,13,34))['ends_at'],t(2026,11,29,4,13,34))
        # Still paid after two anniversaries: renewed twice, this period ends at the third.
        view=m.plan_view({**plan,'started_at':t(2026,8,18,2,11,58)},t(2026,10,21))
        self.assertEqual((view['ends_at'],view['renewals']),(t(2026,11,18,2,11,58),2))
        # Month ends clamp without drifting off the original day.
        self.assertEqual(m.plan_view({**plan,'started_at':t(2026,1,31,8)},t(2026,2,1))['ends_at'],t(2026,2,28,8))
        self.assertEqual(m.plan_view({**plan,'started_at':t(2026,1,31,8)},t(2026,3,1))['ends_at'],t(2026,3,31,8))
        self.assertEqual(m.plan_view({**plan,'period':'annual','started_at':t(2025,11,5)},t(2026,10,21))['ends_at'],t(2026,11,5))
        self.assertEqual(m.plan_view({**plan,'period':None},t(2026,10,21))['ends_at'],t(2026,10,29,4,13,34))
        for other in ({'type':'claude_free'},{'billing':'stripe_subscription_contracted'},{'started_at':None}):
            view=m.plan_view({**plan,**other},t(2026,10,21))
            self.assertEqual((view['ends_at'],view['renewals']),(None,None))
        self.assertIsNone(m.plan_view(None,t(2026,10,21)))

    def test_official_plan_keeps_known_period_and_follows_identity(self):
        start=time.time()-40*86400
        plan={'type':'claude_pro','billing':'apple_subscription','status':None,'started_at':start}
        identity={'account_id':'a','organization_id':'o','email':'main@example.test'}
        annual={'cedar_ember':{'event_props':{'billing_period':'annual'}}}
        self.mon.record_official_usage('main',identity,annual,time.time(),plan=plan)
        a=self.mon.snapshot()['accounts'][0]
        self.assertEqual((a['plan']['period'],a['plan']['source'],a['plan']['renewals']),('annual','official_oauth_api',0))
        self.assertAlmostEqual(a['plan']['ends_at'],m.add_months(datetime.fromtimestamp(start,UTC),12).timestamp())
        # A response without the period keeps the one this identity reported before.
        self.mon.record_official_usage('main',identity,{},time.time(),plan=plan)
        self.assertEqual(self.mon.snapshot()['accounts'][0]['plan']['period'],'annual')
        self.mon.record_official_usage('main',{**identity,'email':'other@example.test'},{},time.time(),plan=plan)
        self.assertIsNone(self.mon.snapshot()['accounts'][0]['plan']['period'])
        self.mon.record_official_usage('main',identity,{},time.time())
        self.assertIsNone(self.mon.snapshot()['accounts'][0]['plan'])

    def test_disabled_account_plan_comes_from_client_profile(self):
        registry=m.load_json(self.root/'accounts.json')
        registry['disabled']={'main':{'reason':'用户确认封禁'}}
        m.save_json(self.root/'accounts.json',registry)
        start=time.time()-3*86400
        account={'emailAddress':'main@example.test','organizationType':'claude_pro','billingType':'apple_subscription',
                 'subscriptionCreatedAt':datetime.fromtimestamp(start,UTC).isoformat(),'profileFetchedAt':1792216540884}
        m.save_json(self.root/'main/.claude.json',{'projects':{},'oauthAccount':account})
        plan=self.mon.snapshot()['accounts'][0]['plan']
        self.assertEqual((plan['source'],plan['type'],plan['period'],plan['checked_at']),('client_profile','claude_pro',None,1792216540.884))
        self.assertAlmostEqual(plan['ends_at'],m.add_months(datetime.fromtimestamp(start,UTC),1).timestamp())
        self.assertNotIn('email',plan)
        # Another login in that directory is not this account's plan.
        m.save_json(self.root/'main/.claude.json',{'oauthAccount':{**account,'emailAddress':'other@example.test'}})
        self.assertIsNone(self.mon.snapshot()['accounts'][0]['plan'])

    def test_card_validation_and_private_storage(self):
        with self.assertRaises(ValueError):self.mon.set_card({'profile':'main','status':'available','expires_at':float('nan')})
        self.mon.set_card({'profile':'main','status':'none'})
        self.assertEqual((self.mon.state_dir/'reset-cards.json').stat().st_mode&0o077,0)

    def test_http_origin_host_path_and_real_snapshot(self):
        server=m.HTTPServer(('127.0.0.1',0),m.Handler)
        server.monitor=self.mon; host=f'127.0.0.1:{server.server_port}';server.allowed_hosts={host}
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(method,path,body=None,headers=None):
            conn=http.client.HTTPConnection(host,timeout=3)
            conn.request(method,path,body,headers or {})
            response=conn.getresponse();status=response.status;content=response.read();conn.close()
            return status,content
        try:
            code,body=request('GET','/api/status')
            self.assertEqual(code,200);self.assertEqual(json.loads(body)['accounts'][0]['profile'],'main')
            self.assertEqual(request('GET','/../../accounts.json')[0],404)
            self.assertEqual(request('GET','/api/status',headers={'Host':'attacker.example'})[0],403)
            payload=json.dumps({'profile':'main','status':'none'})
            self.assertEqual(request('POST','/api/reset-card',payload,{'Content-Type':'application/json','Origin':'https://attacker.example'})[0],403)
            self.assertEqual(request('POST','/api/reset-card',payload,{'Content-Type':'application/json','Origin':'http://'+host})[0],200)
        finally:
            server.shutdown();server.server_close();thread.join()


class AutomaticQuotaTests(unittest.TestCase):
    def setUp(self):
        patch=mock.patch.object(m,'PROBE_RETRY_SECONDS',0)
        patch.start();self.addCleanup(patch.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        m.save_json(self.root/'accounts.json',{'version':1,'claude':'/bin/true','profiles':{'main':str(self.root/'main'),'spare':str(self.root/'spare')}})
        self.mon=m.Monitor(self.root,Adapter(),self.root)
        now=time.time()
        self.mon.records={n:{'loggedIn':True,'email':n+'@example.test','windows':{
            'five_hour':{'used_percentage':40,'resets_at':now+3600,'observed_at':now-600,'source':'official_cli_usage'},
            'seven_day':{'used_percentage':10,'resets_at':now+86400,'observed_at':now-600,'source':'official_cli_usage'}}} for n in ('main','spare')}

    def weekly(self):
        return {'seven_day':{'used_percentage':5,'resets_at':time.time()+86400,'observed_at':time.time(),'source':'official_cli_usage'}}

    def test_unstarted_session_window_is_inferred_only_from_a_known_reset(self):
        now=time.time()
        with mock.patch.object(m,'probe_usage',side_effect=lambda *a,**k:self.weekly()) as probe:
            past=m.probe_complete('x',{},'.',known={'five_hour':{'used_percentage':100,'resets_at':now-5}})
            self.assertEqual(probe.call_count,2)
            self.assertEqual((past['five_hour']['used_percentage'],past['five_hour']['inferred']),(0.0,'session_not_started'))
            self.assertNotIn('five_hour',m.probe_complete('x',{},'.',known={'five_hour':{'used_percentage':100,'resets_at':now+600}}))
            self.assertNotIn('five_hour',m.probe_complete('x',{},'.',known={}))
            self.assertIn('five_hour',m.probe_complete('x',{},'.',known={'five_hour':{'used_percentage':0,'resets_at':None}}))
        both={**self.weekly(),'five_hour':{'used_percentage':3,'resets_at':now+60,'observed_at':now,'source':'official_cli_usage'}}
        with mock.patch.object(m,'probe_usage',return_value=both) as probe:
            self.assertEqual(m.probe_complete('x',{},'.')['five_hour']['used_percentage'],3)
            self.assertEqual(probe.call_count,1)

    def job(self, **values):
        job_id=values.pop('job_id','a'*32)
        d={'job_id':job_id,'title':'T','status':'running','account':'main','attempt':1,'session_id':'s'*36,
           'created_at':time.time(),'updated_at':time.time(),'worker_pid':os.getpid(),'worker_start':m.process_ticks(os.getpid()),
           'native_pid':os.getpid(),'native_start':m.process_ticks(os.getpid()),**values}
        m.save_json(self.root/'state/delegation/jobs'/job_id/'job.json',d)
        return self.root/'state/delegation/jobs'/job_id

    def test_delegated_session_reading_updates_its_account(self):
        folder=self.job()
        reset=time.time()+1800
        event={'type':'rate_limit_event','rate_limit_info':{'status':'allowed_warning','unifiedWindows':{
            'five_hour':{'utilization':0.93,'resetsAt':reset},'seven_day':{'utilization':0.21,'resetsAt':reset+86400}}}}
        (folder/'events-1.jsonl').write_text('{"type":"system"}\n'+json.dumps(event,separators=(',',':'))+'\n{"type":"assistant","message":{}}\n')
        snap=self.mon.snapshot()
        a=next(x for x in snap['accounts'] if x['profile']=='main')
        self.assertEqual((a['windows']['five_hour']['used_percentage'],a['windows']['five_hour']['source']),(93.0,'delegated_session'))
        self.assertEqual(a['windows']['five_hour']['resets_at'],reset)
        self.assertEqual(a['delegated'],1)
        self.assertEqual(snap['delegations'][0]['limits']['status'],'allowed_warning')

    def test_waiting_and_dead_worker_states(self):
        self.job(job_id='b'*32,status='waiting',account=None,native_pid=None)
        self.job(job_id='c'*32,status='selecting',account=None,native_pid=None,worker_pid=2**22+7,worker_start='1')
        states={j['job'][:1]:j['status'] for j in m.delegation_jobs(self.root,time.time())}
        self.assertEqual(states,{'b':'waiting','c':'interrupted'})

    def test_policy_flags_and_supervisor_heartbeat(self):
        m.save_json(self.root/'state/delegation/policy.json',{'share_when_short':True,'auto_continue':True,'retry_seconds':60})
        p=m.delegation_policy(self.root,time.time())
        self.assertEqual((p['share_when_short'],p['auto_continue'],p['wait_for_account'],p['supervisor']),(True,True,False,False))
        m.save_json(self.root/'state/delegation/supervisor.json',{'pid':os.getpid(),'start':m.process_ticks(os.getpid()),'at':time.time()})
        self.assertTrue(m.delegation_policy(self.root,time.time())['supervisor'])

    def test_busy_accounts_poll_each_minute_and_resets_trigger_a_read(self):
        registry=self.mon.load_registry();now=time.time()
        self.mon.official={'main':{'next_at':now+200,'attempted_at':now-61},'spare':{'next_at':now+100,'attempted_at':now-10}}
        self.assertEqual(self.mon.official_next(registry,{'main'},now),('main',now-1))
        self.assertEqual(self.mon.official_next(registry,set(),now),('spare',now+100))
        # Reset after the last read: read again right away.
        self.mon.records['spare']['windows']['five_hour']['resets_at']=now-8
        name,due=self.mon.official_next(registry,set(),now)
        self.assertEqual(name,'spare');self.assertLessEqual(due,now)

    def test_official_read_success_failure_and_expired_login(self):
        registry=self.mon.load_registry()
        with mock.patch.object(self.mon.reset_cards,'query') as query:
            self.mon.poll_official(registry,'main',True)
        query.assert_called_once_with('main')
        state=self.mon.official['main']
        self.assertIsNone(state['error']);self.assertAlmostEqual(state['next_at']-time.time(),m.OFFICIAL_BUSY_SECONDS,delta=2)
        with mock.patch.object(self.mon.reset_cards,'query',side_effect=m.CardError('http_401','登录已失效',502)):
            self.mon.poll_official(registry,'main',True)
        self.assertEqual(state['failures'],1);self.assertIn('main',self.mon.requested)
        self.assertAlmostEqual(state['next_at']-time.time(),120,delta=2)
        self.mon.requested.clear()
        (self.root/'spare').mkdir()
        m.save_json(self.root/'spare/.credentials.json',{'claudeAiOauth':{'expiresAt':(time.time()-5)*1000}})
        with mock.patch.object(self.mon.reset_cards,'query') as query:
            self.mon.poll_official(registry,'spare',False)
        query.assert_not_called()
        self.assertIn('spare',self.mon.requested)

if __name__=='__main__':unittest.main(verbosity=2)
