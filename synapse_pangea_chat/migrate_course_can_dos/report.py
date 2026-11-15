"""Reporting for the Can-Do migration endpoints: ids, counts and error types
only.

A course space's Can-Do list holds teachers' notes and a progress record is a
learner's own figures, so neither an exception's message nor its frames are
reported: ``capture_exception`` would ship each frame's local variables, which
here hold that content. The log line and the Sentry event carry a fixed message,
so one kind of event groups as one issue, and the ids and counts the caller
passes.
"""

from __future__ import annotations

import logging
from typing import Any

try:
    import sentry_sdk  # type: ignore[import-not-found]
# silent-ok: sentry-sdk is an optional Synapse extra; without it reports are log-only
except ImportError:
    sentry_sdk = None

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.migrate_course_can_dos.report"
)


def _detail(fields: Any) -> str:
    return " ".join(f"{key}={value}" for key, value in sorted(fields.items()))


def report(message: str, level: str = "warning", /, **fields: Any) -> None:
    """A path that skipped, defaulted or left something unmoved; ``level`` is
    "warning" or "error"."""
    logger.log(
        logging.ERROR if level == "error" else logging.WARNING,
        "%s %s",
        message,
        _detail(fields),
    )
    if sentry_sdk is not None:
        sentry_sdk.capture_message(message, level=level, extras=fields)


def report_failure(message: str, error: BaseException, **fields: Any) -> None:
    """A failure, as its type: never its message or frames."""
    report(message, "error", error_type=type(error).__name__, **fields)
