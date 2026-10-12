# Changelog

## Unreleased

- Avoid rewriting existing context objects and reuse process-local verification proofs for unchanged files during account rotation. Keep full history, final flushes and original agent identifiers; inode, device, mode, size, mtime or ctime changes invalidate cached proofs.
- Reuse validated subagent terminal status instead of reparsing unchanged histories, and extend an already verified prefix hash when the file remains unchanged.
- Report checkpoint progress, reuse/write counts and private rotation phase timings. First verification in a new process still reads file contents.

- Restore native compaction scheduling in plugin 0.2.0: remove the extra retry loop and idle compaction timer that amplified upstream failures. Record outcomes per session, agent and trigger, and distinguish background precomputation from foreground compaction. Preserve native cancellation, vetoes, errors and history. Existing windows must reload the updated plugin; see `docs/compaction.md`.
- Keep large transcript receipt scans outside lifecycle hooks, allowing resume verification to finish within the native hook deadline. Rotation still reconciles missing receipts and saves scan cursors without overwriting concurrent hook updates.

- Replace the proxy-only 60-second request cap with a 5-minute request budget and a 3-minute first-response baseline. Preserve explicit operator settings and native retry/stream watchdog behavior; record numeric timeout settings in private lifecycle diagnostics.

Launcher implementation version: `2026-10-10.4`.

## 0.1.0 — 2026-10-09

Initial public release of the local launcher and quota dashboard.

- Official CLI account profiles, interactive quota-triggered rotation, original-session resume, verified context backups and subagent recovery tracking.
- Disabled-account handling, concurrent-controller leases, stale-task reconciliation, and priority for recently verified quota recovery.
- Bounded handling of concurrent OAuth refresh errors and proxy request timeout defaults.
- Local quota dashboard with explicit data freshness and experimental reset-card query / confirmed redemption.
- Portable per-user installer, loopback defaults, configurable paths, systemd example, Chinese / English getting-started documentation, and offline regression tests.

Launcher implementation version: `2026-10-09.2`. Dashboard implementation version: `1.1.1`. Package version: `0.1.0`.
