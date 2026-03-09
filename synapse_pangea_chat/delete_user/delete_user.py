from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
    SynapseError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from synapse.types import create_requester
from synapse.util.duration import Duration
from twisted.internet import defer
from twisted.web.resource import Resource

from synapse_pangea_chat.delete_room.extract_body_json import extract_body_json
from synapse_pangea_chat.delete_user.is_rate_limited import is_rate_limited

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.delete_user")

SCHEDULE_TABLE = "pangea_delete_user_schedule"
VALID_ACTIONS = {"schedule", "cancel", "force"}


class DeleteUser(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._clock = self._api._hs.get_clock()
        self._datastores = self._api._hs.get_datastores()
        self._deactivate_account_handler = (
            self._api._hs.get_deactivate_account_handler()
        )
        self._schedule_table_ready = False

        self._clock.looping_call(
            self._api._hs.run_as_background_process,
            Duration(seconds=self._config.delete_user_processor_interval_seconds),
            desc="pangea_delete_user_process_schedules",
            func=self._process_scheduled_deletes,
        )

    def render_POST(self, request: SynapseRequest):
        defer.ensureDeferred(self._async_render_POST(request))
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest):
        try:
            requester = await self._auth.get_user_by_req(request)
            requester_id = requester.user.to_string()

            if is_rate_limited(requester_id, self._config):
                respond_with_json(
                    request,
                    429,
                    {"error": "Rate limited"},
                    send_cors=True,
                )
                return

            body = await extract_body_json(request)
            if body is None:
                body = {}
            if not isinstance(body, dict):
                respond_with_json(
                    request,
                    400,
                    {"error": "Invalid JSON in request body"},
                    send_cors=True,
                )
                return

            action = body.get("action", "schedule")
            if not isinstance(action, str) or action not in VALID_ACTIONS:
                respond_with_json(
                    request,
                    400,
                    {
                        "error": "Invalid action. Must be one of: schedule, cancel, force"
                    },
                    send_cors=True,
                )
                return

            target_user_id = body.get("user_id", requester_id)
            if not isinstance(target_user_id, str) or not target_user_id:
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing or invalid user_id"},
                    send_cors=True,
                )
                return

            if not self._api._hs.is_mine_id(target_user_id):
                respond_with_json(
                    request,
                    400,
                    {"error": "Can only delete local users"},
                    send_cors=True,
                )
                return

            is_admin = await self._api.is_user_admin(requester_id)
            if target_user_id != requester_id and not is_admin:
                respond_with_json(
                    request,
                    403,
                    {"error": "Forbidden: server admin required"},
                    send_cors=True,
                )
                return

            await self._ensure_schedule_table()

            if action == "schedule":
                now_ms = self._clock.time_msec()
                execute_at_ms = (
                    now_ms + self._config.delete_user_schedule_delay_seconds * 1000
                )
                await self._upsert_schedule(
                    user_id=target_user_id,
                    execute_at_ms=execute_at_ms,
                    requested_by=requester_id,
                    requested_by_admin=target_user_id != requester_id,
                )
                respond_with_json(
                    request,
                    200,
                    {
                        "message": "Delete scheduled",
                        "action": "schedule",
                        "user_id": target_user_id,
                        "execute_at_ms": execute_at_ms,
                    },
                    send_cors=True,
                )
                return

            if action == "cancel":
                canceled = await self._delete_schedule(target_user_id)
                respond_with_json(
                    request,
                    200,
                    {
                        "message": (
                            "Delete schedule canceled"
                            if canceled
                            else "No delete schedule found"
                        ),
                        "action": "cancel",
                        "user_id": target_user_id,
                        "canceled": canceled,
                    },
                    send_cors=True,
                )
                return

            schedule = await self._get_schedule(target_user_id)
            if schedule is None:
                respond_with_json(
                    request,
                    400,
                    {"error": "No delete schedule found for user"},
                    send_cors=True,
                )
                return

            delete_result = await self._delete_user_now(
                user_id=target_user_id,
                by_admin=target_user_id != requester_id,
            )
            await self._delete_schedule(target_user_id)

            respond_with_json(
                request,
                200,
                {
                    "message": "Deleted",
                    "action": "force",
                    "user_id": target_user_id,
                    "deleted_external_ids": delete_result["deleted_external_ids"],
                    "deleted_threepids": delete_result["deleted_threepids"],
                },
                send_cors=True,
            )
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ) as e:
            logger.error("Forbidden: %s", e)
            respond_with_json(
                request,
                403,
                {"error": "Forbidden"},
                send_cors=True,
            )
        except SynapseError as e:
            logger.error("Synapse error while deleting user: %s", e)
            respond_with_json(
                request,
                e.code,
                {"error": e.msg},
                send_cors=True,
            )
        except Exception as e:
            logger.error("Unexpected error: %s", e)
            respond_with_json(
                request,
                500,
                {"error": "Internal server error"},
                send_cors=True,
            )

    async def _delete_user_now(self, user_id: str, by_admin: bool) -> Dict[str, int]:
        external_ids = await self._datastores.main.get_external_ids_by_user(user_id)
        for auth_provider, external_id in external_ids:
            await self._datastores.main.remove_user_external_id(
                auth_provider,
                external_id,
                user_id,
            )

        threepids = await self._datastores.main.user_get_threepids(user_id)
        for threepid in threepids:
            await self._datastores.main.user_delete_threepid(
                user_id,
                threepid.medium,
                threepid.address,
            )

        await self._deactivate_account_handler.deactivate_account(
            user_id=user_id,
            erase_data=True,
            requester=create_requester(user_id),
            by_admin=by_admin,
        )

        return {
            "deleted_external_ids": len(external_ids),
            "deleted_threepids": len(threepids),
        }

    async def _ensure_schedule_table(self) -> None:
        if self._schedule_table_ready:
            return

        def _create_table(txn: Any) -> None:
            txn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SCHEDULE_TABLE} (
                    user_id TEXT PRIMARY KEY,
                    execute_at_ms BIGINT NOT NULL,
                    requested_at_ms BIGINT NOT NULL,
                    requested_by TEXT NOT NULL,
                    requested_by_admin BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
            txn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {SCHEDULE_TABLE}_execute_at_idx
                ON {SCHEDULE_TABLE}(execute_at_ms)
                """
            )

        await self._datastores.main.db_pool.runInteraction(
            "pangea_delete_user_create_schedule_table",
            _create_table,
        )
        self._schedule_table_ready = True

    async def _upsert_schedule(
        self,
        user_id: str,
        execute_at_ms: int,
        requested_by: str,
        requested_by_admin: bool,
    ) -> None:
        def _upsert(txn: Any) -> None:
            txn.execute(
                f"""
                INSERT INTO {SCHEDULE_TABLE}
                    (user_id, execute_at_ms, requested_at_ms, requested_by, requested_by_admin)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET
                    execute_at_ms = EXCLUDED.execute_at_ms,
                    requested_at_ms = EXCLUDED.requested_at_ms,
                    requested_by = EXCLUDED.requested_by,
                    requested_by_admin = EXCLUDED.requested_by_admin
                """,
                (
                    user_id,
                    execute_at_ms,
                    self._clock.time_msec(),
                    requested_by,
                    requested_by_admin,
                ),
            )

        await self._datastores.main.db_pool.runInteraction(
            "pangea_delete_user_upsert_schedule",
            _upsert,
        )

    async def _delete_schedule(self, user_id: str) -> bool:
        def _delete(txn: Any) -> bool:
            txn.execute(
                f"DELETE FROM {SCHEDULE_TABLE} WHERE user_id = %s",
                (user_id,),
            )
            return bool(txn.rowcount)

        return await self._datastores.main.db_pool.runInteraction(
            "pangea_delete_user_delete_schedule",
            _delete,
        )

    async def _get_schedule(self, user_id: str) -> Dict[str, Any] | None:
        def _get(txn: Any) -> Dict[str, Any] | None:
            txn.execute(
                f"""
                SELECT user_id, execute_at_ms, requested_at_ms, requested_by, requested_by_admin
                FROM {SCHEDULE_TABLE}
                WHERE user_id = %s
                """,
                (user_id,),
            )
            row = txn.fetchone()
            if row is None:
                return None
            return {
                "user_id": row[0],
                "execute_at_ms": row[1],
                "requested_at_ms": row[2],
                "requested_by": row[3],
                "requested_by_admin": row[4],
            }

        return await self._datastores.main.db_pool.runInteraction(
            "pangea_delete_user_get_schedule",
            _get,
        )

    async def _claim_due_schedules(self, now_ms: int) -> list[Dict[str, Any]]:
        def _claim(txn: Any) -> list[Dict[str, Any]]:
            txn.execute(
                f"""
                DELETE FROM {SCHEDULE_TABLE}
                WHERE execute_at_ms <= %s
                RETURNING user_id, requested_by_admin
                """,
                (now_ms,),
            )
            rows = txn.fetchall()
            return [
                {"user_id": row[0], "requested_by_admin": bool(row[1])} for row in rows
            ]

        return await self._datastores.main.db_pool.runInteraction(
            "pangea_delete_user_claim_due_schedules",
            _claim,
        )

    async def _process_scheduled_deletes(self) -> None:
        await self._ensure_schedule_table()
        now_ms = self._clock.time_msec()
        due_schedules = await self._claim_due_schedules(now_ms)

        for schedule in due_schedules:
            user_id = schedule["user_id"]
            try:
                await self._delete_user_now(
                    user_id=user_id,
                    by_admin=bool(schedule["requested_by_admin"]),
                )
            except Exception as e:
                logger.error(
                    "Failed to process scheduled delete for %s: %s",
                    user_id,
                    e,
                )
                retry_at_ms = (
                    self._clock.time_msec()
                    + self._config.delete_user_processor_interval_seconds * 1000
                )
                await self._upsert_schedule(
                    user_id=user_id,
                    execute_at_ms=retry_at_ms,
                    requested_by=user_id,
                    requested_by_admin=bool(schedule["requested_by_admin"]),
                )
