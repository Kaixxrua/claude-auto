import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest import mock

import test_claude_accounts as base
from test_login_retry import BUSY

# Path.home() on Windows needs this even in a cleared environment.
HOME = {key: os.environ[key] for key in ('USERPROFILE',) if key in os.environ}

m = base.m


class SessionRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run = self.root / 'run'
        self.sid, self.client = str(uuid.uuid4()), 'client'
        self.registry = {'profiles': {'second': str(self.root / 'second')}}
        self.patch = mock.patch.object(m, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        m.write_json(self.root / 'accounts.json', self.registry)
        self.state = {'account': 'second', 'client_id': self.client, 'session_id': self.sid,
                      'agents': {}, 'pending_tools': {}, 'background_tasks': []}
        self.save_state()
        self.retry = m.RecoveryRetry(self.run, 'second', self.client, None, allow_user_prompts=True)
        self.event('SessionStart')

    def save_state(self, **changes):
        m.write_json(m.session_state_path(self.run, self.sid), {**self.state, **changes})

    def event(self, kind, **changes):
        self.retry.observe({'at': time.time(), 'event': kind, 'account': 'second',
                            'client_id': self.client, 'session_id': self.sid, **changes})

    def fail(self, **changes):
        self.event('StopFailure', error='server_error', message=BUSY, **changes)

    def arm(self):
        self.event('UserPromptSubmit')
        self.fail()
        self.assertTrue(self.retry.active)
        self.retry.deadline = 0

    def test_manual_in_app_resume_then_request_needs_no_manifest(self):
        self.event('SessionEnd')
        self.sid = str(uuid.uuid4())
        self.save_state()
        self.event('SessionStart')
        self.arm()
        self.assertTrue(self.retry.can_submit(self.registry))
        self.assertEqual(self.retry.prompt, m.LOGIN_PROMPT_RETRY)

    def test_no_prompt_no_retry(self):
        self.fail()
        self.assertFalse(self.retry.active)

    def test_budget_stays_two_without_new_request(self):
        self.arm()
        for _ in range(2):
            self.retry.submitted()
            self.event('UserPromptSubmit')
            self.fail()
        self.assertEqual(self.retry.attempts, 2)
        self.assertEqual(self.retry.phase, 'exhausted')

    def test_draft_cancels_and_late_error_cannot_rearm(self):
        self.arm()
        self.retry.keyboard(b'draft')
        self.fail()
        self.assertFalse(self.retry.active)

    def test_late_submit_receipt_cannot_overwrite_new_draft(self):
        self.retry.keyboard(b'draft')
        self.event('UserPromptSubmit')
        self.fail()
        self.assertFalse(self.retry.active)

    def test_complete_new_user_submission_can_arm_after_previous_turn(self):
        self.event('UserPromptSubmit')
        self.event('Stop')
        self.retry.keyboard(b'next\r')
        self.arm()
        self.assertTrue(self.retry.can_submit(self.registry))

    def test_old_receipt_cannot_override_newer_keyboard(self):
        self.retry.keyboard(b'\r')
        self.event('UserPromptSubmit', at=self.retry.last_input_at - 1)
        self.fail()
        self.assertFalse(self.retry.active)

    def test_subagent_progress_cancels_even_with_agent_id(self):
        self.arm()
        self.event('SubagentStart', agent_id='worker')
        self.fail()
        self.assertEqual(self.retry.phase, 'completed')

    def test_tools_progress_cancels(self):
        self.arm()
        self.event('PreToolUse')
        self.fail()
        self.assertFalse(self.retry.active)

    def test_background_work_blocks_submission(self):
        for change in ({'agents': {'worker': {'state': 'running'}}},
                       {'pending_tools': {'write': {}}},
                       {'background_tasks': [{'status': 'running'}]}):
            with self.subTest(change=change):
                self.save_state(**change)
                self.arm()
                self.assertFalse(self.retry.can_submit(self.registry))

    def test_other_failures_do_not_retry(self):
        for error, message in [('authentication_failed', 'Login expired'),
                               ('rate_limit', "You've hit your session limit"),
                               ('server_error', 'API Error: No response from API'),
                               ('account_banned', BUSY)]:
            with self.subTest(error=error):
                self.event('UserPromptSubmit')
                self.event('StopFailure', error=error, message=message)
                self.assertFalse(self.retry.active)

    def test_old_process_or_other_session_cannot_arm(self):
        self.event('UserPromptSubmit', client_id='old-client')
        self.fail()
        self.assertFalse(self.retry.active)
        self.event('UserPromptSubmit', session_id=str(uuid.uuid4()))
        self.fail()
        self.assertFalse(self.retry.active)

    def test_session_end_before_prompt_prevents_late_retry(self):
        self.event('SessionEnd')
        self.event('UserPromptSubmit')
        self.fail()
        self.assertFalse(self.retry.active)

    def test_disabled_and_failed_recovery_never_submit(self):
        self.arm()
        m.write_json(self.run / 'pending-recovery.json', {'error': 'context invalid'})
        self.assertFalse(self.retry.can_submit(self.registry))
        (self.run / 'pending-recovery.json').unlink()
        self.arm()
        m.write_json(self.root / 'accounts.json', {**self.registry, 'disabled': {'second': {'reason': 'banned'}}})
        self.assertFalse(self.retry.can_submit(self.registry))


class LockWaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = self.root / 'second'
        self.profile.mkdir()
        self.registry = {'profiles': {'second': str(self.profile)}}
        patch = mock.patch.object(m, 'ROOT', self.root)
        patch.start()
        self.addCleanup(patch.stop)
        m.write_json(self.root / 'accounts.json', self.registry)
        self.now = time.time()

    def run_wait(self, sleep, timeout=75):
        with mock.patch.object(m.time, 'time', side_effect=lambda: self.now), \
             mock.patch.object(m.time, 'monotonic', side_effect=lambda: self.now), \
             mock.patch.object(m.time, 'sleep', side_effect=sleep):
            return m.wait_for_login_refresh(self.registry, 'second', timeout=timeout)

    def test_no_lock_starts_without_wait(self):
        self.assertTrue(self.run_wait(lambda _: self.fail('unexpected wait')))

    def test_stale_legacy_lock_left_for_native_cleanup(self):
        lock = self.root / 'second.lock'
        lock.mkdir()
        os.utime(lock, (self.now - 65, self.now - 65))
        self.assertTrue(self.run_wait(lambda _: self.fail('unexpected wait')))
        self.assertTrue(lock.is_dir())

    def test_fresh_legacy_lock_waits_past_native_stale_threshold(self):
        lock = self.root / 'second.lock'
        lock.mkdir()
        os.utime(lock, (self.now, self.now))
        start = self.now
        self.assertTrue(self.run_wait(lambda n: setattr(self, 'now', self.now+n)))
        self.assertGreaterEqual(self.now-start, 61)
        self.assertTrue(lock.is_dir())

    def test_live_current_lock_release_starts_promptly(self):
        lock = self.profile / '.oauth_refresh.lock'
        lock.mkdir()
        self.assertTrue(self.run_wait(lambda _: lock.rmdir()))

    def test_live_lock_does_not_get_removed_after_wait_cap(self):
        lock = self.profile / '.oauth_refresh.lock'
        lock.mkdir()
        def heartbeat(n):
            self.now += n
            os.utime(lock, (self.now, self.now))
        self.assertFalse(self.run_wait(heartbeat, timeout=2))
        self.assertTrue(lock.is_dir())

    def test_disabled_profile_never_waits_or_touches_lock(self):
        m.write_json(self.root/'accounts.json', {**self.registry, 'disabled': {'second': {'reason': 'banned'}}})
        with self.assertRaises(RuntimeError):
            self.run_wait(lambda _: self.fail('unexpected wait'))


class ManualRetryPtyTests(unittest.TestCase):
    def test_manual_resume_in_same_process_retries_without_manifest(self):
        status, trace, output = base.AccountsTests().run_fake('login-retry-manual')
        self.assertEqual(status, 0, output)
        self.assertIn('MANUAL_RETRY_OK', output)
        self.assertEqual([row['account'] for row in trace if 'args' in row], ['main'])


class ProxyTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = {'profiles': {'second': str(self.root / 'second')}}
        patch = mock.patch.object(m, 'ROOT', self.root)
        patch.start()
        self.addCleanup(patch.stop)
        m.write_json(self.root / 'accounts.json', self.registry)

    def test_proxy_gets_bounded_native_timeout(self):
        with mock.patch.dict(os.environ, {**HOME, 'HTTPS_PROXY': 'http://127.0.0.1:17898'}, clear=True):
            env = m.profile_env(self.registry, 'second')
            self.assertEqual(env['API_TIMEOUT_MS'], '300000')
            self.assertEqual(env['CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS'], '180000')
            self.assertNotIn('API_TIMEOUT_MS', os.environ)

    def test_lowercase_proxy_is_supported(self):
        for key in ('https_proxy', 'http_proxy', 'all_proxy'):
            with self.subTest(key=key), mock.patch.dict(os.environ, {**HOME, key: 'http://127.0.0.1:17898'}, clear=True):
                env = m.profile_env(self.registry, 'second')
                self.assertEqual(env['API_TIMEOUT_MS'], '300000')
                self.assertEqual(env['CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS'], '180000')

    def test_all_proxy_is_supported(self):
        for key in ('HTTP_PROXY', 'ALL_PROXY'):
            with self.subTest(key=key), mock.patch.dict(os.environ, {**HOME, key: 'http://127.0.0.1:17898'}, clear=True):
                self.assertEqual(m.profile_env(self.registry, 'second')['API_TIMEOUT_MS'], '300000')

    def test_explicit_operator_timeout_is_preserved(self):
        for value in ('120000', '900000', ''):
            with self.subTest(value=value), mock.patch.dict(os.environ,
                    {**HOME, 'HTTPS_PROXY': 'http://localhost:17898', 'API_TIMEOUT_MS': value}, clear=True):
                env = m.profile_env(self.registry, 'second')
                self.assertEqual(env['API_TIMEOUT_MS'], value)
                self.assertNotIn('CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS', env)

    def test_explicit_first_byte_is_not_capped_by_an_injected_request_timeout(self):
        with mock.patch.dict(os.environ, {**HOME, 'HTTPS_PROXY': 'http://localhost:17898',
                'CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS': '600000'}, clear=True):
            env = m.profile_env(self.registry, 'second')
        self.assertEqual(env['CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS'], '600000')
        self.assertNotIn('API_TIMEOUT_MS', env)

    def test_explicit_timeout_pair_is_preserved(self):
        with mock.patch.dict(os.environ, {**HOME, 'HTTPS_PROXY': 'http://localhost:17898',
                'API_TIMEOUT_MS': '700000', 'CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS': '400000'}, clear=True):
            env = m.profile_env(self.registry, 'second')
        self.assertEqual(env['API_TIMEOUT_MS'], '700000')
        self.assertEqual(env['CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS'], '400000')

    def test_direct_connection_is_unchanged(self):
        with mock.patch.dict(os.environ, HOME, clear=True):
            env = m.profile_env(self.registry, 'second')
            self.assertNotIn('API_TIMEOUT_MS', env)
            self.assertNotIn('CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS', env)

    def test_disabled_account_cannot_get_an_environment(self):
        m.write_json(self.root / 'accounts.json', {**self.registry, 'disabled': {'second': {'reason': 'banned'}}})
        with self.assertRaises(RuntimeError):
            m.profile_env(self.registry, 'second')


if __name__ == '__main__':
    unittest.main()
