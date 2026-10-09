"""Unit tests for the LTI 1.3 tool core (synapse_pangea_chat.lti).

Covers the launch validation (LTI 1.3 Core + 1EdTech Security Framework), the
tool key loaded from module config, the published JWKS, the platform JWKS
cache, Dynamic Registration's OpenID configuration check and the registration
artifacts. The wiring (endpoints, store, approval) is covered end to end in
test_lti_e2e.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from cryptography.hazmat.primitives import serialization

from synapse_pangea_chat import PangeaChat
from synapse_pangea_chat.lti import keys as lti_keys
from synapse_pangea_chat.lti.keys import LtiConfigError, parse_lti_config
from synapse_pangea_chat.lti.platform_jwks import PlatformKeyCache
from synapse_pangea_chat.lti.registration import (
    NRPS_SCOPE,
    RegistrationRejected,
    canvas_static_config,
    check_openid_configuration,
    registration_request,
    tool_urls,
)
from synapse_pangea_chat.lti.store import Platform
from synapse_pangea_chat.lti.validation import (
    LaunchRejected,
    read_header,
    require_connect_role,
    role_path,
    verify_launch,
)

from .lti_platform_double import (
    INSTRUCTOR,
    LEARNER,
    LTI,
    PlatformDouble,
    new_rsa_key,
    private_pem,
    public_jwk,
    public_pem,
)

TOOL_KEY = new_rsa_key()
OTHER_KEY = new_rsa_key()
ISSUER = "https://canvas.school.example"
URLS = tool_urls("https://matrix.pangea.example/")
LAUNCH_URL = URLS.launch
_BASE_CONFIG = {"cms_base_url": "http://127.0.0.1:9", "cms_service_api_key": "k"}


def _platform(state: str = "approved") -> Platform:
    return Platform(
        platform_id="plat-1",
        issuer=ISSUER,
        client_id="pangea-client-1",
        auth_login_url=ISSUER + "/api/lti/authorize_redirect",
        token_url=ISSUER + "/login/oauth2/token",
        jwks_uri=ISSUER + "/api/lti/security/jwks",
        state=state,
    )


def _pyjwk(double: PlatformDouble):
    import jwt

    return jwt.PyJWK(public_jwk(double.key, double.kid), "RS256")


class LaunchValidationTests(unittest.TestCase):
    """`launch rejects bad sig / iss / aud / azp / exp / bad or missing iat /
    missing or mismatched state / reused nonce / unknown deployment / wrong
    message_type / wrong or missing LTI version` (INV-8). State and nonce
    single use are the store's job and are proven in the E2E test; here the
    nonce must equal the one bound to the state."""

    def setUp(self) -> None:
        self.double = PlatformDouble(issuer=ISSUER)
        self.platform = _platform()
        self.nonce = "nonce-abc"

    def _verify(
        self,
        token: str,
        *,
        nonce: Optional[str] = None,
        deployments: frozenset = frozenset({"deployment-1"}),
    ):
        return verify_launch(
            token,
            platform=self.platform,
            key=_pyjwk(self.double),
            expected_nonce=self.nonce if nonce is None else nonce,
            known_deployments=deployments,
            launch_url=LAUNCH_URL,
        )

    def _claims(self, **overrides: Any) -> Dict[str, Any]:
        claims = self.double.claims(nonce=self.nonce, launch_url=LAUNCH_URL)
        for key, value in overrides.items():
            if value is _DROP:
                claims.pop(key, None)
            else:
                claims[key] = value
        return claims

    def assertRejected(self, token: str, code: str, **kwargs: Any) -> None:
        with self.assertRaises(LaunchRejected) as caught:
            self._verify(token, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_valid_launch_is_accepted(self):
        launch = self._verify(self.double.sign(self._claims()))
        self.assertEqual(launch.platform_id, "plat-1")
        self.assertEqual(launch.sub, "canvas-user-42")
        self.assertEqual(launch.deployment_id, "deployment-1")
        self.assertEqual(launch.context_id, "canvas-course-7")
        self.assertEqual(launch.path, "learner")

    def test_bad_signature(self):
        token = self.double.sign(self._claims(), key=OTHER_KEY)
        self.assertRejected(token, "bad_signature")

    def test_tampered_payload(self):
        token = self.double.sign(self._claims())
        header, payload, signature = token.split(".")
        import base64

        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        claims["sub"] = "someone-else"
        forged = (
            base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        )
        self.assertRejected(f"{header}.{forged}.{signature}", "bad_signature")

    def test_wrong_issuer(self):
        token = self.double.sign(self._claims(iss="https://evil.example"))
        self.assertRejected(token, "bad_issuer")

    def test_missing_issuer(self):
        token = self.double.sign(self._claims(iss=_DROP))
        self.assertRejected(token, "missing_claim")

    def test_wrong_audience(self):
        token = self.double.sign(self._claims(aud="another-tool"))
        self.assertRejected(token, "bad_audience")

    def test_audience_list_needs_azp(self):
        token = self.double.sign(self._claims(aud=["pangea-client-1", "other-tool"]))
        self.assertRejected(token, "bad_azp")

    def test_audience_list_with_wrong_azp(self):
        token = self.double.sign(
            self._claims(aud=["pangea-client-1", "other-tool"], azp="other-tool")
        )
        self.assertRejected(token, "bad_azp")

    def test_audience_list_with_matching_azp_is_accepted(self):
        token = self.double.sign(
            self._claims(aud=["pangea-client-1", "other-tool"], azp="pangea-client-1")
        )
        self.assertEqual(self._verify(token).sub, "canvas-user-42")

    def test_single_audience_with_wrong_azp(self):
        token = self.double.sign(self._claims(azp="other-tool"))
        self.assertRejected(token, "bad_azp")

    def test_expired(self):
        now = int(time.time())
        token = self.double.sign(self._claims(exp=now - 3600, iat=now - 7200))
        self.assertRejected(token, "expired")

    def test_missing_exp(self):
        token = self.double.sign(self._claims(exp=_DROP))
        self.assertRejected(token, "missing_claim")

    def test_missing_iat(self):
        token = self.double.sign(self._claims(iat=_DROP))
        self.assertRejected(token, "missing_claim")

    def test_bad_iat(self):
        token = self.double.sign(self._claims(iat="yesterday"))
        self.assertRejected(token, "bad_iat")

    def test_iat_in_future(self):
        token = self.double.sign(self._claims(iat=int(time.time()) + 3600))
        self.assertRejected(token, "bad_iat")

    def test_nonce_mismatch(self):
        token = self.double.sign(self._claims(nonce="a-different-nonce"))
        self.assertRejected(token, "bad_nonce")

    def test_missing_nonce(self):
        token = self.double.sign(self._claims(nonce=_DROP))
        self.assertRejected(token, "missing_claim")

    def test_unknown_deployment(self):
        token = self.double.sign(self._claims(**{LTI + "deployment_id": "dep-x"}))
        self.assertRejected(token, "unknown_deployment")

    def test_no_known_deployment(self):
        token = self.double.sign(self._claims())
        self.assertRejected(token, "unknown_deployment", deployments=frozenset())

    def test_missing_deployment(self):
        token = self.double.sign(self._claims(**{LTI + "deployment_id": _DROP}))
        self.assertRejected(token, "unknown_deployment")

    def test_wrong_message_type(self):
        token = self.double.sign(
            self._claims(**{LTI + "message_type": "LtiDeepLinkingRequest"})
        )
        self.assertRejected(token, "bad_message_type")

    def test_missing_message_type(self):
        token = self.double.sign(self._claims(**{LTI + "message_type": _DROP}))
        self.assertRejected(token, "bad_message_type")

    def test_wrong_version(self):
        token = self.double.sign(self._claims(**{LTI + "version": "1.1"}))
        self.assertRejected(token, "bad_version")

    def test_missing_version(self):
        token = self.double.sign(self._claims(**{LTI + "version": _DROP}))
        self.assertRejected(token, "bad_version")

    def test_wrong_target_link_uri(self):
        token = self.double.sign(
            self._claims(**{LTI + "target_link_uri": "https://evil.example/launch"})
        )
        self.assertRejected(token, "bad_target_link_uri")

    def test_missing_resource_link(self):
        token = self.double.sign(self._claims(**{LTI + "resource_link": _DROP}))
        self.assertRejected(token, "bad_resource_link")

    def test_missing_roles_claim(self):
        token = self.double.sign(self._claims(**{LTI + "roles": _DROP}))
        self.assertRejected(token, "bad_roles")

    def test_missing_sub(self):
        token = self.double.sign(self._claims(sub=_DROP))
        self.assertRejected(token, "missing_claim")

    def test_unsigned_token_rejected(self):
        token = self.double.sign(self._claims(), headers={"kid": self.double.kid})
        header, payload, _ = token.split(".")
        import base64

        none_header = (
            base64.urlsafe_b64encode(
                json.dumps({"alg": "none", "kid": self.double.kid}).encode()
            )
            .rstrip(b"=")
            .decode()
        )
        with self.assertRaises(LaunchRejected) as caught:
            read_header(f"{none_header}.{payload}.")
        self.assertEqual(caught.exception.code, "bad_algorithm")

    def test_hs256_token_rejected_at_header(self):
        token = self.double.sign(self._claims(), key=None)
        header, payload, signature = token.split(".")
        import base64

        hs_header = (
            base64.urlsafe_b64encode(
                json.dumps({"alg": "HS256", "kid": self.double.kid}).encode()
            )
            .rstrip(b"=")
            .decode()
        )
        with self.assertRaises(LaunchRejected) as caught:
            read_header(f"{hs_header}.{payload}.{signature}")
        self.assertEqual(caught.exception.code, "bad_algorithm")

    def test_header_without_kid_rejected(self):
        token = self.double.sign(self._claims(), headers={})
        with self.assertRaises(LaunchRejected) as caught:
            read_header(token)
        self.assertEqual(caught.exception.code, "missing_kid")

    def test_header_returns_kid(self):
        self.assertEqual(read_header(self.double.sign(self._claims())), self.double.kid)

    def test_garbage_token_rejected(self):
        with self.assertRaises(LaunchRejected) as caught:
            read_header("not-a-jwt")
        self.assertEqual(caught.exception.code, "malformed_token")

    def test_pending_platform_launch_refused(self):
        """`pending platform launch refused` (INV-8): even a perfectly signed
        launch from a registered but unapproved platform is refused."""
        self.platform = _platform(state="pending")
        self.assertRejected(self.double.sign(self._claims()), "platform_not_approved")

    def test_approved_platform_launch_accepted(self):
        """`approved platform launch accepted`."""
        self.platform = _platform(state="approved")
        self.assertEqual(
            self._verify(self.double.sign(self._claims())).sub, "canvas-user-42"
        )


class _Drop:
    pass


_DROP = _Drop()


class RolePathTests(unittest.TestCase):
    """`a Learner-role launch can neither start nor complete a course connect`."""

    def test_learner_takes_student_path(self):
        self.assertEqual(role_path([LEARNER]), "learner")

    def test_instructor_and_administrator_take_connect_path(self):
        for roles in (
            [INSTRUCTOR],
            ["http://purl.imsglobal.org/vocab/lis/v2/membership#Administrator"],
            ["http://purl.imsglobal.org/vocab/lis/v2/institution/person#Administrator"],
            [LEARNER, INSTRUCTOR],
        ):
            self.assertEqual(role_path(roles), "instructor", roles)

    def test_other_roles_take_student_path(self):
        for roles in (
            [],
            ["Instructor"],  # a simple name, not the LIS URI
            ["http://purl.imsglobal.org/vocab/lis/v2/membership#Mentor"],
            ["http://purl.imsglobal.org/vocab/lis/v2/membership#ContentDeveloper"],
            [
                "http://purl.imsglobal.org/vocab/lis/v2/membership/Instructor#TeachingAssistant"
            ],
            ["http://purl.imsglobal.org/vocab/lis/v2/system/person#User"],
            [INSTRUCTOR + "x"],
        ):
            self.assertEqual(role_path(roles), "learner", roles)

    def test_learner_launch_cannot_start_or_complete_a_connect(self):
        double = PlatformDouble(issuer=ISSUER)
        token = double.sign(double.claims(nonce="n", launch_url=LAUNCH_URL))
        launch = verify_launch(
            token,
            platform=_platform(),
            key=_pyjwk(double),
            expected_nonce="n",
            known_deployments=frozenset({"deployment-1"}),
            launch_url=LAUNCH_URL,
        )
        with self.assertRaises(LaunchRejected) as caught:
            require_connect_role(launch)
        self.assertEqual(caught.exception.code, "not_instructor")

    def test_instructor_launch_may_connect(self):
        double = PlatformDouble(issuer=ISSUER)
        token = double.sign(
            double.claims(nonce="n", launch_url=LAUNCH_URL, roles=[INSTRUCTOR])
        )
        launch = verify_launch(
            token,
            platform=_platform(),
            key=_pyjwk(double),
            expected_nonce="n",
            known_deployments=frozenset({"deployment-1"}),
            launch_url=LAUNCH_URL,
        )
        self.assertIs(require_connect_role(launch), launch)


class ToolKeyConfigTests(unittest.TestCase):
    """`module refuses to start the LTI endpoints when the Secrets
    Manager-backed private key is absent or malformed, and never generates or
    accepts an inline fallback key` (INV-8) and `jwks publishes only the public
    half of the configured key`."""

    def test_valid_key_is_loaded(self):
        settings = parse_lti_config({"private_key_pem": private_pem(TOOL_KEY)})
        self.assertEqual(
            settings.signing_key.public_jwk["n"],
            public_jwk(TOOL_KEY, "x")["n"],
        )

    def test_absent_key_refused(self):
        for raw in ({}, {"private_key_pem": None}, {"private_key_pem": ""}):
            with self.assertRaises(LtiConfigError, msg=repr(raw)):
                parse_lti_config(raw)

    def test_non_mapping_refused(self):
        for raw in ("pem", ["pem"], True):
            with self.assertRaises(LtiConfigError):
                parse_lti_config(raw)

    def test_malformed_key_refused(self):
        for pem in (
            "not a key",
            "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
            public_pem(TOOL_KEY),
            12345,
        ):
            with self.assertRaises(LtiConfigError):
                parse_lti_config({"private_key_pem": pem})

    def test_encrypted_key_refused(self):
        pem = TOOL_KEY.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"passphrase"),
        ).decode()
        with self.assertRaises(LtiConfigError):
            parse_lti_config({"private_key_pem": pem})

    def test_short_rsa_key_refused(self):
        with self.assertRaises(LtiConfigError):
            parse_lti_config({"private_key_pem": private_pem(new_rsa_key(1024))})

    def test_non_rsa_key_refused(self):
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        with self.assertRaises(LtiConfigError):
            parse_lti_config({"private_key_pem": pem})

    def test_unknown_key_refused(self):
        """A typo such as `private_key` must not leave LTI silently keyless,
        and there is no option that asks the module to make a key."""
        for extra in ({"private_key": "x"}, {"generate_key": True}):
            raw = {"private_key_pem": private_pem(TOOL_KEY), **extra}
            with self.assertRaises(LtiConfigError):
                parse_lti_config(raw)

    def test_error_names_no_key_material(self):
        pem = private_pem(new_rsa_key(1024))
        with self.assertRaises(LtiConfigError) as caught:
            parse_lti_config({"private_key_pem": pem})
        body = pem.splitlines()[1]
        self.assertNotIn(body, str(caught.exception))

    def test_never_generates_a_key(self):
        with mock.patch(
            "cryptography.hazmat.primitives.asymmetric.rsa.generate_private_key"
        ) as generate:
            for raw in ({}, {"private_key_pem": "bad"}):
                with self.assertRaises(LtiConfigError):
                    parse_lti_config(raw)
            parse_lti_config({"private_key_pem": private_pem(TOOL_KEY)})
        generate.assert_not_called()

    def test_jwks_publishes_only_public_half(self):
        settings = parse_lti_config({"private_key_pem": private_pem(TOOL_KEY)})
        jwks = settings.public_jwks()
        self.assertEqual(len(jwks["keys"]), 1)
        key = jwks["keys"][0]
        self.assertEqual(set(key), {"kty", "kid", "alg", "use", "n", "e"})
        self.assertEqual(key["kty"], "RSA")
        self.assertEqual(key["alg"], "RS256")
        self.assertEqual(key["use"], "sig")
        self.assertEqual(key["kid"], settings.signing_key.kid)
        numbers = TOOL_KEY.public_key().public_numbers()
        self.assertEqual(key["n"], public_jwk(TOOL_KEY, "x")["n"])
        self.assertEqual(key["e"], public_jwk(TOOL_KEY, "x")["e"])
        self.assertEqual(numbers.e, 65537)
        serialized = json.dumps(jwks)
        private_d = TOOL_KEY.private_numbers().d
        from jwt.utils import to_base64url_uint

        self.assertNotIn(to_base64url_uint(private_d).decode(), serialized)

    def test_kid_is_rfc7638_thumbprint_and_changes_with_key(self):
        a = parse_lti_config({"private_key_pem": private_pem(TOOL_KEY)})
        b = parse_lti_config({"private_key_pem": private_pem(OTHER_KEY)})
        self.assertNotEqual(a.signing_key.kid, b.signing_key.kid)
        self.assertEqual(
            a.signing_key.kid,
            lti_keys.thumbprint(public_jwk(TOOL_KEY, "ignored")),
        )

    def test_previous_public_keys_are_published_for_rotation(self):
        settings = parse_lti_config(
            {
                "private_key_pem": private_pem(TOOL_KEY),
                "previous_public_keys_pem": [public_pem(OTHER_KEY)],
            }
        )
        kids = [k["kid"] for k in settings.public_jwks()["keys"]]
        self.assertEqual(len(kids), 2)
        self.assertEqual(kids[0], settings.signing_key.kid)
        for key in settings.public_jwks()["keys"]:
            self.assertEqual(set(key), {"kty", "kid", "alg", "use", "n", "e"})

    def test_previous_key_must_be_public(self):
        for previous in ([private_pem(OTHER_KEY)], ["junk"], "not-a-list", [7]):
            with self.assertRaises(LtiConfigError):
                parse_lti_config(
                    {
                        "private_key_pem": private_pem(TOOL_KEY),
                        "previous_public_keys_pem": previous,
                    }
                )

    def test_repr_never_shows_private_key(self):
        settings = parse_lti_config({"private_key_pem": private_pem(TOOL_KEY)})
        text = repr(settings) + repr(settings.signing_key)
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn(str(TOOL_KEY.private_numbers().d), text)

    def test_parse_config_without_lti_block_leaves_lti_off(self):
        config = PangeaChat.parse_config(dict(_BASE_CONFIG))
        self.assertIsNone(config.lti)
        self.assertIsNone(config.lti_config_error)

    def test_parse_config_with_bad_key_leaves_lti_off_and_says_why(self):
        config = PangeaChat.parse_config(
            {**_BASE_CONFIG, "lti": {"private_key_pem": "nope"}}
        )
        self.assertIsNone(config.lti)
        self.assertIsNotNone(config.lti_config_error)
        self.assertNotIn("nope", config.lti_config_error or "")

    def test_parse_config_with_absent_key_leaves_lti_off(self):
        config = PangeaChat.parse_config({**_BASE_CONFIG, "lti": {}})
        self.assertIsNone(config.lti)
        self.assertIsNotNone(config.lti_config_error)

    def test_parse_config_with_good_key(self):
        config = PangeaChat.parse_config(
            {**_BASE_CONFIG, "lti": {"private_key_pem": private_pem(TOOL_KEY)}}
        )
        self.assertIsNotNone(config.lti)
        self.assertIsNone(config.lti_config_error)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class PlatformKeyCacheTests(unittest.TestCase):
    """Platform JWKS is fetched once, cached by kid, and an unknown kid
    refetches at most once per interval."""

    def setUp(self) -> None:
        self.double = PlatformDouble(issuer=ISSUER, kid="k1")
        self.fetches: List[str] = []
        self.document: Any = self.double.jwks()
        self.clock = _Clock()

        async def fetch(url: str) -> Any:
            self.fetches.append(url)
            if isinstance(self.document, Exception):
                raise self.document
            return self.document

        self.cache = PlatformKeyCache(
            fetch, self.clock, max_age_seconds=3600, min_refetch_seconds=60
        )

    def get(self, kid: str):
        return asyncio.run(self.cache.get_key("https://p/jwks", kid))

    def test_cached_by_kid(self):
        self.assertIsNotNone(self.get("k1"))
        self.assertIsNotNone(self.get("k1"))
        self.assertEqual(len(self.fetches), 1)

    def test_unknown_kid_refetch_is_rate_limited(self):
        self.get("k1")
        self.assertIsNone(self.get("k2"))
        self.assertIsNone(self.get("k2"))
        self.assertEqual(len(self.fetches), 1)
        self.clock.now += 61
        self.assertIsNone(self.get("k2"))
        self.assertEqual(len(self.fetches), 2)

    def test_rotated_key_found_after_interval(self):
        self.get("k1")
        rotated = PlatformDouble(issuer=ISSUER, kid="k2")
        self.document = rotated.jwks()
        self.assertIsNone(self.get("k2"))  # within the interval: no refetch
        self.clock.now += 61
        self.assertIsNotNone(self.get("k2"))

    def test_cache_expires(self):
        self.get("k1")
        self.clock.now += 3601
        self.get("k1")
        self.assertEqual(len(self.fetches), 2)

    def test_fetch_failure_is_not_retried_inside_interval(self):
        self.document = RuntimeError("down")
        self.assertIsNone(self.get("k1"))
        self.assertIsNone(self.get("k1"))
        self.assertEqual(len(self.fetches), 1)

    def test_unusable_keys_are_ignored(self):
        good = public_jwk(self.double.key, "k1")
        small = public_jwk(new_rsa_key(1024), "small")
        self.document = {
            "keys": [
                dict(good, kid="hs", kty="oct", k="c2VjcmV0"),
                dict(good, kid="enc", use="enc"),
                dict(good, kid="es", alg="ES256"),
                small,
                "junk",
                dict(good, kid=None),
                good,
            ]
        }
        self.assertIsNotNone(self.get("k1"))
        for kid in ("hs", "enc", "es", "small"):
            self.assertIsNone(self.get(kid), kid)

    def test_not_a_jwks(self):
        self.document = ["keys"]
        self.assertIsNone(self.get("k1"))


class OpenIdConfigurationCheckTests(unittest.TestCase):
    """`registration checks openid config host`."""

    URL = "https://canvas.school.example/api/lti/security/openid-configuration"

    def _doc(self, **overrides: Any) -> Dict[str, Any]:
        doc = {
            "issuer": "https://canvas.school.example",
            "authorization_endpoint": "https://canvas.school.example/authorize",
            "token_endpoint": "https://canvas.school.example/token",
            "jwks_uri": "https://canvas.school.example/jwks",
            "registration_endpoint": "https://canvas.school.example/register",
            "id_token_signing_alg_values_supported": ["RS256"],
            "https://purl.imsglobal.org/spec/lti-platform-configuration": {
                "product_family_code": "canvas"
            },
        }
        doc.update(overrides)
        return doc

    def assertRejected(self, url: str, doc: Any, code: str) -> None:
        with self.assertRaises(RegistrationRejected) as caught:
            check_openid_configuration(url, doc)
        self.assertEqual(caught.exception.code, code)

    def test_matching_host_accepted(self):
        result = check_openid_configuration(self.URL, self._doc())
        self.assertEqual(result.issuer, "https://canvas.school.example")
        self.assertEqual(result.jwks_uri, "https://canvas.school.example/jwks")

    def test_issuer_host_must_match_configuration_host(self):
        self.assertRejected(
            self.URL, self._doc(issuer="https://evil.example"), "issuer_host_mismatch"
        )

    def test_issuer_lookalike_host_rejected(self):
        self.assertRejected(
            self.URL,
            self._doc(issuer="https://canvas.school.example.evil.example"),
            "issuer_host_mismatch",
        )

    def test_issuer_port_must_match(self):
        self.assertRejected(
            self.URL,
            self._doc(issuer="https://canvas.school.example:8443"),
            "issuer_host_mismatch",
        )

    def test_configuration_url_must_be_https(self):
        self.assertRejected(
            "http://canvas.school.example/openid", self._doc(), "bad_configuration_url"
        )

    def test_configuration_url_with_credentials_rejected(self):
        self.assertRejected(
            "https://user:pw@canvas.school.example/openid",
            self._doc(),
            "bad_configuration_url",
        )

    def test_endpoints_must_be_https(self):
        for field in (
            "issuer",
            "authorization_endpoint",
            "token_endpoint",
            "jwks_uri",
            "registration_endpoint",
        ):
            doc = self._doc(**{field: "http://canvas.school.example/x"})
            with self.assertRaises(RegistrationRejected, msg=field):
                check_openid_configuration(self.URL, doc)

    def test_rs256_must_be_supported(self):
        self.assertRejected(
            self.URL,
            self._doc(id_token_signing_alg_values_supported=["HS256"]),
            "rs256_unsupported",
        )

    def test_platform_configuration_required(self):
        doc = self._doc()
        del doc["https://purl.imsglobal.org/spec/lti-platform-configuration"]
        self.assertRejected(self.URL, doc, "not_an_lti_platform")

    def test_not_an_object(self):
        self.assertRejected(self.URL, ["x"], "bad_configuration")


class RegistrationArtifactTests(unittest.TestCase):
    """`dynamic registration response and the static JSON config both declare
    course_navigation, a new-window launch, NRPS contextmembership.readonly and
    privacy_level public`."""

    def test_dynamic_registration_request(self):
        body = registration_request(URLS)
        self.assertEqual(body["initiate_login_uri"], URLS.login)
        self.assertEqual(body["redirect_uris"], [URLS.launch])
        self.assertEqual(body["jwks_uri"], URLS.jwks)
        self.assertEqual(body["token_endpoint_auth_method"], "private_key_jwt")
        self.assertEqual(body["response_types"], ["id_token"])
        self.assertIn("client_credentials", body["grant_types"])
        self.assertIn("implicit", body["grant_types"])
        self.assertEqual(body["scope"].split(), [NRPS_SCOPE])
        self.assertEqual(
            NRPS_SCOPE,
            "https://purl.imsglobal.org/spec/lti-nrps/scope/contextmembership.readonly",
        )
        tool = body["https://purl.imsglobal.org/spec/lti-tool-configuration"]
        self.assertEqual(tool["domain"], "matrix.pangea.example")
        self.assertEqual(tool["target_link_uri"], URLS.launch)
        self.assertEqual(
            tool["https://canvas.instructure.com/lti/privacy_level"], "public"
        )
        self.assertIn("email", tool["claims"])
        [message] = tool["messages"]
        self.assertEqual(message["type"], "LtiResourceLinkRequest")
        self.assertEqual(message["placements"], ["course_navigation"])
        self.assertEqual(
            message["https://canvas.instructure.com/lti/display_type"], "new_window"
        )

    def test_static_canvas_json_config(self):
        config = canvas_static_config(URLS)
        self.assertEqual(config["oidc_initiation_url"], URLS.login)
        self.assertEqual(config["target_link_uri"], URLS.launch)
        self.assertEqual(config["public_jwk_url"], URLS.jwks)
        self.assertEqual(config["scopes"], [NRPS_SCOPE])
        [extension] = config["extensions"]
        self.assertEqual(extension["privacy_level"], "public")
        [placement] = extension["settings"]["placements"]
        self.assertEqual(placement["placement"], "course_navigation")
        self.assertEqual(placement["message_type"], "LtiResourceLinkRequest")
        self.assertEqual(placement["target_link_uri"], URLS.launch)
        self.assertEqual(placement["windowTarget"], "_blank")

    def test_urls_hang_off_public_baseurl(self):
        urls = tool_urls("https://matrix.pangea.example")
        self.assertEqual(
            urls.launch,
            "https://matrix.pangea.example/_synapse/client/pangea/v1/lti/launch",
        )
        self.assertEqual(urls.domain, "matrix.pangea.example")


class LogRedactionTests(unittest.TestCase):
    """`no key or email in logs` (INV-8): rejection paths log a reason code,
    never the token, its claims or key material."""

    def test_rejections_log_nothing_from_the_token(self):
        double = PlatformDouble(issuer=ISSUER)
        email = "student.private@school.example"
        tokens = [
            double.sign(double.claims(nonce="other", launch_url=LAUNCH_URL)),
            double.sign(double.claims(nonce="n", launch_url=LAUNCH_URL), key=OTHER_KEY),
        ]
        logger = logging.getLogger("synapse.module.synapse_pangea_chat.lti")
        with self.assertLogs(logger, level="DEBUG") as logs:
            logger.debug("start")
            for token in tokens:
                with self.assertRaises(LaunchRejected) as caught:
                    verify_launch(
                        token,
                        platform=_platform(),
                        key=_pyjwk(double),
                        expected_nonce="n",
                        known_deployments=frozenset({"deployment-1"}),
                        launch_url=LAUNCH_URL,
                    )
                self.assertNotIn(email, str(caught.exception))
                self.assertNotIn(token, str(caught.exception))
        text = "\n".join(logs.output)
        self.assertNotIn(email, text)
        for token in tokens:
            self.assertNotIn(token.split(".")[1], text)
        self.assertNotIn("PRIVATE KEY", text)


if __name__ == "__main__":
    unittest.main()
