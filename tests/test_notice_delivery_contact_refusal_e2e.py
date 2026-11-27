"""A contact's refusal, read from the CMS, stops a notice Synapse delivers to an
account holding the contact's address, against a real Synapse."""

import requests

from .base_e2e import BaseSynapseE2ETest
from .mock_cms_server import MockCmsServer
from .test_notice_delivery_e2e import MODULE_CONFIG

MODULE_KEY = "e2e-module-cms-key"


class _Deliveries(BaseSynapseE2ETest):
    def _deliver(self, admin_token, user_id, category="activity_nudges"):
        return requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/deliver_notice",
            json={
                "user_id": user_id,
                "category": category,
                "variant": "do_activity",
                "body": "Ready for an activity?",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    async def _account(self, admin_token, *addresses):
        """A new account with ``addresses`` confirmed on it (the admin API
        binds each as validated)."""
        user_id, _ = await self.new_user("alice")
        response = requests.put(
            f"{self.server_url}/_synapse/admin/v2/users/{user_id}",
            json={"threepids": [{"medium": "email", "address": a} for a in addresses]},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return user_id

    def _address(self, name):
        return f"{self.localpart(name)}@example.test"


class TestContactRefusalE2E(_Deliveries):
    cms: MockCmsServer
    cms_url: str

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.cms = MockCmsServer()
        cls.cms_url = cls.cms.start()
        cls.addClassCleanup(cls.cms.stop)

    def setUp(self) -> None:
        super().setUp()
        self.cms.set_refusal_status(200)

    async def _admin(self):
        await self.homeserver(
            module_config={
                **MODULE_CONFIG,
                "cms_base_url": self.cms_url,
                "cms_service_api_key": MODULE_KEY,
            }
        )
        _, admin_token = await self.new_user("admin", admin=True)
        return admin_token

    async def test_a_refusal_on_any_confirmed_address_refuses_and_names_the_contact(
        self,
    ):
        admin = await self._admin()
        first, second = self._address("first"), self._address("second")
        user_id = await self._account(admin, first, second)
        self.cms.set_contact_refusal(second, {"activity_nudges"})

        refused = self._deliver(admin, user_id)
        self.assertEqual(refused.status_code, 200, refused.text)
        self.assertEqual(refused.json()["channel"], "refused")
        self.assertEqual(refused.json()["reason"], "contact_refused")
        self.assertIsNone(refused.json()["push"])
        authorization, body = self.cms.refusal_asks[-1]
        self.assertEqual(authorization, f"service-users API-Key {MODULE_KEY}")
        self.assertEqual(sorted(body["emails"]), sorted([first, second]))
        self.assertEqual(body["category"], "activity_nudges")

        # Another kind is not refused, so delivery goes on to its channels.
        other = self._deliver(admin, user_id, category="suggestions").json()
        self.assertEqual(other["channel"], "none")
        self.assertEqual(other["reason"], "email_disabled")

    async def test_an_account_without_a_confirmed_address_is_not_asked(self):
        admin = await self._admin()
        user_id, _ = await self.new_user("noaddress")
        asks = len(self.cms.refusal_asks)
        data = self._deliver(admin, user_id).json()
        self.assertEqual(data["reason"], "email_disabled")
        self.assertEqual(len(self.cms.refusal_asks), asks)

    async def test_a_failing_cms_sends_nothing(self):
        admin = await self._admin()
        user_id = await self._account(admin, self._address("failing"))
        self.cms.set_refusal_status(503)
        data = self._deliver(admin, user_id).json()
        self.assertEqual(data["channel"], "none")
        self.assertEqual(data["reason"], "contact_check_failed")
        self.assertIsNone(data["push"])


class TestContactRefusalUnreachableE2E(_Deliveries):
    """The CMS address answers nothing at all (the base config's port 9)."""

    async def test_an_unreachable_cms_sends_nothing_and_says_why(self):
        await self.homeserver(module_config=MODULE_CONFIG)
        _, admin = await self.new_user("admin", admin=True)
        user_id = await self._account(admin, self._address("unreachable"))
        data = self._deliver(admin, user_id).json()
        self.assertEqual(data["channel"], "none")
        self.assertEqual(data["reason"], "contact_check_failed")
        self.assertIsNone(data["push"])
        self.assertTrue(
            any("contact refusal unreadable" in line for line in self.server_log())
        )
