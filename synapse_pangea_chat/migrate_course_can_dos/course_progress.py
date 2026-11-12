"""Server-admin endpoint: move learners' course progress records from old
(pre-merge) Can-Do ids to the merged Can-Dos, each sent as its learner.

``POST /_synapse/client/pangea/v1/migrate_course_progress``

Design: migrate-course-progress.instructions.md. A learner's
``pangea.course_progress`` (in their analytics room, state key = course room
id) keys ``can_dos`` by Can-Do id. A learner's app rebuilds that record from
the Can-Dos its course lists, so once a course switches it drops the old
entries and their completion dates. Each merged Can-Do therefore takes the
earliest completion found in the current record and in every earlier version
the learner sent, under the old ids and the merged id alike, so a re-run, or
one after a stale app wrote old ids back, never loses a completion.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from synapse.api.constants import EventTypes
from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from synapse.util.duration import Duration
from synapse.util.frozenutils import unfreeze
from twisted.web.resource import Resource

from synapse_pangea_chat.analytics_push_suppression import ANALYTICS_ROOM_TYPE
from synapse_pangea_chat.migrate_course_can_dos.migrate_course_can_dos import (
    Rewrite,
    parse_can_do_map,
)
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.migrate_course_can_dos.course_progress"
)

COURSE_PROGRESS_STATE_EVENT_TYPE = "pangea.course_progress"
DATE_KEY = "first_completed_at"
THRESHOLD_KEY = "xp_to_complete_at_first_completion"
REPLAYED_KEY = "completed_before_tracking"

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
# At most ten sends a second.
SEND_PAUSE = Duration(milliseconds=100)


# --------------------------------------------------------------------------
# Pure decisions
# --------------------------------------------------------------------------


def _instant(value: Any) -> Optional[datetime]:
    """An ISO-8601 instant as aware UTC (the app writes UTC with a ``Z``);
    anything else None."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    # silent-ok: an unreadable date is no date, as both readers treat it
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _completion(entry: Any) -> Optional[Tuple[datetime, Dict[str, Any]]]:
    """An entry's first completion: when, and the fields that go with it.
    None for an entry with no readable date, or a threshold the dashboard
    would refuse."""
    if not isinstance(entry, Mapping):
        return None
    at = _instant(entry.get(DATE_KEY))
    threshold = entry.get(THRESHOLD_KEY)
    if at is None or not (
        threshold is None
        or (isinstance(threshold, int) and not isinstance(threshold, bool))
    ):
        return None
    return at, {
        DATE_KEY: entry[DATE_KEY],
        THRESHOLD_KEY: threshold,
        REPLAYED_KEY: entry.get(REPLAYED_KEY) is True,
    }


def _xp(entry: Mapping[str, Any]) -> int:
    xp = entry.get("xp")
    return xp if isinstance(xp, int) and not isinstance(xp, bool) else 0


def merge_progress(
    content: Mapping[str, Any],
    earlier: Iterable[Tuple[str, Mapping[str, Any]]],
    can_do_map: Mapping[str, str],
) -> Rewrite:
    """The record with each old Can-Do key moved to its merged Can-Do.

    ``earlier`` holds ``(Can-Do id, entry)`` from the learner's earlier
    versions of this record. A merged Can-Do is written only when the current
    record names it, or an old id of it. Its figures stay as the current
    record has them under the merged id, else come from the old entry with
    the most XP. Its first completion is the earliest found anywhere, with
    the threshold and replay mark stored beside it. Everything else in the
    record is left as it is.
    """
    report: Dict[str, Any] = {"keys_moved": 0, "completions_kept": 0}
    record: Dict[str, Any] = unfreeze(content)
    can_dos = record.get("can_dos")
    if not isinstance(can_dos, dict):
        return Rewrite(None, report)
    merged_ids = set(can_do_map.values())

    def target(key: str) -> Optional[str]:
        return can_do_map.get(key, key if key in merged_ids else None)

    groups: Dict[str, List[str]] = {}
    for key in can_dos:
        merged = target(key)
        if merged is not None:
            groups.setdefault(merged, []).append(key)
    if not groups:
        return Rewrite(None, report)
    history: Dict[str, List[Any]] = {}
    for key, entry in earlier:
        merged = target(key)
        if merged in groups:
            history.setdefault(merged, []).append(entry)

    entries: Dict[str, Optional[Dict[str, Any]]] = {}
    for merged, keys in groups.items():
        current = can_dos.get(merged)
        olds = [can_dos[key] for key in keys if key != merged]
        base: Optional[Dict[str, Any]] = None
        if isinstance(current, dict):
            base = current
        else:
            readable = [entry for entry in olds if isinstance(entry, dict)]
            if readable:
                base = max(readable, key=_xp)
        if base is None:
            entries[merged] = None  # nothing readable to move; left as it is
            continue
        entry = dict(base)
        best: Optional[Tuple[datetime, Dict[str, Any]]] = None
        best_from_current = False
        # The current merged entry first, so a tie keeps it.
        for index, candidate in enumerate([current, *olds, *history.get(merged, [])]):
            found = _completion(candidate)
            if found is not None and (best is None or found[0] < best[0]):
                best, best_from_current = found, index == 0
        if best is not None:
            fields = best[1]
            entry[DATE_KEY] = fields[DATE_KEY]
            if fields[THRESHOLD_KEY] is None:
                entry.pop(THRESHOLD_KEY, None)
            else:
                entry[THRESHOLD_KEY] = fields[THRESHOLD_KEY]
            if fields[REPLAYED_KEY]:
                entry[REPLAYED_KEY] = True
            else:
                entry.pop(REPLAYED_KEY, None)
            if not best_from_current:
                report["completions_kept"] += 1
        entries[merged] = entry

    out: Dict[str, Any] = {}
    for key, value in can_dos.items():
        merged = target(key)
        if merged is None or entries[merged] is None:
            out[key] = value
        elif merged not in out:
            out[merged] = entries[merged]
    report["keys_moved"] = sum(
        1 for key in can_dos if key in can_do_map and entries[can_do_map[key]]
    )
    if out == can_dos:
        return Rewrite(None, {"keys_moved": 0, "completions_kept": 0})
    record["can_dos"] = out
    return Rewrite(record, report)


class ProgressRequest(NamedTuple):
    can_do_map: Dict[str, str]
    dry_run: bool
    # The last (analytics room, course room) the previous page read.
    after: Tuple[str, str]
    limit: int


def parse_request(body: Any) -> Tuple[Optional[ProgressRequest], Optional[str]]:
    """``(request, None)`` or ``(None, error)``."""
    if not isinstance(body, dict):
        return None, "Request body must be a JSON object"
    can_do_map, error = parse_can_do_map(body.get("can_do_map"))
    if can_do_map is None:
        return None, error
    dry_run = body.get("dry_run", False)
    if not isinstance(dry_run, bool):
        return None, "'dry_run' must be a boolean"
    after = body.get("after")
    if after is None:
        after = ["", ""]
    if (
        not isinstance(after, list)
        or len(after) != 2
        or not all(isinstance(part, str) for part in after)
    ):
        return None, "'after' must be the 'next' of the previous page"
    limit = body.get("limit", DEFAULT_LIMIT)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 0 < limit <= MAX_LIMIT
    ):
        return None, f"'limit' must be from 1 to {MAX_LIMIT}"
    return ProgressRequest(can_do_map, dry_run, (after[0], after[1]), limit), None


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

# Every progress record in an analytics room, in the primary key's own order
# with the type fixed, so a page stops at its LIMIT.
_RECORDS_SQL = """
    SELECT cse.room_id, cse.state_key
    FROM current_state_events cse
    JOIN current_state_events cc
        ON cc.room_id = cse.room_id
        AND cc.type = 'm.room.create' AND cc.state_key = ''
    JOIN event_json cj ON cj.event_id = cc.event_id
    WHERE cse.type = ?
        AND (cse.room_id > ? OR (cse.room_id = ? AND cse.state_key > ?))
        AND cj.json::jsonb -> 'content' ->> 'type' = ?
    ORDER BY cse.room_id, cse.state_key
    LIMIT ?
"""

# The dated entries, under the given Can-Do ids, of every version of one
# record the learner sent: rejected and redacted events left out.
_EARLIER_SQL = """
    SELECT DISTINCT c.key, c.value ->> 'first_completed_at',
        c.value -> 'xp_to_complete_at_first_completion',
        c.value -> 'completed_before_tracking'
    FROM events e
    JOIN event_json ej ON ej.event_id = e.event_id
    CROSS JOIN LATERAL jsonb_each(
        CASE WHEN jsonb_typeof(ej.json::jsonb -> 'content' -> 'can_dos') = 'object'
            THEN ej.json::jsonb -> 'content' -> 'can_dos'
            ELSE '{}'::jsonb END
    ) AS c
    WHERE e.room_id = ? AND e.type = ? AND e.state_key = ? AND e.sender = ?
        AND NOT e.outlier AND e.rejection_reason IS NULL
        AND NOT EXISTS (SELECT 1 FROM redactions r WHERE r.redacts = e.event_id)
        AND c.key = ANY(?)
        AND jsonb_typeof(c.value) = 'object'
        AND c.value ->> 'first_completed_at' IS NOT NULL
"""


class MigrateCourseProgress(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi):
        super().__init__()
        self._api = api
        self._auth = api._hs.get_auth()
        self._clock = api._hs.get_clock()
        self._store = api._hs.get_datastores().main

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._async_render_POST, request)
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest) -> None:
        try:
            requester = await self._auth.get_user_by_req(request)
        # silent-ok: the caller's auth failure, answered 401 (logged at INFO)
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ) as e:
            logger.info("Authentication failed: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
            return
        try:
            operator = requester.user.to_string()
            if not await self._api.is_user_admin(operator):
                respond_with_json(
                    request,
                    403,
                    {"error": "Forbidden: server admin required"},
                    send_cors=True,
                )
                return
            req, error = parse_request(await extract_body_json(request))
            if req is None:
                respond_with_json(request, 400, {"error": error}, send_cors=True)
                return
            answer = await self.page(req, operator)
            respond_with_json(request, 200, answer, send_cors=True)
        except Exception:
            logger.exception("Error migrating course progress records")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    async def page(self, req: ProgressRequest, operator: str) -> Dict[str, Any]:
        """One page of records, each planned, and written unless a dry run."""
        room_after, course_after = req.after
        rows = await self._store.db_pool.execute(
            "migrate_course_progress_records",
            _RECORDS_SQL,
            COURSE_PROGRESS_STATE_EVENT_TYPE,
            room_after,
            room_after,
            course_after,
            ANALYTICS_ROOM_TYPE,
            req.limit,
        )
        merged_from: Dict[str, List[str]] = {}
        for old, new in req.can_do_map.items():
            merged_from.setdefault(new, []).append(old)
        records = []
        for room_id, course_id in rows:
            records.append(await self._record(room_id, course_id, req, merged_from))
        written = sum(1 for record in records if record["changed"])
        logger.info(
            "migrate_course_progress: operator=%s dry_run=%s records=%d "
            "to_rewrite=%d written=%d keys_moved=%d completions_kept=%d",
            operator,
            req.dry_run,
            len(records),
            sum(1 for record in records if record["outcome"] == "rewrite"),
            written,
            sum(record["keys_moved"] for record in records),
            sum(record["completions_kept"] for record in records),
        )
        return {
            "dry_run": req.dry_run,
            "records": records,
            "next": list(rows[-1]) if len(rows) == req.limit else None,
        }

    async def _record(
        self,
        room_id: str,
        course_id: str,
        req: ProgressRequest,
        merged_from: Mapping[str, Sequence[str]],
    ) -> Dict[str, Any]:
        state = await self._api.get_room_state(
            room_id,
            [(COURSE_PROGRESS_STATE_EVENT_TYPE, course_id), (EventTypes.Create, "")],
        )
        event = state.get((COURSE_PROGRESS_STATE_EVENT_TYPE, course_id))
        create = state.get((EventTypes.Create, ""))
        learner = create.sender if create is not None else None
        answer: Dict[str, Any] = {
            "room_id": room_id,
            "course_id": course_id,
            "user_id": learner,
            "outcome": "unchanged",
            "keys_moved": 0,
            "completions_kept": 0,
            "changed": False,
        }
        if event is None or learner is None:
            return answer
        if event.sender != learner:
            # The dashboard counts only the learner's own record; sent again
            # by the learner, this one would start to count.
            answer["outcome"] = "not_learners_own"
            return answer

        content = unfreeze(event.content)
        can_dos = content.get("can_dos") if isinstance(content, dict) else None
        if not isinstance(can_dos, dict):
            return answer
        merged_ids = {
            req.can_do_map.get(key, key)
            for key in can_dos
            if key in req.can_do_map or key in merged_from
        }
        if not merged_ids:
            return answer
        keys = sorted(
            {*merged_ids, *(old for new in merged_ids for old in merged_from[new])}
        )
        earlier = [
            (key, {DATE_KEY: at, THRESHOLD_KEY: threshold, REPLAYED_KEY: replayed})
            for key, at, threshold, replayed in await self._store.db_pool.execute(
                "migrate_course_progress_earlier",
                _EARLIER_SQL,
                room_id,
                COURSE_PROGRESS_STATE_EVENT_TYPE,
                course_id,
                learner,
                keys,
            )
        ]
        rewrite = merge_progress(content, earlier, req.can_do_map)
        answer.update(rewrite.report)
        if rewrite.content is None:
            return answer
        if not self._api.is_mine(learner):
            answer["outcome"] = "not_local"
            return answer
        info = await self._api.get_userinfo_by_id(learner)
        if info is None or info.is_deactivated:
            answer["outcome"] = "deactivated"
            return answer
        answer["outcome"] = "rewrite"
        if req.dry_run:
            return answer
        try:
            await self._api.create_and_send_event_into_room(
                {
                    "type": COURSE_PROGRESS_STATE_EVENT_TYPE,
                    "state_key": course_id,
                    "room_id": room_id,
                    "sender": learner,
                    "content": rewrite.content,
                }
            )
        except AuthError as e:
            # The learner left their analytics room, or lost the power to
            # write it, since the record was read.
            logger.warning(
                "migrate_course_progress: %s refused the record for %s sent as "
                "%s: %s",
                room_id,
                course_id,
                learner,
                e.errcode,
            )
            answer["outcome"] = "refused"
            return answer
        except Exception:
            logger.exception(
                "migrate_course_progress: writing the record for %s in %s failed",
                course_id,
                room_id,
            )
            answer["outcome"] = "failed"
            return answer
        answer["changed"] = True
        await self._clock.sleep(SEND_PAUSE)
        return answer
