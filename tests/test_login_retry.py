import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_claude_accounts as base

m = base.m
BUSY = 'Could not refresh your login because another Claude Code process is refreshing it (or exited mid-refresh) · Try again in a minute; if it keeps happening, close other Claude Code windows or sign in again with /login'


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / 'run'
        self.sid, self.client = '11111111-1111-4111-8111-111111111111', 'test-client'
        self.registry = {'profiles': {'second': str(self.root / 'second')}}
        self.root_patch = mock.patch.object(m, 'ROOT', self.root)
        self.root_patch.start()
        m.write_json(self.root / 'accounts.json', self.registry)
        self.recovery = {'session_id': self.sid, 'manifest': 'test-manifest', 'verified': True}
        m.write_json(self.run / 'pending-recovery.json', self.recovery)
        self.state = {'session_id': self.sid, 'account': 'second', 'client_id': self.client,
                      'agents': {}, 'pending_tools': {}, 'background_tasks': []}
        m.write_json(m.session_state_path(self.run, self.sid), self.state)
        self.retry = m.RecoveryRetry(self.run, 'second', self.client, self.sid)

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def event(self, kind, **kwargs):
        return {'account': 'second', 'client_id': self.client, 'session_id': self.sid,
                'event': kind, **kwargs}

    def observe(self, kind, **kwargs):
        self.retry.observe(self.event(kind, **kwargs))

    def start(self):
        self.observe('SessionStart')
        self.observe('UserPromptSubmit')

    def failure(self):
        self.observe('StopFailure', error='server_error', message=BUSY)

    def test_exact_error_only(self):
        self.assertTrue(m.login_refresh_busy({'error': 'server_error', 'message': BUSY}))
        for extra in ({'error': 'rate_limit'}, {'error': 'authentication_failed'},
                      {'agent_id': 'worker'}, {'origin_agent_id': 'worker'},
                      {'details': 'Your account has been disabled.'}, {'details': '[cyber]'}):
            with self.subTest(extra=extra):
                self.assertFalse(m.login_refresh_busy({'error': 'server_error', 'message': BUSY, **extra}))
        for text in ('API Error: No response from API', '401 Unauthorized', 'Refresh token expired', '429 Too many requests'):
            self.assertFalse(m.login_refresh_busy({'error': 'server_error', 'message': text}))

    def test_terminal_replies_are_not_keyboard_input(self):
        for data in (b'\x1b[?1;2c', b'\x1b[>0;276;0c', b'\x1b[24;80R', b'\x1b[?1u',
                     b'\x1b]11;rgb:ffff/ffff/ffff\x07', b'\x1b]10;rgb:0000/0000/0000\x1b\\'):
            self.assertTrue(m.terminal_response_only(data), repr(data))
            self.assertFalse(m.terminal_response_only(data + b'x'), repr(data))
        for data in (b'\x1b', b'\x03', b'x', b'\r', b'\x1b[A', b'\x1b[200~draft\x1b[201~'):
            self.assertFalse(m.terminal_response_only(data), repr(data))

    def test_invalid_state_preserves_process_and_cancels_retry(self):
        self.start()
        self.failure()
        m.session_state_path(self.run, self.sid).write_text('{')
        self.assertFalse(self.retry.can_submit(self.registry))
        self.assertEqual(self.retry.phase, 'cancelled')

    def test_no_retry_without_verified_resume(self):
        for sid in (None, 'different-session'):
            retry = m.RecoveryRetry(self.run, 'second', self.client, sid)
            self.assertFalse(retry.active)
        m.write_json(self.run / 'pending-recovery.json', {**self.recovery, 'verified': False})
        self.start()
        self.failure()
        self.assertEqual(self.retry.phase, 'cancelled')

    def test_no_retry_before_automatic_prompt_is_confirmed(self):
        self.observe('SessionStart')
        self.failure()
        self.assertEqual(self.retry.phase, 'cancelled')

    def test_delays_and_budget_are_bounded(self):
        self.start()
        with mock.patch.object(m.time, 'monotonic', return_value=1000), mock.patch.object(m.time, 'time', return_value=2000):
            self.failure()
            self.assertEqual(self.retry.deadline, 1060)
            self.assertFalse(self.retry.due())
            self.assertEqual(m.read_json(self.run / 'retry-status.json')['next_at'], 2060)
        for now, next_deadline in ((1060, 1180), (1180, None)):
            with mock.patch.object(m.time, 'monotonic', return_value=now):
                self.assertTrue(self.retry.can_submit(self.registry))
                self.retry.submitted()
                self.observe('UserPromptSubmit')
                self.failure()
                self.assertEqual(self.retry.deadline, next_deadline)
        self.assertEqual(self.retry.phase, 'exhausted')
        self.assertEqual(self.retry.attempts, 2)
        self.failure()
        self.assertEqual(self.retry.phase, 'exhausted')

    def test_duplicate_error_does_not_extend_wait(self):
        self.start()
        self.failure()
        deadline = self.retry.deadline
        self.failure()
        self.assertEqual(self.retry.deadline, deadline)
        self.assertEqual(self.retry.attempts, 0)

    def test_keyboard_cancels_permanently(self):
        self.start()
        self.failure()
        self.retry.finish('keyboard-input')
        self.failure()
        self.assertFalse(self.retry.active)
        self.assertFalse(self.retry.due())

    def test_new_prompt_cancels(self):
        self.start()
        self.failure()
        self.observe('UserPromptSubmit')
        self.assertEqual(self.retry.phase, 'cancelled')

    def test_progress_disarms_retry(self):
        for event in ('PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'SubagentStart', 'Stop'):
            with self.subTest(event=event):
                self.retry = m.RecoveryRetry(self.run, 'second', self.client, self.sid)
                self.start()
                self.failure()
                self.observe(event)
                self.failure()
                self.assertEqual(self.retry.phase, 'completed')

    def test_other_failures_and_exit_disarm(self):
        for event in (self.event('SessionEnd'), self.event('StopFailure', error='rate_limit', message="You've hit your session limit"),
                      self.event('StopFailure', error='server_error', message='API Error: No response from API')):
            with self.subTest(event=event):
                self.retry = m.RecoveryRetry(self.run, 'second', self.client, self.sid)
                self.start()
                self.failure()
                self.retry.observe(event)
                self.assertEqual(self.retry.phase, 'cancelled')

    def test_old_client_cannot_arm_or_cancel(self):
        self.start()
        self.observe('StopFailure', client_id='old-client', error='server_error', message=BUSY)
        self.assertEqual(self.retry.phase, 'armed')
        self.failure()
        self.observe('Stop', client_id='old-client')
        self.assertEqual(self.retry.phase, 'waiting')

    def test_session_change_disarms(self):
        self.start()
        self.failure()
        self.observe('SessionStart', session_id='another-session')
        self.assertEqual(self.retry.phase, 'cancelled')

    def test_missing_retry_receipt_cannot_cause_second_submit(self):
        self.start()
        self.failure()
        self.retry.submitted()
        self.failure()
        self.assertEqual(self.retry.phase, 'cancelled')

    def test_activity_or_identity_change_prevents_submission(self):
        for change in ({'agents': {'worker': {'state': 'running'}}}, {'pending_tools': {'tool': {}}},
                       {'client_id': 'new-client'}, {'account': 'different-account'}, {'ended_at': 1}):
            with self.subTest(change=change):
                self.retry = m.RecoveryRetry(self.run, 'second', self.client, self.sid)
                self.start()
                self.failure()
                self.retry.deadline = 0
                m.write_json(m.session_state_path(self.run, self.sid), {**self.state, **change})
                self.assertFalse(self.retry.can_submit(self.registry))
                self.assertEqual(self.retry.phase, 'cancelled')

    def test_disabled_account_prevents_submission(self):
        self.start()
        self.failure()
        self.retry.deadline = 0
        m.write_json(self.root / 'accounts.json', {**self.registry, 'disabled': {'second': {'reason': 'disabled'}}})
        self.assertFalse(self.retry.can_submit(self.registry))


class RetryPtyTests(unittest.TestCase):
    def test_retry_twice_after_rotation_without_user_input(self):
        status, trace, output = base.AccountsTests().run_fake('login-retry-success')
        self.assertEqual(status, 0, output)
        self.assertEqual([r['account'] for r in trace if 'args' in r], ['main', 'second'])
        self.assertEqual([r['retry'] for r in trace if 'retry' in r], [1, 2])
        self.assertEqual(len({r['session_id'] for r in trace}), 1)
        self.assertIn('RESUME_OK', output)

    def test_no_third_retry(self):
        status, trace, output = base.AccountsTests().run_fake('login-retry-exhausted')
        self.assertEqual(status, 0, output)
        self.assertEqual([r['retry'] for r in trace if 'retry' in r], [1, 2])
        self.assertIn('BUDGET_OK', output)

    def test_keyboard_draft_cancels_before_enter(self):
        status, trace, output = base.AccountsTests().run_fake('login-retry-keyboard')
        self.assertEqual(status, 0, output)
        self.assertEqual([r for r in trace if 'retry' in r], [])
        self.assertIn('CANCEL_OK', output)


if __name__ == '__main__':
    unittest.main()
