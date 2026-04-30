from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from synapse.module_api import ModuleApi

from synapse_pangea_chat.preview_with_code.constants import ADMIN_POWER_LEVEL

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.preview_with_code.get_preview"
)


def _content_dict(event: Any) -> Dict[str, Any]:
    content = getattr(event, "content", None)
    if isinstance(content, dict):
        return content
    return {}


def _serialize_event(event: Any) -> Dict[str, Any]:
    return {
        "type": getattr(event, "type", None),
        "state_key": getattr(event, "state_key", None),
        "sender": getattr(event, "sender", None),
        "origin_server_ts": getattr(event, "origin_server_ts", None),
        "event_id": getattr(event, "event_id", None),
        "content": dict(_content_dict(event)),
    }


def _coerce_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def get_room_preview_for_code(
    room_id: str,
    api: ModuleApi,
    pangea_state_event_types: List[str],
) -> Optional[Dict[str, Any]]:
    """
    Build a single-room preview from current room state. Read-only.

    Returns a dict with top-level Matrix room metadata, the joined-admin list
    (members with power level == 100), and a bag of pangea.* state events
    keyed by event type → state_key → full event JSON.

    Returns None if room state cannot be read.
    """
    try:
        state = await api.get_room_state(room_id=room_id)
    except Exception as e:
        logger.error("Failed to read room state for %s: %s", room_id, e)
        return None

    name_evt = state.get(("m.room.name", ""))
    name = _content_dict(name_evt).get("name") if name_evt is not None else None

    avatar_evt = state.get(("m.room.avatar", ""))
    avatar_url = (
        _content_dict(avatar_evt).get("url") if avatar_evt is not None else None
    )

    topic_evt = state.get(("m.room.topic", ""))
    topic = _content_dict(topic_evt).get("topic") if topic_evt is not None else None

    admin_user_ids: List[str] = []
    pl_evt = state.get(("m.room.power_levels", ""))
    if pl_evt is not None:
        users_pl = _content_dict(pl_evt).get("users", {})
        if isinstance(users_pl, dict):
            for user_id, level in users_pl.items():
                if not isinstance(user_id, str):
                    continue
                if _coerce_int(level) == ADMIN_POWER_LEVEL:
                    admin_user_ids.append(user_id)

    admins: List[Dict[str, Any]] = []
    for user_id in sorted(set(admin_user_ids)):
        member_evt = state.get(("m.room.member", user_id))
        if member_evt is None:
            continue
        member_content = _content_dict(member_evt)
        if member_content.get("membership") != "join":
            continue
        admins.append(
            {
                "user_id": user_id,
                "avatar_url": member_content.get("avatar_url"),
            }
        )

    state_events: Dict[str, Dict[str, Dict[str, Any]]] = {}
    pangea_types = set(pangea_state_event_types)
    for (event_type, state_key), event in state.items():
        if event_type not in pangea_types:
            continue
        bucket = state_events.setdefault(event_type, {})
        key = state_key if state_key else "default"
        bucket[key] = _serialize_event(event)

    return {
        "room_id": room_id,
        "name": name,
        "avatar_url": avatar_url,
        "topic": topic,
        "admins": admins,
        "state_events": state_events,
    }
