---
applyTo: "synapse_pangea_chat/moderation/**,synapse_pangea_chat/safety_incidents/**,tests/*moderation*,tests/*safety*"
description: "What the homeserver blocks before send, what it removes after, why self-harm disclosures stay up, and what the Safety page's incident record holds."
---

# Server-Side Chat Moderation

The homeserver checks every unencrypted message twice: Tier 1 refuses unmistakable content before send, and Tier 2 asks the choreographer's moderation service afterwards and removes what it flags. Everything caught, and every learner report, reaches the learner's course admins on the Safety page. The why is the org [trust-and-safety doc](../../../.github/.github/instructions/trust-and-safety.instructions.md)'s.

## The experience, step by step

1. A phone number or a blocking-wordlist term refuses a message; the learner is told the rule and that a machine decided, never the word.
2. Otherwise it appears, and seconds later Tier 2 removes it if flagged. A flagged edit loses only the edit.
3. A message reading most strongly as self-harm stays up, pinned as urgent on the Safety page.
4. A course admin sees every incident and report in their course, with full text.

## Decisions

Re-decider in brackets.

### Both tiers

- **Ships dark; refuses to start misconfigured**: a silently dark tier looks like a clean room. [engineer]
- **Bots and listed senders are exempt**, by exact pattern, never regular expression: an exemption is a security boundary. [engineer]
- **Moderation reads everything a reader sees**: missing displayed text is a bypass, extra text costs one false positive. [engineer]
- **A failure lets the message through, counted**, so a bug never blocks all sends. [engineer]
- **Logs never carry a Matrix ID or message text**: logs are not access-controlled. Synapse's own lines are beyond reach. [engineer]
- **Encrypted rooms are not moderated**; their share is counted, expected zero since the app never enables encryption. [unassigned]

### Tier 1: blocks before send

- **A wrong block is worse than a miss**: it silences an innocent learner, and Tier 2 still catches the miss. [unassigned]
- **Phone numbers and the wordlist only.** No tier detects shared street addresses: a pattern cannot tell sharing from discussing. [unassigned]
- **A term blocks only on recorded evidence** of no ordinary reading in any supported language. Non-English terms rest on a three-model review; no native speaker has reviewed any. [product owner]
- **A work title is not an ordinary reading**: a book named with the vulgar word keeps the word in Tier 1. [product owner]
- **No language guessing, no matching inside words**, so Chinese and Japanese get no Tier 1, and a spelled-out word blocks only with a digit. [engineer]
- **The refusal names the rule, never the match**, so it cannot probe the wordlist. [unassigned]

### Tier 2: removes after send

- **The visible window is accepted** and measured: an AI call in the send path is what the split avoids. [unassigned]
- **Confidence is weighed per category**: classroom swearing stays, targeted abuse goes, sexual content involving minors goes on any flag, a missing score removes. [unassigned]
- **Self-harm is preserved, not redacted, when it is the strongest flag, and the decision is durable.** A learner disclosing self-harm is asking for help, and the message is the only record of it. Abuse that also reads as self-harm, when another flag scores higher, is decided like any other flag; a tie, an unreadable self-harm score, or no score for the other flags keeps it. No setting controls it. [unassigned; [#310](https://github.com/pangeachat/synapse-pangea-chat/issues/310)]
- **The learner's own account sends the removal**: no service account is in every room. [engineer]
- **Activity rooms are moderated**: the orchestrator no longer moderates them. [unassigned]
- **The local wordlist runs beside Tier 2 and only counts** until measured agreement justifies more. [unassigned]
- **A batched check only screens**; removal needs a single-message check, so a batch cannot remove the wrong message. [engineer]
- **One server runs Tier 2**, about threefold over the 1,000-student target; splitting waits for triggers recorded in code. [engineer]

### The Safety page's record

- **Captured at the incident, never recomputed**: sender, full text, room name, the learner's courses then. [unassigned]
- **Moderation never waits on the record**: enforcement first, the row after; repeat verdicts merge and never erase. [unassigned]
- **An admin sees only their course's incidents**, never a learner's other courses; every refusal looks the same. [unassigned]
- **Accepted limits**: a row lost to a restart mid-retry or long outage; a course misattributed in rare room-graph cases. Revisit if lost rows (`pangea_safety_incident_lost_total`) are ever non-zero; other reopening conditions sit beside the code. [owner]
- **Out of scope**: notifications, retention, opt-out and age-based strictness are the org doc's.

## Open questions

- The org doc says both tiers are on everywhere with only the bot exempt; this module ships dark with configurable exemptions. Which is true per deployment?
