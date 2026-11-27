"""A contact's refusal, read before a nudge or marketing notice goes out.

A person's refusals are kept in two places: the account's
``pangea.communication_preferences`` and, for a person Pangea knows by email
address, the CMS contact (written by the CMS unsubscribe page). Neither page
writes the other's record, so every send reads both: a stop in either stops
that kind of mail, and push and in-app of that kind, for every account that
has the contact's address confirmed (contacts doc, "What they have asked us
not to send").

The CMS read takes the account's confirmed addresses, read live, and one
category, and answers only ``refused`` and ``suppressed``. It is read on every
send, never cached, like the account's own preferences, with the module's own
CMS key. Consent that cannot be checked is not consent: an unreachable CMS or
an answer of the wrong shape raises ``ContactRefusalUnavailable`` and the
caller sends nothing.
"""

from __future__ import annotations

import json
from typing import Any, List

import attr
from synapse.module_api import ModuleApi

from synapse_pangea_chat.cms_http import cms_request, service_headers
from synapse_pangea_chat.config import PangeaChatConfig

EMAIL_MEDIUM = "email"
# The CMS answers 400 past 20 addresses; an account with more is asked in turns.
MAX_ADDRESSES_PER_ASK = 20
TIMEOUT_SECONDS = 10


class ContactRefusalUnavailable(Exception):
    """The contact's refusal could not be read; nothing may be sent."""

    reason = "contact_check_failed"


@attr.s(auto_attribs=True, frozen=True)
class ContactStanding:
    refused: bool = False
    #: A permanent bounce or a complaint on any of the addresses. Delivery does not
    #: act on this aggregate; email asks about its own address (address_suppressed).
    suppressed: bool = False


NOTHING_REFUSED = ContactStanding()


class ContactRefusals:
    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        self._http = api.http_client
        self._reactor = api._hs.get_reactor()
        self._store = api._hs.get_datastores().main
        self._url = config.cms_base_url.rstrip("/") + "/api/contacts/refusal"
        # The module's own CMS key: the endpoint answers it yes or no and
        # returns no contact data, so no second credential is needed.
        self._headers = service_headers(config.cms_service_api_key, json_body=True)

    async def confirmed_addresses(self, user_id: str) -> List[str]:
        """The email addresses Synapse has confirmed on the account."""
        addresses = []
        for threepid in await self._store.user_get_threepids(user_id):
            medium = getattr(threepid, "medium", None)
            address = getattr(threepid, "address", None)
            validated_at = getattr(threepid, "validated_at", None)
            if (
                medium == EMAIL_MEDIUM
                and isinstance(address, str)
                and address.strip()
                and validated_at
            ):
                addresses.append(address.strip())
        return addresses

    async def standing(self, user_id: str, category: str) -> ContactStanding:
        addresses = await self.confirmed_addresses(user_id)
        if not addresses:
            # No address means no contact can hold one: nothing to read.
            return NOTHING_REFUSED
        refused = suppressed = False
        for start in range(0, len(addresses), MAX_ADDRESSES_PER_ASK):
            answer = await self._ask(
                addresses[start : start + MAX_ADDRESSES_PER_ASK], category
            )
            refused = refused or answer.refused
            suppressed = suppressed or answer.suppressed
        return ContactStanding(refused=refused, suppressed=suppressed)

    async def address_suppressed(self, address: str, category: str) -> bool:
        """Whether mail to this one address is suppressed. A bounce or a
        complaint belongs to the address, not the account, so email asks about
        the address it is about to use and nothing else."""
        return (await self._ask([address], category)).suppressed

    async def _ask(self, addresses: List[str], category: str) -> ContactStanding:
        try:
            code, raw = await cms_request(
                self._http,
                "POST",
                self._url,
                self._headers,
                max_bytes=4_096,
                body={"emails": addresses, "category": category},
                timeout_seconds=TIMEOUT_SECONDS,
                reactor=self._reactor,
            )
        except Exception as error:  # noqa: BLE001
            raise ContactRefusalUnavailable(
                f"CMS unreachable: {type(error).__name__}"
            ) from error
        if code != 200:
            raise ContactRefusalUnavailable(f"CMS answered {code}")
        try:
            payload: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise ContactRefusalUnavailable("CMS answered non-JSON") from error
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("refused"), bool)
            or not isinstance(payload.get("suppressed"), bool)
        ):
            raise ContactRefusalUnavailable("CMS answer is not the contract's shape")
        return ContactStanding(
            refused=payload["refused"], suppressed=payload["suppressed"]
        )
