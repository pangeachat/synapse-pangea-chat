---
applyTo: "synapse_pangea_chat/course_trip/**"
description: "The homeserver's part of a course as a trip: slicing an existing course into one city per Can-Do, and placing an undated course's ship at its busiest stop."
---

# Course trip (Synapse module)

A course is a trip through cities, one per Can-Do (org [quests and learning objectives](../../../.github/.github/instructions/quests-and-learning-objectives.instructions.md), "A course is a trip" and "Progression"; plan of record pangeachat/.github#780). The course's own copy, `pangea.course_missions` version 2, carries each Can-Do's pins and their city (name and position in integer microdegrees, since Matrix event content has no decimals), and a `start_date` on a dated course. This module slices courses into cities and places the ship of a course that runs by progress.

## The experience

1. An operator slices every existing course, one at a time after a dry run; its Missions then show cities and its ship sits at a city, with no teacher action.
2. A learner in an undated course sees the ship where most active classmates are, sailing on when that changes.
3. A teacher sees, per course, which Can-Dos are viable, thin or empty: what generation has to fill.

Re-decider for every line: Will, unless named.

## The city rule

- A point belongs to the nearest listed city within 250 miles and to no city beyond that; the list is the CMS `cities` collection, read with the module's service key and cached for an hour. The same rule as admin-dash-api, checked against the same example set (`tests/fixtures/nearest_city.json`).

## Slicing a course (`POST /_synapse/client/pangea/v1/slice_course`)

- Server admins only; one space per call, with a dry run; the operator script loops over spaces.
- Slicing narrows a course's own list to one city per Can-Do and never adds to it: the city is the listed city most of its listed activities fall under, ties to the first; its pins are those under that city plus any with no known position. One left out joins the Can-Do's removed activities, so work done on it keeps counting. A course with no copy slices its quest's library. A Can-Do with no activity under a listed city keeps every activity and has no city.
- The copy is written as version 2 at the existing revision plus one, keeping its order, notes, due dates, removals, time zone and any start date. A copy published while the slice was built wins; a version 2 copy is never sliced again unless forced.
- It sends as the space's strongest local instructor at power level 100, as set-course-plan does: no token, no join, nobody promoted. A space with no local instructor is refused and reported. The operator is logged, since room state shows the teacher as sender.
- The response reports each Can-Do as viable (three pinned or more), thin (one or two) or empty: the generation backlog, each generated at the Can-Do's strongest city.
- With `course_missions_v2_enabled` on, every new course gets the trip at creation: create-course-space and set-course-plan slice their copy, and a copy an app writes as version 1 is sliced here as it lands, so no app slices. It stays off until the app's version 2 reader has shipped, because an older app treats a version it does not know as no copy.

## The ship on an undated course (`pangea.course_position`)

- Only an undated version 2 copy gets a position; a dated course's ship derives from its dates in the app.
- Each member's position lives in `pangea.course_progress` in their own analytics room, readable by them and teachers with analytics access, so no client can count the others. The scheduled pass (`course_position_enabled`, every 15 minutes by default) reads them and writes only the busiest stop into the course space: `{stop, from_stop, since, revision}`, indexes into that revision's `missions[]`, never counts and never anyone's position.
- Active members only: a member counts when their progress record for the course was written within the last seven days, the dashboard's "active this week" count; a default to tune, not a new definition. Each is counted at their first incomplete Mission; the earlier stop wins a tie; a course with no active member sits at its first stop.
- The ship sails from the stop it is at to the busiest one; a Mission with no city does not move it; the first write, and the first after the copy's revision changes, has `from_stop` equal to `stop`. Nothing is written when the stop has not changed.
- The write is sent as the strongest local instructor, as the slice is; a space with no local instructor is skipped and logged. `POST /_synapse/client/pangea/v1/course_position` runs one course now for an operator, with a dry run.

## Open questions

- Whether "active" should read the analytics room's last message instead of the progress record's write time, if the two drift apart.
