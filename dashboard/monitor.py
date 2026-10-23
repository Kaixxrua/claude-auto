#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Local Claude quota dashboard using the CLI and undocumented usage APIs."""
from __future__ import annotations

import argparse
import calendar
import contextlib
import copy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.machinery
import importlib.util
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid
import zoneinfo

HERE = Path(__file__).resolve().parent
# The OS layer: installed in share/claude-auto/lib, or lib/ in a source checkout.
sys.path.insert(0, str(next((d for d in (HERE.parent/'claude-auto/lib', HERE.parent/'lib')
                             if (d/'claude_auto_os.py').is_file()), HERE.parent/'lib')))
import claude_auto_os as oslayer  # noqa: E402
from reset_cards import ResetCards, CardError, OfficialClient, timestamp, token  # noqa: E402

VERSION = '1.1.1'
UTC = timezone.utc
PROFILE_RE = re.compile(r'[a-zA-Z0-9_-]{1,64}\Z')
ANSI = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[78=>]')
HEADINGS = {
    'Current session': 'five_hour',
    'Current week (all models)': 'seven_day',
    'Current week (Opus)': 'seven_day_opus',
    'Current week (Sonnet)': 'seven_day_sonnet',
}
MONTHS = {name: i for i, name in enumerate(
    ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}


def load_json(path, default=None):
    try:
        return json.loads(oslayer.read_bytes(path))
    except (OSError, ValueError):
        return copy.deepcopy(default)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | oslayer.BINARY, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        oslayer.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


DELEGATION_ACTIVE = ('queued', 'selecting', 'waiting', 'running')
OFFICIAL_BUSY_SECONDS = 60
OFFICIAL_IDLE_SECONDS = 240
OFFICIAL_MAX_BACKOFF = 900
PROBE_RETRY_SECONDS = 5
PAID_PLANS = ('claude_pro', 'claude_max', 'claude_team', 'claude_enterprise')
# Consumer subscriptions renew on the anniversary of their start; contracts do not follow it.
RENEWING_BILLING = ('apple_subscription', 'google_play_subscription', 'stripe_subscription')
PLAN_MONTHS = {'monthly': 1, 'annual': 12}
_session_cache = {}
_plan_cache = {}


def process_ticks(pid):
    """Process start time, compared the same way claude-delegate does, so a reused PID never counts as the job."""
    return oslayer.process_start(pid)


def session_limits(path, tail=1 << 20):
    """Latest rate_limit_event in a delegated print session's output: the official per-response
    quota, as the status line shows it for interactive clients. Utilization is a fraction there."""
    try:
        stat = path.stat()
    except OSError:
        return None
    cached = _session_cache.get(str(path))
    if cached and cached[0] == (stat.st_size, stat.st_mtime_ns):
        return cached[1]
    value = None
    with contextlib.suppress(OSError):
        with path.open('rb') as stream:
            stream.seek(max(0, stat.st_size-tail))
            lines = stream.read().split(b'\n')
        for line in reversed(lines):
            if b'"rate_limit_event"' not in line[:64]:
                continue
            with contextlib.suppress(ValueError, AttributeError, TypeError, KeyError):
                info = json.loads(line)['rate_limit_info']
                windows = {}
                for key in ('five_hour', 'seven_day'):
                    w = info['unifiedWindows'].get(key)
                    if isinstance(w, dict) and finite(w.get('utilization')) and 0 <= w['utilization'] <= 2:
                        reset = w.get('resetsAt')
                        windows[key] = {'used_percentage': round(min(w['utilization'], 1)*100, 1),
                                        'resets_at': reset if finite(reset) else None}
                if info.get('status') == 'rejected' and info.get('rateLimitType') in windows:
                    windows[info['rateLimitType']]['used_percentage'] = 100.0
                if windows:
                    value = {'windows': windows, 'status': info.get('status'), 'at': stat.st_mtime}
                    break
    if len(_session_cache) > 64:
        _session_cache.clear()
    _session_cache[str(path)] = ((stat.st_size, stat.st_mtime_ns), value)
    return value


def delegation_jobs(root, now, keep=86400, limit=12):
    """Cross-account delegated jobs as summaries only; prompts and reports stay in their private files."""
    jobs = []
    for path in (Path(root)/'state/delegation/jobs').glob('*/job.json'):
        d = load_json(path)
        if not isinstance(d, dict) or not isinstance(d.get('job_id'), str):
            continue
        status = d.get('status')
        updated = d['updated_at'] if finite(d.get('updated_at')) else 0
        if status not in DELEGATION_ACTIVE and now - updated > keep:
            continue
        alive = bool(d.get('native_pid')) and process_ticks(d['native_pid']) == d.get('native_start')
        worker = bool(d.get('worker_pid')) and process_ticks(d['worker_pid']) == d.get('worker_start')
        if status in DELEGATION_ACTIVE and d.get('worker_pid') and not worker:
            status = 'interrupted'
        # The session history is written on every model turn and tool result: the best "still working" signal.
        activity = None
        if d.get('history_root') and isinstance(d.get('session_id'), str):
            with contextlib.suppress(OSError):
                times = [p.stat().st_mtime for p in Path(d['history_root']).glob(f"*/{d['session_id']}.jsonl")]
                activity = max(times) if times else None
        limits = None
        if alive:
            limits = session_limits(path.parent/f"events-{d.get('attempt')}.jsonl")
            saved = d.get('session_limits')
            if not limits and isinstance(saved, dict) and isinstance(saved.get('windows'), dict) and finite(saved.get('at')):
                limits = {'windows': saved['windows'], 'status': saved.get('status'), 'at': saved['at']}
        jobs.append({'job': d['job_id'][:8], 'title': d.get('title'), 'status': status, 'reason': d.get('reason'),
                     'account': d.get('account'), 'attempt': d.get('attempt'), 'mode': d.get('mode'),
                     'model': d.get('model'), 'project': Path(d['cwd']).name if d.get('cwd') else None,
                     'session': d['session_id'][:8] if isinstance(d.get('session_id'), str) else None,
                     'created_at': d.get('created_at'), 'updated_at': updated or None,
                     'activity_at': activity, 'alive': alive, 'permission_denials': d.get('permission_denials'),
                     'shared': d.get('shared') is True, 'continues': d.get('continues') or 0,
                     'auto_stop': d.get('auto_stop'), 'limits': limits})
    # Active jobs first in launch order, then the most recently finished.
    jobs.sort(key=lambda j: (0, j['created_at'] or 0) if j['status'] in DELEGATION_ACTIVE else (1, -(j['updated_at'] or 0)))
    return jobs[:limit]


def delegation_policy(root, now):
    """Which automatic behaviours the delegation policy turns on, and whether the supervisor runs."""
    base = Path(root)/'state/delegation'
    raw = load_json(base/'policy.json', {})
    raw = raw if isinstance(raw, dict) else {}
    value = {key: raw.get(key) is True for key in
             ('share_when_short', 'wait_for_account', 'auto_continue', 'enforce_model', 'avoid_interactive_accounts')}
    cap = raw.get('interactive_cap')
    value['interactive_cap'] = cap if type(cap) is int and 0 < cap <= 100 else 0
    beat = load_json(base/'supervisor.json', {})
    beat = beat if isinstance(beat, dict) else {}
    value['supervisor'] = bool(finite(beat.get('at')) and now - beat['at'] < 180 and beat.get('pid')
                               and process_ticks(beat['pid']) == beat.get('start'))
    return value


def oauth_windows(payload, at):
    """Windows from the official usage API, in the record format /usage readings use."""
    windows = {}
    for key in ('five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet'):
        raw = payload.get(key)
        if not isinstance(raw, dict):
            continue
        used, reset = raw.get('utilization'), timestamp(raw.get('resets_at'))
        if not finite(used) or not 0 <= used <= 100:
            continue
        windows[key] = {'used_percentage': used, 'resets_at': reset,
                        'reset_precision': 'second', 'observed_at': at,
                        'source': 'official_oauth_api'}
    return windows


def token_expiry(registry, name):
    """Expiry of the stored login, read without using the token."""
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
        auth = json.loads(oslayer.read_bytes(Path(registry['profiles'][name])/'.credentials.json'))['claudeAiOauth']
        if finite(auth.get('expiresAt')):
            return auth['expiresAt']/1000
    return None


def billing_period(payload):
    """Monthly or annual, from the reset-card block of the official usage response."""
    card = payload.get('cedar_ember') if isinstance(payload, dict) else None
    props = card.get('event_props') if isinstance(card, dict) else None
    value = props.get('billing_period') if isinstance(props, dict) else None
    return value if value in PLAN_MONTHS else None


def client_plan(directory):
    """The official client's copy of its last profile read. It covers accounts this monitor
    no longer queries, such as disabled ones."""
    directory = Path(directory)
    path = Path.home()/'.claude.json' if directory == Path.home()/'.claude' else directory/'.claude.json'
    try:
        stat = path.stat()
    except OSError:
        return None
    cached = _plan_cache.get(str(path))
    if cached and cached[0] == (stat.st_size, stat.st_mtime_ns):
        return cached[1]
    data = load_json(path, {})
    account = data.get('oauthAccount') if isinstance(data, dict) else None
    value = None
    if isinstance(account, dict) and isinstance(account.get('emailAddress'), str):
        fetched = account.get('profileFetchedAt')
        value = {'email': account['emailAddress'], 'type': token(account.get('organizationType')),
                 'billing': token(account.get('billingType')), 'status': None, 'period': None,
                 'started_at': timestamp(account.get('subscriptionCreatedAt')),
                 'checked_at': fetched/1000 if finite(fetched) else None, 'source': 'client_profile'}
    if len(_plan_cache) > 64:
        _plan_cache.clear()
    _plan_cache[str(path)] = ((stat.st_size, stat.st_mtime_ns), value)
    return value


def add_months(start, months):
    month = start.month - 1 + months
    year, month = start.year + month // 12, month % 12 + 1
    return start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))


def plan_view(plan, now):
    """The official profile reports when a subscription started, never when it ends. A plan
    still paid after an anniversary has renewed, so the current period ends at the next one:
    a month or a year apart, monthly when the usage response does not say."""
    if not isinstance(plan, dict):
        return None
    view = {key: plan.get(key) for key in
            ('type', 'billing', 'status', 'period', 'started_at', 'checked_at', 'source')}
    view.update(ends_at=None, renewals=None)
    start = plan.get('started_at')
    if (view['type'] in PAID_PLANS and view['billing'] in RENEWING_BILLING
            and finite(start) and 0 < start <= now + 86400):
        begin, current = datetime.fromtimestamp(start, UTC), datetime.fromtimestamp(now, UTC)
        step = PLAN_MONTHS.get(view['period'], 1)
        cycles = max(1, ((current.year - begin.year)*12 + current.month - begin.month) // step)
        while add_months(begin, cycles*step).timestamp() <= now:
            cycles += 1
        view.update(ends_at=add_months(begin, cycles*step).timestamp(), renewals=cycles-1)
    return view


def official_usage(registry, name, client_factory=OfficialClient):
    """One authenticated read of the official usage API; no model request. It also reports a
    session window that has not started yet. An expired login is left to the official client,
    which refreshes it on its next start."""
    expires = token_expiry(registry, name)
    if expires is not None and expires <= time.time()+60:
        raise CardError('token_expired', '登录凭据已过期，等官方客户端刷新', 401)
    identity, payload = client_factory(registry, name).read()
    return identity['email'], oauth_windows(payload, time.time())


def reset_zone(name):
    """The zone /usage names: (UTC) as requested on Linux. Claude Code on Windows ignores TZ and
    shows the system zone, which Python there may lack without tzdata; local time is that zone.
    None means local time; False an unrecognized name."""
    if name.upper() == 'UTC':
        return UTC
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return None if re.fullmatch(r'[A-Za-z]+(?:/[\w+-]+)+', name) else False


def parse_reset(text, now, window):
    """Native /usage renders minute precision in the named zone."""
    match = re.fullmatch(
        r'Resets (?:(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{1,2}), )?'
        r'(\d{1,2})(?::(\d{2}))?(am|pm) \(([\w/+-]{1,64})\)', text.strip(), re.I)
    if not match:
        return None
    month, day, hour, minute, half, zone = match.groups()
    hour, minute = int(hour), int(minute or 0)
    if not 1 <= hour <= 12 or not 0 <= minute < 60:
        return None
    zone = reset_zone(zone)
    if zone is False:
        return None
    hour = hour % 12 + (12 if half.lower() == 'pm' else 0)
    dt = datetime.fromtimestamp(now, zone)
    if month:
        candidates = []
        for year in (dt.year - 1, dt.year, dt.year + 1):
            with contextlib.suppress(ValueError):
                candidates.append(datetime(year, MONTHS[month.title()], int(day), hour, minute, tzinfo=zone))
        candidates = [item for item in candidates if -120 <= (item - dt).total_seconds() <= 8 * 86400]
        return min(candidates, key=lambda item: abs((item-dt).total_seconds())).timestamp() if candidates else None
    target = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target.timestamp() < now - 120:
        target += timedelta(days=1)
    if window == 'five_hour' and target.timestamp() - now > 6 * 3600:
        return None
    return target.timestamp()


def parse_usage(raw, now=None):
    now = time.time() if now is None else now
    text = ANSI.sub('', raw).replace('\r', '')
    windows = {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        key = HEADINGS.get(line.strip())
        if not key:
            continue
        part = []
        for following in lines[index + 1:index + 7]:
            if following.strip() in HEADINGS or following.startswith(('Usage credits', "What's contributing")):
                break
            part.append(following.strip())
        block = '\n'.join(part)
        matches = re.findall(r'(?<![\d.])(\d+(?:\.\d+)?)%\s+used', block)
        if not matches:
            continue
        pct = float(matches[-1])
        if not 0 <= pct <= 100:
            continue
        reset_line = next((item for item in part if item.startswith('Resets ')), None)
        reset = parse_reset(reset_line, now, key) if reset_line else None
        windows[key] = {'used_percentage': pct, 'resets_at': reset,
                        'reset_display': reset_line, 'reset_precision': 'minute',
                        'observed_at': now, 'source': 'official_cli_usage'}
    return windows


class ProbeError(RuntimeError):
    pass


def probe_usage(binary, env, cwd, stop_event=None, timeout=18):
    """Open only /usage in a separate client; no continue/resume or model prompt."""
    env = dict(env)
    env.update(TERM='xterm-256color', TZ='UTC', LANG='C.UTF-8', LC_ALL='C.UTF-8')
    output = bytearray()
    windows = {}
    found_at = None
    terminal = oslayer.Terminal([binary, '--safe-mode', '--strict-mcp-config', '--ax-screen-reader',
                                 '--tools', '', '--', '/usage'], env, cwd, cols=220, rows=80)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and terminal.poll() is None:
            if stop_event and stop_event.is_set():
                raise ProbeError('服务正在停止')
            data = terminal.read(.15)
            if data == b'':
                break
            if data:
                output.extend(data)
                if len(output) > 2_000_000:
                    raise ProbeError('官方客户端输出异常，已停止此次查询')
                windows = parse_usage(output.decode(errors='replace'))
                if all(key in windows for key in ('five_hour', 'seven_day')) and found_at is None:
                    found_at = time.monotonic()
            # Wait briefly for the whole UI section, not the local transcript analytics scan.
            if found_at and time.monotonic() - found_at > .4:
                break
        if not windows:
            clean = ANSI.sub('', output.decode(errors='replace'))
            if any(word in clean.lower() for word in ('trust this folder', 'safety check', 'choose the text style')):
                raise ProbeError('需要先在终端完成此账号的 Claude Code 首次设置')
            if any(word in clean.lower() for word in ('not logged in', 'please log in', 'sign in', 'invalid authentication')):
                raise ProbeError('登录已过期，请在终端重新登录此账号')
            raise ProbeError('官方 /usage 未返回可识别的额度（网络、登录或客户端格式变化）')
        return windows
    finally:
        # This is our fresh read-only probe, never a user's work session.
        terminal.close()


def probe_complete(binary, env, cwd, stop_event=None, known=None):
    """/usage, read once more when only the weekly window shows. A new session window appears
    only after the account's first request in it, so a window known to have reset (or known to
    be unstarted) that is still missing after the retry is taken as unused. Without such a
    reading nothing is guessed."""
    windows = probe_usage(binary, env, cwd, stop_event)
    if 'seven_day' not in windows or 'five_hour' in windows:
        return windows
    if stop_event.wait(PROBE_RETRY_SECONDS) if stop_event else time.sleep(PROBE_RETRY_SECONDS):
        raise ProbeError('服务正在停止')
    with contextlib.suppress(ProbeError):
        windows = {**windows, **probe_usage(binary, env, cwd, stop_event)}
    if 'five_hour' in windows:
        return windows
    last = (known or {}).get('five_hour') or {}
    used, reset = last.get('used_percentage'), last.get('resets_at')
    if not finite(used) or not ((finite(reset) and reset <= time.time()) or (reset is None and used == 0)):
        return windows
    windows['five_hour'] = {'used_percentage': 0.0, 'resets_at': None, 'reset_precision': 'minute',
                            'observed_at': windows['seven_day']['observed_at'],
                            'source': 'official_cli_usage', 'inferred': 'session_not_started'}
    return windows


def load_account_module(path):
    loader = importlib.machinery.SourceFileLoader('claude_accounts_monitor_adapter', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Monitor:
    def __init__(self, root, adapter, cwd, interval=120):
        self.root = Path(root)
        self.state_dir = self.root / 'state/monitor'
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.adapter = adapter
        self.cwd = str(cwd)
        self.interval = max(60, interval)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.records = load_json(self.state_dir/'accounts.json', {})
        self.cards = load_json(self.state_dir/'reset-cards.json', {})
        self.busy = None
        self.last_request = 0
        self.requested = set()
        # Persist the initial scan across service restarts. Manual refresh is explicit.
        self.initialized = set(self.records)
        self.started_at = time.time()
        self.error = None
        self.worker = None
        self.registry = {}
        self.load_registry()
        self.reset_cards = ResetCards(self)
        # Light official usage reads between /usage queries; memory only, restarted staggered.
        self.official = {}
        self.official_error = None
        self.official_worker = None

    def load_registry(self):
        data = load_json(self.root/'accounts.json', {})
        if data.get('version') != 1 or not isinstance(data.get('profiles'), dict):
            raise ValueError('找不到已有的 claude-accounts 账号配置')
        if not all(PROFILE_RE.fullmatch(name) and Path(directory).is_absolute()
                   for name, directory in data['profiles'].items()):
            raise ValueError('账号配置格式不正确')
        if not Path(data.get('claude', '')).is_file():
            raise ValueError('找不到官方 Claude Code 客户端')
        with self.lock:
            self.registry = data
        return data

    def start(self):
        self.worker = threading.Thread(target=self.loop, name='official-usage-collector', daemon=True)
        self.worker.start()
        self.official_worker = threading.Thread(target=self.official_loop, name='official-usage-api', daemon=True)
        self.official_worker.start()

    def busy_accounts(self, now):
        busy = set(self.active_accounts())
        busy.update(j['account'] for j in delegation_jobs(self.root, now) if j['alive'] and j['account'])
        return busy

    def official_next(self, registry, busy, now):
        """The account whose usage read is due first: each minute while a session or delegated
        job uses it, every few minutes otherwise, and right after one of its windows resets."""
        best = (None, None)
        for index, name in enumerate(registry['profiles']):
            if name in registry.get('disabled', {}):
                continue
            with self.lock:
                record = self.records.get(name, {})
                state = self.official.setdefault(name, {'next_at': now+2*index})
            if record.get('loggedIn') is not True:
                continue
            due, last = state['next_at'], state.get('attempted_at', 0)
            if not state.get('failures'):
                if name in busy:
                    due = min(due, last+OFFICIAL_BUSY_SECONDS)
                for window in (record.get('windows') or {}).values():
                    reset = window.get('resets_at') if isinstance(window, dict) else None
                    if finite(reset) and last < reset+5 <= now:
                        due = min(due, reset+5)
            if best[0] is None or due < best[1]:
                best = (name, due)
        return best

    def ask_client(self, name, state, now):
        # The official client refreshes its own login when it starts; one /usage query does it.
        if now-state.get('cli_requested_at', 0) > 1800:
            state['cli_requested_at'] = now
            with self.lock:
                self.requested.add(name)
            self.wake.set()

    def poll_official(self, registry, name, busy):
        now = time.time()
        with self.lock:
            state = self.official.setdefault(name, {})
        state['attempted_at'] = now
        try:
            expires = token_expiry(registry, name)
            if expires is not None and expires <= now+60:
                self.ask_client(name, state, now)
                state.update(error='登录凭据待刷新，已请官方客户端查询一次', next_at=now+300)
                return
            self.reset_cards.query(name)
            done = time.time()
            state.update(error=None, failures=0, ok_at=done,
                         next_at=done+(OFFICIAL_BUSY_SECONDS if busy else OFFICIAL_IDLE_SECONDS))
        except CardError as exc:
            if exc.code == 'busy':
                state.update(next_at=now+30)
                return
            if exc.code == 'http_401':
                self.ask_client(name, state, now)
            failures = state.get('failures', 0)+1
            state.update(error=str(exc), failures=failures, next_at=now+min(OFFICIAL_MAX_BACKOFF, 60*2**failures))

    def official_loop(self):
        while not self.stop.is_set():
            try:
                registry = self.load_registry()
                now = time.time()
                busy = self.busy_accounts(now)
                name, due = self.official_next(registry, busy, now)
                if name and due <= now:
                    self.poll_official(registry, name, name in busy)
                    self.official_error = None
                    continue
                self.stop.wait(min(15, max(1, due-now)) if name else 15)
            except Exception as exc:
                # No credentials, responses or environment values in web responses/logs.
                self.official_error = '自动查询暂停：'+type(exc).__name__
                self.stop.wait(30)

    def refresh(self, profile=None):
        self.load_registry()
        now = time.time()
        with self.lock:
            names = set(self.registry['profiles'])
            if profile is not None and profile not in names:
                raise ValueError('账号不存在')
            disabled = set(self.registry.get('disabled', {}))
            if profile in disabled:
                raise ValueError('此账号已停用，不再查询')
            names -= disabled
            if now - self.last_request < 10:
                return False
            self.last_request = now
            self.requested.update({profile} if profile else names)
        self.wake.set()
        return True

    def loop(self):
        while not self.stop.is_set():
            try:
                registry = self.load_registry()
                now = time.time()
                with self.lock:
                    candidates = [name for name in registry['profiles']
                                  if name not in registry.get('disabled', {})
                                  and (name in self.requested or name not in self.initialized)]
                    candidates.sort(key=lambda name: (name not in self.requested,
                                                       self.records.get(name, {}).get('attempted_at', 0)))
                    name = candidates[0] if candidates else None
                    if name:
                        self.requested.discard(name)
                        self.initialized.add(name)
                        self.busy = name
                if name:
                    self.poll(registry, name)
                else:
                    self.wake.wait(2)
                    self.wake.clear()
                self.error = None
            except Exception as exc:
                # No subprocess stdout/stderr or environment values in web responses/logs.
                self.error = '采集器暂时无法工作：' + type(exc).__name__
                self.wake.wait(10)
                self.wake.clear()
            finally:
                with self.lock:
                    self.busy = None

    def poll(self, registry, name):
        registry = self.load_registry()
        if name in registry.get('disabled', {}):
            return
        at = time.time()
        with self.lock:
            record = copy.deepcopy(self.records.get(name, {}))
        record['attempted_at'] = at
        try:
            status = self.adapter.auth_status(registry, name)
            identity = status.get('email') or None
            if identity != record.get('email'):
                record = {'attempted_at': at, 'identity_changed_at': at}
            record.update({key: status.get(key) for key in ('email', 'loggedIn', 'subscriptionType', 'authMethod')})
            record['auth_checked_at'] = at
            if status.get('error'):
                raise ProbeError('无法确认登录状态，请稍后重试')
            if not self.adapter.eligible(status):
                record.update(error='此位置尚未登录 Claude 订阅账号', windows={}, checked_at=None)
            else:
                self.adapter.inherit_preferences(registry, name)
                windows = probe_complete(registry['claude'], self.adapter.profile_env(registry, name),
                                         self.cwd, self.stop, known=record.get('windows'))
                # A partial terminal render must not erase the other window.
                merged = dict(record.get('windows', {}))
                merged.update(windows)
                missing = [key for key in ('five_hour', 'seven_day') if key not in windows]
                labels = {'five_hour': '5 小时', 'seven_day': '7 天'}
                error = ('本次官方查询未返回' + '、'.join(labels[key] for key in missing) +
                         '额度，保留该窗口最近记录') if missing else None
                record.update(windows=merged, checked_at=time.time(), error=error,
                              missing_windows=missing, failures=0, usage_query_started_at=at)
                record['launcher_synced'] = self.sync_launcher_usage(name, windows, record['checked_at'], at)
                self.append_history(name, record)
        except ProbeError as exc:
            record['error'] = str(exc)
            record['failures'] = record.get('failures', 0) + 1
        except Exception as exc:
            record['error'] = '查询未完成：' + type(exc).__name__
            record['failures'] = record.get('failures', 0) + 1
        record.pop('next_poll_at', None)
        with self.lock:
            self.records[name] = record
            save_json(self.state_dir/'accounts.json', self.records)

    def append_history(self, name, record):
        # Seven days of small quota samples, never messages or authentication data.
        path = self.state_dir/'history'/f'{name}.json'
        history = load_json(path, [])
        now = time.time()
        history = [item for item in history if item.get('at', 0) > now - 7*86400]
        history.append({'at': now, 'email': record.get('email'), 'windows': record['windows']})
        save_json(path, history[-6000:])

    def sync_launcher_usage(self, name, windows, at, started_at):
        sync = getattr(self.adapter, 'record_quota_recovery', None)
        return bool(sync and sync(self.root/'state', name, windows, at, started_at))

    def record_official_usage(self, name, identity, payload, at, request_started_at=None, plan=None):
        """The reset query also returns a fresh quota snapshot and plan for the same identity."""
        windows = oauth_windows(payload, at)
        with self.lock:
            record = copy.deepcopy(self.records.get(name, {}))
            if (record.get('email') != identity['email'] or
                (record.get('official_identity') and record['official_identity'] != identity)):
                record = {'identity_changed_at': at}
            record.update(email=identity['email'], loggedIn=True, authMethod='claude.ai',
                          official_identity=identity, auth_checked_at=at, checked_at=at,
                          usage_query_started_at=request_started_at if request_started_at is not None else at)
            record.setdefault('windows', {}).update(windows)
            if isinstance(plan, dict):
                previous = record.get('plan') or {}
                record['plan'] = {**plan, 'period': billing_period(payload) or previous.get('period'),
                                  'checked_at': at, 'source': 'official_oauth_api'}
            missing = [k for k in ('five_hour', 'seven_day') if k not in windows]
            record.update(missing_windows=missing, error='官方查询未返回完整额度，保留最近记录' if missing else None)
            record['launcher_synced'] = self.sync_launcher_usage(name, windows, at, record['usage_query_started_at'])
            self.records[name] = record
            self.initialized.add(name)
            save_json(self.state_dir/'accounts.json', self.records)

    def observations(self):
        """Optional live session readings: label observation time, not a new server fetch."""
        result = {}
        for path in (self.root/'state/runs').glob('*/*-limits.json'):
            value = load_json(path, {})
            name, at = value.get('account'), value.get('at')
            if not isinstance(name, str) or not finite(at) or not isinstance(value.get('limits'), dict):
                continue
            for key in ('five_hour', 'seven_day'):
                item = value['limits'].get(key)
                if not isinstance(item, dict) or not finite(item.get('used_percentage')):
                    continue
                if not 0 <= item['used_percentage'] <= 100:
                    continue
                reset = item.get('resets_at')
                if reset is not None and not finite(reset):
                    continue
                existing = result.setdefault(name, {}).get(key, {})
                if at > existing.get('observed_at', 0):
                    result[name][key] = {**item, 'observed_at': at, 'source': 'session_statusline',
                                         'reset_precision': 'second'}
        return result

    def active_accounts(self):
        """Identify our existing native clients by their settings path, not tokens."""
        result = {}
        root = (self.root/'state/runs').resolve()
        for _, args in oslayer.commands('claude'):
            try:
                if '--settings' not in args:
                    continue
                path = Path(args[args.index('--settings') + 1]).resolve()
                if path.parent.parent != root or not path.name.endswith('-settings.json'):
                    continue
                name = path.name[:-len('-settings.json')]
                if name in self.registry['profiles']:
                    result[name] = result.get(name, 0) + 1
            except (OSError, IndexError, ValueError):
                continue
        return result

    def snapshot(self):
        self.load_registry()
        now = time.time()
        live = self.observations()
        active = self.active_accounts()
        jobs = delegation_jobs(self.root, now)
        quotas = self.adapter.read_quota_state(self.root/'state')
        with self.lock:
            records, cards = copy.deepcopy(self.records), copy.deepcopy(self.cards)
            names = list(self.registry['profiles'])
            profiles = dict(self.registry['profiles'])
            disabled = copy.deepcopy(self.registry.get('disabled', {}))
            busy, error = self.busy, self.error
        accounts = []
        for name in names:
            rec = records.get(name, {})
            windows = rec.setdefault('windows', {})
            # Live reads are only a supplement after an authenticated /usage baseline.
            for key, observation in live.get(name, {}).items():
                boundary = max(windows.get(key, {}).get('observed_at', 0),
                               rec.get('identity_changed_at', 0))
                if rec.get('loggedIn') and observation['observed_at'] > boundary:
                    windows[key] = observation
            # A delegated print session reports the same official reading with every response.
            for job in jobs:
                limits = job.get('limits')
                if job['account'] != name or not limits:
                    continue
                for key, reading in limits['windows'].items():
                    boundary = max(windows.get(key, {}).get('observed_at', 0),
                                   rec.get('identity_changed_at', 0))
                    if rec.get('loggedIn') and limits['at'] > boundary:
                        windows[key] = {**reading, 'observed_at': limits['at'],
                                        'source': 'delegated_session', 'reset_precision': 'second'}
            last_observed = max([w.get('observed_at', 0) for w in windows.values()] or [0])
            rec['last_observed_at'] = last_observed or None
            rec['live'] = active.get(name, 0) > 0
            rec['active_sessions'] = active.get(name, 0)
            # Delegated `claude -p` jobs run without --settings, so active_accounts() cannot see them.
            rec['delegated'] = sum(j['account'] == name and j['alive'] for j in jobs)
            with self.lock:
                auto = dict(self.official.get(name, {}))
            rec['auto'] = {key: auto.get(key) for key in ('ok_at', 'next_at', 'error')}
            for window in windows.values():
                reset = window.get('resets_at')
                window['expired'] = bool(finite(reset) and reset <= now)
                window['stale'] = now - window.get('observed_at', 0) > 300
                window['remaining_percentage'] = max(0, 100-window['used_percentage'])
            card = copy.deepcopy(cards.get(name, {}))
            if not card or card.get('email') != rec.get('email'):
                card = {'status': 'unknown', 'source': 'unavailable_in_cli'}
            if card.get('status') == 'available' and card.get('expires_at') and card['expires_at'] <= now:
                card['status'] = 'expired'
            if card.get('source') == 'manual':
                card['stale'] = now - card.get('verified_at', 0) > 86400
            official_card = self.reset_cards.view(name, rec.get('email'))
            if official_card:
                card = official_card
            core = [windows.get(key) for key in ('five_hour', 'seven_day')]
            quota = quotas.get(name, {})
            until, recorded = quota.get('blocked_until'), quota.get('recorded_at')
            # A newer complete positive reading can supersede an old limit error.
            cleared = finite(recorded) and all(w and not w['expired'] and
                w['used_percentage'] < 100 and w.get('observed_at', 0) > recorded for w in core)
            quota_active = (finite(until) and until > now and finite(recorded)
                            and recorded >= rec.get('identity_changed_at', 0) and not cleared)
            rec['quota_limit'] = {'active': quota_active, 'blocked_until': until,
                                  'recorded_at': recorded, 'source': 'official_client_limit_error'}
            resets = [w['resets_at'] for w in core if w and w['used_percentage'] >= 100
                      and finite(w.get('resets_at')) and not w['expired']]
            if quota_active:
                resets.append(until)
            rec['available_after'] = max(resets) if resets else None
            plan = rec.get('plan')
            if not isinstance(plan, dict):
                # Disabled accounts are never queried; the client's last profile read still has the plan.
                local = client_plan(profiles[name])
                if local and (not rec.get('email') or local['email'] == rec['email']):
                    plan = local
            rec['plan'] = plan_view(plan, now)
            if name in disabled:
                state = 'disabled'
                entry = disabled[name]
                rec['disabled_reason'] = entry.get('reason', '账号已停用') if isinstance(entry, dict) else str(entry)
            elif rec.get('loggedIn') is not True:
                state = 'logged_out' if 'loggedIn' in rec else 'pending'
            elif quota_active or any(w and not w['expired'] and w['used_percentage'] >= 100 for w in core):
                state = 'limited'
            elif all(w and not w['stale'] and not w['expired'] for w in core) and not rec.get('error'):
                state = 'available'
            else:
                state = 'recorded' if all(w for w in core) else 'unknown'
            accounts.append({'profile': name, **rec, 'state': state, 'reset_card': card,
                             'refreshing': name == busy})
        eligible = [a for a in accounts if a['state'] != 'disabled' and a.get('loggedIn')]
        waits = [a for a in eligible if a['state'] == 'limited' and a['available_after']]
        first = min(waits, key=lambda a: a['available_after']) if waits else None
        availability = {'available': sum(a['state'] == 'available' for a in eligible),
                        'limited': sum(a['state'] == 'limited' for a in eligible),
                        'unconfirmed': sum(a['state'] not in ('available', 'limited') for a in eligible),
                        'eligible': len(eligible),
                        'next_profile': first['profile'] if first else None,
                        'next_reset_at': first['available_after'] if first else None}
        return {'version': VERSION, 'server_time': now, 'started_at': self.started_at,
                'mode': 'initial_snapshot_then_statusline', 'browser_interval': 5, 'busy': busy,
                'initial_sync_complete': all(name in self.initialized or name in disabled for name in names) and not busy,
                'collector_error': error, 'availability': availability, 'accounts': accounts,
                'delegations': jobs, 'delegation_policy': delegation_policy(self.root, now),
                'auto_refresh': {'busy_seconds': OFFICIAL_BUSY_SECONDS, 'idle_seconds': OFFICIAL_IDLE_SECONDS,
                                 'running': bool(self.official_worker and self.official_worker.is_alive()),
                                 'error': self.official_error}}

    def set_card(self, payload):
        name = payload.get('profile')
        status = payload.get('status')
        with self.lock:
            if name not in self.registry['profiles']:
                raise ValueError('账号不存在')
            identity = self.records.get(name, {}).get('email')
            if not identity:
                raise ValueError('请先完成这个账号的登录')
            if status not in ('unknown', 'available', 'none', 'used'):
                raise ValueError('重置卡状态不正确')
            scope = payload.get('scope', 'unknown')
            if scope not in ('unknown', 'five_hour', 'seven_day', 'both'):
                raise ValueError('重置卡范围不正确')
            expires = payload.get('expires_at')
            if expires is not None and (not finite(expires) or not 0 < expires < 4102444800):
                raise ValueError('到期时间不正确')
            note = payload.get('note', '')
            if not isinstance(note, str) or len(note) > 300:
                raise ValueError('备注最多 300 字')
            card = {'status': status, 'scope': scope, 'expires_at': expires, 'note': note,
                    'verified_at': time.time(), 'source': 'manual', 'email': identity}
            self.cards[name] = card
            save_json(self.state_dir/'reset-cards.json', self.cards)
        return card


class HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = 'ClaudeQuotaMonitor'

    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def valid_host(self):
        return self.headers.get('Host', '') in self.server.allowed_hosts

    def send(self, code, body, content_type='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def do_GET(self):
        if not self.valid_host():
            return self.send(403, {'error': 'Host not allowed'})
        route = urlsplit(self.path).path
        if route == '/api/status':
            return self.send(200, self.server.monitor.snapshot())
        if route == '/api/health':
            return self.send(200, {'ok': True, 'version': VERSION})
        assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                  '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
                  '/style.css': ('style.css', 'text/css; charset=utf-8'),
                  '/favicon.svg': ('favicon.svg', 'image/svg+xml')}
        if route in assets:
            filename, mime = assets[route]
            return self.send(200, (HERE/'static'/filename).read_bytes(), mime)
        return self.send(404, {'error': 'Not found'})

    def do_POST(self):
        if not self.valid_host():
            return self.send(403, {'error': 'Host not allowed'})
        if self.headers.get('Origin') != 'http://' + self.headers['Host']:
            return self.send(403, {'error': '请从监控页面操作'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.send(415, {'error': 'JSON required'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                return self.send(413, {'error': 'Invalid body size'})
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError('请求格式不正确')
            route = urlsplit(self.path).path
            if route == '/api/refresh':
                accepted = self.server.monitor.refresh(payload.get('profile'))
                return self.send(202 if accepted else 429, {'accepted': accepted, 'error': None if accepted else '刚刚已经请求刷新，请稍等 10 秒'})
            if route == '/api/reset-card':
                return self.send(200, self.server.monitor.set_card(payload))
            if route == '/api/reset-cards/query':
                return self.send(200, self.server.monitor.reset_cards.query(payload.get('profile')))
            if route == '/api/reset-cards/prepare':
                return self.send(200, self.server.monitor.reset_cards.prepare(payload.get('profile'), payload.get('grant_id')))
            if route == '/api/reset-cards/use':
                return self.send(200, self.server.monitor.reset_cards.use(payload.get('confirmation_id'), payload.get('confirmed')))
            return self.send(404, {'error': 'Not found'})
        except CardError as exc:
            return self.send(exc.status, {'error': str(exc), 'code': exc.code})
        except (ValueError, TypeError) as exc:
            return self.send(400, {'error': str(exc)})
        except Exception:
            return self.send(503, {'error': '操作暂时无法完成，请查询最新状态后再核对；使用请求不会自动重试'})


def local_bind(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise argparse.ArgumentTypeError('Use an explicit loopback or private IPv4 address') from None
    private_lan = any(address in ipaddress.IPv4Network(network) for network in
                      ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    if not (address.is_loopback or private_lan):
        raise argparse.ArgumentTypeError('Use an explicit loopback or private IPv4 address')
    return str(address)


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bind', type=local_bind, default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8794)
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('CLAUDE_ACCOUNTS_HOME', str(Path.home()/'.claude-accounts'))))
    parser.add_argument('--adapter', type=Path, default=Path.home()/'.local/bin/claude-accounts')
    parser.add_argument('--cwd', type=Path, default=Path.cwd())
    parser.add_argument('--interval', type=int, default=120)
    parser.add_argument('--once', action='store_true')
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    os.umask(0o077)
    oslayer.utf8_stdio()
    adapter = load_account_module(args.adapter)
    adapter.ROOT = args.root.expanduser().resolve()
    args.root = adapter.ROOT
    monitor = Monitor(args.root, adapter, args.cwd, args.interval)
    # One collector service, independent of every Claude work session.
    lock_fd = os.open(monitor.state_dir/'collector.lock', os.O_CREAT | os.O_RDWR | oslayer.BINARY, 0o600)
    if not oslayer.lock(lock_fd, blocking=False):
        raise SystemExit('已有监控服务在运行')
    if args.once:
        for name in monitor.registry['profiles']:
            monitor.poll(monitor.registry, name)
        print(json.dumps(monitor.snapshot(), ensure_ascii=False))
        return
    server = HTTPServer((args.bind, args.port), Handler)
    server.monitor = monitor
    server.allowed_hosts = {f'{args.bind}:{server.server_port}'}
    if ipaddress.IPv4Address(args.bind).is_loopback:
        server.allowed_hosts.add(f'localhost:{server.server_port}')
    monitor.start()

    def shutdown(*_):
        monitor.stop.set()
        monitor.wake.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, shutdown)
    print(f'Claude quota monitor listening on http://{args.bind}:{server.server_port}', flush=True)
    try:
        server.serve_forever(poll_interval=.3)
    finally:
        monitor.stop.set()
        monitor.wake.set()
        monitor.worker.join(timeout=35)
        server.server_close()
        os.close(lock_fd)


if __name__ == '__main__':
    main()
