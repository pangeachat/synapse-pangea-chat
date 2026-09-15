"""Prometheus collectors for the moderation module.

Plain `prometheus_client`, which Synapse already depends on and exposes
through the same default registry its own metrics use. Synapse's `LaterGauge`
is deliberately not used: 1.159 wants `register_hook(...)` after construction
while 1.124 takes a constructor callback, which is a compatibility hazard
bought for nothing.

**Every collector goes through `_get_or_create`.** Relying on Python's module
cache is correct for a repeated `import` and wrong for `importlib.reload`,
which re-executes the registrations and raises `ValueError: Duplicated
timeseries in CollectorRegistry`. A module that cannot be reloaded cannot be
tested, and the failure surfaces as an unrelated import error somewhere else.

**Label cardinality is bounded by construction.** No label value derives from
a room id, an event id or a user id, and the two label sets that could grow -
drop causes and verdict categories - are closed sets checked at the call site.
An unbounded label is a memory leak in the scrape path, reached from data a
remote service controls.
"""

from typing import Any, Dict, Iterable, Optional

from prometheus_client import REGISTRY, Counter, Gauge, Histogram


def _get_or_create(
    collector_class: Any,
    name: str,
    documentation: str,
    labelnames: Iterable[str] = (),
    **kwargs: Any,
) -> Any:
    """Return the collector registered under ``name``, creating it if absent.

    `_names_to_collectors` is private and there is no public equivalent -
    `prometheus_client` offers no lookup-by-name at all, and the registry
    raises on a duplicate rather than returning what is there. The fallback
    below is what makes the reach-through safe: if a future version renames
    the attribute, this degrades to the plain constructor and the duplicate
    raises loudly at import, which is a failure a test sees.
    """
    registered = getattr(REGISTRY, "_names_to_collectors", {})
    existing = registered.get(name)
    if existing is not None:
        return existing
    return collector_class(name, documentation, list(labelnames), **kwargs)


# --- Tier 1 ---------------------------------------------------------------

TIER1_BLOCKS = _get_or_create(
    Counter,
    "pangea_moderation_tier1_blocks_total",
    "Events rejected pre-persist by the Tier 1 pre-filter.",
    ["reason"],
)

# --- Tier 2 queue and dispatch -------------------------------------------

TIER2_ENQUEUED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_enqueued_total",
    "Messages accepted onto the Tier 2 moderation queue.",
)

TIER2_DROPPED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_dropped_total",
    "Messages the Tier 2 path did not check, by cause.",
    ["cause"],
)

TIER2_CHECKS = _get_or_create(
    Counter,
    "pangea_moderation_tier2_checks_total",
    "Tier 2 moderation calls by outcome.",
    ["outcome"],
)

TIER2_REDACTIONS = _get_or_create(
    Counter,
    "pangea_moderation_tier2_redactions_total",
    "Messages redacted by Tier 2, by verdict category.",
    ["category"],
)

TIER2_REDACTION_FAILED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_redaction_failed_total",
    "Tier 2 redactions that could not be sent, by cause.",
    ["cause"],
)

TIER2_SUPPRESSED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_suppressed_total",
    "Flagged messages deliberately left standing, by verdict category.",
    ["category"],
)

TIER2_REDACTION_SKIPPED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_redaction_skipped_total",
    "Redactions not attempted because a pre-send re-read said not to.",
    ["cause"],
)

TIER2_WORKERS_RESTARTED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_workers_restarted_total",
    "Tier 2 worker loops the supervisor had to restart.",
)

TIER2_QUEUE_DEPTH = _get_or_create(
    Gauge,
    "pangea_moderation_tier2_queue_depth",
    "Messages waiting on the Tier 2 moderation queue.",
)

TIER2_INFLIGHT = _get_or_create(
    Gauge,
    "pangea_moderation_tier2_inflight",
    "Tier 2 moderation jobs a worker is currently running.",
)

TIER2_BREAKER_STATE = _get_or_create(
    Gauge,
    "pangea_moderation_tier2_breaker_state",
    "Tier 2 choreo circuit breaker: 0 closed, 1 half-open, 2 open.",
)

TIER2_SHUTDOWN_INFLIGHT = _get_or_create(
    Gauge,
    "pangea_moderation_tier2_shutdown_inflight",
    "Checks that were still running when a shutdown began. Emitted "
    "synchronously, so it survives a reactor that stops before the drain "
    "finishes.",
)

TIER2_ACTIVE = _get_or_create(
    Gauge,
    "pangea_moderation_tier2_active",
    "1 on the instance running Tier 2, 0 elsewhere.",
)

TIER2_LATENCY = _get_or_create(
    Histogram,
    "pangea_moderation_tier2_latency_seconds",
    "Wall time of one Tier 2 moderation call.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 30.0, float("inf")),
)

TIER2_QUEUE_WAIT = _get_or_create(
    Histogram,
    "pangea_moderation_tier2_queue_wait_seconds",
    "How long an accepted message waited before a worker picked it up.",
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, float("inf")),
)

BREAKER_STATE_VALUES: Dict[str, int] = {
    "closed": 0,
    "half_open": 1,
    "open": 2,
}

# Every reason a message can go unchecked. Closed on purpose: a drop is the
# one thing an operator must be able to see, and a typo that invented a new
# label would hide the drop in a series nobody alerts on.
DROP_CAUSES = frozenset(
    {
        # The queue was at capacity when the message arrived.
        "queue_full",
        # The circuit breaker was open, so no call was made.
        "breaker_open",
        # The breaker was half-open and its single probe was already out.
        "breaker_probe_busy",
        # This event id was already queued or running.
        "duplicate",
        # The module is shutting down and the message was still queued.
        "shutdown",
        # The module was shutting down when the message arrived.
        "stopping",
        # A job was still running when the drain deadline expired.
        "drain_timeout",
        # The queue could not be woken - the reactor clock was already down.
        "no_clock",
        # The job reached a worker and raised before producing a verdict.
        "handler_error",
        # The job was cancelled after a worker picked it up.
        "cancelled",
    }
)

CHECK_OUTCOMES = frozenset({"flagged", "clean", "unevaluated", "error"})

# Deliberately two values, not four. The four failures a self-redaction
# actually has - sender left, elevated redaction power level, remote sender,
# partial failure - are not distinguishable from the exception: Synapse raises
# `AuthError(403, ...)` with errcode `M_FORBIDDEN` for the first two alike, and
# telling them apart needs the room's power levels read back, which is the
# redaction-authorisation work this chunk does not do. Splitting the label on a
# substring of the exception MESSAGE would be both fragile across Synapse
# versions and a route for a Matrix ID to reach a metric, so the label says
# only what can be established: the room refused it, or something else did.
REDACTION_FAILURE_CAUSES = frozenset({"forbidden", "other"})

REDACTION_SKIP_CAUSES = frozenset(
    {"already_redacted", "event_missing", "lookup_failed"}
)


def record_drop(cause: str, count: int = 1) -> None:
    """Count a message the Tier 2 path did not check.

    Raises on an undeclared cause rather than creating the series. A silent
    drop is the failure this whole counter exists to make visible, and a drop
    filed under a label nobody knows about is a silent drop.
    """
    if cause not in DROP_CAUSES:
        raise ValueError(f"unknown moderation drop cause {cause!r}")
    TIER2_DROPPED.labels(cause=cause).inc(count)


def record_check(outcome: str) -> None:
    if outcome not in CHECK_OUTCOMES:
        raise ValueError(f"unknown moderation check outcome {outcome!r}")
    TIER2_CHECKS.labels(outcome=outcome).inc()


def record_redaction_failure(cause: str) -> None:
    if cause not in REDACTION_FAILURE_CAUSES:
        raise ValueError(f"unknown moderation redaction failure cause {cause!r}")
    TIER2_REDACTION_FAILED.labels(cause=cause).inc()


def set_breaker_state(state: str) -> None:
    value: Optional[int] = BREAKER_STATE_VALUES.get(state)
    if value is None:
        raise ValueError(f"unknown breaker state {state!r}")
    TIER2_BREAKER_STATE.set(value)


# The gauge has to carry a value before anything happens, or an operator
# cannot tell "closed" from "this module never loaded".
set_breaker_state("closed")
