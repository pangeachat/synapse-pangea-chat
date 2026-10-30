"""The open-session notice: 30 minutes after a learner starts an activity
session, tell them Pangea Bot takes the open seat at the hour
(notice-delivery.instructions.md, "The open-session notice")."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, Optional, Tuple

from synapse.events import EventBase
from synapse.module_api import ModuleApi

from synapse_pangea_chat.bot_user_ids import is_probable_bot_user_id
from synapse_pangea_chat.room_preview import PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE

if TYPE_CHECKING:
    from synapse_pangea_chat.notice_delivery.deliver import DeliverNotice

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.notice_delivery.open_session_notice"
)

VARIANT = "open_session_bot_soon"
CATEGORY = "activity_nudges"
# Half of pangea-bot's hour (OPEN_SEAT_TIMEOUT_MS in its open-session sweep).
NOTICE_DELAY_MS = 30 * 60 * 1000

TITLE = "Your activity is still waiting"
BODY = (
    "Nobody has joined your activity session yet. In 30 minutes Pangea Bot "
    "takes the open seat, or you can play with the bot now."
)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def notice_request(
    *,
    learner: str,
    bot_user_id: str,
    dm_room_id: str,
    session_room_id: str,
    activity_id: str,
    created_ms: int,
) -> dict:
    """The scheduled deliver_notice request. Built only from the session's
    creation, so a second trigger for the same session is the same request and
    the schedule treats it as a duplicate."""
    routing = {
        "pangea.activity.id": activity_id,
        "pangea.activity.session_room_id": session_room_id,
    }
    return {
        "user_id": learner,
        "category": CATEGORY,
        "variant": VARIANT,
        "sender_id": bot_user_id,
        "notice_room_id": dm_room_id,
        "delivery_method": "push-only",
        "scheduled_at": _iso(created_ms + NOTICE_DELAY_MS),
        "activity_id": activity_id,
        "session_room_id": session_room_id,
        "notice_content": {
            "msgtype": "m.notice",
            "body": BODY,
            # pangea-bot reads every notice in its DM as a check-in and rejects
            # one without notice_type.
            "notice_type": "check_in",
            "check_in_type": VARIANT,
            **routing,
        },
        "push": {"title": TITLE, "body": BODY, "content": routing},
        "eligibility": {"bot_fills_open_seat": True},
        "log": {
            "run": {
                "run_id": f"open-session:{session_room_id}",
                "runner": "synapse",
                "funnel": "learner",
                "decided_at": _iso(created_ms),
            },
            "state": {},
            "copy_key": VARIANT,
        },
    }


class OpenSessionNotice:
    def __init__(self, api: ModuleApi, deliver: DeliverNotice, bot_user_id: str):
        self._api = api
        self._deliver = deliver
        self._bot_user_id = bot_user_id
        self._store = api._hs.get_datastores().main

    async def on_new_event(
        self, event: EventBase, _: Mapping[Tuple[str, str], EventBase]
    ) -> None:
        if (
            event.type != PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE
            or event.get_state_key() != ""
        ):
            return
        # Only the plan a session starts with; a later rewrite is not a new session.
        if "replaces_state" in event.unsigned:
            return
        try:
            await self._schedule(event)
        # A missed heads-up costs one notice; the bot still takes the seat at the hour.
        # silent-ok: logged with its traceback
        except Exception:  # noqa: BLE001
            logger.exception("Open-session notice not scheduled: %s", event.room_id)

    async def _schedule(self, plan: EventBase) -> None:
        room_id = plan.room_id
        activity_id = plan.content.get("activity_id")
        if not isinstance(activity_id, str) or not activity_id:
            logger.warning("Open-session notice: plan without activity_id: %s", room_id)
            return
        state = await self._api.get_room_state(
            room_id, event_filter=[("m.room.create", "")]
        )
        create = state.get(("m.room.create", ""))
        if create is None:
            logger.warning("Open-session notice: no create event: %s", room_id)
            return
        learner = create.sender
        if (
            learner == self._bot_user_id
            or is_probable_bot_user_id(learner)
            or not self._api.is_mine(learner)
        ):
            # A session a bot or a remote user created has no learner here to tell.
            return
        dm_room_id = await self._bot_dm(learner)
        if dm_room_id is None:
            logger.info(
                "Open-session notice: %s has no joined DM with the bot", learner
            )
            return
        queued = await self._deliver.deliver(
            notice_request(
                learner=learner,
                bot_user_id=self._bot_user_id,
                dm_room_id=dm_room_id,
                session_room_id=room_id,
                activity_id=activity_id,
                created_ms=create.origin_server_ts,
            ),
            requested_by=self._bot_user_id,
        )
        logger.info(
            "Open-session notice queued for %s: %s (duplicate=%s)",
            room_id,
            queued.get("schedule_id"),
            queued.get("duplicate"),
        )

    async def _bot_dm(self, learner: str) -> Optional[str]:
        """The learner's DM with the bot, where the notice is recorded: the
        latest room their m.direct lists for the bot that both have joined."""
        direct: Any = await self._api.account_data_manager.get_global(
            learner, "m.direct"
        )
        rooms = direct.get(self._bot_user_id) if isinstance(direct, Mapping) else None
        if not isinstance(rooms, (list, tuple)):
            return None
        for room_id in reversed(rooms):
            if not isinstance(room_id, str):
                continue
            joined = [
                (
                    await self._store.get_local_current_membership_for_user_in_room(
                        user, room_id
                    )
                )[0]
                == "join"
                for user in (learner, self._bot_user_id)
            ]
            if all(joined):
                return room_id
        return None
