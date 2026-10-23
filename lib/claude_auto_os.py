# SPDX-License-Identifier: Apache-2.0
"""Operating-system layer shared by claude-accounts, claude-delegate and the dashboard.

Linux keeps /proc, flock, process groups and pty. Windows uses documented Win32 calls
through ctypes: process creation times for PID identity, share-mode leases, LockFileEx,
console control events and a pseudo console (ConPTY, Windows 10 1809 or later).
Python standard library only.
"""
from __future__ import annotations

import codecs
import contextlib
import datetime
import errno
import os
from pathlib import Path
import queue
import re
import select
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zoneinfo

WINDOWS = os.name == 'nt'
# os.open() is a text-mode CRT call on Windows unless O_BINARY is given.
BINARY = getattr(os, 'O_BINARY', 0)
NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)


def utf8_stdio():
    """Claude Code exchanges UTF-8 with hooks, status lines and MCP servers. Windows pipes
    otherwise use the ANSI code page, which corrupts non-ASCII paths and messages."""
    if not WINDOWS:
        return
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            if stream is sys.stdin:
                stream.reconfigure(encoding='utf-8')
            else:
                stream.reconfigure(encoding='utf-8', errors='replace', newline='\n')


def stdin_bytes():
    """All of stdin, as Claude Code sent it (bytes; a test may substitute a text stream)."""
    return getattr(sys.stdin, 'buffer', sys.stdin).read()


def claude_temp_root():
    """Where Claude Code keeps session scratchpads."""
    if WINDOWS:
        return Path(tempfile.gettempdir())/'claude'
    return Path(tempfile.gettempdir())/('claude-'+str(os.getuid()))


def sync_directory(path):
    """Persist a rename. NTFS journals directory metadata itself."""
    if WINDOWS:
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def time_zone(name):
    """The zone Claude Code names in /usage and limit messages: UTC (what TZ asks for on
    Linux), an IANA zone, or None for local time. Claude Code on Windows ignores TZ and names
    the system zone, which Python there may lack without the tzdata package; local time is
    that zone. False for a name that is no zone."""
    if name.upper() in ('UTC', 'GMT'):
        return datetime.timezone.utc
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return None if re.fullmatch(r'[A-Za-z]+(?:/[\w+-]+)+', name) else False


def path_stamp(path):
    """Identity and content stamp of a path, not following a final link: device, file ID,
    type, size, mtime and change time. The change time catches a same-size rewrite whose
    mtime was restored. Python on Windows reports the creation time as ctime from a path,
    so there a file's change time is read from the file itself."""
    info = os.lstat(path)
    if not WINDOWS:
        return _stamp(info, info.st_ctime_ns)
    if not stat.S_ISREG(info.st_mode):
        return _stamp(info, None)
    # Attributes only, sharing everything: no file data is opened and no writer is held up.
    handle = _CreateFileW(str(path), _FILE_READ_ATTRIBUTES, _FILE_SHARE_ALL, None, _OPEN_EXISTING,
                          _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS, None)
    if handle == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return _stamp(info, _change_time(handle))
    finally:
        _CloseHandle(handle)


def fd_stamp(fd):
    """path_stamp of an open file."""
    info = os.fstat(fd)
    return _stamp(info, _change_time(msvcrt.get_osfhandle(fd)) if WINDOWS else info.st_ctime_ns)


def _stamp(info, changed):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, changed)


def replace(source, target):
    """os.replace; on Windows a reader holding the target open delays it briefly."""
    for attempt in range(60):
        try:
            return os.replace(source, target)
        except PermissionError:
            if not WINDOWS or attempt == 59:
                raise
            time.sleep(.01*min(attempt+1, 10))


def read_bytes(path):
    """Path.read_bytes for a file other processes replace(). On Windows the replaced file
    stays delete-pending while anything, such as a virus scanner, still has it open, and
    opening the name is refused until then."""
    for attempt in range(60):
        try:
            return Path(path).read_bytes()
        except PermissionError:
            if not WINDOWS or attempt == 59:
                raise
            time.sleep(.01*min(attempt+1, 10))


def lock(fd, blocking=True):
    """Exclusive lock on an open file, released when it is closed. False when another
    holder has it and blocking is off."""
    if hasattr(fd, 'fileno'):
        fd = fd.fileno()
    if WINDOWS:
        overlapped = _OVERLAPPED()
        flags = _LOCKFILE_EXCLUSIVE_LOCK | (0 if blocking else _LOCKFILE_FAIL_IMMEDIATELY)
        if _LockFileEx(msvcrt.get_osfhandle(fd), flags, 0, 1, 0, ctypes.byref(overlapped)):
            return True
        error = ctypes.get_last_error()
        if not blocking and error in (_ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING):
            return False
        raise ctypes.WinError(error)
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        return False
    return True


def exclusive(path):
    """Open a lease file no other process can hold at the same time; None while it is held.
    A child that inherits it (see inherit) keeps the lease after this process exits."""
    if WINDOWS:
        handle = _CreateFileW(str(path), _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_ALWAYS,
                              _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT, None)
        if handle in (None, _INVALID_HANDLE):
            error = ctypes.get_last_error()
            if error == _ERROR_SHARING_VIOLATION:
                return None
            raise ctypes.WinError(error)
        return msvcrt.open_osfhandle(handle, 0)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | NOFOLLOW, 0o600)
    if lock(fd, blocking=False):
        return fd
    os.close(fd)
    return None


def inherit(fds):
    """Popen arguments that give a child these open files (leases) too."""
    fds = tuple(fds)
    if not WINDOWS:
        return {'pass_fds': fds}
    if not fds:
        return {}
    handles = []
    for fd in fds:
        handle = msvcrt.get_osfhandle(fd)
        os.set_handle_inheritable(handle, True)
        handles.append(handle)
    # Only the listed handles (and the standard ones) reach the child.
    return {'startupinfo': subprocess.STARTUPINFO(lpAttributeList={'handle_list': handles})}


def detached():
    """Popen arguments for a child that outlives its caller's terminal and can be stopped
    as a group: a new session, or on Windows a new process group with a hidden console."""
    if WINDOWS:
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {'start_new_session': True}


# Runs in a helper process: console control events reach only processes attached to the
# sender's console, and the target's group was created with CREATE_NEW_PROCESS_GROUP.
_BREAK = ('import ctypes,sys\n'
          'k=ctypes.WinDLL("kernel32")\n'
          'p=int(sys.argv[1])\n'
          'k.FreeConsole()\n'
          'sys.exit(0 if k.AttachConsole(p) and k.GenerateConsoleCtrlEvent(1,p) else 1)\n')


def stop_group(pid):
    """Ask a process group made by detached() to exit: SIGTERM, or Ctrl+Break on Windows."""
    if WINDOWS:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run([sys.executable, '-c', _BREAK, str(int(pid))], timeout=15,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)


def kill_group(pid):
    """End a process group made by detached() at once: SIGKILL, or the process tree on Windows."""
    if WINDOWS:
        taskkill = Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32'/'taskkill.exe'
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run([str(taskkill), '/F', '/T', '/PID', str(int(pid))], timeout=30,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def group_leader(pid):
    """Whether pid still leads the process group detached() gave it. Windows has no such
    query; the caller's start-time check is the identity proof there."""
    if WINDOWS:
        return True
    try:
        return os.getpgid(pid) == pid
    except ProcessLookupError:
        return False


def process_start(pid):
    """A running process's start time, so a reused PID never matches; None once it exited."""
    if WINDOWS:
        handle = _open_process(pid, _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE)
        if not handle:
            return None
        try:
            if _WaitForSingleObject(handle, 0) != _WAIT_TIMEOUT:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not _GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return str(times[0].dwHighDateTime << 32 | times[0].dwLowDateTime)
        finally:
            _CloseHandle(handle)
    try:
        fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, ValueError, IndexError, TypeError):
        return None


def parent_pid(pid):
    """The parent of a live process; 0 when unknown. On Windows a parent that exited may
    have passed its PID on, so it must have started before the child."""
    if WINDOWS:
        parent = next((p for child, p, _ in _snapshot() if child == pid), 0)
        own, other = process_start(pid), process_start(parent)
        return parent if own and other and int(other) <= int(own) else 0
    try:
        return int(Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()[1])
    except (OSError, ValueError, IndexError, TypeError):
        return 0


def process_args(pid):
    """A process's argument vector, or None."""
    if WINDOWS:
        handle = _open_process(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
        if not handle:
            return None
        try:
            size = wintypes.ULONG(0)
            _NtQueryInformationProcess(handle, _PROCESS_COMMAND_LINE, None, 0, ctypes.byref(size))
            if not size.value:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            if _NtQueryInformationProcess(handle, _PROCESS_COMMAND_LINE, buffer, size, ctypes.byref(size)) < 0:
                return None
            text = _UNICODE_STRING.from_buffer(buffer)
            line = ctypes.wstring_at(text.Buffer, text.Length//2) if text.Buffer and text.Length else ''
        finally:
            _CloseHandle(handle)
        return split_command_line(line) if line else []
    try:
        data = Path(f'/proc/{int(pid)}/cmdline').read_bytes()
    except (OSError, ValueError, TypeError):
        return None
    return [os.fsdecode(arg) for arg in data.split(b'\0')]


def split_command_line(line):
    """Windows command line to argv, as the C runtime parses it."""
    count = ctypes.c_int(0)
    array = _CommandLineToArgvW(line, ctypes.byref(count))
    if not array:
        return None
    try:
        return [array[i] for i in range(count.value)]
    finally:
        _LocalFree(ctypes.cast(array, ctypes.c_void_p))


def commands(name):
    """(pid, argv) of this user's running instances of the named program, such as claude."""
    if WINDOWS:
        for pid, _, exe in _snapshot():
            if exe.lower() in (name.lower(), name.lower()+'.exe'):
                args = process_args(pid)
                if args:
                    yield pid, args
        return
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            args = [os.fsdecode(arg) for arg in (proc/'cmdline').read_bytes().split(b'\0')]
        except OSError:
            continue
        if args and Path(args[0]).name == name:
            yield int(proc.name), args


def git_bash():
    """The Git Bash that Claude Code on Windows uses for shell commands, if any."""
    configured = os.environ.get('CLAUDE_CODE_GIT_BASH_PATH')
    if configured and Path(configured).is_file():
        return configured
    git = shutil.which('git')
    if git:
        for parent in list(Path(git).resolve().parents)[:3]:
            if (parent/'bin'/'bash.exe').is_file():
                return str(parent/'bin'/'bash.exe')
    return None


def shell_argv(command):
    """argv that runs a configured command line through the shell Claude Code would use."""
    if not WINDOWS:
        return ['/bin/sh', '-c', command]
    bash = git_bash()
    if bash:
        return [bash, '-c', command]
    return ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command]


def hook_command(argv):
    """A command hook that runs argv. Windows uses the exec form, which needs no shell."""
    if WINDOWS:
        return {'command': argv[0], 'args': list(argv[1:])}
    return {'command': shlex.join(argv)}


def short_path(path):
    """The 8.3 alias of a path with spaces, when the volume keeps those aliases."""
    size = _GetShortPathNameW(str(path), None, 0)
    if not size:
        return str(path)
    buffer = ctypes.create_unicode_buffer(size)
    return buffer.value if _GetShortPathNameW(str(path), buffer, size) else str(path)


def shell_line(argv):
    """One command line for a shell-only setting such as statusLine. On Windows Claude Code
    runs it with Git Bash, or PowerShell without it: an unquoted program path followed by
    single-quoted arguments means the same in both."""
    if not WINDOWS:
        return shlex.join(argv)
    program = argv[0]
    if not re.fullmatch(r'[\w.:\\/~-]+', program):
        program = short_path(program)
    program = program.replace('\\', '/')
    if re.fullmatch(r'[\w.:/~-]+', program) and not any("'" in arg for arg in argv[1:]):
        return ' '.join([program] + ["'" + arg + "'" for arg in argv[1:]])
    if git_bash():
        return shlex.join([a.replace('\\', '/') if i == 0 else a for i, a in enumerate(argv)])
    return '& ' + ' '.join("'" + arg.replace("'", "''") + "'" for arg in argv)


class Terminal:
    """A child process in a new pseudo terminal that this process reads (no user terminal):
    the native client's /usage screen. pty on Linux, ConPTY on Windows."""

    def __init__(self, argv, env, cwd, cols=80, rows=24):
        self.process = self.master = self.pty = None
        if WINDOWS:
            self.output = queue.Queue()
            self.pty = _PseudoConsole(argv, env, cwd, cols, rows, self.output.put)
            self.pid = self.pty.pid
            return
        import fcntl
        import pty
        import termios
        self.master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
            self.process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave,
                                            stderr=slave, start_new_session=True)
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        self.pid = self.process.pid

    def read(self, timeout):
        """Output within timeout seconds: bytes, b'' at the end, or None."""
        if WINDOWS:
            try:
                return self.output.get(timeout=timeout)[1]
            except queue.Empty:
                return None
        if not select.select([self.master], [], [], timeout)[0]:
            return None
        try:
            return os.read(self.master, 65536)
        except OSError:
            return b''

    def poll(self):
        return self.pty.poll() if WINDOWS else self.process.poll()

    def close(self, grace=8):
        """End the client if it still runs: a gentle request, then a forced end after grace
        seconds. This is our own read-only probe, never a user's work session."""
        if WINDOWS:
            self.pty.close(grace)
            return
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=3)
        finally:
            os.close(self.master)


def console(argv, env):
    """A child process in a new pseudo terminal joined to this process's own terminal: the
    interactive native client, whose screen and keyboard pass through unchanged."""
    return (_WindowsConsole if WINDOWS else _PosixConsole)(argv, env)


class _PosixConsole:
    def __init__(self, argv, env):
        import pty
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.execvpe(argv[0], argv, env)
            except Exception as exc:
                print(str(exc), file=sys.stderr, flush=True)
                os._exit(127)
        self.status = self.saved = self.prior_winch = None

    def resize(self, *_):
        import fcntl
        import termios
        with contextlib.suppress(OSError):
            size = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b'\0'*8)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, size)

    def raw(self):
        import termios
        import tty
        self.saved = termios.tcgetattr(sys.stdin.fileno())
        self.prior_winch = signal.getsignal(signal.SIGWINCH)
        self.resize()
        signal.signal(signal.SIGWINCH, self.resize)
        tty.setraw(sys.stdin.fileno())

    def restore(self):
        import termios
        if self.saved is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.saved)
            signal.signal(signal.SIGWINCH, self.prior_winch)
            self.saved = None

    def exit_code(self):
        if self.status is None:
            try:
                waited, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                waited, status = self.pid, 0
            if waited:
                self.status = os.waitstatus_to_exitcode(status)
        return self.status

    def events(self, timeout, keyboard=True):
        """[('input' | 'output', bytes)] ready within timeout; b'' marks an end of input."""
        sources = [sys.stdin.fileno(), self.fd] if keyboard else [self.fd]
        result = []
        for source in select.select(sources, [], [], timeout)[0]:
            try:
                data = os.read(source, 65536)
            except OSError as exc:
                if source == self.fd and exc.errno == errno.EIO:
                    continue
                raise
            result.append(('output' if source == self.fd else 'input', data))
        return result

    def typing(self):
        return bool(select.select([sys.stdin.fileno()], [], [], 0)[0])

    def send(self, data):
        os.write(self.fd, data)

    def show(self, data):
        os.write(sys.stdout.fileno(), data)

    def drain(self):
        while select.select([self.fd], [], [], 0)[0]:
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            self.show(chunk)

    def request_exit(self):
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGTERM)

    def close(self):
        os.close(self.fd)


class _WindowsConsole:
    def __init__(self, argv, env):
        self.events_queue = queue.Queue()
        self.held, self.later = [], []
        self.reader = self.modes = None
        self.stdin, self.stdout = _GetStdHandle(_STD_INPUT_HANDLE), _GetStdHandle(_STD_OUTPUT_HANDLE)
        self.size = _console_size(self.stdout)
        self.text = codecs.getincrementaldecoder('utf-8')('replace')
        # A process started in a new process group, such as under OpenSSH, ignores Ctrl+C and
        # passes that on to its children; the client must get Ctrl+C as in any terminal.
        _SetConsoleCtrlHandler(None, False)
        # The child starts below the current line instead of clearing the screen.
        self.pty = _PseudoConsole(argv, env, None, *self.size, self.events_queue.put, inherit_cursor=True)
        self.pid = self.pty.pid

    def resize(self):
        size = _console_size(self.stdout)
        if size != self.size:
            self.size = size
            self.pty.resize(*size)

    def raw(self):
        self.modes = (_console_mode(self.stdin), _console_mode(self.stdout))
        if self.modes[0] is not None:
            _SetConsoleMode(self.stdin, (self.modes[0] & ~(_ENABLE_PROCESSED_INPUT | _ENABLE_LINE_INPUT |
                                         _ENABLE_ECHO_INPUT)) | _ENABLE_VIRTUAL_TERMINAL_INPUT)
        if self.modes[1] is not None:
            _SetConsoleMode(self.stdout, self.modes[1] | _ENABLE_VIRTUAL_TERMINAL_PROCESSING |
                            _DISABLE_NEWLINE_AUTO_RETURN)
        self.stopping = threading.Event()
        self.reader = threading.Thread(target=self.keyboard, name='console-input', daemon=True)
        self.reader.start()

    def keyboard(self):
        # Wait with a timeout instead of inside a read: restore() must end this thread before
        # the next client starts, or a pending read takes that client's input, such as the
        # terminal's answer to its cursor position query.
        decoder = codecs.getincrementaldecoder('utf-16-le')('replace')
        records, count = (_INPUT_RECORD*256)(), wintypes.DWORD()
        while not self.stopping.is_set():
            waited = _WaitForSingleObject(self.stdin, 100)
            if waited == _WAIT_TIMEOUT:
                continue
            if waited != _WAIT_OBJECT_0 or not _ReadConsoleInputW(self.stdin, records, 256, ctypes.byref(count)):
                return
            # Virtual terminal input arrives as characters of key presses.
            units = b''.join(struct.pack('<H', r.key.char)*r.key.repeat for r in records[:count.value]
                             if r.type == _KEY_EVENT and r.key.down and r.key.char)
            text = decoder.decode(units)
            if text:
                self.events_queue.put(('input', text.encode('utf-8')))

    def restore(self):
        if self.reader is not None:
            self.stopping.set()
            self.reader.join(timeout=1)
            self.reader = None
        if self.modes is not None:
            for handle, mode in zip((self.stdin, self.stdout), self.modes):
                if mode is not None:
                    _SetConsoleMode(handle, mode)
            self.modes = None

    def exit_code(self):
        return self.pty.poll()

    def events(self, timeout, keyboard=True):
        self.resize()
        while self.later and self.later[0][0] <= time.monotonic():
            if self.pty.poll() is None:
                self.pty.write(self.later[0][1])
            self.later.pop(0)
        items = []
        with contextlib.suppress(queue.Empty):
            items.append(self.events_queue.get(timeout=timeout))
            while True:
                items.append(self.events_queue.get_nowait())
        result = []
        if keyboard:
            result, self.held = self.held, []
        for kind, data in items:
            (result if keyboard or kind == 'output' else self.held).append((kind, data))
        return result

    def typing(self):
        with self.events_queue.mutex:
            return bool(self.held) or any(kind == 'input' for kind, _ in self.events_queue.queue)

    def send(self, data):
        self.pty.write(data)

    def show(self, data):
        text = self.text.decode(data)
        if not text:
            return
        if _console_mode(self.stdout) is None:
            sys.stdout.buffer.write(text.encode('utf-8'))
            sys.stdout.buffer.flush()
            return
        written = wintypes.DWORD()
        # WriteConsoleW takes at most about 32k characters per call.
        for start in range(0, len(text), 8192):
            part = text[start:start+8192]
            _WriteConsoleW(self.stdout, part, len(part.encode('utf-16-le'))//2, ctypes.byref(written), None)

    def drain(self):
        """Output the console host renders just after the client exits."""
        deadline, quiet = time.monotonic()+1, time.monotonic()+.15
        while time.monotonic() < min(deadline, quiet):
            for kind, data in self.events(.05, keyboard=False):
                if kind == 'output' and data:
                    self.show(data)
                    quiet = time.monotonic()+.15

    def request_exit(self):
        # Windows has no SIGTERM: press Ctrl+C twice the way a person exits the client (the
        # second press within about a second), and twice more if a draft took the first press.
        # Presses stop once the client has exited.
        if self.pty.poll() is None:
            self.pty.write(b'\x03')
            now = time.monotonic()
            self.later = [(now+.3, b'\x03'), (now+3, b'\x03'), (now+3.3, b'\x03')]

    def close(self):
        self.pty.close(0)


if WINDOWS:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _ntdll = ctypes.WinDLL('ntdll')
    _shell32 = ctypes.WinDLL('shell32', use_last_error=True)

    def _api(dll, name, restype, *argtypes):
        try:
            function = getattr(dll, name)
        except AttributeError:
            return None
        function.restype, function.argtypes = restype, argtypes
        return function

    _HANDLE, _DWORD, _BOOL = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
    _LPDWORD = ctypes.POINTER(wintypes.DWORD)
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    _PROCESS_QUERY_LIMITED_INFORMATION, _SYNCHRONIZE, _PROCESS_TERMINATE = 0x1000, 0x100000, 0x1
    _WAIT_TIMEOUT, _WAIT_OBJECT_0 = 0x102, 0
    _PROCESS_COMMAND_LINE = 60
    _GENERIC_READ, _GENERIC_WRITE = 0x80000000, 0x40000000
    _OPEN_ALWAYS, _FILE_ATTRIBUTE_NORMAL, _FILE_FLAG_OPEN_REPARSE_POINT = 4, 0x80, 0x200000
    _OPEN_EXISTING, _FILE_READ_ATTRIBUTES, _FILE_FLAG_BACKUP_SEMANTICS = 3, 0x80, 0x2000000
    _FILE_SHARE_ALL = 0x7
    _ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION, _ERROR_IO_PENDING = 32, 33, 997
    _LOCKFILE_FAIL_IMMEDIATELY, _LOCKFILE_EXCLUSIVE_LOCK = 1, 2
    _STD_INPUT_HANDLE, _STD_OUTPUT_HANDLE = -10, -11
    _ENABLE_PROCESSED_INPUT, _ENABLE_LINE_INPUT, _ENABLE_ECHO_INPUT = 0x1, 0x2, 0x4
    _ENABLE_VIRTUAL_TERMINAL_INPUT = 0x200
    _ENABLE_VIRTUAL_TERMINAL_PROCESSING, _DISABLE_NEWLINE_AUTO_RETURN = 0x4, 0x8
    _EXTENDED_STARTUPINFO_PRESENT, _CREATE_UNICODE_ENVIRONMENT = 0x80000, 0x400
    _STARTF_USESTDHANDLES = 0x100
    _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x20016
    _PSEUDOCONSOLE_INHERIT_CURSOR = 0x1
    _TH32CS_SNAPPROCESS = 0x2
    _KEY_EVENT = 0x1

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [('dwSize', _DWORD), ('cntUsage', _DWORD), ('th32ProcessID', _DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', _DWORD),
                    ('cntThreads', _DWORD), ('th32ParentProcessID', _DWORD),
                    ('pcPriClassBase', wintypes.LONG), ('dwFlags', _DWORD),
                    ('szExeFile', wintypes.WCHAR*260)]

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [('Length', wintypes.USHORT), ('MaximumLength', wintypes.USHORT),
                    ('Buffer', ctypes.c_void_p)]

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                    ('Offset', _DWORD), ('OffsetHigh', _DWORD), ('hEvent', _HANDLE)]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [('cb', _DWORD), ('lpReserved', wintypes.LPWSTR), ('lpDesktop', wintypes.LPWSTR),
                    ('lpTitle', wintypes.LPWSTR), ('dwX', _DWORD), ('dwY', _DWORD),
                    ('dwXSize', _DWORD), ('dwYSize', _DWORD), ('dwXCountChars', _DWORD),
                    ('dwYCountChars', _DWORD), ('dwFillAttribute', _DWORD), ('dwFlags', _DWORD),
                    ('wShowWindow', wintypes.WORD), ('cbReserved2', wintypes.WORD),
                    ('lpReserved2', ctypes.c_void_p), ('hStdInput', _HANDLE),
                    ('hStdOutput', _HANDLE), ('hStdError', _HANDLE)]

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [('StartupInfo', _STARTUPINFOW), ('lpAttributeList', ctypes.c_void_p)]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [('hProcess', _HANDLE), ('hThread', _HANDLE),
                    ('dwProcessId', _DWORD), ('dwThreadId', _DWORD)]

    class _KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [('down', _BOOL), ('repeat', wintypes.WORD), ('key', wintypes.WORD),
                    ('scan', wintypes.WORD), ('char', wintypes.WORD), ('state', _DWORD)]

    class _INPUT_RECORD(ctypes.Structure):
        # Only key events are read; the union's other members are no larger.
        _fields_ = [('type', wintypes.WORD), ('key', _KEY_EVENT_RECORD)]

    class _FILE_BASIC_INFO(ctypes.Structure):
        _fields_ = [('CreationTime', ctypes.c_longlong), ('LastAccessTime', ctypes.c_longlong),
                    ('LastWriteTime', ctypes.c_longlong), ('ChangeTime', ctypes.c_longlong),
                    ('FileAttributes', _DWORD)]

    class _CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
        _fields_ = [('dwSize', wintypes._COORD), ('dwCursorPosition', wintypes._COORD),
                    ('wAttributes', wintypes.WORD), ('srWindow', wintypes.SMALL_RECT),
                    ('dwMaximumWindowSize', wintypes._COORD)]

    _OpenProcess = _api(_k32, 'OpenProcess', _HANDLE, _DWORD, _BOOL, _DWORD)
    _CloseHandle = _api(_k32, 'CloseHandle', _BOOL, _HANDLE)
    _GetProcessTimes = _api(_k32, 'GetProcessTimes', _BOOL, _HANDLE,
                            *[ctypes.POINTER(wintypes.FILETIME)]*4)
    _WaitForSingleObject = _api(_k32, 'WaitForSingleObject', _DWORD, _HANDLE, _DWORD)
    _GetExitCodeProcess = _api(_k32, 'GetExitCodeProcess', _BOOL, _HANDLE, _LPDWORD)
    _TerminateProcess = _api(_k32, 'TerminateProcess', _BOOL, _HANDLE, wintypes.UINT)
    _CreateToolhelp32Snapshot = _api(_k32, 'CreateToolhelp32Snapshot', _HANDLE, _DWORD, _DWORD)
    _Process32FirstW = _api(_k32, 'Process32FirstW', _BOOL, _HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
    _Process32NextW = _api(_k32, 'Process32NextW', _BOOL, _HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
    _NtQueryInformationProcess = _api(_ntdll, 'NtQueryInformationProcess', wintypes.LONG, _HANDLE,
                                      wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
                                      ctypes.POINTER(wintypes.ULONG))
    _CommandLineToArgvW = _api(_shell32, 'CommandLineToArgvW', ctypes.POINTER(wintypes.LPWSTR),
                               wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    _LocalFree = _api(_k32, 'LocalFree', ctypes.c_void_p, ctypes.c_void_p)
    _CreateFileW = _api(_k32, 'CreateFileW', _HANDLE, wintypes.LPCWSTR, _DWORD, _DWORD,
                        ctypes.c_void_p, _DWORD, _DWORD, _HANDLE)
    _LockFileEx = _api(_k32, 'LockFileEx', _BOOL, _HANDLE, _DWORD, _DWORD, _DWORD, _DWORD,
                       ctypes.POINTER(_OVERLAPPED))
    _GetShortPathNameW = _api(_k32, 'GetShortPathNameW', _DWORD, wintypes.LPCWSTR, wintypes.LPWSTR, _DWORD)
    _CreatePipe = _api(_k32, 'CreatePipe', _BOOL, ctypes.POINTER(_HANDLE), ctypes.POINTER(_HANDLE),
                       ctypes.c_void_p, _DWORD)
    _ReadFile = _api(_k32, 'ReadFile', _BOOL, _HANDLE, ctypes.c_void_p, _DWORD, _LPDWORD, ctypes.c_void_p)
    _WriteFile = _api(_k32, 'WriteFile', _BOOL, _HANDLE, ctypes.c_void_p, _DWORD, _LPDWORD, ctypes.c_void_p)
    _CreatePseudoConsole = _api(_k32, 'CreatePseudoConsole', wintypes.LONG, wintypes._COORD, _HANDLE,
                                _HANDLE, _DWORD, ctypes.POINTER(_HANDLE))
    _ResizePseudoConsole = _api(_k32, 'ResizePseudoConsole', wintypes.LONG, _HANDLE, wintypes._COORD)
    _ClosePseudoConsole = _api(_k32, 'ClosePseudoConsole', None, _HANDLE)
    _InitializeProcThreadAttributeList = _api(_k32, 'InitializeProcThreadAttributeList', _BOOL,
                                              ctypes.c_void_p, _DWORD, _DWORD, ctypes.POINTER(ctypes.c_size_t))
    _UpdateProcThreadAttribute = _api(_k32, 'UpdateProcThreadAttribute', _BOOL, ctypes.c_void_p, _DWORD,
                                      ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.c_void_p, ctypes.c_void_p)
    _DeleteProcThreadAttributeList = _api(_k32, 'DeleteProcThreadAttributeList', None, ctypes.c_void_p)
    _CreateProcessW = _api(_k32, 'CreateProcessW', _BOOL, wintypes.LPCWSTR, wintypes.LPWSTR,
                           ctypes.c_void_p, ctypes.c_void_p, _BOOL, _DWORD, ctypes.c_void_p,
                           wintypes.LPCWSTR, ctypes.POINTER(_STARTUPINFOEXW),
                           ctypes.POINTER(_PROCESS_INFORMATION))
    _GetStdHandle = _api(_k32, 'GetStdHandle', _HANDLE, _DWORD)
    _GetConsoleMode = _api(_k32, 'GetConsoleMode', _BOOL, _HANDLE, _LPDWORD)
    _SetConsoleMode = _api(_k32, 'SetConsoleMode', _BOOL, _HANDLE, _DWORD)
    _GetFileInformationByHandleEx = _api(_k32, 'GetFileInformationByHandleEx', _BOOL, _HANDLE,
                                         ctypes.c_int, ctypes.c_void_p, _DWORD)
    _SetConsoleCtrlHandler = _api(_k32, 'SetConsoleCtrlHandler', _BOOL, ctypes.c_void_p, _BOOL)
    _GetConsoleScreenBufferInfo = _api(_k32, 'GetConsoleScreenBufferInfo', _BOOL, _HANDLE,
                                       ctypes.POINTER(_CONSOLE_SCREEN_BUFFER_INFO))
    _ReadConsoleInputW = _api(_k32, 'ReadConsoleInputW', _BOOL, _HANDLE, ctypes.POINTER(_INPUT_RECORD),
                              _DWORD, _LPDWORD)
    _WriteConsoleW = _api(_k32, 'WriteConsoleW', _BOOL, _HANDLE, wintypes.LPCWSTR, _DWORD, _LPDWORD,
                          ctypes.c_void_p)

    def _change_time(handle):
        info = _FILE_BASIC_INFO()
        if not _GetFileInformationByHandleEx(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.ChangeTime

    def _open_process(pid, access):
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None
        return _OpenProcess(access, False, pid) if pid > 0 else None

    def _snapshot():
        """(pid, parent pid, executable name) of every process."""
        handle = _CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if handle in (None, _INVALID_HANDLE):
            return []
        try:
            entry, result = _PROCESSENTRY32W(), []
            entry.dwSize = ctypes.sizeof(entry)
            found = _Process32FirstW(handle, ctypes.byref(entry))
            while found:
                result.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
                found = _Process32NextW(handle, ctypes.byref(entry))
            return result
        finally:
            _CloseHandle(handle)

    def _console_mode(handle):
        mode = wintypes.DWORD()
        return mode.value if handle and _GetConsoleMode(handle, ctypes.byref(mode)) else None

    def _console_size(handle):
        info = _CONSOLE_SCREEN_BUFFER_INFO()
        if not _GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return (120, 40)
        window = info.srWindow
        return (max(20, window.Right-window.Left+1), max(5, window.Bottom-window.Top+1))

    class _PseudoConsole:
        """One client process in a ConPTY; its output goes to sink as ('output', bytes)."""

        def __init__(self, argv, env, cwd, cols, rows, sink, inherit_cursor=False):
            if _CreatePseudoConsole is None:
                raise OSError('Windows 10 version 1809 or later is required (pseudo console).')
            self.sink, self.status = sink, None
            in_read, self.input, self.output, out_write = _HANDLE(), _HANDLE(), _HANDLE(), _HANDLE()
            if (not _CreatePipe(ctypes.byref(in_read), ctypes.byref(self.input), None, 0) or
                    not _CreatePipe(ctypes.byref(self.output), ctypes.byref(out_write), None, 0)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.console = _HANDLE()
            result = _CreatePseudoConsole(wintypes._COORD(cols, rows), in_read, out_write,
                                          _PSEUDOCONSOLE_INHERIT_CURSOR if inherit_cursor else 0,
                                          ctypes.byref(self.console))
            # The console host holds its own copies of these pipe ends.
            _CloseHandle(in_read)
            _CloseHandle(out_write)
            if result < 0:
                _CloseHandle(self.input)
                _CloseHandle(self.output)
                raise OSError(f'CreatePseudoConsole failed: 0x{result & 0xFFFFFFFF:08X}')
            self.reader = threading.Thread(target=self.pump, name='pseudo-console-output', daemon=True)
            self.reader.start()
            try:
                self.process, self.pid = self.spawn(argv, env, cwd)
            except BaseException:
                self.close(0)
                raise

        def spawn(self, argv, env, cwd):
            size = ctypes.c_size_t(0)
            _InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            attributes = ctypes.create_string_buffer(size.value)
            if not _InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not _UpdateProcThreadAttribute(attributes, 0, _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                                                  self.console, ctypes.sizeof(_HANDLE), None, None):
                    raise ctypes.WinError(ctypes.get_last_error())
                info = _STARTUPINFOEXW()
                info.StartupInfo.cb = ctypes.sizeof(info)
                # No standard handles of ours: the client's come from the pseudo console.
                info.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
                info.lpAttributeList = ctypes.addressof(attributes)
                block = ''.join(f'{k}={v}\0' for k, v in sorted(env.items(), key=lambda i: i[0].upper()))
                environment = ctypes.create_unicode_buffer(block+'\0')
                command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
                process = _PROCESS_INFORMATION()
                if not _CreateProcessW(None, command, None, None, False,
                                       _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT,
                                       environment, None if cwd is None else str(cwd),
                                       ctypes.byref(info), ctypes.byref(process)):
                    raise ctypes.WinError(ctypes.get_last_error())
                _CloseHandle(process.hThread)
                return process.hProcess, process.dwProcessId
            finally:
                _DeleteProcThreadAttributeList(attributes)

        def pump(self):
            buffer, count = ctypes.create_string_buffer(65536), wintypes.DWORD()
            while _ReadFile(self.output, buffer, 65536, ctypes.byref(count), None) and count.value:
                self.sink(('output', buffer.raw[:count.value]))
            self.sink(('output', b''))

        def write(self, data):
            view, written = memoryview(data), wintypes.DWORD()
            while view:
                chunk = bytes(view[:65536])
                if not _WriteFile(self.input, chunk, len(chunk), ctypes.byref(written), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                view = view[written.value:]

        def resize(self, cols, rows):
            _ResizePseudoConsole(self.console, wintypes._COORD(cols, rows))

        def poll(self):
            if self.status is None and _WaitForSingleObject(self.process, 0) == _WAIT_OBJECT_0:
                code = wintypes.DWORD()
                _GetExitCodeProcess(self.process, ctypes.byref(code))
                self.status = code.value
            return self.status

        def close(self, grace):
            """Closing the pseudo console sends the client CTRL_CLOSE_EVENT; it is ended
            after grace seconds if it is still running."""
            console, self.console = self.console, None
            if console:
                _ClosePseudoConsole(console)
            process = getattr(self, 'process', None)
            if process:
                if self.poll() is None and _WaitForSingleObject(process, int(grace*1000)) != _WAIT_OBJECT_0:
                    _TerminateProcess(process, 1)
                    _WaitForSingleObject(process, 3000)
                self.poll()
                _CloseHandle(process)
                self.process = None
            self.reader.join(timeout=2)
            for name in ('input', 'output'):
                handle = getattr(self, name)
                if handle:
                    _CloseHandle(handle)
                    setattr(self, name, None)
