---
applyTo: "synapse_pangea_chat/email_policy/**,synapse_pangea_chat/register_email/**"
description: "What Pangea accepts as an email address at signup, where the check is enforced so that every path is covered, and how often the same address may be mailed."
---

# Email Address Policy

Pangea mails a verification link before account creation begins, so the address is accepted on trust at the moment we send. An address that does not exist costs a hard bounce on the SES transactional stream that every other Pangea email shares. This doc decides what we accept and how often we will mail the same person. The request and response contract of the endpoint itself is [register-email.instructions.md](register-email.instructions.md).

## What we accept

An address must have a local part, a domain containing at least one dot, and a final label of two or more letters, within the 254-character limit that mail servers enforce. `a@b` is rejected. Plus-addressing, subdomains, and unusual-but-valid local parts are all accepted.

The rule is deliberately permissive beyond that. It exists to catch obvious junk before it costs a bounce, not to be a complete implementation of the email address standard — rejecting a real learner's unusual address is a worse outcome than letting one junk address through.

**There is no disposable-domain deny-list, by decision.** QA validates the signup flow using temporary email services, and a deny-list would block that testing. Disposable providers also generally accept and deliver mail, so they are not a meaningful source of the hard bounces this policy exists to reduce. If a deny-list is ever added, a way for QA to keep testing has to be part of its design.

**There is no MX or DNS lookup.** It would put a network call with its own failure modes directly in the signup path, where a resolver hiccup becomes a learner who cannot create an account. Revisit if the bounce rate stays high once this is live.

## Where it is enforced

The homeserver is the authority. The client applies the same rule so the learner gets immediate feedback in the form, but the client's check is never the gate — Synapse's built-in registration endpoint is reachable independently of the app, and it is served by a different process than Pangea's own endpoint.

Because of that, the check is registered as a homeserver-wide `is_3pid_allowed` callback rather than written into the endpoint. Synapse consults that callback on every path that binds an email address — Pangea's endpoint, its own registration endpoint, and adding an email to an existing account — so registering once covers all of them. Pangea's endpoint also runs the same check directly, just before Synapse's, so that a malformed address returns an error saying it is malformed instead of the shared callback's generic "domain is not authorized".

Password reset is not covered and does not need to be: it only mails an address already stored against an account.

`email_policy_enabled` turns the whole check off for incident response. On by default.

## How often we will mail

Verification sends are limited per address and per client IP through Synapse's own `rc_3pid_validation` limits, so a resend loop cannot mail one address repeatedly. Using Synapse's limiter rather than a Pangea one is deliberate — it is tunable per environment and already governs the built-in endpoints.

A repeat request carrying the same send-attempt number does not send again; Synapse treats it as the same request. The client depends on this, and advances the attempt number only after a send has actually succeeded.

In the app, the resend control is disabled while a request is in flight and for 30 seconds after, showing the time remaining. See the client's [signup-and-login.instructions.md](../../../client/.github/instructions/signup-and-login.instructions.md).

The endpoint additionally keeps a short per-IP burst limit as a cheap first check, because the route is unauthenticated and rejecting a flood early avoids doing any work for it.

## Key Files

- Address rule: [is_valid_email_address.py](../../synapse_pangea_chat/email_policy/is_valid_email_address.py)
- Callback registration: [email_policy.py](../../synapse_pangea_chat/email_policy/email_policy.py)
- Tests: [test_email_policy_unit.py](../../tests/test_email_policy_unit.py), [test_register_email_e2e.py](../../tests/test_register_email_e2e.py)
