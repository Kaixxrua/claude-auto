"""Plugin packaging and compatibility without launching a real client."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from test_claude_accounts import m


class CompactionLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prefix = Path(self.temp.name)
        self.plugin = self.prefix/'share/claude-auto/compaction-recovery'
        manifest = self.plugin/'.claude-plugin/plugin.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{}')
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(m, 'SCRIPT', self.prefix/'bin/claude-accounts').start()
        mock.patch.dict(os.environ, {'CLAUDE_AUTO_COMPACTION_RECOVERY':'1'}).start()
        self.run = mock.patch.object(m.subprocess, 'run', return_value=
            subprocess.CompletedProcess([], 0, '2.1.296 (Claude Code)\n', '')).start()

    def test_supported_native_loads_installed_plugin(self):
        self.assertEqual(m.compaction_plugin_arguments('/fixture/native'),
                         ['--plugin-dir', str(self.plugin)])
        self.assertEqual(self.run.call_args.args[0], ['/fixture/native','--version'])

    def test_old_or_unknown_native_skips_mod(self):
        for version in ('2.1.286', 'offline fixture'):
            with self.subTest(version=version):
                self.run.return_value.stdout = version
                self.assertEqual(m.compaction_plugin_arguments('/fixture/native'), [])

    def test_opt_out_does_not_run_native(self):
        with mock.patch.dict(os.environ, {'CLAUDE_AUTO_COMPACTION_RECOVERY':'0'}):
            self.assertEqual(m.compaction_plugin_arguments('/fixture/native'), [])
        self.run.assert_not_called()

    def test_missing_asset_does_not_run_native(self):
        (self.plugin/'.claude-plugin/plugin.json').unlink()
        self.assertEqual(m.compaction_plugin_arguments('/fixture/native'), [])
        self.run.assert_not_called()

    def test_native_failure_falls_back_without_changing_account(self):
        self.run.side_effect = subprocess.TimeoutExpired('fixture', 10)
        self.assertEqual(m.compaction_plugin_arguments('/fixture/native'), [])

    def test_installed_copy_is_not_loaded_twice_or_reenabled(self):
        profile = self.prefix/'profile'
        installed = profile/'plugins/installed_plugins.json'
        installed.parent.mkdir(parents=True)
        installed.write_text(json.dumps({'plugins': {'auto-compaction-recovery@fixture': []}}))
        self.assertEqual(m.compaction_plugin_arguments('/fixture/native', profile), [])
        self.run.assert_not_called()

    def test_user_plugin_options_survive_rotation(self):
        args = ['--plugin-dir', '/tmp/fixture plugin', '--model', 'opus', 'user prompt']
        self.assertEqual(m.persistent_arguments(args), args[:-1])
