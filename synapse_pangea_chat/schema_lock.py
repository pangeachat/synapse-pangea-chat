"""One lock around the module's table creation, across Synapse's processes.

Every worker creates the module's tables on first use. ``IF NOT EXISTS`` does
not make two of them safe on PostgreSQL: concurrent ``CREATE TABLE`` /
``CREATE INDEX`` / ``ALTER TABLE`` take catalog and table locks in different
orders and can deadlock (seen on the production restart of 27 Nov 2026 for
``pangea_seat_invitation_schema``; Synapse retried it). Taking one advisory
lock first makes the schema transactions run one at a time; it is released
when the transaction ends. SQLite has one writer already, so nothing is done.
"""

from typing import Any

from synapse.storage.engines import PostgresEngine

# Distinct from the notice schedule (741882936), the transport claim
# (741882937) and the retired Can-Do map's own keys.
SCHEMA_LOCK_KEY = 741882940


def lock_schema(txn: Any) -> None:
    """Call first in a schema-creating transaction."""
    if isinstance(getattr(txn, "database_engine", None), PostgresEngine):
        txn.execute(f"SELECT pg_advisory_xact_lock({SCHEMA_LOCK_KEY})")
