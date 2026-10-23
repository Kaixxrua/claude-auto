"""Exercise a clean install and the real HTTP server without account access."""
import argparse
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from _support import REPO_ROOT, WINDOWS, native_fixture
import monitor


FAKE_NATIVE = '''#!/usr/bin/env python3
import json, os, pathlib, sys, time
profile = pathlib.Path(os.environ.get('CLAUDE_CONFIG_DIR', str(pathlib.Path.home()/'.claude')))
if sys.argv[1:] == ['auth', 'status']:
    print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai',
                      'subscriptionType': 'pro', 'email': profile.name+'@example.test'}))
    sys.exit(0)
if sys.argv[1:] == ['--version']:
    print('0.0.0 (offline test fixture)'); sys.exit(0)
assert '--safe-mode' in sys.argv and '--strict-mcp-config' in sys.argv
assert sys.argv[sys.argv.index('--tools')+1] == ''
assert sys.argv[-1] == '/usage'
print('Current session\\n20% used\\nResets 11:59pm (UTC)\\nCurrent week (all models)\\n35% used\\nResets 11:59pm (UTC)', flush=True)
time.sleep(10)
'''


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root/'isolated home'
        self.home.mkdir()
        self.prefix = self.root/'installation with spaces'
        self.runtime = self.root/'private accounts'
        self.env = os.environ.copy()
        for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN',
                    'ANTHROPIC_PROFILE', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
                    'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDE_CONFIG_DIR'):
            self.env.pop(key, None)
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home), CLAUDE_ACCOUNTS_HOME=str(self.runtime),
                        PYTHONUNBUFFERED='1')
        (self.home/'.local/bin').mkdir(parents=True)
        self.native = native_fixture(self.home/'.local/bin/claude', FAKE_NATIVE)

    def bin(self, name):
        # Windows runs the installed commands through their .cmd files.
        return self.prefix/'bin'/(name+'.cmd' if WINDOWS else name)

    def command(self, *args, check=True, env=None):
        result = subprocess.run([str(arg) for arg in args], env=env or self.env, cwd=self.root,
                                capture_output=True, encoding='utf-8', errors='replace', timeout=20)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def install(self):
        self.command(sys.executable, REPO_ROOT/'scripts/install.py', '--prefix', self.prefix)

    def test_install_init_upgrade_preserves_private_data_and_native_binary(self):
        self.install()
        self.assertFalse(self.runtime.exists(), 'install must not initialize accounts')
        plugin = self.prefix/'share/claude-auto/compaction-recovery'
        self.assertEqual(json.loads((plugin/'.claude-plugin/plugin.json').read_text(encoding='utf-8'))['name'],
                         'auto-compaction-recovery')
        self.assertTrue((plugin/'hooks/register.js').is_file())
        for command in ('claude-auto', 'claude-accounts', 'claude-quota', 'claude-delegate'):
            self.command(self.bin(command), '--help')
        self.command(self.bin('claude-accounts'), 'init')
        registry = json.loads((self.runtime/'accounts.json').read_text(encoding='utf-8'))
        self.assertEqual(registry['profiles']['main'], str(self.home/'.claude'))
        self.assertEqual(registry['profiles']['second'], str(self.runtime/'second'))
        if not WINDOWS:
            self.assertEqual((self.runtime/'accounts.json').stat().st_mode & 0o777, 0o600)
        projects = self.runtime/'second/projects'
        self.assertTrue(projects.is_symlink() or getattr(os.path, 'isjunction', lambda _: False)(projects))
        sentinel = self.runtime/'second/.credentials.json'
        sentinel.write_text('{"fixture": "do not overwrite"}')
        before = (sentinel.read_bytes(), (self.runtime/'accounts.json').read_bytes(), self.native.read_bytes())
        self.install()
        self.assertEqual(before, (sentinel.read_bytes(), (self.runtime/'accounts.json').read_bytes(), self.native.read_bytes()))
        self.assertNotIn('.credentials.json', [p.name for p in self.prefix.rglob('*')])
        delegate = self.prefix/'share/claude-auto/account-delegation'
        if WINDOWS:
            # Windows has no python3 command; the plugin runs with the installing interpreter.
            server = json.loads((delegate/'.mcp.json').read_bytes())['mcpServers']['accounts']
            self.assertEqual(server['command'], sys.executable)
        self.assertTrue((delegate/'.mcp.json').is_file())
        rpc = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})+'\n'
        result = subprocess.run([sys.executable, str(delegate/'scripts/server.py')],
                                input=rpc, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)['result']['tools']), 5)

    def test_installer_preflight_rejects_symlink_without_partial_upgrade(self):
        bindir = self.prefix/'bin'
        bindir.mkdir(parents=True)
        (bindir/'claude-accounts').write_text('old launcher')
        victim = self.root/'keep.txt'
        victim.write_text('keep')
        self.bin('claude-auto').symlink_to(victim)
        result = self.command(sys.executable, REPO_ROOT/'scripts/install.py', '--prefix', self.prefix, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(victim.read_text(encoding='utf-8'), 'keep')
        self.assertEqual((bindir/'claude-accounts').read_text(encoding='utf-8'), 'old launcher')

    @unittest.skipIf(WINDOWS, 'systemd user service')
    def test_systemd_install_only_writes_unit_under_isolated_home(self):
        self.command(sys.executable, REPO_ROOT/'scripts/install.py', '--systemd')
        service = self.home/'.config/systemd/user/claude-quota-monitor.service'
        self.assertIn('--bind 127.0.0.1', service.read_text(encoding='utf-8'))
        self.assertFalse((service.parent/'default.target.wants').exists())
        self.assertFalse(self.runtime.exists())

    def test_installed_server_collects_fixture_usage_and_serves_all_assets(self):
        self.install()
        self.command(self.bin('claude-accounts'), 'init')
        # Explicit --root must also configure the dynamically loaded launcher.
        bait = self.root/'other accounts'
        bait.mkdir()
        (bait/'accounts.json').write_text(json.dumps({'disabled': {'main': 'wrong root', 'second': 'wrong root'}}))
        env = dict(self.env, CLAUDE_ACCOUNTS_HOME=str(bait))
        command = [str(self.prefix/'bin/claude-quota'), '--root', str(self.runtime), '--port', '0', '--cwd', str(self.root)]
        options = {}
        if WINDOWS:
            # Run the dashboard itself, in its own group, so Ctrl+Break can stop it like a terminal would.
            command[:1] = [sys.executable, str(self.prefix/'share/claude-quota-monitor/monitor.py'),
                           '--adapter', str(self.prefix/'bin/claude-accounts')]
            options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        process = subprocess.Popen(command, env=env, cwd=self.root, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding='utf-8', **options)
        try:
            started = []
            reader = threading.Thread(target=lambda: started.append(process.stdout.readline()), daemon=True)
            reader.start()
            reader.join(10)
            self.assertTrue(started, 'server did not start')
            line = started[0].strip()
            self.assertIn('http://127.0.0.1:', line)
            port = int(line.rsplit(':', 1)[1])

            def request(path, host=None):
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                try:
                    conn.request('GET', path, headers={'Host': host or f'127.0.0.1:{port}'})
                    response = conn.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    conn.close()

            deadline = time.monotonic()+10
            while True:
                code, headers, body = request('/api/status')
                status = json.loads(body)
                if all(a.get('windows', {}).get('five_hour') for a in status['accounts']):
                    break
                if time.monotonic() > deadline:
                    self.fail('fixture collection did not finish: '+body.decode())
                time.sleep(.1)
            self.assertEqual(code, 200)
            self.assertEqual(len(status['accounts']), 2)
            self.assertTrue(all(a['windows']['five_hour']['used_percentage'] == 20 for a in status['accounts']))
            self.assertEqual(headers['Cache-Control'], 'no-store')
            for asset in ('/', '/app.js', '/style.css', '/favicon.svg', '/api/health'):
                with self.subTest(asset=asset):
                    code, _, body = request(asset)
                    self.assertEqual(code, 200)
                    self.assertTrue(body)
            self.assertEqual(request('/api/status', f'localhost:{port}')[0], 200)
            self.assertEqual(request('/api/status', f'evil.test:{port}')[0], 403)
            self.assertEqual(request('/../accounts.json')[0], 404)
            self.assertEqual(request('/.credentials.json')[0], 404)
        finally:
            if process.poll() is None:
                if WINDOWS:
                    sys.path.insert(0, str(REPO_ROOT/'lib'))
                    import claude_auto_os
                    claude_auto_os.stop_group(process.pid)
                else:
                    process.terminate()
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stdout+stderr)


class DashboardDefaultsTests(unittest.TestCase):
    def test_default_is_loopback_and_custom_root_is_respected(self):
        with mock.patch.dict(os.environ, {'CLAUDE_ACCOUNTS_HOME': '/tmp/fixture-accounts'}):
            args = monitor.build_parser().parse_args([])
        self.assertEqual(args.bind, '127.0.0.1')
        self.assertEqual(args.root, Path('/tmp/fixture-accounts'))
        self.assertEqual(args.cwd, Path.cwd())

    def test_wildcard_public_and_unsupported_addresses_are_rejected(self):
        for address in ('0.0.0.0', '8.8.8.8', '255.255.255.255', '::', '::1', 'localhost', 'invalid'):
            with self.subTest(address=address), self.assertRaises(argparse.ArgumentTypeError):
                monitor.local_bind(address)
        for address in ('127.0.0.1', '10.0.0.5', '172.16.1.5', '192.168.1.5'):
            self.assertEqual(monitor.local_bind(address), address)


if __name__ == '__main__':
    unittest.main()
