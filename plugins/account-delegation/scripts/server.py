#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Resolve the installed/source launcher even from a native plugin cache."""
from pathlib import Path
import os
import shutil
import sys

here = Path(__file__).resolve()
candidates = [here.parents[3]/'bin/claude-delegate', here.parents[4]/'bin/claude-delegate']
found = shutil.which('claude-delegate')
if found:
    candidates.append(Path(found))
candidates.append(Path.home()/'.local/bin/claude-delegate')
for command in candidates:
    if command.is_file():
        os.execv(sys.executable, [sys.executable, str(command), 'mcp'])
sys.exit('Install claude-auto and add its bin directory to PATH before using account-delegation.')
