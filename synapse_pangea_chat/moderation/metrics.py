"""Prometheus collectors for the moderation module.

Plain `prometheus_client`, which Synapse already depends on and exposes
through the same default registry its own metrics use. Synapse's `LaterGauge`
is deliberately not used: 1.159 wants `register_hook(...)` after construction
while 1.124 takes a constructor callback, which is a compatibility hazard
bought for nothing.

**Every collector goes through `_get_or_create`.** Relying on Python's module
cache is correct for a repeated `import` and wrong for `importlib.reload`,
which re-executes the registrations and raises `ValueError: Duplicated
timeseries in CollectorRegistry`. A module that cannot be reloaded cannot be
tested, and the failure surfaces as an unrelated import error somewhere else.

**Label cardinality is bounded by construction.** No label value derives from
a room id, an event id or a user id, and the two label sets that could grow -
drop causes and verdict categories - are closed sets checked at the call site.
An unbounded label is a memory leak in the scrape path, reached from data a
remote service controls.
"""

from typing import Any, Dict, Iterable, Optional

from prometheus_client import REGISTRY, Counter, Gauge, Histogram

from synapse_pangea_chat.moderation.severity import (
    BASIS_ABOVE,
    BASIS_BELOW,
    BASIS_NO_SCORES,
)


def _get_or_create(
    collector_class: Any,
    name: str,
    documentation: str,
    labelnames: Iterable[str] = (),
    **kwargs: Any,
) -> Any:
    """Return the collector registered under ``name``, creating it if absent.

    `_names_to_collectors` is private and there is no public equivalent -
    `prometheus_client` offers no lookup-by-name at all, and the registry
    raises on a duplicate rather than returning what is there. The fallback
    below is what makes the reach-through safe: if a future version renames
    the attribute, this degrades to the plain constructor and the duplicate
    raises loudly at import, which is a failure a test sees.
    """
    registered = getattr(REGISTRY, "_names_to_collectors", {})
    existing = registered.get(name)
    if existing is not None:
        return existing
    return collector_class(name, documentation, list(labelnames), **kwargs)


# Tier 1's own verdict counters are deliberately absent. Declaring a metric
# nothing increments publishes a series that reads as a steady zero - which an
# operator cannot tell from "the thing never happens", and which is worse than
# no series at all. They belong to the change that instruments the pre-filter.

# The two tiers, named once. Every per-tier label set is this set: two
# spellings of "which tier" drift apart the first time one of them gains a
# value, and a label the validator does not know is a series nobody alerts on.
TIERS = frozenset({"tier1", "tier2"})

# --- What each tier could read -------------------------------------------

MESSAGE_EVENTS = _get_or_create(
    Counter,
    "pangea_moderation_message_events_total",
    "Message-bearing events each tier was offered, by whether the tier could "
    "read them. `encrypted` is the structural limit: in an E2EE room Synapse "
    "holds a megolm envelope and no plaintext, so neither tier can moderate "
    "one. The point of the split is the RATIO - "
    "encrypted/(encrypted+plaintext) is the share of traffic moderation is "
    "blind to, and without a denominator that share was not computable from "
    "any series at all. Counted after the exempt-sender filter, so an exempt "
    "bot is not reported as a gap encryption caused.",
    ["tier", "encryption"],
)

# Whether the tier could read the event, and the whole of the vocabulary. Not
# a boolean label and not a bare `encrypted_total`: the question an operator
# asks is a fraction, and a numerator with no denominator cannot answer one.
ENCRYPTION_STATES = frozenset({"plaintext", "encrypted"})

# --- Extraction ----------------------------------------------------------

EXTRACTION_INCOMPLETE = _get_or_create(
    Counter,
    "pangea_moderation_extraction_incomplete_total",
    "Messages whose displayed text could not be read in full, by tier. The "
    "message was still checked on whatever text WAS read.",
    ["tier"],
)

# Both tiers, because the same message is read twice and either read can fail
# on its own - and an operator needs to know which tier is blind.
EXTRACTION_TIERS = TIERS

TIER1_FAILED = _get_or_create(
    Counter,
    "pangea_moderation_tier1_failed_total",
    "Messages Tier 1 could not evaluate at all. It fails open, so the send "
    "went through unchecked by the blocking tier; Tier 2 still sees it.",
)

# --- Tier 2 queue and dispatch -------------------------------------------

TIER2_ENQUEUED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_enqueued_total",
    "Messages accepted onto the Tier 2 moderation queue.",
)

TIER2_DROPPED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_dropped_total",
    "Messages the Tier 2 path did not check, by cause.",
    ["cause"],
)

TIER2_CHECKS = _get_or_create(
    Counter,
    "pangea_moderation_tier2_checks_total",
    "Tier 2 moderation calls by outcome.",
    ["outcome"],
)

TIER2_REDACTIONS = _get_or_create(
    Counter,
    "pangea_moderation_tier2_redactions_total",
    "Messages redacted by Tier 2, by verdict category.",
    ["category"],
)

TIER2_REDACTION_FAILED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_redaction_failed_total",
    "Tier 2 redactions that could not be sent, by cause.",
    ["cause"],
)

TIER2_SUPPRESSED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_suppressed_total",
    "Flagged messages deliberately left standing, by verdict category.",
    ["category"],
)

TIER2_REDACTION_SKIPPED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_redaction_skipped_total",
    "Redactions not attempted because a pre-send re-read said not to.",
    ["cause"],
)

TIER2_TRUNCATED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_truncated_total",
    "Messages longer than the moderation endpoint reads. The verdict covers "
    "the prefix only; the remainder was sent and never judged.",
)

MATCHER_AGREEMENT = _get_or_create(
    Counter,
    "pangea_moderation_tier2_matcher_agreement_total",
    "Tier 2 messages by what the moderation service said and what the "
    "deterministic wordlist matcher said, so the two can be compared. The "
    "matcher does not redact; this is what would justify letting it.",
    ["service", "matcher"],
)

# What the service said about the message. `no_verdict` is not an outcome the
# service produced - it is every route by which we have no opinion from it at
# all (transport failure, timeout, open breaker, `evaluated: false`), and it
# is kept separate because a matcher hit on a message the service never judged
# is a coverage gap of a completely different kind from one it judged clean.
MATCHER_SERVICE_STATES = frozenset({"flagged", "clean", "no_verdict"})

# What the wordlist matcher said. `error` rather than folding a failure into
# `miss`: a matcher that broke did not find the message clean, and reading it
# as clean is the fail-silent shape this module counts everywhere else.
MATCHER_STATES = frozenset({"hit", "miss", "error"})

TIER2_DISPOSITION_WRITE_FAILED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_disposition_write_failed_total",
    "Preserved dispositions that could not be written to the durable table. "
    "The message was still preserved; what is missing is the record that "
    "binds a restart and a second instance.",
)

TIER2_DISPOSITION_UNWRITTEN = _get_or_create(
    Gauge,
    "pangea_moderation_tier2_disposition_unwritten",
    "Preserved dispositions still waiting to be written to the durable table. "
    "A GAUGE and not a counter, because the number that matters is how many "
    "are outstanding RIGHT NOW: a row the database will never take stands at "
    "one here forever, which is the only way anybody finds out. The messages "
    "are still preserved and still protected from a claim in this process; "
    "what is missing is the record that binds a restart and a second "
    "instance, and unrelated events are no longer refused a redaction "
    "while it is.",
)

TIER2_REDACTED_AFTER_PRESERVE = _get_or_create(
    Counter,
    "pangea_moderation_tier2_redacted_after_preserve_total",
    "Redactions that landed on an event another instance preserved while the "
    "send was in flight. A disclosure was removed and a human has to know; "
    "this is nonzero only when two instances both run background tasks.",
)

TIER2_CLAIM_STRANDED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_claim_stranded_total",
    "Redaction claims that could not be given back after no redaction was "
    "sent. The row says the event was redacted and it was not, so nothing "
    "will ever take that message down; a human has to clear the row.",
)

TIER2_CLAIM_RETAINED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_claim_retained_total",
    "Redaction claims deliberately NOT given back after the send raised, by "
    "what a re-read of the event established. `landed` means the redaction is "
    "durably in the room and the raise arrived afterwards; `unknown` means the "
    "re-read could not say. Distinct from `claim_stranded`, which is a release "
    "that was attempted and failed: this one was never attempted, because "
    "giving a claim back is only correct when the send provably did not "
    "happen. Both leave a row a human may have to clear.",
    ["evidence"],
)

# What the re-read after a raised send established. Two values, because they
# are the two ways a release is NOT justified, and an operator reads them
# differently: `landed` is a message that is gone, `unknown` is a message
# whose state nobody can establish.
CLAIM_RETENTION_EVIDENCE = frozenset({"landed", "unknown"})

TIER2_WORKERS_RESTARTED = _get_or_create(
    Counter,
    "pangea_moderation_tier2_workers_restarted_total",
    "Tier 2 worker loops the supervisor had to restart.",
)

TIER2_QUEUE_DEPTH = _get_or_create(
    Gauge,
    "pangea_moderation_tier2_queue_depth",
    "Messages waiting on the Tier 2 moderation queue.",
)

TIER2_INFLIGHT = _get_or_create(
    Gauge,
    "pangea_moderation_tier2_inflight",
    "Tier 2 moderation jobs a worker is currently running.",
)

TIER2_BREAKER_STATE = _get_or_create(
    Gauge,
    "pangea_moderation_tier2_breaker_state",
    "Tier 2 choreo circuit breaker: 0 closed, 1 half-open, 2 open.",
)

TIER2_BATCH_SIZE = _get_or_create(
    Histogram,
    "pangea_moderation_tier2_batch_size",
    "Messages carried by one batched Tier 2 provider call. `_count` is the "
    "number of batch calls and `_sum` the messages they carried, so the ratio "
    "is the amortisation batching is actually achieving - which is the number "
    "the capacity arithmetic assumes and the one that collapses first when "
    "the queue is empty.",
    buckets=(1, 2, 4, 8, 16, 32, 64, 128, 256, float("inf")),
)

# A GAUGE and not a counter, because it is a state rather than an event: the
# question an operator has is "is this deployment batching at all", and on a
# staging homeserver running an un-upgraded choreo the answer is no for the
# life of the process. Pinned at 0 on an upgraded deployment; 1 is a
# deployment whose Tier 2 capacity is back to one message per provider call.
TIER2_BATCH_UNSUPPORTED = _get_or_create(
    Gauge,
    "pangea_moderation_tier2_batch_unsupported",
    "1 when the moderation endpoint has refused a batched request and Tier 2 "
    "has fallen back to single-text calls for the life of this process.",
)

# The SCREEN's own per-item verdict, which is not the same thing as a check:
# a screen result that says `flagged` decides nothing, because the message is
# then re-asked one text at a time and that answer is what redacts. This is
# what makes the confirmation traffic visible - `flagged` here is one extra
# provider call each, and it is the term in the capacity arithmetic that is a
# guess rather than a measurement.
SCREEN_VERDICTS = frozenset({"clean", "flagged", "no_verdict"})

TIER2_SCREEN = _get_or_create(
    Counter,
    "pangea_moderation_tier2_screen_total",
    "Per-message outcome of the batched Tier 2 screen, before confirmation.",
    ["verdict"],
)

TIER2_SHUTDOWN_INFLIGHT = _get_or_create(
    Gauge,
    "pangea_moderation_tier2_shutdown_inflight",
    "Checks that were still running when a shutdown began. Emitted "
    "synchronously, so it survives a reactor that stops before the drain "
    "finishes.",
)

TIER2_ACTIVE = _get_or_create(
    Gauge,
    "pangea_moderation_tier2_active",
    "1 on the instance running Tier 2, 0 elsewhere.",
)

TIER2_LATENCY = _get_or_create(
    Histogram,
    "pangea_moderation_tier2_latency_seconds",
    "Wall time of one Tier 2 moderation call.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 30.0, float("inf")),
)

TIER2_QUEUE_WAIT = _get_or_create(
    Histogram,
    "pangea_moderation_tier2_queue_wait_seconds",
    "How long an accepted message waited before a worker picked it up.",
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, float("inf")),
)

TIER2_REDACTION_WINDOW = _get_or_create(
    Histogram,
    "pangea_moderation_tier2_redaction_window_seconds",
    "The VISIBLE WINDOW: wall time from a flagged message reaching "
    "`on_new_event` - by which point it has persisted and every member of the "
    "room can read it - to its redaction being sent. Tier 2 moderates after "
    "persist by design, so this gap is inherent and is not being closed; what "
    "was missing is that nobody could say how long it is. It is a LOWER BOUND "
    "on what a reader experiences: it starts at the notifier rather than at "
    "the sender's keystroke, so the client's round trip and the persist "
    "itself are outside it, as is the time a client takes to apply the "
    "redaction it receives. Distinct from `tier2_latency_seconds`, which "
    "times the provider call alone, and from `tier2_queue_wait_seconds`, "
    "which times the wait before it: this is the whole path end to end, "
    "including both of those and the redaction send.",
    buckets=(
        0.25,
        0.5,
        1.0,
        2.0,
        3.0,
        5.0,
        8.0,
        13.0,
        21.0,
        34.0,
        60.0,
        120.0,
        float("inf"),
    ),
)

# --- Severity ------------------------------------------------------------

TIER2_CATEGORY_SCORE = _get_or_create(
    Histogram,
    "pangea_moderation_tier2_category_score",
    "The provider's confidence in each flagged category that Tier 2 weighed, "
    "and which way the threshold went. This is the series an operator tunes "
    "`moderation.tier2_category_thresholds` from: the defaults are a starting "
    "position rather than a measurement, and the two distributions - what was "
    "redacted and what was left standing - are what turn them into numbers "
    "taken from this platform's own traffic. Every weighed category is "
    "observed, not just the one that decided, because the question an "
    "operator asks is about the distribution of evidence and not about which "
    "entry happened to win. Absent entirely when the endpoint sends no "
    '`category_scores`, which is what `severity_basis{basis="no_scores"}` '
    "says out loud.",
    ["category", "outcome"],
    buckets=(
        0.01,
        0.05,
        0.1,
        0.2,
        0.3,
        0.4,
        0.5,
        0.6,
        0.7,
        0.8,
        0.9,
        0.95,
        0.99,
        1.0,
    ),
)

# Which way the threshold went for this score. Two values, and they describe
# the SEVERITY verdict rather than the message's fate: a category at or above
# its threshold may still not be redacted, because the claim, the durable
# preserve or a re-read can each stop the send afterwards. Labelling it with
# the final outcome would make the histogram unusable for the one job it has,
# which is tuning the threshold.
SCORE_OUTCOMES = frozenset({"at_or_above", "below"})

# Imported from the policy rather than restated here. A label set this file
# spelled out on its own would drift from the decision it describes the first
# time a basis was added, and a basis the validator does not know is a verdict
# nobody can see - which is the whole failure this counter exists to end.
SEVERITY_BASES = frozenset({BASIS_ABOVE, BASIS_BELOW, BASIS_NO_SCORES})

TIER2_SEVERITY_BASIS = _get_or_create(
    Counter,
    "pangea_moderation_tier2_severity_basis_total",
    "What decided each flagged verdict: the score cleared its category's "
    "threshold, it fell below, or there was no usable score and the module "
    "fell back to redacting the way it did before thresholds existed. The "
    "third value is the one to watch during rollout - it is how an operator "
    "tells a choreo that reports `category_scores` from one that does not, "
    "and a deployment sitting at 100% `no_scores` has thresholds configured "
    "and none of them in effect.",
    ["basis"],
)

BREAKER_STATE_VALUES: Dict[str, int] = {
    "closed": 0,
    "half_open": 1,
    "open": 2,
}

# Every reason a message can go unchecked. Closed on purpose: a drop is the
# one thing an operator must be able to see, and a typo that invented a new
# label would hide the drop in a series nobody alerts on.
DROP_CAUSES = frozenset(
    {
        # The queue was at capacity when the message arrived.
        "queue_full",
        # The circuit breaker was open, so no call was made.
        "breaker_open",
        # The breaker was half-open and its single probe was already out.
        "breaker_probe_busy",
        # This event id was already queued or running.
        "duplicate",
        # The module is shutting down and the message was still queued.
        "shutdown",
        # The module was shutting down when the message arrived.
        "stopping",
        # A job was still running when the drain deadline expired.
        "drain_timeout",
        # The queue could not be woken - the reactor clock was already down.
        "no_clock",
        # The job reached a worker and raised before producing a verdict.
        "handler_error",
        # The job was cancelled after a worker picked it up.
        "cancelled",
        # Nothing readable could be extracted, so there was nothing to ask
        # about. NOT the same as a message with no text: this one had text and
        # we could not read it.
        "extraction_failed",
        # `on_new_event` raised before the job reached the queue. It fails
        # open by contract, and an uncounted failure there was a message that
        # vanished between the notifier and the queue with nothing to show.
        "dispatch_error",
    }
)

CHECK_OUTCOMES = frozenset({"flagged", "clean", "unevaluated", "error"})

# Deliberately two values, not four. The four failures a self-redaction
# actually has - sender left, elevated redaction power level, remote sender,
# partial failure - are not distinguishable from the exception: Synapse raises
# `AuthError(403, ...)` with errcode `M_FORBIDDEN` for the first two alike, and
# telling them apart needs the room's power levels read back, which is the
# redaction-authorisation work this chunk does not do. Splitting the label on a
# substring of the exception MESSAGE would be both fragile across Synapse
# versions and a route for a Matrix ID to reach a metric, so the label says
# only what can be established: the room refused it, or something else did.
REDACTION_FAILURE_CAUSES = frozenset({"forbidden", "other"})

REDACTION_SKIP_CAUSES = frozenset(
    {
        "already_redacted",
        "event_missing",
        "lookup_failed",
        # The event carries a durable PRESERVED disposition. A self-harm
        # disclosure, protected by an earlier verdict, that a later one wanted
        # to take down.
        "preserved",
        # The drain has ended, so this job was written off before it got
        # here. A shutdown that has returned must not change a room.
        "shutdown",
        # The service flagged the message and named no category we can read,
        # so there is no decision to take - not a redaction and not a
        # preserve.
        "unusable_verdict",
        # Every flagged category scored strictly below its configured
        # threshold, so the message stands. NOT a preserve - nothing durable
        # is written and a later, more severe verdict on the same event
        # decides on its own merits.
        "below_threshold",
        # The disposition table could not be read, so we cannot establish that
        # this event was NOT preserved. The one place this module refuses to
        # act on an unknown rather than carrying on - see
        # `moderation.disposition`.
        "disposition_unknown",
    }
)


def record_drop(cause: str, count: int = 1) -> None:
    """Count a message the Tier 2 path did not check.

    Raises on an undeclared cause rather than creating the series. A silent
    drop is the failure this whole counter exists to make visible, and a drop
    filed under a label nobody knows about is a silent drop.
    """
    if cause not in DROP_CAUSES:
        raise ValueError(f"unknown moderation drop cause {cause!r}")
    TIER2_DROPPED.labels(cause=cause).inc(count)


def record_message_event(tier: str, encryption: str) -> None:
    """Count one message-bearing event a tier was offered.

    Both labels are validated against closed sets, for the reason every label
    here is: this pair exists to let an operator SEE the share of traffic
    moderation cannot read, and a value filed under a label nobody knows about
    is exactly the invisibility the counter was added to end.
    """
    if tier not in TIERS:
        raise ValueError(f"unknown moderation tier {tier!r}")
    if encryption not in ENCRYPTION_STATES:
        raise ValueError(f"unknown moderation encryption state {encryption!r}")
    MESSAGE_EVENTS.labels(tier=tier, encryption=encryption).inc()


def record_extraction_incomplete(tier: str) -> None:
    """Count a message whose displayed text could not be read in full.

    Raises on an unknown tier for the same reason `record_drop` does: this
    counter exists to make an unknown visible, and an unknown filed under a
    label nobody alerts on is still invisible.
    """
    if tier not in EXTRACTION_TIERS:
        raise ValueError(f"unknown moderation extraction tier {tier!r}")
    EXTRACTION_INCOMPLETE.labels(tier=tier).inc()


def record_check(outcome: str, count: int = 1) -> None:
    """Count the verdict that DECIDED one message's disposition.

    Exactly one increment per message that reached a check, which is the
    invariant batching had to preserve: a batched screen records `clean` for
    the items it clears and records NOTHING for the ones it flags, because a
    flagged item is re-asked one text at a time and it is that answer which
    decides. Counting both would report two checks for one message and halve
    every rate derived from this series.
    """
    if outcome not in CHECK_OUTCOMES:
        raise ValueError(f"unknown moderation check outcome {outcome!r}")
    TIER2_CHECKS.labels(outcome=outcome).inc(count)


def record_screen(verdict: str, count: int = 1) -> None:
    """Count one message the batched screen looked at.

    Validated against a closed set like every other label here: the screen is
    what stands between a message and a confirmation call, and a verdict filed
    under a label nobody knows about is a message whose fate is unaccounted
    for.
    """
    if verdict not in SCREEN_VERDICTS:
        raise ValueError(f"unknown moderation screen verdict {verdict!r}")
    TIER2_SCREEN.labels(verdict=verdict).inc(count)


def record_redaction_failure(cause: str) -> None:
    if cause not in REDACTION_FAILURE_CAUSES:
        raise ValueError(f"unknown moderation redaction failure cause {cause!r}")
    TIER2_REDACTION_FAILED.labels(cause=cause).inc()


def record_claim_retained(evidence: str) -> None:
    """Count a redaction claim kept on purpose, and say on what evidence.

    Validated against a closed set like every other label here: a retained
    claim is a row somebody may have to clear by hand, and one filed under a
    label nobody alerts on is a row nobody clears.
    """
    if evidence not in CLAIM_RETENTION_EVIDENCE:
        raise ValueError(f"unknown moderation claim retention evidence {evidence!r}")
    TIER2_CLAIM_RETAINED.labels(evidence=evidence).inc()


def record_matcher_agreement(service: str, matcher: str) -> None:
    """Count one cell of the service/matcher agreement matrix."""
    if service not in MATCHER_SERVICE_STATES:
        raise ValueError(f"unknown moderation matcher service state {service!r}")
    if matcher not in MATCHER_STATES:
        raise ValueError(f"unknown moderation matcher state {matcher!r}")
    MATCHER_AGREEMENT.labels(service=service, matcher=matcher).inc()


def record_redaction_skip(cause: str) -> None:
    """Count a redaction that was not attempted, and say why.

    Validated like every other closed label set. The collector used to be
    incremented through `.labels(...)` at the call sites, which is the one
    shape that can invent a series: `REDACTION_SKIP_CAUSES` existed and
    nothing consulted it.
    """
    if cause not in REDACTION_SKIP_CAUSES:
        raise ValueError(f"unknown moderation redaction skip cause {cause!r}")
    TIER2_REDACTION_SKIPPED.labels(cause=cause).inc()


def record_category_score(category: str, outcome: str, score: float) -> None:
    """Observe one flagged category's score and which way its threshold went.

    The category label is the NORMALISED name, never the wire name the service
    sent: the normalisation is what bounds this label to seven values, and a
    provider that invents a category must not be able to create a series by
    doing so. `outcome` is validated for the reason every closed label set
    here is - a value nobody knows about is a series nobody alerts on, and
    this one exists to be read.
    """
    if outcome not in SCORE_OUTCOMES:
        raise ValueError(f"unknown moderation score outcome {outcome!r}")
    TIER2_CATEGORY_SCORE.labels(category=category, outcome=outcome).observe(score)


def record_severity_basis(basis: str) -> None:
    """Count what decided one flagged verdict."""
    if basis not in SEVERITY_BASES:
        raise ValueError(f"unknown moderation severity basis {basis!r}")
    TIER2_SEVERITY_BASIS.labels(basis=basis).inc()


def set_breaker_state(state: str) -> None:
    value: Optional[int] = BREAKER_STATE_VALUES.get(state)
    if value is None:
        raise ValueError(f"unknown breaker state {state!r}")
    TIER2_BREAKER_STATE.set(value)


# The gauge has to carry a value before anything happens, or an operator
# cannot tell "closed" from "this module never loaded".
set_breaker_state("closed")
