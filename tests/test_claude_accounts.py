import importlib.machinery
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock
from _support import REPO_ROOT, WINDOWS
try:
    import fcntl
    import pty
except ImportError:  # Windows: tests/test_windows.py drives the launcher through ConPTY.
    fcntl = pty = None

MODULE_PATH = REPO_ROOT/'bin/claude-accounts'
loader = importlib.machinery.SourceFileLoader('accounts', str(MODULE_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

FAKE = r'''#!/usr/bin/env python3
import json, os, pathlib, signal, subprocess, sys, threading, time
profile = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
name = profile.name
if sys.argv[1:] == ['auth','status']:
    print(json.dumps({'loggedIn':True,'authMethod':'claude.ai','subscriptionType':'pro','email':name+'@example.test'}))
    sys.exit(0)
args = sys.argv[1:]
settings = json.loads(pathlib.Path(args[args.index('--settings')+1]).read_text(encoding='utf-8'))
sid = os.environ['TEST_SESSION']
project = pathlib.Path(os.environ['CLAUDE_ACCOUNTS_HOME'])/'main'/'projects'/'fake-project'
project.mkdir(parents=True,exist_ok=True)
transcript = project/(sid+'.jsonl')
def save(row):
    with transcript.open('a') as stream:
        stream.write(json.dumps(row)+'\n')
        stream.flush(); os.fsync(stream.fileno())
save({'type':'user','message':{'role':'user','content':'Original task must survive'},'sessionId':sid})
trace = pathlib.Path(os.environ['TEST_TRACE'])
with trace.open('a') as f: f.write(json.dumps({'account':name,'args':args,'session_id':sid})+'\n')
def hook(event, **extra):
    payload = {'hook_event_name':event,'session_id':sid,'transcript_path':str(transcript),'cwd':str(project),**extra}
    for entry in settings['hooks'].get(event,[]):
        for command in entry['hooks']:
            subprocess.run(command['command'],shell=True,input=json.dumps(payload),text=True,check=True)
hook('SessionStart')
def exit_cleanly(*_):
    save({'type':'system','data':'final-flush-before-exit'})
    hook('SessionEnd')
    sys.exit(0)
signal.signal(signal.SIGTERM,exit_cleanly)
scenario = os.environ['TEST_SCENARIO']
if scenario == 'login-retry-manual':
    hook('SessionEnd')
    hook('SessionStart')
    hook('UserPromptSubmit',prompt='continue prior task')
    hook('StopFailure',error='server_error',last_assistant_message='Could not refresh your login because another Claude Code process is refreshing it (or exited mid-refresh)')
    line=sys.stdin.readline()
    assert '因登录刷新冲突而未能执行的请求' in line, repr(line)
    hook('UserPromptSubmit',prompt=line)
    hook('Stop')
    print('MANUAL_RETRY_OK',flush=True)
    time.sleep(.2)
    sys.exit(0)
if scenario == 'startup':
    print('STARTUP_OK',flush=True)
    time.sleep(.15)
    sys.exit(0)
if scenario == 'concurrent' and name == 'main':
    barrier = pathlib.Path(os.environ['TEST_BARRIER'])
    (barrier/(sid+'.ready')).touch()
    deadline = time.monotonic()+8
    while len(list(barrier.glob('*.ready'))) < int(os.environ['TEST_CONCURRENCY']):
        if time.monotonic() > deadline:
            print('CONCURRENCY_BARRIER_TIMEOUT',flush=True)
            sys.exit(2)
        time.sleep(.05)
if (scenario.startswith('login-retry-') or scenario in ('switch','concurrent','subagents','child-only','stale-background','scratch-links','disable-next','native-receipts')) and name == os.environ['TEST_LAST_ACCOUNT']:
    assert '--resume' in args and args[args.index('--resume')+1] == sid
    assert '--tools' in args and args[args.index('--tools')+1] == 'Read'
    if scenario == 'concurrent':
        assert '--effort' in args and args[args.index('--effort')+1] == 'max'
    if scenario.startswith('login-retry-'):
        import select
        hook('UserPromptSubmit',prompt='original automatic recovery')
        for attempt in range(3):
            hook('StopFailure',error='server_error',last_assistant_message='Could not refresh your login because another Claude Code process is refreshing it (or exited mid-refresh) · Try again in a minute; if it keeps happening, close other Claude Code windows or sign in again with /login')
            print('NATIVE_REFRESH_BUSY',flush=True)
            if scenario == 'login-retry-keyboard' or attempt == 2:
                time.sleep(1)
                assert not select.select([sys.stdin],[],[],0)[0], 'Unexpected continuation after cancellation/exhaustion'
                run=pathlib.Path(args[args.index('--settings')+1]).parent
                status=json.loads((run/'retry-status.json').read_text(encoding='utf-8'))
                assert status['phase'] == ('cancelled' if scenario == 'login-retry-keyboard' else 'exhausted'), status
                print('CANCEL_OK' if scenario == 'login-retry-keyboard' else 'BUDGET_OK',flush=True)
                sys.exit(0)
            line=sys.stdin.readline()
            assert '继续上一条自动恢复请求' in line,repr(line)
            with trace.open('a') as f: f.write(json.dumps({'retry':attempt+1,'account':name,'session_id':sid})+'\n')
            hook('UserPromptSubmit',prompt=line)
            if attempt == 1 and scenario == 'login-retry-success':
                hook('Stop')
                print('RESUME_OK',flush=True)
                time.sleep(.2)
                sys.exit(0)
    if scenario in ('subagents','child-only','stale-background'):
        pending_path=pathlib.Path(args[args.index('--settings')+1]).parent/'pending-recovery.json'
        pending=json.loads(pending_path.read_text(encoding='utf-8'))
        assert pending['verified'] is True
        assert pending['pending_agents']==['worker-failed']
        manifest=json.loads(pathlib.Path(pending['manifest']).read_text(encoding='utf-8'))
        assert len(manifest['agents'])==3
        assert manifest['agents']['worker-slow']['state']=='stopped'
        assert manifest['agents']['worker-failed']['state']=='failed'
        for agent in ('worker-slow','worker-failed','worker-done'):
            assert (project/sid/'subagents'/('agent-'+agent+'.jsonl')).is_file()
        assert 'late-final-output' in (project/sid/'subagents'/'agent-worker-slow.jsonl').read_text(encoding='utf-8')
        hook('SubagentStart',agent_id='worker-failed',agent_type='general-purpose')
        after=json.loads(pending_path.read_text(encoding='utf-8'))
        assert after['pending_agents']==[] and after['resumed_agents']==['worker-failed']
    print('RESUME_OK',flush=True)
    time.sleep(.15)
    sys.exit(0)
if scenario == 'safety':
    hook('StopFailure',error='rate_limit',last_assistant_message='Session paused: safeguards flagged this session [cyber]')
    time.sleep(.15)
    sys.exit(0)
if scenario == 'transient':
    hook('StopFailure',error='rate_limit',last_assistant_message='429 Too many requests')
    time.sleep(.15)
    sys.exit(0)
if scenario == 'banned':
    hook('StopFailure',error='authentication_failed',last_assistant_message='Your account has been disabled.')
    hook('StopFailure',error='rate_limit',last_assistant_message="You've hit your session limit")
    time.sleep(.3)
    sys.exit(0)
if scenario == 'disable-next':
    registry_path=pathlib.Path(os.environ['CLAUDE_ACCOUNTS_HOME'])/'accounts.json'
    registry=json.loads(registry_path.read_text(encoding='utf-8'))
    registry['disabled']={'second':{'reason':'disabled while controller is running'}}
    registry_path.write_text(json.dumps(registry))
if scenario == 'scratch-links':
    import tempfile
    scratch=pathlib.Path(tempfile.gettempdir())/('claude-'+str(os.getuid()))/'rotation-test'/sid/'scratchpad'
    scratch.mkdir(parents=True)
    (scratch/'assets').symlink_to(project,target_is_directory=True)
    (scratch/'dangling').symlink_to('not-present')
    hook('Stop',scratchpad_dir=str(scratch))
if scenario == 'native-receipts':
    import datetime, tempfile
    stamp=lambda t:datetime.datetime.fromtimestamp(t,datetime.timezone.utc).isoformat()
    now=time.time()
    for task in ('server','waiter'):
        save({'type':'user','sessionId':sid,'timestamp':stamp(now-10),
              'toolUseResult':{'backgroundTaskId':task},'message':{'content':[{'type':'tool_result','tool_use_id':task+'-launch','content':'background job'}]}})
    save({'type':'user','sessionId':sid,'timestamp':stamp(now-5),
          'toolUseResult':{'task_id':'server','task_type':'local_bash','message':'Successfully stopped task: server (node server.mjs)'},
          'message':{'content':[{'type':'tool_result','tool_use_id':'server-stop','content':'stopped'}]}})
    save({'type':'queue-operation','operation':'enqueue','sessionId':sid,'timestamp':stamp(now-4),
          'content':'<task-notification><task-id>waiter</task-id><status>completed</status></task-notification>'})
    scratch=pathlib.Path(tempfile.gettempdir())/('claude-'+str(os.getuid()))/'rotation-test'/sid/'scratchpad'
    scratch.mkdir(parents=True)
    (scratch/'.credentials.json').write_text('isolated fake credentials must never be backed up')
    hook('Stop',scratchpad_dir=str(scratch),background_tasks=[{'id':task,'type':'shell','status':'running'} for task in ('server','waiter')])
if scenario in ('subagents','child-only','stale-background'):
    subdir=project/sid/'subagents'; subdir.mkdir(parents=True,exist_ok=True)
    for agent in ('worker-slow','worker-failed','worker-done'):
        path=subdir/('agent-'+agent+'.jsonl')
        path.write_text(json.dumps({'type':'user','message':{'content':'full private context '+agent}})+'\n')
        path.with_suffix('.meta.json').write_text(json.dumps({'agentType':'general-purpose'}))
        hook('SubagentStart',agent_id=agent,agent_type='general-purpose')
    hook('SubagentStop',agent_id='worker-done',agent_type='general-purpose',agent_transcript_path=str(subdir/'agent-worker-done.jsonl'),last_assistant_message='done')
    hook('PreToolUse',agent_id='worker-slow',tool_use_id='slow-tool',tool_name='Bash',tool_input={'command':'fake harmless test'})
    if scenario == 'stale-background':
        hook('Stop',background_tasks=[
            {'id':'worker-failed','type':'subagent','status':'running'},
            {'id':'watch','type':'shell','status':'running','command':'python3 '+str(project/'watch.py')}])
    hook('StopFailure',agent_id='worker-failed',error='rate_limit',last_assistant_message="You've hit your session limit")
    hook('SubagentStop',agent_id='worker-failed',agent_type='general-purpose',agent_transcript_path=str(subdir/'agent-worker-failed.jsonl'))
    def finish_later():
        time.sleep(3)
        with (subdir/'agent-worker-slow.jsonl').open('a') as stream:
            stream.write(json.dumps({'type':'assistant','message':{'content':'late-final-output'}})+'\n')
        hook('PostToolUse',agent_id='worker-slow',tool_use_id='slow-tool',tool_name='Bash',tool_response={'stdout':'late-final-output'})
        hook('SubagentStop',agent_id='worker-slow',agent_type='general-purpose',agent_transcript_path=str(subdir/'agent-worker-slow.jsonl'),last_assistant_message='late-final-output')
    threading.Thread(target=finish_later,daemon=True).start()
if scenario == 'missing-history':
    transcript.unlink()
if scenario != 'child-only':
    hook('StopFailure',error='rate_limit',last_assistant_message="You've hit your session limit · resets later")
if scenario == 'missing-history':
    time.sleep(.5); sys.exit(0)
time.sleep(30)
'''


def write_quota_worker(state_dir, name, blocked_until, barrier, stage):
    # stage tells a failing test how far this worker got.
    Path(stage).write_text('barrier')
    barrier.wait(timeout=30)
    Path(stage).write_text('record')
    m.record_quota(Path(state_dir),name,blocked_until)
    Path(stage).write_text('done')


class AccountsTests(unittest.TestCase):
    def test_only_confirmed_quota(self):
        event = {'error':'rate_limit','message':"You've hit your session limit"}
        self.assertTrue(m.quota_failure(event, {}))
        self.assertFalse(m.quota_failure({'error':'rate_limit','message':'429'}, {}))
        self.assertTrue(m.quota_failure({**event,'agent_id':'subagent'}, {}))
        self.assertFalse(m.quota_failure({**event,'error':'billing_error'}, {}))
        self.assertFalse(m.quota_failure({**event,'message':'[cyber] '+event['message']}, {}))
        usage = {'at':time.time(),'limits':{'five_hour':{'used_percentage':100,'resets_at':time.time()+60}}}
        self.assertTrue(m.quota_failure({'error':'rate_limit'}, usage))
        self.assertFalse(m.quota_failure({'error':'rate_limit','agent_id':'child','message':'429'}, usage))
        self.assertFalse(m.quota_failure({**event,'agent_id':'child','message':'[cyber] '+event['message']}, usage))
        usage['at'] -= 300
        self.assertFalse(m.quota_failure({'error':'rate_limit'}, usage))

    def test_resume_preserves_restrictions(self):
        sid = str(uuid.uuid4())
        result = m.resume_arguments(['--tools','Read','--permission-mode','plan','--add-dir','/tmp/a',
                                     '--strict-mcp-config','--resume','old-id','original task'],sid)
        self.assertEqual(result[:7],['--tools','Read','--permission-mode','plan','--add-dir','/tmp/a','--strict-mcp-config'])
        self.assertNotIn('original task', result)
        self.assertNotIn('old-id', result)
        self.assertIn(sid,result)

    def test_reset_time_is_remembered_when_statusline_omits_five_hour(self):
        now = m.datetime.datetime(2026,10,7,17,15,tzinfo=m.datetime.timezone.utc).timestamp()
        event = {'error':'rate_limit','message':"You've hit your session limit · resets 9:40pm (UTC)"}
        with mock.patch.object(m.time,'time',return_value=now):
            expected = m.datetime.datetime(2026,10,7,21,40,tzinfo=m.datetime.timezone.utc).timestamp()
            self.assertEqual(m.quota_reset_from_error(event, {'at':now,'limits':{'seven_day':{'used_percentage':13}}}), expected)
            self.assertEqual(m.quota_reset_from_error({**event,'message':"You've hit your session limit · resets 9:40pm (unknown-zone)"}, {}),0)

    def test_system_zone_without_tz_database_is_local_time(self):
        # Claude Code on Windows names the system zone; Python there may lack tzdata.
        now = m.datetime.datetime(2026,10,7,17,15).timestamp()
        event = {'error':'rate_limit','message':"You've hit your session limit · resets 9:40pm (Asia/Singapore)"}
        missing = m.oslayer.zoneinfo.ZoneInfoNotFoundError('No time zone found with key Asia/Singapore')
        with mock.patch.object(m.time,'time',return_value=now), \
                mock.patch.object(m.oslayer.zoneinfo,'ZoneInfo',side_effect=missing):
            self.assertEqual(m.quota_reset_from_error(event,{}),m.datetime.datetime(2026,10,7,21,40).timestamp())

    def test_reset_time_crosses_midnight(self):
        now = m.datetime.datetime(2026,10,7,23,15,tzinfo=m.datetime.timezone.utc).timestamp()
        with mock.patch.object(m.time,'time',return_value=now):
            result=m.quota_reset_from_error({'message':"You've hit your session limit · resets 2:40am (UTC)"},{})
            self.assertEqual(result,m.datetime.datetime(2026,10,8,2,40,tzinfo=m.datetime.timezone.utc).timestamp())
        with self.assertRaises(RuntimeError): m.persistent_arguments(['--unknown-option'])

    def test_new_profile_no_credentials_copy(self):
        previous = m.ROOT
        with tempfile.TemporaryDirectory() as temp:
            m.ROOT = Path(temp)/'registry'
            m.private_dir(m.ROOT)
            base=Path(temp)/'main'; base.mkdir()
            (base/'.credentials.json').write_text('TEST_SECRET_NOT_TO_COPY')
            (base/'settings.json').write_text('{}')
            registry={'version':1,'claude':'/fake','profiles':{'main':str(base)},'default':'main'}
            path=m.add_profile(registry,'second')
            self.assertFalse((path/'.credentials.json').exists())
            self.assertTrue((path/'projects').is_symlink())
            self.assertEqual((path/'settings.json').resolve(),(base/'settings.json').resolve())
            with self.assertRaises(RuntimeError): m.add_profile(registry,'../escape')
        m.ROOT=previous

    def test_prepare_many_profiles_preserves_existing(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(m, 'ROOT', Path(temp)/'registry'):
            base=Path(temp)/'main'; base.mkdir()
            (base/'.credentials.json').write_text('TEST_SECRET_NOT_TO_COPY')
            (base/'settings.json').write_text('{}')
            registry={'version':1,'claude':'/fake','profiles':{'main':str(base)},'default':'main'}
            m.add_profile(registry,'second')
            names=m.prepare_profiles(registry,12)
            self.assertEqual(names,['main','second']+[f'account{i:02d}' for i in range(3,13)])
            self.assertEqual(m.prepare_profiles(registry,12),names)
            self.assertEqual((base/'.credentials.json').read_text(encoding='utf-8'),'TEST_SECRET_NOT_TO_COPY')
            for name in names[1:]:
                profile=Path(registry['profiles'][name])
                self.assertFalse((profile/'.credentials.json').exists())
                self.assertEqual((profile/'settings.json').resolve(),(base/'settings.json').resolve())
            with self.assertRaises(RuntimeError): m.prepare_profiles(registry,5)
            self.assertEqual(len(registry['profiles']),12)

    def test_batch_login_skips_completed_and_can_resume(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(m, 'ROOT', Path(temp)/'registry'):
            base=Path(temp)/'main'; base.mkdir()
            registry={'version':1,'claude':'/fake','profiles':{'main':str(base)},'default':'main'}
            m.add_profile(registry,'second')
            names=m.prepare_profiles(registry,4)
            def signed_in(name):
                return {'loggedIn':True,'authMethod':'claude.ai','email':name+'@example.test'}
            statuses={name:{'loggedIn':False} for name in names}
            statuses.update(main=signed_in('main'),account04=signed_in('account04'))
            calls=[]
            def login(command,env):
                self.assertEqual(command,['/fake','auth','login','--claudeai'])
                name=Path(env['CLAUDE_CONFIG_DIR']).name
                calls.append(name)
                if name=='account03' and calls.count(name)==1:
                    return subprocess.CompletedProcess(command,1)
                statuses[name]=signed_in(name)
                return subprocess.CompletedProcess(command,0)
            with mock.patch.object(m,'auth_status',side_effect=lambda registry,name:statuses[name]), \
                 mock.patch.object(m.subprocess,'run',side_effect=login), mock.patch.object(m,'say'):
                self.assertEqual(m.login_profiles(registry,names,skip_existing=True),1)
                self.assertTrue(m.eligible(statuses['second']))
                self.assertEqual(m.login_profiles(registry,names,skip_existing=True),0)
            self.assertEqual(calls,['second','account03','account03'])

    def test_batch_login_rejects_duplicate_identity(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(m, 'ROOT', Path(temp)/'registry'):
            base=Path(temp)/'main'; base.mkdir()
            registry={'version':1,'claude':'/fake','profiles':{'main':str(base)},'default':'main'}
            names=m.prepare_profiles(registry,3)
            statuses={name:{'loggedIn':False} for name in names}
            statuses['main']={'loggedIn':True,'authMethod':'claude.ai','email':'Main@Example.test'}
            def login(command,env):
                name=Path(env['CLAUDE_CONFIG_DIR']).name
                statuses[name]={'loggedIn':True,'authMethod':'claude.ai','email':' main@example.test '}
                return subprocess.CompletedProcess(command,0)
            with mock.patch.object(m,'auth_status',side_effect=lambda registry,name:statuses[name]), \
                 mock.patch.object(m.subprocess,'run',side_effect=login) as run, mock.patch.object(m,'say'):
                with self.assertRaisesRegex(RuntimeError,'同一账号'):
                    m.login_profiles(registry,names,skip_existing=True)
                self.assertEqual(run.call_count,1)
            self.assertFalse(m.eligible(statuses['account03']))

    def test_quota_merge_retains_reset_until_a_new_window(self):
        old={'blocked_until':200,'recorded_at':100}
        unknown={'blocked_until':0,'recorded_at':150}
        self.assertEqual(m.merge_quota_record(old,unknown)['blocked_until'],200)
        self.assertEqual(m.merge_quota_record(unknown,old)['blocked_until'],200)
        after_reset={'blocked_until':0,'recorded_at':250}
        self.assertEqual(m.merge_quota_record(old,after_reset)['blocked_until'],0)

    def test_concurrent_quota_writes_and_legacy_writer_do_not_lose_updates(self):
        with tempfile.TemporaryDirectory() as temp:
            state=Path(temp)
            now=time.time()
            m.write_json(state/'quota.json',{'legacy':{'blocked_until':now+900,'recorded_at':now}})
            writes=[(f'account{i:02d}',now+600+i) for i in range(6)]
            writes += [('shared',now+1200+i*60) for i in range(4)]
            context=multiprocessing.get_context('spawn' if WINDOWS else 'fork')
            barrier=context.Barrier(len(writes))
            stages=[Path(temp)/f'stage{i}' for i in range(len(writes))]
            # Windows spawns a fresh interpreter per worker; a busy CI machine needs time.
            processes=[context.Process(target=write_quota_worker,args=(str(state),name,reset,barrier,str(stage)))
                       for (name,reset),stage in zip(writes,stages)]
            try:
                started=time.monotonic()
                for process in processes: process.start()
                for process in processes: process.join(timeout=max(0,started+60-time.monotonic()))
                progress=[(process.exitcode,stage.read_text() if stage.exists() else 'starting')
                          for process,stage in zip(processes,stages)]
                self.assertEqual([code for code,_ in progress],[0]*len(processes),
                                 f'{time.monotonic()-started:.1f}s, {barrier.n_waiting} at the barrier: {progress}')
            finally:
                for process in processes:
                    if process.is_alive(): process.terminate(); process.join(timeout=3)
            # Simulate the old, still-running launcher replacing its aggregate file.
            m.write_json(state/'quota.json',{'oldsession':{'blocked_until':now+300,'recorded_at':time.time()}})
            quota=m.read_quota_state(state)
            for name,reset in writes[:6]: self.assertEqual(quota[name]['blocked_until'],reset)
            self.assertEqual(quota['shared']['blocked_until'],max(reset for name,reset in writes if name=='shared'))
            self.assertIn('oldsession',quota)

    def run_fake(self, scenario, names=('main','second'), blocked_profiles=(), initial='main', disabled_profiles=()):
        if WINDOWS:
            self.skipTest('POSIX PTY fixture; tests/test_windows.py rotates through ConPTY')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            binary=root/'fake-claude'; binary.write_text(FAKE); binary.chmod(0o700)
            for name in names:
                (root/name).mkdir(); (root/name/'settings.json').write_text('{}')
            registry={'version':1,'claude':str(binary),'profiles':{n:str(root/n) for n in names},'default':'main'}
            registry['disabled']={n:{'reason':'user disabled'} for n in disabled_profiles}
            (root/'accounts.json').write_text(json.dumps(registry))
            if blocked_profiles:
                m.write_json(root/'state/quota.json',{
                    name:{'blocked_until':time.time()+600,'recorded_at':time.time()}
                    for name in blocked_profiles})
            env=os.environ.copy()
            for key in m.AUTH_ENV: env.pop(key,None)
            (root/'isolated-home').mkdir()
            env.update(CLAUDE_ACCOUNTS_HOME=str(root),TEST_SCENARIO=scenario,
                       TEST_SESSION=str(uuid.uuid4()),TEST_TRACE=str(root/'trace.jsonl'),
                       HOME=str(root/'isolated-home'),TEST_LAST_ACCOUNT=names[-1])
            launcher=MODULE_PATH
            if scenario.startswith('login-retry-'):
                launcher=root/'test-launcher.py'
                launcher.write_text(MODULE_PATH.read_text(encoding='utf-8').replace('LOGIN_RETRY_DELAYS = (60, 120)', 'LOGIN_RETRY_DELAYS = (0.3, 0.4)'))
                env['PYTHONPATH']=str(REPO_ROOT/'lib')
            if scenario == 'stale-background':
                project=root/'main/projects/fake-project'; project.mkdir(parents=True)
                watch=project/'watch.py'; watch.write_text('pass\n')
                m.write_json(root/'persistent-watchers.json',[{
                    'cwd':str(project),'argv':['/usr/bin/python3',str(watch)],
                    'script_sha256':m.file_hash(watch),'service':'claude-test-watcher.service','log':str(root/'watch.log')}])
                mockbin=root/'mockbin'; mockbin.mkdir()
                systemctl=mockbin/'systemctl'; systemctl.write_text('#!/bin/sh\nexit 0\n'); systemctl.chmod(0o700)
                env['PATH']=str(mockbin)+os.pathsep+env['PATH']
            pid,fd=pty.fork()
            if pid==0:
                account_args=['--account',initial] if initial is not None else []
                os.execvpe(sys.executable,[sys.executable,str(launcher),'run',*account_args,'--','--tools','Read','original task'],env)
            output=bytearray(); status=None; deadline=time.monotonic()+30
            typed=False
            try:
                while time.monotonic()<deadline:
                    waited,code=os.waitpid(pid,os.WNOHANG)
                    if waited:
                        status=os.waitstatus_to_exitcode(code); break
                    if select.select([fd],[],[],.1)[0]:
                        try: output.extend(os.read(fd,65536))
                        except OSError: pass
                    if scenario == 'login-retry-keyboard' and not typed and b'NATIVE_REFRESH_BUSY' in output:
                        os.write(fd,b'x')  # A draft, without Enter, must cancel too.
                        typed=True
                if status is None:
                    os.kill(pid,9); os.waitpid(pid,0)
                    self.fail('PTY integration timed out: '+output.decode(errors='replace'))
            finally:
                os.close(fd)
                import shutil
                scratch=Path(tempfile.gettempdir())/('claude-'+str(os.getuid()))/'rotation-test'/env['TEST_SESSION']
                if scratch.exists(): shutil.rmtree(scratch)
            trace_path=root/'trace.jsonl'
            trace=[json.loads(line) for line in trace_path.read_text(encoding='utf-8').splitlines()] if trace_path.exists() else []
            return status,trace,output.decode(errors='replace')

    def test_pty_archives_scratch_links_then_resumes(self):
        status,trace,output=self.run_fake('scratch-links')
        self.assertEqual(status,0,output)
        self.assertEqual([x['account'] for x in trace],['main','second'])
        self.assertIn('RESUME_OK',output)

    def test_pty_reconciles_native_terminal_receipts_and_excludes_scratch_credentials(self):
        status,trace,output=self.run_fake('native-receipts')
        self.assertEqual(status,0,output)
        self.assertEqual([x['account'] for x in trace],['main','second'])
        self.assertIn('RESUME_OK',output)

    def test_disabled_default_is_skipped_without_launching_main(self):
        status,trace,output=self.run_fake('switch',names=('main','second','account03'),initial=None,disabled_profiles=('main',))
        self.assertEqual(status,0,output)
        self.assertEqual([x['account'] for x in trace],['second','account03'])

    def test_explicit_disabled_account_is_refused_before_launch(self):
        status,trace,output=self.run_fake('startup',disabled_profiles=('main',))
        self.assertNotEqual(status,0,output)
        self.assertEqual(trace,[])
        self.assertIn('已停用',output)

    def test_newly_disabled_next_account_is_skipped_by_live_controller(self):
        status,trace,output=self.run_fake('disable-next',names=('main','second','account03'))
        self.assertEqual(status,0,output)
        self.assertEqual([x['account'] for x in trace],['main','account03'])

    def test_official_ban_is_not_retried_on_a_different_account(self):
        status,trace,output=self.run_fake('banned')
        self.assertEqual(status,0,output)
        self.assertEqual([x['account'] for x in trace],['main'])
        self.assertIn('已停用',output)

    def test_account_ban_is_not_quota_and_generic_auth_error_is_not_a_ban(self):
        for error,message in [('rate_limit','Your account has been disabled.'),('account_on_hold',''),
                              ('authentication_failed','This organization has been suspended.')]:
            event={'error':error,'message':message}
            self.assertTrue(m.account_unavailable(event))
            self.assertFalse(m.quota_failure(event,{'at':time.time(),'limits':{'five_hour':{'used_percentage':100,'resets_at':time.time()+60}}}))
        for message in ['401 Unauthorized','API Error: No response from API','429 Too many requests',"You've hit your session limit"]:
            self.assertFalse(m.account_unavailable({'error':'server_error','message':message}))

    def test_pty_switch_and_resume(self):
        status,trace,output=self.run_fake('switch')
        self.assertEqual(status,0,output)
        self.assertEqual([x['account'] for x in trace],['main','second'])
        self.assertIn('RESUME_OK',output)

    def test_all_exhausted_stops(self):
        names=('main','second','account03','account04','account05')
        status,trace,output=self.run_fake('exhausted',names)
        self.assertEqual(status,75,output)
        self.assertEqual([x['account'] for x in trace],list(names))

    def test_five_accounts_switch_and_resume(self):
        names=('main','second','account03','account04','account05')
        status,trace,output=self.run_fake('switch',names)
        self.assertEqual(status,0,output)
        self.assertEqual([x['account'] for x in trace],list(names))
        self.assertIn('RESUME_OK',output)

    def test_account03_rotates_to_04_not_back_to_available_main(self):
        names=('main','second','account03','account04','account05')
        status,trace,output=self.run_fake('switch',names,initial='account03')
        self.assertEqual(status,0,output)
        self.assertEqual([x['account'] for x in trace],['account03','account04','account05'])

    def test_ring_wraps_only_after_last_account(self):
        names=['main','second','account03','account04','account05']
        self.assertEqual(m.account_ring(names,'account03',after=True),['account04','account05','main','second','account03'])
        self.assertEqual(m.account_ring(names,'account05',after=True),names)

    def test_ordered_rotation_skips_known_exhausted_next_account(self):
        names=('main','second','account03','account04','account05')
        status,trace,output=self.run_fake('switch',names,blocked_profiles=('account04',),initial='account03')
        self.assertEqual(status,0,output)
        self.assertEqual([x['account'] for x in trace],['account03','account05'])

    def test_safety_and_transient_do_not_rotate(self):
        for scenario in ('safety','transient'):
            with self.subTest(scenario=scenario):
                status,trace,output=self.run_fake(scenario)
                self.assertEqual(status,0,output)
                self.assertEqual(len(trace),1)

    def test_waits_for_subagent_flush_then_resumes_original_ids(self):
        status,trace,output=self.run_fake('subagents')
        self.assertEqual(status,0,output)
        self.assertEqual([x['account'] for x in trace],['main','second'])
        self.assertIn('RESUME_OK',output)

    def test_child_only_quota_rotates_without_waiting_for_parent_error(self):
        status,trace,output=self.run_fake('child-only')
        self.assertEqual(status,0,output)
        self.assertEqual([x['account'] for x in trace],['main','second'])
        self.assertIn('RESUME_OK',output)

    def test_stale_failed_agent_and_external_watcher_do_not_deadlock_real_pty(self):
        status,trace,output=self.run_fake('stale-background')
        self.assertEqual(status,0,output)
        self.assertEqual([x['account'] for x in trace],['main','second'])
        self.assertIn('RESUME_OK',output)

    def test_missing_history_stops_without_switching(self):
        status,trace,output=self.run_fake('missing-history')
        self.assertEqual(status,75,output)
        self.assertEqual(len(trace),1)

    def test_startup_skips_known_exhausted_account(self):
        status,trace,output=self.run_fake('startup',blocked_profiles=('main',))
        self.assertEqual(status,0,output)
        self.assertEqual([x['account'] for x in trace],['second'])

    @unittest.skipIf(WINDOWS, 'POSIX PTY fixture')
    def test_three_controllers_run_and_resume_independently_with_old_lock_held(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            binary=root/'fake-claude'; binary.write_text(FAKE); binary.chmod(0o700)
            names=('main','second')
            for name in names:
                (root/name).mkdir(); (root/name/'settings.json').write_text('{}')
            registry={'version':1,'claude':str(binary),'profiles':{n:str(root/n) for n in names},'default':'main'}
            (root/'accounts.json').write_text(json.dumps(registry))
            (root/'isolated-home').mkdir()
            (root/'barrier').mkdir()
            (root/'state').mkdir()
            children=[]
            with (root/'state/controller.lock').open('a+') as old_lock:
                fcntl.flock(old_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                try:
                    for index in range(3):
                        sid=str(uuid.uuid4())
                        env=os.environ.copy()
                        for key in m.AUTH_ENV: env.pop(key,None)
                        env.update(CLAUDE_ACCOUNTS_HOME=str(root),TEST_SCENARIO='concurrent',
                                   TEST_SESSION=sid,TEST_TRACE=str(root/f'trace-{index}.jsonl'),
                                   HOME=str(root/'isolated-home'),TEST_LAST_ACCOUNT='second',
                                   TEST_BARRIER=str(root/'barrier'),TEST_CONCURRENCY='3')
                        pid,fd=pty.fork()
                        if pid==0:
                            os.execvpe(sys.executable,[sys.executable,str(MODULE_PATH),'run','--',
                                       '--tools','Read','--effort','max',f'task {index}'],env)
                        children.append({'pid':pid,'fd':fd,'sid':sid,'index':index,
                                         'output':bytearray(),'status':None})
                    deadline=time.monotonic()+20
                    while any(child['status'] is None for child in children) and time.monotonic()<deadline:
                        active=[child for child in children if child['status'] is None]
                        ready,_,_=select.select([child['fd'] for child in active],[],[],.1)
                        for child in active:
                            if child['fd'] in ready:
                                try: child['output'].extend(os.read(child['fd'],65536))
                                except OSError: pass
                            waited,code=os.waitpid(child['pid'],os.WNOHANG)
                            if waited: child['status']=os.waitstatus_to_exitcode(code)
                    for child in children:
                        self.assertEqual(child['status'],0,child['output'].decode(errors='replace'))
                        trace=[json.loads(line) for line in (root/f"trace-{child['index']}.jsonl").read_text(encoding='utf-8').splitlines()]
                        self.assertEqual([item['account'] for item in trace],['main','second'])
                        self.assertTrue(all(item['session_id']==child['sid'] for item in trace))
                        resumed=trace[1]['args']
                        self.assertEqual(resumed[resumed.index('--resume')+1],child['sid'])
                        self.assertEqual(resumed[resumed.index('--effort')+1],'max')
                    self.assertEqual(len(list((root/'state/runs').iterdir())),3)
                finally:
                    for child in children:
                        if child['status'] is None:
                            os.kill(child['pid'],15)
                            deadline=time.monotonic()+6
                            while time.monotonic()<deadline:
                                waited,_=os.waitpid(child['pid'],os.WNOHANG)
                                if waited: break
                                time.sleep(.05)
                            else: os.kill(child['pid'],9); os.waitpid(child['pid'],0)
                        os.close(child['fd'])


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # Manifests record resolved paths; Windows TEMP may be an 8.3 alias such as RUNNER~1.
        self.root = Path(self.temp.name).resolve()
        self.previous_root = m.ROOT
        m.ROOT = self.root/'accounts'
        self.profile = self.root/'main'
        self.sid = str(uuid.uuid4())
        self.project = self.profile/'projects'/'test-project'
        self.project.mkdir(parents=True)
        self.transcript = self.project/(self.sid+'.jsonl')
        self.original = (json.dumps({'type':'user','message':{'content':'完整原文，不要用摘要替换'}})+'\n').encode()
        self.transcript.write_bytes(self.original)
        self.subdir = self.project/self.sid/'subagents'
        self.subdir.mkdir(parents=True)
        self.agent = self.subdir/'agent-original.jsonl'
        self.agent.write_text(json.dumps({'type':'assistant','message':{'content':'original agent context'}})+'\n')
        self.agent.with_suffix('.meta.json').write_text(json.dumps({'agentType':'general-purpose','toolUseId':'tool-original'}))
        self.registry = {'profiles':{'main':str(self.profile)}}
        self.run = self.root/'run'
        m.private_dir(self.run)
        m.write_json(self.run/'launch.json',{'resume_options':['--tools','Read,SendMessage','--permission-mode','plan']})
        self.state = {'session_id':self.sid,'transcript_path':str(self.transcript),'cwd':str(self.project),
                      'agents':{'original':{'state':'failed','agent_type':'general-purpose','agent_transcript_path':str(self.agent)}},
                      'pending_tools':{},'scratchpads':[]}

    def tearDown(self):
        m.ROOT = self.previous_root
        self.temp.cleanup()

    def payload(self, kind, **extra):
        return {'session_id':self.sid,'transcript_path':str(self.transcript),'cwd':str(self.project),
                'hook_event_name':kind,**extra}

    def checkpoint(self):
        return m.checkpoint_context(self.registry,self.state,self.run,'test')

    def scratch(self):
        import shutil
        scratch=m.oslayer.claude_temp_root()/'rotation-test'/self.sid/'scratchpad'
        scratch.mkdir(parents=True)
        self.addCleanup(shutil.rmtree,scratch.parent)
        # Claude Code reports the scratchpad under TEMP as given; the manifest resolves it.
        self.state['scratchpads']=[str(scratch)]
        return scratch.resolve()

    def test_scratch_links_preserved_without_reading_external_targets(self):
        scratch=self.scratch()
        secret=self.root/'external';secret.mkdir();(secret/'.credentials.json').write_text('must-not-be-read')
        (scratch/'assets').symlink_to(secret,target_is_directory=True)
        (scratch/'missing.jsonl').symlink_to('../not-present')
        (scratch/'circular').symlink_to(scratch)
        path=self.checkpoint();manifest=m.verify_checkpoint(path,live=True)
        links=[x for x in manifest['files'] if x.get('kind')=='symlink']
        self.assertEqual(len(links),3)
        self.assertFalse(any('.credentials.json' in x['source'] for x in manifest['files']))
        for item in links:
            self.assertEqual((path.parents[2]/'objects'/item['sha256']).read_bytes(),os.fsencode(item['link_target']))
            Path(item['source']).unlink()
        self.assertEqual(m.restore_missing_context(path,self.registry),3)
        self.assertEqual(os.readlink(scratch/'missing.jsonl'),'../not-present')
        self.assertEqual(m.restore_missing_context(path,self.registry),0)
        self.assertEqual((secret/'.credentials.json').read_text(encoding='utf-8'),'must-not-be-read')

    def test_changed_scratch_link_blocks_recovery_without_overwrite(self):
        scratch=self.scratch();link=scratch/'assets';link.symlink_to(self.project)
        path=self.checkpoint();link.unlink();link.symlink_to(self.root)
        with self.assertRaises(m.ContextNotReady):m.verify_checkpoint(path,live=True)
        with self.assertRaises(m.ContextNotReady):m.restore_missing_context(path,self.registry)
        self.assertTrue(link.is_symlink() and os.path.samefile(link,self.root))

    def test_scratch_credentials_are_explicitly_excluded_without_reading_or_restoring(self):
        scratch=self.scratch();credential=scratch/'.credentials.json';credential.write_text('test-auth-state')
        (scratch/'notes.txt').write_text('work history')
        original=Path.open
        def guarded(path,*args,**kwargs):
            if path==credential:raise AssertionError('must not open credential file')
            return original(path,*args,**kwargs)
        with mock.patch.object(Path,'open',guarded):path=self.checkpoint()
        manifest=m.verify_checkpoint(path,live=True)
        self.assertEqual(manifest['excluded_files'],[{'source':str(credential),'reason':'credential-file-not-read'}])
        self.assertNotIn(str(credential),[item['source'] for item in manifest['files']])
        credential.unlink()
        self.assertEqual(m.restore_missing_context(path,self.registry),0)
        self.assertFalse(credential.exists())

    def test_credentials_in_session_history_still_fail_closed(self):
        (self.subdir/'.credentials.json').write_text('never read')
        with self.assertRaises(m.ContextNotReady):self.checkpoint()

    def test_old_malformed_main_row_is_archived_byte_for_byte_with_diagnostics(self):
        raw=self.original+b'{"old damaged row": invalid}\n'+b'{"type":"assistant","message":{"content":"native session continued"}}\n'
        self.transcript.write_bytes(raw)
        path=self.checkpoint();manifest=m.verify_checkpoint(path,live=True)
        item=next(item for item in manifest['files'] if item['role']=='main')
        self.assertEqual(item['historical_json_errors'],1)
        self.assertEqual(item['historical_json_error_samples'][0]['line'],2)
        self.assertEqual((path.parents[2]/'objects'/item['sha256']).read_bytes(),raw)
        self.transcript.unlink()
        self.assertEqual(m.restore_missing_context(path,self.registry),1)
        self.assertEqual(self.transcript.read_bytes(),raw)

    def test_damaged_final_main_record_still_blocks_checkpoint(self):
        self.transcript.write_bytes(self.original+b'{"not finished": invalid}\n')
        with self.assertRaises(m.ContextNotReady):self.checkpoint()

    def test_generated_builds_and_environments_are_recorded_but_source_work_is_archived(self):
        scratch=self.scratch();build=scratch/'check-dist';build.mkdir()
        (build/'index.html').write_text('<script type="module" crossorigin src="/assets/index-AbcDef12.js"></script>')
        (build/'large.bin').write_bytes(b'generated output remains on disk')
        env=scratch/'imgenv';env.mkdir();(env/'pyvenv.cfg').write_text('home = /usr/bin')
        (env/'library.py').write_text('generated dependency')
        handwritten=scratch/'hand-dist';handwritten.mkdir();(handwritten/'index.html').write_text('hand written work')
        (scratch/'build.sh').write_text('rebuild command')
        manifest=m.verify_checkpoint(self.checkpoint(),live=True)
        excluded={Path(item['source']).name:item['reason'] for item in manifest['excluded_files']}
        self.assertEqual(excluded,{'check-dist':'generated-vite-build-output','imgenv':'python-virtual-environment'})
        sources={item['source'] for item in manifest['files']}
        self.assertIn(str(handwritten/'index.html'),sources)
        self.assertIn(str(scratch/'build.sh'),sources)
        self.assertNotIn(str(build/'large.bin'),sources)
        self.assertTrue((build/'large.bin').exists())

    def append_native_row(self, row):
        row.setdefault('sessionId',self.sid)
        with self.transcript.open('a') as stream:stream.write(json.dumps(row)+'\n')

    def test_native_taskstop_receipt_survives_old_cursor_and_stale_snapshot(self):
        now=time.time();stamp=lambda t:m.datetime.datetime.fromtimestamp(t,m.datetime.timezone.utc).isoformat()
        self.state['background_tasks']=[{'id':'server','type':'shell','status':'running'}]
        self.state['background_tasks_at']=now
        self.append_native_row({'type':'user','timestamp':stamp(now-10),'toolUseResult':{'backgroundTaskId':'server'},
                                'message':{'content':[{'type':'tool_result','tool_use_id':'launch','content':'started'}]}})
        self.append_native_row({'type':'user','timestamp':stamp(now-5),
                                'toolUseResult':{'task_id':'server','task_type':'local_bash','message':'Successfully stopped task: server (node server.mjs)'},
                                'message':{'content':[{'type':'tool_result','tool_use_id':'stop','content':'stopped'}]}})
        self.state['receipt_cursors']={str(self.transcript):{'inode':self.transcript.stat().st_ino,'offset':self.transcript.stat().st_size}}
        self.assertEqual(m.rotation_busy(self.state)[2],[])
        self.assertEqual(self.state['background_tasks'][0]['status'],'stopped')
        self.assertEqual(self.state['receipt_schema'],2)

    def test_native_queue_and_attachment_receipts_survive_later_stale_snapshot(self):
        now=time.time();stamp=lambda t:m.datetime.datetime.fromtimestamp(t,m.datetime.timezone.utc).isoformat()
        self.state['background_tasks']=[{'id':name,'type':'shell','status':'running'} for name in ('queue','attachment','untrusted')]
        self.state['background_tasks_at']=now
        self.state['task_starts']={name:now-10 for name in ('queue','attachment','untrusted')}
        notice=lambda name:'<task-notification><task-id>'+name+'</task-id><status>completed</status></task-notification>'
        self.append_native_row({'type':'queue-operation','operation':'enqueue','timestamp':stamp(now-5),'content':notice('queue')})
        self.append_native_row({'type':'attachment','timestamp':stamp(now-4),
                                'attachment':{'type':'queued_command','commandMode':'task-notification','origin':{'kind':'task-notification'},'prompt':notice('attachment'),'timestamp':stamp(now-6)}})
        self.append_native_row({'type':'user','timestamp':stamp(now-3),'message':{'content':notice('untrusted')}})
        self.assertEqual([x['id'] for x in m.rotation_busy(self.state)[2]],['untrusted'])

    def test_older_completion_cannot_finish_new_launch_with_same_task_id(self):
        now=time.time();stamp=lambda t:m.datetime.datetime.fromtimestamp(t,m.datetime.timezone.utc).isoformat()
        self.state['background_tasks']=[{'id':'job','type':'shell','status':'running'}]
        self.state['task_starts']={'job':now}
        self.append_native_row({'type':'queue-operation','operation':'enqueue','timestamp':stamp(now-5),
                                'content':'<task-notification><task-id>job</task-id><status>completed</status></task-notification>'})
        self.assertEqual(len(m.rotation_busy(self.state)[2]),1)

    def test_failed_taskstop_hook_does_not_mark_a_running_task_finished(self):
        self.state['background_tasks']=[{'id':'job','type':'shell','status':'running'}]
        m.write_json(m.session_state_path(self.run,self.sid),self.state)
        response={'task_id':'job','task_type':'local_bash','message':'Successfully stopped task: job (node server.mjs)'}
        m.lifecycle_record(self.run,'main',self.payload('PostToolUseFailure',tool_name='TaskStop',tool_input={'task_id':'job'},tool_response=response))
        self.assertEqual(len(m.rotation_busy(m.read_json(m.session_state_path(self.run,self.sid)))[2]),1)
        m.lifecycle_record(self.run,'main',self.payload('PostToolUse',tool_name='TaskStop',tool_input={'task_id':'job'},tool_response=response))
        self.assertEqual(m.rotation_busy(m.read_json(m.session_state_path(self.run,self.sid)))[2],[])

    def test_recovery_refuses_link_parent_redirection(self):
        scratch=self.scratch();nested=scratch/'nested';nested.mkdir();link=nested/'assets';link.symlink_to(self.project)
        path=self.checkpoint();link.unlink();nested.rmdir();outside=self.root/'outside';outside.mkdir();nested.symlink_to(outside)
        with self.assertRaises(m.ContextNotReady):m.restore_missing_context(path,self.registry)
        self.assertEqual(list(outside.iterdir()),[])

    def test_disable_blocks_auth_even_with_stale_registry_and_enable_is_explicit(self):
        registry={'version':1,'claude':sys.executable,'profiles':{'main':str(self.profile),'second':str(self.root/'second')},'default':'main'}
        m.write_json(m.ROOT/'accounts.json',registry)
        m.set_profile_disabled('main','user confirmed banned')
        with mock.patch.object(m.subprocess,'run',side_effect=AssertionError('disabled account must not be probed')):
            self.assertTrue(m.auth_status(registry,'main')['disabled'])
        with self.assertRaises(RuntimeError):m.profile_env(registry,'main')
        self.assertEqual(m.read_quota_state(m.ROOT/'state')['main']['blocked_until'],m.DISABLED_UNTIL)
        self.assertEqual(m.load_registry()['default'],'second')
        m.record_quota(m.ROOT/'state','main',0)
        self.assertEqual(m.read_quota_state(m.ROOT/'state')['main']['blocked_until'],m.DISABLED_UNTIL)
        m.set_profile_disabled('main')
        self.assertIsNone(m.disabled_reason(registry,'main'))
        self.assertNotIn('main',m.read_quota_state(m.ROOT/'state'))

    def test_full_history_metadata_receipts_and_task_files_are_archived(self):
        task = self.profile/'tasks'/self.sid/'1.json'
        task.parent.mkdir(parents=True)
        task.write_text('{"status":"in_progress"}')
        receipt='未写进 transcript 的工具回执：'+'x'*12000
        m.lifecycle_record(self.run,'main',self.payload('PostToolUse',tool_use_id='a',tool_response={'output':receipt}))
        checkpoint = self.checkpoint()
        manifest = m.verify_checkpoint(checkpoint,live=True)
        by_path={item['source']:item for item in manifest['files']}
        self.assertIn(str(task),by_path)
        self.assertIn(str(self.agent.with_suffix('.meta.json')),by_path)
        archive=checkpoint.parents[2]/'objects'
        self.assertEqual((archive/by_path[str(self.transcript)]['sha256']).read_bytes(),self.original)
        journal=next(item for item in manifest['files'] if item['role']=='hook-journal')
        self.assertIn(receipt,(archive/journal['sha256']).read_text(encoding='utf-8'))
        self.assertEqual(manifest['resume_options'],['--tools','Read,SendMessage','--permission-mode','plan'])
        if not WINDOWS:
            self.assertTrue(all((archive/item['sha256']).stat().st_mode & 0o077 == 0 for item in manifest['files']))

    def test_append_is_allowed_but_truncation_is_rejected(self):
        checkpoint=self.checkpoint()
        with self.transcript.open('ab') as stream: stream.write(b'{"type":"system"}\n')
        m.verify_checkpoint(checkpoint,live=True)
        self.transcript.write_text('{}\n')
        with self.assertRaises(m.ContextNotReady): m.verify_checkpoint(checkpoint,live=True)
        m.verify_checkpoint(checkpoint,live=False)

    def test_failed_agent_overrides_stale_running_background_snapshot(self):
        self.state['background_tasks'] = [{'id':'original','type':'subagent','status':'running'}]
        self.assertEqual(m.rotation_busy(self.state), ([], [], []))
        self.assertEqual(self.state['background_tasks'][0]['status'], 'failed')

    def test_completed_tool_receipt_clears_missing_post_hook(self):
        self.state['pending_tools'] = {'denied-tool':{'tool_name':'SendMessage'}, 'still-running':{'tool_name':'Bash'}}
        with self.transcript.open('a') as stream:
            stream.write(json.dumps({'type':'user','message':{'content':[
                {'type':'tool_result','tool_use_id':'denied-tool','is_error':True,'content':'Permission check unavailable'}]}})+'\n')
        m.reconcile_background(self.state)
        self.assertEqual(list(self.state['pending_tools']), ['still-running'])
        self.assertEqual(m.rotation_busy(self.state)[1], ['still-running'])

    def test_native_notification_finishes_shell_but_user_text_does_not(self):
        self.state['background_tasks'] = [{'id':'job','type':'shell','status':'running','command':'render'}]
        self.state['background_tasks_at'] = time.time()-5
        row={'type':'user','timestamp':m.datetime.datetime.now(m.datetime.timezone.utc).isoformat(),
             'message':{'content':'<task-notification><task-id>job</task-id><status>completed</status></task-notification>'}}
        with self.transcript.open('a') as stream: stream.write(json.dumps(row)+'\n')
        self.assertEqual(len(m.rotation_busy(self.state)[2]), 1)
        row['origin']={'kind':'task-notification'}
        with self.transcript.open('a') as stream: stream.write(json.dumps(row)+'\n')
        self.assertEqual(m.rotation_busy(self.state)[2], [])

    def test_prior_notification_does_not_finish_resumed_task(self):
        self.state['background_tasks']=[{'id':'job','type':'shell','status':'running','command':'render'}]
        self.state['background_tasks_at']=time.time()
        row={'type':'user','origin':{'kind':'task-notification'},'timestamp':'2025-01-01T00:00:00Z',
             'message':{'content':'<task-notification><task-id>job</task-id><status>completed</status></task-notification>'}}
        with self.transcript.open('a') as stream: stream.write(json.dumps(row)+'\n')
        self.assertEqual(len(m.rotation_busy(self.state)[2]),1)

    def test_new_native_session_retires_old_background_registry(self):
        self.state.update(account='main', ended_at=time.time(), background_tasks=[{'id':'old','type':'shell','status':'running'}])
        m.write_json(m.session_state_path(self.run,self.sid),self.state)
        m.lifecycle_record(self.run,'second',self.payload('SessionStart'))
        state=m.read_json(m.session_state_path(self.run,self.sid))
        self.assertFalse(state.get('background_tasks'))
        self.assertEqual(state['prior_background_tasks'][0]['id'],'old')
        self.assertNotIn('ended_at',state)

    def test_same_process_resume_keeps_actual_background_jobs(self):
        self.state.update(account='main',background_tasks=[{'id':'job','type':'shell','status':'running','command':'render'}])
        m.write_json(m.session_state_path(self.run,self.sid),self.state)
        m.lifecycle_record(self.run,'main',self.payload('SessionStart'))
        self.assertEqual(len(m.rotation_busy(m.read_json(m.session_state_path(self.run,self.sid)))[2]),1)

    def test_only_hash_pinned_active_external_watcher_is_nonblocking(self):
        watcher_script=self.project/'watch.py'; watcher_script.write_text('pass\n')
        watcher={'cwd':str(self.project),'argv':['/usr/bin/python3',str(watcher_script),'--every','10'],
                 'script_sha256':m.file_hash(watcher_script),'service':'claude-test-watcher.service','log':str(self.root/'watch.log')}
        m.write_json(m.ROOT/'persistent-watchers.json',[watcher])
        self.state['background_tasks']=[{'id':'watch','type':'shell','status':'running','command':'python3 watch.py --every 10'},
                                        {'id':'render','type':'shell','status':'running','command':'render'}]
        with mock.patch.object(m.subprocess,'run',return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual([x['id'] for x in m.rotation_busy(self.state)[2]],['render'])
            self.assertEqual(run.call_args.args[0],['systemctl','--user','is-active','--quiet','claude-test-watcher.service'])
            watcher_script.write_text('changed\n')
            self.assertEqual(len(m.rotation_busy(self.state)[2]),2)
        self.assertEqual(self.state['background_tasks'][0]['status'],'running')

    def test_fixture_subagent_in_scratch_folder_is_not_a_resumable_agent(self):
        # Real session artifacts can contain test fixtures with agent-like paths.
        fixture=self.project/self.sid/'tool-results'/'fixture'/'subagents'/'agent-fake.jsonl'
        fixture.parent.mkdir(parents=True); fixture.write_text('{"type":"user"}\n')
        manifest=m.verify_checkpoint(self.checkpoint())
        self.assertNotIn('fake',manifest['agents'])
        self.assertIn(str(fixture),[x['source'] for x in manifest['files']])

    def test_external_watcher_alert_is_visible_and_does_not_allow_new_writes(self):
        log=self.root/'watch.log'; log.write_text('[ALERT] wrong model\n')
        m.write_json(m.ROOT/'persistent-watchers.json',[
            {'cwd':str(self.project),'service':'claude-test-watcher.service','log':str(log)}])
        result=m.lifecycle_record(self.run,'main',self.payload('UserPromptSubmit'))
        self.assertIn(str(log),result['hookSpecificOutput']['additionalContext'])
        denied=m.lifecycle_record(self.run,'main',self.payload('PreToolUse',tool_name='Write',tool_use_id='write'))
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'],'deny')
        allowed=m.lifecycle_record(self.run,'main',self.payload('PreToolUse',tool_name='Read',tool_use_id='read'))
        self.assertIsNone(allowed)

    def test_corrupted_backup_is_rejected(self):
        checkpoint=self.checkpoint()
        manifest=json.loads(checkpoint.read_text(encoding='utf-8'))
        item=manifest['files'][0]
        (checkpoint.parents[2]/'objects'/item['sha256']).write_text('damaged')
        with self.assertRaises(m.ContextNotReady): m.arm_recovery(self.run,checkpoint)
        self.assertFalse((self.run/'pending-recovery.json').exists())

    def test_missing_files_restore_exactly_and_existing_conflict_is_not_overwritten(self):
        checkpoint=self.checkpoint()
        agent_bytes=self.agent.read_bytes()
        self.agent.unlink()
        self.assertEqual(m.restore_missing_context(checkpoint,self.registry),1)
        self.assertEqual(self.agent.read_bytes(),agent_bytes)
        self.transcript.write_text('{"new":"different user history"}\n')
        conflicting=self.transcript.read_bytes()
        with self.assertRaises(m.ContextNotReady): m.restore_missing_context(checkpoint,self.registry)
        self.assertEqual(self.transcript.read_bytes(),conflicting)

    def test_old_live_controller_is_not_given_a_force_kill_rotation_event(self):
        (self.run/'launch.json').unlink()
        payload=self.payload('StopFailure',error='rate_limit',last_assistant_message="You've hit your session limit")
        with mock.patch.object(m.sys,'stdin',io.StringIO(json.dumps(payload))), mock.patch.object(m,'load_registry',return_value=self.registry):
            m.native_hook('_hook',self.run,'main')
        events=[json.loads(line) for line in (self.run/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(events[-1]['event'],'LegacyRotationPaused')
        self.assertTrue(self.transcript.exists())

    def test_child_error_is_promoted_for_already_running_context_safe_controller(self):
        payload=self.payload('StopFailure',agent_id='original',agent_type='general-purpose',error='rate_limit',last_assistant_message="You've hit your session limit")
        with mock.patch.object(m.sys,'stdin',io.StringIO(json.dumps(payload))):
            m.native_hook('_hook',self.run,'main')
        event=json.loads((self.run/'events.jsonl').read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(event['event'],'StopFailure')
        self.assertIsNone(event['agent_id'])
        self.assertEqual(event['origin_agent_id'],'original')
        original=json.loads((self.run/'lifecycle.jsonl').read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(original['payload']['agent_id'],'original')

    def test_native_internal_failure_is_kept_without_inventing_missing_subagent(self):
        m.lifecycle_record(self.run,'main',self.payload('StopFailure',agent_id='internal-request',error='rate_limit'))
        state=m.read_json(m.session_state_path(self.run,self.sid))
        self.assertNotIn('internal-request',state['agents'])
        self.assertIn('internal-request',state['unattributed_events'])
        m.context_sources(self.registry,state)

    def test_internal_stop_with_nonexistent_native_path_does_not_block_checkpoint(self):
        m.lifecycle_record(self.run,'main',self.payload('SubagentStop',agent_id='internal-auto',agent_type='',agent_transcript_path=str(self.subdir/'agent-internal-auto.jsonl'),last_assistant_message='internal receipt'))
        state=m.read_json(m.session_state_path(self.run,self.sid))
        self.assertNotIn('internal-auto',state['agents'])
        checkpoint=m.checkpoint_context(self.registry,state,self.run,'test')
        manifest=m.verify_checkpoint(checkpoint)
        self.assertIn('internal-auto',manifest['unattributed_events'])
        self.assertTrue(any(x['role']=='hook-journal' for x in manifest['files']))

    def test_migrates_old_untracked_entries_without_losing_receipts(self):
        state={**self.state,'agents':{**self.state['agents'],'internal-old':{'state':'stopped','agent_transcript_path':str(self.subdir/'agent-internal-old.jsonl')}}}
        m.reconcile_untracked_hooks(state)
        self.assertIn('original',state['agents'])
        self.assertNotIn('internal-old',state['agents'])
        self.assertIn('internal-old',state['unattributed_events'])

    def test_real_subagent_missing_history_still_blocks_checkpoint(self):
        m.lifecycle_record(self.run,'main',self.payload('SubagentStart',agent_id='missing-worker',agent_type='general-purpose'))
        m.lifecycle_record(self.run,'main',self.payload('StopFailure',agent_id='missing-worker',error='rate_limit'))
        state=m.read_json(m.session_state_path(self.run,self.sid))
        with self.assertRaises(m.ContextNotReady): m.context_sources(self.registry,state)

    def test_partial_record_and_symlink_are_rejected(self):
        with self.agent.open('a') as stream: stream.write('{"incomplete":')
        with self.assertRaises(m.ContextNotReady): self.checkpoint()
        self.agent.write_text('{}\n')
        (self.subdir/'unsafe').symlink_to(self.root)
        with self.assertRaises(m.ContextNotReady): self.checkpoint()

    def test_recovery_requires_original_agent_id(self):
        checkpoint=self.checkpoint()
        m.arm_recovery(self.run,checkpoint)
        m.lifecycle_record(self.run,'second',self.payload('SessionStart'))
        result=m.lifecycle_record(self.run,'second',self.payload('PreToolUse',tool_name='Agent',tool_use_id='new'))
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')
        result=m.lifecycle_record(self.run,'second',self.payload('PreToolUse',tool_name='Edit',tool_use_id='premature-edit'))
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')
        self.assertIsNone(m.lifecycle_record(self.run,'second',self.payload('PreToolUse',tool_name='Read',tool_use_id='inspect-history')))
        m.lifecycle_record(self.run,'second',self.payload('SubagentStart',agent_id='replacement',agent_type='general-purpose'))
        self.assertEqual(m.read_json(self.run/'pending-recovery.json')['pending_agents'],['original'])
        m.lifecycle_record(self.run,'second',self.payload('SubagentStart',agent_id='original',agent_type='general-purpose'))
        self.assertEqual(m.read_json(self.run/'pending-recovery.json')['pending_agents'],[])
        self.assertIsNone(m.lifecycle_record(self.run,'second',self.payload('PreToolUse',tool_name='Agent',tool_use_id='new2')))

    def test_wrong_session_blocks_prompt_and_tools(self):
        m.arm_recovery(self.run,self.checkpoint())
        wrong=self.payload('SessionStart',session_id=str(uuid.uuid4()))
        m.lifecycle_record(self.run,'second',wrong)
        result=m.lifecycle_record(self.run,'second',{**wrong,'hook_event_name':'UserPromptSubmit'})
        self.assertEqual(result['decision'],'block')
        result=m.lifecycle_record(self.run,'second',{**wrong,'hook_event_name':'PreToolUse','tool_name':'Bash','tool_use_id':'bad'})
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')

    def test_resume_verifies_without_replaying_history_in_deadline_hook(self):
        m.arm_recovery(self.run,self.checkpoint())
        before = self.transcript.read_bytes(), self.agent.read_bytes()
        with mock.patch.object(m,'reconcile_receipts',side_effect=AssertionError('slow history replay')):
            m.lifecycle_record(self.run,'second',self.payload('SessionStart'))
            self.assertTrue(m.read_json(self.run/'pending-recovery.json')['verified'])
            self.assertIsNone(m.lifecycle_record(self.run,'second',self.payload('UserPromptSubmit')))
            m.lifecycle_record(self.run,'second',self.payload('PreToolUse',tool_name='Read',tool_use_id='read'))
            m.lifecycle_record(self.run,'second',self.payload('PostToolUse',tool_name='Read',tool_use_id='read'))
        self.assertEqual(before,(self.transcript.read_bytes(),self.agent.read_bytes()))
        self.assertFalse(m.read_json(m.session_state_path(self.run,self.sid))['pending_tools'])

    def test_controller_persists_replayed_receipts_and_cursors(self):
        self.state['pending_tools']={'finished':{'tool_name':'Read'},'running':{'tool_name':'Bash'}}
        m.write_json(m.session_state_path(self.run,self.sid),self.state)
        with self.transcript.open('a') as stream:
            stream.write(json.dumps({'type':'user','message':{'content':[
                {'type':'tool_result','tool_use_id':'finished','content':'done'}]}})+'\n')
        self.assertEqual(m.rotation_busy(self.state,self.run)[1],['running'])
        saved=m.read_json(m.session_state_path(self.run,self.sid))
        self.assertEqual(list(saved['pending_tools']),['running'])
        self.assertEqual(saved['receipt_cursors'][str(self.transcript)]['offset'],self.transcript.stat().st_size)
        with mock.patch.object(m,'native_task_result',side_effect=AssertionError('repeated history replay')):
            self.assertEqual(m.rotation_busy(saved,self.run)[1],['running'])

    def test_controller_scan_never_overwrites_concurrent_hook(self):
        m.write_json(m.session_state_path(self.run,self.sid),self.state)
        original=m.reconcile_receipts
        def scan(state):
            original(state)
            # This hook also proves the scan did not hold the lifecycle lock.
            m.lifecycle_record(self.run,'main',self.payload('PreToolUse',tool_name='Bash',tool_use_id='new'))
        with mock.patch.object(m,'reconcile_receipts',side_effect=scan):
            with self.assertRaises(m.ContextNotReady): m.rotation_busy(self.state,self.run)
        saved=m.read_json(m.session_state_path(self.run,self.sid))
        self.assertIn('new',saved['pending_tools'])
        self.assertEqual(m.rotation_busy(saved,self.run)[1],['new'])

    def test_draining_waits_for_inflight_tool_and_does_not_start_new_one(self):
        m.lifecycle_record(self.run,'main',self.payload('PreToolUse',tool_name='Bash',tool_use_id='running'))
        m.write_json(self.run/'draining.json',{'session_id':self.sid})
        result=m.lifecycle_record(self.run,'main',self.payload('PreToolUse',tool_name='Bash',tool_use_id='denied'))
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')
        state=m.read_json(m.session_state_path(self.run,self.sid))
        self.assertEqual(m.rotation_busy(state)[1],['running'])
        m.lifecycle_record(self.run,'main',self.payload('PostToolUseFailure',tool_use_id='running',error='cancelled'))
        state=m.read_json(m.session_state_path(self.run,self.sid))
        self.assertEqual(m.rotation_busy(state),([],[],[]))

    def test_api_failed_subagent_is_not_marked_completed(self):
        m.lifecycle_record(self.run,'main',self.payload('SubagentStart',agent_id='original',agent_type='general-purpose'))
        m.lifecycle_record(self.run,'main',self.payload('StopFailure',agent_id='original',error='rate_limit'))
        m.lifecycle_record(self.run,'main',self.payload('SubagentStop',agent_id='original',agent_type='general-purpose'))
        state=m.read_json(m.session_state_path(self.run,self.sid))
        self.assertEqual(state['agents']['original']['state'],'failed')

    def test_service_safety_stop_is_archived_but_not_auto_resumed(self):
        m.lifecycle_record(self.run,'main',self.payload('SubagentStart',agent_id='original',agent_type='general-purpose'))
        m.lifecycle_record(self.run,'main',self.payload('StopFailure',agent_id='original',error='rate_limit',last_assistant_message='Session paused: safeguards [cyber]'))
        m.lifecycle_record(self.run,'main',self.payload('SubagentStop',agent_id='original',agent_type='general-purpose'))
        self.state=m.read_json(m.session_state_path(self.run,self.sid))
        self.assertEqual(self.state['agents']['original']['state'],'blocked')
        checkpoint=self.checkpoint()
        m.arm_recovery(self.run,checkpoint)
        m.lifecycle_record(self.run,'second',self.payload('SessionStart'))
        result=m.lifecycle_record(self.run,'second',self.payload('PreToolUse',tool_name='SendMessage',tool_input={'to':'original'},tool_use_id='retry'))
        self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')

    def test_banned_subagent_is_blocked_in_recovery_manifest(self):
        m.lifecycle_record(self.run,'main',self.payload('SubagentStart',agent_id='original',agent_type='general-purpose'))
        m.lifecycle_record(self.run,'main',self.payload('StopFailure',agent_id='original',error='authentication_failed',last_assistant_message='Your account has been disabled.'))
        self.state=m.read_json(m.session_state_path(self.run,self.sid))
        checkpoint=self.checkpoint()
        m.arm_recovery(self.run,checkpoint)
        recovery=m.read_json(self.run/'pending-recovery.json')
        self.assertEqual(recovery['pending_agents'],[])
        self.assertEqual(recovery['blocked_agents'],['original'])

    def test_same_session_cannot_be_owned_by_two_live_launchers(self):
        with mock.patch.object(m,'legacy_session_owners',return_value=[]), mock.patch.object(m,'process_identity',return_value='same-boot-process'):
            m.claim_session(self.sid,123)
            m.claim_session(self.sid,123)
            with self.assertRaises(m.ContextNotReady): m.claim_session(self.sid,456)
        with mock.patch.object(m,'legacy_session_owners',return_value=[]), mock.patch.object(m,'process_identity',side_effect=lambda pid: 'new-process' if pid==456 else None):
            m.claim_session(self.sid,456)

    def test_no_forced_kill_after_graceful_exit_timeout(self):
        # The console offers nothing else: no kill or close is reachable from here.
        console=mock.Mock(spec=['request_exit','exit_code','events','show'])
        console.exit_code.return_value=None
        console.events.return_value=[('output',b'still working')]
        self.assertFalse(m.terminate_child(console,timeout=0.05))
        console.request_exit.assert_called_once_with()
        console.show.assert_called_with(b'still working')


if __name__=='__main__': unittest.main(verbosity=2)
