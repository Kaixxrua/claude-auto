# SPDX-License-Identifier: Apache-2.0
"""Explicit, authenticated reset-card queries and one-shot, confirmed redemption.

Credentials stay in memory. The journal is persisted BEFORE a redemption request;
ambiguous responses are never retried, even across a service restart.
"""
from __future__ import annotations

import copy
from datetime import datetime
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

HERE = Path(__file__).resolve().parent
# The OS layer: installed in share/claude-auto/lib, or lib/ in a source checkout.
sys.path.insert(0, str(next((d for d in (HERE.parent/'claude-auto/lib', HERE.parent/'lib')
                             if (d/'claude_auto_os.py').is_file()), HERE.parent/'lib')))
import claude_auto_os as oslayer  # noqa: E402

ORIGIN = 'https://api.anthropic.com'
GRANT_ID = re.compile(r'[a-z0-9_-]{1,40}\Z')
UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z')
TOKEN = re.compile(r'[a-z0-9_]{1,64}\Z')
WINDOWS = ('five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet', 'seven_day_overage_included')
RESULTS = {
    'reset': '官方已确认额度重置。',
    'already_used': '官方表示这张卡已使用；本次没有确认新的重置。',
    'not_limited': '官方表示当前未达到所需限额，没有重置。',
    'cooldown': '当前处于冷却期，没有重置。',
    'ineligible': '此账号暂不符合使用条件，没有重置。',
    'unavailable': '官方暂时无法提供重置，请重新查询卡片状态。',
}
REASONS = {
    'tenure': '账号暂不满足官方资格条件（tenure）', 'tier': '套餐不符合条件',
    'seat': '席位不符合条件', 'surface': '官方不接受当前客户端入口',
    'cli_version': '需要更新官方 Claude Code 客户端', 'no_grant': '没有符合条件的卡片',
    'paused': '卡片已暂停', 'expired': '卡片已过期', 'cooldown': '尚在冷却期',
    'not_limited': '尚未达到使用所需的限额', 'already_used': '卡片已使用',
    'config_off': '官方暂未开放', 'mobile': '此入口不符合条件',
    'not_next_grant': '需要先使用官方指定的另一张卡片',
    'stamp_indeterminate': '官方未能确认操作结果', 'reset_unconfirmed': '官方未确认重置完成',
}


class CardError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.status = code, status


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.timestamp() if dt.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def token(value):
    return value if isinstance(value, str) and TOKEN.fullmatch(value) else None


def reason_text(reason):
    return REASONS.get(reason, '官方暂未允许使用，具体资格以官方页面为准')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@lru_cache(maxsize=8)
def client_version(binary, modified):
    try:
        result = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=5)
        version = re.fullmatch(r'(\d+\.\d+\.\d+) \(Claude Code\)\s*', result.stdout)
        if result.returncode == 0 and version:
            return version.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    raise CardError('client_version', '无法读取官方客户端版本，请检查 Claude Code 安装', 503)


class OfficialClient:
    def __init__(self, registry, name):
        if name not in registry['profiles'] or name in registry.get('disabled', {}):
            raise CardError('disabled', '账号不存在或已停用，不会查询或使用卡片')
        try:
            auth = json.loads((Path(registry['profiles'][name])/'.credentials.json').read_bytes())['claudeAiOauth']
            token = auth['accessToken']
            if not isinstance(token, str) or not token or 'user:profile' not in auth.get('scopes', []):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise CardError('login_required', '没有可用的官方登录凭据，请先在终端登录此账号') from None
        binary = registry['claude']
        version = client_version(binary, Path(binary).stat().st_mtime_ns)
        self.headers = {'Authorization': 'Bearer '+token, 'anthropic-beta': 'oauth-2025-04-20',
                        'User-Agent': f'claude-cli/{version} (external, cli)',
                        'Accept': 'application/json', 'Content-Type': 'application/json'}
        self.plan = None

    def request(self, path, payload=None):
        # Callers supply only fixed paths or a validated official organization UUID.
        req = urllib.request.Request(ORIGIN+path, headers=self.headers,
            data=None if payload is None else json.dumps(payload).encode(),
            method='GET' if payload is None else 'POST')
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=10 if payload is None else 25) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError()
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError()
                return value
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            message = {401: '登录已失效，请在终端重新登录此账号后查询',
                       403: '官方拒绝访问，无法确认卡片状态',
                       429: '官方查询过于频繁，请稍后重试'}.get(code, '官方接口暂时不可用')
            raise CardError('http_'+str(code), message, 502) from None
        except (OSError, ValueError, urllib.error.URLError):
            raise CardError('network_or_format', '官方查询超时或返回格式异常，无法确认卡片状态', 502) from None

    def read(self):
        profile = self.request('/api/oauth/profile')
        account, org = profile.get('account'), profile.get('organization')
        if not isinstance(account, dict) or not isinstance(org, dict):
            raise CardError('identity', '官方未返回可核对的账号身份', 502)
        if not all(isinstance(v, str) and UUID.fullmatch(v) for v in (account.get('uuid'), org.get('uuid'))):
            raise CardError('identity', '官方未返回可核对的账号身份', 502)
        email = account.get('email')
        if not isinstance(email, str) or not 3 <= len(email) <= 320:
            raise CardError('identity', '官方未返回账号邮箱，无法安全使用卡片', 502)
        identity = {'account_id': account['uuid'], 'organization_id': org['uuid'], 'email': email}
        # Kept apart from the identity: a renewal must not look like a different account.
        self.plan = {'type': token(org.get('organization_type')), 'billing': token(org.get('billing_type')),
                     'status': token(org.get('subscription_status')),
                     'started_at': timestamp(org.get('subscription_created_at'))}
        return identity, self.request('/api/oauth/usage?cedar_ember=1&skip_spend=1')

    def redeem(self, identity, grant_id, request_id):
        if not UUID.fullmatch(identity['organization_id']) or not GRANT_ID.fullmatch(grant_id):
            raise CardError('invalid_id', '卡片或组织标识无效')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id):
            raise CardError('invalid_id', '请求标识无效')
        return self.request('/api/organizations/'+identity['organization_id']+'/reset_rate_limits',
                            {'program': 'cedar_ember', 'grant_id': grant_id, 'request_id': request_id})


def normalize(identity, payload, at):
    card = payload.get('cedar_ember')
    base = {'source': 'official_oauth_api', 'status': 'unknown', 'checked_at': at,
            'identity': identity, 'email': identity['email'], 'grants': [], 'resets_left': None,
            'error': None, 'stale': False}
    if not isinstance(card, dict) or not isinstance(card.get('eligible'), bool) or not isinstance(card.get('grants'), list):
        return {**base, 'error': '官方未返回完整重置卡信息；这不代表没有卡'}
    grants = []
    for raw in card['grants']:
        if not isinstance(raw, dict):
            return {**base, 'error': '官方卡片格式已变化，暂时无法确认'}
        gid, left, total = raw.get('id'), raw.get('resets_left'), raw.get('resets_total')
        clears, blocking = raw.get('clears'), raw.get('blocking')
        start, end = timestamp(raw.get('starts_at')), timestamp(raw.get('ends_at'))
        if (not isinstance(gid, str) or not GRANT_ID.fullmatch(gid) or
            not integer(left) or not integer(total) or left > total or
            not isinstance(clears, list) or not clears or not all(k in WINDOWS for k in clears) or
            not isinstance(blocking, list) or not all(isinstance(k, str) for k in blocking) or
            not all(isinstance(raw.get(k), bool) for k in ('paused', 'usable_now', 'use_requires_limit')) or
            start is None or end is None or end <= start):
            return {**base, 'error': '官方卡片格式已变化，暂时无法确认'}
        active = start <= at < end and not raw['paused']
        usable = (card['eligible'] and active and left > 0 and raw['usable_now'] and not blocking
                  and card.get('next_grant_id') == gid
                  and (not raw['use_requires_limit'] or card.get('at_limit') is True)
                  and (not card.get('cooldown_until') or
                       (timestamp(card['cooldown_until']) is not None and timestamp(card['cooldown_until']) <= at)))
        grants.append({'id': gid, 'label': str(raw.get('label') or gid)[:300],
                       'resets_left': left, 'resets_total': total, 'starts_at': start, 'ends_at': end,
                       'clears': clears, 'paused': raw['paused'], 'usable_now': bool(usable),
                       'use_requires_limit': raw['use_requires_limit'], 'blocking': blocking,
                       'active': active})
    if len({g['id'] for g in grants}) != len(grants):
        return {**base, 'error': '官方返回重复卡片，暂时无法确认'}
    left = sum(g['resets_left'] for g in grants if g['active'])
    reason = card.get('ineligible_reason')
    return {**base, 'status': 'available' if left else ('none' if card['eligible'] else 'ineligible'),
            'eligible': card['eligible'], 'reason': reason if reason in REASONS else None,
            'reason_text': None if card['eligible'] else reason_text(reason),
            'at_limit': card.get('at_limit') is True, 'grants': grants, 'resets_left': left,
            'usable_count': sum(g['resets_left'] for g in grants if g['usable_now']),
            'weekly_resets_at': timestamp(card.get('weekly_resets_at')),
            'cooldown_until': timestamp(card.get('cooldown_until'))}


class ResetCards:
    def __init__(self, monitor, client_factory=OfficialClient):
        self.monitor, self.client_factory = monitor, client_factory
        self.lock = threading.RLock()
        self.profile_locks = {}
        self.inventory_path = monitor.state_dir/'official-reset-cards.json'
        self.journal_path = monitor.state_dir/'reset-card-operations.json'
        self.journal_error = False
        self.inventory = self.load(self.inventory_path)
        self.operations = self.load(self.journal_path, journal=True)
        for operation in self.operations.values():
            if operation.get('phase') == 'submitting':
                operation['phase'] = 'unknown'

    def load(self, path, journal=False):
        try:
            value = json.loads(path.read_bytes())
            if not isinstance(value, dict) or not all(isinstance(v, dict) for v in value.values()):
                raise ValueError()
            return value
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            if journal:
                self.journal_error = True
            return {}

    def save(self, path, value):
        # Also sync the directory: a restart must not lose a submitted operation.
        tmp = path.with_name('.'+path.name+'-'+uuid.uuid4().hex)
        try:
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | oslayer.BINARY, 0o600),
                           'w', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            oslayer.replace(tmp, path)
            oslayer.sync_directory(path.parent)
        finally:
            tmp.unlink(missing_ok=True)

    def registry(self, name):
        registry = self.monitor.load_registry()
        if not isinstance(name, str) or name not in registry['profiles']:
            raise CardError('profile', '账号不存在', 400)
        if name in registry.get('disabled', {}):
            raise CardError('disabled', '此账号已停用，不会查询或使用重置卡')
        return registry

    def guard(self, name):
        self.registry(name)
        with self.lock:
            lock = self.profile_locks.setdefault(name, threading.Lock())
        if not lock.acquire(blocking=False):
            raise CardError('busy', '此账号正在查询或提交，请等候完成')
        return lock

    def pending(self, identity):
        return any(op.get('identity') == identity and op.get('phase') in ('submitting', 'unknown')
                   for op in self.operations.values())

    def view(self, name, email=None):
        with self.lock:
            card = copy.deepcopy(self.inventory.get(name))
            if not card:
                return None
            card['stale'] = time.time()-card.get('checked_at', 0) > 300 or bool(card.get('error'))
            card['pending'] = self.pending(card.get('identity'))
            history = [op for op in self.operations.values() if op.get('identity') == card.get('identity')
                       and op.get('phase') not in ('prepared', 'submitting') and op.get('outcome')]
            if history:
                card['last_result'] = self.outcome(max(history, key=lambda op: op.get('created_at', 0)))
            if email is not None and card.get('email') != email:
                card.update(status='unknown', stale=True, error='账号身份已变化，请重新查询重置卡')
            if self.journal_error:
                card.update(error='使用记录无法读取，暂时禁止使用；查询仍可进行', stale=True)
            for grant in card.get('grants', []):
                if not grant['starts_at'] <= time.time() < grant['ends_at']:
                    grant['active'] = grant['usable_now'] = False
                if card['stale'] or card['pending']:
                    grant['usable_now'] = False
            if card.get('resets_left') is not None:
                card['resets_left'] = sum(g['resets_left'] for g in card['grants'] if g['active'])
            if card.get('status') == 'available' and card['resets_left'] == 0:
                card['status'] = 'expired'
            card['usable_count'] = sum(g['resets_left'] for g in card['grants'] if g['usable_now'])
            card.pop('identity', None)
            return card

    def read(self, name):
        registry = self.registry(name)
        started_at = time.time()
        try:
            client = self.client_factory(registry, name)
            identity, payload = client.read()
            card = normalize(identity, payload, time.time())
            self.monitor.record_official_usage(name, identity, payload, card['checked_at'], started_at,
                                               getattr(client, 'plan', None))
            with self.lock:
                self.inventory[name] = card
                self.save(self.inventory_path, self.inventory)
                changed = False
                for op in self.operations.values():
                    if op.get('phase') != 'unknown' or op.get('identity') != card.get('identity') or card.get('error'):
                        continue
                    grant = next((g for g in card['grants'] if g['id'] == op['grant']['id']), None)
                    if grant and grant['resets_left'] < op['grant']['resets_left']:
                        op.update(phase='reconciled', outcome={'result': 'observed_change',
                            'message': '最新官方查询确认卡片剩余次数已减少；此前提交的直接结果仍未知。',
                            'completed_at': time.time()})
                        changed = True
                if changed:
                    self.save(self.journal_path, self.operations)
            return client, card
        except CardError as exc:
            with self.lock:
                old = self.inventory.get(name, {'status': 'unknown', 'source': 'official_oauth_api', 'grants': [], 'resets_left': None})
                self.inventory[name] = {**old, 'error': str(exc), 'error_code': exc.code, 'attempted_at': time.time()}
                self.save(self.inventory_path, self.inventory)
            raise

    def query(self, name):
        lock = self.guard(name)
        try:
            with self.lock:
                previous = self.inventory.get(name, {})
                last = max(previous.get('checked_at', 0), previous.get('attempted_at', 0))
                if time.time()-last < 5:
                    return self.view(name)
            self.read(name)
            return self.view(name)
        finally:
            lock.release()

    def candidate(self, card, gid):
        if self.journal_error or card.get('error') or self.pending(card.get('identity')):
            raise CardError('unconfirmed', '卡片状态或上次提交结果待确认，暂时不能使用')
        grant = next((g for g in card['grants'] if g['id'] == gid and g['usable_now']), None)
        if not grant:
            raise CardError('unusable', card.get('reason_text') or '此卡片当前不能使用，请重新查询')
        return grant

    def prepare(self, name, gid):
        lock = self.guard(name)
        try:
            _, card = self.read(name)
            with self.lock:
                grant = self.candidate(card, gid)
                token = uuid.uuid4().hex
                operation = {'profile': name, 'identity': card['identity'], 'grant': grant,
                             'request_id': uuid.uuid4().hex, 'expires_at': time.time()+180,
                             'phase': 'prepared', 'created_at': time.time()}
                # Expired confirmations cannot be used. Submitted entries are retained.
                self.operations = {k: v for k, v in self.operations.items()
                    if v.get('phase') != 'prepared' or v.get('expires_at', 0) > time.time()}
                self.operations[token] = operation
                self.save(self.journal_path, self.operations)
            return {'confirmation_id': token, 'profile': name, 'email': card['email'],
                    'grant': grant, 'expires_at': operation['expires_at'],
                    'weekly_resets_at': card['weekly_resets_at']}
        finally:
            lock.release()

    def outcome(self, operation):
        return copy.deepcopy(operation.get('outcome') or {
            'result': 'unknown', 'message': '提交结果尚未确认。请查询卡片状态；系统不会重复提交。'})

    def use(self, token, confirmed):
        if confirmed is not True or not isinstance(token, str):
            raise CardError('confirmation', '请先核对账号与卡片，并明确确认使用', 400)
        with self.lock:
            op = self.operations.get(token)
            if not op:
                raise CardError('confirmation', '确认信息无效，请重新查询', 400)
            if op['phase'] != 'prepared':
                return self.outcome(op)
            name = op['profile']
        lock = self.guard(name)
        try:
            if op['expires_at'] <= time.time():
                raise CardError('expired_confirmation', '确认已过期，请重新核对卡片')
            client, card = self.read(name)
            with self.lock:
                if card['identity'] != op['identity']:
                    raise CardError('identity_changed', '账号身份已变化，已取消此次使用')
                grant = self.candidate(card, op['grant']['id'])
                if any(grant[k] != op['grant'][k] for k in ('id', 'label', 'resets_left', 'clears', 'starts_at', 'ends_at')):
                    raise CardError('card_changed', '卡片信息已变化，请重新核对后使用')
                if any(other is not op and other.get('identity') == op['identity']
                       and other.get('grant', {}).get('id') == grant['id']
                       and other.get('outcome', {}).get('result') in ('reset', 'observed_change')
                       and other['outcome'].get('completed_at', 0) >= op['created_at']
                       for other in self.operations.values()):
                    raise CardError('card_changed', '此卡片在确认后已有其他使用记录，请重新核对')
                if op['expires_at'] <= time.time():
                    raise CardError('expired_confirmation', '确认已过期，请重新核对卡片')
                self.registry(name)  # Recheck disabled state just before submission.
                op['phase'] = 'submitting'
                self.save(self.journal_path, self.operations)
            try:
                response = client.redeem(op['identity'], grant['id'], op['request_id'])
                result = response.get('result')
                if (result not in RESULTS or response.get('reason') in ('stamp_indeterminate', 'reset_unconfirmed')
                    or (response.get('resets_left') is not None and not integer(response['resets_left']))):
                    raise CardError('unknown_result', '官方尚未确认结果')
                outcome = {'result': result, 'message': RESULTS[result], 'completed_at': time.time(),
                           'resets_left': response.get('resets_left'),
                           'cooldown_until': timestamp(response.get('cooldown_until'))}
                phase = 'complete'
            except Exception:
                # Includes timeout/HTTP errors: delivery may have succeeded remotely.
                outcome = {'result': 'unknown', 'message': '提交结果未确认，可能已消耗卡片。请查询卡片状态；不会自动重试或再次提交。'}
                phase = 'unknown'
            with self.lock:
                op.update(phase=phase, outcome=outcome)
                self.save(self.journal_path, self.operations)
            try:
                self.read(name)
            except CardError:
                outcome = {**outcome, 'refresh_error': '操作后的状态查询失败，请稍后查询'}
            return {**outcome, 'card': self.view(name)}
        finally:
            lock.release()
