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
from html.parser import HTMLParser
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

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
    scrubbing_logger,
    sender_digest,
)
from synapse_pangea_chat.moderation.tier1_prefilter import check_text
from synapse_pangea_chat.room_preview import PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE

logger = scrubbing_logger("synapse.modules.synapse_pangea_chat.moderation")

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


# The relation type that makes an event a replacement. Per the Matrix spec a
# replacement is `m.relates_to.rel_type == "m.replace"` and nothing else; the
# mere presence of an `m.new_content` key means nothing, and no client renders
# `m.new_content` without the relation.
_REPLACE_REL_TYPE = "m.replace"

# Tags that produce a visual break when a client renders `formatted_body`.
# Everything else is inline, and inline elements concatenate with no gap: the
# displayed text of `4<b>1</b>5` is `415`, so that is the string the rules see.
_BLOCK_LEVEL_TAGS = frozenset(
    {
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
# Attribute values a client puts on screen. `alt` is rendered whenever an
# inline image does not load, and both are read aloud by screen readers, so
# text parked there is text a reader receives. `href` is deliberately NOT in
# this set: clients show the link's TEXT, and a URL full of digits is a
# plausible false positive for the phone rule. Recorded as a limit in
# .github/instructions/moderation.instructions.md.
_DISPLAYED_ATTRIBUTES = frozenset({"alt", "title"})


class _DisplayedText(HTMLParser):
    """Reduces `formatted_body` to the characters a reader actually sees.

    A regex (`<[^>]+>`) was the previous implementation and it fails in both
    directions. It deletes `< b and call ...` — ordinary text containing a
    less-than sign — as though it were a tag, which drops displayed text; and,
    because it never decodes entities, `&#52;15-555-2671` reaches the rules as
    an entity string while the reader sees a phone number. A real parser fixes
    both, and fixes them in the order ADR-8a(ii) requires: the tag scanner runs
    over the raw text first, so `&lt;I will kill you&gt;` becomes the visible
    text `<I will kill you>` rather than being decoded into a tag and deleted.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def _handle_tag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        if tag in _BLOCK_LEVEL_TAGS:
            self._parts.append("\n")
        for name, value in attrs:
            if name in _DISPLAYED_ATTRIBUTES and value:
                self._parts.append(f"\n{value}\n")

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self._handle_tag(tag, attrs)

    def handle_startendtag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        self._handle_tag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_LEVEL_TAGS:
            self._parts.append("\n")

    def result(self) -> str:
        return "".join(self._parts)


def _displayed_text(formatted: str) -> str:
    """The reader-visible text of an HTML body, never less than it displays."""
    parser = _DisplayedText()
    parser.feed(formatted)
    parser.close()
    text = parser.result()
    # An unterminated tag at the end of the string is consumed and discarded by
    # the parser, as a sanitiser would discard it. Its raw tail is appended all
    # the same: over-reading text nobody sees can only cost a false positive on
    # malformed HTML, while under-reading is the bypass this whole function
    # exists to prevent, and the two are not symmetric.
    last_open = formatted.rfind("<")
    if last_open != -1 and ">" not in formatted[last_open:]:
        text = f"{text}\n{formatted[last_open + 1:]}"
    return text


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

        The rule, and the only rule: **what the rules see is never less than
        what a reader sees.** Extraction that returns a subset of the displayed
        text is not a missed detection, it is a bypass any sender can trigger
        on every message, so where the displayed text is ambiguous the union of
        the candidate surfaces is moderated. Over-reading costs a false
        positive on one message; under-reading costs the tier.

        There is no msgtype allowlist, and that is the same rule again rather
        than an omission. `body` is, by definition in the Matrix spec, "a
        textual representation of the message", and clients render it for every
        msgtype - as the caption of an image, as the description of a location,
        and as the whole message for a msgtype they do not recognise. An
        allowlist of `m.text`/`m.image`/... therefore handed any sender a
        one-field bypass of both tiers: `{"msgtype": "m.not-a-real-type",
        "body": "<payload>"}` is displayed as the payload and matched an
        allowlist of nothing. The event TYPE is the gate; within
        `m.room.message`, every displayed string is read.

        An edit therefore ADDS a surface rather than replacing one (ADR-8a(0)).
        Both are displayed: modern clients render `m.new_content`, older ones
        render the outer `body` fallback (conventionally `* <new text>`), so
        choosing between them leaves whichever was not chosen unmoderated.

        And `m.new_content` is only a replacement when `m.relates_to.rel_type`
        says so. Preferring it on presence alone was a total bypass of both
        tiers: `{"msgtype": "m.text", "body": "<payload>", "m.new_content": {}}`
        has no relation, is displayed as `<payload>` by every client, and
        extracted as nothing at all.
        """
        if event.type != "m.room.message":
            return None
        # `Mapping`, not `dict`: event content is not guaranteed to be a plain
        # dict. Synapse builds events through a Rust type whose `content` is a
        # `JsonObject`, and a homeserver running with `use_frozen_dicts: true`
        # hands modules `immutabledict` values. An `isinstance(..., dict)`
        # test on either returns False, which would silently stop moderating
        # the replacement text of every edit - a bypass that fails open and
        # says nothing.
        content = event.content or {}
        if not isinstance(content, Mapping):
            return None

        surfaces: List[Mapping[str, Any]] = [content]
        new_content = content.get("m.new_content")
        if _is_replacement(content) and isinstance(new_content, Mapping):
            surfaces.append(new_content)

        parts: List[str] = []
        for surface in surfaces:
            parts.extend(_surface_text(surface))
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
        category = _summarize_categories(result.get("categories") or ())
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


def _is_replacement(content: Mapping[str, Any]) -> bool:
    """True when the event's own relation declares it a replacement.

    Tier 1 trusts the event's `rel_type` and nothing more, because deciding
    whether the relation is VALID - target exists, same room, same sender -
    needs a database read, and the send path cannot afford one (ADR-8a). The
    price is a known false positive: a bogus replacement relation makes us read
    `m.new_content` on an event no client renders that way. That direction is
    safe; the reverse is the bypass this function exists to close.
    """
    relates_to = content.get("m.relates_to")
    return (
        isinstance(relates_to, Mapping)
        and relates_to.get("rel_type") == _REPLACE_REL_TYPE
    )


def _surface_text(surface: Mapping[str, Any]) -> List[str]:
    """Every displayed string carried by one content surface."""
    parts: List[str] = []
    body = surface.get("body")
    if isinstance(body, str):
        if body.strip():
            parts.append(body)
    elif body is not None:
        # A `body` that is not a string is malformed, and malformed is not the
        # same as absent: `EventValidator` only requires `body` to be present
        # for the msgtypes it knows, and a client that renders one renders
        # `str(body)`. Dropping it on a type test is the extraction bypass in
        # its smallest form, so the value is stringified and matched.
        parts.append(str(body))
    formatted = surface.get("formatted_body")
    if isinstance(formatted, str) and formatted.strip():
        displayed = _displayed_text(formatted)
        if displayed.strip():
            parts.append(displayed)
    return parts


# The provider's documented category vocabulary, and the whole of it. A
# category name is a string chosen by a service we do not run; it reaches a log
# line and the redaction reason that lands in a room, so it is checked against
# this list rather than trusted. A response of
# `{"flagged": true, "categories": ["@alice:example.org"]}` otherwise logs that
# Matrix ID verbatim and writes it into a room, and
# `["<the message body>"]` does the same for a message body - by a route no
# review of our own format strings would find, because our format string is
# `category=%s` and looks harmless.
_PROVIDER_CATEGORIES = frozenset(
    {
        "harassment",
        "harassment/threatening",
        "hate",
        "hate/threatening",
        "illicit",
        "illicit/violent",
        "self-harm",
        "self-harm/instructions",
        "self-harm/intent",
        "sexual",
        "sexual/minors",
        "violence",
        "violence/graphic",
    }
)
# Where an unrecognised category lands: a bounded constant, never the string
# the service sent. Per ADR-7b this is what makes an unknown category a
# non-event for logs, for metric cardinality and for the redaction reason.
UNKNOWN_CATEGORY = "other"
# Used when the service flags a message and names no category at all.
UNNAMED_CATEGORY = "flagged"


def _normalize_category(category: Any) -> str:
    """Map a provider category name onto the orchestrator's flag vocabulary.

    `self-harm/intent` -> `self_harm`, so both moderation code paths speak one
    vocabulary. Anything outside the documented list - including anything that
    is not a string - becomes `other`, and the value the service sent is
    discarded here rather than carried one frame further.
    """
    if not isinstance(category, str) or category not in _PROVIDER_CATEGORIES:
        return UNKNOWN_CATEGORY
    return category.split("/", 1)[0].replace("-", "_")


def _summarize_categories(categories: Iterable[Any]) -> str:
    """One safe category label for a verdict's category list.

    The first RECOGNISED category wins, so a list whose first entry is junk -
    the shape an injected value takes - does not cost us the real finding that
    follows it.
    """
    fallback: Optional[str] = None
    for category in categories:
        normalized = _normalize_category(category)
        if normalized != UNKNOWN_CATEGORY:
            return normalized
        fallback = UNKNOWN_CATEGORY
    return fallback or UNNAMED_CATEGORY
