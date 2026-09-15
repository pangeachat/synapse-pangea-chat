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

import inspect
import logging
import re
from typing import Any, Mapping, Optional, Tuple, Union

from synapse.api.errors import Codes
from synapse.events import EventBase
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.module_api import NOT_SPAM, ModuleApi

from synapse_pangea_chat.moderation.choreo_client import (
    ModerationCheckError,
    moderate_text,
)
from synapse_pangea_chat.moderation.exempt import (
    CONFIG_KEY,
    ExemptGlobError,
    glob_match,
    validate_glob,
)
from synapse_pangea_chat.moderation.log_safety import (
    error_site,
    new_digest_key,
    sender_digest,
)
from synapse_pangea_chat.moderation.tier1_prefilter import check_text
from synapse_pangea_chat.room_preview import PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE

logger = logging.getLogger("synapse.modules.synapse_pangea_chat.moderation")

# Synapse 1.159 inserted `server_name` as the second positional parameter of
# `run_as_background_process`; 1.124 has no such parameter. Calling the 1.124
# shape on 1.159 passes the coroutine function as `server_name` and the event
# as `func`, which raises a TypeError that `on_new_event`'s fail-open handler
# swallows - Tier 2 silently never runs. COMPAT.yml requires both pins, so
# the call is adapted rather than pinned, using the pattern already in
# delete_user.py, export_user_data.py and backfill_l2.py. Deduplicating the
# four copies into a shared module is tracked separately; adding a fourth
# copy that drifts is the failure this comment exists to prevent.
_RUN_AS_BG_SUPPORTS_SERVER_NAME = (
    "server_name" in inspect.signature(run_as_background_process).parameters
)


def _background_process_args(homeserver: Any, desc: str, func: Any) -> Tuple[Any, ...]:
    if _RUN_AS_BG_SUPPORTS_SERVER_NAME:
        return (desc, homeserver.hostname, func)
    return (desc, func)


_TEXTUAL_MSGTYPES = ("m.text", "m.emote", "m.notice")
# Media messages carry a caption (or filename) in `body`, which readers see
# exactly like message text — so it is moderated too. Without this, any
# abusive text sent as an image caption bypassed both tiers entirely.
_CAPTION_MSGTYPES = ("m.image", "m.video", "m.file", "m.audio")
# Tags are stripped from `formatted_body`; a message whose plain `body` is
# innocuous can carry the real payload in its HTML twin.
_HTML_TAGS = re.compile(r"<[^>]+>")


class ChatModeration:
    """Registers the enabled moderation tiers. Constructed only when at least
    one tier is enabled (see PangeaChat.__init__), mirroring the module's
    flag-gated sub-module convention."""

    def __init__(self, api: ModuleApi, config: Any):
        self._api = api
        self._config = config
        # Validated again here, not only at config-parse time: this is the
        # single place the values are turned into matching behaviour, so a
        # value that reached it unvalidated must fail startup rather than
        # quietly exempt the wrong senders. The container's type is checked
        # first because a bare string is iterable: `"@bot:*"` would become
        # six single-character globs, one of which is `*`, and `*` exempts
        # every sender on every homeserver.
        exempt_globs = config.moderation_exempt_user_id_globs
        if isinstance(exempt_globs, str) or not isinstance(exempt_globs, (list, tuple)):
            raise ExemptGlobError(
                f'Config "moderation.{CONFIG_KEY}" must be a list of strings, '
                f"got {type(exempt_globs).__name__}"
            )
        for exempt_glob in exempt_globs:
            validate_glob(exempt_glob)
        self._exempt_globs = list(exempt_globs)
        # Keyed per instance so a Matrix ID cannot be recovered from a log
        # line by enumeration; see moderation.log_safety.
        self._log_digest_key = new_digest_key()

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
        if content.get("msgtype") not in _TEXTUAL_MSGTYPES + _CAPTION_MSGTYPES:
            return None
        parts = []
        body = content.get("body")
        if isinstance(body, str) and body.strip():
            parts.append(body)
        formatted = content.get("formatted_body")
        if isinstance(formatted, str) and formatted.strip():
            parts.append(_HTML_TAGS.sub(" ", formatted))
        text = "\n".join(parts).strip()
        return text or None

    def _is_exempt_sender(self, sender: str) -> bool:
        # Whole-string, both ends. A prefix match here exempted any sender
        # whose Matrix ID merely started the same way, which skipped both
        # tiers - see moderation.exempt.
        return any(glob_match(g, sender) for g in self._exempt_globs)

    def _sender_digest(self, sender: str) -> str:
        """A log-safe stand-in for a sender's Matrix ID."""
        return sender_digest(sender, self._log_digest_key)

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
                # No Matrix ID and no message text: this record says that
                # somebody in this room tripped this rule, which is what an
                # operator needs, and stops short of saying who or what. The
                # blocked event is never persisted, so there is no event id
                # to give either; the digest is what links repeated blocks
                # from one sender within this process.
                logger.info(
                    "tier1 blocked an event in %s (rule=%s, sender_digest=%s)",
                    event.room_id,
                    reason,
                    self._sender_digest(event.sender),
                )
                return Codes.FORBIDDEN
            return NOT_SPAM
        except Exception as exc:
            # silent-ok: fail-open by contract — a moderation bug must never
            # block all sends; the failure is logged and Tier 2 still runs.
            #
            # Type and site, not `logger.exception`: the traceback ends with
            # the exception's own message, and anything raised while matching
            # a message body is liable to quote that body.
            logger.warning(
                "tier1 pre-filter failed at %s (%s); allowing event",
                error_site(exc),
                type(exc).__name__,
            )
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
            run_as_background_process(
                *_background_process_args(
                    self._api._hs,
                    "pangea_moderation_tier2",
                    self._check_and_redact,
                ),
                event,
                text,
            )
        except Exception as exc:
            # silent-ok: fail-open by contract; observe-only hook, so the
            # only cost of a failure here is a missed check — logged by type
            # and site rather than as a traceback, for the reason given on
            # the Tier-1 handler above.
            logger.warning(
                "tier2 dispatch failed for %s at %s (%s)",
                event.event_id,
                error_site(exc),
                type(exc).__name__,
            )

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
        try:
            await self._api.create_and_send_event_into_room(
                {
                    "type": "m.room.redaction",
                    "room_id": event.room_id,
                    "sender": event.sender,
                    "redacts": event.event_id,
                    "content": {"redacts": event.event_id, "reason": reason},
                }
            )
        except Exception as exc:
            # A redaction send can fail for reasons that are ordinary rather
            # than exceptional: the sender has left, been kicked or been
            # banned (room auth checks membership before it checks redaction
            # rights), the room raises the send level for redactions, or the
            # sender is remote and the module cannot author an event as them.
            #
            # It is caught HERE, and not allowed to escape, because this
            # coroutine runs under `run_as_background_process`, which calls
            # `logger.exception` on whatever reaches it. Synapse's own
            # "User <mxid> not in room <room>" carries the Matrix ID, so
            # letting the exception through would put an identified sender
            # into a plaintext log by a route none of this module's own
            # format strings mention.
            #
            # Counting these failures and escalating the legitimate ones is
            # separate work; today the message stays up and the failure is
            # visible, which is what the previous behaviour was missing.
            logger.warning(
                "tier2 redaction failed for %s in %s at %s (%s); message stays",
                event.event_id,
                event.room_id,
                error_site(exc),
                type(exc).__name__,
            )


def _normalize_category(category: str) -> str:
    """Map an OpenAI moderation category name onto the orchestrator's flag
    vocabulary where the two overlap (`self-harm/intent` -> `self_harm`),
    passing through normalized names otherwise, so both moderation code
    paths speak one vocabulary."""
    return category.split("/", 1)[0].replace("-", "_")
