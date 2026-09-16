"""How confident the provider has to be before Tier 2 deletes a message.

**The defect.** `/choreo/moderate` answered `flagged` plus the names of the
categories that tripped, and until the handler started reporting
`category_scores` those two values were the whole of the evidence a caller
had. Observed live: a learner typed `shit` in a language class, the endpoint
answered `flagged: true, categories: ["harassment"]`, and Tier 2 redacted it.
A targeted threat produces a byte-identical verdict. In a classroom mild
swearing is ordinary conversation between people practising a language, and
deleting it is the wrong outcome - but no threshold can be applied to a
verdict that carries no severity at all, so there was no way to reach a
different one.

**Per category, never one global constant.** Google's Gemini safety settings
are the shape to copy: a threshold chosen separately for each harm category,
expressed as a judgement about SEVERITY rather than about the classifier's raw
probability, and documented with the expectation that child-safety categories
are set STRICTER than everything else while ordinary-offence categories are
set looser. The reason is that the two errors do not cost the same thing. A
missed `sexual/minors` on a platform with minors on it is unacceptable at any
confidence; a false positive on `harassment` silences a learner for swearing.
One number cannot express both, and a single global threshold set low enough
for the first is the behaviour this module already had.

**Self-harm never reaches here.** `_check_and_redact` takes the preserve
branch before it asks about severity, so a verdict naming any self-harm
category leaves the message standing whatever it scored, and a threshold is
not a route to redacting one. That ordering is asserted by tests, and this
module refuses a threshold key for a self-harm category outright rather than
accepting an inert one: a config that appears to control the disposition of a
self-harm disclosure would be a lie about what the module does.

**The absent field is the ordinary case, not an error.** Staging runs a choreo
that does not send `category_scores` at all; an upgraded one sends it on every
verdict. Both have to work, and the two must not differ in the direction that
matters. With no readable score for a tripped category there is no evidence
that the message is mild, so the decision is the one the module took before
any of this existed: redact. Severity can make the module more permissive on
EVIDENCE and never on the absence of it - which also means a service that
omits, mistypes or nulls a score cannot talk this module out of a redaction.
(It could always have done so by answering `flagged: false`; what matters is
that the new field grants it no power it did not have.)
"""

import math
from typing import Any, Dict, Mapping, NamedTuple, Optional, Set, Tuple

from synapse_pangea_chat.moderation.categories import (
    PROVIDER_CATEGORIES,
    UNKNOWN_CATEGORY,
    normalize_category,
)

#: The operator's key for the table below.
CONFIG_KEY = "tier2_category_thresholds"

#: The wire names whose disposition is PRESERVE, so a threshold on one could
#: never do anything. Derived from the vocabulary rather than listed, so a
#: sub-category the provider adds under `self-harm/` is covered the day it
#: appears. Refused as a config key - see the module docstring.
PRESERVED_WIRE_CATEGORIES: Tuple[str, ...] = tuple(
    sorted(
        name for name in PROVIDER_CATEGORIES if normalize_category(name) == "self_harm"
    )
)

# A flagged category redacts when its score is AT OR ABOVE its threshold, so
# `0.0` means "every flag of this category redacts" and `1.0` means "nothing
# short of certainty does".
#
# **These are a starting position, not a measurement.** Nobody has yet seen
# this platform's own score distribution, and inventing a precise-looking
# number would be worse than admitting that: what the table has to be is
# per-category, defensible in the direction of each error, and TUNABLE. The
# histogram beside it - `pangea_moderation_tier2_category_score`, recorded on
# both the redact and the leave-standing branch - is the instrument for
# replacing these with numbers from real traffic, and the operator who does
# that is the point of shipping them as config.
#
# Two things anchor the numbers we do have. The provider only NAMES a category
# in `categories` when its own internal threshold tripped, so every value here
# is a second bar on top of the provider's, not the only one. And the observed
# spread on `harassment` is wide: the mild classroom case that prompted all of
# this scored around 0.3, while targeted abuse scores above 0.99, so 0.7
# separates them with room on both sides.
DEFAULT_CATEGORY_THRESHOLDS: Dict[str, float] = {
    # --- Child safety: strictest, and not a trade-off we make. ------------
    # Any flag redacts. This is a language-learning platform with minors on
    # it, and there is no confidence at which leaving this standing to avoid
    # a false positive is the right call.
    "sexual/minors": 0.0,
    # --- Credible threats and violent wrongdoing --------------------------
    # Set low but not at zero: these sub-categories are narrow and the
    # provider does not name them casually, so a flag here is strong evidence
    # on its own - but "I'll kill you at chess" is a real sentence in a
    # classroom and the score is what tells it apart.
    "harassment/threatening": 0.20,
    "hate/threatening": 0.20,
    "illicit/violent": 0.20,
    # --- The ordinary abuse taxonomy, at classroom settings ---------------
    # Slurs, adult sexual content, violence and wrongdoing. Looser than the
    # threatening sub-categories above and stricter than plain harassment,
    # because each of these also fires on ordinary classroom vocabulary: a
    # history lesson is violent, sex education and body vocabulary are
    # `sexual`, and a news discussion is `illicit`.
    "hate": 0.50,
    "sexual": 0.50,
    "violence": 0.50,
    "violence/graphic": 0.50,
    "illicit": 0.50,
    # --- The swearing band ------------------------------------------------
    # The category the observed false positive came back under, and the one
    # where a language classroom differs most from a general-purpose product.
    "harassment": 0.70,
    # --- A name we do not recognise ---------------------------------------
    # Redacts on any flag. An unknown category is not evidence of mildness,
    # and a provider that invents a name must not thereby get a softer
    # disposition than one that uses the documented vocabulary.
    UNKNOWN_CATEGORY: 0.0,
}

#: Every key an operator may set: the documented categories that are not
#: preserved, plus the bucket unrecognised ones fall into.
CONFIGURABLE_KEYS: Tuple[str, ...] = tuple(sorted(DEFAULT_CATEGORY_THRESHOLDS))

#: What decided a verdict, for the metric and the log line.
BASIS_ABOVE = "above_threshold"
BASIS_BELOW = "below_threshold"
#: At least one tripped category had no score this module could read, so
#: severity was not the basis for anything. The value an operator watches to
#: answer "is thresholding actually in effect yet?", which on an un-upgraded
#: choreo is "no, and every flag still redacts".
BASIS_NO_SCORES = "no_scores"


class Weighed(NamedTuple):
    """One tripped category whose score this module could read."""

    #: The normalised name, which is what a bounded metric label may carry.
    category: str
    score: float
    threshold: float


class SeverityDecision(NamedTuple):
    redact: bool
    basis: str
    #: Every tripped category that carried a readable score, for the
    #: histogram. All of them rather than one "deciding" pick: each was
    #: weighed, and an operator tuning a threshold wants the distribution of
    #: what was weighed, not of what happened to win.
    weighed: Tuple[Weighed, ...]
    #: The category that decided, when one did - the first to reach its
    #: threshold on a redaction, the closest call on a message left standing.
    #: None when no score was readable at all.
    driver: Optional[Weighed]


def validate_thresholds(raw: Any) -> Dict[str, float]:
    """Return the configured thresholds merged over the defaults, or raise.

    Strict about keys for the reason every config check in this module is
    strict: a misspelled category name is silent at runtime. The default
    underneath it keeps applying, so an operator who meant to loosen
    `harassment` and wrote `harrassment` gets the old behaviour and no
    indication anywhere that their change did nothing.
    """
    if raw is None:
        return dict(DEFAULT_CATEGORY_THRESHOLDS)
    if not isinstance(raw, Mapping):
        raise ValueError(f'Config "moderation.{CONFIG_KEY}" must be a mapping')
    thresholds = dict(DEFAULT_CATEGORY_THRESHOLDS)
    for key, value in raw.items():
        if key in PRESERVED_WIRE_CATEGORIES:
            raise ValueError(
                f'Config "moderation.{CONFIG_KEY}" key {key!r} cannot be '
                "given a threshold: a self-harm disclosure is never redacted "
                "at any score, so this would be a setting that appears to "
                "control a decision it has no part in"
            )
        if key not in CONFIGURABLE_KEYS:
            raise ValueError(
                f'Config "moderation.{CONFIG_KEY}" key {key!r} is not a '
                "category this provider reports. Allowed keys: "
                f"{', '.join(CONFIGURABLE_KEYS)}"
            )
        # `bool` first: `True` is an `int` in Python, and a threshold of
        # `true` would read as 1.0 and quietly stop that category redacting.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f'Config "moderation.{CONFIG_KEY}" entry {key!r} must be a number'
            )
        if not math.isfinite(value) or not 0.0 <= float(value) <= 1.0:
            raise ValueError(
                f'Config "moderation.{CONFIG_KEY}" entry {key!r} must be '
                "between 0.0 and 1.0"
            )
        thresholds[key] = float(value)
    return thresholds


def threshold_for(wire_name: Any, thresholds: Mapping[str, float]) -> float:
    """The threshold that governs one tripped category.

    Exact wire name first, then the top-level family, then the unknown
    bucket - so `harassment/threatening` can be set apart from `harassment`
    but inherits it when it is not, and a name outside the vocabulary is
    governed by the bucket rather than by whatever its text happens to
    resemble.
    """
    if not isinstance(wire_name, str) or wire_name not in PROVIDER_CATEGORIES:
        return thresholds[UNKNOWN_CATEGORY]
    if wire_name in thresholds:
        return thresholds[wire_name]
    family = wire_name.split("/", 1)[0]
    if family in thresholds:
        return thresholds[family]
    return thresholds[UNKNOWN_CATEGORY]


def _readable_score(scores: Any, wire_name: str) -> Optional[float]:
    """This category's score, or None when there is not one we can use.

    Exact key only. A fallback to the family's score would attribute one
    category's confidence to another - `harassment/threatening` is not
    `harassment`, and treating them as interchangeable is exactly the
    conflation the scores exist to end.

    `bool` is excluded before `int` for the usual Python reason, and a value
    outside `[0, 1]` or a NaN is treated as unreadable rather than clamped: a
    number we cannot interpret is not evidence, and the whole design here is
    that only evidence softens a redaction.
    """
    if not isinstance(scores, Mapping):
        return None
    if wire_name not in PROVIDER_CATEGORIES:
        # An unrecognised name is never thresholdable, whatever the service
        # sent a score under. See `threshold_for`.
        return None
    score = scores.get(wire_name)
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    if not math.isfinite(score) or not 0.0 <= float(score) <= 1.0:
        return None
    return float(score)


def decide(
    categories: Any, scores: Any, thresholds: Mapping[str, float]
) -> SeverityDecision:
    """Whether this flagged verdict is severe enough to redact.

    The rule in one line: **redact unless every tripped category has a
    readable score strictly below its own threshold.** Stated that way round
    on purpose - it is the form that makes the two compatibility properties
    obvious. An absent `category_scores` redacts, which is what the module did
    before. A partially present one redacts, because an unscored category is
    an unknown and an unknown is not mildness.

    **Each distinct category is weighed once.** The rule is `any` over the
    categories, and `any` over a list equals `any` over its set, so the dedupe
    changes no decision - what it removes is an amplification the sender
    controls. A response is data from a service we do not run and the
    transport caps the body at 1 MiB rather than at a category count, so
    `["harassment"] * 75000` is a well-formed verdict; without this, each
    occurrence became its own histogram observation, taken inline on the
    reactor thread. Order is preserved, because the FIRST category to cross
    its threshold is the one reported as the driver.
    """
    weighed = []
    crossed: Optional[Weighed] = None
    unreadable = False
    seen: Set[Any] = set()
    for wire_name in categories:
        if wire_name in seen:
            continue
        seen.add(wire_name)
        threshold = threshold_for(wire_name, thresholds)
        score = _readable_score(scores, wire_name)
        if score is None:
            unreadable = True
            continue
        entry = Weighed(normalize_category(wire_name), score, threshold)
        weighed.append(entry)
        if crossed is None and score >= threshold:
            crossed = entry
    if crossed is not None:
        # One category over its bar is enough. A verdict of
        # `["harassment", "hate/threatening"]` where only the second is severe
        # is a severe message.
        return SeverityDecision(True, BASIS_ABOVE, tuple(weighed), crossed)
    if unreadable or not weighed:
        return SeverityDecision(True, BASIS_NO_SCORES, tuple(weighed), None)
    # The closest call is the driver here, because it is the one an operator
    # tuning this threshold downward would catch first.
    return SeverityDecision(
        False,
        BASIS_BELOW,
        tuple(weighed),
        max(weighed, key=lambda entry: entry.score),
    )
