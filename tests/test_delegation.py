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
    print('Current session\n'+str(options.get('used', 0))+'% used\nResets 11:59pm (UTC)\nCurrent week (all models)\n20% used\nResets 11:59pm (UTC)', flush=True)
    time.sleep(15); sys.exit(0)
assert '-p' in sys.argv and '--strict-mcp-config' in sys.argv
assert sys.argv[sys.argv.index('--permission-mode')+1] == 'dontAsk'
assert '--dangerously-skip-permissions' not in sys.argv
assert 'CLAUDECODE' not in os.environ
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
if 'WAIT' in prompt: time.sleep(8)
if 'FAIL' in prompt: sys.exit(1)
if 'LIMIT' in prompt:
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
               'CLAUDE_CONFIG_DIR': profiles['parent'], 'CLAUDECODE': '1'}
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


def threading_event():
    import threading
    return threading.Event()


if __name__ == '__main__':
    unittest.main()
