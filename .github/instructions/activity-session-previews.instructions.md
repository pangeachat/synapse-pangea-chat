---
applyTo: "synapse_pangea_chat/activity_session_previews/**,synapse_pangea_chat/__init__.py,tests/test_activity_session_previews_e2e.py"
description: "Space-scoped activity-session preview endpoint — what it returns, who may call it, and how it reuses room_preview."
---

# Activity-Session Previews

A space-scoped read that answers "which joinable activity sessions exist under my courses" in one request, so the world map and the activity start page can discover a coursemate's open session without the client walking each course space's full room hierarchy. Client-side discovery design lives in [world-map.instructions.md](../../../client/.github/instructions/world-map.instructions.md) ("Discovering joinable sessions"); this doc owns the server contract.

## Why it exists

Discovery used to enumerate every room under each joined course space from the client and preview the survivors. In a course with more rooms than one hierarchy page, sessions past the first page were missed, and every discovery cycle read thousands of irrelevant rooms — analytics rooms, chats — to surface a handful of sessions ([pangeachat/client#7982](https://github.com/pangeachat/client/issues/7982)). Moving the enumerate-and-filter step to the server, where the space's child list and each child's room state already sit, makes the read both complete and cheap.

## What it does

This module takes the following query parameters as input:
1. A required, comma-separated list of spaceIDs for which to return child activity sessions (?rooms)
2. An options activityID to filter by (?activity)

The endpoint returns preview data for **only** the activity-session rooms under those spaces, in the **same shape as [room_preview](room-preview.instructions.md)**. It is a thin front on the room-preview reader — space to children to activity filter to the existing preview projection — and `room_preview` itself is unchanged, still serving per-room previews (invited sessions, chat details).

- **Optional activity scope.** An activity id narrows the result to that activity's sessions (the start page's case); without it, every activity session under the spaces is returned (the map's case). This includes finished and abandoned sessions, which accumulate over a semester. Filtering for joinable activities is done client-side. We may need to revisit this can course sizes increase.
- **A session is a child room carrying a `pangea.activity_plan`.** That state event is where the preview already reads the activity id, so the filter costs no extra read. The session filter reads pangea.activity_plan independently of the config-driven room_preview_state_event_types list, so a config-trimmed deploy doesn't silently break the map.
- **Direct children only.** Sessions are direct `m.space.child` of the course space. The endpoint returns **all** such children that pass the filter, regardless of the space-child suggestion flag — a launcher-added session must always surface. Do not return children that have been removed from the parent space.

## Access

**Membership-gated, unlike `room_preview`.** `room_preview` answers for any room id to any authenticated caller; a space-scoped query instead reveals *which* sessions exist under a space, so the caller must be a **joined member** of each space they ask about. Spaces the caller is not in are **dropped silently** rather than failing the whole request, so one stale space id never sinks a batch.

## Inherited from room_preview

The response shape, the `membership_summary`, the field-projection of sensitive state (`pangea.activity_plan` down to its reference keys, `m.room.join_rules` down to `join_rule`), the current-state sourcing, the in-memory cache with reactive invalidation, and per-user rate limiting are all [room_preview](room-preview.instructions.md)'s and are unchanged here. This endpoint only adds space-to-children resolution, the membership gate, and the session filter in front of that reader.
