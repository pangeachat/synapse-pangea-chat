# Scheduled notice eligibility and cancellation — test audit

Date: 2026-10-08. Intent: [notice-delivery.instructions.md, Scheduled delivery](../.github/instructions/notice-delivery.instructions.md#scheduled-delivery). The user approved the eligibility/cancellation wording and requested load testing before production rollout.

## Coverage

| Contract | Evidence |
| --- | --- |
| Optional conditions; reject malformed input before enqueue | `tests/test_notice_eligibility.py`: `test_no_conditions_do_not_read_data`, `test_unknown_or_wrong_types_rejected` |
| Recheck return/contact/activity/session evidence at delivery | `tests/test_notice_eligibility.py`; real Synapse/PostgreSQL/CMS cases in `tests/integration/notice_delivery_cms.py` |
| Unknown evidence sends nothing and records a reason | `test_failed_evidence_suppresses_and_logs`, `test_suppressed_delivery_has_log_but_no_event_or_email`; real CMS unavailable-plan case |
| Cancellation is admin-only, terminal and idempotent | Real HTTP DELETE assertions in `notice_delivery_cms.py`; `test_cancel_survives_restart_and_enqueue_retry` |
| Claimed sends cannot be cancelled or automatically retried | `test_claim_wins_cancellation_cannot_report_success`, `test_interrupted_claim_is_never_replayed`, `test_send_exception_does_not_requeue` |
| Slow delivery cannot create overlapping scheduler drains | `test_slow_delivery_does_not_start_another_drain` |
| Bound eligibility reads and preserve foreground responsiveness | `test_room_cap_stops_before_state_query`; Locust `tests/load_notice_schedule.py` and isolated runner `tests/integration/notice_schedule_load.py` |
| Existing scheduling, deduplication, preferences, email HTML and click path | Existing `tests/test_notice_schedule.py` plus real CMS/captured-SMTP integration, including restart |

Unit tests isolate collaborators intentionally. The CMS integration crosses real service boundaries; SMTP is captured locally. The load scenario uses Locust and performs real in-app event/log writes. No in-scope orphan scripts or bucket misclassifications were found. The load runner is manually invoked and excluded from automatic test discovery.

## Load scope and limits

The isolated fixture uses real Synapse, PostgreSQL and a seeded local CMS with 32 synthetic recipients, each with a small room history. Ten Locust users enqueue/cancel and one continuously syncs. The admin admission limit is raised only in the local fixture to expose worker pressure. Every rung cancels 20% of schedules before their due time; every remaining result must be `in_app` with a complete Notification_Log. Assertions require zero HTTP failures, more than 100 foreground samples, sync p95 below 250 ms and maximum below two seconds.

This measures a local burst, not production capacity. It does not reproduce production membership/history cardinality, federation traffic, hardware, DB pool competition, or email/push transport latency. Peak RSS samples cover the Synapse process, not the CMS and database processes. Slow external delivery is covered by the non-overlap unit test, not by the load fixture. Deployed staging load and host/database observations remain a rollout gate before production.

## Bucket results

The final local Locust ladder passed in 243.9 seconds against the branch after integrating `origin/main`. All HTTP requests succeeded, all intended deliveries finished with complete logs, and all cancellations remained cancelled.

| Scheduled | Delivered / cancelled | Sync p95 | Sync max | Maximum send lateness | Peak Synapse RSS |
| --- | --- | --- | --- | --- | --- |
| 100 | 80 / 20 | 14 ms | 205 ms | 6.6 s | 144.5 MiB |
| 500 | 400 / 100 | 17 ms | 91 ms | 29.5 s | 145.2 MiB |
| 1,000 | 800 / 200 | 21 ms | 153 ms | 64.0 s | 144.4 MiB |

After removing the unindexed global message-history fallback, the final 1,000-notice run also enabled `recipient_not_returned` alongside activity/contact checks. It passed in 139.6 seconds: 800 deliveries, 200 cancellations, zero HTTP failures, sync p95 15 ms / maximum 220 ms, maximum send lateness 78.0 seconds, and peak Synapse RSS 147.8 MiB. This additional run exercises the indexed per-user activity read; the earlier ladder exercised activity/contact checks. Run-to-run timing differences are not evidence of a throughput improvement.

The repository-wide unit/integration suite passed: 1,261 tests in 1,542 seconds. The focused notice suite passed 45 tests after the final optimization. The real CMS/Synapse/captured-SMTP integration passed in 142 seconds, including both canonical and pinned CMS plan reads, full/unknown sessions, saved completion after role removal, returned/contact suppression, admin-only idempotent cancellation and the existing restart/email/click checks. Ruff, Black and full-module mypy checks passed (147 source files).

Smoke/eval involving paid providers are not applicable: this feature changes scheduling and internal eligibility reads, not a paid model/provider integration. Deployed verification requires the new branch to be approved and deployed first; no staging or production claim is made by these local runs.

## Bounded staging probe

`python -m tests.staging_tests.notice_schedule` runs a 100-job Locust probe with three enqueue users and one foreground sync user. Staging requires `NOTICE_LOAD_ALLOW_STAGING=1`, the existing staging bot token, and a CMS admin session for cleanup; it rejects production and sends only to the bot itself in a temporary private room. The queue retains terminal deduplication receipts; the probe removes its room and CMS rows. It verifies 80 in-app deliveries, 20 cancellations, no early events, return/contact suppression and complete log outcomes. `NOTICE_HTTP_PROBE_ONLY=1` selects this same probe in the isolated local runner.

The local rehearsal passed in 179 seconds, with zero Locust HTTP failures, sync p95 13 ms and maximum 101 ms. Black, Ruff and mypy passed across 237 source files. The earlier CI dependency failure was corrected by including the pinned Locust version in development dependencies.
