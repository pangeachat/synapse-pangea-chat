"""Server-side chat moderation (trust-and-safety: engine built, rollout here).

Two tiers, split by what each can afford to do in the send path:

- Tier 1 (`check_event_for_spam`, pre-persist, CAN reject): deterministic
  pattern checks — phone numbers, street addresses, profanity wordlist.
  Sub-millisecond, so blocking inline is safe; MUST fail open.
- Tier 2 (`on_new_event`, post-persist, observe-only): calls the shared
  choreo moderation handler for the nuanced categories and redacts flagged
  messages after the fact. Runs as a background process so a slow provider
  never back-pressures event persistence.

Redactions are sent AS THE OFFENDING SENDER (self-redaction): the module-API
send path enforces normal room auth, and a user may always redact their own
message, so this works in every room — DMs included — without requiring a
privileged member. The moderation reason rides on the redaction event.

Activity rooms (those carrying an activity-plan state event) are skipped by
Tier 2: the conversation orchestrator already bundles moderation there, and
double-moderating would double-redact and double-spend.

Design doc: .github/instructions/moderation.instructions.md (repo-level) and
the org trust-and-safety doc it descends from.
"""

import logging
import re
from typing import Any, Mapping, Optional, Tuple, Union

from synapse.api.errors import Codes
from synapse.events import EventBase
from synapse.module_api import NOT_SPAM, ModuleApi

from synapse_pangea_chat.moderation.choreo_client import (
    ModerationCheckError,
    moderate_text,
)
from synapse_pangea_chat.moderation.tier1_prefilter import check_text
from synapse_pangea_chat.room_preview import PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE

logger = logging.getLogger("synapse.modules.synapse_pangea_chat.moderation")

_TEXTUAL_MSGTYPES = ("m.text", "m.emote", "m.notice")


class ChatModeration:
    """Registers the enabled moderation tiers. Constructed only when at least
    one tier is enabled (see PangeaChat.__init__), mirroring the module's
    flag-gated sub-module convention."""

    def __init__(self, api: ModuleApi, config: Any):
        self._api = api
        self._config = config
        self._exempt_patterns = [
            re.compile(p) for p in config.moderation_exempt_user_id_patterns
        ]

        if config.moderation_tier1_enabled:
            api.register_spam_checker_callbacks(
                check_event_for_spam=self.check_event_for_spam,
            )
        if config.moderation_tier2_enabled:
            api.register_third_party_rules_callbacks(
                on_new_event=self.on_new_event,
            )

    # ------------------------------------------------------------------
    # Shared filters
    # ------------------------------------------------------------------

    def _extract_text(self, event: EventBase) -> Optional[str]:
        """The moderatable text of a message event, or None to skip it.

        Handles edits by preferring m.new_content (the replacement text is
        what readers will see)."""
        if event.type != "m.room.message":
            return None
        content = event.content or {}
        new_content = content.get("m.new_content")
        if isinstance(new_content, dict):
            content = new_content
        if content.get("msgtype") not in _TEXTUAL_MSGTYPES:
            return None
        body = content.get("body")
        if not isinstance(body, str) or not body.strip():
            return None
        return body

    def _is_exempt_sender(self, sender: str) -> bool:
        return any(p.match(sender) for p in self._exempt_patterns)

    # ------------------------------------------------------------------
    # Tier 1 — deterministic pre-filter (blocks before persist)
    # ------------------------------------------------------------------

    async def check_event_for_spam(self, event: EventBase) -> Union[str, Codes, bool]:
        try:
            text = self._extract_text(event)
            if text is None or self._is_exempt_sender(event.sender):
                return NOT_SPAM
            reason = check_text(text, self._config.moderation_tier1_phone_regions)
            if reason is not None:
                logger.info(
                    "tier1 blocked event from %s in %s (reason=%s)",
                    event.sender,
                    event.room_id,
                    reason,
                )
                return Codes.FORBIDDEN
            return NOT_SPAM
        except Exception:
            # silent-ok: fail-open by contract — a moderation bug must never
            # block all sends; the failure is logged and Tier 2 still runs.
            logger.exception("tier1 pre-filter failed; allowing event")
            return NOT_SPAM

    # ------------------------------------------------------------------
    # Tier 2 — LLM moderation (redacts after persist)
    # ------------------------------------------------------------------

    async def on_new_event(
        self,
        event: EventBase,
        state_events: Mapping[Tuple[str, str], EventBase],
    ) -> None:
        """Fire-and-forget the Tier 2 check so event persistence never waits
        on an HTTP round-trip."""
        try:
            text = self._extract_text(event)
            if text is None or self._is_exempt_sender(event.sender):
                return
            if self._room_has_activity_plan(state_events):
                # The conversation orchestrator owns moderation in activity
                # rooms; checking here would double-moderate.
                return
            from synapse.metrics.background_process_metrics import (
                run_as_background_process,
            )

            run_as_background_process(
                "pangea_moderation_tier2",
                self._check_and_redact,
                event,
                text,
            )
        except Exception:
            # silent-ok: fail-open by contract; observe-only hook, so the
            # only cost of a failure here is a missed check — logged.
            logger.exception("tier2 dispatch failed for %s", event.event_id)

    @staticmethod
    def _room_has_activity_plan(
        state_events: Mapping[Tuple[str, str], EventBase],
    ) -> bool:
        return any(
            ev_type == PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE
            for (ev_type, _state_key) in state_events.keys()
        )

    async def _check_and_redact(self, event: EventBase, text: str) -> None:
        try:
            result = await moderate_text(
                text,
                base_url=self._config.moderation_choreo_base_url,
                access_token=self._config.moderation_choreo_access_token,
            )
        except ModerationCheckError as e:
            # silent-ok: fail-open by contract — the choreo handler itself
            # fails open on provider errors, and a transport failure here
            # must not crash the background task. Logged with reason type.
            logger.warning(
                "tier2 moderation check unavailable for %s: %s", event.event_id, e
            )
            return

        if not result.get("flagged"):
            return
        categories = result.get("categories") or []
        category = _normalize_category(categories[0]) if categories else "flagged"
        logger.info(
            "tier2 flagged event %s in %s (category=%s); redacting",
            event.event_id,
            event.room_id,
            category,
        )
        reason = f"{self._config.moderation_redaction_reason_prefix}: {category}"
        # Self-redaction: sent as the offending sender so room power levels
        # can never block it (redacting one's own message needs only the
        # default event-send level). `redacts` is provided both top-level
        # (room versions < 11) and in content (v11+); Synapse's event
        # creation code copies to the right place for the room version.
        await self._api.create_and_send_event_into_room(
            {
                "type": "m.room.redaction",
                "room_id": event.room_id,
                "sender": event.sender,
                "redacts": event.event_id,
                "content": {"redacts": event.event_id, "reason": reason},
            }
        )


def _normalize_category(category: str) -> str:
    """Map an OpenAI moderation category name onto the orchestrator's flag
    vocabulary where the two overlap (`self-harm/intent` -> `self_harm`),
    passing through normalized names otherwise, so both moderation code
    paths speak one vocabulary."""
    return category.split("/", 1)[0].replace("-", "_")
