"""LTI 1.3 tool core: OIDC login, launch validation, JWKS, Dynamic Registration
and operator approval of registered platforms (SPEC §8, lane B1).

The endpoints start only when module config carries a usable `lti` block (the
private key, wired from Secrets Manager). Without one they are not registered
at all, and the reason is logged, never the key.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from synapse.module_api import ModuleApi

from synapse_pangea_chat.lti.endpoints import (
    LaunchHandler,
    registration_resources,
    respond_launch_not_available,
)
from synapse_pangea_chat.lti.registration import PATH_PREFIX, tool_urls
from synapse_pangea_chat.lti.store import LtiStore

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.lti")


def register_lti(
    api: ModuleApi,
    config: "PangeaChatConfig",
    on_launch: Optional[LaunchHandler] = None,
) -> Optional[LtiStore]:
    """Register the LTI resources, or log why they are not started.

    Returns the store when the endpoints are up, for the later lanes (course
    link, student link) to build on.
    """
    if config.lti is None:
        if config.lti_config_error is not None:
            logger.error("LTI endpoints not started: %s", config.lti_config_error)
        else:
            logger.info("LTI endpoints not started: no lti config")
        return None
    store = LtiStore(api._hs.get_datastores().main.db_pool)
    urls = tool_urls(api.public_baseurl)
    resources = registration_resources(
        api,
        config.lti,
        store,
        urls,
        on_launch or respond_launch_not_available,
    )
    for suffix, resource in resources.items():
        api.register_web_resource(path="/" + PATH_PREFIX + suffix, resource=resource)
    logger.info("LTI endpoints started: kid=%s", config.lti.signing_key.kid)
    return store
