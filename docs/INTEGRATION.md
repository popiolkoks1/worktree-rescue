# Agent integration

The engine accepts a small subset of Claude's documented lifecycle payload through stdin:

```json
{"hook_event_name":"SessionStart","session_id":"unique-session-id","cwd":"/absolute/managed/worktree"}
```

Send the same session ID with SessionEnd when the agent actually exits. Stop only reports a finished turn and keeps the session protected. PostToolUse and UserPromptSubmit refresh activity. The CLI intentionally discards prompts, tool results and transcripts before storing events.

For a non-Claude adapter:

1. Call `python3 scripts/rescue.py create --repo REPO --name TASK` and parse the returned JSON path.
2. Send SessionStart before starting the agent in that directory.
3. Send SessionEnd after its process exits.
4. Start `python3 scripts/rescue.py worker` as your background service, or let the lifecycle hooks launch it automatically.

Example events can be sent using a subprocess with a JSON stdin argument. Do not interpolate user prompts or paths into shell command text. Use an argument array.

The default plugin creates managed worktrees. Existing linked worktrees, including those created through Bash or another plugin, require explicit `adopt PATH --base main` enrollment. Use the generated lifecycle-only companion plugin alongside another worktree integration (see README). Companion mode does not intercept another tool's removal, so disable that tool's automatic removal if recoverable cleanup is required. No adapter should infer session death from inactivity alone.

## Claude hooks

- WorktreeCreate: synchronous; creates a registered worktree and prints only its absolute path. Replaces native creation.
- WorktreeRemove: synchronous; archives and removes the requested owned worktree, or returns a nonzero status while preserving it. Ignores only the requesting session lease and its hook ancestors; other active sessions and processes still block removal.
- SessionStart, UserPromptSubmit, PostToolUse, Stop: enqueue lifecycle metadata, then ensure a single detached worker is running.
- SessionEnd: enqueue the end event without waiting for the global archive lock. Claude's default exit budget is short.

Installing another WorktreeCreate/WorktreeRemove override alongside the default Rescue plugin is unsupported. The generated companion omits both hooks and uses the same local engine/state. Adopted branches are retained. No hooks edit Claude's settings, load repository-defined shell commands, or request model inference.

References: [Claude hooks](https://code.claude.com/docs/en/hooks), [plugin manifest](https://code.claude.com/docs/en/plugins-reference), [marketplaces](https://code.claude.com/docs/en/plugin-marketplaces).
