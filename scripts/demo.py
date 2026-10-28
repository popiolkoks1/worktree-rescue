#!/usr/bin/env python3
"""An isolated real-Git demonstration; no Claude installation or model calls."""
import tempfile
import time
from pathlib import Path

from rescue import Rescue, git, inventory


def main():
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
        record = rescue.create(repo, 'demo')
        path = Path(record['path'])
        (path / 'feature.txt').write_text('staged change\n')
        git(path, 'add', 'feature.txt')
        (path / 'feature.txt').write_text('unstaged change\n')
        (path / '.env').write_text('DEMO_TOKEN=synthetic-not-a-secret\n')
        before = inventory(path)
        state = rescue.state()
        state['worktrees'][record['id']]['last_activity'] = time.time() - 8 * 86400
        rescue.save(state)
        print('Created an isolated worktree with staged, unstaged and ignored files.')
        result = rescue.sweep()
        if result[0]['action'] != 'archived':
            raise RuntimeError(str(result))
        print('Archived and verified all work before removing the inactive worktree.')
        restored = root / 'restored'
        rescue.restore(result[0]['archive'], restored)
        assert inventory(restored) == before
        assert git(restored, 'show', ':feature.txt') == 'staged change'
        assert (restored / 'feature.txt').read_text() == 'unstaged change\n'
        assert (restored / '.env').exists()
        assert git(repo, 'rev-parse', 'main') == original_head
        print('Restored: Git history, staged change, unstaged change and ignored config.')
        print('Original main: unchanged. Model calls: 0. Temporary data: removed on exit.')


if __name__ == '__main__':
    main()
