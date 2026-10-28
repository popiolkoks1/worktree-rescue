#!/usr/bin/env python3
"""An isolated real-Git demonstration; no Claude installation or model calls."""
import argparse
import tempfile
import time
from pathlib import Path

from rescue import Rescue, git, inventory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--external", action="store_true", help="Recover a worktree created by another tool")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='rescue-demo-') as temp:
        root = Path(temp).resolve()
        repo = root / 'project'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.name', 'Demo')
        git(repo, 'config', 'user.email', 'demo@example.invalid')
        git(repo, 'config', 'commit.gpgsign', 'false')
        (repo / 'feature.txt').write_text('original\n')
        (repo / '.gitignore').write_text('.env\n')
        git(repo, 'add', 'feature.txt', '.gitignore')
        git(repo, 'commit', '-m', 'Initial project')
        original_head = git(repo, 'rev-parse', 'HEAD')
        rescue = Rescue(root / 'private-state')
        rescue.config.update(grace_seconds=0, minimum_free_bytes=0)
        (repo / '.worktreeinclude').write_text('.env\n')
        (repo / '.env').write_text('DEMO_TOKEN=synthetic-not-a-secret\n')
        if args.external:
            external = root / 'other-tool-worktree'
            git(repo, 'worktree', 'add', '-b', 'other-tool/task', str(external))
            (external / '.env').write_bytes((repo / '.env').read_bytes())
            record = rescue.adopt(external, 'main')
            print('1. Enrolled a worktree created by another tool; its branch will be retained.')
        else:
            record = rescue.create(repo, 'demo')
            print('1. Created a worktree and copied ignored .env through .worktreeinclude.')
        path = Path(record['path'])
        (path / 'feature.txt').write_text('staged change\n')
        git(path, 'add', 'feature.txt')
        (path / 'feature.txt').write_text('unstaged change\n')
        assert (path / '.env').read_bytes() == (repo / '.env').read_bytes()
        (path / 'unfinished.bin').write_bytes(bytes(range(256)))
        before = inventory(path)
        state = rescue.state()
        state['worktrees'][record['id']]['last_activity'] = time.time() - 8 * 86400
        rescue.save(state)
        print('2. Left staged and unstaged edits, an untracked binary and ignored config.')
        result = rescue.sweep()
        if result[0]['action'] != 'archived':
            raise RuntimeError(str(result))
        assert not path.exists()
        print('3. Cleanup policy archived the inactive work; verified the directory is gone.')
        print('   Restore command: /rescue:restore ' + result[0]['archive'] + ' --to /new/path')
        restored = root / 'restored'
        rescue.restore(result[0]['archive'], restored)
        assert inventory(restored) == before
        assert git(restored, 'show', ':feature.txt') == 'staged change'
        assert (restored / 'feature.txt').read_text() == 'unstaged change\n'
        assert (restored / '.env').exists()
        assert git(repo, 'rev-parse', 'main') == original_head
        print('4. Restored and verified: history, staging, working edits, binary and ignored config.')
        if args.external:
            assert git(repo, 'rev-parse', 'refs/heads/other-tool/task') == original_head
            print('   External branch: retained.')
        print('Original main: unchanged. Model calls: 0. Temporary data: removed on exit.')


if __name__ == '__main__':
    main()
