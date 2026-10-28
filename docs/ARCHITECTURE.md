# Architecture and recovery

## State

The private state directory contains registry.json, config.json, history.jsonl, an atomic event inbox, managed worktrees and recovery archives. File and daemon locks use POSIX flock. Only one archive/restore mutation runs at a time. Session hooks enqueue unique files without waiting for the archive lock. The worker saves applied events before deleting inbox entries, so replay after a crash is idempotent.

Session leases are keyed by session ID and optional owner PID/start time. A live owner or unknown owner protects the worktree indefinitely. Verified process exit releases the lease and restarts the grace period. Inactivity alone never releases a lease.

## Eligibility

The engine checks recorded canonical paths, the Git common directory, exact worktree administration directory, private ownership marker, expected branch and active Git operations. It refuses source checkouts, replaced symlinks and unregistered paths. It checks same-user process working directories and command paths without terminating them. Probe errors preserve the worktree.

Default policy waits ten minutes for integrated clean work. Inactive unfinished work becomes archivable after seven days; setting archive_after_days to 0 disables that path. WorktreeRemove is a direct native removal request, so it can bypass the normal age threshold, but not recovery or other-session checks.

## Archive transaction

1. Check ownership, sessions and processes.
2. Hash file contents, modes and symlink targets without following symlinks. Include ignored and untracked files; reject nested repositories and special files.
3. Reserve capacity for the uncompressed snapshot plus repository objects and the configured free-space floor.
4. Write the index tree and a temporary recovery commit so staged blobs are reachable from a self-contained Git bundle. No commit hooks run.
5. Bundle history; tar working files; read every archived file and compare hashes/modes/links with the original inventory.
6. Recheck working contents, HEAD and index tree. Atomically publish the archive with checksums and metadata.
7. Recheck ownership, liveness and contents; remove only the registered worktree with Git. Delete only a Rescue-created namespaced branch with an expected old commit check; always retain explicitly adopted branches.
8. Persist the result and history. Remove the temporary snapshot ref. Archives remain until the user deletes them.

A failure before removal preserves the original. A failure after archive publication may leave both copies. An interruption after Git removal leaves the independently discoverable archive even if registry state is not yet updated. The implementation does not claim an atomic transaction with unrelated processes; writers can race the final removal. Avoid editing ended worktrees outside registered sessions.

## Restore

Verify archive checksums before writing. Import the bundle into the original common Git directory, a supplied repository, or a new private bare recovery repository if the original no longer exists. Create a detached, no-checkout worktree at a new path. Restore the index tree and working files, rejecting traversal, .git members, unsupported entry types and writes through symlink parents. Rehash the result and compare against the saved inventory.

Restore never overwrites an existing directory and never auto-registers the restored worktree for deletion. The source archive remains intact even if restoration fails midway. Detached restored commits remain reachable through a recovery ref; create your own branch with `git switch -c recovered-work` when ready.

## Operating limits

macOS uses lsof and ps. Linux uses /proc and ps. This release does not install a system service, support Windows, detect every open file handle, run dependency setup, fetch remotes, identify squash merges or encrypt archives. The worker starts on hook activity and continues until stopped. Disable the plugin, pause cleanup and stop the worker when uninstalling.

## Environment and companion integrations

Managed creation selects the intersection of ignored files and `.worktreeinclude` matches using Git's pattern parser. Only regular files are copied; symlinks in either path are rejected, and existing destination files are never overwritten. Failed copying rolls back the newly created worktree and branch before registration.

Explicit adoption records an external linked worktree and writes a Rescue marker in its private Git directory. The same identity, branch, operation, process and session checks apply outside the managed directory. Primary checkouts and detached worktrees are not adopted. Existing markers belonging to a different registry are refused. Adopted branches survive directory removal.

The companion build command produces a separate portable plugin with the same engine, commands and lifecycle hooks but no create/remove hooks. It observes future session events only; other tools can still independently remove worktrees. Enrollment is not a backup and does not archive until the cleanup policy allows it.
