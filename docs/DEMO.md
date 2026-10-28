# Recovery demo

Run `python3 scripts/demo.py` from a checkout. To test enrollment of a worktree created by another tool, run `python3 scripts/demo.py --external`. Both demos use disposable repositories, require no Claude installation or API key, and make zero model calls.

```text
1. Created a worktree and copied ignored .env through .worktreeinclude.
2. Left staged and unstaged edits, an untracked binary and ignored config.
3. Cleanup policy archived the inactive work; verified the directory is gone.
   Restore command: /rescue:restore ad61aae93db84265 --to /new/path
4. Restored and verified: history, staging, working edits, binary and ignored config.
Original main: unchanged. Model calls: 0. Temporary data: removed on exit.
```

The archive ID varies per run. The shown restore command is illustrative: temporary demo archives are removed on exit. For real work, use an ID from `/rescue:archives`.

The demo advances the recorded inactivity timestamp by eight days instead of making you wait. It then invokes the actual sweep policy, verifies that the original worktree directory has disappeared, restores into a new worktree and compares the full file inventory and staged content. It verifies that the source branch did not move. External mode additionally verifies that the original external branch survives cleanup.

This proves the local recovery path; it does not simulate a live Claude session or benchmark disk savings.
