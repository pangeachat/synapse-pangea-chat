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

Activity rooms (those carrying an activity-plan state event) ARE moderated by
Tier 2. They used to be skipped, on the premise that the conversation
orchestrator bundled moderation into its per-turn call — a premise that is no
longer true, and the stale comment asserting it is what kept the core product
surface unmoderated. On `2-step-choreographer@origin/main` the orchestrator's
`flag` is documented "always null: moderation left the LLM layer in the reset",
`ModerationFlag` is defined and never constructed anywhere in that repository,
and the only route to `/choreo/moderate` is the standalone moderator router
this module calls. `moderation.tier2_moderate_activity_rooms` restores the skip
if that ever changes.

Design doc: .github/instructions/moderation.instructions.md (repo-level) and
the org trust-and-safety doc it descends from.
"""

import inspect
import re
from functools import partial
from html.parser import HTMLParser
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    TypeGuard,
    Union,
)

from synapse.api.errors import AuthError, Codes, SynapseError
from synapse.events import EventBase
from synapse.logging.context import run_in_background
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.module_api import NOT_SPAM, ModuleApi

from synapse_pangea_chat.moderation import metrics, severity
from synapse_pangea_chat.moderation.breaker import CircuitBreaker
from synapse_pangea_chat.moderation.categories import (
    PROVIDER_CATEGORIES,
    UNKNOWN_CATEGORY,
    normalize_category,
)
from synapse_pangea_chat.moderation.choreo_client import (
    ChoreoChecker,
    assert_no_proxy_in_front_of,
    install_proxy_log_guard,
)
from synapse_pangea_chat.moderation.compat import reraise_if_cancelled
from synapse_pangea_chat.moderation.dispatch import ModerationJob, Tier2Dispatcher
from synapse_pangea_chat.moderation.disposition import (
    GRANTED,
    PRESERVED,
    DispositionStore,
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
    scrub_reachable_handlers,
    scrubbing_logger,
    sender_digest,
)
from synapse_pangea_chat.moderation.profanity import contains_profanity
from synapse_pangea_chat.moderation.refusal import refusal_body
from synapse_pangea_chat.moderation.refusal import (
    validate_messages as validate_refusal_messages,
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
# Tags that break the line WHEREVER THEY APPEAR. Table tags are deliberately
# absent: `<td>` outside a table, and a `</td>` with no open cell, are ignored
# entirely by an HTML5 parser, so treating them as breaks split
# `call 41</td>5-555-2671` - one number on screen - into two harmless halves.
# Everything not listed is inline, and inline elements concatenate with no gap:
# the displayed text of `4<b>1</b>5` is `415`, so that is the string the rules
# see.
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
        "ul",
    }
)
# Attribute values a client puts on screen, listed as TAG/ATTRIBUTE pairs
# rather than as bare attribute names, and split by WHERE they appear.
#
# An attribute is displayed only where the renderer displays it: `alt` on an
# `<img>` is shown whenever the image does not load, and `alt` on a `<b>` is
# shown by nothing at all, so treating the name alone as displayed text made
# `<b alt="415-555-2671">hello</b>` a Tier-1 block on a message that reads
# "hello". Tier 1 blocks before persist, so a false positive silences an
# innocent learner.
#
# The split matters as much as the list. An image's alternative text stands
# IN the text flow - it replaces the image, so `call <img alt="415">-555-2671`
# reads `call 415-555-2671` - while a `title` is a tooltip and a spoiler's
# reason is revealed separately, neither of them between the characters either
# side. Putting the first kind in the flow keeps a number whole; keeping the
# second kind out of it stops `41<b title="notes">5</b>-555-2671`, which also
# reads as one number, from being split by text nobody sees there.
#
# `href` is deliberately absent from both: clients show the link's TEXT, and a
# URL full of digits is a plausible phone false positive. Recorded as a limit
# in .github/instructions/moderation.instructions.md.
_INLINE_ATTRIBUTES = frozenset({("img", "alt")})
_OUT_OF_FLOW_ATTRIBUTES = frozenset(
    {
        ("img", "title"),
        ("a", "title"),
        ("abbr", "title"),
        ("span", "data-mx-spoiler"),
        ("span", "data-mx-maths"),
        ("div", "data-mx-maths"),
        # An ordered list's `start` is displayed as its first item's marker,
        # and the Matrix spec permits the attribute explicitly.
        ("ol", "start"),
    }
)
# Elements whose content is never rendered as text. Including it was a
# false-positive source with no upside: nobody reads a stylesheet.
_INVISIBLE_ELEMENTS = frozenset({"script", "style"})
# Cells break the line, but only inside a table: outside one HTML5 ignores them
# entirely, and a newline there split a displayed number in half.
_TABLE_CELL_TAGS = frozenset({"caption", "td", "th", "tr"})
# The tags that actually HOLD content inside a table. `tr` is not one: text
# between a `</td>` and its `</tr>` is outside every cell, and HTML5 foster-
# parents it out of the table exactly like text before the first row. Reading
# `tr` as a cell meant `<table>41<tr><td>notes</td>5-555-2671</tr></table>` -
# `415-555-2671` on screen - collected only the `41`.
_TABLE_CONTENT_TAGS = frozenset({"caption", "td", "th"})
# Tags that IMPLICITLY close an open cell. HTML5 inserts the end tag for you:
# a second `<td>` closes the first, and a row or section boundary closes
# whatever cell is open. Counting cells instead of tracking one open cell
# meant `<table>41<td>notes<td>x</tr>5-555-2671</table>` never came back out
# of a cell, so the `5-555-2671` a reader sees before the table was collected
# by nothing.
_CLOSES_A_CELL = frozenset({"caption", "td", "th", "tr", "tbody", "thead", "tfoot"})
# HTML5 parses `<image>` as `img`, so its alternative text is displayed.
_TAG_ALIASES = {"image": "img"}

# Python's HTMLParser reads an abruptly-closed comment as an OPEN one and keeps
# scanning for a later terminator, so everything up to the next `-->` vanishes.
# HTML5 closes the comment at once and displays what follows
# (https://html.spec.whatwg.org/multipage/parsing.html#comment-start-state), so
# `<!-->call 415-555-2671<!-- -->` is a phone number on screen and was nothing
# at all to the extractor. Rewriting the abrupt form into the well-formed empty
# comment the spec says it is puts the two back in agreement.
_ABRUPT_COMMENTS = re.compile(r"<!--?>")

# `<![` is a MARKED SECTION to Python's parser and a BOGUS COMMENT to HTML5,
# and the difference is how much text disappears.
#
# HTML5's markup-declaration-open state takes `[CDATA[` as a CDATA section only
# when the adjusted current node is a foreign element; in an HTML body it is a
# parse error and a bogus comment, which ends at the FIRST `>`. Everything
# after that `>` - the `]]>` included - is displayed. Python's parser instead
# swallows the whole section up to `]]>`, so `<![CDATA[>call 415-555-2671]]>`
# was a phone number to every reader and nothing at all to the extractor, and
# an UNTERMINATED section swallowed the rest of the message.
#
# The previous fix recovered the remainder inside `unknown_decl` and parsed it
# with a nested parser, one per section - which is a stack frame and a full
# re-parse per section. 300 nested sections, about 4 KB, raised `RecursionError`
# out of extraction and took the whole event with it, plain body included.
#
# Rewriting `<![` so the parser reaches its own bogus-comment branch is the
# same reading with no recursion at all: one flat parse, whatever the nesting.
# The inserted character is invisible either way - a bogus comment is not
# displayed - and it cannot create `<!--` or `<!doctype`.
#
# Three characters in and three out, and the substitute is NOT a word
# character. Both halves matter, and the second one is a correctness rule
# rather than tidiness. The rewrite runs over the raw string before parsing,
# so it also lands inside ATTRIBUTE values, where `<![` is literal displayed
# text rather than markup - and a word character there MERGES WITH THE TOKEN
# THAT FOLLOWS: `alt="<![415-555-2671"` became `<!x415-555-2671`, one token,
# and the phone number a reader sees stopped matching. Under-reading is the
# expensive direction, so the substitute is punctuation, which separates
# tokens exactly as `[` did.
#
# `(` specifically: it cannot combine with what follows into `<!--` (the
# parser compares four characters from the `<`, and the third is fixed here),
# so it cannot turn a bogus comment into a real one.
_MARKED_SECTIONS = re.compile(r"<!\[")
_BOGUS_COMMENT_OPEN = "<!("


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
        self.attribute_text: List[str] = []
        # A boolean, not a counter: `script` and `style` put the tokenizer into
        # CDATA mode, so they cannot nest - anything that looks like a nested
        # opener inside one is source text. A counter could be incremented
        # twice and then never fall back to zero, which hid every visible
        # character after the real closing tag.
        self._invisible = False
        # Block tags only break the line where a tree builder would put them.
        # `</div>` with no open `div` is ignored by HTML5, and a newline there
        # split a displayed number in half; a `<td>` outside a table is ignored
        # too, while inside one it separates cells that render apart. Tracking
        # the open blocks and whether we are in a table is the smallest amount
        # of tree context that gets both right.
        self._open_blocks: List[str] = []
        self._table_depth = 0
        # Foster parenting. Character data that appears inside a table but
        # outside a cell is moved OUT by an HTML5 tree builder and displayed
        # immediately BEFORE the table, in order - so
        # `<table>41<tr><td>notes</td></tr>5-555-2671</table>` reads as
        # `415-555-2671` followed by a one-cell table, while this extractor
        # reads it in source order with the cell breaks between.
        #
        # **We do not reconstruct that reading. We report that we cannot.**
        # Three attempts to synthesise the fostered run produced, in turn: a
        # missed number when a row held the text, a missed number when one
        # `<div>` sat inside a cell, and an INVENTED number across a nested
        # table - a pre-send block on text no reader sees, which is the
        # expensive direction. Getting it right needs the insertion modes and
        # the element stack of a real tree builder; that is out of scope here,
        # and each partial model of it was wrong in a new place.
        #
        # So the case is made SAFE rather than silently wrong: the extraction
        # is marked incomplete, which counts it and hands it to Tier 2, and
        # the text is left in source order for whatever the rules can make of
        # it. Tier 1 does not act on a reading we know is not the reader's.
        self._in_a_cell = False
        self._fostered_text = False

    def handle_data(self, data: str) -> None:
        if self._invisible:
            return
        self._emit(data)

    def _emit(self, text: str) -> None:
        """Displayed text, in source order - and a note when the reader will
        not see it in that order."""
        self._parts.append(text)
        if self._table_depth > 0 and not self._in_a_cell and text.strip():
            self._fostered_text = True

    def _end_table(self) -> None:
        self._in_a_cell = False

    def _breaks_line(self, tag: str) -> bool:
        if tag in _BLOCK_LEVEL_TAGS:
            return True
        if tag == "table":
            # A table is always a block box, and unlike `td` it cannot be
            # ignored: `<table>` opens a table wherever it appears. Two
            # adjacent tables render one above the other, so their contents
            # do not join - `<table>41</table><table>5-555-2671</table>` is
            # not a phone number on screen and must not be one here.
            return True
        return self._table_depth > 0 and tag in _TABLE_CELL_TAGS

    def _handle_tag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        tag = _TAG_ALIASES.get(tag, tag)
        if tag == "table":
            # A nested table starts its own fostered run: text belonging to
            # the outer table must not be joined to text belonging to the
            # inner one, because they are displayed in different places.
            self._end_table()
            self._table_depth += 1
        elif self._table_depth > 0 and tag in _CLOSES_A_CELL:
            self._in_a_cell = tag in _TABLE_CONTENT_TAGS
        if self._breaks_line(tag):
            self._open_blocks.append(tag)
            self._parts.append("\n")

        if self._invisible:
            # Attributes inside script or style source are not markup and are
            # displayed by nothing.
            return
        # First occurrence wins, as HTML5 says: a duplicate attribute is a
        # parse error and the LATER one is dropped. Emptiness is judged after
        # that choice, not before it - skipping an empty first value and
        # reading the non-empty duplicate invents text the renderer discarded.
        seen: Dict[str, str] = {}
        for name, value in attrs:
            if name not in seen:
                seen[name] = value or ""
        for name, value in seen.items():
            if not value:
                continue
            if (tag, name) == ("ol", "start") and not value.strip("+-").isdigit():
                # A non-numeric `start` is ignored and the list renders with
                # its ordinary markers, so the value is on screen nowhere.
                continue
            if (tag, name) in _INLINE_ATTRIBUTES:
                self._emit(value)
            elif (tag, name) in _OUT_OF_FLOW_ATTRIBUTES:
                self.attribute_text.append(value)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self._handle_tag(tag, attrs)
        if tag in _INVISIBLE_ELEMENTS:
            self._invisible = True

    def handle_startendtag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        # A trailing slash does not make an element void in HTML5: `<script/>`
        # opens a script element exactly as `<script>` does, and everything
        # after it is source until the close tag.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = _TAG_ALIASES.get(tag, tag)
        if tag in _INVISIBLE_ELEMENTS:
            self._invisible = False
        if tag == "table" and self._table_depth > 0:
            self._table_depth -= 1
            self._end_table()
        elif self._table_depth > 0 and tag in _CLOSES_A_CELL:
            self._in_a_cell = False
        # Only a tag that actually opened a block closes one. An unmatched
        # `</div>` is ignored by HTML5, and a newline there split a displayed
        # number in two.
        if tag in self._open_blocks:
            while self._open_blocks:
                if self._open_blocks.pop() == tag:
                    break
            self._parts.append("\n")

    def close(self) -> None:
        super().close()
        self._end_table()

    @property
    def rearranged(self) -> bool:
        """True when the reader sees this text in an order we did not read it
        in, so the caller can count the message and hand it to Tier 2."""
        return self._fostered_text

    def result(self) -> str:
        return "".join(self._parts + ["\n" + a for a in self.attribute_text])


def _displayed_text(formatted: str) -> str:
    """The reader-visible text of an HTML body, never less than it displays."""
    # HTML5 REPLACES U+0000 with U+FFFD rather than dropping it, so
    # `call 41<NUL>5-...` is not a phone number on screen - it has a
    # replacement character in the middle of it. Deleting the NUL instead
    # joined the digits and invented a match, and inside a tag name it turned
    # an unknown element into a real `script` and hid its contents.
    #
    # There is no doctype preprocessing here, and its absence is deliberate. A
    # `>` inside a quoted public or system identifier DOES end the doctype
    # under HTML5 (abrupt-doctype-public-identifier), which is exactly what
    # Python's parser already does - so the quote-aware scan that was here
    # deleted text a renderer displays, mangled doctype-shaped text inside an
    # `alt` value, and recursed once per declaration.
    prepared = formatted.replace("\x00", "\ufffd")
    prepared = _ABRUPT_COMMENTS.sub("<!---->", prepared)
    prepared = _MARKED_SECTIONS.sub(_BOGUS_COMMENT_OPEN, prepared)
    parser = _DisplayedText()
    parser.feed(prepared)
    parser.close()
    return parser.result()


def _displayed_reading(formatted: str) -> Tuple[str, bool]:
    """The displayed text, and whether the reader sees it in this ORDER.

    Two values because they are two facts: the characters, and whether the
    arrangement we read them in is the arrangement on screen. See
    `_DisplayedText` for why the second one is reported rather than repaired.
    """
    prepared = formatted.replace("\x00", "\ufffd")
    prepared = _ABRUPT_COMMENTS.sub("<!---->", prepared)
    prepared = _MARKED_SECTIONS.sub(_BOGUS_COMMENT_OPEN, prepared)
    parser = _DisplayedText()
    parser.feed(prepared)
    parser.close()
    return parser.result(), parser.rearranged


#: The event type carrying a plaintext message. The gate for both tiers: what
#: is inside it is read field by field, and nothing outside it is a message.
MESSAGE_EVENT_TYPE = "m.room.message"
#: The event type carrying a megolm envelope. Synapse holds the ciphertext and
#: no plaintext, so neither tier can moderate one - see `_encryption_state`.
ENCRYPTED_EVENT_TYPE = "m.room.encrypted"


def _encryption_state(event: EventBase) -> Optional[str]:
    """Which message-bearing event this is, in the counter's vocabulary, or
    None when it is not a message at all.

    **The limit this makes visible is structural and is not closed here.** In
    an end-to-end encrypted room the homeserver receives an `m.room.encrypted`
    event whose payload is a megolm envelope; it holds no key and no
    plaintext, so there is nothing for a rule to match or for a classifier to
    read. Neither tier can moderate one, and this module does not try: handing
    a base64 envelope to `/choreo/moderate` would spend a request on
    ciphertext and publish an encrypted room's traffic pattern to a third
    party, and refusing the event instead would punish a sender who did
    nothing wrong.

    What WAS wrong is that the limit was silent. An operator who switched
    moderation on had no series that distinguished "this room is clean" from
    "we never read a word of it", and no way to put a number on the share of
    traffic in the second state. The counter carries both halves of a ratio -
    `encrypted / (encrypted + plaintext)` - because a numerator with no
    denominator answers no question anybody asks.

    A state event is neither. `check_event_for_spam` is offered every event on
    the homeserver, and counting topic changes and membership as readable
    traffic would swamp the denominator and leave the ratio meaning nothing.
    """
    event_type = getattr(event, "type", None)
    if event_type == MESSAGE_EVENT_TYPE:
        return "plaintext"
    if event_type == ENCRYPTED_EVENT_TYPE:
        return "encrypted"
    return None


class _Extracted(NamedTuple):
    """What extraction found, and whether it found all of it.

    Two fields because "no text" and "we could not read the text" are
    different facts with different correct responses, and collapsing them into
    `Optional[str]` is what let a parser failure skip both tiers silently.
    """

    #: The displayed text, or None when there is none to moderate.
    text: Optional[str]
    #: True when some displayed surface could not be read. The message is
    #: still checked on whatever WAS read; this says the check is partial.
    incomplete: bool


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
        # Validated here for the same reason as the globs above: config-parse
        # time is where an operator's mistake is named, and this constructor is
        # where a value that arrived by any other route - a test, a second
        # entry point - has to fail rather than serve a learner a message
        # nobody checked.
        self._refusal_messages = validate_refusal_messages(
            config.moderation_tier1_refusal_messages
        )
        # Same reason again, and one more specific to this value: a threshold
        # decides whether a message stays up, so a table that reached here
        # unvalidated - a key naming no category, a value outside [0, 1] -
        # must fail startup rather than govern a redaction from inside a
        # `KeyError` nobody sees.
        self._category_thresholds = severity.validate_thresholds(
            config.moderation_tier2_category_thresholds
        )
        # Read once here rather than off `self._config` at every event: this
        # is tested on the notifier's inline path, for every event on the
        # homeserver.
        self._moderate_activity_rooms = bool(
            config.moderation_tier2_moderate_activity_rooms
        )
        # Keyed per instance so a Matrix ID cannot be recovered from a log
        # line by enumeration; see moderation.log_safety.
        self._log_digest_key = new_digest_key()
        # A handler may carry its own `LoggingContextFilter` - Synapse still
        # documents that configuration - and a handler's filters run AFTER the
        # logger's, so such a handler puts the sender's Matrix ID back on a
        # record this module had already cleaned. Done here rather than at
        # import so it runs once moderation is actually enabled, by which time
        # the deployment's logging config is in place.
        scrub_reachable_handlers(logger)

        self._tier2_active = False
        self._dispatcher: Optional[Tier2Dispatcher] = None
        self._checker: Optional[ChoreoChecker] = None
        self._disposition: Optional[DispositionStore] = None
        self._clock: Any = None

        if config.moderation_tier1_enabled:
            api.register_spam_checker_callbacks(
                check_event_for_spam=self.check_event_for_spam,
            )
        if config.moderation_tier2_enabled:
            self._start_tier2()
            api.register_third_party_rules_callbacks(
                on_new_event=self.on_new_event,
            )

    def _start_tier2(self) -> None:
        """Build the Tier-2 machinery, on the one instance that should run it.

        **The guard is the whole of the cross-process story, and it is not
        idempotency.** `on_new_event` is dispatched from
        `Notifier.notify_new_room_events`, which has three callers: the local
        persister, the federation path, and `replication/tcp/client.py`'s
        `EventsStream` branch of `on_rdata` - and that last one runs on every
        worker subscribed to the events stream. Without a guard, an N-worker
        deployment makes N moderation calls and N redaction attempts for one
        message.

        It does not cost coverage. `ReplicationCommandHandler.__init__` builds
        its stream set from all of `STREAMS_MAP` on every process, so the
        background-tasks instance receives every event either by persisting it
        or by replication; in a monolith there is one process and the flag is
        true there.

        Two instances that both have `run_background_tasks` set is still a
        misconfiguration and still not detectable from inside either process -
        two independent processes, two independent in-memory sets. What it no
        longer costs is a duplicated redaction: `moderation.disposition` holds
        the claim in the database, so the second instance finds the event
        already decided and skips it. The guard is what stops N workers making
        N moderation CALLS; the table is what stops them sending N redactions.

        The invariant is made observable rather than asserted: a
        `pangea_moderation_tier2_active` gauge is 1 here and 0 elsewhere, so
        `sum(...) == 0` alerts on "Tier 2 is enabled and nothing is running
        it". A WARNING on every non-background worker would fire in a HEALTHY
        deployment and train operators to ignore it.
        """
        # Installed on every instance that has Tier 2 enabled, not only the
        # one that runs it: the guard protects a log, and the log belongs to
        # the process rather than to the worker pool.
        install_proxy_log_guard()
        self._tier2_active = bool(self._api.should_run_background_tasks())
        metrics.TIER2_ACTIVE.set(1 if self._tier2_active else 0)
        if not self._tier2_active:
            return

        homeserver = self._api._hs
        self._clock = homeserver.get_clock()
        self._disposition = DispositionStore(homeserver)
        config = self._config
        breaker = CircuitBreaker(
            clock=self._clock,
            failure_threshold=config.moderation_tier2_breaker_failure_threshold,
            cooldown_seconds=config.moderation_tier2_breaker_cooldown_seconds,
            max_cooldown_seconds=config.moderation_tier2_breaker_max_cooldown_seconds,
        )
        agent = self._api.http_client.agent
        # Before anything is built, and it RAISES: a proxied CONNECT leaks a
        # socket per stalled handshake that no deadline of ours can close, and
        # the breaker's probe keeps opening more. See `choreo_client`'s module
        # docstring. Moderation off and loud is a state an operator can see
        # and fix; a homeserver quietly running out of file descriptors is not.
        assert_no_proxy_in_front_of(agent, config.moderation_choreo_base_url)
        self._checker = ChoreoChecker(
            # `.agent`, not the client itself - see `choreo_client`'s module
            # docstring for the three properties of `SimpleHttpClient`'s own
            # request methods that rule them out. The agent is the shared,
            # pooled one either way.
            agent=agent,
            clock=self._clock,
            base_url=config.moderation_choreo_base_url,
            access_token=config.moderation_choreo_access_token,
            breaker=breaker,
            timeout_seconds=config.moderation_tier2_request_timeout_seconds,
        )
        self._dispatcher = Tier2Dispatcher(
            homeserver=homeserver,
            clock=self._clock,
            handler=self._screen_batch,
            workers=config.moderation_tier2_workers,
            queue_size=config.moderation_tier2_queue_size,
            supervisor_interval_seconds=(
                config.moderation_tier2_supervisor_interval_seconds
            ),
            drain_timeout_seconds=config.moderation_tier2_drain_timeout_seconds,
            max_batch=config.moderation_tier2_max_batch,
            batch_max_wait_seconds=config.moderation_tier2_batch_max_wait_seconds,
        )
        self._dispatcher.start()
        logger.info(
            "tier2 moderation is active on this instance: %d workers, queue "
            "%d, batches of up to %d",
            config.moderation_tier2_workers,
            config.moderation_tier2_queue_size,
            config.moderation_tier2_max_batch,
        )

    # ------------------------------------------------------------------
    # Shared filters
    # ------------------------------------------------------------------

    def _extract_text(self, event: EventBase) -> "_Extracted":
        """The moderatable text of a message event, and whether it is all of it.

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

        **A failure to READ is not a finding of NOTHING**, and returning the
        two the same way is the same class `tier1_prefilter.check_text` names
        for the rules, one layer up. A `formatted_body` that broke the parser
        used to take the whole event with it - including the plain `body` that
        had already been read and said `call 415-555-2671` - and both tiers
        then saw a message with no text, which is a bypass any sender can
        trigger on every message and which nothing counted.

        So every surface, and every field within a surface, is read on its
        own: one that fails costs its own text and nothing else, and the
        result says so. The caller counts the shortfall and Tier 2 still gets
        the message - an unknown is escalated, never dropped quietly.
        """
        if event.type != MESSAGE_EVENT_TYPE:
            return _Extracted(None, False)
        # `Mapping`, not `dict`: event content is not guaranteed to be a plain
        # dict. Synapse builds events through a Rust type whose `content` is a
        # `JsonObject`, and a homeserver running with `use_frozen_dicts: true`
        # hands modules `immutabledict` values. An `isinstance(..., dict)`
        # test on either returns False, which would silently stop moderating
        # the replacement text of every edit - a bypass that fails open and
        # says nothing.
        content = event.content or {}
        if not isinstance(content, Mapping):
            # A content we cannot even index is a content we cannot read, and
            # a client that renders it renders something. Reported as an
            # unknown rather than as an empty message.
            return _Extracted(None, True)

        surfaces: List[Mapping[str, Any]] = [content]
        new_content = content.get("m.new_content")
        if _is_replacement(content) and isinstance(new_content, Mapping):
            surfaces.append(new_content)

        parts: List[str] = []
        incomplete = False
        for surface in surfaces:
            try:
                surface_parts, surface_incomplete = _surface_text(surface)
            except Exception as exc:
                reraise_if_cancelled(exc)
                # Type and site, never the message: a reader that failed on a
                # message body routinely quotes that body back.
                incomplete = True
                logger.warning(
                    "moderation could not read a surface of %s at %s (%s); "
                    "the rest of the event is still checked",
                    event.event_id,
                    error_site(exc),
                    type(exc).__name__,
                )
                continue
            parts.extend(surface_parts)
            incomplete = incomplete or surface_incomplete
        text = "\n".join(parts).strip()
        return _Extracted(text or None, incomplete)

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

    async def check_event_for_spam(
        self, event: EventBase
    ) -> Union[str, Codes, bool, Tuple[Codes, Dict[str, Any]]]:
        """Tier 1's verdict, and - on a refusal - the reason for it.

        The return type is the callback's own:
        `Awaitable[str | Codes | tuple[Codes, JsonDict] | bool]`. A block
        returns the tuple form, because a bare `Codes` reaches the sender as
        Synapse's fixed "rejected as probable spam" and tells a learner
        nothing about what happened or that a machine decided it. What the
        dict may and may not say is `moderation.refusal`'s subject.
        """
        try:
            if self._is_exempt_sender(event.sender):
                return NOT_SPAM
            # AFTER the exempt filter, deliberately: an exempt bot is
            # unmoderated for a reason that has nothing to do with encryption,
            # and folding the two together would report its traffic as a
            # coverage gap E2EE caused.
            encryption = _encryption_state(event)
            if encryption is not None:
                metrics.record_message_event("tier1", encryption)
            extracted = self._extract_text(event)
            if extracted.incomplete:
                # Counted before the verdict, and whatever the verdict turns
                # out to be: this says "there was displayed text we could not
                # read", which is true of a message that then passes every
                # rule as much as of one that trips one.
                metrics.record_extraction_incomplete("tier1")
            text = extracted.text
            if text is None:
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
                # The rule identifier goes to the SENDER, who already knows
                # what they wrote, and nowhere else. It is not the same
                # disclosure as the log line above, which sits beside a room
                # id and would be an assertion about a message to somebody who
                # never saw it.
                return Codes.FORBIDDEN, refusal_body(reason, self._refusal_messages)
            return NOT_SPAM
        except Exception as exc:
            # silent-ok: fail-open by contract — a moderation bug must never
            # block all sends; the failure is logged and Tier 2 still runs.
            #
            # Counted as well as logged: a message the blocking tier could not
            # look at is an unknown, and an uncounted unknown reads exactly
            # like a clean message on every dashboard an operator has. The
            # per-surface counter above cannot see this one - it is raised
            # before or around the extraction that increments it.
            #
            # Type and site, not `logger.exception`: the traceback ends with
            # the exception's own message, and anything raised while matching
            # a message body is liable to quote that body.
            metrics.TIER1_FAILED.inc()
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
        """Hand the Tier 2 check to the worker pool and return.

        This is awaited INLINE by `Notifier.notify_new_room_events`, for every
        event on the homeserver. So it does no I/O, takes no lock, and never
        waits: the most it does is a bounded-buffer append and, at most once
        per reactor turn, schedule a zero-delay wakeup. A full queue is a
        counted refusal here, never back-pressure on event persistence.
        """
        try:
            # First, and before the text is even extracted: on every other
            # worker this callback's entire cost should be one boolean.
            if not self._tier2_active or self._dispatcher is None:
                return
            if self._is_exempt_sender(event.sender):
                return
            # Counted here for the same reason and with the same placement as
            # in Tier 1: after the exempt filter, before any decision, and on
            # both halves of the ratio. An encrypted event goes no further -
            # `_extract_text` gates on the event TYPE, so it was already never
            # enqueued, and what changes is that the gap is now on a gauge
            # rather than inferred from a queue that stayed empty.
            encryption = _encryption_state(event)
            if encryption is not None:
                metrics.record_message_event("tier2", encryption)
            extracted = self._extract_text(event)
            if extracted.incomplete:
                metrics.record_extraction_incomplete("tier2")
            text = extracted.text
            if text is None:
                if extracted.incomplete:
                    # The message had displayed text and we could not read any
                    # of it, so there is nothing to ask the service about. A
                    # counted drop, not a message that passed.
                    metrics.record_drop("extraction_failed")
                return
            if not self._moderate_activity_rooms and self._room_has_activity_plan(
                state_events
            ):
                # Only when an operator has asked for it. The default is to
                # moderate: the orchestrator stopped producing a moderation
                # flag in the reset, so skipping here left activity sessions
                # checked by nothing at all. See the module docstring.
                #
                # Note what is ABOVE this line and stays above it: the exempt
                # filter. The bot is a participant in an activity room rather
                # than a bystander, and Tier 2 redacting its replies would be
                # a visible product regression, so the exemption has to win
                # over the room's inclusion.
                return
            self._dispatcher.enqueue(
                ModerationJob(
                    event_id=event.event_id,
                    room_id=event.room_id,
                    sender=event.sender,
                    text=text,
                    enqueued_at=self._clock.time(),
                )
            )
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok: fail-open by contract; observe-only hook, so the
            # only cost of a failure here is a missed check — logged by type
            # and site rather than as a traceback, for the reason given on
            # the Tier-1 handler above.
            #
            # Counted as well as logged. Fail-open must not mean fail-silent:
            # a message that never reached the queue is a message that will
            # not be checked, and it looked identical to a clean one on every
            # dashboard an operator has.
            metrics.record_drop("dispatch_error")
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

    async def _screen_batch(self, jobs: Tuple[ModerationJob, ...]) -> None:
        """Screen a batch with one call, then confirm anything it flagged.

        **No redaction is ever taken on a batched verdict.** The wire contract
        is positional, so a result attributed to the wrong message would
        redact an innocent learner and leave a harmful one standing - and a
        redaction is irreversible. So the batch is only ever a SCREEN: a
        flagged entry sends that ONE message back through the ordinary
        single-text path, and it is that call's verdict which drives severity,
        the preserve branch, the durable claim and the send.

        That makes misattribution structurally impossible rather than merely
        unlikely, because a response to a request carrying exactly one text
        has no index to get wrong. Every guarantee in `_check_and_redact` is
        preserved by construction, since that function is unchanged and is
        still the only thing that redacts.

        The cost is one extra call per FLAGGED message, which is the term in
        the capacity arithmetic that is an estimate rather than a measurement;
        `pangea_moderation_tier2_screen_total{verdict="flagged"}` is what makes
        it a measurement on real traffic.

        The screen's own mapping is closed at both ends. The texts are derived
        from `jobs` in the same expression that pairs the results back to it,
        `jobs` is an immutable tuple, so there is no second ordering for the
        two to disagree about; `check_batch` refuses any response whose length
        differs from the request's; and `zip(..., strict=True)` raises rather
        than truncating if either of those were ever wrong.
        """
        if self._checker is None:
            return
        if len(jobs) == 1:
            # One message needs no screen, and a batch of one against an
            # un-upgraded endpoint would cost a refused round trip before the
            # single call it was always going to make.
            await self._check_and_redact(jobs[0])
            return
        for job in jobs:
            self._count_truncation(job)
        verdicts = await self._checker.check_batch([job.text for job in jobs])
        # `strict=True` is the last line of defence and it is deliberately a
        # RAISE rather than a shorter loop: a silent truncation here pairs
        # every verdict after the shortfall with the wrong message. The raise
        # reaches the dispatcher, which counts every job in the batch as
        # unchecked - the fail-open direction.
        if not self._checker.batch_supported:
            # The demotion has to reach the DISPATCHER and not just the
            # checker. `check_batch`'s fallback asks one text at a time inside
            # the worker holding the batch, so a batch of 32 against an
            # un-upgraded endpoint is 32 sequential ~2 s calls - a minute
            # during which the last message is unmoderated, and long enough
            # that the drain deadline abandons the lot. Throughput is
            # unaffected (every worker is equally busy either way); the damage
            # is entirely latency, and it is reachable during a rolling choreo
            # upgrade where production load meets an endpoint answering 422.
            self._stop_batching()
        for job, result in zip(jobs, verdicts.results, strict=True):
            # Each job inside its own guard. Sharing one provider call is a
            # transport decision and must not widen a blast radius: before
            # batching every job was its own handler call, so a failure cost
            # exactly that message, and an exception raised deciding job two
            # would otherwise abandon jobs three and four - which have
            # verdicts in hand and nothing wrong with them. Same rule the
            # extraction applies per surface and the disposition applies per
            # row.
            try:
                await self._decide_screened(job, result, verdicts.confirmed)
            except Exception as exc:
                reraise_if_cancelled(exc)
                # silent-ok: fail-open by contract, and the loop has to
                # survive for the jobs behind this one. Counted for this
                # message alone - the dispatcher's own counter would count the
                # whole batch, which overstates the gap by everything that
                # actually succeeded.
                metrics.record_drop("handler_error")
                logger.warning(
                    "tier2 decision failed for %s in %s at %s (%s)",
                    job.event_id,
                    job.room_id,
                    error_site(exc),
                    type(exc).__name__,
                )

    async def _decide_screened(
        self,
        job: ModerationJob,
        result: Optional[Dict[str, Any]],
        confirmed: bool,
    ) -> None:
        if confirmed:
            # This answer came back from a request carrying only its own text,
            # so it is already the verdict a confirmation would fetch. This is
            # the single-text fallback against an un-upgraded endpoint; asking
            # again would double every flagged message's provider calls
            # against the deployment with the least capacity to spare.
            await self._decide(job, result)
            return
        if result is not None and result.get("flagged"):
            # A screen result, not a decision. Ask again about this one
            # message alone; `_check_and_redact` records its own matcher
            # agreement and its own check outcome from that answer.
            await self._check_and_redact(job, count_truncation=False)
            return
        self._record_matcher_agreement(job, result)

    def _stop_batching(self) -> None:
        if self._dispatcher is None or self._dispatcher.max_batch == 1:
            return
        self._dispatcher.max_batch = 1
        logger.info(
            "tier2 moderation is no longer batching: the endpoint refused a "
            "batched request, so work is taken one message at a time"
        )

    @staticmethod
    def _count_truncation(job: ModerationJob) -> None:
        if len(job.text) > MATCHER_MAX_CHARS:
            # `/choreo/moderate` truncates its input, so a longer message gets
            # a verdict about its PREFIX and the remainder is judged by
            # nothing. Counted for the same reason every other partial look is:
            # "we did not read all of this" must not be reported as a clean
            # message. Chunking past the truncation is ADR-8b and is not this
            # change; making the gap visible is.
            metrics.TIER2_TRUNCATED.inc()

    async def _check_and_redact(
        self, job: ModerationJob, *, count_truncation: bool = True
    ) -> None:
        """Ask about one message, then act on the answer.

        Split from `_decide` so the batched path can reach the decision with a
        verdict it already has. The split is a seam and not a behaviour
        change: this is still the only route to a redaction, and every
        guarantee - the preserve branch, the thresholds, the durable claim,
        the drain check - lives below it in `_decide`, untouched.
        """
        if self._checker is None:
            return
        if count_truncation:
            # False when a batched screen has already counted this message.
            # The counter is one per MESSAGE Tier 2 read only part of, and a
            # confirmation is a second look at a message already counted - so
            # counting it again would report every long flagged message twice.
            self._count_truncation(job)
        result = await self._checker.check(job.text)
        await self._decide(job, result)

    async def _decide(
        self, job: ModerationJob, result: Optional[Dict[str, Any]]
    ) -> None:
        """Everything that follows from one message's own verdict.

        `result` must be a verdict about THIS message and nothing else - the
        answer to a request that carried only `job.text`. A positionally
        mapped batch entry is not that, which is why `_screen_batch` sends a
        flagged screen result back through `_check_and_redact` instead of
        arriving here with it.
        """
        self._record_matcher_agreement(job, result)
        if result is None:
            # No verdict. Every route to here - transport failure, timeout,
            # a bad token, an open breaker, `evaluated: false` - is already
            # counted and logged by the checker, and every one of them means
            # the same thing: leave the message alone.
            return

        if not result.get("flagged"):
            return
        categories = result.get("categories")
        if not _usable_categories(categories):
            # A flagged verdict whose categories we cannot read is NOT a
            # verdict, and it is not a preserve either - saying so would be
            # the same overclaim in the other direction. Nothing is recorded
            # and nothing is redacted: this delivery produced no decision, and
            # a later well-formed verdict decides on its own merits.
            #
            # What it does NOT establish is that the unreadable category was
            # not self-harm, so a later `harassment` verdict on the same event
            # can still redact it. Closing that would mean recording a durable
            # preserve on a verdict we could not read, which protects
            # harassment forever on one malformed response. The response shape
            # is validated at the transport boundary too; this is the check at
            # the point of DECISION, which a caller cannot bypass, and it is
            # counted so an operator sees the endpoint misbehaving.
            metrics.record_redaction_skip("unusable_verdict")
            logger.warning(
                "tier2 flagged event %s in %s with no usable category; "
                "no decision taken",
                job.event_id,
                job.room_id,
            )
            return
        category = _summarize_categories(categories)
        if _should_preserve(categories):
            # Deleting a disclosure of self-harm is itself a harm: the learner
            # is asking for help, the message is the only record that they did,
            # and a redaction removes it from the room while telling nobody.
            # So the verdict is kept and the message is left standing.
            #
            # Recorded even on the way down, unlike a redaction: writing the
            # protection down can only ever keep a message up, and a shutdown
            # that loses it is a restart that does not know why.
            #
            # RECORDED, and recorded before anything else: the decision has to
            # outlive this job, this process and this instance, because a
            # second verdict on the same event used to redact what the first
            # one had protected. See `moderation.disposition`.
            #
            # This is the whole of the response today, and it is not enough on
            # its own: nothing routes the finding to a teacher or safeguarding
            # contact, so a preserved flag reaches the logs and stops there.
            # That path is pangeachat/admin-dash#105, and it is the reason this
            # branch is a preserve rather than an escalate.
            await self._record_preserved(job, category)
            metrics.TIER2_SUPPRESSED.labels(category=category).inc()
            logger.info(
                "tier2 flagged event %s in %s (category=%s); preserved, not redacted",
                job.event_id,
                job.room_id,
                category,
            )
            return
        # Severity, and only now. The preserve branch above has already
        # returned, so a self-harm disclosure never reaches a threshold and no
        # value an operator can configure takes part in that decision - which
        # is asserted directly rather than left to this comment.
        decision = severity.decide(
            categories, result.get("category_scores"), self._category_thresholds
        )
        metrics.record_severity_basis(decision.basis)
        outcome = "at_or_above" if decision.redact else "below"
        for weighed in decision.weighed:
            metrics.record_category_score(weighed.category, outcome, weighed.score)
        if not decision.redact:
            # Every flagged category scored below its own threshold. In a
            # language classroom that is the ordinary case for mild swearing,
            # which the provider reports under `harassment` with the same
            # `flagged: true` a targeted threat gets.
            #
            # NOT a preserve: nothing durable is written, so a later and more
            # severe verdict on the same event decides on its own merits.
            metrics.record_redaction_skip("below_threshold")
            driver = decision.driver
            logger.info(
                "tier2 flagged event %s in %s (category=%s, score=%.3f, "
                "threshold=%.3f); below the redaction threshold, left standing",
                job.event_id,
                job.room_id,
                driver.category if driver else category,
                driver.score if driver else 0.0,
                driver.threshold if driver else 0.0,
            )
            return
        # The ORDER matters. The re-read comes first and the claim second, so
        # there is no `await` between taking the claim and sending: a claim
        # taken and then abandoned at an await - a cancellation, a shutdown -
        # is a row that says `redacted` on a message that is still standing,
        # and nothing after it would ever take that message down.
        if not await self._is_still_redactable(job):
            return
        claim = await self._may_redact(job, category)
        if claim is None:
            return
        # Bound after the narrowing, because the release below runs inside a
        # closure and a captured Optional does not carry the narrowing with
        # it.
        claim_id: str = claim
        # The score and the bar it cleared ride on the line that says a
        # message was taken down, because "why was this deleted" is the
        # question this record exists to answer and a category alone stopped
        # being a whole answer the moment a threshold could have said
        # otherwise. A float about content is not a word of it: no Matrix ID
        # and no message text, same as every other line here. `no_scores`
        # reports -1, which is not a score any provider can send and so cannot
        # be mistaken for one.
        driver = decision.driver
        logger.info(
            "tier2 flagged event %s in %s (category=%s, basis=%s, score=%.3f, "
            "threshold=%.3f); redacting",
            job.event_id,
            job.room_id,
            category,
            decision.basis,
            driver.score if driver else -1.0,
            driver.threshold if driver else -1.0,
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
                    "room_id": job.room_id,
                    "sender": job.sender,
                    "redacts": job.event_id,
                    "content": {"redacts": job.event_id, "reason": reason},
                }
            )
        except Exception as exc:
            # A raise here is an UNKNOWN, not a failure, and the two are not
            # interchangeable: the send's durable write happens inside
            # Synapse's `handle_new_client_event` and the fan-out to pushers,
            # the notifier and the third-party rules runs after it, so this
            # handler can be reached with the redaction already in the room.
            # `_settle_claim` is therefore what decides, on a re-read, whether
            # the claim goes back and whether this was a failure at all.
            #
            # Settled on the CANCELLATION path as well, which is what the
            # callback is for: `reraise_if_cancelled` re-raises before
            # anything below it runs, so a settlement written underneath is
            # unreachable exactly when it matters most. And cancellation here
            # is not hypothetical - the drain cancels an abandoned worker
            # parked on this very send.
            #
            # It is caught HERE, and not allowed to escape, because this
            # coroutine runs under `run_as_background_process`, which calls
            # `logger.exception` on whatever reaches it. Synapse's own
            # "User <mxid> not in room <room>" carries the Matrix ID, so
            # letting the exception through would put an identified sender
            # into a plaintext log by a route none of this module's own
            # format strings mention.
            #
            # Escalating the legitimate failures is separate work; today the
            # message stays up and the failure is counted and visible, which
            # is what the previous behaviour was missing.
            #
            # `partial` rather than a lambda, because it binds the arguments
            # NOW: `except ... as exc` unbinds `exc` at the end of the block,
            # so a closure that reads it later reads a name that no longer
            # exists. It happens to work while the call is synchronous, and
            # it is the kind of working-by-accident the next edit breaks
            # silently.
            reraise_if_cancelled(
                exc, partial(self._settle_claim, job, claim_id, category, exc)
            )
            self._settle_claim(job, claim_id, category, exc)
            logger.warning(
                "tier2 redaction raised for %s in %s at %s (%s); whether it "
                "landed is being re-read",
                job.event_id,
                job.room_id,
                error_site(exc),
                type(exc).__name__,
            )
            return
        # OUTSIDE the try, and deliberately: anything that raised in here
        # while inside it would be read as a failed send, and the module would
        # release a claim and count a failure on a redaction that had already
        # landed. The success metric is out here for the same reason - the
        # `try` holds the send and nothing else.
        metrics.TIER2_REDACTIONS.labels(category=category).inc()
        # The VISIBLE WINDOW, observed here and nowhere else: the send has
        # returned, so this is the first moment the message is provably no
        # longer readable, and `enqueued_at` was taken in `on_new_event`,
        # which the notifier awaits once the event has persisted and is
        # visible. Everything in between - the queue wait, the provider call,
        # the disposition reads, the send - is inside the number.
        #
        # Only on the redaction path. A message left standing has no window,
        # and folding a preserve or a below-threshold verdict into this series
        # would make the distribution describe something nobody is asking
        # about.
        #
        # Outside the `try` for the same reason the counter above is: the
        # `try` holds the send and nothing else.
        metrics.TIER2_REDACTION_WINDOW.observe(
            max(self._clock.time() - job.enqueued_at, 0.0)
        )
        await self._warn_if_preserved_meanwhile(job)

    def _record_matcher_agreement(
        self, job: ModerationJob, result: Optional[Mapping[str, Any]]
    ) -> None:
        """Run the deterministic wordlist matcher beside the service verdict,
        and record only what the two of them said.

        **It does not redact, and that is the decision rather than an
        omission.** The 470-term multilingual matcher had no production caller
        at all, which made Tier 2 LLM-only and left the directive that the
        post-send check be MORE COMPLETE unmet. But the matcher is known to be
        noisy - notably in Korean, where `년` is an ordinary bound noun
        meaning "year" and is on the list as a slur - so making it a second
        redaction trigger unmeasured would delete innocent learners' messages
        in exactly the languages nobody on this change can read.

        So it runs, and the agreement matrix is published: service flagged /
        matcher hit / both / neither. That makes the service's real miss rate
        measurable in staging on real traffic, and promoting the matcher to a
        redaction trigger becomes a decision somebody takes on evidence.

        **Both sides see the same text**, which is what makes the matrix mean
        anything: the endpoint truncates its input, so a matcher reading past
        that point would report disagreements that are an artifact of the
        cut rather than of the model.
        """
        if result is None:
            service = "no_verdict"
        else:
            service = "flagged" if result.get("flagged") else "clean"
        try:
            text = job.text[:MATCHER_MAX_CHARS]
            matcher = "hit" if contains_profanity(text) else "miss"
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok: an observability signal must never cost a check. A
            # failure is still COUNTED - a matcher that broke did not find the
            # message clean.
            matcher = "error"
            logger.warning(
                "tier2 wordlist matcher failed at %s (%s)",
                error_site(exc),
                type(exc).__name__,
            )
        metrics.record_matcher_agreement(service, matcher)

    async def _record_preserved(self, job: ModerationJob, category: str) -> None:
        if self._disposition is None:
            return
        await self._disposition.record_preserved(
            event_id=job.event_id, room_id=job.room_id, category=category
        )

    async def _may_redact(self, job: ModerationJob, category: str) -> Optional[str]:
        """Claim the right to redact this event, or decline.

        Returns the claim id when the claim was granted, and None otherwise.

        Asked before every redaction, and not only when this verdict happens
        to be a self-harm one: the verdict that arrives second is by
        definition a different one, and `self-harm/intent` then `harassment`
        on the same event is exactly the sequence that used to delete a
        disclosure.

        An unknown answer is a NO. This is the one decision in the module that
        does not fail towards carrying on, for the reason in
        `moderation.disposition`.
        """
        if self._dispatcher is not None and not self._dispatcher.actions_permitted:
            # The drain has ended and this job was written off. Nothing here
            # can stop a coroutine mid-`await`, and the cancellation that
            # tries to is catchable by every broad `except` in this module -
            # so the last line is a check at the point of action, which no
            # exception can swallow.
            metrics.record_redaction_skip("shutdown")
            logger.warning(
                "tier2 will not redact %s in %s: moderation has already shut "
                "down and this check was written off",
                job.event_id,
                job.room_id,
            )
            return None
        if self._disposition is None:
            # No store means no disposition can be established, and an
            # unknown disposition is never a redaction - the same rule as a
            # read that fails. An empty claim id was returned here, which is
            # falsy but not None, so the caller's `is None` test let it
            # through and the whole guarantee reverted to what it replaced.
            # Unreachable today (`_start_tier2` sets the store before the
            # checker and nothing clears it) and written to be right anyway.
            metrics.record_redaction_skip("disposition_unknown")
            logger.warning(
                "tier2 will not redact %s in %s: no disposition store",
                job.event_id,
                job.room_id,
            )
            return None
        claimed = await self._disposition.claim_redaction(
            event_id=job.event_id, room_id=job.room_id, category=category
        )
        if claimed is None:
            metrics.record_redaction_skip("disposition_unknown")
            logger.warning(
                "tier2 will not redact %s in %s: its disposition could not be "
                "established, and an unknown disposition is never a redaction",
                job.event_id,
                job.room_id,
            )
            return None
        outcome, claim_id = claimed
        if outcome == GRANTED:
            return claim_id
        if outcome == PRESERVED:
            metrics.record_redaction_skip("preserved")
            logger.info(
                "tier2 will not redact %s in %s: it carries a preserved "
                "disposition from an earlier verdict",
                job.event_id,
                job.room_id,
            )
            return None
        metrics.record_redaction_skip("already_redacted")
        return None

    async def _warn_if_preserved_meanwhile(self, job: ModerationJob) -> None:
        """Did a preserve land while this redaction was in flight?

        The one thing a table cannot do is recall an action already taken in
        another system, so this does not prevent the harm - it makes it
        visible. The window is between the claim committing and the send
        landing, it is unreachable inside one instance (the dispatcher holds
        one in-flight claim per event id), and it is reachable only when two
        instances both run background tasks, which is a misconfiguration.
        When it happens a disclosure has been removed from a room and the only
        remaining remedy is a human who knows that it was.
        """
        if self._disposition is None:
            return
        if not await self._disposition.is_preserved(job.event_id):
            return
        metrics.TIER2_REDACTED_AFTER_PRESERVE.inc()
        logger.error(
            "tier2 redacted %s in %s and the event was preserved while the "
            "send was in flight: a disclosure has been removed and cannot be "
            "restored by this module. Check that only one instance has "
            "run_background_tasks set (pangea_moderation_tier2_active)",
            job.event_id,
            job.room_id,
        )

    def _settle_claim(
        self, job: ModerationJob, claim_id: str, category: str, error: BaseException
    ) -> None:
        """Decide what to do with the claim after the send RAISED.

        **A claim may be given back only when the send provably did NOT
        happen.** Release used to be the default on every exit from the
        handler, and a default is the wrong shape here, because the two
        mistakes are not comparable:

        - A claim kept on a message that is still standing blocks later
          verdicts on that one event. A human can clear the row, and the
          counters (`claim_retained`, `claim_stranded`) are what tells them to.
        - A claim given back on a message that is ALREADY GONE erases the only
          record that a redaction was taken. The disposition table exists so a
          human can trust what it says about a safeguarding decision, and a
          table that forgets a redaction is a table that will later report the
          event as untouched - or as `preserved` - for a message nobody can
          get back.

        So the unknown goes to the first of those, and the evidence is a
        re-read. Nothing about this is specific to cancellation: cancellation
        is merely the reachable case, because `worker.cancel()` at the drain
        deadline delivers `CancelledError` at whatever `await` the coroutine
        is parked on and rolls back nothing that already committed.

        Detached with `run_in_background` rather than awaited, and that is the
        point: the caller may be being CANCELLED, and awaiting here would be
        cancelled with it. A detached call still runs.

        The failure LABEL is derived here and the exception is not carried
        into the detached half: a label is a bounded string, and holding a
        live exception across a background task keeps its traceback - and
        every frame's locals, message text included - alive with it.
        """
        if self._disposition is None or not claim_id:
            return
        run_in_background(
            self._settle_claim_now,
            job,
            claim_id,
            category,
            _redaction_failure_cause(error),
        )

    async def _settle_claim_now(
        self, job: ModerationJob, claim_id: str, category: str, cause: str
    ) -> None:
        """The detached half of `_settle_claim`. Never raises to its caller
        except on a cancellation of its own, which leaves the claim in place -
        the safe direction, at the cost of the count."""
        disposition = self._disposition
        if disposition is None:
            return
        landed = await self._redaction_landed(job)
        if landed is False:
            # The only branch with positive evidence: the event is there and
            # it is not redacted, so the send did not happen and the claim
            # would otherwise turn a transient failure into a permanent one.
            metrics.record_redaction_failure(cause)
            await disposition.release_redaction_claim(job.event_id, claim_id)
            return
        if landed is True:
            # It landed. Counted as a redaction rather than as a failed send,
            # because it IS one - counting it the other way was the same
            # inverted assumption in metric form - and reported through the
            # same path a send that returned normally goes through, so a
            # preserve that arrived underneath is not silent here either.
            metrics.TIER2_REDACTIONS.labels(category=category).inc()
            metrics.record_claim_retained("landed")
            logger.warning(
                "tier2 redaction for %s in %s raised after it had already "
                "landed; the claim is kept, because releasing it would leave "
                "nothing recording that the message was taken down",
                job.event_id,
                job.room_id,
            )
            await self._warn_if_preserved_meanwhile(job)
            return
        metrics.record_redaction_failure(cause)
        metrics.record_claim_retained("unknown")
        logger.error(
            "tier2 could not establish whether the redaction of %s in %s "
            "landed, so the claim is kept: the row says redacted and nobody "
            "can say whether it is, which a human has to resolve",
            job.event_id,
            job.room_id,
        )

    async def _redaction_landed(self, job: ModerationJob) -> Optional[bool]:
        """Did the redaction reach the room? True, False, or None for "we
        could not find out".

        A second reader of the same row as `_is_still_redactable`, and
        deliberately not the same function: the two want OPPOSITE things from
        an unknown. That one skips a redaction it cannot justify; this one
        keeps a claim it cannot justify giving back. One `except` cannot serve
        two contradictory defaults, and folding them together is how a
        fail-open default reached a decision that must not fail open.

        A missing event is an unknown and not a `False`. It is not evidence
        the redaction did not land - a redacted event is still readable, so
        `None` here means something else happened to it - and guessing in the
        permissive direction is the thing this function exists to stop.
        """
        try:
            store = self._api._hs.get_datastores().main
            existing = await store.get_event(job.event_id, allow_none=True)
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok: the caller counts and logs the unknown this produces.
            # Type and site rather than a traceback, as everywhere else here.
            logger.warning(
                "tier2 could not re-read %s to settle its claim at %s (%s)",
                job.event_id,
                error_site(exc),
                type(exc).__name__,
            )
            return None
        if existing is None:
            return None
        return bool(existing.internal_metadata.is_redacted())

    async def _is_still_redactable(self, job: ModerationJob) -> bool:
        """Re-read the target immediately before sending the redaction.

        This is what makes the redaction idempotent, and the two guarantees
        are worth separating because only one of them is absolute:

        - **In this process it cannot double-redact.** The dispatcher's
          in-flight set holds an event id from the moment a job is accepted
          until it finishes, so there is never more than one job for an event
          id at a time - and there is no `await` between this read and the
          send that follows it, on a single-threaded reactor. So the send only
          ever happens on an event that a fresh read said was not redacted.
        - **Across processes the guarantee is the disposition claim**, not
          this check. Two instances could both read an unredacted event here
          before either sends; only one of them holds the claim
          `_may_redact` took, and the other returned before reaching this
          function. See `moderation.disposition`.

        A read that fails is a skip, not a redaction. Moderation fails open,
        and the precondition for sending is a read that SAID the event is
        still there - not the absence of an answer.

        The cost of a duplicate, for scale, and the reason this stayed a
        guard rather than becoming a lock: Synapse accepts a second redaction
        of an already-redacted event and creates a second redaction event. No
        further content is lost; the damage is noise in the DAG and a second
        notification.
        """
        try:
            store = self._api._hs.get_datastores().main
            existing = await store.get_event(job.event_id, allow_none=True)
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok: fail-open by contract, and logged by type and site
            # rather than as a traceback for the reason on the handler below.
            metrics.record_redaction_skip("lookup_failed")
            logger.warning(
                "tier2 could not re-read %s before redacting at %s (%s); "
                "message stays",
                job.event_id,
                error_site(exc),
                type(exc).__name__,
            )
            return False
        if existing is None:
            metrics.record_redaction_skip("event_missing")
            return False
        if existing.internal_metadata.is_redacted():
            metrics.record_redaction_skip("already_redacted")
            return False
        return True

    async def shutdown(self) -> None:
        """Drain Tier 2. Registered with the homeserver by the dispatcher;
        exposed here so a test can drive it without reaching into privates."""
        if self._dispatcher is not None:
            await self._dispatcher.shutdown()


def _redaction_failure_cause(error: BaseException) -> str:
    """Which bounded label a failed redaction send goes under.

    Type and status code only. See `metrics.REDACTION_FAILURE_CAUSES` for why
    the label is not finer-grained than this.
    """
    if isinstance(error, (AuthError, SynapseError)) and getattr(
        error, "code", None
    ) in (401, 403):
        return "forbidden"
    return "other"


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


# `filename` is the original name of an attachment, which a client shows
# beside or instead of the caption whenever the two differ
# (https://spec.matrix.org/v1.16/client-server-api/#mfile). It is read only
# for the msgtypes that HAVE an attachment: on an `m.text` message a
# `filename` key is not an attachment name, it is a key no client renders, and
# reading it there made an ordinary text message a Tier-1 block.
_ATTACHMENT_MSGTYPES = frozenset({"m.file", "m.image", "m.video", "m.audio"})
# The only `format` for which a client renders `formatted_body`. Without the
# check, `formatted_body` under an unsupported format - which every client
# ignores - was matched, which is a false positive on text nobody sees.
_HTML_FORMAT = "org.matrix.custom.html"


def _surface_text(surface: Mapping[str, Any]) -> Tuple[List[str], bool]:
    """Every displayed string carried by one content surface, and whether any
    of them could not be read.

    Field by field, each inside its own guard, because a surface has three
    independent readings and a failure in one says nothing about the others.
    The `formatted_body` reader is a parser running over attacker-chosen
    input; when it gives up, the plain `body` beside it is still perfectly
    readable and is still what most clients show. Losing it was the bypass.
    """
    parts: List[str] = []
    incomplete = False
    for name, reader in _SURFACE_READERS:
        try:
            read, rearranged = reader(surface)
            parts.extend(read)
            # "Read in an order the reader does not see" counts the same as
            # "could not read": in both cases what the rules were given is not
            # what is on screen, so the message is counted and handed to Tier
            # 2 rather than judged on it.
            incomplete = incomplete or rearranged
        except Exception as exc:
            reraise_if_cancelled(exc)
            incomplete = True
            # The name comes from the TABLE, not from the callable. Reading
            # `reader.__name__` here put an attribute access inside the
            # handler that isolates the fields, and anything that raises
            # there costs the whole surface - which is the isolation this
            # loop exists to provide.
            logger.warning(
                "moderation could not read the %s of a message surface at "
                "%s (%s); the other fields are still checked",
                name,
                error_site(exc),
                type(exc).__name__,
            )
    return parts, incomplete


def _body_field(surface: Mapping[str, Any]) -> Tuple[List[str], bool]:
    return _field_text(surface.get("body")), False


def _filename_field(surface: Mapping[str, Any]) -> Tuple[List[str], bool]:
    # `isinstance` first: a `msgtype` that is a list or an object is unhashable,
    # and the set-membership test then raises `TypeError` out of extraction -
    # which the fail-open handler catches, discarding the outer body that had
    # already been read. A malformed field must not cost the message its check.
    msgtype = surface.get("msgtype")
    if isinstance(msgtype, str) and msgtype in _ATTACHMENT_MSGTYPES:
        return _field_text(surface.get("filename")), False
    return [], False


def _formatted_field(surface: Mapping[str, Any]) -> Tuple[List[str], bool]:
    formatted = surface.get("formatted_body")
    if (
        surface.get("format") != _HTML_FORMAT
        or not isinstance(formatted, str)
        or not formatted.strip()
    ):
        return [], False
    displayed, rearranged = _displayed_reading(formatted)
    return ([displayed] if displayed.strip() else []), rearranged


#: The three independent readings of one content surface, each named for the
#: log line so that nothing inside the per-field guard can raise.
_SURFACE_READERS: Tuple[
    Tuple[str, Callable[[Mapping[str, Any]], Tuple[List[str], bool]]], ...
]


def _field_text(body: Any) -> List[str]:
    parts: List[str] = []
    if isinstance(body, str):
        if body.strip():
            parts.append(body)
    elif body is not None:
        # A non-string here is malformed, and malformed is not the same as
        # absent. Synapse's `EventValidator` runs on events this homeserver
        # CREATES; `on_new_event` also sees events that arrived over
        # federation, where the sending homeserver decides what passes, and a
        # client that renders a non-string field renders `str(...)` of it.
        # Dropping it on a type test is the extraction bypass in its smallest
        # form, so the value is stringified and matched.
        parts.append(str(body))
    return parts


_SURFACE_READERS = (
    ("body", _body_field),
    ("filename", _filename_field),
    ("formatted_body", _formatted_field),
)


# The provider's vocabulary, the unknown-category constant and the name
# normalisation live in `moderation.categories`, because `moderation.severity`
# needs all three and cannot import them from the package that imports it.
# Re-exported under their original private names so nothing that already reads
# them from here has to move.
_PROVIDER_CATEGORIES = PROVIDER_CATEGORIES
_normalize_category = normalize_category
# Used when the service flags a message and names no category at all.
UNNAMED_CATEGORY = "flagged"

# Categories whose disposition is to LEAVE THE MESSAGE UP. Redaction is the
# right answer to content that harms the room; it is the wrong answer to a
# learner disclosing that they intend to harm themselves, where the message is
# a request for help and deleting it helps nobody.
PRESERVE_CATEGORIES = frozenset({"self_harm"})
# The same names written without a separator at all. A provider that sends
# `selfharm` has said the one thing that must never be redacted, and losing a
# disclosure to a missing hyphen is not a trade this feature makes.
_PRESERVE_RUN_ON = frozenset({"selfharm"})

# How much of a message the deterministic matcher reads, matching what the
# endpoint reads. `/choreo/moderate` truncates its input at 10,000 characters
# (`input=text[:_MAX_TEXT_LEN]`), so a matcher that read further would report
# disagreements that are an artifact of the cut rather than of the model - and
# the agreement matrix only means something if both sides saw the same text.
# It also bounds the scan, which runs on the reactor thread inside the worker.
MATCHER_MAX_CHARS = 10_000


def _usable_categories(categories: Any) -> TypeGuard[List[str]]:
    """Can a redaction decision be taken on this category list at all?

    A non-empty list of strings, and nothing else. `categories: null` is a
    PRESENT key with an unusable value and `result.get(...) or ()` read it as
    an empty list, which summarised to `flagged` and redacted - so a response
    that failed to name its category could delete a disclosure of self-harm,
    which is the one thing this feature must never do.
    """
    return (
        isinstance(categories, list)
        and bool(categories)
        and all(isinstance(category, str) for category in categories)
    )


def _should_preserve(categories: Iterable[Any]) -> bool:
    """True when any recognised category says to leave the message standing.

    Every category is examined, not just the one `_summarize_categories` picks
    for the log line. That function returns the FIRST recognised category, so a
    verdict of `["harassment", "self-harm/intent"]` summarises as `harassment`
    — and deciding on the summary would have redacted a self-harm disclosure
    because a second label sorted ahead of it.

    Preserving therefore wins over redacting when a verdict carries both. A
    message that is genuinely both is the hardest case and the reasoning is
    the same as for the simple one: a learner in crisis who is also being
    abusive still needs the disclosure to survive, and the abuse is answerable
    by a human who can see it. The cost is that a redaction the room wanted
    does not happen, which stays true until somebody is notified — see
    `_check_and_redact`.
    """
    return any(_preserves(category) for category in categories)


def _preserves(category: Any) -> bool:
    """Does this one category name say "leave the message standing"?

    The documented vocabulary first, and then the PREFIX, because the
    vocabulary check alone had a hole the provider can open on its own: a
    category we do not recognise normalises to `other`, and `other` redacts -
    so `self-harm/invented`, a sub-category added upstream after our fixture
    was pinned, would have deleted a disclosure. A name we do not know that
    is nonetheless plainly self-harm is treated as self-harm; the cost is a
    redaction that does not happen, and the alternative cost is the one thing
    this feature must never do.
    """
    if _normalize_category(category) in PRESERVE_CATEGORIES:
        return True
    if not isinstance(category, str):
        return False
    # Stripped, and every separator folded, because the point of this branch
    # is to recognise a name we do NOT have in our vocabulary - so it cannot
    # depend on the service spelling it the way we would. A leading space
    # defeated it: `" self-harm/intent"` normalised to `" self_harm"`, fell
    # through to `other`, and `other` redacts.
    head = category.strip().casefold().split("/", 1)[0]
    head = head.replace("-", "_").replace(" ", "_")
    return head in PRESERVE_CATEGORIES or head.replace("_", "") in _PRESERVE_RUN_ON


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
