"""Import only the source checkout; never the operator's installed launcher."""
import io
import os
from pathlib import Path
import platform
import sys
import unittest
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT/'dashboard'))
WINDOWS = os.name == 'nt'
# An existing program for registries whose client is never started.
PROGRAM = sys.executable


def native_fixture(path, script):
    """A fake native client running script with this Python: an executable script on Linux;
    on Windows a console .exe, pip's launcher stub with the script appended as a zip application."""
    if not WINDOWS:
        path.write_text(script)
        path.chmod(0o755)
        return path
    try:
        import pip._vendor.distlib as distlib
    except ImportError:
        raise unittest.SkipTest('pip launcher stub unavailable') from None
    stub = Path(distlib.__file__).with_name('t64-arm.exe' if platform.machine() == 'ARM64' else 't64.exe')
    if not stub.is_file():
        raise unittest.SkipTest('pip launcher stub unavailable')
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('__main__.py', script)
    path = path.with_suffix('.exe')
    path.write_bytes(stub.read_bytes()+b'#!"'+os.fsencode(sys.executable)+b'"\n'+archive.getvalue())
    return path


def link_directory(link, target):
    """A directory link; Windows without the symbolic-link right takes a junction."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if not WINDOWS:
            raise
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
