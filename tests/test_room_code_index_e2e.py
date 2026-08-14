"""End-to-end coverage for the room access-code index (issue #163).

The unit tests prove the statements are right on SQLite; this proves they are
right on PostgreSQL, inside a real homeserver, with the ``on_new_event`` hook
and the startup backfill doing the writing.

One Synapse instance covers the whole lifecycle, because starting one is by far
the most expensive thing here and the backfill's start delay only has to be
waited out once.
"""

import asyncio
import logging
from typing import Any, List, Optional, Tuple

import psycopg2
import requests
from psycopg2.extensions import parse_dsn

from synapse_pangea_chat.room_code.access_code_index import (
    INDEX_TABLE,
    LEASE_KEY,
    LEASE_TABLE,
)

from .base_e2e import BaseSynapseE2ETest

logger = logging.getLogger(__name__)

# The backfill waits START_DELAY_SECONDS (30) before its first attempt. A lone
# instance claims the lease on that attempt, so this only has to cover the
# delay plus a scan of a near-empty database.
INDEX_READY_TIMEOUT_SECONDS = 90


class TestRoomCodeIndexE2E(BaseSynapseE2ETest):
    # -- direct database access --------------------------------------------

    def _sql(
        self, postgres: Any, sql: str, params: Tuple[Any, ...] = ()
    ) -> List[Tuple[Any, ...]]:
        dsn_params = parse_dsn(postgres.url())
        dsn_params["dbname"] = "testdb"
        conn = psycopg2.connect(psycopg2.extensions.make_dsn(**dsn_params))
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description is None:
                    return []
                return cur.fetchall()
        finally:
            conn.close()

    async def _wait_for_index_ready(self, postgres: Any) -> None:
        """Block until the backfill has marked itself complete."""
        waited = 0
        while waited < INDEX_READY_TIMEOUT_SECONDS:
            try:
                rows = self._sql(
                    postgres,
                    f"SELECT completed_at_ms FROM {LEASE_TABLE} WHERE lease_key = %s",
                    (LEASE_KEY,),
                )
                if rows and rows[0][0] is not None:
                    logger.info("room code index reported ready after %ss", waited)
                    return
            except psycopg2.errors.UndefinedTable:
                pass  # the module has not created its schema yet
            await asyncio.sleep(2)
            waited += 2
        self.fail(
            f"room code index backfill did not complete within "
            f"{INDEX_READY_TIMEOUT_SECONDS}s"
        )

    def _indexed_codes(self, postgres: Any, room_id: str) -> List[Tuple[Any, ...]]:
        return self._sql(
            postgres,
            f"SELECT access_code_lower, admin_access_code_lower "
            f"FROM {INDEX_TABLE} WHERE room_id = %s",
            (room_id,),
        )

    # -- HTTP helpers -------------------------------------------------------

    def _set_join_rules(self, room_id: str, access_token: str, content: dict) -> None:
        response = requests.put(
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}"
            f"/state/m.room.join_rules",
            json=content,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _knock_with_code(self, code: str, access_token: str) -> requests.Response:
        return requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/knock_with_code",
            json={"access_code": code},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def _wait_for_indexed_code(
        self,
        postgres: Any,
        room_id: str,
        expected_access_code: Optional[str],
    ) -> None:
        """Wait for the hook to catch up with a join-rules write."""
        for _ in range(10):
            rows = self._indexed_codes(postgres, room_id)
            if expected_access_code is None:
                if not rows or rows[0][0] is None:
                    return
            elif rows and rows[0][0] == expected_access_code:
                return
            await asyncio.sleep(1)
        self.fail(
            f"index did not converge on access_code={expected_access_code!r} "
            f"for {room_id}; rows={self._indexed_codes(postgres, room_id)}"
        )

    async def _wait_for_membership(
        self, room_id: str, user_id: str, access_token: str, membership: str
    ) -> bool:
        url = (
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}"
            f"/state/m.room.member/{user_id}"
        )
        for _ in range(5):
            response = requests.get(
                url, headers={"Authorization": f"Bearer {access_token}"}
            )
            if (
                response.status_code == 200
                and response.json().get("membership") == membership
            ):
                return True
            await asyncio.sleep(1)
        return False

    # -- the test -----------------------------------------------------------

    async def test_e2e_room_code_index_lifecycle(self) -> None:
        """Schema, backfill completion, hook maintenance, and indexed reads.

        1. The backfill creates its schema and marks itself complete.
        2. A room whose code is set while the module is running is indexed.
        3. Knocking with that code still invites the user.
        4. The index really is the read path — remove a row and the code stops
           resolving, restore it and the code works again.
        5. Rotating a code retires the old one.
        """
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse()

            for user in ("test1", "test2"):
                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user=user,
                    password="123123123",
                    admin=True,
                )
            _, owner_token = await self.login_user(user="test1", password="123123123")
            joiner_id, joiner_token = await self.login_user(
                user="test2", password="123123123"
            )

            room_id = await self.create_private_room_knock_allowed_room(owner_token)
            self._set_join_rules(
                room_id,
                owner_token,
                {"join_rule": "knock", "access_code": "hookd22"},
            )

            # (1) The backfill runs and opens the index to readers.
            await self._wait_for_index_ready(postgres)

            # (2) The room's code is in the index.
            await self._wait_for_indexed_code(postgres, room_id, "hookd22")

            # (3) The indexed read path resolves the code to an invite, and is
            # still case-insensitive.
            response = self._knock_with_code("HooKd22", joiner_token)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["rooms"], [room_id])
            self.assertTrue(
                await self._wait_for_membership(
                    room_id, joiner_id, owner_token, "invite"
                ),
                "the code holder should have been invited",
            )

            # (4) Prove the lookup is actually going through the index: with the
            # row removed the code stops resolving, which it would not do if the
            # request were still scanning join rules.
            self._sql(
                postgres,
                f"DELETE FROM {INDEX_TABLE} WHERE room_id = %s",
                (room_id,),
            )
            self.assertEqual(
                self._knock_with_code("hookd22", joiner_token).status_code,
                400,
                "with no index row the code must not resolve — otherwise the "
                "request fell back to the full scan",
            )

            # Rewriting the join rules puts it back, via the hook.
            self._set_join_rules(
                room_id,
                owner_token,
                {"join_rule": "knock", "access_code": "hookd22"},
            )
            await self._wait_for_indexed_code(postgres, room_id, "hookd22")
            self.assertEqual(
                self._knock_with_code("hookd22", joiner_token).status_code, 200
            )

            # (5) Rotating the code retires the old one.
            self._set_join_rules(
                room_id,
                owner_token,
                {"join_rule": "knock", "access_code": "rotatd3"},
            )
            await self._wait_for_indexed_code(postgres, room_id, "rotatd3")

            self.assertEqual(
                self._knock_with_code("hookd22", joiner_token).status_code,
                400,
                "the retired code should no longer resolve to a room",
            )
            self.assertEqual(
                self._knock_with_code("rotatd3", joiner_token).status_code, 200
            )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
