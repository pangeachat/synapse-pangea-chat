"""Keep Synapse's own notification pipeline off the bot's notices.

A bot nudge is a ``p.room.notice`` event in the person's bot DM. The bot
delivers it deliberately — direct push, or the nudge email — so Synapse's
rule-driven pipeline must not also act on it: the email pusher would mail it as
a missed message (with no unsubscribe, on the transactional stream) and an HTTP
pusher would push it a second time. A per-user override rule that says
``dont_notify`` for that event type closes both paths. Installed lazily, the
first time a nudge is delivered to the person, and idempotent — the same shape
as the analytics-invite suppression rule.
"""

from __future__ import annotations

import logging
from typing import Any, Set

from synapse.module_api import ModuleApi
from synapse.push.rulekinds import PRIORITY_CLASS_MAP

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.nudge_delivery.push_rule"
)

BOT_NOTICE_EVENT_TYPE = "p.room.notice"
BOT_NOTICE_PUSH_RULE_ID = "p.rule.bot_notice"
_NAMESPACED_RULE_ID = f"global/override/{BOT_NOTICE_PUSH_RULE_ID}"
_OVERRIDE_PRIORITY_CLASS = PRIORITY_CLASS_MAP["override"]
_CONDITIONS = [
    {"kind": "event_match", "key": "type", "pattern": BOT_NOTICE_EVENT_TYPE},
]
_ACTIONS = ["dont_notify"]

# Users whose rule this process has already confirmed. A restart re-checks the
# database once per user, which is the cheap direction to be wrong in.
_confirmed_users: Set[str] = set()


async def _has_rule(store: Any, user_id: str) -> bool:
    existing = await store.db_pool.simple_select_one_onecol(
        table="push_rules",
        keyvalues={"user_name": user_id, "rule_id": _NAMESPACED_RULE_ID},
        retcol="rule_id",
        allow_none=True,
        desc="bot_notice_push_rule_exists",
    )
    return existing is not None


async def ensure_bot_notice_push_rule(api: ModuleApi, user_id: str) -> bool:
    """Give ``user_id`` the rule; return True if it was installed just now."""
    if user_id in _confirmed_users:
        return False
    store = api._hs.get_datastores().main
    if await _has_rule(store, user_id):
        _confirmed_users.add(user_id)
        return False
    try:
        await store.add_push_rule(
            user_id=user_id,
            rule_id=_NAMESPACED_RULE_ID,
            priority_class=_OVERRIDE_PRIORITY_CLASS,
            conditions=_CONDITIONS,
            actions=_ACTIONS,
        )
    except Exception:
        # Two deliveries racing for the same never-seen user both try to
        # insert; the unique index makes the loser fail after the winner
        # succeeded, which is the outcome both wanted.
        if not await _has_rule(store, user_id):
            raise
        _confirmed_users.add(user_id)
        return False
    api._hs.get_push_rules_handler().notify_user(user_id)
    _confirmed_users.add(user_id)
    logger.info("Installed bot-notice push suppression rule for %s", user_id)
    return True


def reset_confirmed_users_for_tests() -> None:
    _confirmed_users.clear()
