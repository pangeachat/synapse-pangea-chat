---
applyTo: "synapse_pangea_chat/email_invite/**"
---

# Create Course Space — Synapse Module

Cross-repo design: [teacher-funnel.instructions.md](../../../.github/.github/instructions/teacher-funnel.instructions.md)

## Deferred v2 preparation

A requested course is ready when its content and settings are prepared and its invitation is durable. Its Matrix space is created when the first instructor claims it. Unclaimed invitations do not depend on the conversational bot being online or remaining a room member.

The preparation record has a stable invitation ID and stores the quest reference, title, description, target language, optional image and request summary, requesting address, and private claim-code fingerprints. The room ID is absent until provisioning. Synapse receives the prepared details from the caller and does not fetch from or write to CMS. The requesting address and claim codes never appear in room state.

Keep the existing v1 creation endpoint unchanged for compatibility with callers that require an immediate room ID. Add a v2 preparation contract at the same endpoint name: an authorized server operator supplies a stable request key, requesting address, quest reference, title, target-language code and optional presentation fields. It returns the invitation ID, preparation status and email-delivery result, without raw codes or a placeholder room ID. The stable key is scoped to the creating service and identifies a requested course rather than an address. Repeating unchanged input returns the same invitation and its current status without resending; changed input under the same key is a conflict. A delivery whose outcome is uncertain remains explicitly uncertain until reconciled.

An authorized operator can read current status through `GET /_synapse/client/pangea/v2/course_invitations/{invitation_id}`. It returns preparation/provisioning/completion/revocation status, the room ID when known, the reserved claimant reference when present, and delivery-attempt references, timestamps and outcomes. Outcomes distinguish unsent, accepted by the mail transport, failed and uncertain; transport acceptance is not proof of inbox receipt. It never returns addresses or raw codes. Status reads have no send or provisioning side effects and are authoritative for reporting and reconciliation, including after completion. A legacy room ID can be used as the reference to read its room-backed claim status. An authorized operator can revoke a deferred invitation through DELETE on its status resource; revocation prevents further claims and does not remove existing room membership.

Persist the invitation before emailing. Failed first delivery leaves it prepared and eligible for an explicit resend, not marked delivered. The v2 reminder endpoint accepts an invitation ID; v1 continues to resolve a room ID to its legacy invitation. Both mint a new code for the same invitation and email the requesting address, returning the send result without the code. Missing, revoked or completed invitations are refused. Recover a partial claim before sending another reminder.

The caller continues applying the communication controls and funnel timing and recording actual decisions in Notification_Log. CMS and contact records mirror invitation and eventual room references; they do not authorize a claim. Ending the outreach schedule does not revoke the invitation.

Claiming and recovery are governed by [knock-with-code](knock-with-code.instructions.md#claiming-a-course).

## Legacy v1 endpoint

`POST /_synapse/client/pangea/v1/create_course_space`

Lives in the `email_invite/` sub-package alongside `invite_by_email`.

### Contract

- **Auth**: Bearer token (bot user). Choreo logs in with bot credentials from AWS Secrets Manager.
- **Input**: Course plan ID, title, description, image URL, the requesting address (`teacher_email`, optional; see below), target language, and an optional one-line `request_summary` of what was asked for, which the first email quotes back. Endpoint does **not** fetch from CMS — all details passed in body.
  - **Target language must be an IETF code** (`es`), never a display name (`Spanish`). It is stored as `l2` on the space's course-plan state event and the public course catalog matches it by base language, so a display name matches nothing — and it is sticky, because the one-time `l2` backfill treats any non-empty value as already correct. It is optional: a space created without it is valid and gets repaired by that backfill, which is the only reason omitting it is safe. See [public-courses](public-courses.instructions.md).
- **Output**: Room ID, the class code, the single-use admin code, the admin join URL, and whether the first email went out (`emailed`) (same format the client already uses for class links — see [joining-courses](../../../client/.github/instructions/joining-courses.instructions.md) Route 1). Which code the teacher sees, and when, is owned by [knock-with-code](knock-with-code.instructions.md) ("Claiming a course").

### What it does

1. Creates a private Matrix space with knock join rules, a course plan state event, and the power levels the client gives a course space it creates itself ([`defaultSpacePowerLevelsContent`](../../../client/lib/pangea/common/constants/default_power_level.dart), with `m.space.child` at 0). Every new space defaults `m.space.child` to 0: a regular member must be able to attach a room, because learners' activity sessions fan out into their courses as space children ([activities.instructions.md](../../../client/.github/instructions/activities.instructions.md)). The space also requires instructor analytics access to join, as a course created in the client does ([course-analytics-access](../../../.github/.github/instructions/course-analytics-access.instructions.md)).
2. Generates the class code and a single-use admin code. The class code goes in join rules directly (bypasses `request_room_code`); the admin code is kept out of room state, in a server-side claim record with the address the course was created for ([knock-with-code](knock-with-code.instructions.md), "Claiming a course")
3. Uploads course image as room avatar if provided
4. Sends the teacher the first email: their course is ready, with the admin link behind a button, a line saying the tap leads to sign-up or log-in, and no class code. It prints no admin code and shows no store badges (decided 2026-10-09, reversing the 2026-10-05 addition): the link is the only claim path in mail, and a teacher who installs the app first claims a prepared invitation by signing in with the requesting address (claim by verified email, which covers prepared invitations only, not historical room-backed courses). The canonical copy for this email is `notices/course_invite/teacher_invite.md` in the engagement repo; this template is the implementation, kept in parity by review, and a change to the canonical file does not change this email until the template follows it. The second email, carrying the class code, is sent when the course is claimed ([knock-with-code](knock-with-code.instructions.md))

## Claim reminders

`POST /_synapse/client/pangea/v1/send_course_claim_reminder`

Sends the requesting address a reminder carrying a new claim link for a course not yet claimed ([knock-with-code](knock-with-code.instructions.md), "A reminder carries a new claim link"). Like the first email, it carries the link alone and says to tap it again after signing in; no code and no store links (decided 2026-10-09). Also how a first email that failed to send is sent again.

- **Auth**: Bearer token of a server admin (the bot).
- **Input**: `room_id`, and the rendered message: `subject`, `body` (plain text; a blank line separates paragraphs) and `cta_label`. The claim link is the button's target and is never passed in. The caller renders the text so the endpoint does not change when the message catalog's templates arrive.
- **Output**: whether it was sent. Never the code or the link.
- **Refused**: a room with no claim record, a course already claimed, and a course recorded without an address.

The caller records the send in the Notification_Log, as it does the delivery: Synapse does not write to the CMS.

## Who the course is created for

The endpoint is told the address the course was requested from and records it. That address is where the claim link goes, and where the class link goes once the course is claimed; the claim never requires the teacher's Pangea account to match it. It may be matched as an additional path (owner decision 2026-10-05, pangeachat/client#9363): when an account whose Synapse-verified address equals it signs in, or that verified address is added to the account, the module may claim the prepared invitation for that account, through the same claim path as the link and with no confirmation, as the link path has none; the match reads only verified identifiers Synapse holds, and no lookup ever returns the code. It is recorded because nobody from the course is a member until the claim, so at claim time there is nowhere else to find it.

The address is kept out of room state. Every member of a course can read its room state, so an address stored there would be visible to every student who joins.

A course created without an address sends no email and stays bot-administered until someone is granted admin deliberately.

Claim behaviour is owned by [knock-with-code](knock-with-code.instructions.md) ("Claiming a course"); this endpoint supplies the address and the codes.

### Unauthenticated teachers

If the teacher doesn't have a Pangea account, the client handles this: the code from the link is cached to disk, the user is prompted to sign up, and after account creation the cached code is submitted. For a teacher claiming a course that code is the admin code, so the claim goes through the same join flow as any other code. No special handling needed here — see [joining-courses](../../../client/.github/instructions/joining-courses.instructions.md) "Pre-login persistence."

## Dependencies

- **The two emails** are sent through Synapse's own mail path (the homeserver's `email` config), so they go out from its sender and stream. A failed first email is captured and answered as `emailed: false` rather than failing the request, because the space already exists and a retry would make a second one. The claim record is written before the first email; if it cannot be written the request fails and names the room, because the admin code lives in that record and the course could not otherwise be claimed.
- **Email templates** ship inside the package (`email_invite/templates/`), as the nudge emails' do, and are read through the module API's template loader. They are not in [synapse-templates](../../../synapse-templates/): that repo is pinned to a tag per environment, so a template there would need a tag and an inventory bump in each environment before the module that sends it could load.

## Future Work

- Automated pipeline trigger (webhook from CMS on status change) — issue TBD
