---
applyTo: "synapse_pangea_chat/public_courses/**,synapse_pangea_chat/__init__.py,tests/test_room_preview_e2e.py,tests/staging_tests/staging_tests.py"
---

# Public Courses

The public course catalog behind Browse — where a learner finds a course to join.

## Route contract

- Canonical: `GET /_synapse/client/pangea/v1/public_courses`
- Compatibility alias: `GET /_synapse/client/unstable/org.pangea/public_courses`
- Both routes serve the same resource and return identical payloads. New callers use
  the canonical route.
- Matrix bearer auth. Rate limiting is shared across both routes.
- A malformed cursor, or a filter value that cannot be honored, is refused with
  `400 M_INVALID_PARAM` — never served as an empty page or as unfiltered
  results. Both would answer a question the caller did not ask, with nothing in
  the response to say so.

## What appears

**INVARIANT.** A room appears in the catalog if, and only if, it is published in the
public room directory and has a current `pangea.course_plan` state event carrying a
plan id.

Nothing else is checked: not the member count, the join rule or the quest's contents, so a quest with zero missions appears. Synapse cannot see quest content and must not be taught to; holding the rule to Matrix state keeps it one rule, applied the same way with or without a filter.

Only current room state counts, so a room that once had a course plan and no longer does is not a course.

The id is a `quest-plans` row id — that is what a course space pins (see the
cross-repo [quests and learning objectives](../../../.github/.github/instructions/quests-and-learning-objectives.instructions.md)
spec). Anything resolving a plan id against the CMS asks `quest-plans` first; the
retired v1 `course-plans` collection is only a fallback for rooms old enough to
reference it.

## Language

A course's target language is written into its `pangea.course_plan` event as `l2`
when the quest is attached. The CMS stays authoritative for everything else about a
plan; `l2` is the single field Matrix carries, because it is the only filter Browse
applies and it does not change over a course's life.

`target_language` matches on base language: `es` matches `es`, `es-ES`, and `es-MX`,
and the reverse. Exact matching drops regionally-tagged courses and empties the list
(issue #53).

A room with no `l2` appears when no language filter is passed, and is excluded when
one is.

There are no `language_of_instructions` or `cefr_level` filters. They had no caller,
and serving them would put a CMS lookup back on the read path. A client that wants a
course's L1 or level reads them from the plan it already fetches to render the card.

## Central courses

A central course is the one Pangea-run course per language for learners who aren't in a teacher's course ([central courses](../../../.github/.github/instructions/central-courses.instructions.md)). The catalog marks which courses are central, so the client can send a learner to one without a second lookup.

- **The mark.** A course is central when its current `pangea.course_settings` state event carries `"central": true`. Every course in the response has a `central` field, which is `false` for all other courses.
- **Only a Pangea admin can make a course central.** The mark counts only when the `pangea.course_settings` event was sent by an account listed in `central_course_admin_user_ids`, which defaults to `@support:<server name>`. Any course admin can write that event, so without this check a teacher could mark their course central and receive every learner the client sends there. A mark from anyone else is ignored, not refused: the course is reported as not central.
- **The catalog doesn't enforce one central course per language.** If two courses in the same language are marked, both are reported as central. Only Pangea admins can set the mark, so a second one is an operations mistake to fix in room state, and the server doesn't guess which course was meant.
- **Whether a course appears doesn't change.** The mark only adds a field.

## Pagination

Filtering happens before pagination, so a page is full unless the catalog is exhausted, and a non-null `next_batch` means more results genuinely exist; a caller never has to guess whether a thin page means the end.

A page holds at most 100 courses; a larger `limit` gets 100 and a cursor, because each page reads every listed room's state.

## Trips

Each course carries its [trip](course-trip.instructions.md): `course_missions` and `course_position` are the content of those state events, or null. Nothing is derived here, so a public ship is placed by the client's one rule for ships. The [world view](../../../.github/.github/instructions/world-view.instructions.md) reads them for ships of courses the learner is not in.

- **Read as the app reads them.** Only the event under the empty state key counts, and only when its sender is a course admin now; otherwise the field is null, as the app ignores it inside the course.
- **Only the public part.** The copy keeps what places a ship and dates its route: its version, revision, time zone and start date, and each Can-Do's id, activities, due date and city. A teacher's notes and the Can-Dos and activities they removed stay with members, because a listed course may be one a learner must ask to join. A field is public only once named in the code.

## Who runs a course

`admins` lists each course admin still in the course who is not a bot, with the name they show there. The client says Pangea for a central course. `institution` is the school the course names, `{name, id}` from its own `pangea.course_institution` event, read as the trip is; `id` is a ROR or NCES id when one was picked, as on a course request.

## When the CMS is unavailable

Browse is unaffected — there is no CMS call on the read path. There is also no
fall-back path for a filter that cannot be served: falling back would return rooms
the eligibility rule excludes, and a wrong catalog is worse than a smaller one.

## Compatibility

Keep the unstable alias until all known callers and tests are migrated. When the
canonical route changes, update operator tooling, bot callers, tests, and README
references in the same session.
