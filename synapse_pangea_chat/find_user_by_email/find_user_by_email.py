"""Server-admin lookup: which local accounts hold this email address?

``POST /_synapse/client/pangea/v1/find_user_by_email``

Synapse's admin user search matches only the user-ID localpart and the display
name, so an email address cannot be resolved to an account through it. This
endpoint answers that question directly against the bound threepids.

POST rather than GET: the address is personal data, and a query string would be
written into Synapse's access log for every lookup.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from twisted.web.resource import Resource

from synapse_pangea_chat.find_user_by_email.is_rate_limited import is_rate_limited
from synapse_pangea_chat.find_user_by_email.lookup import find_users_by_email_db
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.find_user_by_email.find_user_by_email"
)

# RFC 3696 puts the practical ceiling at 254; Synapse allows 500. Match Synapse
# so an address it accepted at bind time can always be looked up again.
MAX_EMAIL_ADDRESS_LENGTH = 500


def normalise_address(raw: object) -> Optional[str]:
    """Return the trimmed address, or None if it cannot be an email address.

    Casing is left alone — the lookup compares case-insensitively, and the
    caller's spelling is echoed back so they can see what they asked for.
    """
    if not isinstance(raw, str):
        return None
    address = raw.strip()
    if not address or len(address) > MAX_EMAIL_ADDRESS_LENGTH:
        return None
    parts = address.split("@")
    if len(parts) != 2:
        return None
    if not parts[0] or not parts[1]:
        return None
    return address


class FindUserByEmail(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._async_render_POST, request)
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest) -> None:
        try:
            requester = await self._auth.get_user_by_req(request)
            requester_id = requester.user.to_string()

            if is_rate_limited(requester_id, self._config):
                respond_with_json(
                    request, 429, {"error": "Rate limited"}, send_cors=True
                )
                return

            if not await self._api.is_user_admin(requester_id):
                respond_with_json(
                    request,
                    403,
                    {"error": "Forbidden: server admin required"},
                    send_cors=True,
                )
                return

            body = await extract_body_json(request)
            if not isinstance(body, dict):
                respond_with_json(
                    request,
                    400,
                    {"error": "Request body must be a JSON object"},
                    send_cors=True,
                )
                return

            address = normalise_address(body.get("address"))
            if address is None:
                respond_with_json(
                    request,
                    400,
                    {"error": "'address' must be a valid email address"},
                    send_cors=True,
                )
                return

            results = await find_users_by_email_db(
                self._datastores.main.db_pool, address
            )

            respond_with_json(
                request,
                200,
                {"address": address, "results": results},
                send_cors=True,
            )
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ) as e:
            logger.info("Authentication failed: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
        except Exception:
            # Never echo the address into the error path — it is personal data
            # and this handler's failures are all schema/DB level.
            logger.exception("Error looking up user by email")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )
