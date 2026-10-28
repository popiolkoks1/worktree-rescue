# Security and data recovery

This project deletes only registered linked worktrees after creating a verified local archive. It is not a sandbox, a full backup product or a defense against an agent with the same account permissions.

Archives contain ignored files, which may include secrets. State is private to the user (0700); archives are not encrypted. Never attach archives, registry files or raw local history to a public issue. Redact paths and credentials from reproductions.

Do not run it on shared-user systems where the same OS account is used by mutually untrusted processes. Stop the worker before manually editing state. Do not edit an ended worktree outside a registered session while cleanup is enabled.

If you find a path that can delete an unregistered directory, corrupt recovery data or leak credentials, avoid posting a working exploit publicly. Contact the maintainer through their GitHub profile: https://github.com/popiolkoks1. Security advisories can be enabled in the repository for private reporting. No private-reporting channel is claimed until it is available.

Please include the version, OS, Git/Python versions and a minimal synthetic reproduction. Never send real credentials or personal files.
