"""Failure reporting for the student invitation paths: ids and counts only.

An exception raised around an invitation can quote its email (a database
error's DETAIL line, a template error), so neither its message nor its frames
are reported: the log line and the Sentry event carry the exception type and
the ids the caller passes. ``capture_message`` is used instead of
``capture_exception`` because the latter ships each frame's local variables,
which here hold addresses.
"""

from __future__ import annotations

import logging
from typing import Any

try:
    import sentry_sdk  # type: ignore[import-not-found]
# silent-ok: sentry-sdk is an optional Synapse extra; without it reports are log-only
except ImportError:
    sentry_sdk = None

logger = logging.getLogger("synapse.module.synapse_pangea_chat.student_invitations")


def report_failure(what: str, error: BaseException, **ids: Any) -> None:
    detail = " ".join(f"{key}={value}" for key, value in sorted(ids.items()))
    message = f"{what} failed ({type(error).__name__}) {detail}".rstrip()
    logger.error("%s", message)
    if sentry_sdk is not None:
        sentry_sdk.capture_message(message, level="error")
