# Recovery demo

Run `python3 scripts/demo.py` from a checkout. The demo runs real Git operations against disposable data; it does not need Claude Code or an AI account.

```text
Created an isolated worktree with staged, unstaged and ignored files.
Archived and verified all work before removing the inactive worktree.
Restored: Git history, staged change, unstaged change and ignored config.
Original main: unchanged. Model calls: 0. Temporary data: removed on exit.
```

The script verifies restored content against a SHA-256 inventory, checks the staged version separately from the working version and verifies that source main did not move. It is a recovery demonstration, not a benchmark of reclaimed space or AI agent behavior.
