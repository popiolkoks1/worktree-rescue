import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'rescue.py'
spec = importlib.util.spec_from_file_location('rescue', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RescueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='rescue integration ')
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'source repo'
        self.repo.mkdir()
        self.g('init', '-b', 'main')
        self.g('config', 'user.name', 'Test')
        self.g('config', 'user.email', 'test@example.invalid')
        self.g('config', 'commit.gpgsign', 'false')
        (self.repo / 'tracked.txt').write_text('original\n')
        (self.repo / '.gitignore').write_text('secret.env\n')
        self.g('add', 'tracked.txt', '.gitignore')
        self.g('commit', '-m', 'initial')
        self.source_head = self.g('rev-parse', 'main')
        self.rescue = module.Rescue(self.root / 'state')
        self.rescue.config.update(grace_seconds=0, minimum_free_bytes=0)
        self.create = lambda: self.rescue.create(self.repo, 'feature with spaces')
        self.env = dict(os.environ, WORKTREE_RESCUE_NO_WORKER='1')

    def tearDown(self):
        self.temp.cleanup()

    def g(self, *args, cwd=None):
        result = subprocess.run(['git', '-C', str(cwd or self.repo), *args], text=True, capture_output=True, check=True)
        return result.stdout.strip()

    def sweep(self):
        return self.rescue.sweep()

    def test_clean_finished_worktree_is_archived_and_main_is_untouched(self):
        record = self.create()
        result = self.sweep()
        self.assertEqual(result[0]['action'], 'archived', result)
        self.assertFalse(Path(record['path']).exists())
        self.assertEqual(self.g('rev-parse', 'main'), self.source_head)
        self.assertEqual(self.g('branch', '--show-current'), 'main')
        self.assertNotIn(record['branch'], self.g('branch', '--format=%(refname:short)'))
        self.assertTrue((self.rescue.home / 'archives' / result[0]['archive'] / 'commits.bundle').is_file())

    def test_unmerged_commits_are_kept_until_stale(self):
        record = self.create()
        path = Path(record['path'])
        (path / 'new.txt').write_text('unfinished')
        self.g('add', 'new.txt', cwd=path)
        self.g('commit', '-m', 'unfinished', cwd=path)
        self.assertEqual(self.sweep()[0]['action'], 'keep')
        self.assertTrue(path.is_dir())

    def test_integrated_commit_becomes_eligible(self):
        record = self.create()
        path = Path(record['path'])
        (path / 'new.txt').write_text('finished')
        self.g('add', 'new.txt', cwd=path)
        self.g('commit', '-m', 'finished', cwd=path)
        self.g('merge', '--ff-only', record['branch'])
        self.assertEqual(self.sweep()[0]['action'], 'archived')

    def test_staged_unstaged_binary_ignored_untracked_and_symlinks_restore(self):
        record = self.create()
        path = Path(record['path'])
        (path / 'tracked.txt').write_text('staged\n')
        self.g('add', 'tracked.txt', cwd=path)
        (path / 'tracked.txt').write_text('unstaged\n')
        (path / 'untracked.bin').write_bytes(bytes(range(256)))
        (path / 'secret.env').write_text('LOCAL_TOKEN=private-value\n')
        (path / 'empty-dir').mkdir()
        (path / 'outside-link').symlink_to(self.repo / 'tracked.txt')
        before = module.inventory(path)
        status = self.g('status', '--porcelain=v1', cwd=path)
        state = self.rescue.state()
        state['worktrees'][record['id']]['last_activity'] = time.time() - 8 * 86400
        self.rescue.save(state)
        result = self.sweep()
        self.assertEqual(result[0]['action'], 'archived', result)
        target = self.root / 'restored worktree'
        self.rescue.restore(result[0]['archive'], target)
        self.assertEqual(module.inventory(target), before)
        self.assertEqual(self.g('status', '--porcelain=v1', cwd=target), status)
        self.assertEqual(self.g('show', ':tracked.txt', cwd=target), 'staged')
        self.assertEqual((target / 'tracked.txt').read_text(), 'unstaged\n')
        self.assertTrue((target / 'outside-link').is_symlink())

    def test_restore_after_original_repository_is_deleted(self):
        record = self.create()
        result = self.sweep()
        shutil.rmtree(self.repo)
        target = self.root / 'recovered'
        self.rescue.restore(result[0]['archive'], target)
        self.assertEqual((target / 'tracked.txt').read_text(), 'original\n')
        self.assertEqual(self.g('rev-parse', 'HEAD', cwd=target), self.source_head)

    def test_dirty_work_is_not_cleaned_early(self):
        record = self.create()
        (Path(record['path']) / 'tracked.txt').write_text('uncommitted')
        self.assertEqual(self.sweep()[0]['action'], 'keep')

    def test_unknown_live_session_never_expires_based_on_age(self):
        record = self.rescue.create(self.repo, 'leased', 'session', None)
        state = self.rescue.state()
        state['worktrees'][record['id']]['last_activity'] = time.time() - 30 * 86400
        self.rescue.save(state)
        result = self.sweep()
        self.assertEqual(result[0]['action'], 'keep')
        self.assertIn('session', result[0]['reason'])

    def test_end_session_event_releases_lease(self):
        record = self.rescue.create(self.repo, 'leased', 'session', None)
        self.rescue.enqueue({'hook_event_name': 'SessionEnd', 'session_id': 'session'})
        self.assertEqual(self.sweep()[0]['action'], 'archived')

    def test_stop_is_not_session_end(self):
        record = self.create()
        self.rescue.enqueue({'hook_event_name': 'Stop', 'session_id': 'session', 'cwd': record['path']})
        self.assertEqual(self.sweep()[0]['action'], 'keep')

    def test_multiple_sessions_protect_same_worktree(self):
        record = self.create()
        for sid in ('one', 'two'):
            self.rescue.enqueue({'hook_event_name': 'SessionStart', 'session_id': sid, 'cwd': record['path']})
        self.rescue.enqueue({'hook_event_name': 'SessionEnd', 'session_id': 'one'})
        self.assertEqual(self.sweep()[0]['action'], 'keep')
        self.rescue.enqueue({'hook_event_name': 'SessionEnd', 'session_id': 'two'})
        self.assertEqual(self.sweep()[0]['action'], 'archived')

    def test_reused_pid_is_not_a_live_lease(self):
        record = self.rescue.create(self.repo, 'pid-reuse', 'session', {'pid': os.getpid(), 'token': 'not-the-current-process-start'})
        state = self.rescue.state()
        self.assertFalse(self.rescue.leased(state, record))
        self.assertFalse(state['sessions']['session']['active'])

    def test_running_process_blocks_cleanup(self):
        record = self.create()
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], cwd=record['path'])
        try:
            result = self.sweep()
            self.assertEqual(result[0]['action'], 'keep', result)
            self.assertIn(str(process.pid), result[0]['reason'])
        finally:
            process.terminate()
            process.wait()

    def test_unavailable_process_probe_preserves_worktree(self):
        record = self.create()
        with patch.object(module, 'busy_processes', side_effect=module.RescueError('probe unavailable')):
            result = self.sweep()
        self.assertEqual(result[0]['action'], 'keep')
        self.assertTrue(Path(record['path']).is_dir())

    def test_grace_period_blocks_immediate_cleanup(self):
        self.create()
        self.rescue.config['grace_seconds'] = 600
        self.assertEqual(self.sweep()[0]['action'], 'keep')

    def test_pause_prevents_sweeps(self):
        record = self.create()
        self.rescue.config['enabled'] = False
        self.assertEqual(self.sweep()[0]['action'], 'keep')
        self.assertTrue(Path(record['path']).is_dir())

    def test_archive_budget_preserves_original(self):
        record = self.create()
        self.rescue.config['archive_budget_bytes'] = 0
        result = self.sweep()
        self.assertEqual(result[0]['action'], 'keep')
        self.assertTrue(Path(record['path']).is_dir())

    def test_ownership_marker_and_branch_changes_are_rejected(self):
        record = self.create()
        marker = Path(record['gitdir']) / 'worktree-rescue.json'
        marker.unlink()
        self.assertEqual(self.sweep()[0]['action'], 'keep')
        module.atomic_json(marker, {'id': record['id']})
        self.g('switch', '-c', 'someone-elses-branch', cwd=record['path'])
        self.assertEqual(self.sweep()[0]['action'], 'keep')

    def test_primary_checkout_and_unregistered_directories_are_never_removed(self):
        fake = self.create()
        fake['path'] = str(self.repo)
        with self.assertRaises(module.RescueError):
            self.rescue.validate(fake)
        with self.assertRaises(module.RescueError):
            module.handle_hook(self.rescue, {'hook_event_name': 'WorktreeRemove', 'worktree_path': str(self.repo)})
        self.assertEqual(self.g('rev-parse', 'main'), self.source_head)

    def test_nested_repository_is_preserved(self):
        record = self.create()
        nested = Path(record['path']) / 'nested'
        nested.mkdir()
        self.g('init', cwd=nested)
        state = self.rescue.state()
        state['worktrees'][record['id']]['last_activity'] = time.time() - 8 * 86400
        self.rescue.save(state)
        result = self.sweep()
        self.assertEqual(result[0]['action'], 'keep')
        self.assertIn('Nested', result[0]['reason'])

    def test_symlink_replacement_is_preserved(self):
        record = self.create()
        path = Path(record['path'])
        moved = path.with_name(path.name + '-moved')
        path.rename(moved)
        path.symlink_to(self.repo, target_is_directory=True)
        result = self.sweep()
        self.assertEqual(result[0]['action'], 'keep')
        self.assertTrue((self.repo / 'tracked.txt').exists())

    def test_dry_run_does_not_remove_or_archive(self):
        record = self.create()
        result = self.rescue.sweep(dry_run=True)
        self.assertEqual(result[0]['action'], 'archive')
        self.assertTrue(Path(record['path']).exists())
        self.assertEqual(list((self.rescue.home / 'archives').glob('*/manifest.json')), [])

    def test_modified_archive_is_rejected(self):
        self.create()
        result = self.sweep()
        archive = self.rescue.home / 'archives' / result[0]['archive'] / 'files.tar.gz'
        with archive.open('ab') as file:
            file.write(b'corruption')
        with self.assertRaisesRegex(module.RescueError, 'integrity'):
            self.rescue.restore(result[0]['archive'], self.root / 'restore')

    def test_restore_never_overwrites_existing_path(self):
        self.create()
        result = self.sweep()
        with self.assertRaisesRegex(module.RescueError, 'new path'):
            self.rescue.restore(result[0]['archive'], self.repo)

    def test_changed_content_during_archive_is_preserved(self):
        record = self.create()
        original = module.inventory
        calls = 0
        def racing(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                (Path(path) / 'tracked.txt').write_text('concurrent edit')
            return original(path)
        with patch.object(module, 'inventory', side_effect=racing):
            result = self.sweep()
        self.assertEqual(result[0]['action'], 'keep')
        self.assertEqual((Path(record['path']) / 'tracked.txt').read_text(), 'concurrent edit')

    def test_hook_path_stdout_and_silent_lifecycle(self):
        payload = {'hook_event_name': 'WorktreeCreate', 'session_id': 'cli-session', 'cwd': str(self.repo), 'name': 'hook-test'}
        command = [sys.executable, str(SCRIPT), '--home', str(self.rescue.home), 'hook']
        created = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True, env=self.env, check=True)
        self.assertEqual(created.stderr, '')
        self.assertTrue(Path(created.stdout.strip()).is_dir())
        payload.update(hook_event_name='SessionEnd', cwd=created.stdout.strip())
        ended = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True, env=self.env, check=True)
        self.assertEqual(ended.stdout, '')
        self.assertEqual(ended.stderr, '')

    def test_session_event_does_not_record_prompts_or_tool_results(self):
        payload = {'hook_event_name': 'SessionStart', 'session_id': 's', 'cwd': str(self.repo), 'prompt': 'SECRET_PROMPT', 'tool_response': 'SECRET_RESPONSE'}
        command = [sys.executable, str(SCRIPT), '--home', str(self.rescue.home), 'hook']
        subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True, env=self.env, check=True)
        contents = ''.join(file.read_text() for file in (self.rescue.home / 'events').glob('*.json'))
        self.assertNotIn('SECRET_', contents)

    def test_invalid_config_is_rejected(self):
        module.atomic_json(self.rescue.home / 'config.json', {'grace_seconds': -1})
        with self.assertRaises(module.RescueError):
            module.Rescue(self.rescue.home)

    def test_one_parent_session_protects_multiple_subagent_worktrees(self):
        first = self.rescue.create(self.repo, 'first', 'parent-session', None)
        second = self.rescue.create(self.repo, 'second', 'parent-session', None)
        self.assertTrue(all(row['action'] == 'keep' for row in self.sweep()))
        self.rescue.enqueue({'hook_event_name': 'SessionEnd', 'session_id': 'parent-session'})
        self.assertTrue(all(row['action'] == 'archived' for row in self.sweep()))
        self.assertFalse(Path(first['path']).exists())
        self.assertFalse(Path(second['path']).exists())

    def test_native_remove_archives_before_deleting(self):
        record = self.rescue.create(self.repo, 'native-removal', 'requester', None)
        path = Path(record['path'])
        (path / 'tracked.txt').write_text('unfinished change')
        module.handle_hook(self.rescue, {'hook_event_name': 'WorktreeRemove', 'session_id': 'requester', 'worktree_path': str(path)})
        self.assertFalse(path.exists())
        archives = list((self.rescue.home / 'archives').glob('*/manifest.json'))
        self.assertEqual(len(archives), 1)
        ident = json.loads(archives[0].read_text())['id']
        target = self.root / 'native-restored'
        self.rescue.restore(ident, target)
        self.assertEqual((target / 'tracked.txt').read_text(), 'unfinished change')

    def test_native_remove_preserves_other_session(self):
        record = self.rescue.create(self.repo, 'native-removal', 'requester', None)
        self.rescue.enqueue({'hook_event_name': 'SessionStart', 'session_id': 'other', 'cwd': record['path']})
        with self.assertRaisesRegex(module.RescueError, 'session'):
            module.handle_hook(self.rescue, {'hook_event_name': 'WorktreeRemove', 'session_id': 'requester', 'worktree_path': record['path']})
        self.assertTrue(Path(record['path']).exists())

    def test_git_post_checkout_hook_does_not_run(self):
        hook = self.repo / '.git' / 'hooks' / 'post-checkout'
        marker = self.root / 'hook-ran'
        hook.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
        hook.chmod(0o755)
        self.create()
        self.assertFalse(marker.exists())

    def test_background_worker_archives_without_manual_sweep(self):
        module.atomic_json(self.rescue.home / 'config.json', dict(module.DEFAULTS, grace_seconds=0, poll_seconds=1, minimum_free_bytes=0))
        record = self.create()
        process = subprocess.Popen([sys.executable, str(SCRIPT), '--home', str(self.rescue.home), 'worker'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.time() + 20
            while Path(record['path']).exists() and time.time() < deadline:
                time.sleep(0.1)
            self.assertFalse(Path(record['path']).exists(), 'worker did not clean up')
            worker = module.read_json(self.rescue.home / 'worker.json')
            self.assertEqual(worker['pid'], process.pid)
            duplicate = subprocess.run([sys.executable, str(SCRIPT), '--home', str(self.rescue.home), 'worker'], capture_output=True, timeout=5)
            self.assertEqual(duplicate.returncode, 0)
            self.assertEqual(module.read_json(self.rescue.home / 'worker.json')['pid'], process.pid)
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stderr.close()


    def test_missing_integration_ref_preserves_worktree(self):
        record = self.create()
        state = self.rescue.state()
        state['worktrees'][record['id']]['base_ref'] = 'refs/heads/missing-integration'
        self.rescue.save(state)
        result = self.sweep()
        self.assertEqual(result[0]['action'], 'keep')
        self.assertIn('unavailable', result[0]['reason'])

    def test_restore_rejects_traversal_even_if_checksums_are_recomputed(self):
        import tarfile
        self.create()
        result = self.sweep()
        archive_id = result[0]['archive']
        directory = self.rescue.home / 'archives' / archive_id
        with tarfile.open(directory / 'files.tar.gz', 'w:gz') as archive:
            member = tarfile.TarInfo('../outside.txt')
            member.size = 7
            archive.addfile(member, io.BytesIO(b'payload'))
        metadata = module.read_json(directory / 'manifest.json')
        metadata['files_sha256'] = module.sha256(directory / 'files.tar.gz')
        module.atomic_json(directory / 'manifest.json', metadata)
        with self.assertRaisesRegex(module.RescueError, 'Unsafe'):
            self.rescue.restore(archive_id, self.root / 'restored')
        self.assertFalse((self.root / 'outside.txt').exists())

    def test_restore_rejects_writes_through_symlink_parent(self):
        import tarfile
        self.create()
        result = self.sweep()
        archive_id = result[0]['archive']
        directory = self.rescue.home / 'archives' / archive_id
        with tarfile.open(directory / 'files.tar.gz', 'w:gz') as archive:
            link = tarfile.TarInfo('redirect')
            link.type = tarfile.SYMTYPE
            link.linkname = str(self.root)
            archive.addfile(link)
            member = tarfile.TarInfo('redirect/outside.txt')
            member.size = 7
            archive.addfile(member, io.BytesIO(b'payload'))
        metadata = module.read_json(directory / 'manifest.json')
        metadata['files_sha256'] = module.sha256(directory / 'files.tar.gz')
        module.atomic_json(directory / 'manifest.json', metadata)
        with self.assertRaisesRegex(module.RescueError, 'symlink'):
            self.rescue.restore(archive_id, self.root / 'restored')
        self.assertFalse((self.root / 'outside.txt').exists())

    def test_environment_uses_git_patterns_and_copies_only_ignored_files(self):
        (self.repo / '.gitignore').write_text('secret.env\n.env*\nlocal/\n')
        (self.repo / '.worktreeinclude').write_text('.env*\nlocal/**\n!local/skip.txt\ntracked.txt\n')
        (self.repo / '.env').write_text('LOCAL=example\n')
        (self.repo / 'local').mkdir()
        (self.repo / 'local' / 'keep.txt').write_text('local data')
        (self.repo / 'local' / 'skip.txt').write_text('excluded')
        record = self.create()
        path = Path(record['path'])
        self.assertEqual((path / '.env').read_text(), 'LOCAL=example\n')
        self.assertEqual((path / 'local' / 'keep.txt').read_text(), 'local data')
        self.assertFalse((path / 'local' / 'skip.txt').exists())
        self.assertEqual((path / 'tracked.txt').read_text(), 'original\n')

    def test_environment_symlink_is_refused_and_creation_rolled_back(self):
        (self.repo / '.worktreeinclude').write_text('secret.env\n')
        outside = self.root / 'outside'
        outside.write_text('private')
        (self.repo / 'secret.env').symlink_to(outside)
        with self.assertRaisesRegex(module.RescueError, 'symlink'):
            self.create()
        self.assertFalse(self.rescue.state()['worktrees'])
        self.assertEqual(list((self.rescue.home / 'worktrees').iterdir()), [])
        self.assertEqual(self.g('branch', '--format=%(refname:short)'), 'main')

    def test_adopt_external_archive_restore_and_retain_branch(self):
        path = self.root / 'external'
        self.g('worktree', 'add', '-b', 'other-tool/task', str(path))
        (path / 'tracked.txt').write_text('staged\n')
        self.g('add', 'tracked.txt', cwd=path)
        (path / 'tracked.txt').write_text('working\n')
        (path / 'secret.env').write_text('LOCAL=example')
        before = module.inventory(path)
        record = self.rescue.adopt(path, 'main')
        self.assertTrue(record['external'])
        self.assertEqual(self.rescue.adopt(path, 'main')['id'], record['id'])
        state = self.rescue.state()
        state['worktrees'][record['id']]['last_activity'] -= 8 * 86400
        self.rescue.save(state)
        result = self.sweep()[0]
        self.assertEqual(result['action'], 'archived', result)
        self.assertFalse(path.exists())
        self.assertIn('other-tool/task', self.g('branch', '--format=%(refname:short)'))
        restored = self.root / 'recovered-external'
        self.rescue.restore(result['archive'], restored)
        self.assertEqual(module.inventory(restored), before)
        self.assertEqual(self.g('show', ':tracked.txt', cwd=restored), 'staged')

    def test_adopt_refuses_main_subdirectory_and_same_base(self):
        with self.assertRaisesRegex(module.RescueError, 'primary'):
            self.rescue.adopt(self.repo, 'main')
        path = self.root / 'external'
        self.g('worktree', 'add', '-b', 'external', str(path))
        (path / 'nested').mkdir()
        with self.assertRaisesRegex(module.RescueError, 'root'):
            self.rescue.adopt(path / 'nested', 'main')
        with self.assertRaisesRegex(module.RescueError, 'different'):
            self.rescue.adopt(path, 'external')
        self.assertFalse(self.rescue.state()['worktrees'])

    def test_adopt_external_session_lease_blocks_cleanup(self):
        path = self.root / 'external'
        self.g('worktree', 'add', '-b', 'external', str(path))
        self.rescue.adopt(path, 'main')
        self.rescue.enqueue(dict(hook_event_name='SessionStart', session_id='external', cwd=str(path)))
        self.assertEqual(self.sweep()[0]['action'], 'keep')
        self.rescue.enqueue(dict(hook_event_name='SessionEnd', session_id='external', cwd=str(path)))
        self.assertEqual(self.sweep()[0]['action'], 'archived')

    def test_companion_has_only_lifecycle_hooks_and_can_run(self):
        target = self.root / 'companion'
        subprocess.run([sys.executable, str(SCRIPT), '--home', str(self.rescue.home),
                        'companion', '--to', str(target)], check=True, capture_output=True)
        hooks = json.loads((target / 'hooks' / 'hooks.json').read_text())['hooks']
        self.assertNotIn('WorktreeCreate', hooks)
        self.assertNotIn('WorktreeRemove', hooks)
        self.assertIn('SessionEnd', hooks)
        self.assertTrue((target / 'LICENSE').is_file())
        self.assertTrue((target / 'README.md').is_file())
        self.assertEqual(json.loads((target / '.claude-plugin' / 'plugin.json').read_text())['license'], 'MIT')
        self.assertTrue((target / 'commands' / 'restore.md').exists())
        subprocess.run([sys.executable, str(target / 'scripts' / 'rescue.py'),
                        '--home', str(self.rescue.home), 'status'], check=True, capture_output=True)


if __name__ == '__main__':
    unittest.main()
