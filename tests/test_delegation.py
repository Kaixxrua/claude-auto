"""Independent jobs, real subprocesses and MCP without real accounts or API calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

from _support import REPO_ROOT
import monitor


FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
root = pathlib.Path(os.environ['CLAUDE_ACCOUNTS_HOME'])
profile = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
options = json.loads((profile/'fixture.json').read_text())
with (root/'fixture-calls.jsonl').open('a') as trace:
    trace.write(json.dumps({'account': profile.name, 'args': sys.argv[1:]})+'\n')
if sys.argv[1:] == ['auth', 'status']:
    print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'email': options['email']}))
    sys.exit(0)
if '/usage' in sys.argv:
    if options.get('bad_usage'):
        print('No quota information', flush=True); sys.exit(0)
    week = 'Current week (all models)\n20% used\nResets 11:59pm (UTC)'
    if options.get('no_session'):
        print(week, flush=True); sys.exit(0)
    # A session window always resets within five hours of its start.
    reset = time.gmtime(time.time()+7200)
    label = f"{reset.tm_hour % 12 or 12}:{reset.tm_min:02d}{'am' if reset.tm_hour < 12 else 'pm'}"
    print('Current session\n'+str(options.get('used', 0))+'% used\nResets '+label+' (UTC)\n'+week, flush=True)
    time.sleep(15); sys.exit(0)
assert '-p' in sys.argv and '--strict-mcp-config' in sys.argv
assert sys.argv[sys.argv.index('--permission-mode')+1] == 'dontAsk'
assert '--dangerously-skip-permissions' not in sys.argv
assert 'CLAUDECODE' not in os.environ
assert 'CLAUDE_CODE_MESSAGING_TOKEN' not in os.environ
if options.get('stall_stdin'): time.sleep(30)
prompt = sys.stdin.read()
resuming = '--resume' in sys.argv
sid = sys.argv[sys.argv.index('--resume' if resuming else '--session-id')+1]
history = profile/'projects'/'fixture'/f'{sid}.jsonl'
history.parent.mkdir(parents=True, exist_ok=True)
previous = history.read_text() if history.exists() else ''
if resuming:
    assert previous, 'resume lost history'
with history.open('a') as f: f.write(json.dumps({'type':'user','content':prompt})+'\n')
if 'WRONGMODEL' in prompt:
    print(json.dumps({'type':'assistant','message':{'model':'claude-other-1','content':[]}}), flush=True)
    time.sleep(8)
if 'WAIT' in prompt: time.sleep(8)
if 'CLIMB' in prompt and not resuming:
    print(json.dumps({'type':'rate_limit_event','rate_limit_info':{'status':'allowed','rateLimitType':'five_hour','unifiedWindows':{'five_hour':{'utilization':0.85,'resetsAt':time.time()+3600},'seven_day':{'utilization':0.2,'resetsAt':time.time()+86400}}}}), flush=True)
    time.sleep(20)
if 'FAIL' in prompt: sys.exit(1)
if 'FLAKY' in prompt:
    print(json.dumps({'type':'result','subtype':'success','is_error':True,'api_error_status':529,'result':'API Error: 529','session_id':sid}), flush=True)
    sys.exit(1)
if 'LIMIT' in prompt:
    options['used'] = 100
    (profile/'fixture.json').write_text(json.dumps(options))
    print(json.dumps({'type':'rate_limit_event','rate_limit_info':{'status':'rejected','rateLimitType':'five_hour','unifiedWindows':{'five_hour':{'utilization':1.0,'resetsAt':time.time()+3600},'seven_day':{'utilization':0.2,'resetsAt':time.time()+86400}}}}), flush=True)
    print(json.dumps({'type':'assistant','error':'rate_limit'}), flush=True)
    print(json.dumps({'type':'result','subtype':'error_during_execution','is_error':True,'session_id':sid}), flush=True)
    sys.exit(1)
text = 'resumed with prior context' if resuming else 'delegated result'
with history.open('a') as f: f.write(json.dumps({'type':'assistant','content':text})+'\n')
print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':text,'session_id':sid,'permission_denials':[]}), flush=True)
'''


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base/'accounts'
        self.root.mkdir()
        self.home = self.base/'home'
        self.home.mkdir()
        self.project = self.base/'project with spaces'
        self.project.mkdir()
        self.native = self.base/'native'
        self.native.write_text(FAKE)
        self.native.chmod(0o755)
        self.history = self.root/'shared-projects'
        self.history.mkdir()
        profiles = {}
        for name in ('parent', 'full', 'alpha', 'beta', 'main'):
            p = self.root/name
            p.mkdir()
            (p/'projects').symlink_to(self.history)
            (p/'fixture.json').write_text(json.dumps({'email': name+'@example.test', 'used': 100 if name=='full' else 0}))
            profiles[name] = str(p)
        self.registry = {'version':1, 'claude':str(self.native), 'profiles':profiles,
                         'default':'parent', 'disabled':{'main':{'reason':'operator disabled'}}}
        (self.root/'accounts.json').write_text(json.dumps(self.registry))
        env = {'HOME': str(self.home), 'CLAUDE_ACCOUNTS_HOME': str(self.root),
               'CLAUDE_CONFIG_DIR': profiles['parent'], 'CLAUDECODE': '1',
               'CLAUDE_CODE_MESSAGING_TOKEN': 'parent-session-only'}
        patch = mock.patch.dict(os.environ, env)
        patch.start(); self.addCleanup(patch.stop)
        for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN',
                    'ANTHROPIC_PROFILE', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
            os.environ.pop(key, None)
        self.m = monitor.load_account_module(REPO_ROOT/'bin/claude-delegate')
        self.a, self.probe = self.m.dependencies()
        self.jobs = self.m.Jobs(self.a, self.probe)
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for p in (self.jobs.root/'jobs').glob('*/job.json'):
            d = json.loads(p.read_text())
            for kind in ('native', 'worker'):
                pid = d.get(kind+'_pid')
                if pid and self.m.process_start(pid) == d.get(kind+'_start'):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(pid, signal.SIGTERM)

    def submit(self, prompt='Do a read-only task', **kwargs):
        return self.jobs.submit(prompt=prompt, cwd=str(self.project), **kwargs)

    def until(self, job_id, statuses=None, seconds=15):
        deadline=time.monotonic()+seconds
        statuses = statuses or self.m.TERMINAL
        while time.monotonic() < deadline:
            d = self.jobs.read(job_id)
            if d['status'] in statuses:
                return d
            time.sleep(.05)
        self.fail('job did not reach expected state: '+str(self.jobs.public(d)))

    def calls(self):
        p=self.root/'fixture-calls.jsonl'
        return [json.loads(s) for s in p.read_text().splitlines()] if p.exists() else []

    def policy(self, **values):
        self.a.write_json(self.jobs.root/'policy.json', values)

    def fixture(self, name, **options):
        p=self.root/name/'fixture.json'
        p.write_text(json.dumps({**json.loads(p.read_text()), **options}))

    def runs(self):
        return [c for c in self.calls() if '-p' in c['args']]

    def run_worker_inline(self, prompt='Do a read-only task', **kwargs):
        # In this process, so module attributes can be patched for the worker.
        with mock.patch.object(self.jobs,'launch',side_effect=lambda d:self.jobs.public(d)):
            job=self.submit(prompt,**kwargs)
        d=self.jobs.read(job['job_id']);self.jobs.save(d,worker_pid=os.getpid(),worker_start=self.m.process_start(os.getpid()))
        try:
            self.jobs.worker(job['job_id'])
        finally:
            # Avoid cleanup signaling the test runner.
            d=self.jobs.read(job['job_id']);self.jobs.save(d,worker_pid=None)
        return self.jobs.read(job['job_id'])

    def windows(self, used=0, at=None):
        at=time.time() if at is None else at
        return {key: {'used_percentage':used, 'observed_at':at, 'resets_at':at+3600,
                      'source':'official_cli_usage'} for key in self.m.WINDOWS}

    def test_quota_requires_both_fresh_authenticated_windows(self):
        now=time.time()
        self.assertTrue(self.m.valid_windows(self.windows(), now))
        for w in ({}, {'five_hour': self.windows()['five_hour']}, self.windows(at=now-500)):
            self.assertFalse(self.m.valid_windows(w, now))
        w=self.windows();w['five_hour']['source']='manual'
        self.assertFalse(self.m.valid_windows(w, now))
        w=self.windows();w['seven_day']['resets_at']=now-1
        self.assertFalse(self.m.valid_windows(w, now))
        w=self.windows();w['five_hour']['used_percentage']=float('nan')
        self.assertFalse(self.m.valid_windows(w, now))

    def test_idle_zero_window_without_reset_is_valid(self):
        w=self.windows()
        for v in w.values():v['resets_at']=None
        self.assertTrue(self.m.valid_windows(w,time.time()))
        w['five_hour']['used_percentage']=1
        self.assertFalse(self.m.valid_windows(w,time.time()))

    def test_success_skips_disabled_parent_and_exhausted_profiles(self):
        job=self.submit();d=self.until(job['job_id'])
        self.assertEqual(d['status'],'completed',d)
        self.assertEqual(d['account'],'alpha')
        self.assertEqual(self.jobs.result(job['job_id'])['text'],'delegated result')
        names={c['account'] for c in self.calls()}
        self.assertNotIn('main',names)
        self.assertFalse(any(c['account'] in ('parent','full') and '-p' in c['args'] for c in self.calls()))
        self.assertEqual((self.jobs.directory(job['job_id'])/'job.json').stat().st_mode & 0o777,0o600)
        args=next(c['args'] for c in self.calls() if '-p' in c['args'])
        self.assertIn('--safe-mode',args)
        self.assertEqual(args[args.index('--tools')+1],'Read,Glob,Grep')

    def test_parallel_jobs_reserve_different_accounts(self):
        first=self.submit('WAIT one');a=self.until(first['job_id'],{'running'})
        second=self.submit('WAIT two');b=self.until(second['job_id'],{'running'})
        self.assertNotEqual(a['account'],b['account'])
        self.jobs.cancel(first['job_id']);self.jobs.cancel(second['job_id'])
        self.assertEqual(self.until(first['job_id'])['status'],'cancelled')
        self.assertEqual(self.until(second['job_id'])['status'],'cancelled')

    def test_worker_crash_keeps_native_lease_and_allows_cancel(self):
        first=self.submit('WAIT one');a=self.until(first['job_id'],{'running'})
        os.kill(a['worker_pid'],signal.SIGKILL)
        self.until(first['job_id'],{'interrupted'})
        with self.assertRaisesRegex(ValueError,'still running'):
            self.jobs.resume(first['job_id'],'Continue')
        second=self.submit('WAIT two');b=self.until(second['job_id'],{'running'})
        self.assertNotEqual(a['account'],b['account'])
        self.assertEqual(self.jobs.cancel(first['job_id'])['status'],'cancelled')
        self.assertEqual(self.jobs.read(second['job_id'])['status'],'running')
        self.jobs.cancel(second['job_id']);self.until(second['job_id'])

    def test_resume_rejects_incomplete_history_and_different_history_root(self):
        first=self.submit();d=self.until(first['job_id'])
        p=self.history/'fixture'/f"{d['session_id']}.jsonl"
        original=p.read_bytes();p.write_bytes(original+b'{"incomplete":')
        with self.assertRaisesRegex(ValueError,'incomplete'):self.jobs.resume(first['job_id'],'continue')
        p.write_bytes(original)
        for n in ('alpha','beta'):
            link=self.root/n/'projects';link.unlink();link.mkdir()
        self.jobs.resume(first['job_id'],'continue')
        self.assertEqual(self.until(first['job_id'])['status'],'blocked')
        self.assertEqual(sum('-p' in c['args'] for c in self.calls()),1)

    def test_cancel_affects_only_the_owned_job(self):
        one=self.submit('WAIT one');self.until(one['job_id'],{'running'})
        two=self.submit('WAIT two');self.until(two['job_id'],{'running'})
        self.jobs.cancel(one['job_id'])
        self.assertEqual(self.until(one['job_id'])['status'],'cancelled')
        self.assertEqual(self.jobs.read(two['job_id'])['status'],'running')
        self.assertEqual(self.until(two['job_id'])['status'],'completed')

    def test_large_prompt_cancellation_does_not_wait_for_stdin_reader(self):
        (self.root/'alpha/fixture.json').write_text(json.dumps({'email':'alpha@example.test','stall_stdin':True}))
        job=self.submit('x'*100_000);self.until(job['job_id'],{'running'})
        self.jobs.cancel(job['job_id'])
        self.assertEqual(self.until(job['job_id'],seconds=5)['status'],'cancelled')

    def test_resume_preserves_session_across_accounts(self):
        first=self.submit();old=self.until(first['job_id'])
        self.registry['disabled']['alpha']={'reason':'test disable'}
        self.a.write_json(self.root/'accounts.json',self.registry)
        resumed=self.jobs.resume(first['job_id'],'Continue the previous result')
        new=self.until(first['job_id'])
        self.assertEqual(new['status'],'completed',new)
        self.assertEqual(new['session_id'],old['session_id'])
        self.assertEqual(new['account'],'beta')
        self.assertEqual(new['attempt'],2)
        self.assertEqual(self.jobs.result(first['job_id'])['text'],'resumed with prior context')

    def test_resume_refuses_concurrent_or_missing_history(self):
        job=self.submit('WAIT');d=self.until(job['job_id'],{'running'})
        with self.assertRaises(ValueError):self.jobs.resume(job['job_id'],'continue')
        self.jobs.cancel(job['job_id']);self.until(job['job_id'])
        (self.history/'fixture'/f"{d['session_id']}.jsonl").unlink()
        with self.assertRaisesRegex(ValueError,'missing'):self.jobs.resume(job['job_id'],'continue')

    def test_generic_execution_failure_is_never_replayed(self):
        job=self.submit('FAIL');d=self.until(job['job_id'])
        self.assertEqual(d['status'],'failed')
        self.assertEqual(sum('-p' in c['args'] for c in self.calls()),1)

    def test_quota_failure_retains_context_and_requires_explicit_resume(self):
        job=self.submit('LIMIT');d=self.until(job['job_id'])
        self.assertEqual(d['status'],'quota_exhausted')
        self.assertEqual(sum('-p' in c['args'] for c in self.calls()),1)
        self.assertTrue((self.history/'fixture'/f"{d['session_id']}.jsonl").exists())

    def test_unknown_quota_blocks_without_model_request(self):
        for n in ('full','alpha','beta'):
            (self.root/n/'fixture.json').write_text(json.dumps({'email':n+'@example.test','bad_usage':True}))
        job=self.submit();d=self.until(job['job_id'])
        self.assertEqual(d['status'],'blocked')
        self.assertFalse(any('-p' in c['args'] for c in self.calls()))

    def test_project_mode_keeps_existing_permissions_without_auto_approval(self):
        job=self.submit(mode='project');self.until(job['job_id'])
        args=next(c['args'] for c in self.calls() if '-p' in c['args'])
        self.assertNotIn('--safe-mode',args)
        self.assertNotIn('--setting-sources',args)
        self.assertNotIn('--allowedTools',args)
        self.assertEqual(args[args.index('--permission-prompts')+1],'none')

    def test_disabled_after_probe_is_not_launched(self):
        # Execute a worker directly to introduce the change at the query boundary.
        with mock.patch.object(self.jobs,'launch',side_effect=lambda d:self.jobs.public(d)):
            job=self.submit()
        d=self.jobs.read(job['job_id']);self.jobs.save(d,worker_pid=os.getpid(),worker_start=self.m.process_start(os.getpid()))
        def disable(reg,name,status,stop,cwd):
            self.registry['disabled'][name]={'reason':'disabled during query'}
            self.a.write_json(self.root/'accounts.json',self.registry)
            return self.windows()
        with mock.patch.object(self.jobs,'checked_windows',side_effect=disable),mock.patch.object(self.jobs,'run_native') as run:
            self.jobs.worker(job['job_id']);run.assert_not_called()
        # Avoid cleanup signaling the test runner.
        d=self.jobs.read(job['job_id']);self.jobs.save(d,worker_pid=None)

    def test_identity_change_does_not_reuse_old_quota(self):
        self.a.write_json(self.jobs.root/'quota/alpha.json',{'identity':'wrong','windows':self.windows(),'usage_query_started_at':time.time()})
        self.assertEqual(self.jobs.cached_windows('alpha','new-identity'),({},0))

    def test_newer_limit_without_reset_overrides_cached_quota(self):
        start=time.time()-5
        self.a.write_json(self.root/'state/quotas/alpha.json',{'recorded_at':time.time(),'blocked_until':0})
        with mock.patch.object(self.jobs,'cached_windows',return_value=(self.windows(),start)):
            with self.assertRaisesRegex(ValueError,'Newer quota failure'):
                self.jobs.checked_windows(self.registry,'alpha',{'email':'alpha@example.test'},threading_event(),str(self.project))

    def test_duplicate_identity_cannot_take_second_lease(self):
        h=self.m.identity({'email':'alpha@example.test'})
        with self.m.lease(self.jobs.root/'leases'/f'identity-{h}.lock') as held:
            self.assertTrue(held)
            job=self.submit();d=self.until(job['job_id'])
            self.assertEqual(d['account'],'beta')

    def test_invalid_input_never_creates_job(self):
        for bad in ('../bad','x'*32,None):
            with self.assertRaises(ValueError):self.jobs.read(bad)
        for kwargs in ({'mode':'bypassPermissions'},{'model':'--bad'},{'max_seconds':True}):
            with self.assertRaises(ValueError):self.submit(**kwargs)
        self.assertEqual(self.jobs.listing(),{'jobs':[]})

    def test_mcp_handshake_tools_and_tool_errors(self):
        requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}},
                  {'jsonrpc':'2.0','method':'notifications/initialized'},
                  {'jsonrpc':'2.0','id':2,'method':'tools/list'},
                  {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'result','arguments':{'job_id':'../bad'}}},
                  {'jsonrpc':'2.0','id':4,'method':'not-a-method'}]
        out=io.StringIO();self.m.serve(self.jobs,io.StringIO('\n'.join(map(json.dumps,requests))),out)
        replies=[json.loads(s) for s in out.getvalue().splitlines()]
        self.assertEqual([r['id'] for r in replies],[1,2,3,4])
        self.assertEqual(len(replies[1]['result']['tools']),5)
        self.assertTrue(replies[2]['result']['isError'])
        self.assertEqual(replies[3]['error']['code'],-32601)

    def test_share_when_short_puts_second_job_on_busy_account(self):
        self.policy(share_when_short=True)
        self.fixture('beta',used=100)
        first=self.submit('WAIT one');a=self.until(first['job_id'],{'running'})
        second=self.submit('WAIT two');b=self.until(second['job_id'],{'running'})
        self.assertEqual((a['account'],b['account']),('alpha','alpha'))
        self.assertFalse(a.get('shared'));self.assertTrue(b['shared'])
        for j in (first,second):self.jobs.cancel(j['job_id'])
        for j in (first,second):self.assertEqual(self.until(j['job_id'])['status'],'cancelled')

    def test_free_account_resetting_soon_is_waited_for_instead_of_sharing(self):
        self.policy(share_when_short=True,wait_for_account=True,retry_seconds=1,share_wait_seconds=900)
        self.fixture('beta',used=100)
        first=self.submit('WAIT one');self.until(first['job_id'],{'running'})
        now=time.time()
        self.a.write_json(self.jobs.root/'quota/session/beta.json',{'at':now,'windows':{
            'five_hour':{'used_percentage':100,'resets_at':now+3},'seven_day':{'used_percentage':20,'resets_at':now+86400}}})
        second=self.submit('WAIT two');d=self.until(second['job_id'],{'waiting'})
        self.assertIn('resets soon',d['reason'])
        self.fixture('beta',used=0)
        self.a.write_json(self.jobs.root/'quota/beta.json',{'identity':self.m.identity({'email':'beta@example.test'}),
                          'windows':self.windows(),'usage_query_started_at':time.time()})
        d=self.until(second['job_id'],{'running','completed'},seconds=20)
        self.assertEqual(d['account'],'beta');self.assertFalse(d.get('shared'))
        for j in (first,second):self.jobs.cancel(j['job_id'])
        for j in (first,second):self.until(j['job_id'])

    def test_short_pool_without_sharing_still_blocks(self):
        self.fixture('beta',used=100)
        first=self.submit('WAIT one');self.until(first['job_id'],{'running'})
        second=self.submit('WAIT two')
        self.assertEqual(self.until(second['job_id'])['status'],'blocked')
        self.jobs.cancel(first['job_id']);self.until(first['job_id'])

    def test_waits_for_an_account_then_runs(self):
        self.policy(wait_for_account=True,retry_seconds=1)
        for n in ('alpha','beta'):self.fixture(n,used=100)
        job=self.submit();d=self.until(job['job_id'],{'waiting'})
        self.assertIn('exhausted until',d['reason'])
        calls=len(self.calls());time.sleep(2.5)
        # Recorded full windows are skipped until their reset, without new queries.
        self.assertEqual(len([c for c in self.calls()[calls:] if '/usage' in c['args']]),0)
        self.fixture('beta',used=0)
        # A fresh reading, as the dashboard records after a reset.
        self.a.write_json(self.jobs.root/'quota/beta.json',{'identity':self.m.identity({'email':'beta@example.test'}),
                          'windows':self.windows(),'usage_query_started_at':time.time()})
        d=self.until(job['job_id'],seconds=20)
        self.assertEqual((d['status'],d['account']),('completed','beta'),d)

    def test_waiting_job_can_be_cancelled(self):
        self.policy(wait_for_account=True,retry_seconds=1)
        for n in ('alpha','beta'):self.fixture(n,used=100)
        job=self.submit();self.until(job['job_id'],{'waiting'})
        self.jobs.cancel(job['job_id'])
        self.assertEqual(self.until(job['job_id'])['status'],'cancelled')
        self.assertEqual(self.runs(),[])

    def test_quota_stop_continues_in_same_session_on_another_account(self):
        self.policy(auto_continue=True)
        job=self.submit('LIMIT then finish');d=self.until(job['job_id'],seconds=30)
        self.assertEqual(d['status'],'completed',d)
        self.assertEqual((d['account'],d['attempt'],d['continues']),('beta',2,1))
        self.assertEqual([c['account'] for c in self.runs()],['alpha','beta'])
        self.assertIn('--resume',self.runs()[1]['args'])
        self.assertEqual(self.jobs.result(job['job_id'])['text'],'resumed with prior context')
        self.assertEqual((d['attempts'][0]['status'],d['attempts'][0]['kind']),('quota_exhausted','quota'))
        session=json.loads((self.jobs.root/'quota/session/alpha.json').read_text())
        self.assertEqual(session['windows']['five_hour']['used_percentage'],100)

    def test_continuation_limit_leaves_job_for_review(self):
        self.policy(auto_continue=True,max_continues=0)
        job=self.submit('LIMIT');d=self.until(job['job_id'])
        self.assertEqual(d['status'],'quota_exhausted')
        self.assertIn('limit',d['auto_stop'])
        self.assertEqual(self.jobs.supervise_once(),[])
        self.assertEqual(len(self.runs()),1)

    def test_reply_from_another_model_stops_and_continues_elsewhere(self):
        self.policy(auto_continue=True,enforce_model=True)
        job=self.submit('WRONGMODEL',model='claude-opus-5-5[1m]');d=self.until(job['job_id'],seconds=30)
        self.assertEqual(d['status'],'completed',d)
        self.assertEqual(d['model_mismatches'][0]['model'],'claude-other-1')
        self.assertEqual(d['attempts'][0]['status'],'model_mismatch')
        self.assertEqual([c['account'] for c in self.runs()],['alpha','beta'])

    def test_effort_comes_from_job_then_policy(self):
        effort=lambda: (lambda a: a[a.index('--effort')+1] if '--effort' in a else None)(self.runs()[-1]['args'])
        self.policy(effort='high')
        d=self.until(self.submit()['job_id']);self.assertEqual(d['status'],'completed',d)
        self.assertEqual(effort(),'high')
        self.until(self.submit(effort='max')['job_id']);self.assertEqual(effort(),'max')
        self.policy(effort='extreme')
        self.until(self.submit()['job_id']);self.assertIsNone(effort())
        with self.assertRaises(ValueError):
            self.submit(effort='extreme')

    def test_interactive_cap_uses_parent_account_last(self):
        for name in ('alpha', 'beta'):
            self.fixture(name, used=100)
        self.policy(interactive_cap=80)
        d=self.until(self.submit()['job_id'], seconds=30)
        self.assertEqual(d['status'],'completed',d)
        self.assertEqual((d['account'],d['cap']),('parent',80))
        self.assertEqual([c['account'] for c in self.runs()],['parent'])

    def test_free_account_comes_before_capped_parent(self):
        self.policy(interactive_cap=80)
        d=self.until(self.submit()['job_id'], seconds=30)
        self.assertEqual(d['status'],'completed',d)
        self.assertEqual(d['account'],'alpha')
        self.assertIsNone(d['cap'])

    def test_interactive_cap_needs_room_to_start(self):
        for name in ('alpha', 'beta'):
            self.fixture(name, used=100)
        self.fixture('parent', used=75)
        self.policy(interactive_cap=80)
        d=self.until(self.submit()['job_id'], seconds=30)
        self.assertEqual(d['status'],'blocked',d)
        self.assertIn('parent: shared with an interactive session, 75% used (limit 80%)', d['reason'])
        self.assertEqual(self.runs(),[])

    def test_without_cap_parent_stays_excluded(self):
        for name in ('alpha', 'beta'):
            self.fixture(name, used=100)
        d=self.until(self.submit()['job_id'], seconds=30)
        self.assertEqual(d['status'],'blocked',d)
        self.assertEqual(self.runs(),[])

    def test_reaching_cap_stops_and_continues_elsewhere(self):
        for name in ('alpha', 'beta'):
            self.fixture(name, used=100)
        self.policy(interactive_cap=80, auto_continue=True)
        d=self.until(self.submit('CLIMB')['job_id'], seconds=30)
        # Stopped at the reading, not after the fake session's 20 seconds.
        self.assertLess(d['run_seconds'], 10)
        self.assertEqual(d['attempts'][0]['account'],'parent')
        self.assertEqual((d['attempts'][0]['status'],d['attempts'][0]['kind']),('quota_exhausted','reserve'))
        # The parent is avoided now and the others are full, so the continuation finds nothing.
        self.assertEqual(d['status'],'blocked',d)
        self.assertIn('parent: stopped this job recently', d['reason'])

    def test_service_error_continues_after_backoff(self):
        self.policy(auto_continue=True,transient_seconds=1)
        job=self.submit('FLAKY');d=self.until(job['job_id'],seconds=30)
        self.assertEqual(d['status'],'completed',d)
        self.assertEqual(d['attempts'][0]['kind'],'transient')

    def test_supervisor_continues_quota_stop_left_by_older_worker(self):
        job=self.submit('LIMIT');d=self.until(job['job_id'])
        # The worker saves its final status just before it exits; the supervisor skips a live worker.
        deadline=time.monotonic()+5
        while self.m.process_start(d['worker_pid'])==d['worker_start'] and time.monotonic()<deadline:time.sleep(.05)
        d.pop('stop_kind');self.jobs.save(d)
        self.assertEqual(self.jobs.supervise_once(),[])
        self.policy(auto_continue=True)
        self.assertEqual(self.jobs.supervise_once(),[job['job_id']])
        d=self.until(job['job_id'],seconds=30)
        self.assertEqual((d['status'],d['account'],d['attempt']),('completed','beta',2),d)
        self.assertEqual(self.jobs.supervise_once(),[])

    def test_supervisor_continues_after_worker_and_session_died(self):
        self.policy(auto_continue=True)
        job=self.submit('WAIT then finish');d=self.until(job['job_id'],{'running'})
        os.kill(d['worker_pid'],signal.SIGKILL);os.killpg(d['native_pid'],signal.SIGKILL)
        self.until(job['job_id'],{'interrupted'})
        deadline=time.monotonic()+5
        while self.m.process_start(d['native_pid'])==d['native_start'] and time.monotonic()<deadline:time.sleep(.05)
        self.assertEqual(self.jobs.supervise_once(),[job['job_id']])
        d=self.until(job['job_id'],seconds=30)
        self.assertEqual(d['status'],'completed',d)
        self.assertEqual(d['attempts'][-1]['kind'],'crash')

    def test_supervisor_leaves_cancelled_jobs_alone(self):
        self.policy(auto_continue=True)
        job=self.submit('WAIT');self.until(job['job_id'],{'running'})
        self.jobs.cancel(job['job_id']);self.assertEqual(self.until(job['job_id'])['status'],'cancelled')
        self.assertEqual(self.jobs.supervise_once(),[])

    def test_official_usage_read_replaces_usage_screen(self):
        with mock.patch.object(self.probe,'official_usage',side_effect=lambda reg,name:(name+'@example.test',self.windows())):
            d=self.run_worker_inline()
        self.assertEqual((d['status'],d['account']),('completed','full'))
        self.assertFalse(any('/usage' in c['args'] for c in self.calls()))

    def test_official_usage_for_another_identity_is_ignored(self):
        with mock.patch.object(self.probe,'official_usage',return_value=('someone@example.test',self.windows())):
            d=self.run_worker_inline()
        self.assertEqual((d['status'],d['account']),('completed','alpha'))

    def test_unstarted_session_window_counts_as_unused_after_known_reset(self):
        self.fixture('alpha',no_session=True)
        now=time.time()
        self.a.write_json(self.root/'state/monitor/accounts.json',{'alpha':{'loggedIn':True,'email':'alpha@example.test',
            'windows':{'five_hour':{'used_percentage':100,'resets_at':now-60,'observed_at':now-4000,'source':'official_cli_usage'}}}})
        with mock.patch.object(self.probe,'PROBE_RETRY_SECONDS',0):
            d=self.run_worker_inline()
        self.assertEqual((d['status'],d['account']),('completed','alpha'),d)
        windows=json.loads((self.jobs.root/'quota/alpha.json').read_text())['windows']
        self.assertEqual(windows['five_hour']['inferred'],'session_not_started')

    def test_missing_session_window_without_history_is_not_guessed(self):
        self.fixture('alpha',no_session=True)
        with mock.patch.object(self.probe,'PROBE_RETRY_SECONDS',0):
            d=self.run_worker_inline()
        self.assertEqual((d['status'],d['account']),('completed','beta'))

    def test_interactive_accounts_are_avoided_when_configured(self):
        self.policy(avoid_interactive_accounts=True)
        self.fixture('full',used=0)
        with mock.patch.object(self.jobs,'interactive_accounts',return_value={'full','alpha'}):
            d=self.run_worker_inline()
        self.assertEqual(d['account'],'beta')


class WakeTests(unittest.TestCase):
    """The waiter that tells a session about its jobs, on job files written directly."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base/'accounts'
        self.project = self.base/'project'
        for p in (self.root/'one', self.root/'two', self.project):
            p.mkdir(parents=True)
        (self.root/'accounts.json').write_text(json.dumps({
            'version': 1, 'claude': sys.executable, 'default': 'one',
            'profiles': {'one': str(self.root/'one'), 'two': str(self.root/'two')}}))
        patch = mock.patch.dict(os.environ, {'CLAUDE_ACCOUNTS_HOME': str(self.root),
                                             'CLAUDE_CONFIG_DIR': str(self.root/'one')})
        patch.start(); self.addCleanup(patch.stop)
        self.m = monitor.load_account_module(REPO_ROOT/'bin/claude-delegate')
        self.jobs = self.m.Jobs(*self.m.dependencies())
        self.me = (os.getpid(), self.m.process_start(os.getpid()))
        self.scopes = [str(self.project.resolve())]

    def job(self, status='completed', parent='sess-a', **fields):
        d = {'job_id': os.urandom(16).hex(), 'session_id': str(uuid.uuid4()), 'title': 'T fixture',
             'cwd': str(self.project.resolve()), 'mode': 'project', 'status': status, 'account': 'two',
             'attempt': 1, 'created_at': time.time(), 'updated_at': time.time(), 'parent_session': parent, **fields}
        self.jobs.a.write_json(self.jobs.directory(d['job_id'])/'job.json', d)
        return d

    def supervisor(self):
        self.jobs.a.write_json(self.jobs.root/'supervisor.json', {
            'pid': self.me[0], 'start': self.me[1], 'at': time.time(), 'interval': 30})

    def client(self):
        """A stand-in client process that the test can end."""
        p = subprocess.Popen(['sleep', '60'])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p, (p.pid, self.m.process_start(p.pid))

    def test_first_waiter_only_notes_what_already_happened(self):
        self.job()
        self.assertEqual(self.jobs.wake_round('sess-a', self.scopes, self.me), [])
        d = self.job(title='T10 沙滩')
        lines = self.jobs.wake_round('sess-a', self.scopes, self.me)
        self.assertEqual(len(lines), 1)
        self.assertIn('T10 沙滩', lines[0]); self.assertIn('已完成', lines[0]); self.assertIn(d['job_id'], lines[0])
        # Shown once to the client that is still running.
        self.assertEqual(self.jobs.wake_round('sess-a', self.scopes, self.me), [])

    def test_jobs_belong_to_their_session_or_to_the_directory_when_unrecorded(self):
        for s in ('sess-a', 'sess-b'):
            self.jobs.wake_round(s, self.scopes, self.me)
        self.job(parent='sess-a', title='mine')
        self.job(parent=None, title='old')
        self.job(parent=None, title='elsewhere', cwd=str(self.base))
        a = self.jobs.wake_round('sess-a', self.scopes, self.me)
        b = self.jobs.wake_round('sess-b', self.scopes, self.me)
        self.assertEqual(sorted(l.split('（')[0] for l in a), ['- mine', '- old'])
        self.assertEqual([l.split('（')[0] for l in b], ['- old'])

    def test_running_and_continuing_jobs_stay_quiet_until_a_stop_needs_the_session(self):
        self.jobs.a.write_json(self.jobs.root/'policy.json', {'auto_continue': True})
        self.supervisor()
        self.jobs.wake_round('sess-a', self.scopes, self.me)
        self.job(status='running')
        self.job(status='waiting')
        quota = self.job(status='quota_exhausted')
        self.assertEqual(self.jobs.wake_round('sess-a', self.scopes, self.me), [])
        # The supervisor has not continued it in time.
        quota['updated_at'] = time.time()-self.m.WAKE_GRACE-1
        self.jobs.a.write_json(self.jobs.directory(quota['job_id'])/'job.json', quota)
        lines = self.jobs.wake_round('sess-a', self.scopes, self.me)
        self.assertEqual(len(lines), 1); self.assertIn('额度用完', lines[0])
        stopped = self.job(status='failed', auto_stop='Automatic continuation limit reached')
        lines = self.jobs.wake_round('sess-a', self.scopes, self.me)
        self.assertEqual(len(lines), 1); self.assertIn('limit reached', lines[0])
        self.assertIn(stopped['job_id'], lines[0])

    def test_without_a_supervisor_a_stop_is_reported_at_once(self):
        self.jobs.a.write_json(self.jobs.root/'policy.json', {'auto_continue': True})
        self.jobs.wake_round('sess-a', self.scopes, self.me)
        self.job(status='quota_exhausted')
        self.assertEqual(len(self.jobs.wake_round('sess-a', self.scopes, self.me)), 1)

    def test_notice_lost_with_an_exited_client_is_sent_again_unless_it_arrived(self):
        old, client = self.client()
        self.jobs.wake_round('sess-a', self.scopes, client)
        lost = self.job(title='lost')
        arrived = self.job(title='arrived')
        self.assertEqual(len(self.jobs.wake_round('sess-a', self.scopes, client)), 2)
        old.kill(); old.wait()
        transcript = self.base/'session.jsonl'
        transcript.write_text(json.dumps({'type': 'attachment', 'text': f"[wake:{arrived['job_id']}:1:completed]"})+'\n')
        lines = self.jobs.wake_round('sess-a', self.scopes, self.me, transcript=str(transcript))
        self.assertEqual(len(lines), 1)
        self.assertIn(lost['job_id'], lines[0]); self.assertIn('可能没送到', lines[0])
        self.assertEqual(self.jobs.wake_round('sess-a', self.scopes, self.me, transcript=str(transcript)), [])

    def test_hook_waiter_exits_2_with_notices_and_quietly_when_client_is_gone(self):
        self.jobs.wake_round('sess-a', self.scopes, self.me)
        self.job()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.jobs.wake('sess-a', self.scopes, self.me, hook=True, poll=.05, seconds=5), 2)
        self.assertIn('委派任务有新进展', err.getvalue())
        old, client = self.client()
        old.kill(); old.wait()
        started = time.monotonic()
        self.assertEqual(self.jobs.wake('sess-a', self.scopes, client, hook=True, poll=.05), 0)
        self.assertLess(time.monotonic()-started, 2)

    def test_one_waiter_per_client_and_none_in_a_delegated_session(self):
        directory = self.jobs.root/'wake'
        self.jobs.a.private_dir(directory)
        fd = os.open(directory/f'sess-a.{self.me[0]}.wait', os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.job()
        self.assertEqual(self.jobs.wake('sess-a', self.scopes, self.me, hook=True, poll=.05, seconds=1), 0)
        d = self.job(status='running')
        started = time.monotonic()
        self.assertEqual(self.jobs.wake(d['session_id'], self.scopes, self.me, hook=True, poll=.05, seconds=5), 0)
        self.assertLess(time.monotonic()-started, 2)

    def test_hook_install_is_idempotent_keeps_other_settings_and_uninstalls(self):
        settings = self.project/'.claude'/'settings.local.json'
        settings.parent.mkdir()
        other = {'hooks': [{'type': 'command', 'command': 'echo other'}]}
        settings.write_text(json.dumps({'permissions': {'allow': ['Bash']}, 'hooks': {'Stop': [other]}}))
        settings.chmod(0o640)
        for _ in range(2):
            self.jobs.wake_hook('install', str(self.project))
        value = json.loads(settings.read_text())
        self.assertEqual(value['permissions'], {'allow': ['Bash']})
        self.assertIn(other, value['hooks']['Stop'])
        for event in self.m.WAKE_EVENTS:
            ours = [h for g in value['hooks'][event] for h in g['hooks'] if h['command'].endswith(' _wake')]
            self.assertEqual(len(ours), 1)
            self.assertTrue(ours[0]['asyncRewake']); self.assertLess(ours[0]['timeout']*1000, 2**31-1)
        self.assertEqual(settings.stat().st_mode & 0o777, 0o640)
        self.assertIn('installed', self.jobs.wake_hook('status', str(self.project))[0])
        self.jobs.wake_hook('uninstall', str(self.project))
        value = json.loads(settings.read_text())
        self.assertEqual(value['hooks'], {'Stop': [other]})
        self.assertIn('not installed', self.jobs.wake_hook('status', str(self.project))[0])

    def test_hook_install_covers_every_account(self):
        report = self.jobs.wake_hook('install')
        self.assertEqual(len(report), 2)
        for name in ('one', 'two'):
            value = json.loads((self.root/name/'settings.json').read_text())
            self.assertEqual(set(value['hooks']), set(self.m.WAKE_EVENTS))

    def test_submit_records_the_parent_session(self):
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': 'e478f8a3-5077-4f9f-bf20-7c4aab95b516'}), \
                mock.patch.object(self.jobs, 'launch', side_effect=lambda d: self.jobs.public(d)):
            job = self.jobs.submit(prompt='x', cwd=str(self.project))
        self.assertEqual(self.jobs.read(job['job_id'])['parent_session'], 'e478f8a3-5077-4f9f-bf20-7c4aab95b516')


def threading_event():
    import threading
    return threading.Event()


if __name__ == '__main__':
    unittest.main()
