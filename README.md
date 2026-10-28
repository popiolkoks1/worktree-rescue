# Worktree Rescue

<img src=".claude-plugin/icon.png" alt="Worktree Rescue" width="96" height="96">

**Let Claude finish. Keep the work. Lose the leftovers.**

An open-source Claude Code plugin that quietly archives and cleans up finished Git worktrees. No account, cloud service, model calls or telemetry. Your source checkout stays where it is.

```text
Claude creates a worktree → Rescue registers ownership
You work normally          → active sessions protect the worktree
The task ends              → Rescue checks Git and running processes
Cleanup is eligible        → verified archive → remove worktree
Need it back?              → /rescue:restore ARCHIVE_ID --to /new/path
```

Every removal creates a verified local recovery archive first. It contains history reachable from the worktree HEAD, staged changes, working files, ignored files and symlinks. Archives are never automatically deleted.

## Install in Claude Code

Requires **macOS or Linux**, Python 3.9+, Git, `ps`, and `lsof` on macOS. Use **Claude Code 2.1.300 or later** so plugin WorktreeCreate hooks run for `claude --worktree` sessions. Windows is not supported in this release.

```text
/plugin marketplace add popiolkoks1/worktree-rescue
/plugin install rescue@worktree-rescue
```

Restart Claude Code, then launch an isolated session:

```bash
claude --worktree fix-login
```

The plugin also handles native `EnterWorktree` and agent isolation worktrees. It does not ask the model to remember cleanup. A local Python worker checks registered worktrees every minute.

For a local checkout before publishing/installing:

```bash
claude --plugin-dir /absolute/path/to/worktree-rescue --worktree fix-login
```

Claude owns the worktree-creation hook slot: use Rescue instead of another WorktreeCreate/WorktreeRemove integration, not alongside it. `--worktree --tmux` creates its worktree before plugins load and is outside this plugin's automatic registration.

## What happens automatically?

| Situation | Behavior |
|---|---|
| A session is still active | Keep the worktree. |
| Session liveness cannot be determined | Keep it, regardless of age. |
| A same-user process is using its directory or command path | Keep it. Never terminate processes. |
| Clean and all commits are reachable from the integration branch | After 10 minutes without activity, archive and remove it. |
| Uncommitted or unmerged work, inactive for less than 7 days | Keep it. |
| Unfinished work, inactive for at least 7 days | Archive and remove it. Restore whenever needed. |
| Claude requests removal through WorktreeRemove | Archive first, then remove if no other session/process is using it. |
| Missing ownership, changed branch, symlink replacement or an in-progress Git operation | Preserve it for manual review. |
| Not enough archive capacity or disk space | Keep it. No deletion without a verified archive. |

`Stop` means a turn finished; it does **not** release a session. `SessionEnd` releases it. For crashed sessions, a verified PID plus start time lets the worker detect exit without confusing PID reuse. If the owner process cannot be identified, the plugin preserves the worktree until a SessionEnd event is received.

The integration reference is captured at creation: local `origin/HEAD` when available, otherwise `main`, `master`, or the source branch. Rescue never fetches or calls GitHub automatically. Squash/rebase merges may not be recognized as ancestry; those worktrees follow the unfinished-work policy.

## Commands

These commands run only when you ask. Routine hooks print nothing, except the required path from WorktreeCreate.

| Claude Code command | Purpose |
|---|---|
| `/rescue:status` | Show pending worktrees and reasons for keeping them. |
| `/rescue:archives` | List recovery archives. |
| `/rescue:restore ID --to /new/path` | Restore to a new detached worktree, preserving staging. |
| `/rescue:sweep` | Run the usual cleanup policy now. |
| `/rescue:pause` | Pause automatic cleanup. |
| `/rescue:resume` | Resume automatic cleanup. |
| `/rescue:history` | Show recent local actions. |

Plugin commands are namespaced by Claude Code; they use `/rescue:status`, not `/rescue status`.

## Try the recovery demo without Claude or an API key

```bash
git clone https://github.com/popiolkoks1/worktree-rescue.git
cd worktree-rescue
python3 scripts/demo.py
```

The demo creates a temporary repository, stages one change, leaves another unstaged, adds an ignored local config file, archives the worktree and restores everything. The temporary data is removed afterward. [Example output](docs/DEMO.md).

## Use with another agent

The engine is independent of Claude Code:

```bash
python3 scripts/rescue.py create --repo /path/to/repo --name agent-task
# Start your agent in the returned path.
python3 scripts/rescue.py status
python3 scripts/rescue.py sweep --dry-run
python3 scripts/rescue.py sweep
python3 scripts/rescue.py archives
python3 scripts/rescue.py restore ARCHIVE_ID --to /new/path
```

External adapters should send SessionStart and SessionEnd metadata through `hook`, as documented in [the integration guide](docs/INTEGRATION.md). Without session leases, process checks still apply, but use explicit lifecycle events for reliable protection. Existing arbitrary worktrees are not auto-adopted.

Optional CLI installation: `python3 -m pip install .` exposes `worktree-rescue`. The Claude plugin uses its bundled script and does not require pip.

## Configuration

All policy is **user-local**, never loaded from an untrusted repository. State defaults to `~/.local/share/worktree-rescue` with mode 0700. Override with `WORKTREE_RESCUE_HOME` or the CLI's `--home` argument.

```bash
python3 scripts/rescue.py config
python3 scripts/rescue.py config --set grace_seconds 600
python3 scripts/rescue.py config --set archive_after_days 7
# 0 keeps unfinished work indefinitely:
python3 scripts/rescue.py config --set archive_after_days 0
python3 scripts/rescue.py config --set archive_budget_bytes 10737418240
```

Default archive budget: 10 GiB; minimum free-space reserve: 1 GiB. Space checks reserve the uncompressed working files plus the repository's Git object size. Large repositories may be preserved until you raise the budget. Full archives favor recoverability over maximum space savings. They can contain dependency caches and may compress poorly.

No archive pruning is automatic. Review and manually remove archives you no longer need. Pause cleanup before managing the archive directory. The daemon is started by session hooks and runs quietly until `stop-worker`; disabling the plugin prevents future hook launches but does not stop an existing worker:

```bash
python3 scripts/rescue.py pause
python3 scripts/rescue.py stop-worker
```

## Recovery and limits

Restore requires a new target path. It verifies file and bundle checksums, restores the Git index and working files, and refuses archive path traversal. If the original repository no longer exists, Rescue creates a private recovery repository from the archived bundle.

- Archives are local **and unencrypted**. Ignored files such as `.env` may contain credentials. Do not publish the state directory or upload archives to an issue.
- This is recovery software, not a sandbox or an off-machine backup. Another process with your account permissions can modify its state. There is an unavoidable final race between checks and removal; active-session tracking and process checks reduce it, not eliminate it.
- Nested Git repositories, initialized submodules, special files and unresolved index states are preserved rather than automatically removed.
- WorktreeCreate replaces Claude's native setup; `.worktreeinclude` and another tool's setup hooks are not copied. Run dependency/setup commands inside the new worktree. Rescue never executes repository-defined shell hooks itself.
- A requested native removal that cannot be archived returns a hook error and keeps the directory. Exceptional failures may be shown by Claude; normal background sweeps are silent and recorded in history.
- Only worktrees Rescue created under its managed directory are eligible. Source checkouts and unrelated projects are never cleanup targets.

See [architecture and recovery](docs/ARCHITECTURE.md) and [security policy](SECURITY.md).

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
python3 scripts/demo.py
claude plugin validate .
```

Tests use disposable real Git repositories and synthetic hook input. They do not invoke models. Live Claude session behavior must also be checked against your installed version; the plugin follows the [official hook reference](https://code.claude.com/docs/en/hooks).

MIT licensed. Contributions and reproducible failures are welcome. This is an early release: start with a disposable project and test restore before relying on it.
