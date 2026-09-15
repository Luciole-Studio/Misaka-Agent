"""Hermes pool policy over MISAKA's one atomic credential store.

Settings name native credential keys; the pool persists only selection/cooldown
metadata. OAuth grants and API keys remain owned by AuthStorage, including refresh.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time

import httpx

from misaka.ai.auth.types import AuthOperationOptions, OAuthCredential
from misaka.ai.utils.abort import race_with_abort_signal
from misaka.core.auth_storage import (
    AuthStorageCredentialStore,
    LockResult,
    _coerce_storage_object,
    _with_lock_async,
)
from misaka.utils.async_lifecycle import settle
from misaka.utils.values import signal_aborted

from ..native import credential_policy as policy
from . import config_bridge, execution

STATE_KEY = '_credential_pools'
_QUOTA_LOCK = threading.Lock()
_RETRY_SELECTION = object()
logger = logging.getLogger(__name__)


def runtime_key(auth):
    """Identity of the credential actually sent, including header-only OAuth."""
    raw = auth.model_dump() if hasattr(auth, 'model_dump') else auth
    return raw.get('apiKey') or (json.dumps(raw.get('headers'), sort_keys=True) if raw.get('headers') else '')


class _RefreshStore:
    """Cancel lock wait freely; once a grant rotates, persist it before cancelling.

    This is an LCM request adapter, not a change to Pi's generic credential-store
    cancellation contract. It uses the same native transaction and named record.
    """
    def __init__(self, store, key, signal):
        self.store, self.key, self.parent = store, key, signal
        self.started = False

    @property
    def aborted(self):
        return not self.started and signal_aborted(self.parent)

    async def read(self, _id, options=None):
        return await self.store.read(self.key, AuthOperationOptions(signal=self))

    async def modify(self, _id, fn, options=None):
        async def guarded(current):
            if signal_aborted(self.parent):
                raise asyncio.CancelledError('LCM OAuth refresh cancelled before exchange')
            self.started = True
            return await fn(current)
        result, cancelled = await settle(asyncio.create_task(self.store.modify(
            self.key, guarded, AuthOperationOptions(signal=self))))
        if cancelled is not None:
            raise cancelled
        if signal_aborted(self.parent):
            raise asyncio.CancelledError('LCM OAuth refresh persisted before cancellation')
        return result


def _codex_base_url():
    return 'https://chatgpt.com/backend-api/codex'


async def _quota_restored(registry, token, base_url, signal):
    """Original probe policy, with native cancellation and endpoint-scoped caching."""
    from ..native.codex_quota import (
        CODEX_QUOTA_PROBE_MIN_INTERVAL_SECONDS,
        _codex_usage_probe_url,
    )
    from ..native.nous_auth import _decode_jwt_claims
    claims = _decode_jwt_claims(token)
    if not claims:
        return None
    cache = vars(registry).setdefault('_lcm_quota_probes', {})
    url = _codex_usage_probe_url(base_url)
    key = (url, hashlib.sha256(token.encode()).hexdigest())
    now = time.monotonic()
    with _QUOTA_LOCK:
        cached = cache.get(key)
        if cached is not None and now - cached[0] < CODEX_QUOTA_PROBE_MIN_INTERVAL_SECONDS:
            return cached[1]
        cache[key] = (now, None)
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json', 'User-Agent': 'codex-cli'}
    account = claims.get('https://api.openai.com/auth', {})
    account = account.get('chatgpt_account_id') if isinstance(account, dict) else None
    if isinstance(account, str) and account.strip():
        headers['ChatGPT-Account-Id'] = account.strip()
    result = None
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await race_with_abort_signal(client.get(url, headers=headers), signal)
        if response.status_code == 200:
            limit = (response.json() or {}).get('rate_limit') or {}
            used = [(limit.get(window) or {}).get('used_percent') for window in ('primary_window', 'secondary_window')]
            used = [value for value in used if isinstance(value, (int, float))]
            result = max(used) < 100.0 if used else None
        elif response.status_code == 429:
            result = False
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        pass  # Probe failures are unknown, never proof that a cooldown expired.
    with _QUOTA_LOCK:
        cache[key] = (now, result)
    return result


def _nous_invoke_jwt_is_usable(*args, **kwargs):
    from ..native.nous_auth import _nous_invoke_jwt_is_usable as usable
    return usable(*args, **kwargs)


class _Selection:
    _is_sole_credential = policy._is_sole_credential
    _find = policy._find
    _current_unlocked = policy._current_unlocked
    current = policy.current
    entry_id_for_api_key = policy.entry_id_for_api_key
    _replace_entry = policy._replace_entry
    _adopt = policy._adopt
    _is_terminal_auth_failure = policy._is_terminal_auth_failure
    _mark_exhausted = policy._mark_exhausted
    _available_entries = policy._available_entries
    _log_no_available_entries = policy._log_no_available_entries
    _select_unlocked = policy._select_unlocked
    _identify_failed_entry = policy._identify_failed_entry
    _rotate_unmatched = policy._rotate_unmatched
    mark_exhausted_and_rotate = policy.mark_exhausted_and_rotate

    def __init__(self, provider, entries, strategy, restored=()):
        self.provider, self._entries, self._strategy = provider, sorted(entries, key=lambda e: e.priority), strategy
        self._lock = threading.RLock()
        self._current_id = self._last_no_entries_log_at = None
        self._unmatched_rotation_streak = 0
        self._restored = restored

    def _persist(self, **kwargs):
        # The enclosing native auth transaction commits the complete mutation.
        pass

    def _resync_stale_entry(self, entry):
        # Entries were hydrated from the latest native credential snapshot.
        return entry

    def _codex_quota_restored_upstream(self, entry):
        return entry.id in self._restored


class Pool:
    def __init__(self, registry, provider, refs, strategy):
        self.registry, self.provider, self.refs, self.strategy = registry, provider, refs, strategy
        self.storage = registry.authStorage
        self.credentials = AuthStorageCredentialStore(self.storage)
        self.current = None
        self._unmatched_rotation_streak = 0

    def _oauth(self):
        provider = self.registry.getProvider(self.provider)
        if provider is None or provider.auth.oauth is None:
            raise ValueError(f'No native OAuth resolver for {self.provider}')
        return provider.auth.oauth

    @classmethod
    def configured(cls, registry, provider):
        config = config_bridge.load_auxiliary_config()
        refs = config.get('credential_pools', {}).get(provider)
        if refs is None:
            return None
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref for ref in refs):
            raise ValueError('credential_pools entries must be nonempty lists of native credential keys')
        if len(set(refs)) != len(refs):
            raise ValueError('credential_pools contains duplicate credential keys')
        strategy = str(config.get('credential_pool_strategies', {}).get(provider, '')).strip().lower()
        if strategy not in policy.SUPPORTED_POOL_STRATEGIES:
            strategy = policy.STRATEGY_FILL_FIRST
        return cls(registry, provider, refs, strategy)

    async def select(self, *, failed_key=None, error=None, signal=None):
        # A competing owner may rotate a grant between the read and write lock.
        # Reread, never commit metadata computed from the old credential revision.
        for _ in range(3):
            execution.check_cancelled()
            chosen = await self._select(failed_key=failed_key, error=error, signal=signal)
            if chosen is not _RETRY_SELECTION:
                self.current = chosen.id if chosen is not None else None
                return chosen
            await asyncio.sleep(0)
        raise RuntimeError('Native credentials kept changing during pool selection')

    async def _select(self, *, failed_key, error, signal):
        options = AuthOperationOptions(signal=signal)
        # Resolve native config values before taking the write lock (a value may
        # invoke a configured key command). Recheck the snapshot under that lock.
        snapshot = _coerce_storage_object(await self.storage.readLatestData(options))
        credentials, runtime_keys, restored = {}, {}, set()
        for ref in self.refs:
            try:
                credential = await self.credentials.read(ref, options)
                if credential is None or getattr(credential, 'last_auth_error', None):
                    continue
                if isinstance(credential, OAuthCredential):
                    # Expired grants are refreshed under the native lock only after
                    # selection. Deriving their inference JWT now can fail too early.
                    runtime_keys[ref] = (credential.access if credential.expires <= time.time() * 1000 + 300_000
                                        else runtime_key(await self._oauth().toAuth(credential)))
                else:
                    runtime_keys[ref] = credential.key
            except Exception as candidate_error:
                execution.check_cancelled()
                if signal_aborted(signal):
                    raise asyncio.CancelledError('LCM pool selection cancelled') from candidate_error
                logger.warning('Auxiliary %s: skipping unusable native credential %s (%s)',
                               self.provider, ref, type(candidate_error).__name__)
                continue
            credentials[ref] = credential
            if self.provider == 'openai-codex':
                from ..native.codex_quota import _entry_is_rate_limit_exhausted
                saved = snapshot.get(STATE_KEY, {}).get(self.provider, {}).get(ref, {})
                secret = credential.access if isinstance(credential, OAuthCredential) else credential.key
                if (saved.get('digest') == hashlib.sha256(secret.encode()).hexdigest()
                        and _entry_is_rate_limit_exhausted(saved)):
                    models = [m for m in self.registry.getAll() if m.provider == self.provider]
                    endpoint = models[0].baseUrl if models else _codex_base_url()
                    if isinstance(credential, OAuthCredential) and credential.expires > time.time() * 1000 + 300_000:
                        resolved = await self._oauth().toAuth(credential)
                        endpoint = resolved.baseUrl or endpoint
                    if await _quota_restored(self.registry, runtime_keys[ref], endpoint, signal) is True:
                        restored.add(ref)

        async def mutate(raw):
            data = _coerce_storage_object(self.storage._parse_storage_data(raw))
            if any(data.get(ref) != snapshot.get(ref) for ref in self.refs):
                return LockResult(result=_RETRY_SELECTION)
            states = dict(data.get(STATE_KEY, {}))
            state = states.get(self.provider, {})
            entries = []
            for index, (ref, credential) in enumerate(credentials.items()):
                if credential is None:
                    continue
                secret = credential.access if isinstance(credential, OAuthCredential) else credential.key
                digest = hashlib.sha256(str(secret or '').encode()).hexdigest()
                saved = state.get(ref, {})
                if saved.get('digest') != digest:
                    saved = {}
                fields = {key: value for key, value in saved.items() if key != 'digest'}
                fields.update(id=ref, source='native', label=ref, auth_type=credential.type,
                              access_token=runtime_keys[ref] or '', priority=fields.get('priority', index))
                entries.append(policy.PooledCredential.from_dict(self.provider, fields))
            selected = _Selection(self.provider, entries, self.strategy, restored)
            selected._current_id = self.current
            selected._unmatched_rotation_streak = self._unmatched_rotation_streak
            # A removed/terminal issuing slot is already unavailable. Do not
            # let unmatched-key rotation reject the sole remaining healthy slot.
            retired_issuer = (self.current is not None and self.current not in credentials
                              and failed_key not in runtime_keys.values())
            if error is None or retired_issuer:
                entry, _ = selected._select_unlocked(refresh=False)
            else:
                entry = selected.mark_exhausted_and_rotate(
                    status_code=getattr(error, 'status_code', None), api_key_hint=failed_key,
                    error_context={'message': str(error)})
            # No raw credential is duplicated into the pool metadata.
            states[self.provider] = {item.id: {
                **{key: getattr(item, key) for key in (*policy._CLEAR_STATUS, 'priority', 'request_count')},
                'digest': hashlib.sha256(str(credentials[item.id].access if isinstance(credentials[item.id], OAuthCredential)
                                            else credentials[item.id].key).encode()).hexdigest(),
            } for item in selected._entries}
            data[STATE_KEY] = states
            self._unmatched_rotation_streak = selected._unmatched_rotation_streak
            return LockResult(result=entry, next=json.dumps(data, indent=2),
                              publish=lambda: self.storage._replace_cached_data(data))

        return await _with_lock_async(self.storage.storage, mutate, options)

    async def acquire(self, model, *, failed_key=None, error=None, signal=None):
        """Resolve a usable candidate; one bad initial refresh must not block its peers."""
        for _ in self.refs:
            entry = await self.select(failed_key=failed_key, error=error, signal=signal)
            if entry is None:
                break
            try:
                auth = await self.auth(model, entry, signal=signal)
                if not auth['ok']:
                    raise RuntimeError(auth['error'])
                return entry, auth
            except Exception as candidate_error:
                execution.check_cancelled()
                if signal_aborted(signal):
                    raise asyncio.CancelledError('LCM pool authentication cancelled') from candidate_error
                logger.debug('Auxiliary %s: native candidate authentication failed', self.provider, exc_info=True)
                failed_key, error = entry.runtime_api_key, candidate_error
        raise RuntimeError(f'No available native credentials for {self.provider}') from error

    async def auth(self, model, entry, *, signal=None, rejected_key=None):
        if entry is None:
            raise RuntimeError(f'No available native credentials for {self.provider}')
        options = AuthOperationOptions(signal=signal)
        credential = await self.credentials.read(entry.id, options)
        if credential is None:
            raise RuntimeError('Selected native credential was removed')
        if not isinstance(credential, OAuthCredential):
            from misaka.ai.auth.resolve import AuthResolutionOverrides
            return await self.registry.getApiKeyAndHeaders(model,
                AuthResolutionOverrides(apiKey=credential.key, env=credential.env, signal=signal))
        # Native OAuth resolver provides provider-specific headers/endpoints and
        # serializes token rotation under the SAME named AuthStorage record.
        from misaka.ai.auth.resolve import _resolveStoredOAuth
        oauth = self._oauth()
        mapped = _RefreshStore(self.credentials, entry.id, signal)
        if rejected_key is not None:
            async def refresh(current):
                if not isinstance(current, OAuthCredential):
                    return None
                if current.expires <= time.time() * 1000 + 300_000:
                    return None  # native expiry resolution below refreshes once, without deriving a stale JWT
                current_auth = await oauth.toAuth(current)
                return await oauth.refresh(current, mapped) if runtime_key(current_auth) == rejected_key else None
            credential = await mapped.modify(entry.id, refresh)
            if not isinstance(credential, OAuthCredential):
                raise RuntimeError('Selected OAuth credential was removed or replaced during refresh')
        if getattr(credential, 'last_auth_error', None):
            raise RuntimeError(f'Selected OAuth credential for {self.provider} requires sign-in')
        result = await _resolveStoredOAuth(mapped, self.provider, oauth, credential, mapped, None)
        if result is None:
            raise RuntimeError('Selected OAuth credential was removed during refresh')
        return {'ok': True, **result.auth.model_dump(), 'env': result.env}
