import requests

from synapse_pangea_chat.nudge_delivery import tokens
from synapse_pangea_chat.nudge_delivery.push_rule import reset_confirmed_users_for_tests

from .base_e2e import BaseSynapseE2ETest

E2E_SECRET = "e2e-nudge-secret"
APP_BASE_URL = "https://app.example.test"
PREFERENCES_TYPE = "pangea.communication_preferences"


class TestNudgeDeliveryE2E(BaseSynapseE2ETest):
    """Deliver, unsubscribe, and click against a real Synapse (no SMTP: the
    email leg is covered by the unit tests; here email stays disabled)."""

    @staticmethod
    def _module_config():
        return {
            "send_push_sygnal_url": "https://sygnal.example.test/_matrix/push/v1/notify",
            "nudge_token_secret": E2E_SECRET,
            "app_base_url": APP_BASE_URL,
        }

    def setUp(self):
        super().setUp()
        reset_confirmed_users_for_tests()

    def _deliver(self, admin_token, **overrides):
        body = {
            "user_id": "@alice:my.domain.name",
            "category": "activity_nudges",
            "variant": "do_activity",
            "body": "Ready for an activity?",
        }
        body.update(overrides)
        return requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/deliver_nudge",
            json=body,
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    async def _boot(self):
        started = await self.start_test_synapse(module_config=self._module_config())
        _, synapse_dir, config_path, *_ = started
        await self.register_user(config_path, synapse_dir, "alice", "pw", admin=False)
        await self.register_user(config_path, synapse_dir, "admin", "pw", admin=True)
        _, alice_token = await self.login_user("alice", "pw")
        _, admin_token = await self.login_user("admin", "pw")
        return started, alice_token, admin_token

    def _stop(self, started):
        (
            postgres,
            synapse_dir,
            _config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = started
        self.stop_synapse(
            server_process=server_process,
            stdout_thread=stdout_thread,
            stderr_thread=stderr_thread,
            synapse_dir=synapse_dir,
            postgres=postgres,
        )

    async def test_deliver_requires_admin(self):
        started, alice_token, _admin_token = await self._boot()
        try:
            self.assertEqual(self._deliver(alice_token).status_code, 403)
            self.assertEqual(self._deliver("not-a-token").status_code, 401)
        finally:
            self._stop(started)

    async def test_no_pusher_and_email_disabled_reports_none_and_installs_rule(self):
        started, alice_token, admin_token = await self._boot()
        try:
            response = self._deliver(admin_token)
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
            again = self._deliver(admin_token).json()
            self.assertFalse(again["push_rule_installed"])
        finally:
            self._stop(started)

    async def test_refusal_in_account_data_blocks_delivery(self):
        started, alice_token, admin_token = await self._boot()
        try:
            put = requests.put(
                f"{self.server_url}/_matrix/client/v3/user/@alice:my.domain.name/account_data/{PREFERENCES_TYPE}",
                json={"version": 1, "refused": ["activity_nudges"], "all_off": False},
                headers={"Authorization": f"Bearer {alice_token}"},
            )
            self.assertEqual(put.status_code, 200, put.text)
            data = self._deliver(admin_token).json()
            self.assertEqual(data["channel"], "refused")
            self.assertEqual(data["reason"], "category_refused")
            self.assertIsNone(data["push"])
        finally:
            self._stop(started)

    async def test_unsubscribe_round_trip_writes_account_data(self):
        started, alice_token, _admin_token = await self._boot()
        try:
            token = tokens.sign_token(
                E2E_SECRET.encode(),
                {"k": "unsub", "u": "@alice:my.domain.name", "c": "activity_nudges"},
                now_ms=int(__import__("time").time() * 1000),
                ttl_ms=60_000,
            )
            url = f"{self.server_url}/_synapse/client/pangea/v1/unsubscribe"
            page = requests.get(url, params={"t": token})
            self.assertEqual(page.status_code, 200)
            self.assertIn("Stop activity reminders", page.text)

            # GET must not have acted.
            before = requests.get(
                f"{self.server_url}/_matrix/client/v3/user/@alice:my.domain.name/account_data/{PREFERENCES_TYPE}",
                headers={"Authorization": f"Bearer {alice_token}"},
            )
            self.assertEqual(before.status_code, 404)

            done = requests.post(url, data={"t": token, "scope": "category"})
            self.assertEqual(done.status_code, 200, done.text)
            after = requests.get(
                f"{self.server_url}/_matrix/client/v3/user/@alice:my.domain.name/account_data/{PREFERENCES_TYPE}",
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
        finally:
            self._stop(started)

    async def test_click_records_opened_event_and_redirects(self):
        started, alice_token, admin_token = await self._boot()
        try:
            headers_admin = {"Authorization": f"Bearer {admin_token}"}
            headers_alice = {"Authorization": f"Bearer {alice_token}"}
            room = requests.post(
                f"{self.server_url}/_matrix/client/v3/createRoom",
                json={
                    "invite": ["@alice:my.domain.name"],
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
                    "u": "@alice:my.domain.name",
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
            opened = [
                e for e in messages["chunk"] if e["type"] == "p.room.notice.opened"
            ]
            self.assertEqual(len(opened), 1)
            self.assertEqual(opened[0]["sender"], "@alice:my.domain.name")
            self.assertEqual(
                opened[0]["content"]["notification_event_id"], notice_event_id
            )
            self.assertEqual(opened[0]["content"]["check_in_type"], "do_activity")

            junk = requests.get(
                f"{self.server_url}/_synapse/client/pangea/v1/n",
                params={"t": "junk"},
                allow_redirects=False,
            )
            self.assertEqual(junk.status_code, 302)
            self.assertEqual(junk.headers["Location"], f"{APP_BASE_URL}/")
        finally:
            self._stop(started)
