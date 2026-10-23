import copy
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import _support
import monitor as m
import reset_cards as r

IDENTITY={'account_id':'11111111-1111-1111-1111-111111111111',
          'organization_id':'22222222-2222-2222-2222-222222222222','email':'tester@example.test'}
GID='opus55-launch-promax-20260921'
def iso(t):return datetime.fromtimestamp(t,timezone.utc).isoformat()
def usage():
    return {'five_hour':{'utilization':100,'resets_at':iso(time.time()+3600)},
            'seven_day':{'utilization':69,'resets_at':iso(time.time()+86400)},
            'cedar_ember':{'eligible':True,'ineligible_reason':None,'at_limit':True,
              'next_grant_id':GID,'weekly_resets_at':iso(time.time()+86400),'cooldown_until':None,
              'grants':[{'id':GID,'label':'Official reset test card','resets_total':1,'resets_left':1,
                'starts_at':iso(time.time()-86400),'ends_at':iso(time.time()+86400),
                'clears':['five_hour','seven_day','seven_day_overage_included'],
                'paused':False,'usable_now':True,'use_requires_limit':False,'blocking':[]}]}}

class Adapter:
    def read_quota_state(self,path):return m.load_json(path/'quota.json',{})

class FakeClient:
    def __init__(self):
        self.identity=copy.deepcopy(IDENTITY);self.payload=usage();self.read_error=None
        self.response={'result':'reset','resets_left':0,'cleared':['five_hour','seven_day']}
        self.calls=[];self.reads=0;self.enter=None;self.release=None
    def read(self):
        self.reads+=1
        if self.read_error:raise self.read_error
        return copy.deepcopy(self.identity),copy.deepcopy(self.payload)
    def redeem(self,identity,gid,request_id):
        self.calls.append((copy.deepcopy(identity),gid,request_id))
        if self.enter:self.enter.set();self.release.wait(3)
        if isinstance(self.response,Exception):raise self.response
        if self.response.get('result')=='reset':
            self.payload['cedar_ember']['grants'][0].update(resets_left=0,usable_now=False)
            self.payload['five_hour']['utilization']=0;self.payload['seven_day']['utilization']=0
        return copy.deepcopy(self.response)

class CardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.registry={'version':1,'claude':'/bin/true','profiles':{'account06':str(self.root/'account06'),'main':str(self.root/'main')},'disabled':{'main':{'reason':'封禁'}}}
        m.save_json(self.root/'accounts.json',self.registry)
        self.mon=m.Monitor(self.root,Adapter(),self.root)
        self.fake=FakeClient();self.mon.reset_cards=r.ResetCards(self.mon,lambda *_:self.fake)
        self.cards=self.mon.reset_cards
    def tearDown(self):self.tmp.cleanup()
    def prepare(self):return self.cards.prepare('account06',GID)['confirmation_id']
    def grant(self):return self.fake.payload['cedar_ember']['grants'][0]
    def assertError(self,code,fn,*args):
        with self.assertRaises(r.CardError) as ctx:fn(*args)
        self.assertEqual(ctx.exception.code,code)
        return ctx.exception

    def test_real_schema_and_quota_update_without_redemption(self):
        card=self.cards.query('account06')
        self.assertEqual((card['resets_left'],card['usable_count']),(1,1))
        self.assertNotIn('identity',card);self.assertEqual(self.fake.calls,[])
        a=next(a for a in self.mon.snapshot()['accounts'] if a['profile']=='account06')
        self.assertEqual(a['windows']['five_hour']['source'],'official_oauth_api')
        self.assertEqual(a['state'],'limited')

    def test_query_records_plan_and_billing_period(self):
        self.fake.plan={'type':'claude_pro','billing':'apple_subscription','status':None,'started_at':time.time()-86400}
        self.fake.payload['cedar_ember']['event_props']={'billing_period':'monthly','billing_path':'apple'}
        self.cards.query('account06')
        a=next(a for a in self.mon.snapshot()['accounts'] if a['profile']=='account06')
        self.assertEqual((a['plan']['type'],a['plan']['period'],a['plan']['renewals']),('claude_pro','monthly',0))
        self.assertGreater(a['plan']['ends_at'],time.time()+27*86400)

    def test_disabled_never_loads_credentials_or_calls_client(self):
        with patch.object(self.cards,'client_factory',side_effect=AssertionError('disabled API')):
            for method,args in [(self.cards.query,('main',)),(self.cards.prepare,('main',GID))]:
                self.assertError('disabled',method,*args)
        self.assertEqual(self.fake.reads,0)

    def test_query_debounces_and_snapshot_never_queries_upstream(self):
        self.cards.query('account06');self.cards.query('account06')
        for _ in range(3):self.mon.snapshot()
        self.assertEqual(self.fake.reads,1)

    def test_missing_inventory_is_unknown_not_zero(self):
        self.fake.payload['cedar_ember']=None
        card=self.cards.query('account06')
        self.assertEqual(card['status'],'unknown');self.assertIsNone(card['resets_left'])
        self.assertTrue(card['error']);self.assertError('unconfirmed',self.prepare)

    def test_ineligible_tenure_is_not_login_failure(self):
        self.fake.payload['cedar_ember'].update(eligible=False,ineligible_reason='tenure',grants=[])
        card=self.cards.query('account06')
        self.assertEqual(card['status'],'ineligible');self.assertIn('tenure',card['reason_text'])
        self.assertError('unusable',self.prepare)

    def test_known_empty_inventory_is_zero(self):
        self.fake.payload['cedar_ember']['grants']=[]
        card=self.cards.query('account06')
        self.assertEqual((card['status'],card['resets_left']),('none',0))

    def test_all_official_blocking_conditions_prevent_use(self):
        for updates in ({'paused':True},{'usable_now':False},{'ends_at':iso(time.time()-1)},
                        {'starts_at':iso(time.time()+2000)},{'resets_left':0},
                        {'blocking':['cooldown']},{'use_requires_limit':True}):
            with self.subTest(updates=updates):
                self.fake.payload=usage();self.fake.payload['cedar_ember']['at_limit']=False
                self.grant().update(updates)
                self.assertError('unusable',self.prepare)
        self.assertEqual(self.fake.calls,[])

    def test_next_grant_and_cooldown_prevent_use(self):
        self.fake.payload['cedar_ember']['next_grant_id']='other-grant'
        self.assertError('unusable',self.prepare)
        self.fake.payload['cedar_ember'].update(next_grant_id=GID,cooldown_until=iso(time.time()+50))
        self.assertError('unusable',self.prepare)

    def test_malformed_counts_scopes_dates_are_unknown(self):
        for update in ({'resets_left':True},{'resets_left':-1},{'resets_left':2},
                       {'clears':['new_unsafe_scope']},{'ends_at':'tomorrow'}, {'usable_now':'true'}):
            with self.subTest(update=update):
                p=usage();p['cedar_ember']['grants'][0].update(update)
                c=r.normalize(IDENTITY,p,time.time());self.assertEqual(c['status'],'unknown')
                self.assertIsNone(c['resets_left'])

    def test_duplicate_grant_unknown(self):
        self.fake.payload['cedar_ember']['grants']*=2
        self.assertTrue(self.cards.query('account06')['error'])

    def test_last_good_read_preserved_but_not_usable_on_401(self):
        self.cards.query('account06');at=self.cards.inventory['account06']['checked_at']
        self.fake.read_error=r.CardError('http_401','登录已失效')
        self.assertError('http_401',self.cards.read,'account06')
        c=self.cards.view('account06');self.assertEqual(c['resets_left'],1)
        self.assertEqual(c['checked_at'],at);self.assertEqual(c['usable_count'],0)
        self.assertTrue(c['stale'])

    def test_stale_and_elapsed_expiry_disable_use(self):
        self.cards.query('account06');self.cards.inventory['account06']['checked_at']-=301
        self.assertEqual(self.cards.view('account06')['usable_count'],0)
        self.cards.inventory['account06']['checked_at']=time.time()
        self.cards.inventory['account06']['grants'][0]['ends_at']=time.time()-1
        card=self.cards.view('account06');self.assertEqual(card['status'],'expired')
        self.assertEqual(card['resets_left'],0)

    def test_confirmation_required_and_manual_record_cannot_redeem(self):
        self.assertError('confirmation',self.cards.use,'anything',True)
        token=self.prepare();self.assertError('confirmation',self.cards.use,token,'true')
        self.assertEqual(self.fake.calls,[])

    def test_expired_confirmation_prevents_use(self):
        token=self.prepare();self.cards.operations[token]['expires_at']=time.time()-1
        self.assertError('expired_confirmation',self.cards.use,token,True)
        self.assertEqual(self.fake.calls,[])

    def test_identity_switch_between_confirmation_and_use(self):
        token=self.prepare();self.fake.identity['organization_id']='33333333-3333-3333-3333-333333333333'
        self.assertError('identity_changed',self.cards.use,token,True)
        self.assertEqual(self.fake.calls,[])

    def test_disabled_after_prepare_cannot_use(self):
        token=self.prepare();self.registry['disabled']['account06']={}
        m.save_json(self.root/'accounts.json',self.registry)
        self.assertError('disabled',self.cards.use,token,True);self.assertEqual(self.fake.calls,[])

    def test_changed_card_scope_requires_new_confirmation(self):
        token=self.prepare();self.grant()['clears']=['five_hour']
        self.assertError('card_changed',self.cards.use,token,True);self.assertEqual(self.fake.calls,[])

    def test_success_refreshes_real_fields_and_is_durable_idempotent(self):
        token=self.prepare();answer=self.cards.use(token,True)
        self.assertEqual(answer['result'],'reset');self.assertEqual(answer['card']['resets_left'],0)
        self.assertEqual(self.mon.records['account06']['windows']['five_hour']['used_percentage'],0)
        self.assertEqual(self.cards.use(token,True)['result'],'reset')
        resumed=r.ResetCards(self.mon,lambda *_:self.fake)
        self.assertEqual(resumed.use(token,True)['result'],'reset');self.assertEqual(len(self.fake.calls),1)
        for p in (self.cards.inventory_path,self.cards.journal_path):self.assertEqual(p.stat().st_mode&0o077,0)

    def test_concurrent_double_click_sends_one_request(self):
        token=self.prepare();self.fake.enter=threading.Event();self.fake.release=threading.Event()
        results=[];thread=threading.Thread(target=lambda:results.append(self.cards.use(token,True)))
        thread.start();self.assertTrue(self.fake.enter.wait(2))
        self.assertEqual(self.cards.use(token,True)['result'],'unknown')
        self.fake.release.set();thread.join(3)
        self.assertEqual(results[0]['result'],'reset');self.assertEqual(len(self.fake.calls),1)

    def test_timeout_blocks_new_attempts_after_restart(self):
        token=self.prepare();self.fake.response=TimeoutError('do not leak this raw error')
        out=self.cards.use(token,True)
        self.assertEqual(out['result'],'unknown');self.assertTrue(out['card']['pending'])
        self.assertNotIn('raw error',json.dumps(out))
        self.assertEqual(self.cards.use(token,True)['result'],'unknown')
        resumed=r.ResetCards(self.mon,lambda *_:self.fake)
        self.assertError('unconfirmed',resumed.prepare,'account06',GID)
        self.assertEqual(resumed.use(token,True)['result'],'unknown');self.assertEqual(len(self.fake.calls),1)

    def test_alias_profile_cannot_bypass_pending_identity(self):
        token=self.prepare();self.fake.response=TimeoutError();self.cards.use(token,True)
        self.registry['profiles']['alias']=str(self.root/'alias');m.save_json(self.root/'accounts.json',self.registry)
        self.assertError('unconfirmed',self.cards.prepare,'alias',GID)
        self.assertEqual(len(self.fake.calls),1)

    def test_later_inventory_can_reconcile_uncertain_consumption(self):
        token=self.prepare();self.fake.response=TimeoutError();self.cards.use(token,True)
        self.grant().update(resets_left=0,usable_now=False)
        self.cards.read('account06')
        card=self.cards.view('account06')
        self.assertFalse(card['pending']);self.assertEqual(card['resets_left'],0)
        self.assertEqual(self.cards.use(token,True)['result'],'observed_change')
        self.assertEqual(len(self.fake.calls),1)

    def test_other_success_invalidates_old_confirmation_even_if_upstream_lags(self):
        token1=self.prepare();token2=self.prepare()
        old=copy.deepcopy(self.fake.payload)
        self.cards.use(token1,True);self.fake.payload=old
        self.assertError('card_changed',self.cards.use,token2,True)
        self.assertEqual(len(self.fake.calls),1)

    def test_interrupted_process_after_journal_before_response_never_retries(self):
        token=self.prepare();self.cards.operations[token]['phase']='submitting'
        self.cards.save(self.cards.journal_path,self.cards.operations)
        resumed=r.ResetCards(self.mon,lambda *_:self.fake)
        self.assertEqual(resumed.use(token,True)['result'],'unknown')
        self.assertError('unconfirmed',resumed.prepare,'account06',GID)
        self.assertEqual(self.fake.calls,[])

    def test_failed_journal_write_stops_before_post(self):
        token=self.prepare()
        save=self.cards.save
        def fail(path,value):
            if path==self.cards.journal_path:raise OSError('disk full')
            return save(path,value)
        with patch.object(self.cards,'save',side_effect=fail):
            with self.assertRaises(OSError):self.cards.use(token,True)
        self.assertEqual(self.fake.calls,[])

    def test_corrupt_journal_fails_closed(self):
        self.cards.journal_path.write_text('{broken')
        resumed=r.ResetCards(self.mon,lambda *_:self.fake)
        self.assertError('unconfirmed',resumed.prepare,'account06',GID)
        self.assertEqual(self.fake.calls,[])

    def test_negative_result_does_not_claim_success(self):
        for result in ('already_used','not_limited','cooldown','ineligible','unavailable'):
            with self.subTest(result=result):
                self.fake.response={'result':result,'resets_left':1}
                out=self.cards.use(self.prepare(),True)
                self.assertEqual(out['result'],result);self.assertNotIn('已确认额度重置',out['message'])

    def test_unrecognized_result_stays_uncertain(self):
        self.fake.response={'result':'future_result'}
        self.assertEqual(self.cards.use(self.prepare(),True)['result'],'unknown')

    def test_reset_unconfirmed_reason_stays_uncertain(self):
        self.fake.response={'result':'unavailable','reason':'reset_unconfirmed'}
        self.assertEqual(self.cards.use(self.prepare(),True)['result'],'unknown')

    def test_success_then_refresh_failure_keeps_success_and_disables_stale_cards(self):
        redeem=self.fake.redeem
        def effect(*args):
            out=redeem(*args);self.fake.read_error=r.CardError('http_401','登录失效');return out
        self.fake.redeem=effect
        out=self.cards.use(self.prepare(),True)
        self.assertEqual(out['result'],'reset');self.assertTrue(out['refresh_error'])
        self.assertEqual(out['card']['usable_count'],0)

    def test_http_confirmation_origin_host_and_disabled_controls(self):
        server=m.HTTPServer(('127.0.0.1',0),m.Handler);server.monitor=self.mon
        host=f'127.0.0.1:{server.server_port}';server.allowed_hosts={host}
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def post(route,payload,origin=None,host_header=None):
            conn=http.client.HTTPConnection(host,timeout=5)
            conn.request('POST',route,json.dumps(payload),{'Content-Type':'application/json','Origin':origin or 'http://'+host,'Host':host_header or host})
            response=conn.getresponse();body=response.read();conn.close()
            return response.status,json.loads(body)
        try:
            self.assertEqual(post('/api/reset-cards/use',{'confirmed':True})[0],400)
            self.assertEqual(post('/api/reset-cards/query',{'profile':'main'})[0],409)
            self.assertEqual(post('/api/reset-cards/prepare',{'profile':'account06','grant_id':GID},origin='http://evil.test')[0],403)
            self.assertEqual(post('/api/reset-cards/use',{},host_header='evil.test')[0],403)
            code,prepared=post('/api/reset-cards/prepare',{'profile':'account06','grant_id':GID})
            self.assertEqual(code,200);self.assertEqual(self.fake.calls,[])
            payload={'confirmation_id':prepared['confirmation_id'],'confirmed':True}
            self.assertEqual(post('/api/reset-cards/use',payload)[1]['result'],'reset')
            self.assertEqual(post('/api/reset-cards/use',payload)[1]['result'],'reset')
            self.assertEqual(len(self.fake.calls),1)
        finally:server.shutdown();server.server_close();thread.join()

class TransportTests(unittest.TestCase):
    def test_credentials_and_errors_never_exposed_and_correct_headers(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);secret='secret-test-access-token'
            (root/'.credentials.json').write_text(json.dumps({'claudeAiOauth':{'accessToken':secret,'scopes':['user:profile']}}))
            registry={'profiles':{'account06':td},'claude':'/bin/true'}
            with patch.object(r,'client_version',return_value='2.1.295'):
                client=r.OfficialClient(registry,'account06')
            requests=[]
            class Response:
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def read(self,n):return b'{}'
            class Opener:
                def open(self,req,timeout):requests.append(req);return Response()
            with patch.object(r.urllib.request,'build_opener',return_value=Opener()):
                client.request('/api/oauth/usage?cedar_ember=1&skip_spend=1')
                client.redeem(IDENTITY,GID,'a'*32)
            self.assertEqual(requests[0].get_header('Authorization'),'Bearer '+secret)
            self.assertEqual(requests[0].get_header('User-agent'),'claude-cli/2.1.295 (external, cli)')
            self.assertEqual(requests[1].method,'POST')
            self.assertEqual(json.loads(requests[1].data),{'program':'cedar_ember','grant_id':GID,'request_id':'a'*32})
            self.assertNotIn(secret,requests[1].full_url);self.assertNotIn(secret,requests[1].data.decode())
            with patch.object(r.urllib.request,'build_opener',side_effect=OSError(secret)):
                with self.assertRaises(r.CardError) as e:client.request('/api/oauth/profile')
            self.assertNotIn(secret,str(e.exception))

    def test_profile_plan_is_kept_apart_from_identity(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td)/'.credentials.json').write_text(json.dumps({'claudeAiOauth':{'accessToken':'secret','scopes':['user:profile']}}))
            with patch.object(r,'client_version',return_value='2.1.295'):
                client=r.OfficialClient({'profiles':{'account06':td},'claude':'/bin/true'},'account06')
            profile={'account':{'uuid':IDENTITY['account_id'],'email':IDENTITY['email']},
                     'organization':{'uuid':IDENTITY['organization_id'],'organization_type':'claude_pro',
                                     'billing_type':'apple_subscription','subscription_status':'<b>active</b>',
                                     'subscription_created_at':'2026-09-29T04:13:34.325075Z'}}
            with patch.object(client,'request',side_effect=lambda path,*_:profile if path=='/api/oauth/profile' else usage()):
                identity,payload=client.read()
        self.assertEqual(identity,IDENTITY)
        self.assertEqual(client.plan,{'type':'claude_pro','billing':'apple_subscription','status':None,
                                      'started_at':datetime(2026,9,29,4,13,34,325075,tzinfo=timezone.utc).timestamp()})

    def test_no_redirect(self):
        self.assertIsNone(r.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.test'))

    def test_no_scope_does_not_send_request(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td)/'.credentials.json').write_text(json.dumps({'claudeAiOauth':{'accessToken':'secret','scopes':[]}}))
            with patch.object(r.urllib.request,'build_opener',side_effect=AssertionError('network')):
                with self.assertRaises(r.CardError):r.OfficialClient({'profiles':{'a':td},'claude':'/bin/true'},'a')

if __name__=='__main__':unittest.main(verbosity=2)
