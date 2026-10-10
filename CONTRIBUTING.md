# Contributing

Use Linux with Python 3.10+ and the system timezone database. Runtime code uses the standard library. Node.js is needed only for JavaScript syntax checks. Tests do not require Claude Code, a subscription, or external credentials.

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile bin/claude-accounts dashboard/monitor.py dashboard/reset_cards.py scripts/install.py
sh -n bin/claude-auto bin/claude-quota
node --check dashboard/static/app.js
python3 scripts/check_release.py
```

The suite exercises real PTYs with a fake upstream CLI, concurrent controllers, checkpoint integrity, account selection, login lock handling, reset-card confirmation/idempotency, Host/Origin checks, and a fresh installation with a loopback HTTP server. Tests isolate account files in temporary directories. A passing mock suite does not establish compatibility with a new upstream CLI release; report the version when performing an opt-in manual check.

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
