"""LTI Dynamic Registration, the only way a platform installs the tool in v1.

Dynamic Registration (1EdTech LTI Dynamic Registration 1.0): the platform
opens `/lti/register?openid_configuration=<url>&registration_token=<t>`; the
tool fetches that OpenID configuration, checks that the configuration's
`issuer` is on the same host as the URL it was fetched from (so a document
hosted anywhere cannot claim to be some other platform) and that its
`registration_endpoint` is on that host too (the registration token is never
sent elsewhere), posts its client registration there, and records the
platform as pending with the deployment id the platform returns (a response
without one is refused: nothing else adds deployments). Only an operator's
approval lets it launch.

The registration declares the `course_navigation` placement,
opened in a new window (the app's own sign-in storage does not work in an
iframe), the NRPS `contextmembership.readonly` scope, and Canvas
`privacy_level: public` (names and emails).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import attr

from synapse_pangea_chat.lti.http import host_of, https_url

TOOL_NAME = "Pangea Chat"
PATH_PREFIX = "_synapse/client/pangea/v1/lti/"
NRPS_SCOPE = "https://purl.imsglobal.org/spec/lti-nrps/scope/contextmembership.readonly"
TOOL_CONFIGURATION = "https://purl.imsglobal.org/spec/lti-tool-configuration"
PLATFORM_CONFIGURATION = "https://purl.imsglobal.org/spec/lti-platform-configuration"
CANVAS_PRIVACY_LEVEL = "https://canvas.instructure.com/lti/privacy_level"
CANVAS_DISPLAY_TYPE = "https://canvas.instructure.com/lti/display_type"
PLACEMENT = "course_navigation"
MESSAGE_TYPE = "LtiResourceLinkRequest"
_PRODUCT_FAMILY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class RegistrationRejected(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@attr.s(frozen=True, auto_attribs=True)
class ToolUrls:
    login: str
    launch: str
    jwks: str
    register: str
    domain: str


def tool_urls(public_baseurl: str) -> ToolUrls:
    base = public_baseurl if public_baseurl.endswith("/") else public_baseurl + "/"
    prefix = base + PATH_PREFIX
    return ToolUrls(
        login=prefix + "login",
        launch=prefix + "launch",
        jwks=prefix + "jwks",
        register=prefix + "register",
        domain=urlsplit(base).netloc,
    )


def registration_request(urls: ToolUrls) -> Dict[str, Any]:
    """The client registration the tool posts to the platform."""
    return {
        "application_type": "web",
        "response_types": ["id_token"],
        "grant_types": ["implicit", "client_credentials"],
        "initiate_login_uri": urls.login,
        "redirect_uris": [urls.launch],
        "client_name": TOOL_NAME,
        "jwks_uri": urls.jwks,
        "token_endpoint_auth_method": "private_key_jwt",
        "scope": NRPS_SCOPE,
        TOOL_CONFIGURATION: {
            "domain": urls.domain,
            "target_link_uri": urls.launch,
            "claims": ["iss", "sub", "name", "given_name", "family_name", "email"],
            "messages": [
                {
                    "type": MESSAGE_TYPE,
                    "target_link_uri": urls.launch,
                    "label": TOOL_NAME,
                    "placements": [PLACEMENT],
                    CANVAS_DISPLAY_TYPE: "new_window",
                }
            ],
            CANVAS_PRIVACY_LEVEL: "public",
        },
    }


@attr.s(frozen=True, auto_attribs=True)
class PlatformConfiguration:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    registration_endpoint: str
    product_family: Optional[str]


def check_openid_configuration(config_url: str, doc: Any) -> PlatformConfiguration:
    """Validate a platform's OpenID configuration fetched from `config_url`."""
    if https_url(config_url) is None:
        raise RegistrationRejected("bad_configuration_url")
    if not isinstance(doc, dict):
        raise RegistrationRejected("bad_configuration")
    fields = {}
    for name in (
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "jwks_uri",
        "registration_endpoint",
    ):
        value = https_url(doc.get(name))
        if value is None:
            raise RegistrationRejected(f"bad_{name}")
        fields[name] = value
    if host_of(fields["issuer"]) != host_of(config_url):
        raise RegistrationRejected("issuer_host_mismatch")
    # The registration token is a bearer credential: it only goes back to the
    # host that served the configuration, never to a host the document names.
    if host_of(fields["registration_endpoint"]) != host_of(config_url):
        raise RegistrationRejected("registration_endpoint_host_mismatch")
    algorithms = doc.get("id_token_signing_alg_values_supported")
    if not isinstance(algorithms, list) or "RS256" not in algorithms:
        raise RegistrationRejected("rs256_unsupported")
    platform = doc.get(PLATFORM_CONFIGURATION)
    if not isinstance(platform, dict):
        raise RegistrationRejected("not_an_lti_platform")
    family = platform.get("product_family_code")
    return PlatformConfiguration(
        # Platform-controlled and logged: kept only when it is a plain code.
        product_family=(
            family
            if isinstance(family, str) and _PRODUCT_FAMILY.match(family)
            else None
        ),
        **fields,
    )


def registered_client(response: Any) -> tuple[str, str]:
    """(client_id, deployment_id) from the platform's registration response.

    The deployment id is required: approval adds none, so a platform
    registered without one could never launch.
    """
    if not isinstance(response, dict):
        raise RegistrationRejected("bad_registration_response")
    client_id = response.get("client_id")
    if not isinstance(client_id, str) or not 0 < len(client_id) <= 255:
        raise RegistrationRejected("bad_registration_response")
    tool = response.get(TOOL_CONFIGURATION)
    deployment_id = tool.get("deployment_id") if isinstance(tool, dict) else None
    if not isinstance(deployment_id, str) or not 0 < len(deployment_id) <= 255:
        raise RegistrationRejected("missing_deployment_id")
    return client_id, deployment_id
