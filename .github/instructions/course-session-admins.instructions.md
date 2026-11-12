---
applyTo: "synapse_pangea_chat/course_session_admins/**,tests/test_course_session_admins*"
description: "A new activity session shared into a course joins that course's admins, hidden and muted in their app and at the room's invite level, so a teacher can read any new session without a dashboard join and invite her students to it."
---

# Course Session Admins — Synapse Module

A teacher can only read a student's activity session if she is in its room. The dashboard used to join her on demand, one room at a time and with her own token, which Synapse sometimes refused under its per-user join limit. So when a new session is shared into a course, the homeserver joins the course's admins straight away, the way it joins members to a course's default chats.

## Rule

- **When:** a session room is linked into a course space for the first time. A session is a room whose create type is the client's activity-session type. A link sent again for a session already linked changes nothing, so sessions from before this shipped are still reached through the dashboard's on-demand join. Older sessions are reached on request (below).
- **Which rooms:** only a session whose join rule admits this course: `restricted` or `knock_restricted`, with an allow entry naming this course space. Any course member may link a child to the course, so a public, invite-only or other-course room linked into it never pulls the course's admins in.
- **Who:** the course's joined, local admins at power level 100 or more, never the bot. Anyone already in the room, such as the teacher who launched it, is not joined again, and anyone who left or was banned from it is left out. Course members who are not admins are never joined.
- **How:** as themselves, through the room's own join rule, with no invite. A room whose rule does not admit course members is not joined.
- **Hidden and muted:** each admin gets the same two writes the dashboard's review join makes, both before the join: the room goes on her `pangea.admin_review` list and is muted with the app's own mute rule. Muting first means no message sent while she is being joined can notify her. The app keeps a listed room out of her chats and badges until she takes a role in it. A room that could not be listed or muted is not joined. If the join then does not happen, the listing and the mute stay.
- **Able to invite:** each admin in the room, just joined or already in, is raised to the room's invite level, so she can invite classmates to a session from the dashboard. The client creates a session where inviting takes 50 and only the creator and the bot hold a level, so a joined teacher would otherwise sit at 0. She is raised to the invite level and no higher. In a session that level also lets her kick, ban, redact and send most room state, as the bot can; changing power levels stays with the creator. Nobody is ever lowered and nothing else in the room's power levels changes. The change is sent as the room's highest-powered local member who may change power levels, which in a session is its creator; the bot holds 50 there and cannot. A failed raise is logged and leaves the join in place. A [discussion room](discussion-room.instructions.md) is the exception: only its bot invites, at the top level, so raising an admin to its invite level would hand them the bot's power; its admins are joined to read along and left where the discussion module puts them.
- **Never in the way:** the work runs in the background after the link, waits out Synapse's rate limits, and logs each failure to Sentry without failing the student's session. It runs on one Synapse process only, the push rules writer, since only that process can write the mute.

## Older sessions, on request

A server admin can join a course's admins to the sessions it already links, one course per call (`POST /_synapse/client/pangea/v1/backfill_course_session_admins`). Each session gets exactly the treatment a new one does: the same rooms and admins qualify, and each admin is listed, muted, joined and raised to the invite level, at the pace Synapse's rate limits allow. An admin an earlier join left in the room at the default level is raised too. The call answers at once with how many sessions it will walk and does the joins in the background, logging progress. Running it again joins nobody already in. The call must reach the push rules writer; any other process answers 421 and does nothing.

## Known limits

- The dashboard also writes the `pangea.admin_review` list, and neither writer can update it atomically, so in rare cases one room id can be lost and that room shows in the teacher's chats.
- Sessions from before this shipped are only touched when a server admin runs the backfill for their course.
- The power levels are replaced as a whole event, read just before the write. A change someone else makes to them in that moment can be lost; the module's own writes to one room never race each other.
- A session whose creator has left usually has nobody left who may change its power levels, so its admins are joined but not raised; each such room is logged to Sentry.
