"""Claiming prepared courses for an account that holds their requesting address.

An account whose Synapse-verified email equals a prepared invitation's
requesting address claims it without the link, through the same first claim
(create-course-space.instructions.md, "Who the course is created for").

It runs when the account signs in, and when a verified address is added to it:
email sign-up stores the address during registration and the app signs in from
that response without a separate login, so a sign-in check alone would miss it.
"""

from __future__ import annotations

import logging
from typing import Optional

from synapse.api.errors import SynapseError
from synapse.module_api import ModuleApi

from synapse_pangea_chat.email_invite.course_invitations import CourseInvitationStore
from synapse_pangea_chat.email_invite.provision_course import CourseProvisioner
from synapse_pangea_chat.room_code.constants import ERRCODE_CODE_NOT_FOUND

try:
    import sentry_sdk  # type: ignore[import-not-found]
# silent-ok: sentry-sdk is an optional Synapse extra; without it captures are no-ops (below)
except ImportError:
    sentry_sdk = None

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.email_invite.claim_by_email"
)

EMAIL_MEDIUM = "email"


def _capture_exception(e: Exception) -> None:
    if sentry_sdk is not None:
        sentry_sdk.capture_exception(e)


class ClaimByEmail:
    def __init__(
        self,
        api: ModuleApi,
        invitations: CourseInvitationStore,
        provisioner: CourseProvisioner,
    ) -> None:
        self._store = api._hs.get_datastores().main
        self._invitations = invitations
        self._provisioner = provisioner
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
        await self.claim_for(user_id)

    async def on_add_user_third_party_identifier(
        self, user_id: str, medium: str, address: str
    ) -> None:
        if medium == EMAIL_MEDIUM:
            await self.claim_for(user_id)

    async def claim_for(self, user_id: str) -> None:
        # Synapse awaits these callbacks inside the sign-in or registration
        # request and does not catch what they raise, so nothing may escape: a
        # failed claim must not fail the sign-in. The link still claims it.
        # Awaited rather than backgrounded so the course exists before the
        # sign-in response, and a Google sign-in's later login events find it
        # already claimed.
        try:
            threepids = await self._store.user_get_threepids(user_id)
            emails = [t.address for t in threepids if t.medium == EMAIL_MEDIUM]
            invitations = await self._invitations.prepared_for_emails(emails)
        except Exception as e:
            logger.error(
                "Could not look up prepared courses for %s: %s",
                user_id,
                type(e).__name__,
            )
            _capture_exception(e)
            return
        for invitation in invitations:
            ident = invitation["invitation_id"]
            try:
                room = await self._provisioner.claim(
                    invitation, user_id, defer_to_concurrent=True
                )
            except SynapseError as e:
                if e.errcode == ERRCODE_CODE_NOT_FOUND:
                    # Revoked, or claimed by another account's link, since the
                    # lookup: the same outcome the link would give.
                    logger.info(
                        "Invitation %s is no longer claimable for %s", ident, user_id
                    )
                    continue
                logger.error(
                    "Claim of %s by verified address failed for %s: %s",
                    ident,
                    user_id,
                    e.errcode,
                )
                _capture_exception(e)
                continue
            except Exception as e:
                logger.error(
                    "Claim of %s by verified address failed for %s: %s",
                    ident,
                    user_id,
                    type(e).__name__,
                )
                _capture_exception(e)
                continue
            if room is None:
                logger.info(
                    "Invitation %s is being claimed for %s by another request",
                    ident,
                    user_id,
                )
                continue
            logger.info(
                "Claimed invitation %s for %s by verified address", ident, user_id
            )
