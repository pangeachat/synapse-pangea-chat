from __future__ import annotations

import io
import json
import logging
import os
import zipfile
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Sequence

from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
    SynapseError,
)
from synapse.events import EventBase
from synapse.handlers.admin import ExfiltrationWriter
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from synapse.types import JsonMapping, StateMap

try:
    from synapse.util.duration import Duration
except ImportError:
    import synapse as _synapse

    raise ImportError(
        f"synapse_pangea_chat.export_user_data requires Synapse >= 1.148.0 "
        f"(synapse.util.duration is not available in Synapse {_synapse.__version__}). "
        f"Either upgrade Synapse or pin synapse-pangea-chat to commit 9d9d411 "
        f"(the last commit before export_user_data was added)."
    ) from None
from twisted.internet import defer
from twisted.web.resource import Resource

from synapse_pangea_chat.delete_room.extract_body_json import extract_body_json
from synapse_pangea_chat.export_user_data.is_rate_limited import is_rate_limited

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.export_user_data")

SCHEDULE_TABLE = "pangea_export_user_data_schedule"
VALID_ACTIONS = {"schedule", "cancel", "force", "status"}


class JsonExfiltrationWriter(ExfiltrationWriter):
    """Collects exported user data into an in-memory dict for ZIP packaging."""

    def __init__(self) -> None:
        self._data: Dict[str, Any] = {
            "rooms": {},
            "user_data": {
                "profile": None,
                "devices": [],
                "connections": [],
                "account_data": {},
            },
            "media_ids": [],
        }

    def write_events(self, room_id: str, events: List[EventBase]) -> None:
        room = self._data["rooms"].setdefault(room_id, {})
        room.setdefault("events", []).extend(e.get_pdu_json() for e in events)

    def write_state(
        self, room_id: str, event_id: str, state: StateMap[EventBase]
    ) -> None:
        room = self._data["rooms"].setdefault(room_id, {})
        state_dict = room.setdefault("state", {})
        state_dict[event_id] = [e.get_pdu_json() for e in state.values()]

    def write_invite(
        self, room_id: str, event: EventBase, state: StateMap[EventBase]
    ) -> None:
        room = self._data["rooms"].setdefault(room_id, {})
        room.setdefault("events", []).append(event.get_pdu_json())
        room["invite_state"] = list(state.values())

    def write_knock(
        self, room_id: str, event: EventBase, state: StateMap[EventBase]
    ) -> None:
        room = self._data["rooms"].setdefault(room_id, {})
        room.setdefault("events", []).append(event.get_pdu_json())
        room["knock_state"] = list(state.values())

    def write_profile(self, profile: JsonMapping) -> None:
        self._data["user_data"]["profile"] = profile

    def write_devices(self, devices: Sequence[JsonMapping]) -> None:
        self._data["user_data"]["devices"] = list(devices)

    def write_connections(self, connections: Sequence[JsonMapping]) -> None:
        self._data["user_data"]["connections"] = list(connections)

    def write_account_data(
        self, file_name: str, account_data: Mapping[str, JsonMapping]
    ) -> None:
        self._data["user_data"]["account_data"][file_name] = dict(account_data)

    def write_media_id(self, media_id: str, media_metadata: JsonMapping) -> None:
        self._data["media_ids"].append(
            {
                "media_id": media_id,
                "metadata": media_metadata,
            }
        )

    def finished(self) -> Dict[str, Any]:
        return self._data


class ExportUserData(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._clock = self._api._hs.get_clock()
        self._datastores = self._api._hs.get_datastores()
        self._admin_handler = self._api._hs.get_admin_handler()
        self._media_repository = self._api._hs.get_media_repository()
        self._schedule_table_ready = False

        self._clock.looping_call(
            self._api._hs.run_as_background_process,
            Duration(seconds=self._config.export_user_data_processor_interval_seconds),
            desc="pangea_export_user_data_process_schedules",
            func=self._process_scheduled_exports,
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
                        "error": "Invalid action. Must be one of: schedule, cancel, force, status"
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
                    {"error": "Can only export local users"},
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
                # Export starts immediately (no delay like delete)
                await self._upsert_schedule(
                    user_id=target_user_id,
                    execute_at_ms=now_ms,
                    requested_by=requester_id,
                    requested_by_admin=target_user_id != requester_id,
                )
                respond_with_json(
                    request,
                    200,
                    {
                        "message": "Export scheduled",
                        "action": "schedule",
                        "user_id": target_user_id,
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
                            "Export schedule canceled"
                            if canceled
                            else "No export schedule found"
                        ),
                        "action": "cancel",
                        "user_id": target_user_id,
                        "canceled": canceled,
                    },
                    send_cors=True,
                )
                return

            if action == "status":
                schedule = await self._get_schedule(target_user_id)
                respond_with_json(
                    request,
                    200,
                    {
                        "action": "status",
                        "user_id": target_user_id,
                        "scheduled": schedule is not None,
                        "schedule": schedule,
                    },
                    send_cors=True,
                )
                return

            # action == "force"
            if not is_admin:
                respond_with_json(
                    request,
                    403,
                    {"error": "Forbidden: server admin required for force action"},
                    send_cors=True,
                )
                return

            schedule = await self._get_schedule(target_user_id)
            if schedule is None:
                respond_with_json(
                    request,
                    400,
                    {"error": "No export schedule found for user"},
                    send_cors=True,
                )
                return

            await self._export_user_now(target_user_id)
            await self._delete_schedule(target_user_id)

            respond_with_json(
                request,
                200,
                {
                    "message": "Export completed",
                    "action": "force",
                    "user_id": target_user_id,
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
            logger.error("Synapse error while exporting user data: %s", e)
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

    async def _export_user_now(self, user_id: str) -> None:
        # Step 1: Run Synapse's built-in export
        writer = JsonExfiltrationWriter()
        await self._admin_handler.export_user_data(user_id, writer)
        export_data = writer.finished()

        # Step 2: Build ZIP
        zip_bytes = await self._build_export_zip(user_id, export_data)

        # Step 3: Write to disk (warn on failure, continue to CMS upload)
        disk_path = self._write_zip_to_disk(user_id, zip_bytes)

        # Step 4: Upload to CMS if configured (warn on failure)
        await self._upload_to_cms(user_id, zip_bytes)

        logger.info(
            "Export completed for %s (ZIP %d bytes, disk=%s)",
            user_id,
            len(zip_bytes),
            disk_path or "skipped",
        )

    def _write_zip_to_disk(self, user_id: str, zip_bytes: bytes) -> str | None:
        output_dir = self._config.export_user_data_output_dir
        if not output_dir:
            return None

        try:
            os.makedirs(output_dir, exist_ok=True)
            safe_user_id = user_id.replace("@", "").replace(":", "_")
            filename = f"export_{safe_user_id}.zip"
            file_path = os.path.join(output_dir, filename)
            with open(file_path, "wb") as f:
                f.write(zip_bytes)
            logger.info("Export ZIP written to %s", file_path)
            return file_path
        except Exception as e:
            logger.warning("Failed to write export ZIP to disk for %s: %s", user_id, e)
            return None

    async def _upload_to_cms(self, user_id: str, zip_bytes: bytes) -> None:
        cms_base_url = self._config.cms_base_url
        cms_api_key = self._config.cms_service_api_key
        cms_record_id: str | None = None
        try:
            cms_record_id = await self._cms_create_export_record(
                user_id, cms_base_url, cms_api_key
            )
            await self._cms_update_export_status(
                cms_record_id, "processing", cms_base_url, cms_api_key
            )
            await self._cms_upload_zip(
                cms_record_id, user_id, zip_bytes, cms_base_url, cms_api_key
            )
        except Exception as e:
            logger.warning("CMS upload failed for %s: %s", user_id, e)
            if cms_record_id:
                try:
                    await self._cms_update_export_status(
                        cms_record_id,
                        "failed",
                        cms_base_url,
                        cms_api_key,
                        error=f"Export failed for {user_id}",
                    )
                except Exception as cms_err:
                    logger.warning(
                        "Failed to update CMS export status to failed: %s",
                        cms_err,
                    )

    async def _build_export_zip(
        self, user_id: str, export_data: Dict[str, Any]
    ) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # Write the main JSON data
            zf.writestr(
                "user_data.json",
                json.dumps(export_data, default=str, indent=2),
            )

            # Fetch and include media binaries
            for media_entry in export_data.get("media_ids", []):
                media_id = media_entry["media_id"]
                try:
                    media_info = await self._datastores.main.get_local_media(media_id)
                    if media_info is None:
                        continue

                    file_path = self._media_repository.filepaths.local_media_filepath(
                        media_id
                    )
                    try:
                        with open(file_path, "rb") as f:
                            media_bytes = f.read()
                    except FileNotFoundError:
                        logger.warning(
                            "Media file not found on disk for %s: %s",
                            media_id,
                            file_path,
                        )
                        continue

                    # Use media type to determine extension
                    media_type = media_info.media_type or "application/octet-stream"
                    ext = _media_type_to_ext(media_type)
                    upload_name = media_info.upload_name
                    if upload_name:
                        filename = f"media/{media_id}/{upload_name}"
                    else:
                        filename = f"media/{media_id}/file{ext}"
                    zf.writestr(filename, media_bytes)
                except Exception as e:
                    logger.warning("Failed to include media %s: %s", media_id, e)

        return buf.getvalue()

    # ---- CMS API helpers ----

    async def _cms_create_export_record(
        self, user_id: str, cms_base_url: str, cms_api_key: str
    ) -> str:
        from twisted.internet import reactor
        from twisted.web.client import Agent, readBody
        from twisted.web.http_headers import Headers

        agent = Agent(reactor)
        body_bytes = json.dumps(
            {
                "user": user_id,
                "status": "pending",
                "requestedAt": _now_iso(),
            }
        ).encode("utf-8")

        response = await agent.request(
            b"POST",
            f"{cms_base_url}/api/user-exports".encode("utf-8"),
            Headers(
                {
                    b"Content-Type": [b"application/json"],
                    b"Authorization": [f"users API-Key {cms_api_key}".encode("utf-8")],
                }
            ),
            _BytesProducer(body_bytes),
        )

        resp_body = await readBody(response)
        if response.code >= 400:
            raise RuntimeError(
                f"CMS create export record failed ({response.code}): "
                f"{resp_body.decode('utf-8', errors='replace')}"
            )

        data = json.loads(resp_body)
        doc = data.get("doc", data)
        record_id = str(doc.get("id", ""))
        if not record_id:
            raise RuntimeError(f"CMS response missing id: {resp_body.decode()}")
        return record_id

    async def _cms_update_export_status(
        self,
        record_id: str,
        status: str,
        cms_base_url: str,
        cms_api_key: str,
        error: str | None = None,
    ) -> None:
        from twisted.internet import reactor
        from twisted.web.client import Agent, readBody
        from twisted.web.http_headers import Headers

        agent = Agent(reactor)
        payload: Dict[str, Any] = {"status": status}
        if error is not None:
            payload["error"] = error
        body_bytes = json.dumps(payload).encode("utf-8")

        response = await agent.request(
            b"PATCH",
            f"{cms_base_url}/api/user-exports/{record_id}".encode("utf-8"),
            Headers(
                {
                    b"Content-Type": [b"application/json"],
                    b"Authorization": [f"users API-Key {cms_api_key}".encode("utf-8")],
                }
            ),
            _BytesProducer(body_bytes),
        )

        resp_body = await readBody(response)
        if response.code >= 400:
            raise RuntimeError(
                f"CMS update export status failed ({response.code}): "
                f"{resp_body.decode('utf-8', errors='replace')}"
            )

    async def _cms_upload_zip(
        self,
        record_id: str,
        user_id: str,
        zip_bytes: bytes,
        cms_base_url: str,
        cms_api_key: str,
    ) -> None:
        from twisted.internet import reactor
        from twisted.web.client import Agent, readBody
        from twisted.web.http_headers import Headers

        agent = Agent(reactor)
        boundary = b"----PangeaExportBoundary"
        safe_user_id = user_id.replace("@", "").replace(":", "_")
        filename = f"export_{safe_user_id}.zip"

        body_parts = []
        # status field
        body_parts.append(b"--" + boundary)
        body_parts.append(b'Content-Disposition: form-data; name="status"\r\n')
        body_parts.append(b"complete")

        # file field
        body_parts.append(b"--" + boundary)
        body_parts.append(
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/zip\r\n".encode("utf-8")
        )
        body_parts.append(zip_bytes)
        body_parts.append(b"--" + boundary + b"--")

        multipart_body = b"\r\n".join(body_parts)

        response = await agent.request(
            b"PATCH",
            f"{cms_base_url}/api/user-exports/{record_id}".encode("utf-8"),
            Headers(
                {
                    b"Content-Type": [
                        f"multipart/form-data; boundary={boundary.decode()}".encode(
                            "utf-8"
                        )
                    ],
                    b"Authorization": [f"users API-Key {cms_api_key}".encode("utf-8")],
                }
            ),
            _BytesProducer(multipart_body),
        )

        resp_body = await readBody(response)
        if response.code >= 400:
            raise RuntimeError(
                f"CMS upload ZIP failed ({response.code}): "
                f"{resp_body.decode('utf-8', errors='replace')}"
            )

    # ---- Schedule table operations ----

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
            "pangea_export_user_data_create_schedule_table",
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
            "pangea_export_user_data_upsert_schedule",
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
            "pangea_export_user_data_delete_schedule",
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
            "pangea_export_user_data_get_schedule",
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
            "pangea_export_user_data_claim_due_schedules",
            _claim,
        )

    async def _process_scheduled_exports(self) -> None:
        await self._ensure_schedule_table()
        now_ms = self._clock.time_msec()
        due_schedules = await self._claim_due_schedules(now_ms)

        for schedule in due_schedules:
            user_id = schedule["user_id"]
            try:
                await self._export_user_now(user_id)
            except Exception as e:
                logger.error(
                    "Failed to process scheduled export for %s: %s",
                    user_id,
                    e,
                )
                # Re-schedule for retry
                retry_at_ms = (
                    self._clock.time_msec()
                    + self._config.export_user_data_processor_interval_seconds * 1000
                )
                await self._upsert_schedule(
                    user_id=user_id,
                    execute_at_ms=retry_at_ms,
                    requested_by=user_id,
                    requested_by_admin=bool(schedule["requested_by_admin"]),
                )


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _media_type_to_ext(media_type: str) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "audio/mpeg": ".mp3",
        "audio/ogg": ".ogg",
        "application/pdf": ".pdf",
    }
    return mapping.get(media_type, "")


class _BytesProducer:
    """A simple IBodyProducer for Twisted's Agent."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.length = len(body)

    def startProducing(self, consumer):  # type: ignore[no-untyped-def]
        consumer.write(self.body)
        from twisted.internet import defer

        return defer.succeed(None)

    def pauseProducing(self) -> None:
        pass

    def stopProducing(self) -> None:
        pass
