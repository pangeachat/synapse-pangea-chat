---
applyTo: "synapse_pangea_chat/email_invite/create_course_space.py,synapse_pangea_chat/room_code/knock_with_code.py,synapse_pangea_chat/room_code/get_rooms_with_access_code.py"
---

# Create Course Space — Synapse Module

Cross-repo design: [course-request.instructions.md](../../../.github/.github/instructions/teacher-funnel.instructions.md)

## Endpoint

`POST /_synapse/client/pangea/v1/create_course_space`

Lives in the `email_invite/` sub-package alongside `invite_by_email`.

### Contract

- **Auth**: Bearer token (bot user). Choreo logs in with bot credentials from AWS Secrets Manager.
- **Input**: Course plan ID, title, description, image URL, teacher email, target language, optional extra email template vars. Endpoint does **not** fetch from CMS — all details passed in body.
  - **Target language must be an IETF code** (`es`), never a display name (`Spanish`). It is stored as `l2` on the space's course-plan state event and the public course catalog matches it by base language, so a display name matches nothing — and it is sticky, because the one-time `l2` backfill treats any non-empty value as already correct. It is optional: a space created without it is valid and gets repaired by that backfill, which is the only reason omitting it is safe. See [public-courses](public-courses.instructions.md).
- **Output**: Room ID, the course's access code, and the join URL built from it (same format the client already uses for class links — see [joining-courses](../../../client/.github/instructions/joining-courses.instructions.md) Route 1). **One code and one link**: the link the teacher is sent is the link they later give their class. The teacher's own first join claims the course; everyone else joins as an ordinary member. The rule, and why the claim is gated on identity rather than on arriving first, is owned by [knock-with-code](knock-with-code.instructions.md) ("Claiming a course").

### What it does

1. Creates a private Matrix space with knock join rules, a course plan state event, and the power levels the client gives a course space it creates itself ([`defaultSpacePowerLevelsContent`](../../../client/lib/pangea/common/constants/default_power_level.dart), with `m.space.child` at 0). Every new space defaults `m.space.child` to 0: a regular member must be able to attach a room, because learners' activity sessions fan out into their courses as space children ([activities.instructions.md](../../../client/.github/instructions/activities.instructions.md)). The space also requires instructor analytics access to join, as a course created in the client does ([course-analytics-access](../../../.github/.github/instructions/course-analytics-access.instructions.md)).
2. Generates the course's access code and records the teacher it was created for, in join rules directly (bypasses `request_room_code`)
3. Uploads course image as room avatar if provided
4. Sends the teacher their invitation, carrying that one code

## Who the course is created for

The endpoint is told which teacher the course is being created for, and that identity is what the claim is later matched against. It is recorded on the space at creation, because the teacher will not be a member until they follow the link, so there is nothing else to match them by at claim time.

A course created without a recorded teacher identity is valid, and nobody is auto-promoted in it: it stays bot-administered until a course admin is granted deliberately. That is the same fail-closed outcome as an identity that never matches, and for the same reason — a course with no human admin is repairable, a course held by the wrong person is not.

Claim behaviour, the identity gate, and the separate deliberate-grant path are owned by [knock-with-code](knock-with-code.instructions.md) ("Claiming a course"); this endpoint only supplies the identity and the code.

### Unauthenticated teachers

If the teacher doesn't have a Pangea account, the client handles this: the class code is cached to disk, the user is prompted to sign up, and after account creation the cached code auto-joins. No special handling needed here — see [joining-courses](../../../client/.github/instructions/joining-courses.instructions.md) "Pre-login persistence."

## Dependencies

- **`invite_by_email`**: Separate session, in progress. Use placeholder for now.
- **Email templates**: Jinja2 in [synapse-templates](../../../synapse-templates/) repo, rendered by Synapse's built-in email handler — same as registration emails.

## Future Work

- A teacher-facing way to grant course admin to a co-teacher, so a second teacher does not need an operator — issue TBD
- Automated pipeline trigger (webhook from CMS on status change) — issue TBD
