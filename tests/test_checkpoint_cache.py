import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_claude_accounts as base

m = base.m


class CheckpointCacheTests(unittest.TestCase):
    setUp = base.ContextTests.setUp
    tearDown = base.ContextTests.tearDown
    checkpoint = base.ContextTests.checkpoint

    def test_unchanged_files_reuse_verified_objects_without_opening_file_data(self):
        archive = self.root/'archive'
        first = m.archive_file(self.agent, archive)
        with mock.patch.object(m.os, 'open', side_effect=AssertionError('unchanged data must not be reread or rewritten')):
            again = m.archive_file(self.agent, archive)
        self.assertEqual(first, again)

    def test_cold_cache_deduplicates_existing_object_without_temporary_copy(self):
        archive = self.root/'archive'
        first = m.archive_file(self.agent, archive)
        m._CONTEXT_ARCHIVES.clear()
        m._CONTEXT_HASHES.clear()
        with mock.patch.object(m.tempfile, 'mkstemp', side_effect=AssertionError('already archived bytes must not be written again')):
            again = m.archive_file(self.agent, archive)
        self.assertEqual(first, again)

    def test_rewrite_with_same_size_and_restored_mtime_invalidates_source_proof(self):
        checkpoint = self.checkpoint()
        before = self.agent.stat()
        original = self.agent.read_bytes()
        changed = original.replace(b'original', b'tampered')
        self.assertEqual(len(changed), len(original))
        self.agent.write_bytes(changed)
        os.utime(self.agent, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(m.ContextNotReady):
            m.verify_checkpoint(checkpoint, live=True)
        fresh = self.checkpoint()
        self.assertNotEqual(json.loads(checkpoint.read_text(encoding='utf-8'))['files'], json.loads(fresh.read_text(encoding='utf-8'))['files'])

    def test_rewrite_with_restored_mtime_invalidates_backup_proof(self):
        checkpoint = self.checkpoint()
        item = next(i for i in m.read_json(checkpoint)['files'] if i['source'] == str(self.agent))
        backup = checkpoint.parents[2]/'objects'/item['sha256']
        before = backup.stat()
        backup.write_bytes(backup.read_bytes().replace(b'original', b'tampered'))
        os.utime(backup, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(m.ContextNotReady):
            m.arm_recovery(self.run, checkpoint)
        self.assertFalse((self.run/'pending-recovery.json').exists())

    def test_cached_object_missing_or_symlink_is_not_silently_replaced(self):
        archive = self.root/'archive'
        item = m.archive_file(self.agent, archive)
        backup = archive/'objects'/item['sha256']
        backup.unlink()
        with self.assertRaises(m.ContextNotReady):
            m.archive_file(self.agent, archive)
        backup.symlink_to(self.agent)
        with self.assertRaises(m.ContextNotReady):
            m.archive_file(self.agent, archive)

    def test_atomic_file_replacement_invalidates_source_proof(self):
        checkpoint = self.checkpoint()
        before = self.agent.stat()
        replacement = self.agent.with_suffix('.new')
        replacement.write_bytes(self.agent.read_bytes().replace(b'original', b'tampered'))
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(self.agent)
        with self.assertRaises(m.ContextNotReady):
            m.verify_checkpoint(checkpoint, live=True)

    def test_cached_source_cannot_be_replaced_by_symlink(self):
        archive = self.root/'archive'
        m.archive_file(self.agent, archive)
        replacement = self.agent.with_suffix('.copy')
        replacement.write_bytes(self.agent.read_bytes())
        self.agent.unlink()
        self.agent.symlink_to(replacement)
        with self.assertRaises(m.ContextNotReady):
            m.archive_file(self.agent, archive)

    def test_prefix_hash_extends_correctly_and_append_does_not_trust_old_prefix(self):
        path = self.root/'large.bin'
        data = b'original-data-'*10000
        path.write_bytes(data)
        prefix = len(data)//2
        self.assertEqual(m.context_hash(path, prefix), hashlib.sha256(data[:prefix]).hexdigest())
        self.assertEqual(m.context_hash(path), hashlib.sha256(data).hexdigest())
        path.write_bytes(b'X'+data[1:]+b'appended')
        self.assertNotEqual(m.context_hash(path, prefix), hashlib.sha256(data[:prefix]).hexdigest())
        self.assertEqual(m.context_hash(path), hashlib.sha256(path.read_bytes()).hexdigest())

    def test_change_during_hash_is_rejected(self):
        real_fstat = m.os.fstat
        def mutate(fd):
            with self.agent.open('ab') as stream:
                stream.write(b'{}\n')
            return real_fstat(fd)
        with mock.patch.object(m.os, 'fstat', side_effect=mutate), self.assertRaises(m.ContextNotReady):
            m.context_hash(self.agent)

    def test_historical_json_error_permission_does_not_leak_through_cache(self):
        archive = self.root/'archive'
        self.transcript.write_bytes(b'{}\nbroken historical row\n{}\n')
        item = m.archive_file(self.transcript, archive, allow_historical_errors=True)
        self.assertEqual(item['historical_json_errors'], 1)
        with self.assertRaises(m.ContextNotReady):
            m.archive_file(self.transcript, archive, allow_historical_errors=False)

    def test_appended_partial_record_is_not_hidden_by_cache(self):
        self.checkpoint()
        with self.agent.open('ab') as stream:
            stream.write(b'{"unfinished":')
        with self.assertRaises(m.ContextNotReady):
            self.checkpoint()

    def test_agent_failure_metadata_updates_with_changed_history(self):
        self.agent.write_text(json.dumps({'type':'assistant','isApiErrorMessage':True,'error':'rate_limit'})+'\n')
        self.state['agents']['original']['state'] = 'stopped'
        first = m.read_json(self.checkpoint())
        self.assertEqual(first['agents']['original']['state'], 'failed')
        self.agent.write_text(json.dumps({'type':'assistant','message':{'content':'recovered'}})+'\n')
        second = m.read_json(self.checkpoint())
        self.assertEqual(second['agents']['original']['state'], 'stopped')

    def test_second_checkpoint_reuses_files_but_archives_final_flush(self):
        first = m.read_json(self.checkpoint())
        with self.transcript.open('ab') as stream:
            stream.write(b'{"type":"system","data":"final flush"}\n')
        second_path = self.checkpoint()
        second = m.verify_checkpoint(second_path, live=True)
        self.assertEqual(second['checkpoint_stats']['reused_files'], len(first['files'])-1)
        self.assertEqual(second['checkpoint_stats']['written_bytes'], self.transcript.stat().st_size)
        self.assertEqual(second['session_id'], first['session_id'])
        self.assertEqual(second['agents'], first['agents'])


if __name__ == '__main__':
    unittest.main()
