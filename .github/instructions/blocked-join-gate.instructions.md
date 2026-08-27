---
applyTo: "synapse_pangea_chat/blocked_join_gate/**,synapse_pangea_chat/room_code/**,tests/test_blocked_join_gate*"
description: "Server-side refusal of knocks and joins from a user every admin of the room has blocked — what counts as blocked, which entry paths are gated, and why the requester gets a generic error."
---

# Blocked Join Gate

When every admin of a room has blocked a user, that user must not be able to knock on or join the room. Synapse does not enforce this on its own, so the module does. Tracked in [#92](https://github.com/pangeachat/synapse-pangea-chat/issues/92).

- **Client-side joining flow**: [joining-courses.instructions.md](../../../client/.github/instructions/joining-courses.instructions.md)
- **Code-based entry** (also gated here): [knock-with-code.instructions.md](knock-with-code.instructions.md)

## Why the server, and why now

"Block" in the client is the standard Matrix ignore list (`m.ignored_user_list` account data). Matrix defines ignoring as a client-side filter: the ignorer stops seeing the ignored user's events. Synapse never consults it for membership, so a blocked user can still knock and the knock lands in the admin's approval queue — the person the admin blocked keeps re-appearing.

Doing the refusal in the admin's client would only work while an admin is online and would have to be replicated in every admin's client. Doing it in the module makes the rule hold whenever Synapse is up, for every entry path.

## Rule

**A membership request into a room is refused if every admin of that room has blocked the requester.** One admin who has not blocked them is enough to let the request through — the admins as a group, not any one of them, decide. A room with no joined admin never refuses on this rule.

| Term | Meaning |
|---|---|
| Blocked | The requester appears in the admin's `m.ignored_user_list` (the client's block list). |
| Admin | A currently joined room member whose power level is at least 100 — the same notion the client uses to decide who may accept or deny knocks. Lower-powered members' block lists have no effect, and neither do those of admins who have left. In room versions where the creator holds power without a power-levels entry, the creator counts as an admin. |
| Membership request | The requester asking to enter: a Matrix knock, a direct join (public or restricted rooms), a join in response to an invite, or the code-based entry below. A join performed by our own server-side orchestration (activity role assignment, instructor analytics access) is not a membership request and is not gated — those flows act on the room's behalf, and refusing them mid-sequence would strand the user with an invite they can never accept. |

The predicate is one shared function, `is_blocked_by_room_admin(user_id, room_id)`, so every entry path gives the same answer.

## Entry paths gated

| Path | How the gate applies |
|---|---|
| Matrix knock | Refused before the knock event is stored, so admins never see it in the knock queue. |
| Direct join / join after invite | Refused via Synapse's join check (`user_may_join_room`). A pending invite does not override the rule: if every admin has since blocked the requester, accepting the invite is refused too. |
| knock_with_code | The endpoint skips the server-side invite for any matched room whose admins have all blocked the requester, treating it like the existing banned-room branch. If every matched room is skipped for this reason the endpoint responds 403. |

Existing membership is out of scope: a member who is later blocked by the admins stays in the room until an admin removes them. The gate governs entry only.

## What the requester sees

The refusal is a generic forbidden error with no reason and no distinct error code. Matrix ignore semantics keep the ignored user unaware that they are ignored, and a "you are blocked" message would leak it. The client shows its ordinary "could not join" handling. This is a deliberate trade against a friendlier message.

## Design decisions

- **Refuse, don't auto-deny.** The alternative — let the knock land, then have the module deny it as an admin — creates extra membership events, needs an admin identity to act as, and can race a real admin's decision. Vetoing at the hook is one code path with nothing for admins to see.
- **"Who blocks this user" comes from Synapse's own ignore index** (`ignored_by`), then compared against the room's joined admins. One cached lookup per request rather than reading each admin's account data, and when nobody blocks the requester no room state is read at all. This reaches past the public module API; it is the same trade the room-code endpoint already makes for its state queries and is called out in code.
- **Configurable off switch.** A single boolean in the module config, default on, so the gate can be disabled from Ansible without a code deploy.
- **Fail open on hostile-shaped rooms.** A room whose power levels name more than a small bound of admin candidates (or make every member an admin) is not a real course; the gate allows rather than paying an unbounded membership scan an attacker could shape. The gate is a courtesy, so failing open is always the safe direction.

## Key Files

- **Gate and hook registration**: `synapse_pangea_chat/blocked_join_gate/`
- **Code-based entry check**: `synapse_pangea_chat/room_code/knock_with_code.py`
- **Registration**: `synapse_pangea_chat/__init__.py`
- **Config**: `synapse_pangea_chat/config.py`
- **Tests**: `tests/test_blocked_join_gate_unit.py`, `tests/test_blocked_join_gate_e2e.py`
