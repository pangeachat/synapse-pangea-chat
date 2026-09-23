---
applyTo: "synapse_pangea_chat/direct_push/**,synapse_pangea_chat/nudge_delivery/**,synapse_pangea_chat/config.py,synapse_pangea_chat/__init__.py,tests/test_direct_push*.py,tests/test_nudge_delivery*.py"
description: "Nudge delivery — the Synapse module's contracts for carrying a bot nudge to one channel by availability (push, else email), the learner's communication-preferences store and the logged-out unsubscribe surface, the first-party click record, and the push-rule suppression that keeps Synapse's own mailer off bot notices."
---

# Nudge Delivery — Synapse Module

The delivery half of the org design: [user-communication-controls](../../../.github/.github/instructions/user-communication-controls.instructions.md) says everything learner-facing travels the Synapse path with refusals in first-party account data, and [engagement-decisions](../../../.github/.github/instructions/engagement-decisions.instructions.md) fixes the channel order — in-app when present, push when a working device exists, email otherwise. This module implements both for the bot's nudges. Category and variant names are the org catalog's; nothing here defines one.

For Synapse Admin API, Module API, and Matrix spec documentation links, see [synapse-docs.instructions.md](../../../.github/.github/instructions/synapse-docs.instructions.md).

## Deliver a nudge

`POST /_synapse/client/pangea/v1/prepare_nudge` before the notice, then `POST /_synapse/client/pangea/v1/deliver_nudge` after it — both server admin only, rate-limited per caller with the direct-push limits.

The bot calls it after recording the nudge as a `p.room.notice` in the person's bot DM. The request names the person, the catalog **category** (must be one the bot delivers: a nudge category or `trial_marketing`), the **variant**, the L1 **body**, the notice's event and room ids, the `pangea.*` metadata the client routes on, and the activity and session ids for the email deep link. Optional: a title, an email subject, a call-to-action label.

Exactly one channel carries the nudge, decided in this order, and the response names which:

1. **`refused`** — the person's preferences refuse the category (or the global off covers it). Nothing is sent and no push rule is touched.
2. **`in_app`** — the person is online and currently active (Synapse presence; both flags, since `currently_active` can outlive an offline transition). The notice already in their DM is the delivery.
3. **`push`** — at least one enabled HTTP pusher accepted the push: Sygnal returned success **and** did not list the device's pushkey as rejected. A rejected pushkey (an expired or unregistered device token) is a failed push, so the person falls through to email rather than being counted as reached. Email pushers are never counted: they cannot be posted to Sygnal.
4. **`email`** — no working push device, and `nudge_email_enabled` is on, and the person has a verified email address.
5. **`none`** — with a reason code: `email_disabled`, `no_email_address`, `no_public_baseurl`, `no_token_secret`, `send_failed`, prefixed `push_failed_then_` when a push device existed but every push failed.

The response also carries the push transport summary (same shape as `send_push`) and the email outcome, so the bot can log the channel per nudge. Presence being disabled, or a presence read failing, counts as "not in the app" — a nudge the person is due must not be lost to a presence outage, and the cost of being wrong is one push to someone who is online.

## The refusal store

Refusal state is one global account-data event per user, `pangea.communication_preferences`: the refused categories, an `all_off` flag, when it changed, and which surface changed it (`unsubscribe_link` or `app`). It is the store the in-app preference screen reads and writes and the store this module reads before every send, so the two surfaces cannot disagree. Rules the store enforces:

- `credential` can never be refused.
- The global off covers every nudge and marketing category and no event-triggered one: a person who stops nudges still hears when a human writes to them.
- A malformed event reads as "nothing refused" — silencing someone who never asked is the worse failure, and one unwanted nudge is refusable again.
- The unsubscribe surface only ever **adds** refusals; turning a category back on is the signed-in screen's job.

## The unsubscribe surface

`GET` and `POST /_synapse/client/pangea/v1/unsubscribe?t=<token>` — unauthenticated, rate-limited per client address.

Every nudge email carries a link here in its footer and in the `List-Unsubscribe` / `List-Unsubscribe-Post: List-Unsubscribe=One-Click` headers. **GET only shows a confirmation page**; **POST performs the refusal** — a mail scanner that prefetches the link must not unsubscribe anyone (RFC 8058, and the org rule that no emailed link acts on GET). The page offers the category refusal and the global off; the one-click POST from a mail client refuses the category. A bad or expired token gets a 400 page pointing at the in-app screen. The read-merge-write of the store is serialized per person, so two unsubscribes racing each other (a category refusal and the global off) both survive; the module runs on the main process, which is what makes a process-local lock sufficient.

## The click record

`GET /_synapse/client/pangea/v1/n?t=<token>` — unauthenticated, rate-limited per client address.

The email's call-to-action goes through this redirect. It writes the same first-party `p.room.notice.opened` event into the DM that the client writes on a push tap — sender is the person, content names the notice and the variant — so an email click feeds cooldown and backoff exactly like a tap, then redirects to the app: the shareable activity link (`/:activityId`, with `?roomid=` when a session exists) or the World map. The record never blocks the redirect: an expired link or a failed write logs a warning and the person still lands in the app.

## Signed links

Both links are HMAC-signed tokens naming the person, the action, and an expiry (`nudge_token_ttl_days`, default 90 — past CAN-SPAM's 30 and CASL's 60). The key is `nudge_token_secret`, falling back to the homeserver's macaroon secret so no new secret is required to turn email on. Payloads carry ids only, never addresses or bodies. A click token cannot be used to unsubscribe and vice versa.

## Keeping Synapse's own pipeline off bot notices

A `p.room.notice` in a DM would otherwise flow through Synapse's rule-driven notification pipeline — the email pusher mailing it as a missed message with no unsubscribe, an HTTP pusher pushing it a second time. The module installs a per-user override push rule (`p.rule.bot_notice`: `dont_notify` for that event type), idempotently, the same shape as the analytics-invite suppression. Synapse evaluates push actions when an event is persisted, so the rule must exist **before** the notice does: the bot calls `POST /_synapse/client/pangea/v1/prepare_nudge` (server admin only, same rate limit) for a person before recording their first notice, once per person per bot process, and `deliver_nudge` installs the rule again as a backstop. `nudge_suppress_notice_push_rules` turns the installation off; the rules already installed stay.

## Direct push (`send_push`)

`POST /_synapse/client/pangea/v1/send_push` — server admin only — is the lower-level transport `deliver_nudge` uses for its push leg and remains available on its own. It is roomless by design: it forges no Matrix event, creates no timeline entry, unread state, receipt, or push action, and delivers through its own path to Sygnal rather than Synapse's event-driven pipeline. Its response reports the transport attempts for the pushers it posted to. Callers who want channel selection, refusal enforcement, the email leg, or the click record use `deliver_nudge`; `send_push` alone answers "push this payload to this user's devices".

## Configuration

Turning `nudge_email_enabled` on without `nudge_email_postal_address` is a configuration error: the address is a content requirement on marketing-classified mail, so the module refuses to start email delivery without one rather than send a footer that lacks it.

`nudge_email_enabled` (default off — enabling it is a rollout decision recorded in a deploy-note), `nudge_suppress_notice_push_rules` (default on), `nudge_token_secret`, `nudge_token_ttl_days`, `nudge_email_postal_address` (shown in the footer; a CAN-SPAM content requirement for marketing-classified categories), and the public-link rate limits `nudge_public_requests_per_burst` / `nudge_public_burst_duration_seconds`. The email leg also needs the homeserver's `public_baseurl` and working `email` (SMTP) config, both of which the deployments already have. Templates ship inside the package and are read through the module API's template loader.

## Key Files

- [`nudge_delivery/deliver.py`](../../synapse_pangea_chat/nudge_delivery/deliver.py) — channel selection and the email leg
- [`nudge_delivery/categories.py`](../../synapse_pangea_chat/nudge_delivery/categories.py) — catalog categories and the preferences event
- [`nudge_delivery/unsubscribe.py`](../../synapse_pangea_chat/nudge_delivery/unsubscribe.py), [`click.py`](../../synapse_pangea_chat/nudge_delivery/click.py), [`tokens.py`](../../synapse_pangea_chat/nudge_delivery/tokens.py), [`push_rule.py`](../../synapse_pangea_chat/nudge_delivery/push_rule.py)
- [`direct_push/direct_push.py`](../../synapse_pangea_chat/direct_push/direct_push.py) — the push transport
- [`test_nudge_delivery_unit.py`](../../tests/test_nudge_delivery_unit.py), [`test_nudge_delivery_e2e.py`](../../tests/test_nudge_delivery_e2e.py), [`test_direct_push_e2e.py`](../../tests/test_direct_push_e2e.py)
