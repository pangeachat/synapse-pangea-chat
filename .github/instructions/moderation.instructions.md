---
applyTo: "synapse_pangea_chat/moderation/**,tests/*moderation*"
description: "Server-side chat moderation rollout — the two-tier design's wiring decisions: package choices, self-redaction disposition, fail-open contract, and what this module deliberately skips."
---

# Server-Side Chat Moderation

The design — why moderation lives at the homeserver, the two-tier split, and the encryption limit — is owned by the org [trust-and-safety doc](../../../.github/.github/instructions/trust-and-safety.instructions.md); read its **Server-side moderation** section first. This doc records the wiring decisions that doc delegates here. Rollout is tracked in [2-step-choreographer#1746](https://github.com/pangeachat/2-step-choreographer/issues/1746).

Both tiers ship dark: nothing runs until an operator enables a tier in the module's `moderation` config, and Tier 2 refuses to start half-configured — base URL and service token are required at parse time, and the base URL must be a fetchable `http`/`https` URL with a host and no query, fragment or embedded credentials. A non-empty string that is not a URL (`ftp://choreo.invalid`, or a bare hostname) used to start cleanly and then fail on every message inside the fail-open handler, which is indistinguishable from moderation being switched off. A plaintext `http` base URL parses, for local stacks, and logs a WARNING: the service token and every moderated message cross the network in the clear.

**Unknown keys in the `moderation` block are refused at parse time.** Every key in it turns moderation on, so a key silently ignored is a tier silently dark: `tier1_enable: true` (no `d`) parsed cleanly, registered no callback and logged nothing at any level. The error names the key it did not recognise and lists the ones it knows. Configured bot accounts are exempt from both tiers — bot content is governed upstream, and redacting the bot's replies would fight the orchestrator.

## Exempt senders

`moderation.exempt_user_id_globs` lists the senders neither tier ever sees. An exemption is a security boundary, not a convenience filter, so two rules hold.

**Glob syntax, not regular expressions.** `*` matches any run of characters, `?` matches exactly one, and every other character is a literal drawn from the set a Matrix ID is built from (`a-z A-Z 0-9 . _ = / + - @ :`). Anything else — a backslash, a character class, an alternation, an anchor — is refused at parse time. The values only ever describe Matrix IDs, so a regular expression buys nothing, and it is evaluated in the pre-persist send path on every message: a configured pattern such as `@(a+)+:example.org` backtracks catastrophically against a long non-matching Matrix ID and stalls the reactor, where the module's fail-open handling cannot reach it. A consequence worth knowing: a server name written as an IPv6 literal cannot be matched, because `[` and `]` are outside the grammar. Use `*` for the server part.

**The match is whole-string.** `@bot*:example.org` exempts `@bot:example.org` and `@bot-staging:example.org`, and does not exempt `@botimposter:example.org.evil.com`.

A glob of only `*` characters exempts every sender on every homeserver. It parses — that is an operator's call to make — and logs a WARNING naming it.

### Migrating from `exempt_user_id_patterns`

The former key took regular expressions applied with `re.match`, which anchored only the start: `@bot.*:example\.org` exempted `@botimposter:example.org.evil.com` from both tiers. Startup now **refuses** the old key outright — its presence is what is refused, so an empty list or a bare `exempt_user_id_patterns:` is refused too — and names each configured value with a suggested glob. Nothing is translated automatically, because the two grammars overlap and disagree — `@bot?:example.org` is valid in both, and means `@bot:example.org` as a regex and `@bota:example.org` as a glob — so an automatic conversion could silently widen an exemption. Restate each value and check it says what you meant.

## Tier 1 — deterministic pre-filter (blocks on send)

Runs in the send path and can reject a message before it appears, so everything here must stay model-free and sub-millisecond. A wrongly blocked ordinary message is worse than a miss (Tier 2 and human reporting back this up), so every pattern leans conservative:

- **Phone numbers** — the `phonenumbers` library (libphonenumber port), matching valid numbers only. Bare national formats match for the configured regions; international `+CC` formats match regardless. A bare year or house count does not trip it. `moderation.tier1_phone_regions` is checked against libphonenumber's own region list at parse time and must name at least one region: the matcher loops over the regions it is given, so `[]`, `"us"` and `"US "` are all lists of strings that make the phone rule find nothing and say nothing. Use uppercase ISO 3166-1 alpha-2 codes. There is no way to ask for international formats only — libphonenumber still needs a region to match against.
- **Street addresses** — an in-repo pattern requiring house number, name words, and a street-suffix word. English-centric by design; no city/zip-only matching.
- **Profanity** — a curated per-language wordlist covering the languages our learners use, matched through an evasion-resistant normalizer (case, diacritics, leetspeak and homoglyph folding, invisible characters, elongation, and letters spaced apart all reduce to the same needle). An English-only wordlist was the first implementation and was blind in 23 of our 24 full-support languages.

The address pattern stays English-centric, with Tier 2 as its backstop.

**A failure inside Tier 1 allows the message, and it does so for the WHOLE tier.** If any rule cannot complete, the tier has no verdict and the message goes through (logged, and Tier 2 still sees it). The predecessor caught the phone matcher's exception inside `contains_phone_number` and returned `False`, which is not "no phone number here" but "we do not know" wearing the answer's clothes: the address rule then ran and the message was rejected on the strength of a Tier 1 run that had already failed. A partial failure is never laundered into a clean negative — `check_text` runs the rules from one table and raises `Tier1RuleError` on any of them, so a rule added later inherits the policy without anybody remembering to give it one.

Each check has a rule identifier, which is what appears in logs:

| Rule identifier | The check that fired |
|---|---|
| `contact_details` | phone numbers |
| `location_details` | street addresses |
| `profanity` | the wordlist |


### What Tier 1 deliberately does not block

Because Tier 1 blocks before a message is sent, a false positive silences an innocent learner, which is worse than a miss that Tier 2 can still catch. Terms whose ordinary meaning is common therefore stay out of the blocking wordlist and are left to Tier 2's contextual judgement — animal words used as insults (Malay *babi*, Danish *svin*), body or object words (Polish *pedał*, a bicycle pedal), place and people names (the country Niger, Italian *Troia*), scientific vocabulary (*Homo* sapiens), and medical terms (Dutch *kanker*). The reason for each exclusion is recorded alongside the test corpus, which is also where a benign word wrongly caught by a needle is allowlisted.

One collision cannot be resolved by exclusion: stripping diacritics merges Slovak *pica* (a typography unit) with Czech and Slovak *píča* (a common slur). We block it, accepting the rare false positive on the typography term. Revisit if a real learner report shows the benign use.

## Tier 2 — LLM moderation (redacts after)

Fires after an event persists, from a background task so event persistence never waits on HTTP. It calls the choreographer's shared moderation handler (the org doc's single engine rule) authenticated as a dedicated moderation service account — the endpoint accepts any valid token on this homeserver. Category names are normalized onto the orchestrator's flag vocabulary so both moderation paths speak one language.

Category names are checked against the provider's documented vocabulary before they are used. A category is a free-form string from a service we do not run, and it ends up in a log line and in the redaction reason that lands in the room, so anything outside the documented list — including anything that is not a string — becomes the constant `other` and the value the service sent is discarded. A response of `{"flagged": true, "categories": ["@alice:example.org"]}` otherwise published that Matrix ID to the room and to the logs by a route no review of our own format strings would find. The client validates the response's shape at the same boundary: `flagged` must be a bool and `categories` a list of strings, or there is no verdict.

**Disposition is self-redaction: the redaction is sent as the offending sender.** The module send path enforces normal room power levels, and no service user is a member of every room — but a sender may always redact their own message, so self-redaction works in every room, DMs included. The moderation reason rides on the redaction event, prefixed so clients and audits can tell moderation redactions from ordinary ones. (This corrects the org doc's assumption that the module send path is privileged; it is not.)

Tier 2 skips rooms carrying an activity-plan state event: the conversation orchestrator already moderates activity sessions, and a second check would double-redact and double-spend. Every failure in the check-and-redact path is logged and fails open, mirroring the choreo handler's own contract.

### What both tiers read from an event

**The rule: what the rules see is never less than what a reader sees.** An extractor that returns a subset of the displayed text is not a missed detection — it is a bypass any sender can trigger on every message — so where the displayed text is ambiguous, the union of the candidate surfaces is moderated. Over-reading costs a false positive on one message; under-reading costs the tier.

In practice:

- The outer `body` and `formatted_body` are always read. An edit **adds** `m.new_content`'s surfaces rather than replacing them (ADR-8a(0)): modern clients render `m.new_content`, older clients render the outer `* <new text>` fallback, so choosing one leaves the other unmoderated in whichever client renders it.
- `m.new_content` is read **only** when the event's own `m.relates_to.rel_type` is `m.replace`. Preferring it on presence alone was a total bypass of both tiers — `{"msgtype": "m.text", "body": "<payload>", "m.new_content": {}}` is displayed as the payload by every client and extracted as nothing at all. Tier 1 trusts the event's own `rel_type` and does not verify the relation; establishing that the target exists, is in the same room and has the same sender needs a database read the send path cannot afford, so a bogus relation makes Tier 1 read a field no client displays. That direction is a known false positive, and the validated attribution lands with the Tier-2 disposition work.
- Content is read as any `Mapping`, not as `dict`: Synapse's Rust event type and a homeserver running `use_frozen_dicts: true` both hand modules content that is not a plain dict, and a `dict` test on either would silently stop moderating every edit.
- The `msgtype` gate is satisfied by **any** displayed surface, for the same reason.
- `formatted_body` is reduced to displayed text by a real HTML parser, not a regex. Tags are stripped before entities are decoded, so `&lt;I will kill you&gt;` becomes the visible text `<I will kill you>` rather than being decoded into a tag and deleted. Inline elements concatenate (`4<b>1</b>5` is one number on screen) and block elements break the line. `alt` and `title` are read, because a client puts them on screen. A `<` that is not followed by an ASCII letter is a character, not a tag opener, which is what HTML5 says and what the regex got wrong: it deleted `<415-555-2671 >` outright.

Two limits, stated rather than claimed away: `href` is **not** read (clients display the link's text, and a URL full of digits is a plausible phone-rule false positive), and text a client renders only after running a sanitiser we do not run may differ from what this extractor sees.

## What a moderation log line may contain

A moderation log record is not an ordinary diagnostic: it states that a particular person tripped a content filter, and it says something about what they wrote. Application logs are not an access-controlled store, they are shipped to aggregators, and they outlive the decision by months. So the module holds to one rule: **no Matrix ID and no message text ever reaches a log handler.**

What a line does carry is a room id, an event id where one exists, the rule identifier from the table above, and a `sender_digest`. The digest is a keyed hash of the Matrix ID with a key generated once per process: an operator chasing a false positive can see that the same sender tripped a rule repeatedly, and the value cannot be turned back into a Matrix ID — not even by enumerating the homeserver's users, which a plain hash would allow. Correlation stops at the process boundary on purpose; anything longer-lived is a behavioural record of a named person and belongs in the database, behind authorisation. An event id resolves to its sender the same way.

A redaction that cannot be sent is caught inside `_check_and_redact` rather than allowed to propagate. It runs under `run_as_background_process`, which logs whatever reaches it — and Synapse's own "User &lt;mxid&gt; not in room &lt;room&gt;" carries the Matrix ID, which is the ordinary case in a DM the offender leaves. Counting those failures and escalating the legitimate ones is separate work; today the message stays up and the failure is visible.

Exceptions are the channel that is easy to miss. `logger.exception` prints the exception's own message, and a library that fails on a message body routinely quotes that body back, so no moderation handler logs a traceback. Each logs the exception's type and the `file:line` that raised it, which is what identifies a bug, and nothing that came from the message.

Rule identifiers name the **rule**, not the category of personal data it looks for, for the same reason: `rule=phone_number` beside a room id is an assertion about what a specific message contained.

### The identity Synapse attaches, and how it is removed

`synapse.config.logger.one_time_logging_setup` does not attach `LoggingContextFilter` to a logger or a handler — it replaces the process's **log-record factory**, so every record created anywhere in the process is decorated at creation with the in-flight request's `requester` and `authenticated_entity`: the sender's Matrix ID, on every moderation record, whatever the format string says. The shipped `precise` formatter prints neither, which is exactly why this survives review; a structured-logging sink serialises the whole record.

The module removes them. `log_safety.scrubbing_logger` attaches a filter to each of the package's loggers, and the placement is the point: `Logger.handle` runs the logger's own filters **before** `callHandlers` walks the ancestor chain, so one filter covers every handler the record could reach, root handlers and structured sinks included. `requester`, `authenticated_entity`, `ip_address` and `user_agent` are replaced with a constant rather than deleted, so a deployment whose formatter names `%(requester)s` still formats. `request` (the request id), `server_name`, `site_tag`, `method`, `url` and `protocol` are left alone — they name an endpoint and a request, not a person, and they are what makes a record traceable.

**Every logger in `synapse_pangea_chat/moderation/` must be obtained from `scrubbing_logger`, not from `logging.getLogger`.** A logger's filters apply only to records logged through that logger — `callHandlers` inherits an ancestor's handlers, never its filters — so a new file with a bare `getLogger` reopens the channel for its own records. `tests/test_moderation_logging.py::ModerationLoggerCoverageTestCase` walks the package and fails if one is missed.

### What this rule still does not reach

The rule governs records this module creates. One channel sits outside it, and it is a property of Synapse rather than of this module:

- **Synapse's own records, on Synapse's own loggers.** `handle_new_client_event` logs "Denying new event … User &lt;mxid&gt; not in room …" before raising, and `run_as_background_process` calls `logger.exception` on whatever a background task lets escape. A filter on our loggers cannot touch either. The module's answer to the second is to let no exception escape a moderation frame at all — `_check_and_redact` catches the failed self-redaction rather than propagating it. The first is written entirely outside our call stack and cannot be closed from here.

## Deliberately out of scope here

Teacher/course-admin notification of flagged messages, per-room opt-out, and age-conditional strictness are tracked product gaps in the org doc, not behaviors of this module.
