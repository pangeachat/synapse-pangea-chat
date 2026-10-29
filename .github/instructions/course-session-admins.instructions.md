---
applyTo: "synapse_pangea_chat/course_session_admins/**,tests/test_course_session_admins*"
description: "A new activity session shared into a course joins that course's admins, hidden and muted in their app, so a teacher can read any new session without a dashboard join."
---

# Course Session Admins — Synapse Module

A teacher can only read a student's activity session if she is in its room. The dashboard used to join her on demand, one room at a time and with her own token, which Synapse sometimes refused under its per-user join limit. So when a new session is shared into a course, the homeserver joins the course's admins straight away, the way it joins members to a course's default chats.

## Rule

- **When:** a session room is linked into a course space for the first time. A session is a room whose create type is the client's activity-session type. A link sent again for a session already linked changes nothing, so sessions from before this shipped are still reached through the dashboard's on-demand join. There is no backfill.
- **Which rooms:** only a session whose join rule admits this course: `restricted` or `knock_restricted`, with an allow entry naming this course space. Any course member may link a child to the course, so a public, invite-only or other-course room linked into it never pulls the course's admins in.
- **Who:** the course's joined, local admins at power level 100 or more, never the bot. Anyone already in the room, such as the teacher who launched it, is left as they are, and so is anyone who left or was banned from it. Course members who are not admins are never joined.
- **How:** as themselves, through the room's own join rule, with no invite. A room whose rule does not admit course members is not joined.
- **Hidden and muted:** each admin gets the same two writes the dashboard's review join makes. The room goes on her `pangea.admin_review` list before the join, and is muted with the app's own mute rule after it. The app keeps a listed room out of her chats and badges until she takes a role in it. A room that could not be listed is not joined.
- **Never in the way:** the work runs in the background after the link, waits out Synapse's rate limits, and logs each failure to Sentry without failing the student's session.

## Known limits

- The dashboard also writes the `pangea.admin_review` list, and neither writer can update it atomically, so in rare cases one room id can be lost and that room shows in the teacher's chats.
- Sessions that existed before this shipped are not touched.
