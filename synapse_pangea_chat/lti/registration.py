"""LTI Dynamic Registration and the static (Canvas JSON) tool configuration.

Dynamic Registration (1EdTech LTI Dynamic Registration 1.0): the platform
opens `/lti/register?openid_configuration=<url>&registration_token=<t>`; the
tool fetches that OpenID configuration, checks that the configuration's
`issuer` is on the same host as the URL it was fetched from (so a document
hosted anywhere cannot claim to be some other platform), posts its client
registration to the platform's `registration_endpoint`, and records the
platform as pending. Only an operator's approval lets it launch.

Both artifacts declare the same thing: the `course_navigation` placement,
opened in a new window (the app's own sign-in storage does not work in an
iframe), the NRPS `contextmembership.readonly` scope, and Canvas
`privacy_level: public` (names and emails).
"""

from __future__ import annotations

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
    config: str
    domain: str


def tool_urls(public_baseurl: str) -> ToolUrls:
    base = public_baseurl if public_baseurl.endswith("/") else public_baseurl + "/"
    prefix = base + PATH_PREFIX
    return ToolUrls(
        login=prefix + "login",
        launch=prefix + "launch",
        jwks=prefix + "jwks",
        register=prefix + "register",
        config=prefix + "config",
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


def canvas_static_config(urls: ToolUrls) -> Dict[str, Any]:
    """The Canvas developer-key JSON, for a manual install."""
    return {
        "title": TOOL_NAME,
        "description": "Language learning chat for your course.",
        "oidc_initiation_url": urls.login,
        "target_link_uri": urls.launch,
        "public_jwk_url": urls.jwks,
        "scopes": [NRPS_SCOPE],
        "extensions": [
            {
                "platform": "canvas.instructure.com",
                "domain": urls.domain,
                "privacy_level": "public",
                "settings": {
                    "placements": [
                        {
                            "placement": PLACEMENT,
                            "message_type": MESSAGE_TYPE,
                            "target_link_uri": urls.launch,
                            "text": TOOL_NAME,
                            "windowTarget": "_blank",
                        }
                    ]
                },
            }
        ],
        "custom_fields": {},
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
    algorithms = doc.get("id_token_signing_alg_values_supported")
    if not isinstance(algorithms, list) or "RS256" not in algorithms:
        raise RegistrationRejected("rs256_unsupported")
    platform = doc.get(PLATFORM_CONFIGURATION)
    if not isinstance(platform, dict):
        raise RegistrationRejected("not_an_lti_platform")
    family = platform.get("product_family_code")
    return PlatformConfiguration(
        product_family=family[:64] if isinstance(family, str) else None,
        **fields,
    )


def registered_client(response: Any) -> tuple[str, Optional[str]]:
    """(client_id, deployment_id or None) from the platform's registration
    response."""
    if not isinstance(response, dict):
        raise RegistrationRejected("bad_registration_response")
    client_id = response.get("client_id")
    if not isinstance(client_id, str) or not 0 < len(client_id) <= 255:
        raise RegistrationRejected("bad_registration_response")
    deployment_id = None
    tool = response.get(TOOL_CONFIGURATION)
    if isinstance(tool, dict):
        value = tool.get("deployment_id")
        if isinstance(value, str) and 0 < len(value) <= 255:
            deployment_id = value
    return client_id, deployment_id
