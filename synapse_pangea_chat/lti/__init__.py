"""LTI 1.3 tool core (lane B1): OIDC login, launch validation, JWKS, Dynamic
Registration and operator approval of registered platforms (SPEC §8). On top
of it, the Canvas hand-offs (lanes B2, B3): launch redirects, the link step,
course connect, connect status and the NRPS roster import (CONTRACTS C5).

The endpoints start only when module config carries a usable `lti` block (the
private key, wired from Secrets Manager). Without one they are not registered
at all, and the reason is logged, never the key.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from synapse.module_api import ModuleApi

from synapse_pangea_chat.lti.course_link import CourseLinks
from synapse_pangea_chat.lti.endpoints import registration_resources
from synapse_pangea_chat.lti.http import PlatformHttp
from synapse_pangea_chat.lti.link_store import LtiLinkStore
from synapse_pangea_chat.lti.nrps import NrpsClient
from synapse_pangea_chat.lti.registration import PATH_PREFIX, tool_urls
from synapse_pangea_chat.lti.routes import KIND_TICKET, LtiRoute
from synapse_pangea_chat.lti.store import LtiStore
from synapse_pangea_chat.lti.student_launch import LaunchRedirects, LinkStep
from synapse_pangea_chat.notice_delivery.rate_limit import SlidingWindowRateLimiter
from synapse_pangea_chat.student_invitations.rate_limits import limit_for
from synapse_pangea_chat.student_invitations.rooms import ModuleCourseAdmins

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig
    from synapse_pangea_chat.student_invitations import StudentInvitations

logger = logging.getLogger("synapse.module.synapse_pangea_chat.lti")

# A launch's login token lives about 2 minutes (CONTRACTS C5.2).
LOGIN_TOKEN_MS = 2 * 60 * 1000


def register_lti(
    api: ModuleApi,
    config: "PangeaChatConfig",
    invitations: "StudentInvitations",
) -> Optional[LtiStore]:
    """Register the LTI resources, or log why they are not started."""
    if config.lti is None:
        if config.lti_config_error is not None:
            logger.error("LTI endpoints not started: %s", config.lti_config_error)
        else:
            logger.info("LTI endpoints not started: no lti config")
        return None
    homeserver = api._hs
    db_pool = homeserver.get_datastores().main.db_pool
    store = LtiStore(db_pool)
    links = LtiLinkStore(db_pool)
    clock = homeserver.get_clock()
    course_links = CourseLinks(
        links=links,
        platforms=store,
        invitations=invitations.store,
        admins=ModuleCourseAdmins(api),
        nrps=NrpsClient(PlatformHttp(homeserver), config.lti.signing_key, clock.time),
        admin_dash_base_url=config.admin_dash_base_url,
    )

    async def login_token(user_id: str) -> str:
        return await api.create_login_token(user_id, duration_in_ms=LOGIN_TOKEN_MS)

    # Read through Any, as the student invitation stores do: the lookup is a
    # @cached store method, which mypy cannot type without Synapse's plugin.
    main: Any = homeserver.get_datastores().main

    async def record_external_id(issuer: str, sub: str, user_id: str) -> None:
        provider = "lti:" + issuer
        holder = await main.get_user_by_external_id(provider, sub)
        if holder is None:
            await api.record_user_external_id(provider, sub, user_id)
        elif holder != user_id:
            logger.warning("LTI external id already recorded for another account")

    redirects = LaunchRedirects(
        links=links,
        invitations=invitations.store,
        login_tokens=login_token,
        app_base_url=config.app_base_url,
        admin_dash_base_url=config.admin_dash_base_url,
    )
    link_step = LinkStep(
        links=links,
        invitations=invitations.store,
        claims=invitations.claims,
        course_links=course_links,
        login_tokens=login_token,
        external_ids=record_external_id,
    )
    urls = tool_urls(api.public_baseurl)
    resources = registration_resources(api, config.lti, store, urls, redirects)
    for suffix, resource in resources.items():
        api.register_web_resource(path="/" + PATH_PREFIX + suffix, resource=resource)
    for suffix, name, method, kind, handler in (
        ("link", "lti_link", "POST", KIND_TICKET, link_step.link),
        ("connect", "lti_connect", "POST", "teacher", course_links.connect),
        ("course_status", "lti_course_status", "GET", "teacher", course_links.status),
        ("import", "lti_import", "POST", "teacher", course_links.import_roster),
    ):
        burst, seconds = limit_for(config.student_invitation_rate_limits, name)
        api.register_web_resource(
            path="/" + PATH_PREFIX + suffix,
            resource=LtiRoute(
                homeserver,
                name,
                method,
                kind,
                handler,
                SlidingWindowRateLimiter(
                    requests_per_burst=burst, burst_duration_seconds=seconds
                ),
            ),
        )
    # The invite email's "or open Pangea from your Canvas course" line.
    invitations.mailer.canvas_connected = links.is_connected
    logger.info("LTI endpoints started: kid=%s", config.lti.signing_key.kid)
    return store
