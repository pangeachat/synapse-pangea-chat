"""Notice destinations: what a request may ask a link to open, the second
signed link, where each kind lands when clicked, and what the open records
(notice-delivery.instructions.md, "Deliver a notice" and "The click record").
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from synapse_pangea_chat import PangeaChat
from synapse_pangea_chat.notice_delivery import tokens
from synapse_pangea_chat.notice_delivery.click import NoticeClick
from synapse_pangea_chat.notice_delivery.deliver import DeliverNotice
from synapse_pangea_chat.notice_delivery.request import Destination, NoticeRequest
from synapse_pangea_chat.notice_delivery.schedule import NoticeSchedule
from tests.test_notice_content import request_body
from tests.test_notice_delivery_unit import (
    NOW_MS,
    SECRET,
    USER,
    _api,
    _config,
    _direct_push,
    _FakeRequest,
)
from tests.test_notice_schedule import SQLPool

HOSTS = ["calendar.app.google", "admin.pangea.chat"]
BOOKING = "https://calendar.app.google/abc123"
COURSE = {"kind": "course", "course_room_id": "!course:x"}


def body_with(destination=None, secondary=None, method="email-only"):
    body = request_body(method)
    del body["activity_id"]
    if destination is not None:
        body["destination"] = destination
    if secondary is not None:
        body["secondary_destination"] = secondary
        body["email"]["html"] += '<a href="{{cta2_url}}">Book</a>'
        body["email"]["text"] += "\nBook {{cta2_url}}"
    return body


def parse(body):
    return NoticeRequest.parse(body, external_link_hosts=HOSTS)


class TestDestinationParsing(unittest.TestCase):
    def test_top_level_ids_without_a_destination_mean_an_activity_or_the_app(self):
        req = NoticeRequest.parse(request_body())
        self.assertEqual(
            req.destination, Destination("activity", activity_id="activity-1")
        )
        self.assertIsNone(req.secondary_destination)
        with_session = {**request_body(), "session_room_id": "!s:x"}
        self.assertEqual(
            NoticeRequest.parse(with_session).destination.compact(),
            {"k": "activity", "a": "activity-1", "s": "!s:x"},
        )
        self.assertEqual(parse(body_with()).destination, Destination("app"))

    def test_each_kind_and_its_compact_form(self):
        cases = {
            "app": ({"kind": "app"}, {"k": "app"}),
            "activity": (
                {"kind": "activity", "activity_id": "act-2", "session_room_id": "!s:x"},
                {"k": "activity", "a": "act-2", "s": "!s:x"},
            ),
            "course": (COURSE, {"k": "course", "c": "!course:x"}),
            "subscription": ({"kind": "subscription"}, {"k": "subscription"}),
            "external": (
                {"kind": "external", "url": BOOKING},
                {"k": "external", "u": BOOKING},
            ),
        }
        for kind, (given, compact) in cases.items():
            with self.subTest(kind=kind):
                req = parse(body_with(given))
                self.assertEqual(req.destination.kind, kind)
                self.assertEqual(req.destination.compact(), compact)

    def test_a_destination_object_wins_over_top_level_ids(self):
        body = {**body_with(COURSE), "activity_id": "activity-1"}
        self.assertEqual(parse(body).destination.kind, "course")

    def test_rejects_malformed_destinations_with_a_reason(self):
        cases = {
            "not an object": "a string",
            "unknown kind": {"kind": "map"},
            "activity without id": {"kind": "activity"},
            "course without room": {"kind": "course"},
            "course with a bad room id": {"kind": "course", "course_room_id": "course"},
            "external without url": {"kind": "external"},
            "external over http": {
                "kind": "external",
                "url": "http://calendar.app.google/x",
            },
            "external to an unlisted host": {
                "kind": "external",
                "url": "https://evil.example/x",
            },
            "external to a host prefix": {
                "kind": "external",
                "url": "https://calendar.app.google.evil.example/x",
            },
            "app with an id": {"kind": "app", "activity_id": "act"},
            "subscription with a url": {"kind": "subscription", "url": BOOKING},
            "course with an activity": {**COURSE, "activity_id": "act"},
        }
        for name, destination in cases.items():
            with self.subTest(name=name), self.assertRaises(ValueError) as error:
                parse(body_with(destination))
            self.assertTrue(str(error.exception), name)
            with self.subTest(name=name, slot="secondary"), self.assertRaises(
                ValueError
            ):
                parse(body_with(secondary=destination))

    def test_the_allowlist_is_the_configured_one(self):
        body = body_with({"kind": "external", "url": "https://other.example/x"})
        with self.assertRaises(ValueError):
            parse(body)
        req = NoticeRequest.parse(body, external_link_hosts=["other.example"])
        self.assertEqual(req.destination.url, "https://other.example/x")
        # The module default applies when the caller passes nothing.
        NoticeRequest.parse(body_with({"kind": "external", "url": BOOKING}))

    def test_a_second_link_needs_its_slot_in_both_bodies_and_only_then(self):
        req = parse(body_with(COURSE, {"kind": "external", "url": BOOKING}))
        self.assertEqual(req.secondary_destination.kind, "external")
        for field in ("html", "text"):
            body = body_with(COURSE, {"kind": "external", "url": BOOKING})
            body["email"][field] = body["email"][field].replace("{{cta2_url}}", "")
            with self.subTest(missing=field), self.assertRaisesRegex(
                ValueError, "cta2_url"
            ):
                parse(body)
        body = body_with(COURSE)
        body["email"]["html"] += "{{cta2_url}}"
        with self.assertRaisesRegex(ValueError, "no secondary_destination"):
            parse(body)

    def test_flat_bodies_accept_a_destination_but_not_a_second_link(self):
        flat = {
            "user_id": USER,
            "category": "activity_nudges",
            "variant": "do_activity",
            "body": "Ready?",
            "notice_event_id": "$notice:x",
            "notice_room_id": "!dm:x",
        }
        self.assertIsNone(
            DeliverNotice._validate({**flat, "destination": COURSE}, HOSTS)
        )
        self.assertIn(
            "course_room_id",
            DeliverNotice._validate({**flat, "destination": {"kind": "course"}}, HOSTS),
        )
        self.assertIn(
            "structured",
            DeliverNotice._validate({**flat, "secondary_destination": COURSE}, HOSTS),
        )


class TestClickResolution(unittest.IsolatedAsyncioTestCase):
    def _token(self, **extra):
        payload = {
            "k": "click",
            "u": USER,
            "e": "$notice:x",
            "r": "!dm:x",
            "v": "do_activity",
        }
        payload.update(extra)
        return tokens.sign_token(SECRET, payload, now_ms=NOW_MS, ttl_ms=10_000)

    async def _click(self, api, token, config=None):
        handler = NoticeClick(api, config or _config())
        redirects = []
        with patch(
            "synapse_pangea_chat.notice_delivery.click.respond_with_redirect",
            new=lambda request, url, *a, **k: redirects.append(url.decode()),
        ):
            await handler._async_render_GET(_FakeRequest(args={b"t": [token.encode()]}))
        [redirect] = redirects
        return redirect

    async def test_each_kind_lands_where_the_client_expects(self):
        cases = {
            "app": ({"k": "app"}, "https://app.example.test/"),
            "activity": (
                {"k": "activity", "a": "act-1", "s": "!s:x"},
                "https://app.example.test/act-1?roomid=%21s%3Ax",
            ),
            "course": (
                {"k": "course", "c": "!course:x"},
                "https://app.example.test/?c=%21course%3Ax&left=course",
            ),
            "subscription": (
                {"k": "subscription"},
                "https://app.example.test/?right=settingspage:subscription",
            ),
            "external": ({"k": "external", "u": BOOKING}, BOOKING),
        }
        for kind, (compact, expected) in cases.items():
            with self.subTest(kind=kind):
                api = _api()
                redirect = await self._click(api, self._token(d=compact, l="cta"))
                self.assertEqual(redirect, expected)
                event = api.create_and_send_event_into_room.await_args.args[0]
                self.assertEqual(event["type"], "p.room.notice.opened")
                self.assertEqual(event["content"]["link"], "cta")
                self.assertEqual(event["content"]["destination_kind"], kind)
                self.assertEqual(event["content"]["notification_event_id"], "$notice:x")

    async def test_the_second_link_records_which_link_was_opened(self):
        api = _api()
        await self._click(
            api, self._token(d={"k": "course", "c": "!course:x"}, l="cta2")
        )
        content = api.create_and_send_event_into_room.await_args.args[0]["content"]
        self.assertEqual(
            (content["link"], content["destination_kind"]), ("cta2", "course")
        )

    async def test_an_external_host_no_longer_allowed_falls_back_home(self):
        api = _api()
        with self.assertLogs(
            "synapse.module.synapse_pangea_chat.notice_delivery.common", level="WARNING"
        ):
            redirect = await self._click(
                api,
                self._token(d={"k": "external", "u": BOOKING}),
                _config(notice_external_link_hosts=["admin.pangea.chat"]),
            )
        self.assertEqual(redirect, "https://app.example.test/")
        api.create_and_send_event_into_room.assert_awaited_once()

    async def test_links_issued_before_destinations_still_resolve(self):
        api = _api()
        redirect = await self._click(api, self._token(a="act-1", s="!s:x"))
        self.assertEqual(redirect, "https://app.example.test/act-1?roomid=%21s%3Ax")
        content = api.create_and_send_event_into_room.await_args.args[0]["content"]
        self.assertEqual(
            (content["link"], content["destination_kind"]), ("cta", "activity")
        )
        redirect = await self._click(_api(), self._token())
        self.assertEqual(redirect, "https://app.example.test/")

    async def test_a_tampered_destination_is_an_invalid_link(self):
        token = self._token(d={"k": "external", "u": BOOKING})
        payload, signature = token.split(".")
        forged = payload[:-2] + "AA." + signature
        self.assertEqual(await self._click(_api(), forged), "https://app.example.test/")


class TestSendEmailLinks(unittest.IsolatedAsyncioTestCase):
    def _handler(self, api):
        return DeliverNotice(api, _config(), _direct_push(0, 1))

    def _click_payload(self, url):
        token = parse_qs(urlsplit(url).query)["t"][0]
        return tokens.verify_token(SECRET, token, now_ms=NOW_MS)

    async def test_both_slots_are_replaced_with_their_own_signed_link(self):
        api = _api(
            threepids=[SimpleNamespace(medium="email", address="a@example.test")]
        )
        body = body_with(
            COURSE, {"kind": "external", "url": "https://calendar.app.google/a?b=1&c=2"}
        )
        req = NoticeRequest.parse(body, external_link_hosts=HOSTS)
        result = await self._handler(api)._send_email(
            body, user_id=USER, category="activity_nudges", req=req
        )
        self.assertEqual(result, {"sent": True, "reason": None})
        kwargs = (
            api._hs.get_send_email_handler.return_value.send_email.await_args.kwargs
        )
        text = kwargs["text"]
        self.assertNotIn("{{cta_url}}", text)
        self.assertNotIn("{{cta2_url}}", text)
        self.assertNotIn("{{cta2_url}}", kwargs["html"])
        links = [
            line.split(" ", 1)[1]
            for line in text.splitlines()
            if line.startswith(("Open ", "Book "))
        ]
        self.assertEqual(len(links), 2)
        primary, secondary = (self._click_payload(link) for link in links)
        self.assertEqual(primary["d"], {"k": "course", "c": "!course:x"})
        self.assertEqual(primary["l"], "cta")
        self.assertEqual(
            secondary["d"],
            {"k": "external", "u": "https://calendar.app.google/a?b=1&c=2"},
        )
        self.assertEqual(secondary["l"], "cta2")
        self.assertEqual(
            (primary["u"], primary["e"], primary["r"]),
            (USER, "$notice:test", "!dm:test"),
        )
        self.assertNotEqual(links[0], links[1])
        # Links are HTML-escaped in the html body, raw in the text body.
        self.assertIn(links[1].replace("&", "&amp;"), kwargs["html"])
        self.assertIn(links[1], text)

    async def test_the_activity_link_shape_is_unchanged_without_destinations(self):
        api = _api(
            threepids=[SimpleNamespace(medium="email", address="a@example.test")]
        )
        body = {**request_body("email-only"), "session_room_id": "!s:x"}
        await self._handler(api)._send_email(
            body,
            user_id=USER,
            category="activity_nudges",
            req=NoticeRequest.parse(body),
        )
        text = api._hs.get_send_email_handler.return_value.send_email.await_args.kwargs[
            "text"
        ]
        [link] = [
            line.split(" ", 1)[1]
            for line in text.splitlines()
            if line.startswith("Open ")
        ]
        payload = self._click_payload(link)
        self.assertEqual((payload["a"], payload["s"]), ("activity-1", "!s:x"))
        self.assertEqual(
            payload["d"], {"k": "activity", "a": "activity-1", "s": "!s:x"}
        )
        self.assertEqual(payload["l"], "cta")
        self.assertNotIn("cta2", text)

    async def test_a_flat_body_takes_a_destination_too(self):
        api = _api(
            threepids=[SimpleNamespace(medium="email", address="a@example.test")]
        )
        body = {
            "user_id": USER,
            "category": "activity_nudges",
            "variant": "do_activity",
            "body": "Ready?",
            "notice_event_id": "$notice:x",
            "notice_room_id": "!dm:x",
            "destination": {"kind": "subscription"},
        }
        await self._handler(api)._send_email(
            body, user_id=USER, category="activity_nudges"
        )
        api._hs.get_send_email_handler.return_value.send_email.assert_awaited_once()


class TestSchedulePassThrough(unittest.IsolatedAsyncioTestCase):
    async def test_both_destinations_reach_the_eventual_send(self):
        api = _api()
        pool = SQLPool()
        self.addCleanup(pool.db.close)
        api._hs.get_datastores.return_value.main.db_pool = pool
        execute = AsyncMock(return_value={"channel": "email", "log_status": "complete"})
        queue = NoticeSchedule(api, execute, HOSTS)
        body = body_with(COURSE, {"kind": "external", "url": BOOKING})
        del body["notice_event_id"]
        body.update(
            scheduled_at=datetime.fromtimestamp(
                (NOW_MS + 60000) / 1000, timezone.utc
            ).isoformat(),
            sender_id="@admin:test",
            notice_content={"body": "Book a session", "check_in_type": "do_activity"},
        )
        await queue.enqueue(body)
        api._hs.get_clock.return_value.time_msec.return_value = NOW_MS + 60000
        await queue.run_due()
        sent = execute.await_args.args[0]
        self.assertEqual(sent["destination"], COURSE)
        self.assertEqual(
            sent["secondary_destination"], {"kind": "external", "url": BOOKING}
        )
        self.assertIn("{{cta2_url}}", sent["email"]["text"])

    async def test_enqueue_honours_the_configured_allowlist(self):
        api = _api()
        api._hs.get_datastores.return_value.main.db_pool = SQLPool()
        queue = NoticeSchedule(api, AsyncMock(), ["admin.pangea.chat"])
        body = body_with({"kind": "external", "url": BOOKING})
        del body["notice_event_id"]
        body.update(
            scheduled_at="2026-10-11T00:00:00+00:00",
            sender_id="@admin:test",
            notice_content={"body": "x"},
        )
        with self.assertRaises(ValueError):
            await queue.enqueue(body)


class TestConfig(unittest.TestCase):
    def test_default_and_custom_allowlist(self):
        base = {"cms_base_url": "x", "cms_service_api_key": "y"}
        config = PangeaChat.parse_config(base)
        self.assertEqual(
            config.notice_external_link_hosts,
            ["calendar.app.google", "admin.pangea.chat", "admin.staging.pangea.chat"],
        )
        custom = PangeaChat.parse_config(
            {**base, "notice_external_link_hosts": [" Booking.Example "]}
        )
        self.assertEqual(custom.notice_external_link_hosts, ["booking.example"])
        for bad in ("calendar.app.google", [""], ["https://a.example/"], [1]):
            with self.subTest(bad=bad), self.assertRaisesRegex(
                ValueError, "notice_external_link_hosts"
            ):
                PangeaChat.parse_config({**base, "notice_external_link_hosts": bad})
