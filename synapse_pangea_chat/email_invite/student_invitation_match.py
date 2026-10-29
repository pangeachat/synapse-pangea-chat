"""Inviting an account into the course a student invitation was sent for, when one of its
verified addresses is the invited address (invite-by-email.instructions.md, "Joining without
the link").

It runs when the account signs in, when a verified address is added to it, and when a teacher
sends an invitation to an address an account already holds. Each invitation matches one
account once, so a student who declines or leaves is not invited again, and only invitations
from the last 120 days match.
"""

from __future__ import annotations

import logging
from typing import Optional

from synapse.module_api import ModuleApi
from synapse.util.threepids import canonicalise_email

from synapse_pangea_chat.blocked_join_gate import is_blocked_by_room_admin
from synapse_pangea_chat.email_invite.student_invitations import StudentInvitationStore
from synapse_pangea_chat.room_code.constants import (
    MEMBERSHIP_BAN,
    MEMBERSHIP_INVITE,
    MEMBERSHIP_JOIN,
)
from synapse_pangea_chat.room_code.invite_user_to_room import (
    NoInviterAvailableError,
    invite_user_to_room,
)
from synapse_pangea_chat.room_code.user_is_room_member import (
    get_user_room_membership,
)

try:
    import sentry_sdk  # type: ignore[import-not-found]
# silent-ok: sentry-sdk is an optional Synapse extra; without it captures are no-ops (below)
except ImportError:
    sentry_sdk = None

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.email_invite.student_invitation_match"
)

EMAIL_MEDIUM = "email"
MATCH_WINDOW_MS = 120 * 24 * 60 * 60 * 1000


def _capture_exception(e: Exception) -> None:
    if sentry_sdk is not None:
        sentry_sdk.capture_exception(e)


class StudentInvitationMatch:
    def __init__(
        self,
        api: ModuleApi,
        students: StudentInvitationStore,
        blocked_join_gate_enabled: bool = False,
    ) -> None:
        self._api = api
        self._store = api._hs.get_datastores().main
        self._students = students
        self._blocked_join_gate_enabled = blocked_join_gate_enabled
        api.register_account_validity_callbacks(on_user_login=self.on_user_login)
        api.register_third_party_rules_callbacks(
            on_add_user_third_party_identifier=self.on_add_user_third_party_identifier
        )

    async def on_user_login(
        self,
        user_id: str,
        auth_provider_type: Optional[str],
        auth_provider_id: Optional[str],
    ) -> None:
        await self.match_account(user_id)

    async def on_add_user_third_party_identifier(
        self, user_id: str, medium: str, address: str
    ) -> None:
        if medium == EMAIL_MEDIUM:
            await self.match_account(user_id)

    async def match_address(self, address: str) -> None:
        """After a teacher sends an invitation: the account that already holds the address."""
        try:
            canonical = canonicalise_email(address)
        # silent-ok: an address Synapse cannot parse is no account's verified address
        except ValueError:
            return
        try:
            user_id = await self._store.get_user_id_by_threepid(EMAIL_MEDIUM, canonical)
        except Exception as e:
            logger.error(
                "Could not look up the account for an invited address: %s",
                type(e).__name__,
            )
            _capture_exception(e)
            return
        if user_id is not None:
            await self.match_account(user_id)

    async def match_account(self, user_id: str) -> None:
        # Synapse awaits these callbacks inside the sign-in or registration
        # request and does not catch what `on_user_login` raises, so nothing
        # may escape: a failed invite must not fail the sign-in. Awaited
        # rather than backgrounded so the invite is there when the app first
        # syncs.
        try:
            threepids = await self._store.user_get_threepids(user_id)
            emails = [t.address for t in threepids if t.medium == EMAIL_MEDIUM]
            now = self._api._hs.get_clock().time_msec()
            invitations = await self._students.unmatched_for_emails(
                emails, now - MATCH_WINDOW_MS
            )
        except Exception as e:
            logger.error(
                "Could not look up student invitations for %s: %s",
                user_id,
                type(e).__name__,
            )
            _capture_exception(e)
            return
        for invitation in invitations:
            ident = invitation["invitation_id"]
            taken = False
            try:
                taken = await self._students.match(ident, user_id, now)
                if taken:
                    await self._invite(invitation["room_id"], user_id, ident)
            except NoInviterAvailableError as e:
                # A deleted course, or one with no joined admin left. Trying
                # again at every sign-in would not help, so the match stands;
                # the emailed link still works.
                logger.warning("No one can invite %s for invitation %s", user_id, ident)
                _capture_exception(e)
            except Exception as e:
                logger.error(
                    "Invite for invitation %s failed for %s: %s",
                    ident,
                    user_id,
                    type(e).__name__,
                )
                _capture_exception(e)
                if taken:
                    await self._release(ident, user_id)

    async def _invite(self, room_id: str, user_id: str, ident: str) -> None:
        membership = await get_user_room_membership(self._api, user_id, room_id)
        if membership in (MEMBERSHIP_JOIN, MEMBERSHIP_INVITE, MEMBERSHIP_BAN):
            # Already in the course or invited to it, or banned from it. The
            # match stands, so leaving or an unban later does not re-invite.
            logger.info(
                "Invitation %s matched %s, whose membership is %s",
                ident,
                user_id,
                membership,
            )
            return
        if self._blocked_join_gate_enabled and await is_blocked_by_room_admin(
            self._api, room_id, user_id
        ):
            # Every admin blocked the account (blocked-join-gate.instructions.md).
            logger.info("Invitation %s matched %s, who is blocked", ident, user_id)
            return
        await invite_user_to_room(api=self._api, user_id=user_id, room_id=room_id)
        logger.info(
            "Invited %s into %s for invitation %s by verified address",
            user_id,
            room_id,
            ident,
        )

    async def _release(self, ident: str, user_id: str) -> None:
        try:
            await self._students.unmatch(ident, user_id)
        except Exception as e:
            # The match stands, so this invitation will not invite this
            # account; the emailed link still works.
            logger.error(
                "Could not release invitation %s for %s: %s",
                ident,
                user_id,
                type(e).__name__,
            )
            _capture_exception(e)
