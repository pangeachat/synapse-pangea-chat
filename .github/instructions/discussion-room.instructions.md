---
applyTo: "synapse_pangea_chat/discussion_room/**"
description: "Discussion rooms on the homeserver: which room a student is placed in, who counts toward a room's 12, how seats are written and ended, the room's power levels and bot, who may join directly, and what the preview answers."
---

# Discussion Rooms — Synapse Module

The homeserver places each student in a room for one of their course's discussions, writes their seat, and ends or reopens it when they tap "I'm done" or "Wait, I'm not done". The start page asks it where Join would place them. Product rules live in the org [activities doc, Mode: Discussion](../../../.github/.github/instructions/activities.instructions.md#mode-discussion).

## Which rooms are a discussion's

- A discussion room is one the discussion bot created and marked, at creation, with its course and discussion. That mark can't be changed later and only the server can make the bot create a room, so a room a student made to look like one is never used.
- Only rooms linked into the course count. A room that can't be linked is left by the bot and the request fails, since nobody could find it.
- The activity must be a Discussion-mode activity with one shared role that this course may use: a catalog discussion filed under one of the course's Missions, as a roleplay is, or the course's own edited copy. The course's Missions are those of its quest (named by `uuid`, or `course_plan_id` in older spaces) and those its own Can-Do list adds; that list counts only when a course admin wrote it, as the client reads it. Another course's copy, or anything else, answers not found. Two courses that share a discussion never share a room.

## Placement

- **Own room.** A student who holds a seat goes back to that room, ended or not, even when it is full, because their awards are there. That includes a student who left on their own.
- **Otherwise** the fullest room under 12, so conversations fill up rather than spread thin, or a new room when none has space.
- **Who counts toward 12:** seat holders who are in the room or on their way in. Students who ended count. A student who left, was removed or was banned doesn't, which frees their place like an empty roleplay seat.
- **Removed or banned** students are never put back in that room; their next tap places them in another.
- **Course admins** who tap Join are placed like students: a seat, and they count. Admins joined automatically to read along have no seat, don't count and are never placed.
- A join a room refuses takes back the seat just written and tries another room. A join still rate limited after about ten seconds takes back the seat and answers busy.

## Seats

- The server is the only writer of seats, one write at a time per discussion, so two students arriving together never overfill a room and an ending never drops someone else's seat.
- A seat is keyed by student, since every student holds the same role; the client and the bot rely on that key.
- Ending sets the student's own finished and saved times to the server's clock; ending again keeps the first time. Reopening clears both. Nothing else changes.

## Power levels and the bot

- The bot creates every room and stays at the top level, so nobody can remove it: placement, awards and facilitation depend on it. A room found without its bot is reported and skipped.
- Course admins are moderators: they can remove or ban a student but can't touch the bot or the room's settings. When the course's admins change, the bot brings every discussion room in line.
- Students can talk but can't change the plan or the seats.
- The room's image comes from the activity's own media, never from the request.
- A new room pins the activity version the start page read, the same reference a roleplay session pins, so a later edit never changes the questions under awards already given. Without a usable version the room follows the latest version.

## Direct joins

A course member could join a discussion room directly, outside placement and its count. That join is refused unless the person holds a seat they weren't removed from, is invited, is a course admin, or is the bot.

## The preview

It answers what Join would do without placing anyone: the room, its count, and whether the student is already in it. It takes no lock and reuses room state for a few seconds.

## Answers

- Placing answers the room and whether it was just made; the preview answers the room (none means a new room), its count and whether the caller is in it; ending or reopening answers the room and the caller's seat.
- 400 for missing fields or an unknown action, 401 without a valid login, 403 for a caller who isn't in the course, whom no room will take, or who ends or reopens a seat they don't hold or in a room that isn't a discussion room, 404 for an activity that isn't one of this course's discussions or an unknown room, 429 when rate limited or a join is still refused as too fast, 502 when the CMS can't be asked, 503 without a discussion bot.

## Failures and concurrency

Every 500 and 502 is reported to Sentry. The per-discussion lock lives in one process, which holds because only the main process serves this endpoint; serving it from several workers would need Synapse's cross-worker lock.

## Configuration

The discussion bot is the account named by the user-activity filter's bot setting, shared with that filter; without it the endpoint answers 503. Room capacity defaults to 12, and request limits are in the shared per-route table.

## Blocked-join gate

Placement doesn't apply the [blocked-join gate](blocked-join-gate.instructions.md). The gate counts the blocks of a room's full admins, and in a discussion room that is only the bot; no student starts the room, so nobody's block would decide.
