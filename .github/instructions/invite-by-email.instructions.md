---
applyTo: "synapse_pangea_chat/email_invite/**"
---

# Invite by Email — Synapse Module

Cross-repo design: [conference-course-invite.instructions.md](../../../.github/.github/instructions/conference-course-invite.instructions.md)

## Endpoint

`POST /_synapse/client/pangea/v1/invite_by_email`

- **Auth**: Matrix access token. Caller must be admin (power level 100) in the room → 403 otherwise.
- **Request**: `{ room_id, emails, message? }`
- **Response**: `{ emailed, errors }`

## Behavior

For each email, send a branded Jinja2 email with join link.

Room name, description, avatar, access code, and inviter identity are all read from room state — nothing is passed in the request. Inviter identity = all human users at the highest power level in the room.

The optional `message` field is included in the email as a personal note from the inviter.

## Email

Uses Synapse's built-in email sending (SMTP via Exim → SES). Templates live in [synapse-templates](../../../synapse-templates/) — same deployment pattern as registration/password-reset emails. See [course-invite-by-email.instructions.md](../../../synapse-templates/.github/instructions/course-invite-by-email.instructions.md) for template design.

## Reused from `room_code/`

- Room invite helper
- Inviter resolution from power levels
- Auth pattern from `knock_with_code`

## Future Work

- Rate limiting / batch size limits — no issue yet
- Invite delivery tracking — no issue yet
