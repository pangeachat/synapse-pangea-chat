"""Sending the claim notice, once, with a retry for a failed send.

The notice is the second email of a requested course's claim: the class link,
sent to the address the course was requested from and naming the account that
claimed it (knock-with-code.instructions.md, "Claiming a course"). It is how the
requesting teacher learns their course was claimed, so a transient mail failure
must not lose it: the admin code is already burned by then, and resubmitting
the link answers "code not found".

``notify`` sends it under a lease from the claim store. ``knock_with_code``
calls it right after the claim; a looping call retries every owed notice whose
lease has run out. Each process that loads the module runs the loop, and the
lease is what keeps any one notice to one sender.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional, cast

from synapse.api.constants import EventTypes
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.module_api import ModuleApi
from synapse.types import UserID

from synapse_pangea_chat.email_invite.build_join_url import build_join_url
from synapse_pangea_chat.email_invite.course_claim_emails import CourseClaimMailer
from synapse_pangea_chat.email_invite.course_claims import (
    MAX_NOTICE_ATTEMPTS,
    CourseClaimStore,
)
from synapse_pangea_chat.moderation.compat import (
    background_process_args,
    looping_call_interval,
)
from synapse_pangea_chat.room_code.constants import (
    ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    EVENT_TYPE_M_ROOM_JOIN_RULES,
)

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

try:
    import sentry_sdk  # type: ignore[import-not-found]
# silent-ok: sentry-sdk is an optional Synapse extra; without it captures are no-ops (below)
except ImportError:
    sentry_sdk = None

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.email_invite.course_claim_notice"
)

#: How long one send holds the notice before another may try it.
NOTICE_LEASE_MS = 10 * 60 * 1000
#: How often the retry looks for owed notices.
RETRY_INTERVAL_SECONDS = 5 * 60


def _capture_exception(e: Exception) -> None:
    if sentry_sdk is not None:
        sentry_sdk.capture_exception(e)


class ClaimNoticeAbandoned(Exception):
    """A claim notice failed ``MAX_NOTICE_ATTEMPTS`` times and is no longer
    retried; the requesting teacher has not been told their course was
    claimed."""


class CourseClaimNotifier:
    def __init__(
        self,
        api: ModuleApi,
        config: "PangeaChatConfig",
        store: CourseClaimStore,
        mailer: CourseClaimMailer,
    ) -> None:
        self._api = api
        self._config = config
        self._store = store
        self._mailer = mailer
        self._clock = api._hs.get_clock()

    def start_retry_loop(self) -> None:
        self._clock.looping_call(
            cast(Any, run_as_background_process),
            cast(Any, looping_call_interval(RETRY_INTERVAL_SECONDS)),
            *background_process_args(
                self._api._hs,
                "pangea_course_claim_notice_retry",
                self.retry_outstanding,
            ),
        )

    async def retry_outstanding(self) -> None:
        try:
            owed = await self._store.outstanding_notices(self._clock.time_msec())
        except Exception as e:
            logger.error(f"Failed to list owed claim notices: {type(e).__name__}")
            _capture_exception(e)
            return
        for room_id, claimer_id in owed:
            await self.notify(room_id, claimer_id)

    async def notify(self, room_id: str, claimer_id: str) -> None:
        """Send the claim notice if it is owed and nobody else is sending it.

        Never raises: a failure is captured, and the notice stays owed for the
        retry until its attempts run out.
        """
        attempt: Optional[int] = None
        try:
            reservation = await self._store.reserve_notice(
                room_id, claimer_id, self._clock.time_msec(), NOTICE_LEASE_MS
            )
            if reservation is None:
                return
            attempt = reservation.attempt
            course_title, class_code = await self._course_title_and_class_code(room_id)
            await self._mailer.send_course_claimed(
                email_address=reservation.requested_email,
                course_title=course_title,
                claimed_by_user_id=claimer_id,
                claimed_by_display_name=await self._display_name(claimer_id),
                class_url=build_join_url(self._config.app_base_url, class_code),
                class_code=class_code,
            )
            await self._store.mark_notice_sent(
                room_id, claimer_id, self._clock.time_msec()
            )
        except Exception as e:
            logger.error(
                f"Failed to send the claim notice for {room_id} "
                f"(attempt {attempt}): {type(e).__name__}"
            )
            _capture_exception(e)
            if attempt is not None and attempt >= MAX_NOTICE_ATTEMPTS:
                _capture_exception(
                    ClaimNoticeAbandoned(
                        f"Claim notice for {room_id} abandoned after {attempt} attempts"
                    )
                )

    async def _course_title_and_class_code(self, room_id: str) -> tuple[str, str]:
        state = await self._api.get_room_state(
            room_id=room_id,
            event_filter=[
                (EVENT_TYPE_M_ROOM_JOIN_RULES, None),
                (EventTypes.Name, None),
            ],
        )
        class_code: Any = None
        title = ""
        for event in state.values():
            if event.type == EVENT_TYPE_M_ROOM_JOIN_RULES:
                class_code = event.content.get(ACCESS_CODE_JOIN_RULE_CONTENT_KEY)
            elif event.type == EventTypes.Name:
                title = event.content.get("name") or ""
        if not isinstance(class_code, str) or not class_code:
            raise ValueError(f"Claimed course {room_id} has no class code")
        return title, class_code

    async def _display_name(self, user_id: str) -> Optional[str]:
        if not self._api.is_mine(user_id):
            return None
        profile = await self._api.get_profile_for_user(
            UserID.from_string(user_id).localpart
        )
        return profile.display_name
