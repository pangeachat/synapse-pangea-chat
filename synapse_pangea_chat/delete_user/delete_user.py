from __future__ import annotations

import logging
from typing import TYPE_CHECKING

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
from twisted.internet import defer
from twisted.web.resource import Resource

from synapse_pangea_chat.delete_room.extract_body_json import extract_body_json
from synapse_pangea_chat.delete_user.is_rate_limited import is_rate_limited

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.delete_user")


class DeleteUser(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()
        self._deactivate_account_handler = (
            self._api._hs.get_deactivate_account_handler()
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

            external_ids = await self._datastores.main.get_external_ids_by_user(
                target_user_id
            )
            for auth_provider, external_id in external_ids:
                await self._datastores.main.remove_user_external_id(
                    auth_provider,
                    external_id,
                    target_user_id,
                )

            threepids = await self._datastores.main.user_get_threepids(target_user_id)
            for threepid in threepids:
                await self._datastores.main.user_delete_threepid(
                    target_user_id,
                    threepid.medium,
                    threepid.address,
                )

            await self._deactivate_account_handler.deactivate_account(
                user_id=target_user_id,
                erase_data=True,
                requester=create_requester(target_user_id),
                by_admin=target_user_id != requester_id,
            )

            respond_with_json(
                request,
                200,
                {
                    "message": "Deleted",
                    "user_id": target_user_id,
                    "deleted_external_ids": len(external_ids),
                    "deleted_threepids": len(threepids),
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
