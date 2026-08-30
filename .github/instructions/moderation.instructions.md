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
- **Profanity** — the `better-profanity` wordlist. Also English-centric.

The English bias is accepted: Tier 1 exists chiefly to stop minors sharing contact details and to catch drive-by profanity instantly; the multilingual backstop is Tier 2. A failure inside Tier 1 allows the message (fail open, logged) — a moderation bug must never block all sends.

## Tier 2 — LLM moderation (redacts after)

Fires after an event persists, from a background task so event persistence never waits on HTTP. It calls the choreographer's shared moderation handler (the org doc's single engine rule) authenticated as a dedicated moderation service account — the endpoint accepts any valid token on this homeserver. Category names are normalized onto the orchestrator's flag vocabulary so both moderation paths speak one language.

**Disposition is self-redaction: the redaction is sent as the offending sender.** The module send path enforces normal room power levels, and no service user is a member of every room — but a sender may always redact their own message, so self-redaction works in every room, DMs included. The moderation reason rides on the redaction event, prefixed so clients and audits can tell moderation redactions from ordinary ones. (This corrects the org doc's assumption that the module send path is privileged; it is not.)

Tier 2 skips rooms carrying an activity-plan state event: the conversation orchestrator already moderates activity sessions, and a second check would double-redact and double-spend. Every failure in the check-and-redact path is logged and fails open, mirroring the choreo handler's own contract.

## Deliberately out of scope here

Teacher/course-admin notification of flagged messages, per-room opt-out, and age-conditional strictness are tracked product gaps in the org doc, not behaviors of this module.
