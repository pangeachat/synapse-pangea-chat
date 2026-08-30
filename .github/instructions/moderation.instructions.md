---
applyTo: "synapse_pangea_chat/moderation/**,tests/*moderation*"
description: "Server-side chat moderation rollout — the two-tier design's wiring decisions: package choices, self-redaction disposition, fail-open contract, and what this module deliberately skips."
---

# Server-Side Chat Moderation

The design — why moderation lives at the homeserver, the two-tier split, and the encryption limit — is owned by the org [trust-and-safety doc](../../../.github/.github/instructions/trust-and-safety.instructions.md); read its **Server-side moderation** section first. This doc records the wiring decisions that doc delegates here. Rollout is tracked in [2-step-choreographer#1746](https://github.com/pangeachat/2-step-choreographer/issues/1746).

Both tiers ship dark: nothing runs until an operator enables a tier in the module's `moderation` config, and Tier 2 refuses to start half-configured (base URL and service token are required at parse time). Configured bot accounts are exempt from both tiers — bot content is governed upstream, and redacting the bot's replies would fight the orchestrator.

## Tier 1 — deterministic pre-filter (blocks on send)

Runs in the send path and can reject a message before it appears, so everything here must stay model-free and sub-millisecond. A wrongly blocked ordinary message is worse than a miss (Tier 2 and human reporting back this up), so every pattern leans conservative:

- **Phone numbers** — the `phonenumbers` library (libphonenumber port), matching valid numbers only. Bare national formats match for the configured regions; international `+CC` formats match regardless. A bare year or house count does not trip it.
- **Street addresses** — an in-repo pattern requiring house number, name words, and a street-suffix word. English-centric by design; no city/zip-only matching.
- **Profanity** — a curated per-language wordlist covering the languages our learners use, matched through an evasion-resistant normalizer (case, diacritics, leetspeak and homoglyph folding, invisible characters, elongation, and letters spaced apart all reduce to the same needle). An English-only wordlist was the first implementation and was blind in 23 of our 24 full-support languages.

The address pattern stays English-centric, with Tier 2 as its backstop. A failure inside Tier 1 allows the message (fail open, logged) — a moderation bug must never block all sends.

### What Tier 1 deliberately does not block

Because Tier 1 blocks before a message is sent, a false positive silences an innocent learner, which is worse than a miss that Tier 2 can still catch. Terms whose ordinary meaning is common therefore stay out of the blocking wordlist and are left to Tier 2's contextual judgement — animal words used as insults (Malay *babi*, Danish *svin*), body or object words (Polish *pedał*, a bicycle pedal), place and people names (the country Niger, Italian *Troia*), scientific vocabulary (*Homo* sapiens), and medical terms (Dutch *kanker*). The reason for each exclusion is recorded alongside the test corpus, which is also where a benign word wrongly caught by a needle is allowlisted.

One collision cannot be resolved by exclusion: stripping diacritics merges Slovak *pica* (a typography unit) with Czech and Slovak *píča* (a common slur). We block it, accepting the rare false positive on the typography term. Revisit if a real learner report shows the benign use.

## Tier 2 — LLM moderation (redacts after)

Fires after an event persists, from a background task so event persistence never waits on HTTP. It calls the choreographer's shared moderation handler (the org doc's single engine rule) authenticated as a dedicated moderation service account — the endpoint accepts any valid token on this homeserver. Category names are normalized onto the orchestrator's flag vocabulary so both moderation paths speak one language.

**Disposition is self-redaction: the redaction is sent as the offending sender.** The module send path enforces normal room power levels, and no service user is a member of every room — but a sender may always redact their own message, so self-redaction works in every room, DMs included. The moderation reason rides on the redaction event, prefixed so clients and audits can tell moderation redactions from ordinary ones. (This corrects the org doc's assumption that the module send path is privileged; it is not.)

Tier 2 skips rooms carrying an activity-plan state event: the conversation orchestrator already moderates activity sessions, and a second check would double-redact and double-spend. Every failure in the check-and-redact path is logged and fails open, mirroring the choreo handler's own contract.

## Deliberately out of scope here

Teacher/course-admin notification of flagged messages, per-room opt-out, and age-conditional strictness are tracked product gaps in the org doc, not behaviors of this module.
