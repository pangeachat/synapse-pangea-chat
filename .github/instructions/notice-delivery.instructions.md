---
applyTo: "synapse_pangea_chat/direct_push/**,synapse_pangea_chat/notice_delivery/**,synapse_pangea_chat/config.py,synapse_pangea_chat/__init__.py,tests/test_direct_push*.py,tests/test_notice_delivery*.py,tests/test_open_session_notice*.py"
description: "How the Synapse module delivers a notice to an account: one channel per send, the refusal store and the signed-out unsubscribe page, signed links and the click record, and keeping Synapse's own mail and push off bot notices."
---

# Notice Delivery — Synapse Module

The module is how a notice reaches a person with an account. It picks one channel, honors the person's refusals, signs every emailed link and records the send, for every caller: the bot, the engagement runner and the module's own open-session notice. What a notice is and what every send must carry belong to [notices](../../../engagement/.github/instructions/notices.instructions.md). Credential mail and course-claim mail keep their own delivery paths.

## Sending a notice

The caller records the notice in the person's bot DM, then a server admin asks the module to deliver it. A notice that may use any channel goes by exactly one, in this order:

1. **Refused**: the person refused the category, or the global off covers it. Nothing is sent.
2. **In app**: the person is online and active, so the notice in their DM is the delivery.
3. **Push**: a device accepted the push. An expired device token counts as a failed push, so the person falls through to email.
4. **Email**: email is turned on and the person has a verified address.
5. **None**, with the reason.

When presence is off or unreadable, the person counts as not in the app: an outage must not lose a notice they are due, and being wrong costs one push to someone online. A caller may force one channel; refusals and the category's allowed channels still apply, and a forced push does not fall back to email.

Every send is recorded once in the [Notification_Log](../../../cms/.github/instructions/notification-log.instructions.md). A retry cannot send the same decision twice, and an uncertain outcome waits for reconciliation instead of being resent. When the caller has written the row itself, as the engagement runner does, the module still sends that row at most once.

## Email and links

Notice email uses the [brand email layout](../../../synapse-templates/.github/instructions/brand-email-layout.instructions.md). The caller writes the copy, including why the person receives it; the module adds the signed links, the sender and the unsubscribe headers, and does not run caller copy as a template.

Each link lands where the request named: a place in the app, or a page on a configured list of external hosts, checked again at the click so removing a host retires links already sent. Each link names one person and one action, carries ids only (never an address or message text), and expires after 90 days, past CAN-SPAM's 30 and CASL's 60.

Every call to action goes through the module, which records the open in the DM as the same event the app writes on a push tap, so an email click counts toward cooldown and backoff like a tap. A valid link always reaches its destination, even when a whole class clicks from one school network.

## Scheduled notices

A scheduled notice survives restarts. Just before sending, the module checks refusals and availability again, plus any conditions the caller set, such as the person having come back; a failed or unreadable check sends nothing and records why. An admin can cancel one until it starts sending.

## The open-session notice

Thirty minutes after a learner starts an activity session, the module pushes them a notice that Pangea Bot takes the open seat at the hour; tapping it opens the session. It first checks the session the way the bot does before filling a seat ([bot-modes](../../../pangea-bot/.github/instructions/bot-modes.instructions.md)), so a session that filled, got the bot or was left gets nothing.

## Refusals and the unsubscribe page

A person's refusals are one account-data event, `pangea.communication_preferences`, which the in-app preference screen and the unsubscribe page both write and every send reads; [notices](../../../engagement/.github/instructions/notices.instructions.md#refusability) says what can be refused. A malformed event reads as nothing refused: silencing someone who never asked is the worse failure, and an unwanted nudge can be refused again.

Every refusable notice email links to a signed-out unsubscribe page. Opening the link only shows the page; submitting it acts, so a mail scanner cannot unsubscribe anyone. It only adds refusals; turning one back on needs the signed-in screen. Do not promise in-app re-enabling until [client#9234](https://github.com/pangeachat/client/issues/9234) ships.

Synapse's own missed-message mail links to Synapse's unsubscribe page, which acts on a plain open and records no refusal; the module can serve that link with its own page instead.

## Keeping Synapse's own mail and push off bot notices

Left alone, Synapse would mail a bot notice as a missed message with no unsubscribe link and push it a second time, so each recipient gets a push rule that silences notice events before their first notice is recorded.

## Direct push (`send_push`)

A push names the account that sent the notice, because the app follows a notice's link only when its environment's bot sent it ([client notices](../../../client/.github/instructions/notices.instructions.md)). Each push goes to the Sygnal the device registered with when that Sygnal is on the allowed list, so staging can reach a production app build ([client deployment](../../../client/.github/instructions/deployment.instructions.md#environment-switcher)).
