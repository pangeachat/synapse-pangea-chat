# Notice delivery API

Design: [notice delivery](../.github/instructions/notice-delivery.instructions.md). All calls require a server-admin Matrix bearer token. Internal module callers use `DeliverNotice.deliver` with the same payload and validation.

For immediate delivery (see [Scheduled notices](#scheduled-notices) for deferred creation):

1. Call `POST /_synapse/client/pangea/v1/prepare_notice` with `{"user_id":"@learner:example.org"}` before recording a notice, to suppress Synapse's duplicate push/email pipeline.
2. Record a `p.room.notice` from the admin bot into its DM with the recipient. The recipient must be joined.
3. Call `POST /_synapse/client/pangea/v1/deliver_notice` with the following shape. Keep the same run/person and notice references on every retry.

```json
{
  "user_id": "@learner:example.org",
  "category": "activity_nudges",
  "variant": "do_activity",
  "notice_event_id": "$recorded-notice",
  "notice_room_id": "!bot-dm:example.org",
  "delivery_method": "use-available",
  "activity_id": "activity-id",
  "session_room_id": null,
  "push": {
    "title": "Your next activity",
    "body": "Try this activity from your class.",
    "content": {"pangea.activity_id": "activity-id"}
  },
  "email": {
    "subject": "Your next activity",
    "html": "CALLER-RENDERED BRAND HTML",
    "text": "Try this activity: {{cta_url}}\n{{receiving_reason}}\nUnsubscribe: {{unsubscribe_url}}\n{{postal_address}}",
    "receiving_reason": "You're receiving this because you're enrolled in a class on Pangea Chat."
  },
  "log": {
    "run": {
      "run_id": "stable-operator-run-id",
      "runner": "skill",
      "funnel": "learner",
      "decided_at": "2026-09-27T12:00:00Z",
      "policy_version": "learner-funnel-revision"
    },
    "state": {"engagement_status": "quiet_assigned"},
    "copy_key": "do_activity.v1"
  }
}
```

The HTML placeholder above is illustrative, not a sendable body. Render the standard `notice_delivery/templates/brand_base.html` shell (or an activity-card template extending it), passing literal `{{cta_url}}`, `{{unsubscribe_url}}`, `{{receiving_reason}}`, and `{{postal_address}}` into those template slots. Both email bodies must retain all four placeholders. Synapse replaces them literally, escaping HTML values; it does not evaluate caller HTML as Jinja. The receiving reason is required and must be accurate for the recipient. Never pass credentials, email addresses, names, or message bodies in the log context.

`use-available` requires both channel payloads. `email-only` requires email and `push-only` requires push. Either can include the other payload, which is also validated. `in-app-only` uses the already-recorded notice. Email-only categories remain email-only; `allow_notifications` remains in-app-only. Credential and consuming course-claim emails keep their dedicated paths.

The result includes `channel`, `reason`, `push`, `email`, `notification_log_id`, `log_status`, and `duplicate`. A completed retry returns the prior delivery with `duplicate: true`; the replayed push summary contains counts, not device identifiers. HTTP 409 means the run/person is reserved but unresolved, or refers to another notice. Do not change the run ID to evade it. Reconcile the transport and existing log row first. `log_status: pending_reconciliation` means delivery completed but its final log update failed; do not resend. The decision runner still writes decisions that skip before delivery and fills later outcome fields from first-party events.

No new CMS collection or preference store is required. Existing CMS service-user credentials must permit create/read/update on `notification-log`. The legacy flat `body`/`email_subject` request and `deliver_nudge` alias remain available with caller-owned logging; new operator tooling should use the structured payload and must not create a second log row after delivery.

Admin limits are independently configurable through `notice_admin_requests_per_minute` (600) and `notice_admin_burst` (100), per endpoint and caller. These are admission limits, not a measured guarantee of email throughput. Pace batches and keep concurrency bounded.

## Local verification

Run from this checkout with its absolute path in `PYTHONPATH`, so spawned Synapse processes load this checkout instead of another editable installation. The ordinary suite is `python -m unittest discover -s tests -t . -p 'test_*.py'`.

For the real CMS boundary, run `pnpm exec tsx <synapse-checkout>/tests/fixtures/notice_log_cms.mts /tmp/notice-cms.json` from the sibling CMS checkout, whose `.env` must point to local Postgres. Then run `NOTICE_CMS_FIXTURE=/tmp/notice-cms.json python -m unittest tests.integration.notice_delivery_cms` from this checkout. The fixture uses actual CMS REST handlers, authentication, collection hooks, and uniqueness constraints. Stop it with SIGTERM to remove its test rows and temporary service user. It does not replace or restart a running CMS.

After staging deployment, `python -m unittest tests.staging_tests.notice_delivery` exercises the live structured endpoint and CMS log. Supply `SYNAPSE_AUTH_TOKEN` (server admin), `NOTICE_TEST_USER_ID` (an explicitly authorized internal staging recipient), and `NOTICE_CMS_API_KEY` through the environment. This sends one email, records three notices, and attempts one push; it does not establish physical-device receipt. Read the emitted channel and log IDs, and verify the received email's signed links separately. End the temporary admin session afterward.

## Scheduled notices

Add `scheduled_at` (ISO 8601 with a timezone, for example `2026-10-15T14:00:00Z`) to the structured request. Omit `notice_event_id`; supply `sender_id` (a local server admin joined to the DM) and `notice_content` (the exact `p.room.notice` content the client expects). Keep `notice_room_id`, recipient, category/variant, channel content, and `log` as usual. Do not post the Matrix notice yourself or call `prepare_notice` for this request: the service handles suppression and event creation when due.

The endpoint returns HTTP 202 with `schedule_id`, `scheduled_at_ms`, `status`, `result`, and `duplicate`. Repeating the same request returns the same schedule; changing its content or time while reusing the run/person returns 409. Query `GET /_synapse/client/pangea/v1/deliver_notice?schedule_id=…` with a server-admin token for status and the eventual delivery result, including `notification_log_id` and `notice_event_id`.

Queued requests are stored in the Synapse database. Polling checks due work every five seconds; scheduling is a not-before time, not an exact wall-clock guarantee. A time already in the past becomes due on the next poll. No event, push, email, or delivery log is created at submission. When due, the service reserves Notification_Log before creating the notice and uses the usual delivery path. Refusals or lost eligibility produce no notice. Successful completion removes queued message content, retaining the request fingerprint and safe result for deduplication. Existing immediate requests keep their current behavior.

`queued` means no send has started. `complete` includes delivery and definite no-send outcomes; inspect `result.channel` and `result.reason`. `pending_reconciliation` means execution has claimed the job and may be in progress or interrupted; inspect its result/log and server evidence before any resend. The worker never automatically reclaims it, so an SMTP acknowledgement or event-creation result lost during a restart cannot cause duplicate delivery. A queued job survives a restart. There is no recurring schedule or reschedule/cancel API in this version.

## Course/activity images

Images are ordinary HTML in `email.html`; Synapse preserves them while substituting signed link/footer slots. Use public HTTPS assets, descriptive `alt` text, and responsive inline styles. Do not use `mxc://` or authenticated media endpoints, which email clients cannot fetch. The service does not download or proxy images.

The bundled `notice_email.html` brand template accepts an optional `images` list when the caller renders it, with `url` and `alt` for each course/activity image. This is a template rendering argument, not an extra API field. Pass `{{cta_url}}`, `{{unsubscribe_url}}`, `{{receiving_reason}}`, and `{{postal_address}}` through that render unchanged for Synapse to fill. Include the activity/course name and action in `email.text` so blocked images and plaintext readers still receive useful content.
