# Changelog

## 0.2.0

- Copy ignored environment files selected by Git-compatible `.worktreeinclude` patterns, with symlink and overwrite protection.
- Explicitly adopt external linked worktrees while retaining their branches after cleanup.
- Generate a lifecycle-only companion plugin for use alongside other worktree tools.
- Demonstrate managed and external-worktree recovery, including environment setup, binary files and separate staging.
- Explain the recovery use case and document comparisons and interoperability limits.

## 0.1.0

- Claude Code plugin with native worktree lifecycle hooks.
- Quiet local worker, session leases and process-aware preservation.
- Automatic cleanup of integrated work and configurable archival of stale unfinished work.
- Verified full archives containing Git history, staging and ignored/untracked working files.
- Restore into a new detached worktree, including when the original repository is gone.
- User-local policy, pause/resume, status, history and standalone CLI.
- MIT license and model-free real-Git integration tests.
