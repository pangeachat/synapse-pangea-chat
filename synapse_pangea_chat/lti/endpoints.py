"""The LTI tool's web resources.

Public (platform- and browser-facing): `login` (OIDC third-party login
initiation), `launch` (the id_token POST), `jwks`, `config` (static Canvas
JSON) and `register` (Dynamic Registration). Server-admin only: `platforms`
(list) and `platforms/<id>/approve`.

Login binds a fresh state and nonce to the platform server-side and to the
browser with a cookie; the launch must present the same state in its form and
in that cookie, and consumes it, so a launch cannot be replayed or injected
into another browser.

Logs and error bodies carry reason codes and platform ids only: never a
token, a claim, a key or an email.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Awaitable, Callable, Dict, List, Optional
from urllib.parse import urlencode, urlsplit

from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.http import server
from synapse.http.server import finish_request, respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from twisted.web.resource import Resource

from synapse_pangea_chat.lti.http import PlatformHttp, UpstreamError, https_url
from synapse_pangea_chat.lti.keys import LtiSettings
from synapse_pangea_chat.lti.platform_jwks import PlatformKeyCache
from synapse_pangea_chat.lti.registration import (
    PATH_PREFIX,
    RegistrationRejected,
    ToolUrls,
    canvas_static_config,
    check_openid_configuration,
    registered_client,
    registration_request,
)
from synapse_pangea_chat.lti.store import LtiStore, PlatformExists
from synapse_pangea_chat.lti.validation import (
    Launch,
    LaunchRejected,
    read_header,
    verify_launch,
)
from synapse_pangea_chat.notice_delivery.rate_limit import SlidingWindowRateLimiter

logger = logging.getLogger("synapse.module.synapse_pangea_chat.lti")

STATE_TTL_MS = 10 * 60 * 1000
COOKIE_PREFIX = "pangea_lti_state_"
MAX_ARG_LENGTH = 4096

LaunchHandler = Callable[[SynapseRequest, Launch], Awaitable[None]]


def _arg(
    request: SynapseRequest, name: str, limit: int = MAX_ARG_LENGTH
) -> Optional[str]:
    values = request.args.get(name.encode("ascii")) or []
    if len(values) != 1:
        return None
    try:
        value = values[0].decode("utf-8")
    # silent-ok: an undecodable parameter is treated as absent and refused by the caller
    except UnicodeDecodeError:
        return None
    if not value or len(value) > limit:
        return None
    return value


def _client_ip(request: SynapseRequest) -> str:
    return request.getClientAddress().host


def _refuse(request: SynapseRequest, status: int, reason: str) -> None:
    errcode = (
        "ORG.PANGEA.LTI_PLATFORM_NOT_APPROVED"
        if reason == "platform_not_approved"
        else "ORG.PANGEA.LTI_REJECTED"
    )
    respond_with_json(
        request,
        status,
        {"errcode": errcode, "error": "LTI request refused", "reason": reason},
        send_cors=False,
    )


def cookie_name(state: str) -> str:
    """Per-state cookie name, so two launches in one browser do not collide."""
    return COOKIE_PREFIX + hashlib.sha256(state.encode("ascii")).hexdigest()[:16]


def _set_state_cookie(request: SynapseRequest, state: str, max_age: int) -> None:
    value = state if max_age > 0 else ""
    # SameSite=None: the launch is a cross-site form POST from the platform.
    request.cookies.append(
        (
            f"{cookie_name(state)}={value}; Path=/{PATH_PREFIX}launch; "
            f"Max-Age={max_age}; HttpOnly; Secure; SameSite=None"
        ).encode("ascii")
    )


async def respond_launch_not_available(request: SynapseRequest, launch: Launch) -> None:
    """Placeholder until the link and connect steps land (lanes B2/B3)."""
    respond_with_json(
        request,
        501,
        {
            "errcode": "ORG.PANGEA.LTI_LAUNCH_NOT_AVAILABLE",
            "error": "This launch was verified, but its next step is not available yet",
            "path": launch.path,
        },
        send_cors=False,
    )


class _Async(Resource):
    isLeaf = True

    def _run(self, handler: Callable[[SynapseRequest], Awaitable[None]], request):
        run_in_background(self._guard, handler, request)
        return server.NOT_DONE_YET

    async def _guard(self, handler, request: SynapseRequest) -> None:
        try:
            await handler(request)
        except Exception as e:
            # Type only: a message here could carry upstream text or a value.
            logger.error("LTI %s failed: %s", type(self).__name__, type(e).__name__)
            respond_with_json(
                request,
                500,
                {"errcode": "M_UNKNOWN", "error": "Internal server error"},
                send_cors=False,
            )


class LtiLogin(_Async):
    """OIDC third-party login initiation (GET or POST from the platform)."""

    def __init__(
        self,
        api: ModuleApi,
        store: LtiStore,
        urls: ToolUrls,
        rate_limiter: SlidingWindowRateLimiter,
    ):
        super().__init__()
        self._clock = api._hs.get_clock()
        self._store = store
        self._urls = urls
        self._rate_limiter = rate_limiter

    def render_GET(self, request: SynapseRequest):
        return self._run(self._handle, request)

    def render_POST(self, request: SynapseRequest):
        return self._run(self._handle, request)

    async def _handle(self, request: SynapseRequest) -> None:
        if self._rate_limiter.is_rate_limited(_client_ip(request)):
            _refuse(request, 429, "rate_limited")
            return
        issuer = _arg(request, "iss")
        login_hint = _arg(request, "login_hint")
        target = _arg(request, "target_link_uri")
        client_id = _arg(request, "client_id", 255)
        deployment_id = _arg(request, "lti_deployment_id", 255)
        message_hint = _arg(request, "lti_message_hint")
        if issuer is None or login_hint is None:
            _refuse(request, 400, "missing_parameter")
            return
        if target != self._urls.launch:
            _refuse(request, 400, "bad_target_link_uri")
            return
        platforms = await self._store.platforms_for_issuer(issuer, client_id)
        if not platforms:
            _refuse(request, 400, "unknown_platform")
            return
        if len(platforms) > 1:
            _refuse(request, 400, "ambiguous_platform")
            return
        [platform] = platforms
        if not platform.approved:
            logger.info("LTI login refused: platform=%s pending", platform.platform_id)
            _refuse(request, 403, "platform_not_approved")
            return
        if deployment_id is not None and deployment_id not in (
            await self._store.deployments(platform.platform_id)
        ):
            _refuse(request, 403, "unknown_deployment")
            return

        state, nonce = await self._store.issue_state(
            platform.platform_id, now_ms=self._clock.time_msec(), ttl_ms=STATE_TTL_MS
        )
        params = {
            "scope": "openid",
            "response_type": "id_token",
            "response_mode": "form_post",
            "prompt": "none",
            "client_id": platform.client_id,
            "redirect_uri": self._urls.launch,
            "login_hint": login_hint,
            "state": state,
            "nonce": nonce,
        }
        if message_hint is not None:
            params["lti_message_hint"] = message_hint
        separator = "&" if urlsplit(platform.auth_login_url).query else "?"
        location = platform.auth_login_url + separator + urlencode(params)
        _set_state_cookie(request, state, STATE_TTL_MS // 1000)
        request.setHeader(b"Cache-Control", b"no-store")
        request.setHeader(b"Location", location.encode("ascii"))
        request.setResponseCode(302)
        finish_request(request)


class LtiLaunch(_Async):
    """The platform's form POST of the id_token and state."""

    def __init__(
        self,
        api: ModuleApi,
        store: LtiStore,
        key_cache: PlatformKeyCache,
        urls: ToolUrls,
        rate_limiter: SlidingWindowRateLimiter,
        on_launch: LaunchHandler,
    ):
        super().__init__()
        self._clock = api._hs.get_clock()
        self._store = store
        self._key_cache = key_cache
        self._urls = urls
        self._rate_limiter = rate_limiter
        self._on_launch = on_launch

    def render_POST(self, request: SynapseRequest):
        return self._run(self._handle, request)

    async def _handle(self, request: SynapseRequest) -> None:
        platform_id = None
        try:
            if self._rate_limiter.is_rate_limited(_client_ip(request)):
                raise LaunchRejected("rate_limited", status=429)
            if request.args.get(b"error"):
                raise LaunchRejected("platform_error")
            state = _arg(request, "state", 128)
            id_token = _arg(request, "id_token", 65536)
            if state is None or not state.isascii():
                raise LaunchRejected("missing_state")
            cookie = request.getCookie(cookie_name(state).encode("ascii"))
            if cookie is None or not hmac.compare_digest(cookie, state.encode("ascii")):
                raise LaunchRejected("state_mismatch")
            consumed = await self._store.consume_state(
                state, now_ms=self._clock.time_msec()
            )
            if consumed is None:
                raise LaunchRejected("unknown_state")
            _set_state_cookie(request, state, 0)
            nonce, platform_id = consumed
            platform = await self._store.get_platform(platform_id)
            if platform is None:
                raise LaunchRejected("unknown_platform")
            if not platform.approved:
                raise LaunchRejected("platform_not_approved", status=403)
            kid = read_header(id_token)
            key = await self._key_cache.get_key(platform.jwks_uri, kid)
            if key is None:
                raise LaunchRejected("unknown_key")
            launch = verify_launch(
                id_token or "",
                platform=platform,
                key=key,
                expected_nonce=nonce,
                known_deployments=await self._store.deployments(platform_id),
                launch_url=self._urls.launch,
            )
        except LaunchRejected as rejected:
            logger.info(
                "LTI launch rejected: reason=%s platform=%s",
                rejected.code,
                platform_id or "-",
            )
            _refuse(request, rejected.status, rejected.code)
            return
        logger.info(
            "LTI launch verified: platform=%s path=%s", launch.platform_id, launch.path
        )
        await self._on_launch(request, launch)


class LtiJwks(_Async):
    def __init__(self, settings: LtiSettings):
        super().__init__()
        self._settings = settings

    def render_GET(self, request: SynapseRequest):
        return self._run(self._handle, request)

    async def _handle(self, request: SynapseRequest) -> None:
        request.setHeader(b"Cache-Control", b"public, max-age=300")
        respond_with_json(request, 200, self._settings.public_jwks(), send_cors=True)


class LtiStaticConfig(_Async):
    def __init__(self, urls: ToolUrls):
        super().__init__()
        self._urls = urls

    def render_GET(self, request: SynapseRequest):
        return self._run(self._handle, request)

    async def _handle(self, request: SynapseRequest) -> None:
        respond_with_json(
            request, 200, canvas_static_config(self._urls), send_cors=True
        )


_CLOSE_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Pangea Chat</title></head>
<body><p>{message}</p>
<script>
(window.opener || window.parent).postMessage({{subject: "org.imsglobal.lti.close"}}, "*");
</script></body></html>
"""
_ERROR_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Pangea Chat</title></head>
<body><p>Pangea Chat could not be registered ({reason}).</p></body></html>
"""


def _respond_registration_page(
    request: SynapseRequest, status: int, html: str, frame_origin: Optional[str]
) -> None:
    """The registration page is shown inside the platform's own page, so it
    may be framed, but only by the platform's origin."""
    body = html.encode("utf-8")
    request.setResponseCode(status)
    request.setHeader(b"Content-Type", b"text/html; charset=utf-8")
    request.setHeader(b"Content-Length", b"%d" % (len(body),))
    request.setHeader(b"Cache-Control", b"no-store")
    ancestors = frame_origin or "'none'"
    request.setHeader(
        b"Content-Security-Policy",
        (
            f"default-src 'none'; script-src 'unsafe-inline'; "
            f"frame-ancestors {ancestors}"
        ).encode("ascii"),
    )
    request.write(body)
    finish_request(request)


def logged_registration_uri(uri: bytes) -> bytes:
    """The request URI as Synapse should log it: path only, query redacted.

    Dynamic Registration puts the platform's registration token in the query
    string, and Synapse's access log writes the request URI when the request
    finishes, redacting only `access_token` and `client_secret`. Matching the
    parameter name is not enough (percent-encoding spells it many ways), so
    the whole query is dropped; the arguments are already parsed by then.
    """
    path, separator, _ = uri.partition(b"?")
    return path + b"?<redacted>" if separator else path


def _redact_registration_token(request: SynapseRequest) -> None:
    if isinstance(request.uri, bytes):
        request.uri = logged_registration_uri(request.uri)


class LtiRegister(_Async):
    """Dynamic Registration: the platform opens this URL in its admin UI
    (GET with query parameters, or POST with the same as a form body)."""

    def __init__(
        self,
        api: ModuleApi,
        store: LtiStore,
        http: PlatformHttp,
        urls: ToolUrls,
        rate_limiter: SlidingWindowRateLimiter,
    ):
        super().__init__()
        self._clock = api._hs.get_clock()
        self._store = store
        self._http = http
        self._urls = urls
        self._rate_limiter = rate_limiter

    def render_GET(self, request: SynapseRequest):
        return self._run(self._handle, request)

    def render_POST(self, request: SynapseRequest):
        # The same parameters as a form body; Twisted parses them into args.
        return self._run(self._handle, request)

    async def _handle(self, request: SynapseRequest) -> None:
        _redact_registration_token(request)
        config_url = _arg(request, "openid_configuration", 2048)
        frame_origin = None
        if config_url is not None and https_url(config_url) is not None:
            parts = urlsplit(config_url)
            frame_origin = f"https://{parts.netloc}"
        try:
            if self._rate_limiter.is_rate_limited(_client_ip(request)):
                raise RegistrationRejected("rate_limited")
            registration_token = _arg(request, "registration_token")
            if config_url is None or frame_origin is None:
                raise RegistrationRejected("bad_configuration_url")
            try:
                document = await self._http.get_json(config_url)
            except UpstreamError as e:
                raise RegistrationRejected("configuration_" + e.reason) from None
            platform = check_openid_configuration(config_url, document)
            try:
                response = await self._http.post_json(
                    platform.registration_endpoint,
                    registration_request(self._urls),
                    registration_token,
                )
            except UpstreamError as e:
                raise RegistrationRejected("registration_" + e.reason) from None
            client_id, deployment_id = registered_client(response)
            try:
                platform_id = await self._store.create_platform(
                    issuer=platform.issuer,
                    client_id=client_id,
                    auth_login_url=platform.authorization_endpoint,
                    token_url=platform.token_endpoint,
                    jwks_uri=platform.jwks_uri,
                    product_family=platform.product_family,
                    deployment_id=deployment_id,
                    now_ms=self._clock.time_msec(),
                )
            except PlatformExists:
                raise RegistrationRejected("already_registered") from None
        except RegistrationRejected as rejected:
            logger.info("LTI registration refused: reason=%s", rejected.code)
            status = 429 if rejected.code == "rate_limited" else 400
            _respond_registration_page(
                request,
                status,
                _ERROR_PAGE.format(reason=rejected.code),
                frame_origin,
            )
            return
        logger.info(
            "LTI platform registered (pending approval): platform=%s family=%s",
            platform_id,
            platform.product_family or "-",
        )
        _respond_registration_page(
            request,
            200,
            _CLOSE_PAGE.format(
                message="Pangea Chat is registered. It can be used once "
                "Pangea has approved this registration."
            ),
            frame_origin,
        )


class LtiPlatformsAdmin(_Async):
    """`GET platforms` lists registrations; `POST platforms/<id>/approve`
    approves one (no body: its deployments come from its registration).
    Server admins only."""

    def __init__(self, api: ModuleApi, store: LtiStore):
        super().__init__()
        self._api = api
        self._clock = api._hs.get_clock()
        self._store = store

    def render_GET(self, request: SynapseRequest):
        return self._run(self._list, request)

    def render_POST(self, request: SynapseRequest):
        return self._run(self._approve, request)

    async def _admin(self, request: SynapseRequest) -> Optional[str]:
        try:
            requester = await self._api.get_user_by_req(request)
        # silent-ok: the caller's auth failure, answered 401
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ):
            respond_with_json(
                request,
                401,
                {"errcode": "M_UNAUTHORIZED", "error": "Unauthorized"},
                send_cors=True,
            )
            return None
        user_id = requester.user.to_string()
        if not await self._api.is_user_admin(user_id):
            respond_with_json(
                request,
                403,
                {"errcode": "M_FORBIDDEN", "error": "Server admin required"},
                send_cors=True,
            )
            return None
        return user_id

    def _segments(self, request: SynapseRequest) -> List[str]:
        return [s.decode("utf-8", "replace") for s in (request.postpath or []) if s]

    async def _list(self, request: SynapseRequest) -> None:
        if await self._admin(request) is None:
            return
        if self._segments(request):
            respond_with_json(request, 404, {"errcode": "M_NOT_FOUND"}, send_cors=True)
            return
        platforms = await self._store.list_platforms()
        respond_with_json(request, 200, {"platforms": platforms}, send_cors=True)

    async def _approve(self, request: SynapseRequest) -> None:
        operator = await self._admin(request)
        if operator is None:
            return
        segments = self._segments(request)
        if len(segments) != 2 or segments[1] != "approve":
            respond_with_json(request, 404, {"errcode": "M_NOT_FOUND"}, send_cors=True)
            return
        platform_id = segments[0]
        found = await self._store.approve(
            platform_id,
            operator=operator,
            now_ms=self._clock.time_msec(),
        )
        if not found:
            respond_with_json(request, 404, {"errcode": "M_NOT_FOUND"}, send_cors=True)
            return
        logger.info(
            "LTI platform approved: platform=%s operator=%s",
            platform_id,
            operator,
        )
        respond_with_json(
            request,
            200,
            {"platform_id": platform_id, "state": "approved"},
            send_cors=True,
        )


def registration_resources(
    api: ModuleApi,
    settings: LtiSettings,
    store: LtiStore,
    urls: ToolUrls,
    on_launch: LaunchHandler,
) -> Dict[str, Resource]:
    """Every LTI resource, keyed by the path suffix after `lti/`."""
    http = PlatformHttp(api._hs)
    clock = api._hs.get_clock()
    key_cache = PlatformKeyCache(http.get_json, clock.time)
    return {
        "login": LtiLogin(
            api,
            store,
            urls,
            SlidingWindowRateLimiter(requests_per_burst=300, burst_duration_seconds=60),
        ),
        "launch": LtiLaunch(
            api,
            store,
            key_cache,
            urls,
            SlidingWindowRateLimiter(requests_per_burst=300, burst_duration_seconds=60),
            on_launch,
        ),
        "jwks": LtiJwks(settings),
        "config": LtiStaticConfig(urls),
        "register": LtiRegister(
            api,
            store,
            http,
            urls,
            SlidingWindowRateLimiter(requests_per_burst=10, burst_duration_seconds=600),
        ),
        "platforms": LtiPlatformsAdmin(api, store),
    }
