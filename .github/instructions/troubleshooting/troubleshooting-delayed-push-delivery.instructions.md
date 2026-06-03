---
description: "Troubleshooting delayed_push cases where a user expects a phone notification after becoming inactive."
applyTo: "synapse_pangea_chat/delayed_push/**,.github/instructions/delayed-push.instructions.md"
---

# Delayed Push Delivery Troubleshooting

## Symptoms

- A staging user receives a web/client notification but no phone notification after another user or bot replies.
- The user appears inactive from the client, but phone push does not arrive until the delayed-push max age.
- Synapse pusher cursors remain behind the event while the event is still pending.

## Root cause

- `delayed_push` uses Synapse presence `currently_active`, not the operator's or client's perception of inactivity.
- Synapse can still report `currently_active=true` while the latest presence row is `offline`; in that state delayed_push continues to defer until the user becomes not currently active or the event reaches `max_delay_ms`.
- Once Sygnal accepts a notification and Synapse advances the pusher cursor, Synapse will not retry that same event even if the phone does not display it.

## Verification

- Check the target user's `pushers.last_stream_ordering` against the event's `event_push_actions.stream_ordering`.
- Check `presence_stream.state`, `currently_active`, and `last_user_sync_ts` for the target user.
- Check Sygnal logs around the pusher `last_success` time for the event ID; redact push tokens from any shared output.
- If pusher cursors advanced past the event and Sygnal returned HTTP 200, the server side considers the phone notification sent.
