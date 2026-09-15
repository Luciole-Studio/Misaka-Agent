"""Run Hermes' synchronous-call recovery policy over native async requests.

LCM calls the sync Hermes entry point: its configurable retry budget, not the
different one-retry async entry point, applies here. Only awaiting I/O and owner
cancellation and native credential/provider resolution belong to this adapter.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx

from misaka.ai.utils.abort import sleep
from misaka.ai.utils.error_body import provider_error_headers, provider_error_status

from ..native import auxiliary_recovery as policy
from . import execution

logger = logging.getLogger(__name__)


class _HttpError(RuntimeError):
    def __init__(self, cause, status):
        super().__init__(str(cause))
        self.status_code = status
        self.headers = provider_error_headers(cause)


def provider_error(cause):
    """Normalize native SDK shapes to Hermes' predicates, retaining the cause.

    Hermes' OpenAI-compatible clients already emit status_code and connection /
    timeout classes. Native httpx / botocore don't all use that spelling.
    """
    if isinstance(cause, httpx.TimeoutException):
        return TimeoutError(str(cause))
    if isinstance(cause, httpx.TransportError):
        return ConnectionError(str(cause))
    status = provider_error_status(cause)
    if status is not None and getattr(cause, 'status_code', None) != status:
        return _HttpError(cause, status)
    return cause


async def _backoff(seconds):
    try:
        await sleep(seconds * 1000, execution.current_signal())
    finally:
        # Native sleep reports a signal as RuntimeError; LCM cancellation must
        # bypass Exception handlers, including deterministic-summary fallback.
        execution.check_cancelled()


async def _transient(primary, task):
    try:
        return await primary()
    except Exception as error:
        if not policy._should_retry_same_provider(task, error, ''):
            raise
        retries = policy._transient_retry_count()
        last_error = error
        for attempt in range(1, retries + 1):
            delay = min(policy._TRANSIENT_RETRY_BACKOFF_BASE * (2.0 ** (attempt - 1)), 8.0)
            logger.info('Auxiliary %s: transient transport error (attempt %d/%d); '
                        'retrying same provider after %.1fs: %s',
                        task or 'call', attempt, retries, delay, last_error)
            await _backoff(delay)
            try:
                return await primary()
            except Exception as retry_error:
                if not policy._is_transient_transport_error(retry_error):
                    raise
                last_error = retry_error
        raise last_error


async def recover(primary, *, task, base_url, temperature, max_tokens, extra_body=None,
                  refresh=None, rotate=None, fallback=None, retries=True, heal_model=None, paid_access=None):
    params = {key: value for key, value in {'temperature': temperature, 'max_tokens': max_tokens}.items()
              if value is not None}

    if extra_body:
        params["extra_body"] = dict(extra_body)

    async def call(kwargs):
        execution.check_cancelled()
        return await primary(**{key: kwargs.get(key) for key in ('temperature', 'max_tokens')},
                             **({'extra_body': kwargs['extra_body']} if 'extra_body' in kwargs else {}))

    async def credit_aware():
        try:
            return await call(params)
        except Exception as error:
            affordable = policy._affordable_max_tokens_from_error(error)
            if affordable is None or (max_tokens is not None and 0 < max_tokens <= affordable):
                raise
            logger.info('Auxiliary %s: credit-limited request; retrying once with output cap %d',
                        task or 'call', affordable)
            return await call({**params, 'max_tokens': affordable})

    try:
        return await _transient(credit_aware, task) if retries else await call(params)
    except Exception as error:  # noqa: BLE001 - final ladder error is rethrown
        # The unchanged parameter ladder only reads client.base_url (ZAI's
        # unlabelled 1210 error), task, and tag. No SDK client or second auth owner.
        route = SimpleNamespace(client=SimpleNamespace(base_url=base_url), task=task, tag='')

        async def perform(step):
            # Parameter retries are ONE raw request, not another transient or
            # affordable-token retry tree (same ordering as Hermes' sync driver).
            return await call(step.args[1])

        response, remaining_error, recovered_params = await policy._drive_ladder_async(
            policy._ladder_parameter_rungs(error, route, params, max_tokens), perform)
        if remaining_error is None:
            return response
        if heal_model is not None:
            from ..native.auxiliary_nous import _is_model_not_found_error
            if _is_model_not_found_error(remaining_error) and await heal_model():
                try:
                    return await call(recovered_params)
                except Exception as healed_error:  # noqa: BLE001 - original Nous rung accepts any retry error
                    remaining_error = healed_error
        if (paid_access is not None and refresh is not None and policy._is_payment_error(remaining_error)
                and await paid_access() and await refresh()):
            try:
                return await call(recovered_params)
            except Exception as paid_error:
                if not (policy._is_auth_error(paid_error) or policy._is_payment_error(paid_error)
                        or policy._is_rate_limit_error(paid_error) or policy._is_connection_error(paid_error)):
                    raise
                remaining_error = paid_error
        if policy._is_auth_error(remaining_error) and refresh is not None and await refresh():
            return await call(recovered_params)
        accepts = lambda err: policy._is_auth_error(err) or policy._is_payment_error(err) or policy._is_rate_limit_error(err)
        if rotate is not None and accepts(remaining_error):
            if policy._is_rate_limit_error(remaining_error) and not policy._is_payment_error(remaining_error):
                try:
                    return await call(recovered_params)
                except Exception as retry_error:
                    if not accepts(retry_error):
                        raise
                    remaining_error = retry_error
            if await rotate(remaining_error):
                try:
                    return await call(recovered_params)
                except Exception as rotated_error:
                    if not accepts(rotated_error):
                        raise
                    await rotate(rotated_error)
                    remaining_error = rotated_error
        if fallback is not None:
            response = await fallback(remaining_error)
            if response is not None:
                return response
        raise remaining_error
