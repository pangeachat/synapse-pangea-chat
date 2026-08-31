---
applyTo: "synapse_pangea_chat/analytics_push_suppression.py,synapse_pangea_chat/grant_instructor_analytics_access/**,synapse_pangea_chat/assign_room_membership/**"
description: "Why an analytics-room invite must never push, and the two server-side halves that stop it — the invite marker and the account push rule."
---

# Analytics Room Push Suppression

**INVARIANT: an analytics room must never produce a notification.** Analytics rooms are internal data stores that a learner never opens and an instructor reads through the dashboard. A push about one is always noise, and it exposes plumbing the product deliberately hides.

## Why the client cannot do this

Synapse decides what pushes, by evaluating push rules stored per account — see [push-notifications](../../../.github/.github/instructions/push-notifications.instructions.md). Three consequences, each of which sank an earlier client-side attempt:

- A per-room mute is a `room`-class rule. The default invite rule (`.m.rule.invite_for_me`) is an `override`, which outranks it. **A room mute can never suppress an invite**, no matter when it is set.
- The invitee has no room to mute until the invite arrives, so the first push is already gone.
- A rule the client installs at login only protects accounts that have run that client version. An instructor who works from the admin dashboard, or has not reopened the app since, has no rule at all.

Sygnal forwards whatever Synapse decides to send, so it is not a place to intervene either.

## The mechanism

Two halves, each useless alone, both applied server-side by [`analytics_push_suppression`](../../synapse_pangea_chat/analytics_push_suppression.py):

1. **The marker.** An invite into an analytics room carries `reason: "p.analytics_request"` in its content. Push conditions cannot read room state, so a rule has no other way to tell an analytics invite from a real one — the event has to say so itself.
2. **The rule.** Before inviting, the module ensures the invitee's account carries the `p.rule.analytics_invite` override rule that matches that marker and does not notify. Installing it server-side is what removes the dependency on the client.

The client still installs the same rule under the same id, which is harmless — the server check is idempotent and the ids match, so the two never conflict. The client's copy is now a convenience, not the guarantee.

## Where it applies

Both invite paths into an analytics room, because both are how an instructor gets access: the grant endpoint ([grant-instructor-analytics-access](grant-instructor-analytics-access.instructions.md)) and the admin force-join ([assign-room-membership](assign-room-membership.instructions.md)), which the bot's retroactive-grant operator script uses.

Installing the rule is a prerequisite of the invite, not a step beside it, so a failure there stops the grant rather than letting an unprotected invite through. The one failure that is *not* treated as a failure is losing a race to a concurrent install: the rule then exists, which is the outcome that was wanted.

The cost of that ordering is that a genuinely failed install costs the student their grant. Those failures are reported per instructor in the endpoint's `errors`, and **a caller that ignores `errors` turns them into silent data loss** — a student whose instructor never joined, with a `200` on the wire. Anything calling these endpoints is expected to read `errors`, not just the status code.

## Rejected: a module-wide invite guard

Catching *any* invite into an analytics room with a `check_event_allowed` callback was considered and rejected. Registering that callback makes Synapse load prior room state for every event on the homeserver — the check for registered callbacks happens before the state read, so it cannot be narrowed to member events. That is a server-wide cost paid on every message to cover an invite path that does not exist yet. Add the two lines to a third path when a third path is written.

## Not in scope

Ordinary events in an analytics room. They carry Pangea-specific event types, and no default push rule matches an unknown type, so they do not notify today. If an analytics room ever carries `m.room.message`, that changes and this doc needs revisiting.
