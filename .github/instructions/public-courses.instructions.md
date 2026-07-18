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

## What appears

**INVARIANT.** A room appears in the catalog if, and only if, it is published in the
public room directory and has a current `pangea.course_plan` state event carrying a
plan id.

Nothing else is checked — not the member count, not the join rule, and above all not
the contents of the quest. A quest with zero missions is a published course and
appears. Synapse cannot see quest content and must not be taught to: holding the rule
to Matrix state is what keeps it one rule, applied the same way whether or not a
filter is passed. Two code paths that each decided eligibility separately are how the
catalog came to disagree with itself.

The plan id is read from `uuid`, falling back to `course_plan_id` — spaces created
server-side write the latter. Only current room state counts, so a room that once had
a course plan and no longer does is not a course.

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

## Pagination

Filtering happens before pagination, so a page is full unless the catalog is
exhausted, and a non-null `next_batch` means more results genuinely exist. A caller
that receives a full page and a cursor can keep paging without guessing whether the
thin page it got means "no more courses" or "kept looking in the wrong place".

## When the CMS is unavailable

Browse is unaffected — there is no CMS call on the read path. There is also no
fall-back path for a filter that cannot be served: falling back would return rooms
the eligibility rule excludes, and a wrong catalog is worse than a smaller one.

## Compatibility

Keep the unstable alias until all known callers and tests are migrated. When the
canonical route changes, update operator tooling, bot callers, tests, and README
references in the same session.
