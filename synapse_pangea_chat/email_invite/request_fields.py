"""Shared by the operator invitation endpoints: reading a bounded text field
from a request body, and capturing a failure the endpoint answers itself."""

from __future__ import annotations

from typing import Any

try:
    import sentry_sdk  # type: ignore[import-not-found]
# silent-ok: sentry-sdk is an optional Synapse extra; without it captures are no-ops (below)
except ImportError:
    sentry_sdk = None


def _capture_exception(e: Exception) -> None:
    # Failures here are answered, so they never reach Synapse's request-level
    # capture.
    if sentry_sdk is not None:
        sentry_sdk.capture_exception(e)


def _text_field(body: dict[str, Any], name: str, max_length: int) -> str | None:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        return None
    return value.strip()
