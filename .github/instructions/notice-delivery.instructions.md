---
applyTo: "synapse_pangea_chat/direct_push/**,synapse_pangea_chat/notice_delivery/**,synapse_pangea_chat/config.py,synapse_pangea_chat/__init__.py,tests/test_direct_push*.py,tests/test_notice_delivery*.py"
description: "Notice delivery — the Synapse module's channel selection, communication preferences, logged-out unsubscribe surface, first-party click record, and suppression of duplicate delivery for bot notices."
---

# Notice Delivery — Synapse Module

This module provides shared notice delivery for automated Synapse flows and operators. The [user-communication-controls](../../../.github/.github/instructions/user-communication-controls.instructions.md) catalog owns message categories, variants, refusability, and allowed delivery methods. For `use-available`, delivery selects in-app when the person is active, push when a working device accepts it, then email. Refusals use the same first-party account-data store regardless of caller or channel. Nothing here defines a separate catalog.

For Synapse Admin API, Module API, and Matrix spec documentation links, see [synapse-docs.instructions.md](../../../.github/.github/instructions/synapse-docs.instructions.md).

## Deliver a notice

`POST /_synapse/client/pangea/v1/prepare_notice` before the notice, then `POST /_synapse/client/pangea/v1/deliver_notice` after it — both server admin only. Automated flows and operators use the same delivery service. Each endpoint has an independent configurable per-caller limit, defaulting to 600 requests per minute with a burst of 100; neither spends the direct-push allowance.

The caller records a `p.room.notice` in the person's bot DM first. The request names the person, catalog category and variant, notice event and room, and activity and session ids for the email target. Availability-based sends require push title/body and email subject/HTML/plain text; forced sends require their selected channel's content and may include both. Missing content is rejected before delivery. Category restrictions and refusals apply to forced sends too. The in-app-only `allow_notifications` variant cannot be pushed or emailed. Credential mail and consuming course-claim links retain their existing dedicated paths.

Caller-rendered email carries `{{cta_url}}` and `{{unsubscribe_url}}` in both HTML and plain text. Synapse supplies those signed links, the sender and unsubscribe headers; caller content is never evaluated as a server-side template. Email uses the brand template with a caller-supplied receiving reason. The existing flat body/subject format remains a compatibility path for deployed bot callers while they migrate to structured content.

The request names where its links land. `destination` and the optional `secondary_destination` are objects with a `kind`: `app` (the World map), `activity` (requires `activity_id`, with `session_room_id` when a session exists), `course` (requires `course_room_id`), `subscription` (the subscription settings page), or `external` (requires an `https` `url` whose host is in `notice_external_link_hosts`, so our mail can never be an open redirect). A kind carrying an id it does not take, a missing required id, or an unlisted external host is refused with the reason. A request with top-level `activity_id` and `session_room_id` and no `destination` object means an activity destination, or the app home when neither is present, exactly as before. A `secondary_destination` requires `{{cta2_url}}` in both the HTML and the plain text, and the slot is refused without one, so a second link can be neither silently dropped nor left unrendered ([notices.instructions.md](../../../engagement/.github/instructions/notices.instructions.md), "Notice destinations"). The flat compatibility body takes a `destination` but no second link, because the module's own template has one call to action.


Structured delivery includes the decision's run, runner, funnel, state and copy key. Synapse records delivery in the existing [Notification_Log](../../../cms/.github/instructions/notification-log.instructions.md), returning its record id; callers do not write a second row. For a request without `notification_log_id` it reserves the run/person before sending; for a caller-owned request it claims the row in its own transport-claim table before sending. Either way a retry cannot send that decision again, including when delivery or its final write has an uncertain outcome, and an unresolved reservation or claim requires reconciliation, not automatic resending. The decision runner still records skips that never reach delivery and later engagement outcomes. Legacy callers without decision context retain caller-owned logging until migrated. A request that carries `notification_log_id` names a row the caller already wrote: the caller owns the record, Synapse reserves and finishes nothing, answers with `log_status` `caller`, and the caller writes the receipt onto its own row. The log context is then optional. This is how the engagement runner sends. The reservation path stays for requests without the id: the contract changes by addition, and a request without the id is never refused or sent unguarded. On the caller-owned path every 4xx happens before any send and is a definite no-send; only a 5xx or a timeout is uncertain. Skipping the record never skips idempotency: a caller-owned request is claimed in the module's own transport-claim table before any transport, keyed by the row. A repeat of the same request after a definite result answers with that result and sends nothing; a repeat while the result is uncertain is refused until reconciled; a different payload under the same row is a conflict. The phase and the notice event are persisted before transport, so an interrupted delivery leaves evidence. A caller-owned request may omit the decision context, except when it carries eligibility conditions, which read the decision's time and funnel.

For `use-available`, exactly one channel carries the notice, decided in this order, and the response names which:

1. **`refused`** — the person's preferences refuse the category (or the global off covers it). Nothing is sent and no push rule is touched.
2. **`in_app`** — the person is online and currently active (Synapse presence; both flags, since `currently_active` can outlive an offline transition). The notice already in their DM is the delivery.
3. **`push`** — at least one enabled HTTP pusher accepted the push: Sygnal returned success **and** did not list the device's pushkey as rejected. A rejected pushkey (an expired or unregistered device token) is a failed push, so the person falls through to email rather than being counted as reached. Email pushers are never counted: they cannot be posted to Sygnal.
4. **`email`** — no working push device, and `notice_email_enabled` is on, and the person has a verified email address.
5. **`none`** — with a reason code: `email_disabled`, `no_email_address`, `no_public_baseurl`, `no_token_secret`, `send_failed`, prefixed `push_failed_then_` when a push device existed but every push failed.

The response also carries the push transport summary (same shape as `send_push`) and the email outcome. Forced push never falls through to email; forced email does not attempt push. Email-only categories use email regardless of presence. Presence being disabled, or a presence read failing, counts as "not in the app" — a notice the person is due must not be lost to a presence outage, and the cost of being wrong is one push to someone who is online.

The former `deliver_nudge` and `prepare_nudge` URLs remain compatibility aliases of the same resources. Configuration accepts the former `nudge_*` keys as aliases for `notice_*`; conflicting values are rejected. Existing signed email links and catalog category identifiers remain valid.

## Email appearance

Notice emails use the standard [Pangea Brand template](../../../admin/email-marketing/templates/base.html), including its logo, header, gold accents, NSF badge, and company footer. The message body and call to action occupy its content area; the footer shows a simple “Unsubscribe” link, which retains its category-specific destination.

Course/activity images use email-accessible HTTPS URLs within the brand template, with alternative text and a useful plaintext version.

## Scheduled delivery

An optional `scheduled_at` defers the entire notice, including its in-app event, push, and email. Scheduled requests provide notice content instead of an existing event ID. Existing immediate requests remain supported.

Schedules survive restarts and are deduplicated by run/person, or by the caller's row when the request carries `notification_log_id`. At send time, recheck permissions, membership, preferences, and availability, then use the existing delivery service and Notification_Log. Uncertain delivery outcomes require reconciliation rather than automatic resending.

Scheduled notices may include optional eligibility conditions, checked against current server data immediately before delivery. Conditions can suppress delivery when the recipient has returned since the decision, another qualifying contact violates the caller’s minimum spacing, the target activity has been started or completed, or the target session is full or inaccessible.

Failed or unavailable checks send nothing and record a reason in Notification_Log. Requests without these conditions retain existing behavior.

Server admins may cancel queued notices atomically. Cancellation succeeds only before the worker claims the notice, is idempotent, and prevents any notice event, push, or email. Claimed or uncertain deliveries cannot be reported as cancelled. Cancelled decisions remain deduplicated.

## The refusal store

Refusal state is one global account-data event per user, `pangea.communication_preferences`: the refused categories, an `all_off` flag, when it changed, and which surface changed it (`unsubscribe_link` or `app`). It is the store the in-app preference screen reads and writes and the store this module reads before every send, so the two surfaces cannot disagree. Rules the store enforces:

- `credential` can never be refused.
- The global off covers every nudge and marketing category and no event-triggered one: a person who stops nudges still hears when a human writes to them.
- A malformed event reads as "nothing refused" — silencing someone who never asked is the worse failure, and one unwanted nudge is refusable again.
- The unsubscribe surface only ever **adds** refusals; turning a category back on is the signed-in screen's job.
- A `missed_message` refusal also removes every `m.email` pusher the person has. Synapse's own missed-message mail is sent by those pushers, not by this module, so removing them is what makes the refusal take effect; the client's sign-in sync keeps them matching the store after that.

## The unsubscribe surface

`GET` and `POST /_synapse/client/pangea/v1/unsubscribe?t=<token>` — unauthenticated, rate-limited per client address.

Every refusable notice email carries a link here in its footer and in the `List-Unsubscribe` / `List-Unsubscribe-Post: List-Unsubscribe=One-Click` headers. **GET only shows a confirmation page**; **POST performs the refusal** — a mail scanner that prefetches the link must not unsubscribe anyone (RFC 8058, and the org rule that no emailed link acts on GET). The page offers the category refusal and the global off; the one-click POST from a mail client refuses the category. A bad or expired token gets a 400 page pointing at the in-app screen. Both unsubscribe pages write the store through one path whose read-merge-write is serialized per person, so two unsubscribes racing each other (a category refusal and the global off, or either page against the other) both survive; the module runs on the main process, which is what makes a process-local lock sufficient.

The confirmation, success, and expired-link pages share a branded, responsive layout. The confirmation shows Material-style green switches for each reminder and offer category, plus a master switch and one Save preferences action. It reads current preferences and can add several refusals in one submission; already-refused categories stay off and cannot be re-enabled from a logged-out link. The missed-message switch is outside the master switch, since the global off does not cover it; switching it off refuses `missed_message`. Course-invitation controls remain unavailable until that send path honors this store; account emails are always on. The page shares the brand email footer, including NSF proof, company address, copyright, privacy and terms links. Do not promise in-app re-enabling until [client#9234](https://github.com/pangeachat/client/issues/9234) ships.

### The missed-message link

Synapse links every missed-message email to its own unsubscribe page, `/_synapse/client/unsubscribe`, in the email body and in the `List-Unsubscribe` header, signed with Synapse's own key. That page removes the email pusher as soon as the link is opened, so a mail scanner that opens links on delivery unsubscribes the person. It also records no refusal, so the client's sign-in sync would turn the email back on. With `notice_missed_message_unsubscribe_enabled` on, the module serves that path in Synapse's place, because a module's pages take precedence over Synapse's own at the same path. Opening the link shows the confirmation page above. The page's button, or a mail client's one-click unsubscribe, records the `missed_message` refusal, which removes the email pushers. A link whose signature doesn't match gets the 400 page.

## The click record

`GET /_synapse/client/pangea/v1/n?t=<token>` — unauthenticated, rate-limited per client address.

Every call to action goes through this redirect. It writes the same first-party `p.room.notice.opened` event into the DM that the client writes on a push tap — sender is the person, content names the notice and the variant, plus which link was opened (`cta` or `cta2`) and its `destination_kind` — so an email click feeds cooldown and backoff exactly like a tap, then redirects to the destination the link was signed for, in the client's workspace URL grammar ([routing.instructions.md](../../../client/.github/instructions/routing.instructions.md)): `app` is the World map root; `activity` is the shareable activity link (`/:activityId`, with `?roomid=` when a session exists); `course` is the root with `?c=` naming the course and `left=course` opening its card; `subscription` is the root with `?right=settingspage:subscription`; `external` is the url itself, checked against `notice_external_link_hosts` again at click time, so a host removed from the list retires the links already sent and the person lands on the World map instead, with a warning logged. The record never blocks the redirect: an expired link or a failed write logs a warning and the person still lands in the app.


## Signed links

Every link is its own HMAC-signed token naming the person, the action, and an expiry (`notice_token_ttl_days`, default 90 — past CAN-SPAM's 30 and CASL's 60). The key is `notice_token_secret`, falling back to the homeserver's macaroon secret so no new secret is required to turn email on. A click token also names its destination and which link it is (`cta` or `cta2`), so the destination cannot be edited into the URL and a click on the second link is recorded as such; links issued before destinations existed carry only the activity ids and resolve as they always did. Payloads carry ids only, never addresses or bodies. A click token cannot be used to unsubscribe and vice versa.


## Keeping Synapse's own pipeline off bot notices

A `p.room.notice` in a DM would otherwise flow through Synapse's rule-driven notification pipeline — the email pusher mailing it as a missed message with no unsubscribe, an HTTP pusher pushing it a second time. The module installs a per-user override push rule (`p.rule.bot_notice`: `dont_notify` for that event type), idempotently, the same shape as the analytics-invite suppression. Synapse evaluates push actions when an event is persisted, so the rule must exist **before** the notice does: the bot calls `POST /_synapse/client/pangea/v1/prepare_notice` (server admin only, same rate limit) for a person before recording their first notice, once per person per bot process, and `deliver_notice` installs the rule again as a backstop. `notice_suppress_notice_push_rules` turns the installation off; the rules already installed stay.

## Direct push (`send_push`)

`POST /_synapse/client/pangea/v1/send_push` — server admin only — is the lower-level transport `deliver_notice` uses for its push leg and remains available on its own. It is roomless by design: it forges no Matrix event, creates no timeline entry, unread state, receipt, or push action, and delivers through its own path to Sygnal rather than Synapse's event-driven pipeline. Its response reports the transport attempts for the pushers it posted to. Callers who want channel selection, refusal enforcement, the email leg, or the click record use `deliver_notice`; `send_push` alone answers "push this payload to this user's devices".

## Configuration

Turning `notice_email_enabled` on without `notice_email_postal_address` is a configuration error: the address is a content requirement on marketing-classified mail, so the module refuses to start email delivery without one rather than send a footer that lacks it.

`notice_email_enabled` (default off — enabling it is a rollout decision recorded in a deploy-note), `notice_missed_message_unsubscribe_enabled` (default off — serving Synapse's unsubscribe path in its place is a rollout decision recorded in a deploy-note), `notice_suppress_notice_push_rules` (default on), `notice_token_secret`, `notice_token_ttl_days`, `notice_email_postal_address` (shown in the footer; a CAN-SPAM content requirement for marketing-classified categories), `notice_external_link_hosts` (the hosts an `external` destination may link to; default the booking page `calendar.app.google` and the teacher dashboards `admin.pangea.chat` and `admin.staging.pangea.chat`), and the public-link rate limits `notice_public_requests_per_burst` / `notice_public_burst_duration_seconds`. The email leg also needs the homeserver's `public_baseurl` and working `email` (SMTP) config, both of which the deployments already have. Templates ship inside the package and are read through the module API's template loader.

## Key Files

- [`notice_delivery/deliver.py`](../../synapse_pangea_chat/notice_delivery/deliver.py) — channel selection and the email leg
- [`notice_delivery/request.py`](../../synapse_pangea_chat/notice_delivery/request.py) — request validation, including `Destination`; [`common.py`](../../synapse_pangea_chat/notice_delivery/common.py) resolves a destination to its URL

- [`notice_delivery/categories.py`](../../synapse_pangea_chat/notice_delivery/categories.py) — catalog categories and the preferences event
- [`notice_delivery/unsubscribe.py`](../../synapse_pangea_chat/notice_delivery/unsubscribe.py), [`missed_message_unsubscribe.py`](../../synapse_pangea_chat/notice_delivery/missed_message_unsubscribe.py), [`refusal_store.py`](../../synapse_pangea_chat/notice_delivery/refusal_store.py), [`click.py`](../../synapse_pangea_chat/notice_delivery/click.py), [`tokens.py`](../../synapse_pangea_chat/notice_delivery/tokens.py), [`push_rule.py`](../../synapse_pangea_chat/notice_delivery/push_rule.py)
- [`direct_push/direct_push.py`](../../synapse_pangea_chat/direct_push/direct_push.py) — the push transport
- [`test_notice_delivery_unit.py`](../../tests/test_notice_delivery_unit.py), [`test_notice_destinations.py`](../../tests/test_notice_destinations.py), [`test_notice_delivery_e2e.py`](../../tests/test_notice_delivery_e2e.py), [`test_direct_push_e2e.py`](../../tests/test_direct_push_e2e.py)

