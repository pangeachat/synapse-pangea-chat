"""The admin mint of an account's unsubscribe link, for mail the engagement runner sends itself."""

from __future__ import annotations

import json
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

from synapse_pangea_chat.notice_delivery import tokens
from synapse_pangea_chat.notice_delivery.common import (
    TOKEN_KIND_UNSUBSCRIBE,
    UNSUBSCRIBE_PATH,
)
from synapse_pangea_chat.notice_delivery.unsubscribe_link import MintUnsubscribeLink

SECRET = b"unit-test-secret"
NOW_MS = 1_700_000_000_000
USER = "@alice:my.domain.name"
ADMIN = "@runner:my.domain.name"


def _config(**overrides):
    config = MagicMock()
    config.notice_token_secret = SECRET.decode()
    config.notice_token_ttl_days = 90
    config.notice_admin_requests_per_minute = 600
    config.notice_admin_burst = 100
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _api(
    *, admin=True, exists=True, mine=True, public_baseurl="https://matrix.example.test/"
):
    api = MagicMock()
    api._hs.get_clock.return_value.time_msec.return_value = NOW_MS
    api._hs.config.server.public_baseurl = public_baseurl
    api._hs.config.key.macaroon_secret_key = b"macaroon"
    requester = MagicMock()
    requester.user.to_string.return_value = ADMIN
    api._hs.get_auth.return_value.get_user_by_req = AsyncMock(return_value=requester)
    api.is_user_admin = AsyncMock(return_value=admin)
    api.check_user_exists = AsyncMock(return_value=USER if exists else None)
    api.is_mine = MagicMock(return_value=mine)
    return api


class TestCanonicalUser(unittest.IsolatedAsyncioTestCase):
    async def test_the_token_names_the_account_as_the_server_spells_it(self):
        # A differently cased localpart finds the same account; the token must name it as stored, or the refusal
        # would land on an id no reader uses.
        api = _api()
        responses = []
        with patch(
            "synapse_pangea_chat.notice_delivery.unsubscribe_link.respond_with_json",
            lambda request, code, payload, send_cors=False: responses.append(
                (code, payload)
            ),
        ):
            await MintUnsubscribeLink(api, _config())._async_render_POST(
                _Request(
                    {"user_id": "@Alice:my.domain.name", "category": "suggestions"}
                )
            )
        code, payload = responses[0]
        self.assertEqual(code, 200)
        token = parse_qs(urlparse(payload["url"]).query)["t"][0]
        self.assertEqual(tokens.verify_token(SECRET, token, now_ms=NOW_MS)["u"], USER)


class _Request:
    def __init__(self, body):
        self.content = BytesIO(
            json.dumps(body).encode() if not isinstance(body, bytes) else body
        )


class TestMintUnsubscribeLink(unittest.IsolatedAsyncioTestCase):
    async def _post(self, api, body, config=None):
        responses = []
        with patch(
            "synapse_pangea_chat.notice_delivery.unsubscribe_link.respond_with_json",
            lambda request, code, payload, send_cors=False: responses.append(
                (code, payload)
            ),
        ):
            await MintUnsubscribeLink(api, config or _config())._async_render_POST(
                _Request(body)
            )
        return responses[0]

    async def test_mints_the_link_the_unsubscribe_page_verifies(self):
        code, payload = await self._post(
            _api(), {"user_id": USER, "category": "suggestions"}
        )
        self.assertEqual(code, 200)
        url = urlparse(payload["url"])
        self.assertEqual(
            f"{url.scheme}://{url.netloc}{url.path}",
            "https://matrix.example.test/" + UNSUBSCRIBE_PATH,
        )
        token = parse_qs(url.query)["t"][0]
        claims = tokens.verify_token(SECRET, token, now_ms=NOW_MS)
        self.assertEqual(
            (claims["k"], claims["u"], claims["c"]),
            (TOKEN_KIND_UNSUBSCRIBE, USER, "suggestions"),
        )
        self.assertEqual(payload["expires_at"], NOW_MS + 90 * 86_400_000)
        self.assertEqual(claims["exp"], payload["expires_at"])

    async def test_an_event_category_that_can_be_refused_is_minted(self):
        code, payload = await self._post(
            _api(), {"user_id": USER, "category": "course_invite"}
        )
        self.assertEqual(code, 200)

    async def test_not_refusable_or_unknown_categories_are_refused(self):
        for category in ("credential", "nonsense", None, 3):
            code, payload = await self._post(
                _api(), {"user_id": USER, "category": category}
            )
            self.assertEqual(code, 400, category)

    async def test_only_an_admin_may_mint(self):
        code, _ = await self._post(
            _api(admin=False), {"user_id": USER, "category": "suggestions"}
        )
        self.assertEqual(code, 403)

    async def test_a_user_this_server_does_not_have_is_404(self):
        self.assertEqual(
            (
                await self._post(
                    _api(exists=False), {"user_id": USER, "category": "suggestions"}
                )
            )[0],
            404,
        )
        self.assertEqual(
            (
                await self._post(
                    _api(mine=False),
                    {"user_id": "@bob:elsewhere.org", "category": "suggestions"},
                )
            )[0],
            404,
        )

    async def test_a_malformed_request_is_400(self):
        for body in (
            b"not json",
            {"category": "suggestions"},
            {"user_id": "alice", "category": "suggestions"},
            [1, 2],
        ):
            self.assertEqual((await self._post(_api(), body))[0], 400, body)

    async def test_no_public_baseurl_or_secret_is_a_500_that_says_why(self):
        code, payload = await self._post(
            _api(public_baseurl=""), {"user_id": USER, "category": "suggestions"}
        )
        self.assertEqual(
            (code, payload["error"]), (500, "public_baseurl is not configured")
        )
        api = _api()
        api._hs.config.key.macaroon_secret_key = None
        code, payload = await self._post(
            api,
            {"user_id": USER, "category": "suggestions"},
            _config(notice_token_secret=""),
        )
        self.assertEqual(
            (code, payload["error"]), (500, "no token secret is configured")
        )

    async def test_minting_records_nothing_and_sends_nothing(self):
        api = _api()
        await self._post(api, {"user_id": USER, "category": "suggestions"})
        api.account_data_manager.put_global.assert_not_called()
        api.create_and_send_event_into_room.assert_not_called()


if __name__ == "__main__":
    unittest.main()
