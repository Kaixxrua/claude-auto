#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Resolve the installed/source launcher even from a native plugin cache."""
from pathlib import Path
import os
import shutil
import subprocess
import sys

here = Path(__file__).resolve()
candidates = [here.parents[3]/'bin/claude-delegate', here.parents[4]/'bin/claude-delegate']
found = shutil.which('claude-delegate')
if found:
    # On Windows that is the claude-delegate.cmd shim beside the script.
    found = Path(found)
    candidates.append(found.with_suffix('') if found.suffix.lower() == '.cmd' else found)
candidates.append(Path.home()/'.local/bin/claude-delegate')
for command in candidates:
    if command.is_file():
        if os.name == 'nt':
            # execv on Windows starts a new process and ends this one, which closes the MCP
            # connection; serve from a child on the same pipes instead.
            sys.exit(subprocess.run([sys.executable, str(command), 'mcp']).returncode)
        os.execv(sys.executable, [sys.executable, str(command), 'mcp'])
sys.exit('Install claude-auto and add its bin directory to PATH before using account-delegation.')
