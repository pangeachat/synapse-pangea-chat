"""Email address policy — refuse addresses Pangea will not mail.

Registered as a homeserver-wide callback rather than written into Pangea's
registration endpoint, because Synapse's own registration endpoint is reachable
independently of the app and is served by a different worker. Synapse consults
this callback on every path that binds an email address, so registering once
covers all of them.

Design: .github/instructions/email-address-policy.instructions.md
"""

import logging
from typing import Any

from synapse.module_api import ModuleApi

from synapse_pangea_chat.email_policy.is_valid_email_address import (
    is_valid_email_address,
)

logger = logging.getLogger("synapse.module.synapse_pangea_chat.email_policy")

_MEDIUM_EMAIL = "email"


class EmailPolicy:
    def __init__(self, config: Any, api: ModuleApi):
        self._api = api
        self._api.register_password_auth_provider_callbacks(
            is_3pid_allowed=self.is_3pid_allowed,
        )

    async def is_3pid_allowed(
        self, medium: str, address: str, registration: bool
    ) -> bool:
        """Phone numbers are not a Pangea signup path, so only email is judged."""
        if medium != _MEDIUM_EMAIL:
            return True

        if is_valid_email_address(address):
            return True

        # The address itself is a third-party identifier, so only its domain is
        # logged — enough to recognise a pattern of junk signups without
        # recording who tried to sign up.
        _, _, domain = address.rpartition("@")
        logger.info("Refused an email address failing the address policy: @%s", domain)
        return False
