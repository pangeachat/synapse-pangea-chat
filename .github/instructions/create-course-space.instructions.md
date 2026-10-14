---
applyTo: "synapse_pangea_chat/email_invite/**"
---

# Create Course Space — Synapse Module

Cross-repo design: [teacher-funnel.instructions.md](../../../.github/.github/instructions/teacher-funnel.instructions.md)

## Deferred v2 preparation

A requested course is ready when its content and settings are prepared and its invitation is durable. Its Matrix space is created when the first instructor claims it. Unclaimed invitations do not depend on the conversational bot being online or remaining a room member.

The preparation record has a stable invitation ID and stores the quest reference, title, description, target language, optional image and request summary, requesting address, and private claim-code fingerprints. The room ID is absent until provisioning. Synapse receives the prepared details from the caller and does not fetch from or write to CMS, apart from reading the course copy when the claim creates the space (Legacy v1 endpoint, step 5). The requesting address and claim codes never appear in room state.

Keep the existing v1 creation endpoint unchanged for compatibility with callers that require an immediate room ID. Add a v2 preparation contract at the same endpoint name: an authorized server operator supplies a stable request key, requesting address, quest reference, title, target-language code and optional presentation fields. It returns the invitation ID, preparation status and email-delivery result, without raw codes or a placeholder room ID. The stable key is scoped to the creating service and identifies a requested course rather than an address. Repeating unchanged input returns the same invitation and its current status without resending; changed input under the same key is a conflict. A delivery whose outcome is uncertain remains explicitly uncertain until reconciled.

An authorized operator can read current status through `GET /_synapse/client/pangea/v2/course_invitations/{invitation_id}`. It returns preparation/provisioning/completion/revocation status, the room ID when known, the reserved claimant reference when present, and delivery-attempt references, timestamps and outcomes. Outcomes distinguish unsent, accepted by the mail transport, failed and uncertain; transport acceptance is not proof of inbox receipt. It never returns addresses or raw codes. Status reads have no send or provisioning side effects and are authoritative for reporting and reconciliation, including after completion. A legacy room ID can be used as the reference to read its room-backed claim status. An authorized operator can revoke a deferred invitation through DELETE on its status resource; revocation prevents further claims and does not remove existing room membership.

Additional-instructor invitations into an existing course are prepared and listed through `/_synapse/client/pangea/v2/instructor_invitations`. They share the status, revocation and reminder resources above, and every status read names the invitation's `kind`, `instructor` or `course`. Their contract, including what the claim does and does not do, is owned by [knock-with-code](knock-with-code.instructions.md#codes-share-kit-and-existing-courses).

The v2 prepare takes `send_email: false` to persist the invitation and send nothing; the caller then sends the delivery through the v2 reminder endpoint with its own rendered copy (the engagement runner's `course_invite/teacher_invite`), so the first email is content the runner owns and a copy change needs no module deploy. Persist the invitation before emailing. Failed first delivery leaves it prepared and eligible for an explicit resend, not marked delivered. The v2 reminder endpoint accepts an invitation ID; v1 continues to resolve a room ID to its legacy invitation. Both mint a new code for the same invitation and email the requesting address, returning the send result without the code. The v2 endpoint also takes a caller-rendered email: `html` and `text` that both carry `{{cta_url}}` (and may carry `{{receiving_reason}}` and `{{postal_address}}`, filled from the request's `receiving_reason` and the configured postal address), sent as given with the claim link substituted and nothing evaluated as a template, so the engagement runner's full draft (its brand shell, the section it chose, the call to action twice) goes out as rendered. Such an email carries no `{{unsubscribe_url}}` and no `{{cta2_url}}`: pre-claim mail has one call to action and its refusal store is the request record, and the endpoint refuses either slot. Missing, revoked or completed invitations are refused. Recover a partial claim before sending another reminder. The v2 reminder may also carry `claimed_email` (`subject`, `html`, `text`, `receiving_reason`): the share kit the caller rendered (the engagement runner's `course_invite/class_link`, Will 2026-10-11), stored on the invitation before the send and sent to the requesting address when the course is claimed, with `{{class_url}}` (required in both parts), `{{class_code}}`, `{{receiving_reason}}` and `{{postal_address}}` filled by literal replacement. It carries no `{{cta_url}}`, `{{unsubscribe_url}}` or `{{cta2_url}}`, which the endpoint refuses. The latest one stored for an invitation is the one sent; an invitation with none stored gets the module's own template.

The share kit carries a QR code of the class link, for the teacher to put on a slide (Will, 2026-10-12). The caller places it with `{{class_qr}}` as an image source in the HTML (`<img src="{{class_qr}}">`, with its own size and alt text). At the claim the module fills the slot with a reference to the QR code, which it attaches to the email as an inline image. The image is attached rather than hosted, because Outlook blocks remote images until the reader allows them and an attached image needs no new public endpoint. The plain text refuses the slot: it cannot show an image and already prints the link. A share kit without the slot, including one stored before the slot existed, goes out with no image. The module's own template carries the same QR code. A QR code that cannot be made fails the send, so the share kit stays owed and is retried.

**Replacing the course plan.** A thin request is delivered with a starter quest and gets its tailored quest later, in the same course ([teacher-funnel](../../../engagement/.github/instructions/teacher-funnel.instructions.md), "A thin request gets the starter"; decided 2026-10-12). An authorized operator replaces an invitation's course plan through the status resource, giving the new quest reference, title and description. Before completion the stored preparation is updated and later claims provision from it, with the same invitation and claim codes. After completion the module rewrites the provisioned room's course-plan state, name and topic itself, as the server, so no instructor token and no bot membership is needed. The replacement never touches membership, power levels, the class code, the claim codes or the share kit, and it is refused for a revoked invitation. Status reads list each replacement with its time and quest reference, so the request record's mirror can show which build the course holds. A replacement in a created room also replaces its course copy (`pangea.course_missions`, step 5 below), or the copy keeps the old quest's Can-Dos: built for the new quest by the same read, with revision one above the old copy's (1 if there was none) so an open Studio refuses a stale publish. If the read fails, the copy is written with empty content, so readers fall back to the shared quest, and the failure is logged. It is sent by a member at power level 100, or it is ignored. The rewrite of a created room (course plan, copy, name and topic) is built as [set-course-plan](set-course-plan.instructions.md); replacing an invitation's quest before the claim is not built yet.


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
5. Writes the course copy (`pangea.course_missions`, Content Studio) in the space's initial state, as the v2 claim does when it creates a deferred course, sent by the space's creator, who is at power level 100, which the event requires. It is the one CMS read either path makes: the quest's objectives in order, each with its visible library activities at the quest's language. If the read fails, the course is created without a copy and the failure is logged; the Studio writes the copy on first publish ([content-studio](../../../.github/.github/instructions/content-studio.instructions.md)).

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

- **The two emails** are sent through Synapse's own mail path (the homeserver's `email` config), so they go out from its sender and stream. A failed first email is captured and answered as `emailed: false` rather than failing the request, because the space already exists and a retry would make a second one. The claim record is written before the first email; if it cannot be written the request fails and names the room, because the admin code lives in that record and the course could not otherwise be claimed. A share kit with a QR code is the one email the module assembles itself, because Synapse's mailer sends only text and HTML: the module adds the image as an inline part and hands the message to Synapse's own SMTP transport, so it goes out from the same sender and stream. That transport is not part of Synapse's module API, so the module's tests check it against the Synapse version they run.
- **Email templates** ship inside the package (`email_invite/templates/`), as the nudge emails' do, and are read through the module API's template loader. They are not in [synapse-templates](../../../synapse-templates/): that repo is pinned to a tag per environment, so a template there would need a tag and an inventory bump in each environment before the module that sends it could load. Their layout is the [brand email layout](../../../synapse-templates/.github/instructions/brand-email-layout.instructions.md); the package carries a generated copy of it for the same reason, never edited by hand.

## Future Work

- Automated pipeline trigger (webhook from CMS on status change) — issue TBD
