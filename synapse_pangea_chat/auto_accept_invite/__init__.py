import logging
import time
from typing import Any, Dict

from synapse.module_api import (
    EventBase,
    ModuleApi,
    StateMap,
    UserID,
    run_as_background_process,
)

from synapse_pangea_chat.room_code.invite_user_to_room import invite_user_to_room

logger = logging.getLogger(__name__)

ACCOUNT_DATA_DIRECT_MESSAGE_LIST = "m.direct"

# Cooldown period (seconds) after a failed knock auto-invite per room
_KNOCK_COOLDOWN_SECONDS = 300  # 5 minutes


class AutoAcceptInviteIfKnocked:
    def __init__(self, config: Any, api: ModuleApi):
        # Keep a reference to the config and Module API
        self._api = api
        self._config = config
        self._event_handler = api._hs.get_event_handler()
        self._auto_invite_knocker_enabled = getattr(
            config, "auto_invite_knocker_enabled", False
        )
        # Track rooms that recently failed knock auto-invite {room_id: timestamp}
        self._knock_cooldowns: Dict[str, float] = {}

        should_run_on_this_worker = (
            config.auto_accept_invite_worker == self._api.worker_name
        )

        if not should_run_on_this_worker:
            logger.info(
                "Not accepting invites on this worker (configured: %r, here: %r)",
                config.auto_accept_invite_worker,
                self._api.worker_name,
            )
            return

        logger.info(
            "Accepting invites on this worker (here: %r)", self._api.worker_name
        )

        # Register the callback.
        self._api.register_third_party_rules_callbacks(
            on_new_event=self.on_new_event,
        )

    async def on_new_event(self, event: EventBase, _: StateMap[EventBase]) -> None:
        # Only handle membership state events
        if event.type != "m.room.member" or not event.is_state():
            return

        # Handle invites for local users: auto-accept if the user previously knocked
        if event.membership == "invite" and self._api.is_mine(event.state_key):
            is_direct_message = event.content.get("is_direct", False)

            has_previously_knocked = await self._has_user_previously_knocked(
                inviter=event.sender,
                invitee=event.state_key,
                room_id=event.room_id,
            )
            logger.debug(
                "User %s has previously knocked on room %s: %s",
                event.state_key,
                event.room_id,
                has_previously_knocked,
            )

            if has_previously_knocked:
                # Make the user join the room. We run this as a background process
                # to circumvent a race condition that occurs when responding to
                # invites over federation (see
                # https://github.com/matrix-org/synapse-auto-accept-invite/issues/12)
                await run_as_background_process(
                    "retry_make_join",
                    self._retry_make_join,
                    event.state_key,
                    event.state_key,
                    event.room_id,
                    "join",
                    bg_start_span=False,
                )

                if is_direct_message:
                    # Mark this room as a direct message!
                    await self._mark_room_as_direct_message(
                        event.state_key, event.sender, event.room_id
                    )
            return

        # Handle knocks: automatically find an inviter and invite the
        # knocker so the auto-accept flow above completes the join.
        # Only enabled when auto_invite_knocker_enabled is True in config.
        if event.membership == "knock" and self._auto_invite_knocker_enabled:
            # Check cooldown — skip if this room recently failed
            now = time.monotonic()
            last_failure = self._knock_cooldowns.get(event.room_id, 0)
            if now - last_failure < _KNOCK_COOLDOWN_SECONDS:
                logger.info(
                    "Skipping auto-invite for knocker %s in room %s "
                    "(cooldown active, %ds remaining)",
                    event.state_key,
                    event.room_id,
                    int(_KNOCK_COOLDOWN_SECONDS - (now - last_failure)),
                )
                return

            logger.info(
                "User %s knocked on room %s, attempting to auto-invite",
                event.state_key,
                event.room_id,
            )
            await run_as_background_process(
                "auto_invite_knocker",
                self._auto_invite_knocker,
                event.state_key,
                event.room_id,
                bg_start_span=False,
            )

    async def _mark_room_as_direct_message(
        self, user_id: str, dm_user_id: str, room_id: str
    ) -> None:
        """
        Marks a room (`room_id`) as a direct message with the counterparty `dm_user_id`
        from the perspective of the user `user_id`.
        """

        # This is a dict of User IDs to tuples of Room IDs
        # (get_global will return a frozendict of tuples as it freezes the data,
        # but we should accept either frozen or unfrozen variants.)
        # Be careful: we convert the outer frozendict into a dict here,
        # but the contents of the dict are still frozen (tuples in lieu of lists,
        # etc.)
        dm_map: Dict[str, tuple[str, ...]] = dict(
            await self._api.account_data_manager.get_global(
                user_id, ACCOUNT_DATA_DIRECT_MESSAGE_LIST
            )
            or {}
        )

        if dm_user_id not in dm_map:
            dm_map[dm_user_id] = (room_id,)
        else:
            dm_rooms_for_user = dm_map[dm_user_id]
            if not isinstance(dm_rooms_for_user, (tuple, list)):
                # Don't mangle the data if we don't understand it.
                logger.warning(
                    "Not marking room as DM for auto-accepted invitation; "
                    "dm_map[%r] is a %s not a list.",
                    type(dm_rooms_for_user),
                    dm_user_id,
                )
                return

            dm_map[dm_user_id] = tuple(dm_rooms_for_user) + (room_id,)

        await self._api.account_data_manager.put_global(
            user_id, ACCOUNT_DATA_DIRECT_MESSAGE_LIST, dm_map
        )

    async def _has_user_previously_knocked(
        self, inviter: str, invitee: str, room_id: str
    ) -> bool:
        """
        Check if a user has previously knocked on a room by looking at room membership history.

        Args:
            user_id: The user ID to check for previous knocks
            room_id: The room ID to check knock history for

        Returns:
            True if the user's most recent membership event is a "knock", False otherwise
        """
        try:
            # Get the membership events for this user in this room
            membership_events_iter = await self._api.get_state_events_in_room(
                room_id, types=[("m.room.member", invitee)]
            )
            membership_events = list(membership_events_iter)

            if not membership_events:
                return False

            membership_event = membership_events[0]

            membership = getattr(
                membership_event,
                "membership",
                getattr(membership_event, "content", {}).get("membership", None),
            )

            if not membership:
                return False

            if membership == "knock":
                return True

            if membership == "invite":
                # If the most recent event is an invite, check if it replaces a knock
                replaces_state = getattr(membership_event, "unsigned", {}).get(
                    "replaces_state", None
                )
                if isinstance(replaces_state, str):
                    event = await self._event_handler.get_event(
                        user=UserID.from_string(inviter),
                        room_id=room_id,
                        event_id=replaces_state,
                    )
                    if event:
                        event_content = getattr(event, "content", {})
                        if event_content.get("membership") == "knock":
                            return True

            return False
        except Exception as e:
            # If we can't determine knock history, err on the side of caution
            logger.error(
                "Unable to determine knock history for user %s in room %s: %s",
                invitee,
                room_id,
                e,
            )
            return False

    async def _retry_make_join(
        self, sender: str, target: str, room_id: str, new_membership: str
    ) -> None:
        """
        Retry sending the `make_join` request with increasing backoff.
        Works around a race condition when receiving invites over federation.

        Args:
            sender: the user performing the membership change
            target: the for whom the membership is changing
            room_id: room id of the room to join to
            new_membership: the type of membership event (in this case will be "join")
        """

        sleep = 0
        retries = 0
        max_retries = 3
        join_event = None

        while retries < max_retries:
            try:
                await self._api.sleep(sleep)
                join_event = await self._api.update_room_membership(
                    sender=sender,
                    target=target,
                    room_id=room_id,
                    new_membership=new_membership,
                )
            except Exception as e:
                logger.warning(
                    "update_room_membership failed for %s in room %s "
                    "(attempt %d/%d): %s",
                    target,
                    room_id,
                    retries + 1,
                    max_retries,
                    e,
                )
                sleep = 2**retries
                retries += 1

            if join_event is not None:
                break

    async def _auto_invite_knocker(self, knocker_user_id: str, room_id: str) -> None:
        """
        Automatically invite a user who knocked on a room.

        Locates a room member with invite power and sends the invite.
        The existing invite auto-accept logic will then detect that the
        invite replaced a knock and auto-join the user.

        If no user has sufficient power to invite, this is a no-op.
        On failure, sets a cooldown to avoid hammering the same room.
        """
        try:
            await invite_user_to_room(
                api=self._api,
                user_id=knocker_user_id,
                room_id=room_id,
            )
        except Exception as e:
            logger.error(
                "Failed to auto-invite knocker %s to room %s: %s",
                knocker_user_id,
                room_id,
                e,
            )
            # Set cooldown so we don't retry this room immediately
            self._knock_cooldowns[room_id] = time.monotonic()
