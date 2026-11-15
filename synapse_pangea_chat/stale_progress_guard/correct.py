"""Pure decisions for the stale course progress guard.

Design: stale-progress-guard.instructions.md. An app that loaded a course's
outline before its Can-Dos were merged writes the learner's
``pangea.course_progress`` with retired Can-Do ids and no completion dates.
The guard corrects such a write against the record already stored, so it
never undoes the migration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

from synapse.util.frozenutils import unfreeze

from synapse_pangea_chat.migrate_course_can_dos.course_progress import (
    COMPLETED_KEY,
    DATE_KEY,
    REPLAYED_KEY,
    THRESHOLD_KEY,
    _completion,
    _xp,
)

COURSE_PROGRESS = "pangea.course_progress"
COURSE_MISSIONS = "pangea.course_missions"
TEACHER_MODE = "pangea.teacher_mode"
PINS_KEY = "pinned_activities_by_objective"


class Correction(NamedTuple):
    # The content to store in place of the write's, or None to store it as sent.
    content: Optional[Dict[str, Any]]
    report: Dict[str, int]


_NONE = Correction(None, {})


def correct_record(
    incoming: Mapping[str, Any],
    stored: Optional[Mapping[str, Any]],
    retired: Mapping[str, str],
) -> Correction:
    """The record to store for a write that names a retired Can-Do id.

    A write naming none is stored as sent. Otherwise every Can-Do is keyed by
    its merged id and:

    - its figures come from the write's own entry under the merged id, else
      the stored entry under it, else the old entry (stored or written) with
      the most XP;
    - its first completion is the earliest found in any of them, with the
      threshold and replay mark stored beside that date;
    - it is completed whenever any of them was or a date is kept;
    - a Can-Do the stored record holds and the write left out is kept.

    The rest of the record (when and by which version it was computed, the
    XP that completes a Can-Do, fields this module does not know) is the
    write's.
    """
    record: Dict[str, Any] = unfreeze(incoming)
    written = record.get("can_dos") if isinstance(record, dict) else None
    if not isinstance(written, dict) or not any(key in retired for key in written):
        return _NONE
    stored_can_dos: Dict[str, Any] = {}
    if isinstance(stored, Mapping):
        candidate = unfreeze(stored).get("can_dos")
        if isinstance(candidate, dict):
            stored_can_dos = candidate

    def target(key: str) -> str:
        return retired.get(key, key)

    # Per merged id: the write's entry under it, the stored entry under it,
    # then the old entries, stored first.
    fresh: Dict[str, Any] = {}
    kept: Dict[str, Any] = {}
    olds: Dict[str, List[Any]] = {}
    order: List[str] = []

    def place(key: str) -> None:
        if key not in order:
            order.append(key)

    for key, entry in stored_can_dos.items():
        merged = target(key)
        place(merged)
        if key == merged:
            kept[merged] = entry
        else:
            olds.setdefault(merged, []).append(entry)
    written_targets = set()
    for key, entry in written.items():
        merged = target(key)
        written_targets.add(merged)
        place(merged)
        if key == merged:
            fresh[merged] = entry
        else:
            olds.setdefault(merged, []).append(entry)

    report = {
        "retired_keys": sum(1 for key in written if key in retired),
        "kept_from_stored": sum(1 for key in order if key not in written_targets),
        "completions_kept": 0,
    }
    out: Dict[str, Any] = {}
    for merged in order:
        fresh_entry = fresh.get(merged)
        kept_entry = kept.get(merged)
        readable_olds: List[Dict[str, Any]] = [
            e for e in olds.get(merged, []) if isinstance(e, dict)
        ]
        candidates: List[Mapping[str, Any]] = [
            *(e for e in (fresh_entry, kept_entry) if isinstance(e, dict)),
            *readable_olds,
        ]
        if not candidates:
            # Nothing readable to move: an unreadable entry stays where it was.
            for source in (fresh, kept):
                if merged in source:
                    out[merged] = source[merged]
            continue
        base: Dict[str, Any]
        if isinstance(fresh_entry, dict):
            base = fresh_entry
        elif isinstance(kept_entry, dict):
            base = kept_entry
        else:
            base = max(readable_olds, key=_xp)
        entry, kept_date = _with_completion(dict(base), base, candidates)
        if kept_date:
            report["completions_kept"] += 1
        out[merged] = entry

    record["can_dos"] = out
    return Correction(record, report)


def _with_completion(
    entry: Dict[str, Any], base: Mapping[str, Any], candidates: List[Mapping[str, Any]]
) -> Tuple[Dict[str, Any], bool]:
    """``entry`` with the earliest first completion among ``candidates`` and
    completed whenever any was; and whether the date came from an entry other
    than ``base``."""
    best: Optional[Tuple[datetime, Dict[str, Any]]] = None
    best_is_base = False
    # ``base`` first, so a tie keeps its own date.
    ordered = [base, *(c for c in candidates if c is not base)]
    for candidate in ordered:
        found = _completion(candidate)
        if found is not None and (best is None or found[0] < best[0]):
            best, best_is_base = found, candidate is base
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
    completed = [c for c in ordered if c.get(COMPLETED_KEY) is True]
    if completed or best is not None:
        entry[COMPLETED_KEY] = True
        if best is None and any(c.get(REPLAYED_KEY) is True for c in completed):
            entry[REPLAYED_KEY] = True
    return entry, best is not None and not best_is_base


def retired_ids_in_course_state(
    event_type: str, content: Mapping[str, Any], retired: Mapping[str, str]
) -> int:
    """How many retired Can-Do ids a course list or pins event names."""
    if event_type == COURSE_MISSIONS:
        count = 0
        for key in ("missions", "removed_missions"):
            entries = content.get(key)
            if isinstance(entries, (list, tuple)):
                count += sum(
                    1
                    for entry in entries
                    if isinstance(entry, Mapping) and entry.get("lo_id") in retired
                )
        return count
    if event_type == TEACHER_MODE:
        pins = content.get(PINS_KEY)
        if isinstance(pins, Mapping):
            return sum(1 for key in pins if key in retired)
    return 0
