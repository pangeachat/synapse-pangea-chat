import unittest
from typing import Optional
from unittest.mock import MagicMock

from synapse_pangea_chat.email_policy import EmailPolicy, is_valid_email_address


class TestIsValidEmailAddress(unittest.TestCase):
    """The address rule Pangea enforces at signup.

    Design: .github/instructions/email-address-policy.instructions.md
    """

    def test_accepts_ordinary_addresses(self) -> None:
        for address in [
            "learner@example.com",
            "a@b.co",
            "user+tag@gmail.com",
            "first.last@mail.example.co.uk",
            "learner@my-school.edu",
            "learner@a.b.c.example.com",
        ]:
            with self.subTest(address=address):
                self.assertTrue(is_valid_email_address(address))

    def test_refuses_the_addresses_the_old_check_let_through(self) -> None:
        """The whole point of the rule: a single "@" is not an address."""
        for address in ["a@b", "@", "@b", "a@", "not-an-email", "a@b."]:
            with self.subTest(address=address):
                self.assertFalse(is_valid_email_address(address))

    def test_refuses_malformed_domains(self) -> None:
        for address in [
            "a@b..co",  # empty label
            "a@-b.co",  # label starts with a hyphen
            "a@b-.co",  # label ends with a hyphen
            "a@b.c",  # single-letter top-level label
            "a@b.11",  # digits-only top-level label
            "a@b.co m",  # whitespace
            "a b@c.de",
            "a@@b.co",
        ]:
            with self.subTest(address=address):
                self.assertFalse(is_valid_email_address(address))

    def test_accepts_an_internationalised_domain_in_either_form(self) -> None:
        self.assertTrue(is_valid_email_address("learner@bücher.de"))
        self.assertTrue(is_valid_email_address("learner@xn--bcher-kva.de"))

    def test_refuses_an_address_longer_than_mail_servers_accept(self) -> None:
        self.assertTrue(is_valid_email_address("a" * 240 + "@example.com"))
        self.assertFalse(is_valid_email_address("a" * 250 + "@example.com"))


class TestEmailPolicyCallback(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.api = MagicMock()
        self.policy = EmailPolicy(config=MagicMock(), api=self.api)

    def test_registers_itself_as_a_homeserver_wide_callback(self) -> None:
        """Registering the callback is what covers Synapse's own registration
        endpoint, which the Pangea endpoint cannot reach."""
        self.api.register_password_auth_provider_callbacks.assert_called_once_with(
            is_3pid_allowed=self.policy.is_3pid_allowed,
        )

    async def test_refuses_an_address_failing_the_rule(self) -> None:
        self.assertFalse(await self._is_allowed("email", "a@b"))

    async def test_allows_an_address_passing_the_rule(self) -> None:
        self.assertTrue(await self._is_allowed("email", "learner@example.com"))

    async def test_ignores_other_media(self) -> None:
        """Phone numbers are not a Pangea signup path, so they pass untouched."""
        self.assertTrue(await self._is_allowed("msisdn", "447700900000"))

    async def _is_allowed(
        self, medium: str, address: str, registration: Optional[bool] = True
    ) -> bool:
        return await self.policy.is_3pid_allowed(medium, address, bool(registration))


if __name__ == "__main__":
    unittest.main()
