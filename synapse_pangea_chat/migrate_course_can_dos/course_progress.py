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

import json
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
from synapse_pangea_chat.migrate_course_can_dos.report import report, report_failure
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.migrate_course_can_dos.course_progress"
)

COURSE_PROGRESS_STATE_EVENT_TYPE = "pangea.course_progress"
COMPLETED_KEY = "completed"
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


def _date_unreadable(entry: Any) -> bool:
    """An entry whose date or threshold is set but could not be read: no
    completion is taken from it, and the count says so."""
    if not isinstance(entry, Mapping):
        return False
    at = entry.get(DATE_KEY)
    threshold = entry.get(THRESHOLD_KEY)
    return (at is not None and _instant(at) is None) or not (
        threshold is None
        or (isinstance(threshold, int) and not isinstance(threshold, bool))
    )


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


def _completed(entry: Any) -> bool:
    """Any sign the entry was complete: the flag, or a date."""
    return isinstance(entry, Mapping) and (
        entry.get(COMPLETED_KEY) is True or _completion(entry) is not None
    )


def _xp(entry: Mapping[str, Any]) -> int:
    xp = entry.get("xp")
    return xp if isinstance(xp, int) and not isinstance(xp, bool) else 0


def empty_report() -> Dict[str, Any]:
    return {
        "keys_moved": 0,
        "completions_kept": 0,
        "completions_restored": 0,
        "old_keys_left": 0,
        "dates_unreadable": 0,
    }


def merge_progress(
    content: Mapping[str, Any],
    earlier: Iterable[Tuple[str, Mapping[str, Any]]],
    can_do_map: Mapping[str, str],
) -> Rewrite:
    """The record with each old Can-Do key moved to its merged Can-Do.

    ``earlier`` holds ``(Can-Do id, entry)`` from the learner's earlier
    versions of this record. A merged Can-Do is written when the current
    record names it, or an old id of it, or when an earlier version holds a
    completion of it (dated, or flagged ``completed``): a completion is never
    lost because the app left its entry out. Its figures stay as the current
    record has them under the merged id, else come from the old entry with
    the most XP in the record, else from the earlier entry with the most XP.
    Its first completion is the earliest found anywhere, with the threshold
    and replay mark stored beside it. It is completed whenever any entry it
    stands for, now or earlier, was; complete with no date found anywhere, it
    gets the replay mark, so the app dates it from the learner's history
    rather than the migration making one up. Everything else in the record is
    left as it is.
    """
    report = empty_report()
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
    history: Dict[str, List[Any]] = {}
    for key, entry in earlier:
        merged = target(key)
        if merged is not None:
            history.setdefault(merged, []).append(entry)
    # Merged Can-Dos the record no longer names at all, whose completion only
    # an earlier version holds, in the order history first shows them.
    restored = [
        merged
        for merged, entries in history.items()
        if merged not in groups and any(_completed(e) for e in entries)
    ]
    for merged in restored:
        groups[merged] = []
    if not groups:
        return Rewrite(None, report)

    entries: Dict[str, Optional[Dict[str, Any]]] = {}
    for merged, keys in groups.items():
        current = can_dos.get(merged)
        olds = [can_dos[key] for key in keys if key != merged]
        past = history.get(merged, [])
        report["dates_unreadable"] += sum(
            1 for entry in [current, *olds] if _date_unreadable(entry)
        )
        base: Optional[Dict[str, Any]] = None
        if isinstance(current, dict):
            base = current
        else:
            readable: List[Mapping[str, Any]] = [
                entry for entry in olds if isinstance(entry, dict)
            ]
            if not readable and merged in restored:
                readable = [entry for entry in past if isinstance(entry, Mapping)]
            if readable:
                most = max(readable, key=_xp)
                base = dict(most)
        if base is None:
            entries[merged] = None  # nothing readable to move; left as it is
            continue
        entry = dict(base)
        best: Optional[Tuple[datetime, Dict[str, Any]]] = None
        best_from_current = False
        # The current merged entry first, so a tie keeps it.
        for index, candidate in enumerate([current, *olds, *past]):
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
        # Completed whenever any entry it stands for was: the figures can come
        # from an old entry that was still in progress, and the record must
        # never say "not completed" beside a first completion. The app writes
        # a dated entry as completed too (a completed Mission stays complete),
        # so a kept date always means completed.
        completed_elsewhere = any(_completed(e) for e in [*olds, *past])
        if completed_elsewhere or best is not None:
            entry[COMPLETED_KEY] = True
            # Complete with no date found anywhere: the replay mark, so the app
            # dates it from history (CanDoProgress.awaitsReplay) instead of
            # dating it the day it next writes. The current entry's own state
            # is left as the app wrote it.
            if best is None and completed_elsewhere:
                entry.setdefault(DATE_KEY, None)
                entry[REPLAYED_KEY] = True
        entries[merged] = entry

    out: Dict[str, Any] = {}
    for key, value in can_dos.items():
        merged = target(key)
        if merged is None or entries[merged] is None:
            out[key] = value
        elif merged not in out:
            out[merged] = entries[merged]
    for merged in restored:
        if entries[merged] is not None:
            out[merged] = entries[merged]
            report["completions_restored"] += 1
    report["keys_moved"] = sum(
        1 for key in can_dos if key in can_do_map and entries[can_do_map[key]]
    )
    report["old_keys_left"] = sum(1 for key in out if key in can_do_map)
    if out == can_dos:
        return Rewrite(
            None,
            {
                **empty_report(),
                "old_keys_left": report["old_keys_left"],
                "dates_unreadable": report["dates_unreadable"],
            },
        )
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

# Every entry, under the given Can-Do ids, of every version of one record the
# learner sent: rejected and redacted events left out. Incomplete ones too: a
# Can-Do restored from history takes the figures of its entry with the most XP,
# whether or not that one was complete; merge_progress decides whether any
# completion warrants the restore.
_EARLIER_SQL = """
    SELECT DISTINCT c.key, c.value::text
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
"""

# Outcomes that leave a record holding old ids or missing a completion: the
# nightly runner fails over a limit of them. A deactivated learner's record is
# counted on its own instead: its old ids stay in a room no app or account can
# read or write (coordinator, 2026-11-15).
UNRESOLVED_OUTCOMES = (
    "refused",
    "not_local",
    "not_learners_own",
    "failed",
)


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
        except Exception as e:
            report_failure("migrate_course_progress: the page failed", e)
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
        # Every old and merged id: history is read for each record, since a
        # completion can sit under an id the current record no longer names.
        history_keys = sorted({*req.can_do_map, *req.can_do_map.values()})
        records = []
        for room_id, course_id in rows:
            records.append(await self._record(room_id, course_id, req, history_keys))
        counts = page_counts(records)
        logger.info(
            "migrate_course_progress: operator=%s dry_run=%s %s",
            operator,
            req.dry_run,
            " ".join(f"{key}={value}" for key, value in counts.items()),
        )
        left = {
            key: value
            for key, value in counts.items()
            if value
            and key
            in (*UNRESOLVED_OUTCOMES, "unreadable", "old_keys_left", "dates_unreadable")
        }
        if left and not req.dry_run:
            # Failed sends are reported one by one; this is the page's count of
            # everything left unmoved, so it reaches the team, not only the
            # operator's report.
            report(
                "migrate_course_progress: records left unmoved",
                after=f"{room_after}|{course_after}",
                **left,
            )
        return {
            "dry_run": req.dry_run,
            "records": records,
            "counts": counts,
            "next": list(rows[-1]) if len(rows) == req.limit else None,
        }

    async def _record(
        self,
        room_id: str,
        course_id: str,
        req: ProgressRequest,
        history_keys: Sequence[str],
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
            **empty_report(),
            "changed": False,
        }
        content = unfreeze(event.content) if event is not None else None
        if (
            learner is None
            or not isinstance(content, dict)
            or not isinstance(content.get("can_dos"), dict)
        ):
            # Neither the app nor the dashboard reads it as a record.
            answer["outcome"] = "unreadable"
            return answer
        assert event is not None
        if event.sender != learner:
            # The dashboard counts only the learner's own record; sent again
            # by the learner, this one would start to count. Reported only
            # when it names old ids, the one case the migration leaves undone.
            mine = merge_progress(content, [], req.can_do_map)
            answer.update(mine.report)
            if mine.content is not None or mine.report["old_keys_left"]:
                answer["outcome"] = "not_learners_own"
            return answer

        earlier: List[Tuple[str, Mapping[str, Any]]] = []
        for key, raw in await self._store.db_pool.execute(
            "migrate_course_progress_earlier",
            _EARLIER_SQL,
            room_id,
            COURSE_PROGRESS_STATE_EVENT_TYPE,
            course_id,
            learner,
            list(history_keys),
        ):
            value = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(value, Mapping):
                earlier.append((key, value))
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
            report(
                "migrate_course_progress: the room refused the record",
                room_id=room_id,
                course_id=course_id,
                errcode=e.errcode,
            )
            answer["outcome"] = "refused"
            return answer
        except Exception as e:
            report_failure(
                "migrate_course_progress: writing the record failed",
                e,
                room_id=room_id,
                course_id=course_id,
            )
            answer["outcome"] = "failed"
            return answer
        answer["changed"] = True
        await self._clock.sleep(SEND_PAUSE)
        return answer


def page_counts(records: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """The page's records by outcome, and its summed counts."""
    counts: Dict[str, int] = {"records": len(records)}
    for outcome in (
        "rewrite",
        "unchanged",
        "unreadable",
        "deactivated",
        *UNRESOLVED_OUTCOMES,
    ):
        counts[outcome] = sum(1 for r in records if r["outcome"] == outcome)
    for key in empty_report():
        counts[key] = sum(r.get(key, 0) for r in records)
    counts["written"] = sum(1 for r in records if r.get("changed"))
    return counts
