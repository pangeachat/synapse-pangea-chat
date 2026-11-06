"""Explicit Sentry capture for module code.

Background tasks, module callbacks and ``respond_with_json`` handlers never
reach Synapse's request-level Sentry capture, so a failure they absorb is
invisible unless it is captured here (issue #197).
"""

from __future__ import annotations

try:
    import sentry_sdk  # type: ignore[import-not-found]
# silent-ok: sentry-sdk is an optional Synapse extra; without it captures are no-ops
except ImportError:  # pragma: no cover - sentry is optional at runtime
    sentry_sdk = None


def capture_exception(e: BaseException) -> None:
    if sentry_sdk is not None:
        sentry_sdk.capture_exception(e)
