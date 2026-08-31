from __future__ import annotations

import logging

from synapse.api.constants import EventTypes
from synapse.module_api import ModuleApi
from synapse.push.rulekinds import PRIORITY_CLASS_MAP

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.analytics_push_suppression"
)

ANALYTICS_ROOM_TYPE = "p.analytics"
# Marks an invite as analytics plumbing. Push rule conditions cannot read room
# state, so the event itself has to say that it is one.
ANALYTICS_INVITE_REASON = "p.analytics_request"
# Same rule id the client writes, so the server-side copy and the client-side
# copy are one rule rather than two that can drift apart.
ANALYTICS_INVITE_PUSH_RULE_ID = "p.rule.analytics_invite"

_NAMESPACED_PUSH_RULE_ID = f"global/override/{ANALYTICS_INVITE_PUSH_RULE_ID}"
# Must be an override: the default `.m.rule.invite_for_me` that would otherwise
# push is itself an override, and no lower-class rule can outrank it.
_OVERRIDE_PRIORITY_CLASS = PRIORITY_CLASS_MAP["override"]
_PUSH_RULE_CONDITIONS = [
    {"kind": "event_match", "key": "type", "pattern": EventTypes.Member},
    {
        "kind": "event_match",
        "key": "content.reason",
        "pattern": ANALYTICS_INVITE_REASON,
    },
]
_PUSH_RULE_ACTIONS = ["dont_notify"]


async def is_analytics_room(api: ModuleApi, room_id: str) -> bool:
    create_event = (
        await api._hs.get_storage_controllers().state.get_current_state_event(
            room_id, EventTypes.Create, ""
        )
    )
    return (
        create_event is not None
        and create_event.content.get("type") == ANALYTICS_ROOM_TYPE
    )


async def ensure_analytics_invite_push_rule(api: ModuleApi, user_id: str) -> None:
    """Give `user_id` the push rule that silences analytics invites.

    Idempotent. Installing it server-side is what makes the suppression hold for
    an account that has never run a client version that writes the rule itself.
    """
    store = api._hs.get_datastores().main

    existing = await store.db_pool.simple_select_one_onecol(
        table="push_rules",
        keyvalues={"user_name": user_id, "rule_id": _NAMESPACED_PUSH_RULE_ID},
        retcol="rule_id",
        allow_none=True,
        desc="analytics_invite_push_rule_exists",
    )
    if existing is not None:
        return

    await store.add_push_rule(
        user_id=user_id,
        rule_id=_NAMESPACED_PUSH_RULE_ID,
        priority_class=_OVERRIDE_PRIORITY_CLASS,
        conditions=_PUSH_RULE_CONDITIONS,
        actions=_PUSH_RULE_ACTIONS,
    )
    api._hs.get_push_rules_handler().notify_user(user_id)
    logger.info("Installed analytics invite push rule for %s", user_id)
