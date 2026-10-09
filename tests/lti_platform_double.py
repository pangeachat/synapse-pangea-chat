"""A stand-in LTI 1.3 platform for the LTI tests.

It holds a platform signing key, mints launch id_tokens, and can serve an
OpenID configuration, a Dynamic Registration endpoint and a JWKS over HTTPS
(its own throwaway CA), so the E2E test exercises the module's real outbound
fetches instead of a mocked client.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import os
import ssl
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from jwt.utils import to_base64url_uint

LTI = "https://purl.imsglobal.org/spec/lti/claim/"
NRPS_CLAIM = "https://purl.imsglobal.org/spec/lti-nrps/claim/namesroleservice"
LEARNER = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
INSTRUCTOR = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"


def new_rsa_key(bits: int = 2048) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def private_pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def public_pem(key: rsa.RSAPrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


def public_jwk(key: rsa.RSAPrivateKey, kid: str) -> Dict[str, str]:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "alg": "RS256",
        "use": "sig",
        "n": to_base64url_uint(numbers.n).decode(),
        "e": to_base64url_uint(numbers.e).decode(),
    }


class PlatformDouble:
    """Signs launches the way Canvas does: RS256, a kid header, LTI claims."""

    def __init__(
        self,
        issuer: str,
        client_id: str = "pangea-client-1",
        deployment_id: str = "deployment-1",
        kid: str = "platform-key-1",
        key: Optional[rsa.RSAPrivateKey] = None,
    ):
        self.issuer = issuer
        self.client_id = client_id
        self.deployment_id = deployment_id
        self.kid = kid
        self.key = key or new_rsa_key()
        self.nrps_url = issuer + "/nrps"

    def jwks(self) -> Dict[str, Any]:
        return {"keys": [public_jwk(self.key, self.kid)]}

    def claims(
        self,
        *,
        nonce: str,
        launch_url: str,
        roles: Optional[List[str]] = None,
        sub: str = "canvas-user-42",
        email: str = "student.private@school.example",
        context_id: str = "canvas-course-7",
    ) -> Dict[str, Any]:
        now = int(time.time())
        return {
            "iss": self.issuer,
            "aud": self.client_id,
            "sub": sub,
            "exp": now + 300,
            "iat": now,
            "nonce": nonce,
            "email": email,
            "name": "Student Private",
            LTI + "message_type": "LtiResourceLinkRequest",
            LTI + "version": "1.3.0",
            LTI + "deployment_id": self.deployment_id,
            LTI + "target_link_uri": launch_url,
            LTI + "resource_link": {"id": "resource-link-1"},
            LTI + "roles": [LEARNER] if roles is None else roles,
            LTI + "context": {"id": context_id, "title": "Spanish 1"},
            # Canvas adds the NRPS service to every launch once the tool has
            # the contextmembership.readonly scope.
            NRPS_CLAIM: {
                "context_memberships_url": self.nrps_url,
                "service_versions": ["2.0"],
            },
        }

    def sign(
        self,
        claims: Dict[str, Any],
        *,
        key: Optional[rsa.RSAPrivateKey] = None,
        headers: Optional[Dict[str, Any]] = None,
        algorithm: str = "RS256",
    ) -> str:
        header = {"kid": self.kid}
        if headers is not None:
            header = headers
        return jwt.encode(claims, key or self.key, algorithm=algorithm, headers=header)


def _write_test_ca(directory: str) -> tuple[str, str, str]:
    """A CA and a localhost leaf it signed; returns (ca, cert, key) paths."""
    ca_key = new_rsa_key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "LTI test CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = new_rsa_key()
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    paths = []
    for name, data in (
        ("ca.pem", ca_cert.public_bytes(serialization.Encoding.PEM)),
        ("leaf.pem", leaf_cert.public_bytes(serialization.Encoding.PEM)),
        ("leaf.key", private_pem(leaf_key).encode()),
    ):
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(data)
        paths.append(path)
    return paths[0], paths[1], paths[2]


class HttpsPlatformServer:
    """Serves the platform's OpenID configuration, registration and JWKS."""

    def __init__(self, registration_token: str = "reg-token-1"):
        self._dir = tempfile.mkdtemp(prefix="lti-platform-")
        self.ca_path, cert_path, key_path = _write_test_ca(self._dir)
        self.registration_token = registration_token
        self.registrations: List[Dict[str, Any]] = []
        self.jwks_requests = 0
        self.issuer_override: Optional[str] = None
        self.registration_endpoint_override: Optional[str] = None
        self.client_id_override: Optional[str] = None
        self.omit_deployment_id = False
        # Every Authorization header the registration endpoint received.
        self.tokens_seen: List[str] = []
        # NRPS: the OAuth2 token endpoint and a paged membership container.
        self.access_token = "nrps-access-token-e2e-5b7e"
        self.token_requests: List[Dict[str, str]] = []
        self.nrps_requests: List[Dict[str, str]] = []
        self.nrps_pages: List[List[Dict[str, Any]]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            # Keep-alive like a real platform: Synapse's pooled client reuses
            # the connection of the configuration fetch for the registration.
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _json(
                self, code: int, body: Any, headers: Optional[Dict[str, str]] = None
            ) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def _nrps(self) -> None:
                server.nrps_requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization") or "",
                        "accept": self.headers.get("Accept") or "",
                    }
                )
                if self.headers.get("Authorization") != (
                    f"Bearer {server.access_token}"
                ):
                    self._json(401, {})
                    return
                query = parse_qs(urlsplit(self.path).query)
                page = int(query.get("page", ["1"])[0])
                members = server.nrps_pages[page - 1] if server.nrps_pages else []
                headers = {}
                if page < len(server.nrps_pages):
                    headers[
                        "Link"
                    ] = f'<{server.base_url}/nrps?page={page + 1}>; rel="next"'
                self._json(
                    200,
                    {
                        "id": server.base_url + "/nrps",
                        "context": {"id": "canvas-course-7"},
                        "members": members,
                    },
                    headers,
                )

            def do_GET(self) -> None:
                if self.path == "/.well-known/openid-configuration":
                    self._json(200, server.openid_configuration())
                elif self.path == "/redirect":
                    # An open redirect on the platform's host.
                    self.send_response(302)
                    self.send_header("Location", "/.well-known/openid-configuration")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif urlsplit(self.path).path == "/nrps":
                    self._nrps()
                elif self.path == "/jwks":
                    server.jwks_requests += 1
                    self._json(200, server.platform.jwks())
                else:
                    self._json(404, {})

            def do_POST(self) -> None:
                if self.path == "/token":
                    length = int(self.headers.get("Content-Length", "0"))
                    form = parse_qs(self.rfile.read(length).decode("ascii"))
                    server.token_requests.append({k: v[0] for k, v in form.items()})
                    self._json(
                        200,
                        {
                            "access_token": server.access_token,
                            "token_type": "Bearer",
                            "expires_in": 3600,
                        },
                    )
                    return
                if self.path != "/register":
                    self._json(404, {})
                    return
                expected = f"Bearer {server.registration_token}"
                server.tokens_seen.append(self.headers.get("Authorization") or "")
                if self.headers.get("Authorization") != expected:
                    self._json(401, {})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                server.registrations.append(body)
                self._json(
                    200,
                    {
                        "client_id": server.client_id_override
                        or server.platform.client_id,
                        "https://purl.imsglobal.org/spec/lti-tool-configuration": (
                            {}
                            if server.omit_deployment_id
                            else {"deployment_id": server.platform.deployment_id}
                        ),
                    },
                )

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        self._httpd.socket = context.wrap_socket(self._httpd.socket, server_side=True)
        self.port = self._httpd.server_address[1]
        self.base_url = f"https://localhost:{self.port}"
        self.platform = PlatformDouble(issuer=self.base_url)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def openid_configuration(self) -> Dict[str, Any]:
        return {
            "issuer": self.issuer_override or self.base_url,
            "authorization_endpoint": self.base_url + "/authorize",
            "token_endpoint": self.base_url + "/token",
            "jwks_uri": self.base_url + "/jwks",
            "registration_endpoint": self.registration_endpoint_override
            or self.base_url + "/register",
            "id_token_signing_alg_values_supported": ["RS256"],
            "https://purl.imsglobal.org/spec/lti-platform-configuration": {
                "product_family_code": "canvas",
                "version": "test",
            },
        }

    def __enter__(self) -> "HttpsPlatformServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
        for name in os.listdir(self._dir):
            os.remove(os.path.join(self._dir, name))
        os.rmdir(self._dir)
