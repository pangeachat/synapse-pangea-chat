---
applyTo: "synapse_pangea_chat/delete_room/**,tests/test_delete_room_e2e.py,tests/test_delete_room_unit.py"
---

# Delete Room — Synapse Module

POST /_synapse/client/pangea/v1/delete_room

Deletes a room from the homeserver. Teachers and course admins reach it from the client (chat details, the chat context menu, and the delete-space dialog). Pangea-bot's space-cleanup job calls the same endpoint to retire inactive spaces.

Matrix has no way to delete a room across all servers, so "delete" here means: detach the room from its parent spaces, remove every local member, and purge all local room data.

### Who may delete

The requester must be authenticated, a joined member of the room, and hold the room's highest power level.

Ties qualify: any member at the top power level may delete. This is intentional — the bot holds admin power level in the rooms it manages, and pangea-bot's cleanup deletes through this same rule.

The comparison covers every member's effective power level, including members who only hold the room's default level.

A room with no power-levels state event denies the request cleanly. It must never return a server error.

The client hides delete buttons from non-admins and in direct chats, but the server-side check is the authority.

### What deletion does

Space link cleanup, best effort. The room's child link in each parent space is removed, sent as the requester. If the requester lacks permission in a parent space, the failure is logged and deletion continues — a leftover child link in the space is accepted rather than blocking deletion.

Every joined local member leaves the room. Each leave is a self-leave (sender is the member) whose content carries `"pangea.room_deleted": true` and a fixed English `reason` — the marker is how clients tell "this room was deleted" apart from a voluntary leave, which is otherwise byte-identical (a kick is already distinguishable by its sender). Delivery of the marker is only as reliable as delivery of the leave itself (see the purge delay below). Members on other homeservers cannot be removed this way; this is accepted because deployment is effectively single-homeserver.

All local room data is purged — but not immediately. The purge is scheduled for days later (`delete_room_purge_delay_seconds`, default 7 days) through Synapse's durable task scheduler. The delay exists so the leave events survive long enough for members' clients to sync them; an immediate purge deletes the leaves before delivery, and those clients then show the deleted room forever. Clients offline for longer than the whole window can still miss the leave — accepted, and tunable via the delay. If a local user rejoins during the window, the scheduled purge fails safe instead of deleting the room out from under them.

Deletion does not block the room from returning through federation or re-creation — the same accepted single-homeserver risk. Because deletion is irreversible, every success is logged with the requester and room id.

### Error contract

Permission denials (not a member, not at the top power level) return 403.
Malformed requests (bad JSON, missing room_id) return 400.
Requests without a valid access token fail with the same authentication semantics as the rest of the module surface.
Rate-limited requests return 429.

### Rate limiting

A per-user sliding window, configured by delete_room_requests_per_burst and delete_room_burst_duration_seconds. It is in-memory and per-process — a best-effort abuse guard, not a security boundary. The client deletes a space's children in parallel, so the configured limit must stay above the largest realistic course size or deleting a big space partially fails.

### Key files
Endpoint registration: ../../synapse_pangea_chat/__init__.py
Handler: synapse_pangea_chat/delete_room/
Config: synapse_pangea_chat/config.py
Tests: tests/test_delete_room_e2e.py, tests/test_delete_room_unit.py
Client caller: client/lib/routes/chat/chat_details/delete_room_extension.dart; bot caller: pangea_bot/synapse_admin_client/room.py (other repos, so plain paths rather than links)