from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

from synapse.api.errors import SynapseError
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from synapse.push.mailer import Mailer
from synapse.util.stringutils import assert_valid_client_secret, random_string
from synapse.util.threepids import check_3pid_allowed, validate_email
from twisted.internet import defer
from twisted.web.resource import Resource

from synapse_pangea_chat.register_email.is_rate_limited import is_rate_limited

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.register_email.register_email"
)


class RegisterEmailRequestToken(Resource):
    """Pangea-specific endpoint that validates username before sending
    registration email token.

    POST /_synapse/client/pangea/v1/register/email/requestToken

    Combines username validation with register email token request so that
    a validation email is never sent for a username that's already taken.
    """

    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._hs = api._hs
        self._registration_handler = self._hs.get_registration_handler()
        self._identity_handler = self._hs.get_identity_handler()
        self._datastores = self._hs.get_datastores()

        if self._hs.config.email.can_verify_email:
            self._registration_mailer = Mailer(
                hs=self._hs,
                app_name=self._hs.config.email.email_app_name,
                template_html=self._hs.config.email.email_registration_template_html,
                template_text=self._hs.config.email.email_registration_template_text,
            )
        else:
            self._registration_mailer = None

    def render_POST(self, request: SynapseRequest):
        defer.ensureDeferred(self._async_render_POST(request))
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest):
        try:
            body = self._extract_body_json(request)
            if not isinstance(body, dict):
                respond_with_json(
                    request,
                    400,
                    {"errcode": "M_NOT_JSON", "error": "Invalid JSON in request body"},
                    send_cors=True,
                )
                return

            missing_fields = [
                f
                for f in ("username", "client_secret", "email", "send_attempt")
                if f not in body
            ]
            if missing_fields:
                respond_with_json(
                    request,
                    400,
                    {
                        "errcode": "M_MISSING_PARAM",
                        "error": f"Missing required fields: {', '.join(missing_fields)}",
                    },
                    send_cors=True,
                )
                return

            username = body["username"]
            client_secret = body["client_secret"]
            email_raw = body["email"]
            send_attempt = body["send_attempt"]
            next_link = body.get("next_link")

            # --- Rate limit by IP (unauthenticated endpoint) ---
            ip = request.getClientAddress().host
            if is_rate_limited(ip, self._config):
                respond_with_json(
                    request,
                    429,
                    {"errcode": "M_LIMIT_EXCEEDED", "error": "Rate limited"},
                    send_cors=True,
                )
                return

            # --- Validate client_secret ---
            try:
                assert_valid_client_secret(client_secret)
            except SynapseError as e:
                respond_with_json(
                    request,
                    e.code,
                    {
                        "errcode": getattr(e.errcode, "value", str(e.errcode)),
                        "error": e.msg,
                    },
                    send_cors=True,
                )
                return

            # --- Username validation ---
            # Respect inhibit_user_in_use_error config (matches /register/available behavior)
            inhibit_user_in_use = self._hs.config.registration.inhibit_user_in_use_error
            try:
                await self._registration_handler.check_username(
                    localpart=username,
                    inhibit_user_in_use_error=inhibit_user_in_use,
                )
            except SynapseError as e:
                respond_with_json(
                    request,
                    e.code,
                    {
                        "errcode": getattr(e.errcode, "value", str(e.errcode)),
                        "error": e.msg,
                    },
                    send_cors=True,
                )
                return

            # --- Email validation (only reached if username is valid) ---

            if self._registration_mailer is None:
                respond_with_json(
                    request,
                    400,
                    {
                        "errcode": "M_UNKNOWN",
                        "error": "Email-based registration has been disabled on this server",
                    },
                    send_cors=True,
                )
                return

            try:
                email = validate_email(email_raw)
            except ValueError as e:
                respond_with_json(
                    request,
                    400,
                    {"errcode": "M_INVALID_PARAM", "error": str(e)},
                    send_cors=True,
                )
                return

            if not await check_3pid_allowed(
                self._hs, "email", email, registration=True
            ):
                respond_with_json(
                    request,
                    403,
                    {
                        "errcode": "M_THREEPID_DENIED",
                        "error": "Your email domain is not authorized to register on this server",
                    },
                    send_cors=True,
                )
                return

            existing_user_id = await self._datastores.main.get_user_id_by_threepid(
                "email", email
            )
            if existing_user_id is not None:
                if self._hs.config.server.request_token_inhibit_3pid_errors:
                    # Don't reveal that the email is already in use.
                    # Sleep a random amount to avoid timing side-channels.
                    await asyncio.sleep(random.randint(100, 1000) / 1000)
                    respond_with_json(
                        request,
                        200,
                        {"sid": random_string(16)},
                        send_cors=True,
                    )
                    return

                respond_with_json(
                    request,
                    400,
                    {
                        "errcode": "M_THREEPID_IN_USE",
                        "error": "Email is already in use",
                    },
                    send_cors=True,
                )
                return

            sid = await self._identity_handler.send_threepid_validation(
                email,
                client_secret,
                send_attempt,
                self._registration_mailer.send_registration_mail,
                next_link,
            )

            respond_with_json(
                request,
                200,
                {"sid": sid},
                send_cors=True,
            )

        except SynapseError as e:
            respond_with_json(
                request,
                e.code,
                {
                    "errcode": getattr(e.errcode, "value", str(e.errcode)),
                    "error": e.msg,
                },
                send_cors=True,
            )

        except Exception as e:
            logger.exception("Error processing register email request: %s", e)
            respond_with_json(
                request,
                500,
                {"errcode": "M_UNKNOWN", "error": "Internal server error"},
                send_cors=True,
            )

    def _extract_body_json(self, request: SynapseRequest):
        try:
            body = request.content.read()
            return json.loads(body.decode("utf-8"))
        except Exception:
            return None
