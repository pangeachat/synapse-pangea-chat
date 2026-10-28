---
applyTo: "synapse_pangea_chat/moderation/**,synapse_pangea_chat/safety_incidents/**,tests/*moderation*,tests/*safety*"
description: "What the homeserver's two moderation tiers refuse, remove, preserve and record, what a refused learner is told, what reaches a course's Safety page, and the limits the owner has accepted."
---

# Server-Side Chat Moderation

## Purpose

Tier 1 refuses a send carrying a phone number or an unambiguous slur before anyone sees it; Tier 2 asks the choreographer's shared moderation handler about each message that appears and removes the harmful ones. The org [trust-and-safety doc](../../../.github/.github/instructions/trust-and-safety.instructions.md) owns why moderation lives at the homeserver and the encryption limit; this doc owns the decisions. Rollout: [2-step-choreographer#1746](https://github.com/pangeachat/2-step-choreographer/issues/1746).

## The experience

1. A message with a valid phone number or a word from the wordlist's universal core does not send. The learner sees one plain sentence: not sent, by an automatic check, which kind of rule (contact details, or a disallowed word), and that their teacher can help; it never names the word or quotes the message, and clients may translate it.
2. Any other message appears at once; Tier 2 judges it within seconds and removes a harmful one with a visible reason. Mild classroom swearing stays up.
3. A self-harm disclosure is never removed, by that verdict or a later one.
4. A harmful edit is removed as the edit; readers see the earlier revision.
5. The bot is never moderated; activity rooms are.
6. Every refusal, removal, kept flag, preserved disclosure and in-app report reaches the Safety page of the courses the learner was in, with full text and room name; one course's admin never learns the others.
7. In an encrypted room nothing is moderated, and the operator can see the share.

## Decisions

Decision; reason; who re-decides.

### Tier 1

- A wrong refusal is worse than a miss, since Tier 2 and reports back it up; every rule leans conservative. Will.
- Both tiers ship dark; a tier refuses to start half-configured or on a misspelled key rather than silently run with less. Engineer.
- Tier 1 is model-free, takes no language, and any failure inside it lets the message through; a guess or a partial failure could only refuse wrongly. Engineer.
- Street addresses are not refused: no pattern tells "I live at 42 Maple Street" from a landmark lesson; no tier detects address sharing today. Will.
- A term is refused pre-send only on positive, per-term evidence, never on a missing collision. No native speaker has reviewed the wordlist; non-English terms stand on a two-of-three model vote a native review supersedes. Will.
- Any recorded benign reading with its own etymology demotes a term; a work title, or a name that is simply the vulgar phrase, is not one (2026-09-21). Will.
- A leet spelling inherits the readings of the word it spells; identifiers like P1CA are ordinary chat. QA.
- A spaced-out spelling is refused only with a digit in it, across whitespace alone; "C U N T" is a spelling lesson. QA.
- Tier 1 never matches inside a word, so Chinese, Japanese and spaced-out Hangul, kana or abugida text are Tier 2's. QA.
- A benign sentence the matcher fails is a bug, never a retired control. QA.
- The refusal names the rule, never the match, or the wordlist becomes enumerable. Engineer.

### Tier 2

- A removal is sent as the offending sender, so it works in every room. Engineer.
- A flag is not a severity: thresholds are per category (child-safety at any confidence, plain harassment loosest, no readable score removes), and the operator tunes them from real traffic. Will.
- Self-harm is preserved durably, and an unreadable disposition is never a removal; a deleted disclosure cannot be brought back. Will.
- The wordlist matcher runs beside the verdict and only counts; it is noisy in languages nobody here reads. Will.
- Everything else fails open. Engineer.
- The window between a message appearing and its removal is inherent; closing it means blocking in the send path. Will.
- Sharding waits for steady queue-full drops, queue depth past a quarter, fifty screened a second, or a flag rate above a tenth. Engineer.

### The Safety page's record

- Sender, text and courses are captured when the incident happens, courses by stream position, never recomputed; moderation never waits on the record. Engineer.
- Retries are in memory; a counted lost row reopens that. Accepted limits, with what reopens each: a course plan pruned by retention (redacting course plans); a late retry relabelling failed as unknown (the page acting on that difference); a forked room graph (federation); multiple event persisters. Will.
- No Matrix ID and no message text ever reaches a log; a handler attached after moderation starts is not scrubbed. Engineer.

## Open questions

- Native review of the wordlist.
- An incident with no resolvable course reaches no Safety page.
- The client's PRIVACY.md claims E2EE by default for private chats; not true of this build.
- Notification, retention, per-room opt-out and age-conditional strictness are the org doc's.
