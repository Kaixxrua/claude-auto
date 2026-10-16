# Contributing

Use Linux with Python 3.10+ and the system timezone database. The launcher and dashboard use the standard library; the optional compaction mod runs inside Claude Code. Node.js is needed for JavaScript checks and mod tests. Automated tests do not require Claude Code, a subscription, or external credentials.

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile bin/claude-accounts bin/claude-delegate dashboard/monitor.py dashboard/reset_cards.py scripts/install.py plugins/account-delegation/scripts/server.py
sh -n bin/claude-auto bin/claude-quota
node --check dashboard/static/app.js
node --test tests/test_compaction.mjs
python3 scripts/check_release.py
```

The suite exercises real PTYs with a fake upstream CLI, concurrent controllers, checkpoint integrity, account selection, login lock handling, reset-card confirmation/idempotency, Host/Origin checks, and a fresh installation with a loopback HTTP server. Tests isolate account files in temporary directories. A passing mock suite does not establish compatibility with a new upstream CLI release; report the version when performing an opt-in manual check.

Delegation tests also cover MCP messages, concurrent identity leases, disabled/unknown/exhausted accounts, cancellation, worker crashes and native session resumption across accounts. They never require live subscriptions.

Keep changes small and include regression coverage when changing switching, persistence, credential handling, or HTTP actions. Use commit titles such as `fix(launcher): preserve session lease during recovery`.

Do not commit profiles, real emails, tokens, cookies, session transcripts, context backups, diagnostic exports, or copies of the official CLI. The repository's `.gitignore` is not a substitute for inspecting staged files. Use fictional `.test` identities in fixtures. Tests must never redeem real reset cards or silently contact an upstream service.

Preserve these invariants:

- Only verified quota exhaustion can trigger account rotation; bans, safety refusals and transient errors cannot.
- Incomplete or conflicting context blocks switching; a working session must not be force-killed to make a test pass.
- Disabled profiles are skipped before login/usage probes. Account identity changes invalidate cached observations and redemption confirmations.
- Unknown and stale quota data remain explicitly unconfirmed.
- A redemption is confirmed by the user and journaled before the upstream call; uncertain outcomes are never automatically resent.
- Tokens never enter responses, logs, fixtures, or exports.

Contributions are provided under Apache-2.0, the repository's license. Keep applicable copyright and NOTICE attribution.
