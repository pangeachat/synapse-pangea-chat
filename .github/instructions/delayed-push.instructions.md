---
description: "Delayed HTTP push suppression rules and Synapse private-API audit requirements."
applyTo: "synapse_pangea_chat/delayed_push/**,synapse_pangea_chat/config.py,synapse_pangea_chat/__init__.py,tests/test_delayed_push*.py"
---

# Delayed Push

Delayed push is an optional Synapse-module-only policy for normal Matrix HTTP pushers. It reduces duplicate phone notifications while a user is actively using another client.

## Contract

- Applies only to normal Synapse `HttpPusher` notifications. DirectPush, email pushers, and badge-only receipt updates remain unchanged.
- Uses Synapse's combined per-user `currently_active` presence signal. Online-but-not-currently-active, unavailable, offline, disabled presence, or presence lookup failure all send normally.
- Read wins over every other state: if Synapse no longer returns a deferred event as unread, no notification is sent.
- Unread events for currently-active users defer at the configured interval until the user becomes inactive or the event reaches the configured max age.
- The max delay clock is measured from the event origin timestamp, not from first deferral.
- Deferral intentionally keeps the pusher cursor unchanged and accepts head-of-line blocking for that user's HTTP pusher/device. Other users and non-HTTP push paths are not blocked.
- Fail open: delayed-push decision errors log and send normally.

## Synapse private API requirement

This feature monkey-patches Synapse's private `HttpPusher` processing path. Keep it disabled by default and require an exact audited Synapse version when enabling it. Every Synapse upgrade must audit `synapse.push.httppusher.HttpPusher._unsafe_process`, `_start_processing`, pusher cursor advancement, and presence `currently_active` semantics before widening the allowed version.

## Rollout

Enable explicitly per environment. Test local/staging before production, and keep logs available for deferred, sent-because-inactive, sent-because-max-age, suppressed-read, and fail-open decisions.
