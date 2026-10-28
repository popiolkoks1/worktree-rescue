# Contributing

Start with a small issue or pull request describing a reproducible problem. Use synthetic data and disposable repositories. Keep all user-facing content in English.

Run:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
python3 scripts/demo.py
claude plugin validate .
```

Deletion and restore changes need real-Git integration tests, including failure cases. Do not reduce preservation checks to make a test pass. Keep runtime dependencies in Python's standard library. Do not add telemetry, automatic uploads, model calls or automatic archive expiration.

Manual Claude smoke checklist: load with --plugin-dir, create an isolated worktree, verify ownership, end a session, inspect the archive, and restore staged/unstaged/ignored files. Also test an active second session and a server process that must block cleanup. Record the Claude version used. Do not run destructive smoke tests on your working project.

The hosted macOS/Linux test matrix is provided in docs/ci-workflow.yml.example. Enable it by copying it to .github/workflows/ci.yml using credentials with workflow permission. Local tests do not depend on hosted CI.
