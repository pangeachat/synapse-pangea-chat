"""Explicit Sentry capture for module code.

Background tasks, module callbacks and ``respond_with_json`` handlers never
reach Synapse's request-level Sentry capture, so a failure they absorb is
invisible unless it is captured here (issue #197).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

try:
    import sentry_sdk  # type: ignore[import-not-found]
# silent-ok: sentry-sdk is an optional Synapse extra; without it captures are no-ops
except ImportError:  # pragma: no cover - sentry is optional at runtime
    sentry_sdk = None


def capture_exception(e: BaseException) -> None:
    if sentry_sdk is not None:
        sentry_sdk.capture_exception(e)


def capture_message(
    message: str,
    *,
    level: str,
    tags: Optional[Mapping[str, str]] = None,
    context: Optional[Mapping[str, Any]] = None,
) -> None:
    """A Sentry event for an unexpected state that raised nothing. The
    message is fixed text, so events group by it; ``tags`` and ``context``
    carry the ids and counts. Never pass personal data: no addresses, user
    ids or free text a person wrote."""
    if sentry_sdk is not None:
        sentry_sdk.capture_message(
            message,
            level=level,
            tags=dict(tags or {}),
            contexts={"pangea": dict(context or {})},
        )
