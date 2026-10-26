import socket
from unittest.mock import MagicMock

import requests
import yaml
from synapse.util.macaroons import MacaroonGenerator

from synapse_pangea_chat.notice_delivery import tokens

from .base_e2e import SERVER_NAME, BaseSynapseE2ETest

E2E_SECRET = "e2e-nudge-secret"
APP_BASE_URL = "https://app.example.test"
PREFERENCES_TYPE = "pangea.communication_preferences"
MODULE_CONFIG = {
    "send_push_sygnal_url": "https://sygnal.example.test/_matrix/push/v1/notify",
    "notice_token_secret": E2E_SECRET,
    "app_base_url": APP_BASE_URL,
}


class TestNoticeDeliveryE2E(BaseSynapseE2ETest):
    """Deliver, unsubscribe, and click against a real Synapse (no SMTP: the
    email leg is covered by the unit tests; here email stays disabled).

    The module remembers per person whose suppression rule it has confirmed,
    so each test delivers to its own alice and sees the rule installed fresh.
    """

    def _prepare(self, admin_token, user_id):
        return requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/prepare_notice",
            json={"user_id": user_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    async def test_prepare_installs_the_suppression_rule_once(self):
        alice_id, alice, admin = await self._alice_and_admin()
        first = self._prepare(admin, alice_id)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["push_rule_installed"])
        self.assertTrue(first.json()["suppression_enabled"])
        second = self._prepare(admin, alice_id)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertFalse(second.json()["push_rule_installed"])
        rules = requests.get(
            f"{self.server_url}/_matrix/client/v3/pushrules/global/override/p.rule.bot_notice",
            headers={"Authorization": f"Bearer {alice}"},
        )
        self.assertEqual(rules.status_code, 200, rules.text)
        self.assertEqual(rules.json()["actions"], ["dont_notify"])
        bad = self._prepare(admin, "nope")
        self.assertEqual(bad.status_code, 400)

    def _deliver(self, admin_token, user_id, **overrides):
        body = {
            "user_id": user_id,
            "category": "activity_nudges",
            "variant": "do_activity",
            "body": "Ready for an activity?",
        }
        body.update(overrides)
        return requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/deliver_notice",
            json=body,
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    async def _alice_and_admin(self):
        """Returns (alice_id, alice_token, admin_token)."""
        await self.homeserver(module_config=MODULE_CONFIG)
        alice_id, alice_token = await self.new_user("alice")
        _, admin_token = await self.new_user("admin", admin=True)
        return alice_id, alice_token, admin_token

    async def test_deliver_requires_admin(self):
        alice_id, alice_token, _admin_token = await self._alice_and_admin()
        self.assertEqual(self._deliver(alice_token, alice_id).status_code, 403)
        self.assertEqual(self._deliver("not-a-token", alice_id).status_code, 401)

    async def test_legacy_routes_share_authentication_and_refusals(self):
        alice_id, alice, admin = await self._alice_and_admin()
        response = requests.put(
            f"{self.server_url}/_matrix/client/v3/user/{alice_id}/account_data/{PREFERENCES_TYPE}",
            json={"version": 1, "refused": ["activity_nudges"]},
            headers={"Authorization": f"Bearer {alice}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        for route in ("deliver_nudge", "deliver_notice"):
            url = f"{self.server_url}/_synapse/client/pangea/v1/{route}"
            body = {
                "user_id": alice_id,
                "category": "activity_nudges",
                "body": "Test",
            }
            denied = requests.post(
                url, json=body, headers={"Authorization": f"Bearer {alice}"}
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            delivered = requests.post(
                url, json=body, headers={"Authorization": f"Bearer {admin}"}
            )
            self.assertEqual(delivered.status_code, 200, delivered.text)
            self.assertEqual(delivered.json()["channel"], "refused")
            self.assertEqual(delivered.json()["reason"], "category_refused")
        for route in ("prepare_nudge", "prepare_notice"):
            url = f"{self.server_url}/_synapse/client/pangea/v1/{route}"
            body = {"user_id": alice_id}
            denied = requests.post(
                url, json=body, headers={"Authorization": f"Bearer {alice}"}
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            prepared = requests.post(
                url, json=body, headers={"Authorization": f"Bearer {admin}"}
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            self.assertTrue(prepared.json()["suppression_enabled"])

    async def test_no_pusher_and_email_disabled_reports_none_and_installs_rule(self):
        alice_id, alice_token, admin_token = await self._alice_and_admin()
        response = self._deliver(admin_token, alice_id)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["channel"], "none")
        self.assertEqual(data["reason"], "email_disabled")
        self.assertTrue(data["push_rule_installed"])
        self.assertEqual(data["push"]["attempted"], 0)

        rules = requests.get(
            f"{self.server_url}/_matrix/client/v3/pushrules/",
            headers={"Authorization": f"Bearer {alice_token}"},
        ).json()
        override_ids = [rule["rule_id"] for rule in rules["global"]["override"]]
        self.assertIn("p.rule.bot_notice", override_ids)

        # Second delivery: the rule is already there and is not re-installed.
        again = self._deliver(admin_token, alice_id).json()
        self.assertFalse(again["push_rule_installed"])

    async def test_refusal_in_account_data_blocks_delivery(self):
        alice_id, alice_token, admin_token = await self._alice_and_admin()
        put = requests.put(
            f"{self.server_url}/_matrix/client/v3/user/{alice_id}/account_data/{PREFERENCES_TYPE}",
            json={"version": 1, "refused": ["activity_nudges"], "all_off": False},
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        self.assertEqual(put.status_code, 200, put.text)
        data = self._deliver(admin_token, alice_id).json()
        self.assertEqual(data["channel"], "refused")
        self.assertEqual(data["reason"], "category_refused")
        self.assertIsNone(data["push"])

    async def test_unsubscribe_round_trip_writes_account_data(self):
        alice_id, alice_token, _admin_token = await self._alice_and_admin()
        token = tokens.sign_token(
            E2E_SECRET.encode(),
            {"k": "unsub", "u": alice_id, "c": "activity_nudges"},
            now_ms=int(__import__("time").time() * 1000),
            ttl_ms=60_000,
        )
        url = f"{self.server_url}/_synapse/client/pangea/v1/unsubscribe"
        page = requests.get(url, params={"t": token})
        self.assertEqual(page.status_code, 200)
        self.assertIn("activity reminders", page.text)

        # GET must not have acted.
        before = requests.get(
            f"{self.server_url}/_matrix/client/v3/user/{alice_id}/account_data/{PREFERENCES_TYPE}",
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        self.assertEqual(before.status_code, 404)

        done = requests.post(url, data={"t": token, "scope": "category"})
        self.assertEqual(done.status_code, 200, done.text)
        after = requests.get(
            f"{self.server_url}/_matrix/client/v3/user/{alice_id}/account_data/{PREFERENCES_TYPE}",
            headers={"Authorization": f"Bearer {alice_token}"},
        ).json()
        self.assertEqual(after["refused"], ["activity_nudges"])
        self.assertFalse(after["all_off"])
        self.assertEqual(after["source"], "unsubscribe_link")

        one_click = requests.post(
            url,
            params={"t": token},
            data="List-Unsubscribe=One-Click",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(one_click.status_code, 200)

        expired = requests.post(url, data={"t": "junk", "scope": "category"})
        self.assertEqual(expired.status_code, 400)

    async def test_click_records_opened_event_and_redirects(self):
        alice_id, alice_token, admin_token = await self._alice_and_admin()
        headers_admin = {"Authorization": f"Bearer {admin_token}"}
        headers_alice = {"Authorization": f"Bearer {alice_token}"}
        room = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json={
                "invite": [alice_id],
                "is_direct": True,
                "preset": "trusted_private_chat",
            },
            headers=headers_admin,
        ).json()
        room_id = room["room_id"]
        self.assertEqual(
            requests.post(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/join",
                headers=headers_alice,
            ).status_code,
            200,
        )
        notice = requests.put(
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/send/p.room.notice/txn-1",
            json={
                "notice_type": "check_in",
                "check_in_type": "do_activity",
                "body": "hi",
            },
            headers=headers_admin,
        ).json()
        notice_event_id = notice["event_id"]

        token = tokens.sign_token(
            E2E_SECRET.encode(),
            {
                "k": "click",
                "u": alice_id,
                "e": notice_event_id,
                "r": room_id,
                "v": "do_activity",
                "a": "activity-123",
                "s": "!session:my.domain.name",
            },
            now_ms=int(__import__("time").time() * 1000),
            ttl_ms=60_000,
        )
        response = requests.get(
            f"{self.server_url}/_synapse/client/pangea/v1/n",
            params={"t": token},
            allow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers["Location"],
            f"{APP_BASE_URL}/activity-123?roomid=%21session%3Amy.domain.name",
        )

        messages = requests.get(
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/messages",
            params={"dir": "b", "limit": 20},
            headers=headers_alice,
        ).json()
        opened = [e for e in messages["chunk"] if e["type"] == "p.room.notice.opened"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["sender"], alice_id)
        self.assertEqual(opened[0]["content"]["notification_event_id"], notice_event_id)
        self.assertEqual(opened[0]["content"]["check_in_type"], "do_activity")

        junk = requests.get(
            f"{self.server_url}/_synapse/client/pangea/v1/n",
            params={"t": "junk"},
            allow_redirects=False,
        )
        self.assertEqual(junk.status_code, 302)
        self.assertEqual(junk.headers["Location"], f"{APP_BASE_URL}/")


class TestMissedMessageUnsubscribeE2E(BaseSynapseE2ETest):
    """Synapse's own unsubscribe link, on a homeserver that sends missed-message
    email and has the module serve that link."""

    port: int

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # The homeserver's public URL is part of its config, so its port is
        # chosen before it starts, once for the class.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]

    async def test_missed_message_unsubscribe_link_confirms_then_refuses(self):
        """Opening the link changes nothing; the one-click POST records the
        refusal and removes the email pusher."""
        homeserver = await self.homeserver(
            module_config={
                **MODULE_CONFIG,
                "notice_missed_message_unsubscribe_enabled": True,
            },
            synapse_config_overrides={
                "public_baseurl": f"http://127.0.0.1:{self.port}/",
                "listeners": [
                    {
                        "port": self.port,
                        "tls": False,
                        "type": "http",
                        "x_forwarded": True,
                        "bind_addresses": ["127.0.0.1"],
                        "resources": [
                            {"names": ["client", "federation"], "compress": False}
                        ],
                    }
                ],
                "email": {
                    "smtp_host": "localhost",
                    "smtp_port": 2525,
                    "notif_from": "Pangea Chat <noreply@example.test>",
                    "enable_notifs": True,
                    "notif_for_new_users": False,
                    "client_base_url": APP_BASE_URL,
                },
            },
        )
        address = f"{self.localpart('alice')}@example.test"
        user_id, alice_token = await self.new_user("alice")
        _, admin_token = await self.new_user("admin", admin=True)
        alice = {"Authorization": f"Bearer {alice_token}"}

        threepid = requests.put(
            f"{self.server_url}/_synapse/admin/v2/users/{user_id}",
            json={"threepids": [{"medium": "email", "address": address}]},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(threepid.status_code, 200, threepid.text)
        pusher = requests.post(
            f"{self.server_url}/_matrix/client/v3/pushers/set",
            json={
                "kind": "email",
                "app_id": "m.email",
                "pushkey": address,
                "app_display_name": "Email Notifications",
                "device_display_name": address,
                "lang": "en",
                "data": {},
            },
            headers=alice,
        )
        self.assertEqual(pusher.status_code, 200, pusher.text)

        def email_pushkeys():
            pushers = requests.get(
                f"{self.server_url}/_matrix/client/v3/pushers", headers=alice
            ).json()["pushers"]
            return [p["pushkey"] for p in pushers if p["app_id"] == "m.email"]

        def preferences():
            return requests.get(
                f"{self.server_url}/_matrix/client/v3/user/{user_id}/account_data/{PREFERENCES_TYPE}",
                headers=alice,
            )

        # The link Synapse's mailer builds, signed with this server's key.
        with open(homeserver.config_path, encoding="utf-8") as f:
            secret = yaml.safe_load(f)["macaroon_secret_key"].encode()
        token = MacaroonGenerator(
            MagicMock(), SERVER_NAME, secret
        ).generate_delete_pusher_token(user_id, "m.email", address)
        url = f"{self.server_url}/_synapse/client/unsubscribe"
        params = {"access_token": token, "app_id": "m.email", "pushkey": address}

        page = requests.get(url, params=params)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Missed-message emails", page.text)
        self.assertEqual(email_pushkeys(), [address], "GET must not act")
        self.assertEqual(preferences().status_code, 404, "GET must not act")

        one_click = requests.post(
            url,
            params=params,
            data="List-Unsubscribe=One-Click",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(one_click.status_code, 200, one_click.text)
        after = preferences().json()
        self.assertEqual(after["refused"], ["missed_message"])
        self.assertEqual(after["source"], "unsubscribe_link")
        self.assertEqual(email_pushkeys(), [])

        tampered = requests.get(url, params={**params, "pushkey": "x@example.test"})
        self.assertEqual(tampered.status_code, 400)
