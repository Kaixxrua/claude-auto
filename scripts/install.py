#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Install this checkout for the current user without touching account data."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile

SOURCE = Path(__file__).resolve().parents[1]
COMMANDS = ('claude-accounts', 'claude-auto', 'claude-quota')
ASSETS = ('monitor.py', 'reset_cards.py', 'static/index.html',
          'static/app.js', 'static/style.css', 'static/favicon.svg')
COMPACTION_ASSETS = ('.claude-plugin/plugin.json', 'hooks/hooks.json', 'hooks/register.js')


def atomic_install(content: bytes, target: Path, mode: int):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.claude-auto-install-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(prefix: Path, systemd: bool = False):
    prefix = prefix.expanduser().absolute()
    default_prefix = Path.home()/'.local'
    if systemd and prefix != default_prefix:
        raise ValueError('--systemd requires the default ~/.local prefix; adapt examples/claude-quota-monitor.service for other prefixes')
    manifest = [(SOURCE/'bin'/name, prefix/'bin'/name, 0o755) for name in COMMANDS]
    manifest += [(SOURCE/'dashboard'/name, prefix/'share/claude-quota-monitor'/name, 0o644) for name in ASSETS]
    manifest += [(SOURCE/name, prefix/'share/doc/claude-auto'/name, 0o644) for name in ('LICENSE', 'NOTICE')]
    manifest += [(SOURCE/'plugins/compaction-recovery'/name,
                  prefix/'share/claude-auto/compaction-recovery'/name, 0o644)
                 for name in COMPACTION_ASSETS]
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
        pending.append((source.read_bytes(), target, mode))
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
    if sys.platform != 'linux' or sys.version_info < (3, 10):
        parser.error('Linux and Python 3.10+ are required')
    try:
        prefix = install(args.prefix, args.systemd)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'Install failed: {exc}\n')
    print(f'Installed claude-auto, claude-accounts and claude-quota in {prefix / "bin"}')
    print('Add that bin directory to PATH, then run: claude-accounts init')
    print('Account data is managed separately by claude-accounts; installation does not initialize it.')
    if args.systemd:
        print('Service file installed. To start: systemctl --user daemon-reload && systemctl --user enable --now claude-quota-monitor.service')
    else:
        print('Start the dashboard with: claude-quota')


if __name__ == '__main__':
    main()
