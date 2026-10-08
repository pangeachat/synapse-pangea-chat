"""Optional scheduled-notice gates. Unknown evidence never authorizes a send."""

import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode

from synapse.storage.engines import PostgresEngine

from synapse_pangea_chat.blocked_join_gate.is_blocked_by_room_admin import (
    is_blocked_by_room_admin,
)

logger = logging.getLogger(__name__)
MAX_ROOMS = 256
MAX_ROLES = 32
FLAGS = {"recipient_not_returned", "activity_not_started", "session_available"}


def validate_eligibility(body: dict) -> None:
    if "eligibility" not in body:
        return
    conditions = body["eligibility"]
    if "scheduled_at" not in body or not isinstance(conditions, dict):
        raise ValueError("eligibility requires a scheduled notice and an object")
    if conditions.keys() - FLAGS - {"min_contact_spacing_ms"}:
        raise ValueError("Unknown eligibility condition")
    for key in FLAGS:
        if key in conditions and not isinstance(conditions[key], bool):
            raise ValueError(f"eligibility.{key} must be a boolean")
    spacing = conditions.get("min_contact_spacing_ms", 0)
    if (
        isinstance(spacing, bool)
        or not isinstance(spacing, int)
        or not 0 <= spacing <= 30 * 86400000
    ):
        raise ValueError("min_contact_spacing_ms must be 0 through 30 days")
    for flag, target in (
        ("activity_not_started", "activity_id"),
        ("session_available", "session_room_id"),
    ):
        if conditions.get(flag) and not body.get(target):
            raise ValueError(f"{flag} requires {target}")


class NoticeEligibility:
    def __init__(self, api: Any, delivery_log: Any):
        self.api = api
        self.store = api._hs.get_datastores().main
        self.log = delivery_log
        self.clock = api._hs.get_clock()

    async def _query(self, sql: str, args: tuple = ()):
        def read(txn):
            # A bad plan must not monopolize a production DB connection.
            if isinstance(self.store.db_pool.engine, PostgresEngine):
                txn.execute("SET LOCAL statement_timeout = '250ms'")
            txn.execute(sql, args)
            return txn.fetchall()

        return await self.store.db_pool.runInteraction("notice_eligibility", read)

    async def check(self, body: dict, req: Any, record_id: str) -> str | None:
        conditions = body.get("eligibility", {})
        if not conditions:
            return None
        try:
            if conditions.get("recipient_not_returned"):
                decided = int(
                    datetime.fromisoformat(
                        req.log.run["decided_at"].replace("Z", "+00:00")
                    ).timestamp()
                    * 1000
                )
                presence = (
                    await self.api._hs.get_presence_handler().current_state_for_user(
                        req.user_id
                    )
                )
                if presence.last_active_ts > decided:
                    return "recipient_returned"
                rows = await self._query(
                    "SELECT 1 FROM user_ips WHERE user_id = ? AND last_seen > ? LIMIT 1",
                    (req.user_id, decided),
                )
                if rows:
                    return "recipient_returned"
                rows = await self._query(
                    "SELECT 1 FROM events WHERE sender = ? AND type = 'm.room.message' "
                    "AND origin_server_ts > ? LIMIT 1",
                    (req.user_id, decided),
                )
                if rows:
                    return "recipient_returned"
            spacing = conditions.get("min_contact_spacing_ms", 0)
            if spacing:
                since = datetime.fromtimestamp(
                    (self.clock.time_msec() - spacing) / 1000, timezone.utc
                ).isoformat()
                code, result = await self.log._request(
                    "GET",
                    "?"
                    + urlencode(
                        {
                            "where[subject.matrix_user_id][equals]": req.user_id,
                            "where[run.funnel][equals]": req.log.run["funnel"],
                            "where[decision.outcome][equals]": "send",
                            "where[createdAt][greater_than]": since,
                            "where[id][not_equals]": record_id,
                            "limit": 1,
                            "depth": 0,
                        }
                    ),
                )
                if code != 200 or not isinstance(result.get("docs"), list):
                    raise ValueError("Contact history unavailable")
                if result["docs"]:
                    return "contact_spacing"
            if conditions.get("activity_not_started"):
                reason = await self._activity(req.user_id, body["activity_id"])
                if reason:
                    return reason
            if conditions.get("session_available"):
                return await self._session(req.user_id, body["session_room_id"])
            return None
        except Exception:
            # No event or transport has run yet. Record the failed check as a
            # skip rather than retrying a stale invitation later.
            logger.exception("Scheduled notice eligibility evidence unavailable")
            return "eligibility_unavailable"

    async def _activity(self, user_id: str, activity_id: str) -> str | None:
        # Start from this user's membership index, including left rooms. Never
        # enumerate a course or the global activity/session catalog per notice.
        rows = await self._query(
            "SELECT room_id FROM local_current_membership WHERE user_id = ? LIMIT ?",
            (user_id, MAX_ROOMS + 1),
        )
        if len(rows) > MAX_ROOMS:
            return "eligibility_evidence_limit"
        rooms = [row[0] for row in rows]
        if not rooms:
            raise ValueError("No membership evidence for recipient")
        placeholders = ",".join("?" for _ in rooms)
        state = await self._query(
            "SELECT c.room_id, c.type, j.json FROM current_state_events c "
            "JOIN event_json j ON j.event_id = c.event_id "
            f"WHERE c.room_id IN ({placeholders}) AND c.state_key = '' "
            "AND c.type IN ('pangea.activity_plan', 'pangea.activity_roles', 'm.room.create', 'pangea.activity_room_ids')",
            tuple(rooms),
        )
        by_room: dict = {}
        owned_rooms = set()
        for room, kind, raw in state:
            event = json.loads(raw)
            content = event["content"]
            if kind == "m.room.create" and event.get("sender") == user_id:
                owned_rooms.add(room)
            by_room.setdefault(room, {})[kind] = content
        saved = set()
        for room in owned_rooms:
            ids = by_room[room].get("pangea.activity_room_ids", {}).get("room_ids", [])
            if not isinstance(ids, list) or any(not isinstance(r, str) for r in ids):
                raise ValueError("Malformed saved activity list")
            saved.update(ids)
            if len(saved) > MAX_ROOMS:
                return "eligibility_evidence_limit"
        missing = saved - by_room.keys()
        if missing:
            placeholders = ",".join("?" for _ in missing)
            rows = await self._query(
                "SELECT c.room_id, j.json FROM current_state_events c "
                "JOIN event_json j ON j.event_id = c.event_id "
                f"WHERE c.room_id IN ({placeholders}) AND c.state_key = '' AND c.type = 'pangea.activity_plan'",
                tuple(missing),
            )
            for room, raw in rows:
                by_room[room] = {"pangea.activity_plan": json.loads(raw)["content"]}
        for room, events in by_room.items():
            if events.get("pangea.activity_plan", {}).get("activity_id") != activity_id:
                continue
            if room in saved:
                return "activity_already_completed"
            roles = self._roles(events.get("pangea.activity_roles", {}))
            if any(
                role.get("user_id", role.get("userId")) == user_id
                for role in roles.values()
            ):
                return "activity_already_started"
        if any(
            not by_room.get(room, {}).get("pangea.activity_plan", {}).get("activity_id")
            for room in saved
        ):
            raise ValueError("Saved activity identity unavailable")
        return None

    @staticmethod
    def _roles(content: dict) -> dict:
        roles = content.get("roles", {})
        if not isinstance(roles, dict) or len(roles) > MAX_ROLES:
            raise ValueError("Unusable activity roles")
        if any(
            not isinstance(r, dict)
            or not isinstance(r.get("user_id", r.get("userId")), str)
            for r in roles.values()
        ):
            raise ValueError("Malformed activity role")
        return roles

    async def _session(self, user_id: str, room_id: str) -> str | None:
        kinds = (
            "pangea.activity_plan",
            "pangea.activity_roles",
            "m.room.join_rules",
            "m.room.tombstone",
        )
        state = await self.api.get_room_state(
            room_id,
            event_filter=[(k, "") for k in kinds] + [("m.room.member", user_id)],
        )

        def content(kind):
            event = state.get((kind, ""))
            return dict(event.content) if event is not None else {}

        if content("m.room.tombstone"):
            return "session_inaccessible"
        member = state.get(("m.room.member", user_id))
        membership = member.content.get("membership") if member else None
        if membership == "ban":
            return "session_inaccessible"
        if membership != "join":
            rules = content("m.room.join_rules")
            accessible = membership == "invite" or rules.get("join_rule") == "public"
            if not accessible and rules.get("join_rule") in {
                "restricted",
                "knock_restricted",
            }:
                allowed = rules.get("allow", [])
                if not isinstance(allowed, list) or len(allowed) > MAX_ROLES:
                    raise ValueError("Unusable restricted-room access evidence")
                for rule in allowed:
                    if rule.get("type") == "m.room_membership":
                        (
                            parent_membership,
                            _,
                        ) = await self.store.get_local_current_membership_for_user_in_room(
                            user_id, rule["room_id"]
                        )
                        if parent_membership == "join":
                            accessible = True
                            break
            if not accessible or await is_blocked_by_room_admin(
                self.api, room_id, user_id
            ):
                return "session_inaccessible"
        plan = content("pangea.activity_plan")
        if not plan.get("activity_id"):
            raise ValueError("Session plan unavailable")
        roles = self._roles(content("pangea.activity_roles"))
        planned_roles = plan.get("roles")
        if not isinstance(planned_roles, (dict, list)):
            if plan.get("version_id"):
                code, document = await self.log._request(
                    "GET",
                    "/" + quote(str(plan["version_id"]), safe=""),
                    collection="activities-v2/versions",
                )
                document = document.get("version", {})
            else:
                code, found = await self.log._request(
                    "GET",
                    "?"
                    + urlencode(
                        {
                            "where[res.plan.activity_id][equals]": plan["activity_id"],
                            "where[req.source_request_hash][exists]": "false",
                            "limit": 2,
                            "depth": 0,
                        }
                    ),
                    collection="activities-v2",
                )
                docs = found.get("docs", [])
                if len(docs) != 1:
                    raise ValueError("Ambiguous activity plan")
                document = docs[0]
            resolved = document.get("res", {}).get("plan", {})
            if code != 200 or resolved.get("activity_id") != plan["activity_id"]:
                raise ValueError("Activity plan unavailable")
            planned_roles = resolved.get("roles")
        capacity = (
            len(planned_roles) if isinstance(planned_roles, (dict, list)) else None
        )
        if (
            isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or not 0 < capacity <= MAX_ROLES
        ):
            raise ValueError("Session capacity unavailable")
        holders = [r.get("user_id", r.get("userId")) for r in roles.values()]
        members = (
            await self.api.get_room_state(
                room_id, event_filter=[("m.room.member", u) for u in holders]
            )
            if holders
            else {}
        )
        occupied = 0
        for role in roles.values():
            holder = role.get("user_id", role.get("userId"))
            event = members.get(("m.room.member", holder))
            if event is None:
                raise ValueError("Role membership unavailable")
            if role.get("finished_at", role.get("finishedAt")) or role.get(
                "archived_at", role.get("archivedAt")
            ):
                return "session_ended"
            if event.content.get("membership") not in {"leave", "ban"}:
                occupied += 1
        return "session_full" if occupied >= capacity else None
