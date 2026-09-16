"""The provider's category vocabulary, and the one safe way to read a name.

A module of its own, and not because `moderation/__init__.py` was long: the
severity thresholds in `moderation/severity.py` have to know which category
names are real and how a wire name folds onto our own, and they cannot import
that from the package that imports them. One definition, two readers, no
circular import and no second copy to drift.

Nothing here is a policy decision about what to DO with a category - that is
`severity` (how confident the provider has to be) and `moderation/__init__`
(which categories are preserved rather than redacted). This file answers only
"is this a name the provider documents, and what do we call it".
"""

from typing import Any, FrozenSet

# The provider's documented category vocabulary, and the whole of it. A
# category name is a string chosen by a service we do not run; it reaches a log
# line and the redaction reason that lands in a room, so it is checked against
# this list rather than trusted. A response of
# `{"flagged": true, "categories": ["@alice:example.org"]}` otherwise logs that
# Matrix ID verbatim and writes it into a room, and
# `["<the message body>"]` does the same for a message body - by a route no
# review of our own format strings would find, because our format string is
# `category=%s` and looks harmless.
PROVIDER_CATEGORIES: FrozenSet[str] = frozenset(
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


def normalize_category(category: Any) -> str:
    """Map a provider category name onto the orchestrator's flag vocabulary.

    `self-harm/intent` -> `self_harm`, so both moderation code paths speak one
    vocabulary. Anything outside the documented list - including anything that
    is not a string - becomes `other`, and the value the service sent is
    discarded here rather than carried one frame further.
    """
    if not isinstance(category, str) or category not in PROVIDER_CATEGORIES:
        return UNKNOWN_CATEGORY
    return category.split("/", 1)[0].replace("-", "_")
