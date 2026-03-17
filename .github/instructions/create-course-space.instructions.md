---
applyTo: "synapse_pangea_chat/email_invite/create_course_space.py,synapse_pangea_chat/room_code/knock_with_code.py,synapse_pangea_chat/room_code/get_rooms_with_access_code.py"
---

# Create Course Space — Synapse Module

Cross-repo design: [course-request.instructions.md](../../../.github/.github/instructions/course-request.instructions.md)

## Endpoint

`POST /_synapse/client/pangea/v1/create_course_space`

Lives in the `email_invite/` sub-package alongside `invite_by_email`.

### Contract

- **Auth**: Bearer token (bot user). Choreo logs in with bot credentials from AWS Secrets Manager.
- **Input**: Course plan ID, title, description, image URL, teacher email, optional extra email template vars. Endpoint does **not** fetch from CMS — all details passed in body.
- **Output**: Room ID, student access code, admin access code, admin join URL (same format the client already uses for class links — see [joining-courses](../../../client/.github/instructions/joining-courses.instructions.md) Route 1).

### What it does

1. Creates a private Matrix space with knock join rules, course plan state event, and power levels matching [client defaults](../../../client/lib/pangea/chat/constants/default_power_level.dart)
2. Generates two unique access codes (student + admin) and sets both in join rules directly (bypasses `request_room_code`)
3. Uploads course image as room avatar if provided
4. Sends branded invite email to teacher via `invite_by_email` with the admin access code

## Admin Access Code

Second code stored alongside the regular access code in join rules. Same format. Teacher uses it via the existing `knock_with_code` endpoint.

### knock_with_code extension

Extend the existing endpoint to check **both** code fields:

- **Student code match** → existing behavior (invite as regular member)
- **Admin code match** → invite + promote to admin + **burn the code** (remove from join rules state). Single-use by design.

Reuses existing validation, rate limiting, and invite mechanics. No new endpoint — the client already calls `knock_with_code` for all code-based joins.

### Unauthenticated teachers

If the teacher doesn't have a Pangea account, the client handles this: the class code is cached to disk, the user is prompted to sign up, and after account creation the cached code auto-joins. No special handling needed here — see [joining-courses](../../../client/.github/instructions/joining-courses.instructions.md) "Pre-login persistence."

## Dependencies

- **`invite_by_email`**: Separate session, in progress. Use placeholder for now.
- **Email templates**: Jinja2 in [synapse-templates](../../../synapse-templates/) repo, rendered by Synapse's built-in email handler — same as registration emails.

## Future Work

- Reusable admin codes (multiple admins per course) — issue TBD
- Automated pipeline trigger (webhook from CMS on status change) — issue TBD
