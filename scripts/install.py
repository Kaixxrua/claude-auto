#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Install this checkout for the current user without touching account data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE/'lib'))
import claude_auto_os as oslayer  # noqa: E402

COMMANDS = ('claude-accounts', 'claude-auto', 'claude-quota', 'claude-delegate')
# Windows runs the Python commands through cmd.exe shims instead of the sh wrappers.
WINDOWS_COMMANDS = ('claude-accounts', 'claude-delegate')
ASSETS = ('monitor.py', 'reset_cards.py', 'static/index.html',
          'static/app.js', 'static/style.css', 'static/favicon.svg')
COMPACTION_ASSETS = ('.claude-plugin/plugin.json', 'hooks/hooks.json', 'hooks/register.js')
DELEGATION_ASSETS = ('.claude-plugin/plugin.json', '.mcp.json', 'scripts/server.py')


def atomic_install(content: bytes, target: Path, mode: int):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.claude-auto-install-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            if hasattr(os, 'fchmod'):
                os.fchmod(stream.fileno(), mode)
        oslayer.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def windows_shims(folder: Path):
    """cmd.exe entry points in folder, beside the scripts. cmd.exe reads a batch file in the
    console code page, so the file stays ASCII; %~dp0 is expanded as Unicode at run time. A
    Python under a non-ASCII user folder without 8.3 names is reached relative to %~dp0."""
    python = sys.executable
    if not python.isascii():
        python = oslayer.short_path(python)
    if python.isascii():
        python = python.replace('%', '%%')
    else:
        try:
            python = '%~dp0' + os.path.relpath(sys.executable, folder).replace('%', '%%')
        except ValueError:  # another drive
            pass
    if not python.isascii():
        raise ValueError(f'the Python path needs an ASCII form (an 8.3 short name, or a path relative to {folder}): {sys.executable}')
    run = f'@echo off\r\n"{python}" '
    return {
        'claude-accounts.cmd': run+'"%~dp0claude-accounts" %*\r\n',
        'claude-auto.cmd': run+'"%~dp0claude-accounts" run %*\r\n',
        'claude-delegate.cmd': run+'"%~dp0claude-delegate" %*\r\n',
        'claude-quota.cmd': run+'"%~dp0..\\share\\claude-quota-monitor\\monitor.py" --adapter "%~dp0claude-accounts" %*\r\n',
    }


def install(prefix: Path, systemd: bool = False):
    prefix = prefix.expanduser().absolute()
    default_prefix = Path.home()/'.local'
    if systemd and oslayer.WINDOWS:
        raise ValueError('--systemd is for Linux user services')
    if systemd and prefix != default_prefix:
        raise ValueError('--systemd requires the default ~/.local prefix; adapt examples/claude-quota-monitor.service for other prefixes')
    commands = WINDOWS_COMMANDS if oslayer.WINDOWS else COMMANDS
    manifest = [(SOURCE/'bin'/name, prefix/'bin'/name, 0o755) for name in commands]
    manifest.append((SOURCE/'lib/claude_auto_os.py', prefix/'share/claude-auto/lib/claude_auto_os.py', 0o644))
    manifest += [(SOURCE/'dashboard'/name, prefix/'share/claude-quota-monitor'/name, 0o644) for name in ASSETS]
    manifest += [(SOURCE/name, prefix/'share/doc/claude-auto'/name, 0o644) for name in ('LICENSE', 'NOTICE')]
    manifest += [(SOURCE/'plugins/compaction-recovery'/name,
                  prefix/'share/claude-auto/compaction-recovery'/name, 0o644)
                 for name in COMPACTION_ASSETS]
    manifest += [(SOURCE/'plugins/account-delegation'/name,
                  prefix/'share/claude-auto/account-delegation'/name, 0o644)
                 for name in DELEGATION_ASSETS]
    manifest.append((SOURCE/'plugins/.claude-plugin/marketplace.json',
                     prefix/'share/claude-auto/.claude-plugin/marketplace.json', 0o644))
    if systemd:
        manifest.append((SOURCE/'examples/claude-quota-monitor.service',
                         Path.home()/'.config/systemd/user/claude-quota-monitor.service', 0o644))
    # Read and validate the entire allowlist before replacing any installed file.
    pending = []
    for source, target, mode in manifest:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f'refusing non-regular installation target: {target}')
        content = source.read_bytes()
        if oslayer.WINDOWS and source.name == '.mcp.json':
            # No python3 command on Windows: the MCP server runs with this interpreter.
            config = json.loads(content)
            config['mcpServers']['accounts']['command'] = sys.executable
            content = (json.dumps(config, indent=2)+'\n').encode()
        pending.append((content, target, mode))
    if oslayer.WINDOWS:
        for name, text in windows_shims(prefix/'bin').items():
            target = prefix/'bin'/name
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError(f'refusing non-regular installation target: {target}')
            pending.append((text.encode('ascii'), target, 0o755))
    for content, target, mode in pending:
        atomic_install(content, target, mode)
    return prefix


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', type=Path, default=Path.home()/'.local',
                        help='installation prefix (default: ~/.local)')
    parser.add_argument('--systemd', action='store_true',
                        help='also install the user service file; does not start or enable it')
    args = parser.parse_args()
    if sys.platform not in ('linux', 'win32') or sys.version_info < (3, 10):
        parser.error('Linux or Windows and Python 3.10+ are required')
    try:
        prefix = install(args.prefix, args.systemd)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'Install failed: {exc}\n')
    print(f'Installed claude-auto, claude-accounts, claude-quota and claude-delegate in {prefix / "bin"}')
    if oslayer.WINDOWS:
        print('Commands run through the .cmd files there; the native Claude Code installer usually has that directory on PATH already.')
    print('Add that bin directory to PATH, then run: claude-accounts init')
    print('Account data is managed separately by claude-accounts; installation does not initialize it.')
    if args.systemd:
        print('Service file installed. To start: systemctl --user daemon-reload && systemctl --user enable --now claude-quota-monitor.service')
    else:
        print('Start the dashboard with: claude-quota')


if __name__ == '__main__':
    main()
