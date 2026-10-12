---
applyTo: "synapse_pangea_chat/member_analytics_profiles/**,tests/test_member_analytics_profiles*.py"
description: "Every joined member's public analytics profile for one room, in one request: who may call it, whose profiles it returns, and why the answer is one unpaged response."
---

# Member Analytics Profiles — Synapse Module

The course Leaderboard ranks every joined member on the stars and level in their public analytics profile ([course-leaderboard.instructions.md](../../../client/.github/instructions/course-leaderboard.instructions.md)). That profile is a custom Matrix profile field, `pangea.analytics_profile`, and the Matrix profile API reads one user per request. So the client had to make one request per member before it could show any row. A course of 150 members took about 20 rounds of 8 requests, and the load grew with every member who joined ([client#9465](https://github.com/pangeachat/client/issues/9465)).

Synapse keeps every local user's profile fields in one table. This endpoint reads the profiles of a room's joined members in one query and returns them in one response.

## Who may call it

**Any member currently joined to the room.** Every refusal (unknown room, caller not joined) is the same 403 with the same body, so the endpoint cannot be used to learn which rooms exist.

A joined caller shares the room with every member in the response. The endpoint therefore returns nothing the caller could not already read one profile at a time through the profile API.

It answers for any room, not only course spaces, because the chat participants page and the teacher's analytics page read the same profiles for a room's members.

Calls are rate limited per caller.

## Whose profiles it returns

| Rule | Why |
| --- | --- |
| Current membership is `join` | The client reads profiles only for joined members. Invited and knocking users are never ranked. |
| Local accounts only | Synapse stores profile fields only for its own users. The client fetches a remote member's profile on its own. |
| Every joined local member is listed, with an empty profile when they have none | The client fetches individually any member missing from the response. Listing a member who has no profile tells it there is nothing to fetch. |
| No filtering by role or account type | Who is ranked (the bot left out, admins included) is the Leaderboard's decision, made on the client. |

Each profile is returned exactly as stored. The client parses it the same way it parses the profile API's answer, so the two paths cannot disagree.

## One response, no paging

The whole room comes back at once. The Leaderboard must have every member's numbers before it ranks anyone, so one page of profiles would give it nothing it could show. A profile is a few hundred bytes, so a room of several hundred members is a few hundred kilobytes at most.

## What it never does

- **Change anything.** It is read-only. Each user still publishes their own profile.
- **Answer about a room the caller has not joined.**

## Rollout

There is no config flag, because every caller it answers can already read this data through the profile API. A client that gets a 404 from a server without this module version falls back to one request per member, so the client and the module can ship in either order.
