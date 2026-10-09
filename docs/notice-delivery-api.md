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

The result includes `channel`, `reason`, `push`, `email`, `notification_log_id`, `log_status`, and, on a replay, `duplicate: true`. A completed retry returns the prior delivery with `duplicate: true`; the replayed push summary contains counts, not device identifiers. HTTP 409 means the run/person is reserved but unresolved, or refers to another notice. Do not change the run ID to evade it. Reconcile the transport and existing log row first. `log_status: pending_reconciliation` means delivery completed but its final log update failed; do not resend. The decision runner still writes decisions that skip before delivery and fills later outcome fields from first-party events.

**Caller-owned record.** A request may carry `notification_log_id`, the Notification_Log row the caller already wrote (a non-empty string of at most 128 characters). Synapse then reserves and finishes nothing in the CMS, answers `log_status: "caller"`, and the caller writes the receipt onto its own row. The `log` context becomes optional, except that eligibility conditions still require it. Idempotency does not move: Synapse claims the row in its own transport-claim table before any transport; a repeat of the same request after a definite result replays that result with `duplicate: true`; a repeat while the earlier attempt is uncertain answers 409 until reconciled; a different payload under the same row answers 409. Every 4xx on this path happens before any send and is a definite no-send; only a 5xx or a timeout is uncertain. The engagement runner is the caller that sends this way.

No new CMS collection or preference store is required. Existing CMS service-user credentials must permit create/read/update on `notification-log`. The legacy flat `body`/`email_subject` request and `deliver_nudge` alias remain available with caller-owned logging; new operator tooling should use the structured payload and must not create a second log row after delivery.

Admin limits are independently configurable through `notice_admin_requests_per_minute` (600) and `notice_admin_burst` (100), per endpoint and caller. These are admission limits, not a measured guarantee of email throughput. Pace batches and keep concurrency bounded.

## Local verification

Run from this checkout with its absolute path in `PYTHONPATH`, so spawned Synapse processes load this checkout instead of another editable installation. The ordinary suite is `python -m unittest discover -s tests -t . -p 'test_*.py'`.

For the real CMS boundary, run `pnpm exec tsx <synapse-checkout>/tests/fixtures/notice_log_cms.mts /tmp/notice-cms.json` from the sibling CMS checkout, whose `.env` must point to local Postgres. Then run `NOTICE_CMS_FIXTURE=/tmp/notice-cms.json python -m unittest tests.integration.notice_delivery_cms` from this checkout. The fixture uses actual CMS REST handlers, authentication, collection hooks, and uniqueness constraints. Stop it with SIGTERM to remove its test rows and temporary service user. It does not replace or restart a running CMS.

After staging deployment, `python -m unittest tests.staging_tests.notice_delivery` exercises the live structured endpoint and CMS log. Supply `SYNAPSE_AUTH_TOKEN` (server admin), `NOTICE_TEST_USER_ID` (an explicitly authorized internal staging recipient), and `NOTICE_CMS_API_KEY` through the environment. This sends one email, records three notices, and attempts one push; it does not establish physical-device receipt. Read the emitted channel and log IDs, and verify the received email's signed links separately. End the temporary admin session afterward.

## Scheduled notices

Add `scheduled_at` (ISO 8601 with a timezone, for example `2026-10-15T14:00:00Z`) to the structured request. Omit `notice_event_id`; supply `sender_id` (a local server admin joined to the DM) and `notice_content` (the exact `p.room.notice` content the client expects). Keep `notice_room_id`, recipient, category/variant, channel content, and `log` as usual. Do not post the Matrix notice yourself or call `prepare_notice` for this request: the service handles suppression and event creation when due.

The endpoint returns HTTP 202 with `schedule_id`, `scheduled_at_ms`, `status`, `result`, and `duplicate`. Repeating the same request returns the same schedule; changing its content or time while reusing the run/person (or the caller's row) returns 409. Query `GET /_synapse/client/pangea/v1/deliver_notice?schedule_id=…` with a server-admin token for status and the eventual delivery result, including `notification_log_id` and `notice_event_id`.

Queued requests are stored in the Synapse database. Polling checks due work every five seconds; scheduling is a not-before time, not an exact wall-clock guarantee. A time already in the past becomes due on the next poll. No event, push, email, or delivery log is created at submission. When due, the service reserves Notification_Log before creating the notice (or, for a caller-owned request, claims the row in its transport-claim table) and uses the usual delivery path. Refusals or lost eligibility produce no notice. Successful completion removes queued message content, retaining the request fingerprint and safe result for deduplication. Existing immediate requests keep their current behavior.

`queued` means no send has started. `complete` includes delivery and definite no-send outcomes; inspect `result.channel` and `result.reason`. `pending_reconciliation` means execution has claimed the job and may be in progress or interrupted; inspect its result/log and server evidence before any resend. The worker never automatically reclaims it, so an SMTP acknowledgement or event-creation result lost during a restart cannot cause duplicate delivery. A queued job survives a restart. There is no recurring schedule or reschedule API.

Cancel with `DELETE /_synapse/client/pangea/v1/deliver_notice?schedule_id=…`, using a server-admin token. HTTP 200 with `status: cancelled` confirms that execution cannot start. Repeating cancellation returns the same status. HTTP 409 means the worker already claimed the job (or it completed); delivery cannot be recalled. Retrying the original enqueue request still returns the cancelled schedule, without resurrecting it.

### Optional eligibility conditions

Add an `eligibility` object to a scheduled request. Unknown keys and invalid types are rejected before enqueue. All supplied conditions must pass; omitted conditions are not evaluated.

| Field | Type | Evidence checked when due |
| --- | --- | --- |
| `recipient_not_returned` | Boolean | Presence activity and indexed persisted client activity since `log.run.decided_at`. These are server-observed activity signals, not email opens. Synapse batches persisted client activity, so this is not an instantaneous activity fence. |
| `min_contact_spacing_ms` | Integer, 0–30 days | Other send decisions for this recipient and funnel in Notification_Log, using their reservation creation time. Pending send reservations count conservatively; the current reservation is excluded. |
| `activity_not_started` | Boolean | Requires `activity_id`. Current role assignments and the recipient-owned saved activity list, including previously left sessions. A claimed role counts as starting. |
| `session_available` | Boolean | Requires `session_room_id`. Membership/access, replacement-room state, assigned roles, completion and capacity from the embedded or CMS-resolved activity plan. A pinned plan is read at its pinned version. |

Suppression returns `channel: none`, a reason such as `recipient_returned`, `contact_spacing`, `activity_already_started`, `activity_already_completed`, `session_full`, `session_ended`, or `session_inaccessible`, and the Notification_Log ID. Failed or malformed evidence returns `eligibility_unavailable`; exceeding the bounded room evidence returns `eligibility_evidence_limit`. These are terminal skips, not delayed retries.

The room evidence read is capped at 256 membership rooms and 256 saved sessions; role/access-rule reads are capped at 32 entries. PostgreSQL eligibility queries have a 250 ms statement timeout. The scheduler allows only one active drain per process, including when a delivery takes longer than the five-second polling interval. Inspect skips and measured queue lag before increasing traffic; these limits are not a production capacity claim.

### Local load verification

Start the existing local CMS fixture, then run `NOTICE_CMS_FIXTURE=/tmp/notice-cms.json python -m unittest tests.integration.notice_schedule_load` with the normal local Synapse test environment. The runner creates an isolated homeserver/database and 32 synthetic recipients, runs the Locust scenario in `tests/load_notice_schedule.py` at 100/500/1,000 scheduled requests, cancels one fifth, exercises due-time activity/contact reads, and keeps foreground `/sync` traffic active. Only in-app delivery is used. It requires zero HTTP failures, correct terminal outcomes, foreground sync p95 below 250 ms and maximum below two seconds; these are local regression budgets. It reports maximum delivery lateness, Synapse-process peak RSS and Locust CSV paths. The runner rejects non-local CMS and the scenario rejects non-local Synapse. Local results do not establish deployed staging/production headroom.

## Course/activity images

Images are ordinary HTML in `email.html`; Synapse preserves them while substituting signed link/footer slots. Use public HTTPS assets, descriptive `alt` text, and responsive inline styles. Do not use `mxc://` or authenticated media endpoints, which email clients cannot fetch. The service does not download or proxy images.

The bundled `notice_email.html` brand template accepts an optional `images` list when the caller renders it, with `url` and `alt` for each course/activity image. This is a template rendering argument, not an extra API field. Pass `{{cta_url}}`, `{{unsubscribe_url}}`, `{{receiving_reason}}`, and `{{postal_address}}` through that render unchanged for Synapse to fill. Include the activity/course name and action in `email.text` so blocked images and plaintext readers still receive useful content.
