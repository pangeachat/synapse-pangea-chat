import logging
from typing import Any, Dict

from synapse.module_api import (
    EventBase,
    ModuleApi,
    StateMap,
    UserID,
    run_as_background_process,
)

logger = logging.getLogger(__name__)

ACCOUNT_DATA_DIRECT_MESSAGE_LIST = "m.direct"


class AutoAcceptInviteIfKnocked:
    def __init__(self, config: Any, api: ModuleApi):
        # Keep a reference to the config and Module API
        self._api = api
        self._config = config
        self._event_handler = api._hs.get_event_handler()
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
        # Check if the event is an invite for a local user.
        is_invite_for_local_user = (
            event.type == "m.room.member"
            and event.is_state()
            and event.membership == "invite"
            and self._api.is_mine(event.state_key)
        )
        if not is_invite_for_local_user:
            # Not an invite for a local user, ignore it.
            return

        is_direct_message = event.content.get("is_direct", False)

        has_previously_knocked = await self._has_user_previously_knocked(
            inviter=event.sender, invitee=event.state_key, room_id=event.room_id
        )
        logger.debug(
            "User %s has previously knocked on room %s: %s",
            event.state_key,
            event.room_id,
            has_previously_knocked,
        )

        if has_previously_knocked:
            # Make the user join the room. We run this as a background process to circumvent a race condition
            # that occurs when responding to invites over federation (see https://github.com/matrix-org/synapse-auto-accept-invite/issues/12)
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
        A function to retry sending the `make_join` request with an increasing backoff. This is
        implemented to work around a race condition when receiving invites over federation.

        Args:
            sender: the user performing the membership change
            target: the for whom the membership is changing
            room_id: room id of the room to join to
            new_membership: the type of membership event (in this case will be "join")
        """

        sleep = 0
        retries = 0
        join_event = None

        while retries < 5:
            try:
                await self._api.sleep(sleep)
                join_event = await self._api.update_room_membership(
                    sender=sender,
                    target=target,
                    room_id=room_id,
                    new_membership=new_membership,
                )
            except Exception as e:
                logger.info(
                    f"Update_room_membership raised the following exception: {e}"
                )
                sleep = 2**retries
                retries += 1

            if join_event is not None:
                break
