# Security

## Trust model

Claude Auto is a single-user local application. It trusts the operating-system account that owns its files. Profiles share preferences, plugins, skills, and session history; separate logins do not provide isolation against local code or other profiles.

The dashboard binds to `127.0.0.1` by default. Explicit private IPv4 bindings are supported for trusted LANs. It has **no user authentication**: a reachable client can read account email/usage data and request reset-card redemption. Email masking does not redact the API response. Prefer loopback plus SSH forwarding; do not publish the port to the internet.

Host validation, same-origin JSON POST requirements, response security headers, fixed static routes, and request-size limits reduce accidental exposure and cross-site requests. They are not substitutes for network isolation or authentication. Reverse proxies and public hosting are not a supported deployment model.

## Credentials and private data

- The launcher delegates login and token refresh to the official CLI. It does not read, copy, modify, or export `.credentials.json`.
- The dashboard's experimental reset-card client reads an existing OAuth access token in memory on demand. It sends authenticated requests only to `https://api.anthropic.com`, rejects redirects, limits response size, and does not return tokens to the browser. It does not implement login or refresh.
- Local account registries, emails, quota history, operation journals, CLI configuration, and context backups are private. Context checkpoints can include full conversations and tool results. Keep the entire account root outside public repositories and shared web directories.
- The application has no telemetry collector. Ordinary quota probes use the official CLI; reset-card queries/redemption make the documented-in-source upstream requests. Your existing CLI configuration, plugins and proxy settings continue to apply to your work sessions.
- A reset-card use requires short-lived confirmation, identity/eligibility checks, and a persisted one-shot operation. Ambiguous upstream outcomes are not automatically retried, even after restart.

Checkpoint verification can reuse process-local SHA-256 proofs while the file's device, inode, type/mode, size, mtime and ctime remain unchanged. Rewrites with a restored mtime, replacement, truncation and symlink substitution invalidate this proof. The cache depends on local filesystem change metadata; it is not a detector for silent storage corruption that leaves all metadata unchanged. A fresh controller re-reads the bytes. Proofs are not loaded from a persistent cache, and changed/new content is hashed and validated before recovery.

## Supported releases and reporting

Security fixes target the latest release on `main`. Compatibility with undocumented upstream endpoints is best effort.

For a sensitive report, use the repository's private [Report a vulnerability](https://github.com/Kaixxrua/claude-auto/security/advisories/new) channel when available. If it is unavailable, open an Issue requesting a private contact without including exploit details, credentials or personal logs. Do not paste tokens, raw session archives, `accounts.json`, or complete dashboard responses into public Issues.

For ordinary bugs, a minimal fixture with `.test` email addresses and fictional identifiers is sufficient. `scripts/check_release.py` rejects common credential patterns and runtime artifacts before publication; it is a defense in addition to review, not proof that arbitrary files are safe to publish.
