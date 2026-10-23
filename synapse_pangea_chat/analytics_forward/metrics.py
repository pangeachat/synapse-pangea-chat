"""Prometheus collectors for the analytics forwarder.

No label derives from a room, event or user id; every label set is closed.
"""

from __future__ import annotations

from typing import Any, Iterable

from prometheus_client import REGISTRY, Counter, Gauge


def _get_or_create(
    cls: Any, name: str, documentation: str, labelnames: Iterable[str] = ()
) -> Any:
    # Reused on a reload rather than registered twice (see moderation.metrics).
    existing = getattr(REGISTRY, "_names_to_collectors", {}).get(name)
    if existing is not None:
        return existing
    return cls(name, documentation, list(labelnames))


ACTIVE = _get_or_create(
    Gauge,
    "pangea_analytics_forward_active",
    "1 on the instance running the analytics forwarder, 0 elsewhere. "
    "Alert when the sum is 0 where the forwarder is configured.",
)
LAG = _get_or_create(
    Gauge,
    "pangea_analytics_forward_lag",
    "Stream positions between the persisted head and the delivered cursor.",
)
REJECTED = _get_or_create(
    Counter,
    "pangea_analytics_forward_rejected",
    "Events the ingest acknowledged but set aside as rejects.",
)
FAILURES = _get_or_create(
    Counter,
    "pangea_analytics_forward_failures",
    "Delivery attempts that did not move the cursor, by cause.",
    ["reason"],
)
FAILURE_REASONS = ("auth", "http", "bad_ack", "error")

CONSENT = _get_or_create(
    Counter,
    "pangea_analytics_forward_consent",
    "Consented analytics invites considered, by verdict.",
    ["verdict"],
)
