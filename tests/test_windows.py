"""Windows: the OS layer and one full account rotation through ConPTY, offline."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

from _support import REPO_ROOT, native_fixture

sys.path.insert(0, str(REPO_ROOT/'lib'))
import claude_auto_os as oslayer  # noqa: E402

LAUNCHER = REPO_ROOT/'bin/claude-accounts'
ANSI = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[78=>]')

FAKE = r'''
import json, os, pathlib, signal, subprocess, sys, time
name = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR']).name
args = sys.argv[1:]
if args == ['auth', 'status']:
    print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'subscriptionType': 'pro', 'email': name+'@example.test'}))
    sys.exit(0)
settings = json.loads(pathlib.Path(args[args.index('--settings')+1]).read_bytes())
sid = os.environ['TEST_SESSION']
project = pathlib.Path(os.environ['CLAUDE_ACCOUNTS_HOME'])/'main'/'projects'/'fake-project'
project.mkdir(parents=True, exist_ok=True)
transcript = project/(sid+'.jsonl')
trace = pathlib.Path(os.environ['TEST_TRACE'])
def record(row):
    with trace.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(row, ensure_ascii=False)+'\n')
def save(row):
    with transcript.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(row, ensure_ascii=False)+'\n')
def hook(event, **extra):
    payload = {'hook_event_name': event, 'session_id': sid, 'transcript_path': str(transcript), 'cwd': str(project), **extra}
    for entry in settings['hooks'].get(event, []):
        for command in entry['hooks']:
            # The exec form, as Claude Code runs hooks on Windows.
            subprocess.run([command['command'], *command['args']], input=json.dumps(payload).encode(), check=True)
save({'type': 'user', 'message': {'role': 'user', 'content': '原始任务必须保留'}, 'sessionId': sid})
record({'account': name, 'args': args, 'session_id': sid})
hook('SessionStart')
if name == os.environ['TEST_LAST_ACCOUNT']:
    assert args[args.index('--resume')+1] == sid, args
    assert args[args.index('--tools')+1] == 'Read', args
    print('RESUME_OK', flush=True)
    time.sleep(.3)
    sys.exit(0)
presses = []
def exit_cleanly(*_):
    # Like Claude Code: the first Ctrl+C only asks for another, the second ends the client.
    # Ending on the first would start SessionEnd while the rotation's second press arrives.
    presses.append(1)
    if len(presses) != 2:
        return
    save({'type': 'system', 'data': 'final-flush-before-exit'})
    hook('SessionEnd')
    record({'account': name, 'exit': 'ctrl-c'})
    os._exit(0)
signal.signal(signal.SIGINT, exit_cleanly)
hook('StopFailure', error='rate_limit', last_assistant_message="You've hit your session limit · resets later")
for _ in range(600):
    time.sleep(.05)
'''


@unittest.skipUnless(oslayer.WINDOWS, 'Windows only')
class OsLayerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def python(self, code, **options):
        return subprocess.Popen([sys.executable, '-c', code], **options)

    def test_lease_follows_the_inheriting_child_and_ends_with_it(self):
        path = self.root/'lease'
        fd = oslayer.exclusive(path)
        self.assertIsNotNone(fd)
        self.assertIsNone(oslayer.exclusive(path))
        child = self.python('import time; time.sleep(1.5)', **oslayer.inherit([fd]))
        os.close(fd)
        self.assertIsNone(oslayer.exclusive(path))
        child.wait(timeout=10)
        again = oslayer.exclusive(path)
        self.assertIsNotNone(again)
        os.close(again)

    def test_lock_is_exclusive_until_closed(self):
        path = self.root/'lock'
        first = os.open(path, os.O_CREAT | os.O_RDWR | oslayer.BINARY)
        second = os.open(path, os.O_RDWR | oslayer.BINARY)
        self.addCleanup(os.close, second)
        self.assertTrue(oslayer.lock(first, blocking=False))
        self.assertFalse(oslayer.lock(second, blocking=False))
        os.close(first)
        self.assertTrue(oslayer.lock(second, blocking=False))

    def test_process_identity_and_arguments(self):
        args = ['with space', 'quote"inside', '中文 参数', 'trailing\\']
        probe = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)', *args])
        self.addCleanup(probe.kill)
        time.sleep(.3)
        self.assertEqual(oslayer.process_args(probe.pid)[-len(args):], args)
        self.assertTrue(oslayer.process_start(probe.pid))
        self.assertEqual(oslayer.parent_pid(probe.pid), os.getpid())
        self.assertIn(probe.pid, [pid for pid, _ in oslayer.commands(Path(sys.executable).stem)])
        probe.kill()
        probe.wait()
        self.assertIsNone(oslayer.process_start(probe.pid))

    def test_stop_group_delivers_ctrl_break_to_a_detached_child(self):
        child = self.python(
            'import signal, sys, time\n'
            'signal.signal(signal.SIGBREAK, lambda *_: sys.exit(5))\n'
            'print("ready", flush=True)\n'
            'for _ in range(400): time.sleep(.05)\n',
            stdout=subprocess.PIPE, **oslayer.detached())
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), b'ready')
        oslayer.stop_group(child.pid)
        self.assertEqual(child.wait(timeout=10), 5)

    def test_kill_group_ends_the_whole_tree(self):
        child = self.python(
            'import subprocess, sys, time\n'
            'grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
            'print(grandchild.pid, flush=True)\n'
            'time.sleep(60)\n',
            stdout=subprocess.PIPE, **oslayer.detached())
        self.addCleanup(child.kill)
        grandchild = int(child.stdout.readline())
        oslayer.kill_group(child.pid)
        child.wait(timeout=10)
        deadline = time.monotonic()+10
        while oslayer.process_start(grandchild) and time.monotonic() < deadline:
            time.sleep(.1)
        self.assertIsNone(oslayer.process_start(grandchild))

    def test_read_bytes_waits_while_a_replaced_file_cannot_be_opened(self):
        path = self.root/'state.json'
        refused = PermissionError(13, 'Access is denied')
        with mock.patch.object(Path, 'read_bytes', side_effect=[refused, refused, b'{}']), \
                mock.patch.object(oslayer.time, 'sleep') as sleep:
            self.assertEqual(oslayer.read_bytes(path), b'{}')
        self.assertEqual(sleep.call_count, 2)
        with mock.patch.object(Path, 'read_bytes', side_effect=refused), \
                mock.patch.object(oslayer.time, 'sleep'), self.assertRaises(PermissionError):
            oslayer.read_bytes(path)

    def test_hook_command_uses_the_exec_form(self):
        self.assertEqual(oslayer.hook_command([r'C:\Program Files\Python\python.exe', 'a b', '_hook']),
                         {'command': r'C:\Program Files\Python\python.exe', 'args': ['a b', '_hook']})

    def test_terminal_reads_utf8_output(self):
        terminal = oslayer.Terminal([sys.executable, '-c', 'import time; print("RESULT 中文"); time.sleep(5)'],
                                    dict(os.environ), str(self.root), cols=120, rows=30)
        output = bytearray()
        try:
            deadline = time.monotonic()+20
            while 'RESULT 中文' not in output.decode(errors='replace') and time.monotonic() < deadline:
                data = terminal.read(.2)
                if data == b'':
                    break
                output.extend(data or b'')
        finally:
            terminal.close(grace=1)
        self.assertIn('RESULT 中文', output.decode(errors='replace'))


@unittest.skipUnless(oslayer.WINDOWS, 'Windows only')
class RotationTests(unittest.TestCase):
    def test_rotation_through_conpty_resumes_the_same_session(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        native = native_fixture(root/'claude', FAKE)
        names = ('main', 'second')
        for name in names:
            (root/name).mkdir()
            (root/name/'settings.json').write_text('{}')
        (root/'accounts.json').write_text(json.dumps(
            {'version': 1, 'claude': str(native), 'profiles': {n: str(root/n) for n in names}, 'default': 'main'}))
        home = root/'isolated-home'
        home.mkdir()
        env = os.environ.copy()
        for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CONFIG_DIR'):
            env.pop(key, None)
        session = str(uuid.uuid4())
        env.update(CLAUDE_ACCOUNTS_HOME=str(root), TEST_SESSION=session, TEST_TRACE=str(root/'trace.jsonl'),
                   TEST_LAST_ACCOUNT=names[-1], HOME=str(home), USERPROFILE=str(home))
        terminal = oslayer.Terminal([sys.executable, str(LAUNCHER), 'run', '--account', 'main', '--',
                                     '--tools', 'Read', 'original task'], env, str(root), cols=120, rows=40)
        output = bytearray()
        status = None
        try:
            deadline = time.monotonic()+90
            while time.monotonic() < deadline:
                data = terminal.read(.2)
                output.extend(data or b'')
                status = terminal.poll()
                if status is not None:
                    while data := terminal.read(.3):
                        output.extend(data)
                    break
        finally:
            terminal.close(grace=2)
        text = ANSI.sub('', output.decode(errors='replace'))
        self.assertEqual(status, 0, text)
        trace = [json.loads(line) for line in (root/'trace.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([row['account'] for row in trace if 'args' in row], ['main', 'second'], text)
        self.assertIn({'account': 'main', 'exit': 'ctrl-c'}, trace)
        self.assertEqual({row['session_id'] for row in trace if 'args' in row}, {session})
        self.assertIn('RESUME_OK', text)


if __name__ == '__main__':
    unittest.main()
