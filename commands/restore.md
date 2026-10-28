---
description: Restore a recovery archive to a new directory.
argument-hint: ARCHIVE_ID --to /absolute/new/path
disable-model-invocation: true
allowed-tools: Bash(python3:*)
---

Restore the archive requested in the user's arguments: $ARGUMENTS

Use `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rescue.py" restore ARCHIVE_ID --to NEW_PATH`.
Validate that ARCHIVE_ID is exactly 16 lowercase hexadecimal characters. Pass NEW_PATH as one safely quoted argument; never evaluate it as shell code. If either is missing, ask for it. Never overwrite an existing directory or run cleanup as part of restoration. Report the returned path in English.
