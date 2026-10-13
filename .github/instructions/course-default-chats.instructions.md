---
applyTo: "synapse_pangea_chat/course_default_chats/**,synapse_pangea_chat/__init__.py,tests/test_course_default_chats*"
description: "A space's Announcements and Introductions chats hold every member of the space, with the space's admins as their admins — kept true server-side as chats are created, members join and admins change, plus the repair endpoint for existing spaces."
---

# Course Default Chats — Synapse Module

Every course is a Matrix space, and any space can have two default chats, Announcements and Introductions, which a space admin creates from the course page in the client ([`DefaultChatsRoomExtension`](../../../client/lib/routes/chat_list/default_chats_room_extension.dart)). Announcements is how teachers reach their students, so it is only useful if every student is in it and every teacher can post. Before this module took it on, a member entered a default chat only when their own app opened the course, and power levels in the chat belonged to whoever created it, so a co-instructor could not post ([client#9480](https://github.com/pangeachat/client/issues/9480), [client#7658](https://github.com/pangeachat/client/issues/7658)). A client cannot join other users, and a client-side fix only runs while some admin's app is open, so the rule lives here.

## Rule

For every space and each of its default chats: every member of the space is joined to the chat, and the space's admins are the chat's admins. Admin power in a default chat comes from the space: promote or demote someone on the course page and both chats follow, so a space member who is not a space admin is not an admin in the chat.

| Term | Meaning |
|---|---|
| Space | A room created with type `m.space`. Every course is one, and a space with no quest attached counts too, because any space can get default chats. |
| Default chat | A child of the space, linked by an `m.space.child` event, whose canonical alias localpart starts with `introductionChat` or `announcementsChat`, and whose creator is a space admin or in which a space admin already holds admin power. The client sets that prefix at creation ([`SpaceConstants`](../../../client/lib/pangea/spaces/space_constants.dart)) and it is the only mark a default chat carries, so it is the contract between the client and this module: the client keeps setting it, and this module reads nothing else about the room. The admin test is what makes the mark the admins' own: any member can link a child to a space, because learners' activity sessions fan out into their courses as children, so on the prefix alone a student could create a room and have the server put the whole class in it. A prefixed child that fails the test is left alone and named in the repair report. |
| Member | A joined, local member of the space who is not a bot account ([`bot_user_ids`](../../synapse_pangea_chat/bot_user_ids.py)). The bot creates requested courses as their first admin, so without this exclusion it would become an Announcements admin and a member of every Introductions chat. |
| Space admin | A joined member of the space at power level 100 or more, counting a creator who holds power without a power-levels entry: the definition in [`instructor_access`](../../synapse_pangea_chat/room_code/instructor_access.py) that the instructor-invitation claim already uses. |
| Admin of the chat | Power level 100 in the chat, what its creator holds, so a mirrored space admin can post in Announcements, pin, and change power levels exactly as the creator can. |

## When the rule is applied

Three events on a space apply it. Nothing else does, and events inside the chats themselves never do.

| Event on the space | What happens |
|---|---|
| A default chat is linked to the space (`m.space.child`), which the client sends right after creating the chat | Every member is joined and every space admin made an admin, so the chat starts complete. |
| A user joins the space | They are joined to each default chat the space already has. A profile change, which Matrix also sends as a join event, is not a join and puts nobody back into a chat they were removed from. |
| The space's power levels change | Each default chat's admins are brought in line with the space's admins, in both directions. |

Synapse waits on the hook that sees these events for every event on the homeserver, so the hook only does a cheap check inline and hands the work to a background task.

Members are joined directly, as themselves, with no invite: the chats admit space members by join rule, and inviting first would double the events and hit Synapse's per-room invite limit sooner. A chat whose join rule does not admit space members, one created before that rule, is joined by invite instead, the way the membership endpoint does it. Joins are still subject to Synapse's per-room join limit, so a large class is joined at that limit's pace in the background rather than failing. Banned members are skipped. A member who left a default chat is joined again the next time that chat is fully synced, by a new admin or a repair run; that is deliberate for a teacher channel, to be revisited only on complaint. Orchestrated joins are exempt from the [blocked join gate](blocked-join-gate.instructions.md), like every other server-side join.

Within one sync the promotions come first, then the joins, then the demotions, so a demotion can never remove the one member who could still let the others in; and no demotion is sent when no space admin would remain joined at admin power in the chat. A promotion in a chat is sent by a joined admin of that chat: at creation its creator, afterwards any mirrored space admin. A chat with no joined admin cannot be brought in line; that is logged and captured to Sentry, never repaired by promoting someone, the same stance the [instructor-invitation sender rule](knock-with-code.instructions.md#codes-share-kit-and-existing-courses) takes. A demotion is sent by the member being demoted, because Matrix lets an admin lower only their own level and never another admin's; a demoted member who has already left the chat keeps their stale entry until they rejoin and the next sync.

## Repairing existing spaces

A server-admin endpoint, `POST /_synapse/client/pangea/v1/sync_course_default_chats`, applies the rule to one space, named by `room_id`. With `dry_run` it answers a report per chat of who would be joined, promoted or demoted and changes nothing. Without it, it answers at once and does the work in the background, because joining a large class is paced by Synapse's join limit and outlasts what the reverse proxy allows a request; the dry run before and after is the report. It takes exactly one space per call: an endpoint that could sweep the whole homeserver is too large a change to leave open behind one request, so the decision of which spaces to touch is made by an operator, in the open. The operator script `scripts/repair_course_default_chats.py` walks every space on the homeserver, dry-runs each, prints the spaces where something would change, and only with `--apply` repairs exactly those, one call per space, re-checking each with dry runs until nothing is left to change. That is how spaces created before this module are brought in line after deploy; with a single room id it is the support tool when a teacher reports a co-instructor who cannot post. One member's failure never stops the rest.

## Off switch

A single boolean in the module config, default on, so the behaviour can be disabled from Ansible without a code deploy.

## Out of scope

- Leaving or being removed from the space removes nobody from a default chat. The client removes a leaving member from the space's rooms itself, so no server rule is needed for now; revisit if removed students turn up still reading Announcements.
- Other chats in the space, activity sessions and admin-made group chats, are untouched.
- The client's own join on opening a course (`joinDefaultChats`, [client#9031](https://github.com/pangeachat/client/issues/9031)) stays as a fallback until this module is on production, then goes; its row in the client's [auto-join table](../../../client/.github/instructions/joining-courses.instructions.md#all-auto-join-cases) changes with it.

## Key Files

- Hook, sync and endpoint: `synapse_pangea_chat/course_default_chats/`
- Shared server-side join (`server_initiated_join`), also used by `assign_room_membership` and the analytics grant: `synapse_pangea_chat/blocked_join_gate/`
- Registration: `synapse_pangea_chat/__init__.py`; config: `synapse_pangea_chat/config.py`
- Tests: `tests/test_course_default_chats_unit.py`, `tests/test_course_default_chats_e2e.py`
