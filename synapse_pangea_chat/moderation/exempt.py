"""Exempt-sender matching for both moderation tiers.

An exempt sender skips Tier 1 and Tier 2 entirely, so this is a security
boundary and not a convenience filter. Two properties matter:

**The match is whole-string.** The original implementation compiled the
configured values as regular expressions and used `re.match`, which anchors
only the start. A deployment that wrote `@bot.*:example\\.org` to exempt its
own bot also exempted `@botimposter:example.org.evil.com` - a remote sender
who picked the localpart could opt out of moderation. Glob matching here is
anchored at both ends.

**The grammar is globs, not regular expressions.** These values only ever
describe Matrix IDs, so arbitrary regex buys nothing, and it is evaluated in
`check_event_for_spam` - the pre-persist send path, on every message. A
configured pattern such as `@(a+)+:example.org` backtracks catastrophically
against a long non-matching Matrix ID, and because the regex engine does not
release the reactor, the module's fail-open exception handling cannot
interrupt it: one line of operator config would stall the homeserver. The
glob grammar has no construct that can do that, and `glob_match` below is an
explicit linear-ish scan rather than a translation back into a regex, so the
property does not depend on how a given CPython version happens to compile
`fnmatch` patterns.

The configuration key is `moderation.exempt_user_id_globs`. The former
`exempt_user_id_patterns` is refused outright rather than reinterpreted:
`@bot?:example.org` is valid under both grammars with different meanings - a
regex `?` makes the preceding character optional, a glob `?` matches one
character - and contains nothing a heuristic could use to tell which was
meant. Silently widening an exemption is the failure this whole module
exists to avoid, so an operator restates the intent instead.
"""

from typing import List

# The glob grammar, stated rather than inherited from `fnmatch`:
#
#   `*`  matches any run of characters, including none
#   `?`  matches exactly one character
#
# Every other character is a literal and must come from the set below - the
# characters a Matrix ID can contain (localpart, `:`, server name, optional
# `:port`). Two exclusions are deliberate:
#
#   `[` and `]` - `fnmatch` reads these as character classes, which would be
#       a second grammar to document and get wrong. The cost is that a server
#       name written as an IPv6 literal cannot be matched; use `*` for the
#       server part, or name the deployment's DNS name instead.
#   `\\` and the regex metacharacters - these are the marks of a value
#       written for the old key, and refusing them is what turns a silent
#       reinterpretation into a startup error the operator can act on.
_LITERAL_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyz" "ABCDEFGHIJKLMNOPQRSTUVWXYZ" "0123456789" "._=/+-@:"
)
_WILDCARDS = frozenset("*?")

LEGACY_CONFIG_KEY = "exempt_user_id_patterns"
CONFIG_KEY = "exempt_user_id_globs"


class ExemptGlobError(ValueError):
    """A configured exempt-sender glob the module refuses to start with."""


def validate_glob(glob: str) -> None:
    """Raise `ExemptGlobError` unless `glob` is a well-formed exempt glob.

    Called at config-parse time so a bad value fails startup once, rather
    than failing - or worse, half-matching - on every message.
    """
    if not isinstance(glob, str) or not glob.strip():
        raise ExemptGlobError(
            f'Config "moderation.{CONFIG_KEY}" entries must be non-empty '
            f"strings; got {glob!r}"
        )
    if glob != glob.strip():
        raise ExemptGlobError(
            f'Config "moderation.{CONFIG_KEY}" entry {glob!r} has leading or '
            "trailing whitespace, which would never match a Matrix ID"
        )
    for character in glob:
        if character in _WILDCARDS or character in _LITERAL_CHARACTERS:
            continue
        raise ExemptGlobError(
            f'Config "moderation.{CONFIG_KEY}" entry {glob!r} contains '
            f"{character!r}, which is not part of the glob grammar. Allowed: "
            "'*' (any run of characters), '?' (one character), and the "
            "characters a Matrix ID is built from. Regular expressions are "
            "not accepted here - see moderation.instructions.md."
        )


def matches_every_sender(glob: str) -> bool:
    """True when the glob exempts every sender on every homeserver.

    Not an error - an operator may genuinely want it - but it disables
    moderation wholesale, so the caller warns rather than passing it over.
    """
    return bool(glob) and set(glob) == {"*"}


def glob_match(glob: str, value: str) -> bool:
    """Whole-string glob match, without a regular expression.

    The standard single-pass wildcard scan: advance through both strings,
    remember the most recent `*` and resume from one character later when a
    literal run fails. Worst case is O(len(glob) * len(value)) and there is
    no construct that can do worse, which is the point - see the module
    docstring.
    """
    glob_index = 0
    value_index = 0
    star_index = -1
    resume_index = 0
    glob_length = len(glob)
    value_length = len(value)

    while value_index < value_length:
        if glob_index < glob_length and (
            glob[glob_index] == "?" or glob[glob_index] == value[value_index]
        ):
            glob_index += 1
            value_index += 1
        elif glob_index < glob_length and glob[glob_index] == "*":
            star_index = glob_index
            glob_index += 1
            resume_index = value_index
        elif star_index >= 0:
            glob_index = star_index + 1
            resume_index += 1
            value_index = resume_index
        else:
            return False

    while glob_index < glob_length and glob[glob_index] == "*":
        glob_index += 1
    return glob_index == glob_length


def suggest_glob(regex_pattern: str) -> str:
    """A best-effort glob for a value written for the old regex key.

    Offered in the migration error so an operator has somewhere to start.
    It is a suggestion and the error says so: a regex can express things a
    glob cannot, and a wrong exemption is exactly the defect being fixed.
    """
    out: List[str] = []
    index = 0
    length = len(regex_pattern)
    while index < length:
        character = regex_pattern[index]
        if character == "\\" and index + 1 < length:
            # An escaped character was a literal in the regex and stays one.
            out.append(regex_pattern[index + 1])
            index += 2
        elif (
            character == "." and index + 1 < length and regex_pattern[index + 1] in "*+"
        ):
            out.append("*")
            index += 2
        elif character == ".":
            out.append("?")
            index += 1
        elif character in "^$":
            # Anchors are implicit in a glob.
            index += 1
        else:
            out.append(character)
            index += 1
    return "".join(out)


def legacy_key_error(values: List[str]) -> str:
    """The message raised when the retired regex key is still configured."""
    lines = [
        f'Config "moderation.{LEGACY_CONFIG_KEY}" has been replaced by '
        f'"moderation.{CONFIG_KEY}", which takes glob patterns rather than '
        "regular expressions.",
        "",
        "The old values are not translated automatically: the two grammars "
        "overlap and disagree (regex '?' makes the previous character "
        "optional, glob '?' matches one character), so translating could "
        "silently change who is exempt from moderation. Restate each value "
        "and check it says what you mean:",
        "",
    ]
    for value in values:
        lines.append(f"  {value!r} -> perhaps {suggest_glob(value)!r}")
    return "\n".join(lines)
