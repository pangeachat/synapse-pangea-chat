"""Module-owned tables for the LTI tool core.

- `lti_platform`: one row per registered platform (issuer + client_id). A row
  starts `pending` and only a server admin moves it to `approved`; a pending
  platform's logins and launches are refused.
- `lti_deployment`: the deployment ids each platform may launch with.
- `lti_nonce`: the state and nonce issued at login initiation, consumed (deleted)
  by the first launch that presents the state, so both are single use.

The tool's own key is NOT stored here: it comes only from module config.
"""

from __future__ import annotations

import secrets
from typing import Any, List, Optional, Tuple

import attr

PENDING = "pending"
APPROVED = "approved"

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS lti_platform (
        platform_id TEXT PRIMARY KEY,
        issuer TEXT NOT NULL,
        client_id TEXT NOT NULL,
        auth_login_url TEXT NOT NULL,
        token_url TEXT NOT NULL,
        jwks_uri TEXT NOT NULL,
        product_family TEXT,
        state TEXT NOT NULL CHECK (state IN ('pending', 'approved')),
        registered_at_ms BIGINT NOT NULL,
        approved_at_ms BIGINT,
        approved_by TEXT,
        UNIQUE (issuer, client_id))""",
    """CREATE TABLE IF NOT EXISTS lti_deployment (
        platform_id TEXT NOT NULL,
        deployment_id TEXT NOT NULL,
        PRIMARY KEY (platform_id, deployment_id))""",
    """CREATE TABLE IF NOT EXISTS lti_nonce (
        state TEXT PRIMARY KEY,
        nonce TEXT NOT NULL UNIQUE,
        platform_id TEXT NOT NULL,
        expires_at_ms BIGINT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS lti_nonce_expires ON lti_nonce (expires_at_ms)",
)

_PLATFORM_COLUMNS = (
    "platform_id",
    "issuer",
    "client_id",
    "auth_login_url",
    "token_url",
    "jwks_uri",
    "state",
)
_SELECT_PLATFORM = "SELECT " + ", ".join(_PLATFORM_COLUMNS) + " FROM lti_platform "


@attr.s(frozen=True, auto_attribs=True)
class Platform:
    platform_id: str
    issuer: str
    client_id: str
    auth_login_url: str
    token_url: str
    jwks_uri: str
    state: str

    @property
    def approved(self) -> bool:
        return self.state == APPROVED


class PlatformExists(Exception):
    """A platform with this issuer and client_id is already registered."""


class LtiStore:
    def __init__(self, db_pool: Any):
        self._db = db_pool
        self._ready = False

    async def ensure(self) -> None:
        if self._ready:
            return

        def create(txn: Any) -> None:
            for sql in SCHEMA:
                txn.execute(sql)

        await self._db.runInteraction("lti_schema", create)
        self._ready = True

    # -- platforms ----------------------------------------------------------

    async def create_platform(
        self,
        *,
        issuer: str,
        client_id: str,
        auth_login_url: str,
        token_url: str,
        jwks_uri: str,
        product_family: Optional[str],
        deployment_id: Optional[str],
        now_ms: int,
    ) -> str:
        """Record a newly registered platform as pending; returns its id."""
        await self.ensure()
        platform_id = secrets.token_urlsafe(16)

        def insert(txn: Any) -> str:
            txn.execute(
                "SELECT 1 FROM lti_platform WHERE issuer = ? AND client_id = ?",
                (issuer, client_id),
            )
            if txn.fetchone() is not None:
                raise PlatformExists()
            txn.execute(
                """INSERT INTO lti_platform
                (platform_id, issuer, client_id, auth_login_url, token_url,
                 jwks_uri, product_family, state, registered_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    platform_id,
                    issuer,
                    client_id,
                    auth_login_url,
                    token_url,
                    jwks_uri,
                    product_family,
                    now_ms,
                ),
            )
            if deployment_id is not None:
                txn.execute(
                    "INSERT INTO lti_deployment (platform_id, deployment_id) VALUES (?, ?)",
                    (platform_id, deployment_id),
                )
            return platform_id

        return await self._db.runInteraction("lti_create_platform", insert)

    async def get_platform(self, platform_id: str) -> Optional[Platform]:
        await self.ensure()

        def select(txn: Any) -> Optional[Platform]:
            txn.execute(_SELECT_PLATFORM + "WHERE platform_id = ?", (platform_id,))
            row = txn.fetchone()
            return Platform(*row) if row else None

        return await self._db.runInteraction("lti_get_platform", select)

    async def platforms_for_issuer(
        self, issuer: str, client_id: Optional[str]
    ) -> List[Platform]:
        await self.ensure()

        def select(txn: Any) -> List[Platform]:
            if client_id is None:
                txn.execute(_SELECT_PLATFORM + "WHERE issuer = ?", (issuer,))
            else:
                txn.execute(
                    _SELECT_PLATFORM + "WHERE issuer = ? AND client_id = ?",
                    (issuer, client_id),
                )
            return [Platform(*row) for row in txn.fetchall()]

        return await self._db.runInteraction("lti_platforms_for_issuer", select)

    async def list_platforms(self) -> List[dict]:
        await self.ensure()

        def select(txn: Any) -> List[dict]:
            txn.execute(
                """SELECT platform_id, issuer, client_id, product_family, state,
                registered_at_ms, approved_at_ms, approved_by
                FROM lti_platform ORDER BY registered_at_ms, platform_id"""
            )
            rows = txn.fetchall()
            txn.execute(
                "SELECT platform_id, deployment_id FROM lti_deployment"
                " ORDER BY deployment_id"
            )
            deployments: dict = {}
            for platform_id, deployment_id in txn.fetchall():
                deployments.setdefault(platform_id, []).append(deployment_id)
            keys = (
                "platform_id",
                "issuer",
                "client_id",
                "product_family",
                "state",
                "registered_at_ms",
                "approved_at_ms",
                "approved_by",
            )
            result = []
            for row in rows:
                item = dict(zip(keys, row))
                item["deployment_ids"] = deployments.get(item["platform_id"], [])
                result.append(item)
            return result

        return await self._db.runInteraction("lti_list_platforms", select)

    async def approve(self, platform_id: str, *, operator: str, now_ms: int) -> bool:
        """Approve a platform. False if it is unknown. Idempotent: approving
        again keeps the first approval's time and operator."""
        await self.ensure()

        def update(txn: Any) -> bool:
            txn.execute(
                "SELECT state FROM lti_platform WHERE platform_id = ?",
                (platform_id,),
            )
            row = txn.fetchone()
            if row is None:
                return False
            if row[0] != APPROVED:
                txn.execute(
                    """UPDATE lti_platform SET state = 'approved',
                    approved_at_ms = ?, approved_by = ? WHERE platform_id = ?""",
                    (now_ms, operator, platform_id),
                )
            return True

        return await self._db.runInteraction("lti_approve_platform", update)

    async def deployments(self, platform_id: str) -> frozenset:
        await self.ensure()

        def select(txn: Any) -> frozenset:
            txn.execute(
                "SELECT deployment_id FROM lti_deployment WHERE platform_id = ?",
                (platform_id,),
            )
            return frozenset(row[0] for row in txn.fetchall())

        return await self._db.runInteraction("lti_deployments", select)

    # -- state and nonce ----------------------------------------------------

    async def issue_state(
        self, platform_id: str, *, now_ms: int, ttl_ms: int
    ) -> Tuple[str, str]:
        """A fresh (state, nonce) pair bound to the platform; expired pairs
        are swept in the same transaction so the table stays small."""
        await self.ensure()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)

        def insert(txn: Any) -> None:
            txn.execute("DELETE FROM lti_nonce WHERE expires_at_ms <= ?", (now_ms,))
            txn.execute(
                """INSERT INTO lti_nonce (state, nonce, platform_id, expires_at_ms)
                VALUES (?, ?, ?, ?)""",
                (state, nonce, platform_id, now_ms + ttl_ms),
            )

        await self._db.runInteraction("lti_issue_state", insert)
        return state, nonce

    async def consume_state(
        self, state: str, *, now_ms: int
    ) -> Optional[Tuple[str, str]]:
        """Delete the state and return (nonce, platform_id), or None when it
        is unknown, already used or expired. Of two concurrent launches with
        the same state, exactly one gets the row."""
        await self.ensure()

        def take(txn: Any) -> Optional[Tuple[str, str]]:
            txn.execute(
                "SELECT nonce, platform_id, expires_at_ms FROM lti_nonce WHERE state = ?",
                (state,),
            )
            row = txn.fetchone()
            if row is None:
                return None
            txn.execute("DELETE FROM lti_nonce WHERE state = ?", (state,))
            if txn.rowcount != 1:
                return None
            nonce, platform_id, expires_at_ms = row
            if expires_at_ms <= now_ms:
                return None
            return nonce, platform_id

        return await self._db.runInteraction("lti_consume_state", take)
