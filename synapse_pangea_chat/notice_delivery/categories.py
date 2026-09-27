"""The message catalog's refusal grain, and the learner's refusal state.

Category names are the org catalog's (user-communication-controls); this
module never defines a new one. Refusal state is a single global account-data
event per user, ``pangea.communication_preferences``, which the in-app
preference screen and the emailed unsubscribe link both write.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, Mapping, Optional

import attr

COMMUNICATION_PREFERENCES_ACCOUNT_DATA_TYPE = "pangea.communication_preferences"
PREFERENCES_VERSION = 1

EVENT_CATEGORIES: FrozenSet[str] = frozenset(
    {"credential", "missed_message", "course_invite"}
)
NUDGE_CATEGORIES: FrozenSet[str] = frozenset(
    {
        "activity_nudges",
        "suggestions",
        "onboarding_nudges",
        "teacher_setup",
        "weekly_class_report",
    }
)
MARKETING_CATEGORIES: FrozenSet[str] = frozenset({"trial_marketing", "campaigns"})
CATEGORIES: FrozenSet[str] = EVENT_CATEGORIES | NUDGE_CATEGORIES | MARKETING_CATEGORIES

# Credential mail cannot be switched off; everything else can.
NOT_REFUSABLE: FrozenSet[str] = frozenset({"credential"})
REFUSABLE_CATEGORIES: FrozenSet[str] = CATEGORIES - NOT_REFUSABLE
# The global off covers every nudge and marketing category and nothing
# event-triggered: a person who says "stop nudging me" still gets told when a
# human writes to them.
GLOBAL_OFF_CATEGORIES: FrozenSet[str] = NUDGE_CATEGORIES | MARKETING_CATEGORIES

# Categories the bot delivers through this module. Event categories are
# Synapse's own mail (verification, missed messages, invites); the bot never
# asks this module to send them.
DELIVERABLE_CATEGORIES: FrozenSet[str] = NUDGE_CATEGORIES | frozenset(
    {"trial_marketing"}
)

SOURCE_UNSUBSCRIBE_LINK = "unsubscribe_link"
SOURCE_APP = "app"


@attr.s(auto_attribs=True, frozen=True)
class CommunicationPreferences:
    refused: FrozenSet[str] = frozenset()
    all_off: bool = False
    updated_ts: Optional[int] = None
    source: Optional[str] = None


def parse_preferences(content: Optional[Mapping[str, Any]]) -> CommunicationPreferences:
    """Read the account-data event; anything malformed reads as no refusals.

    Reading a broken event as "refuse everything" would silence a person who
    never asked for silence, and reading it as "refuse nothing" only costs a
    nudge they can refuse again. The permissive side is the safe one.
    """
    if not isinstance(content, Mapping):
        return CommunicationPreferences()
    raw_refused = content.get("refused")
    refused: set = set()
    # Synapse's account-data manager freezes what it hands modules, so the JSON
    # list arrives as a tuple; accept either shape, never a bare string.
    if isinstance(raw_refused, (list, tuple)):
        refused = {
            c for c in raw_refused if isinstance(c, str) and c in REFUSABLE_CATEGORIES
        }
    updated_ts = content.get("updated_ts")
    source = content.get("source")
    return CommunicationPreferences(
        refused=frozenset(refused),
        all_off=content.get("all_off") is True,
        updated_ts=updated_ts if isinstance(updated_ts, int) else None,
        source=source if isinstance(source, str) else None,
    )


def is_refused(preferences: CommunicationPreferences, category: str) -> bool:
    if category in NOT_REFUSABLE:
        return False
    if preferences.all_off and category in GLOBAL_OFF_CATEGORIES:
        return True
    return category in preferences.refused


def with_refusal(
    preferences: CommunicationPreferences,
    *,
    categories: Iterable[str] = (),
    all_off: Optional[bool] = None,
    now_ms: int,
    source: str,
) -> Dict[str, Any]:
    """Return the account-data content after adding refusals.

    Adding is all the unsubscribe surface ever does; re-enabling a category is
    the in-app screen's job, where the person is signed in.
    """
    refused = set(preferences.refused)
    for category in categories:
        if category in REFUSABLE_CATEGORIES:
            refused.add(category)
    return {
        "version": PREFERENCES_VERSION,
        "refused": sorted(refused),
        "all_off": preferences.all_off if all_off is None else bool(all_off),
        "updated_ts": now_ms,
        "source": source,
    }
