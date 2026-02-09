import logging

from synapse.module_api import ModuleApi

from synapse_pangea_chat.delete_room.constants import (
    EVENT_TYPE_M_SPACE_CHILD,
    EVENT_TYPE_M_SPACE_PARENT,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.delete_room.cleanup_space_relationships"
)


async def cleanup_space_relationships(
    api: ModuleApi, room_id: str, sender_user_id: str
) -> None:
    """
    Clean up space relationships when a room is deleted.

    Args:
        api: The ModuleApi instance
        room_id: The ID of the room being deleted
        sender_user_id: The user ID to use as the sender for the cleanup events

    This function:
    1. Removes m.space.parent events from the deleted room
    2. Removes m.space.child events from parent spaces that reference the deleted room
    """
    try:
        # Step 1: Get all m.space.parent events from the deleted room
        # These tell us which spaces this room was a child of
        space_parent_events = await api.get_room_state(
            room_id=room_id,
            event_filter=[(EVENT_TYPE_M_SPACE_PARENT, None)],
        )

        parent_space_ids = []
        for state_event in space_parent_events.values():
            if state_event.type == EVENT_TYPE_M_SPACE_PARENT:
                # The state_key is the parent space ID
                parent_space_id = state_event.state_key
                if parent_space_id:
                    parent_space_ids.append(parent_space_id)

        logger.info(
            "Found %d parent spaces for room %s", len(parent_space_ids), room_id
        )

        # Step 2: Remove m.space.child events from parent spaces
        for parent_space_id in parent_space_ids:
            try:
                # Check if the parent space has a m.space.child event for this room
                space_child_events = await api.get_room_state(
                    room_id=parent_space_id,
                    event_filter=[(EVENT_TYPE_M_SPACE_CHILD, room_id)],
                )

                # If there's a m.space.child event for this room, remove it
                if space_child_events:
                    logger.info(
                        "Removing m.space.child event for %s from parent space %s",
                        room_id,
                        parent_space_id,
                    )
                    # Send an empty state event to remove the relationship
                    await api.create_and_send_event_into_room(
                        {
                            "type": EVENT_TYPE_M_SPACE_CHILD,
                            "state_key": room_id,
                            "room_id": parent_space_id,
                            "sender": sender_user_id,
                            "content": {},  # Empty content removes the state
                        }
                    )

            except Exception as e:
                logger.error(
                    "Failed to remove m.space.child event from parent space %s: %s",
                    parent_space_id,
                    e,
                )
                # Continue with other parent spaces even if one fails
                continue

        # Step 3: Remove m.space.parent events from the deleted room
        # This happens automatically when the room is purged, but we can be explicit
        for state_event in space_parent_events.values():
            if state_event.type == EVENT_TYPE_M_SPACE_PARENT:
                try:
                    logger.info(
                        "Removing m.space.parent event for %s from room %s",
                        state_event.state_key,
                        room_id,
                    )
                    await api.create_and_send_event_into_room(
                        {
                            "type": EVENT_TYPE_M_SPACE_PARENT,
                            "state_key": state_event.state_key,
                            "room_id": room_id,
                            "sender": sender_user_id,
                            "content": {},  # Empty content removes the state
                        }
                    )
                except Exception as e:
                    logger.error(
                        "Failed to remove m.space.parent event from room %s: %s",
                        room_id,
                        e,
                    )
                    # Continue with other events even if one fails
                    continue

    except Exception as e:
        logger.error(
            "Failed to cleanup space relationships for room %s: %s", room_id, e
        )
        # Don't re-raise the exception as this should not block room deletion
