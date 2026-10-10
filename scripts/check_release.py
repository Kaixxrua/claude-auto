#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check tracked/unignored release files for common secrets and private state.

This is a conservative publication check, not a general-purpose secret scanner.
Review the exact staged diff before every release as well.
"""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TOP_FILES = {'.gitattributes', '.gitignore', 'README.md', 'README.en.md', 'LICENSE',
             'NOTICE', 'SECURITY.md', 'CONTRIBUTING.md', 'CHANGELOG.md'}
TOP_DIRS = {'bin', 'dashboard', 'docs', 'examples', 'scripts', 'tests', 'plugins', '.github'}
PRIVATE_NAMES = {'.claude', '.claude-accounts', '.credentials.json', 'accounts.json',
                 'persistent-watchers.json', 'state', 'logs', '__pycache__', '.env'}
SECRET_PATTERNS = {
    'Anthropic credential': re.compile(r'sk-' + r'ant-[A-Za-z0-9_-]{16,}'),
    'GitHub credential': re.compile(r'gh[pousr]_' + r'[A-Za-z0-9]{20,}|github_pat_' + r'[A-Za-z0-9_]{20,}'),
    'private key': re.compile(r'-----BEGIN ' + r'(?:[A-Z]+ )?PRIVATE KEY-----'),
    'literal access/refresh token': re.compile(
        r'''(?i)(?:access_?token|refresh_?token|api_?key|oauth_?token)["']?\s*[:=]\s*["']([A-Za-z0-9_+/=.-]{32,})["']'''),
    'personal absolute home path': re.compile(r'/(?:home|Users)/[A-Za-z0-9_.-]+/'),
}
EMAIL = re.compile(r'\b[A-Za-z0-9_.+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b')


def files():
    result = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                            cwd=ROOT, capture_output=True, check=True)
    return sorted(set(p.decode('utf-8') for p in result.stdout.split(b'\0') if p))


def inspect(relative):
    path = Path(relative)
    findings = []
    if path.parts[0] not in TOP_DIRS and relative not in TOP_FILES:
        findings.append('outside release allowlist')
    if (PRIVATE_NAMES.intersection(path.parts) or path.name.startswith('.env.') or
            path.suffix in {'.jsonl', '.log', '.pem', '.key', '.pyc'}):
        findings.append('runtime or credential artifact')
    full = ROOT/path
    if full.is_symlink():
        return findings + ['symlink in release']
    content = full.read_bytes()
    if len(content) > 4_000_000:
        findings.append('unexpected large artifact')
    if path.suffix == '.png':
        if not relative.startswith('docs/') or not content.startswith(b'\x89PNG\r\n\x1a\n'):
            findings.append('unexpected binary artifact')
        return findings
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError:
        return findings + ['unexpected non-text file']
    for label, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            findings.append(label)
    for match in EMAIL.finditer(text):
        domain = match.group().split('@', 1)[1].lower()
        if not (domain.endswith('.test') or domain in {'example.com', 'example.org', 'example.net'}):
            findings.append('non-fixture email address')
            break
    # Report only labels and filenames, never the matched credential/value.
    return findings


def main():
    try:
        paths = files()
        findings = [(path, issue) for path in paths for issue in inspect(path)]
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        print('Release check failed: '+type(exc).__name__, file=sys.stderr)
        return 1
    if not paths:
        print('Release check failed: no files found', file=sys.stderr)
        return 1
    for path, issue in findings:
        print(f'{path}: {issue}', file=sys.stderr)
    if findings:
        return 1
    print(f'Release check passed: {len(paths)} source/documentation files; no forbidden artifacts or detected secret patterns.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
