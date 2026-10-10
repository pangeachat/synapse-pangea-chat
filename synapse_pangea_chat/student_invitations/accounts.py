"""What the module reads about an account: its verified emails and its name.

A Synapse-bound email (``user_threepids``) is a verified one: Synapse binds an
address only after its owner proves it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Set

from synapse.types import UserID
from synapse.util.threepids import canonicalise_email

from synapse_pangea_chat.course_member_emails.members import pick_email_per_user

EMAIL_MEDIUM = "email"


def email_key(address: str) -> Optional[str]:
    """The canonical key an invitation is matched by, as Synapse stores a
    verified address (``canonicalise_email``); None if it is not an email."""
    try:
        return canonicalise_email(address)
    # silent-ok: an address Synapse cannot canonicalise matches nothing
    except ValueError:
        return None


class Accounts:
    def __init__(self, api: Any) -> None:
        self._main = api._hs.get_datastores().main

    async def _emails(self, user_id: str) -> Dict[str, Any]:
        threepids = await self._main.user_get_threepids(user_id)
        return {
            t.address: t.added_at
            for t in threepids
            if t.medium == EMAIL_MEDIUM and isinstance(t.address, str)
        }

    async def verified_email_keys(self, user_id: str) -> Set[str]:
        keys: Set[str] = set()
        for address in await self._emails(user_id):
            key = email_key(address)
            if key is not None:
                keys.add(key)
        return keys

    async def first_email(self, user_id: str) -> Optional[str]:
        """The account's first bound email (then alphabetical), as
        ``course_member_emails`` picks it."""
        emails = await self._emails(user_id)
        chosen = pick_email_per_user(
            {"user_id": user_id, "address": address, "added_at": added_at}
            for address, added_at in emails.items()
        )
        return chosen.get(user_id)

    async def user_for_email_key(self, key: str) -> Optional[str]:
        """The account that has this canonical address verified, if any
        (Synapse binds an address to at most one account)."""
        user_id = await self._main.get_user_id_by_threepid(EMAIL_MEDIUM, key)
        return user_id if isinstance(user_id, str) else None

    async def display_name(self, user_id: str) -> Optional[str]:
        name = await self._main.get_profile_displayname(UserID.from_string(user_id))
        return name if isinstance(name, str) and name else None

    async def membership(self, room_id: str, user_id: str) -> Optional[str]:
        membership, _ = await self._main.get_local_current_membership_for_user_in_room(
            user_id, room_id
        )
        return membership
