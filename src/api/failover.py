"""Retry stateless API requests on another logged-in account after failure."""

from __future__ import annotations

from functools import wraps

from fastapi import UploadFile

from src.api.worker_pool import (
    BrowserBusy,
    is_retryable_error,
    last_failed_account,
    reset_last_failed_account,
    reset_request_excluded_accounts,
    restore_last_failed_account,
    set_request_excluded_accounts,
)
from src.core.log import setup_logging

log = setup_logging("request_failover")


async def _rewind_uploads(value) -> None:
    """Rewind multipart files before replaying an endpoint on another account."""
    if isinstance(value, UploadFile):
        await value.seek(0)
    elif isinstance(value, dict):
        for item in value.values():
            await _rewind_uploads(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            await _rewind_uploads(item)


def retry_across_accounts(can_retry=None):
    """Replay an endpoint once per eligible account after a retryable failure.

    `_Acquired` records the account that failed and immediately removes it from
    pool scheduling. The next invocation receives that account as a request-local
    exclusion, so it can only run on the next logged-in account.
    """
    def decorate(endpoint):
        @wraps(endpoint)
        async def wrapped(*args, **kwargs):
            if can_retry is not None and not can_retry(*args, **kwargs):
                return await endpoint(*args, **kwargs)

            excluded: set[str] = set()
            last_error = None
            while True:
                excluded_token = set_request_excluded_accounts(excluded)
                failed_token = reset_last_failed_account()
                try:
                    return await endpoint(*args, **kwargs)
                except BrowserBusy:
                    if last_error is not None:
                        raise last_error
                    raise
                except BaseException as error:
                    failed_account = last_failed_account()
                    if (
                        not is_retryable_error(error)
                        or not failed_account
                        or failed_account in excluded
                    ):
                        raise
                    excluded.add(failed_account)
                    last_error = error
                    log.warning(
                        f"[{failed_account}] request failed; retrying on another "
                        f"eligible account ({len(excluded)} excluded)"
                    )
                    await _rewind_uploads(args)
                    await _rewind_uploads(kwargs)
                finally:
                    restore_last_failed_account(failed_token)
                    reset_request_excluded_accounts(excluded_token)

        return wrapped
    return decorate
