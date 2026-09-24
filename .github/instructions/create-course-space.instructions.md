---
applyTo: "synapse_pangea_chat/email_invite/create_course_space.py,synapse_pangea_chat/room_code/knock_with_code.py,synapse_pangea_chat/room_code/get_rooms_with_access_code.py"
---

# Create Course Space — Synapse Module

Cross-repo design: [teacher-funnel.instructions.md](../../../.github/.github/instructions/teacher-funnel.instructions.md)

## Endpoint

`POST /_synapse/client/pangea/v1/create_course_space`

Lives in the `email_invite/` sub-package alongside `invite_by_email`.

### Contract

- **Auth**: Bearer token (bot user). Choreo logs in with bot credentials from AWS Secrets Manager.
- **Input**: Course plan ID, title, description, image URL, the requesting address (`teacher_email`, optional; see below), target language, and an optional one-line `request_summary` of what was asked for, which the first email quotes back. Endpoint does **not** fetch from CMS — all details passed in body.
  - **Target language must be an IETF code** (`es`), never a display name (`Spanish`). It is stored as `l2` on the space's course-plan state event and the public course catalog matches it by base language, so a display name matches nothing — and it is sticky, because the one-time `l2` backfill treats any non-empty value as already correct. It is optional: a space created without it is valid and gets repaired by that backfill, which is the only reason omitting it is safe. See [public-courses](public-courses.instructions.md).
- **Output**: Room ID, the class code, the single-use admin code, the admin join URL, and whether the first email went out (`emailed`) (same format the client already uses for class links — see [joining-courses](../../../client/.github/instructions/joining-courses.instructions.md) Route 1). Which code the teacher sees, and when, is owned by [knock-with-code](knock-with-code.instructions.md) ("Claiming a course").

### What it does

1. Creates a private Matrix space with knock join rules, a course plan state event, and the power levels the client gives a course space it creates itself ([`defaultSpacePowerLevelsContent`](../../../client/lib/pangea/common/constants/default_power_level.dart), with `m.space.child` at 0). Every new space defaults `m.space.child` to 0: a regular member must be able to attach a room, because learners' activity sessions fan out into their courses as space children ([activities.instructions.md](../../../client/.github/instructions/activities.instructions.md)). The space also requires instructor analytics access to join, as a course created in the client does ([course-analytics-access](../../../.github/.github/instructions/course-analytics-access.instructions.md)).
2. Generates the class code and a single-use admin code and sets both in join rules directly (bypasses `request_room_code`), and records the address the course was created for
3. Uploads course image as room avatar if provided
4. Sends the teacher the first email: their course is ready, with the admin link behind a button and no class code. The second email, carrying the class code, is sent when the course is claimed ([knock-with-code](knock-with-code.instructions.md))

## Who the course is created for

The endpoint is told the address the course was requested from and records it. That address is where the claim link goes, and where the claim notice goes once the course is claimed; the teacher's Pangea account is never matched against it. It is recorded because nobody from the course is a member until the claim, so at claim time there is nowhere else to find it.

The address is kept out of room state. Every member of a course can read its room state, so an address stored there would be visible to every student who joins.

A course created without an address sends no email and stays bot-administered until someone is granted admin deliberately.

Claim behaviour is owned by [knock-with-code](knock-with-code.instructions.md) ("Claiming a course"); this endpoint supplies the address and the codes.

### Unauthenticated teachers

If the teacher doesn't have a Pangea account, the client handles this: the code from the link is cached to disk, the user is prompted to sign up, and after account creation the cached code is submitted. For a teacher claiming a course that code is the admin code, so the claim goes through the same join flow as any other code. No special handling needed here — see [joining-courses](../../../client/.github/instructions/joining-courses.instructions.md) "Pre-login persistence."

## Dependencies

- **The two emails** are sent through Synapse's own mail path (the homeserver's `email` config), so they go out from its sender and stream. A failed first email is captured and answered as `emailed: false` rather than failing the request, because the space already exists and a retry would make a second one. The address is recorded before the first email is sent; if the record fails, no email goes out, since a claim with no record could not send the class link.
- **Email templates** ship inside the package (`email_invite/templates/`), as the nudge emails' do, and are read through the module API's template loader. They are not in [synapse-templates](../../../synapse-templates/): that repo is pinned to a tag per environment, so a template there would need a tag and an inventory bump in each environment before the module that sends it could load.

## Future Work

- A teacher-facing way to grant course admin to a co-teacher, so a second teacher does not need an operator — issue TBD
- Automated pipeline trigger (webhook from CMS on status change) — issue TBD
