---
applyTo: "synapse_pangea_chat/room_code/**,synapse_pangea_chat/preview_with_code/**"
description: "How a seven-character code gets a person into a course: class codes, admin codes, an educator's claim of a requested course, additional-instructor invitations and the share kit."
---

# Course codes

A seven-character code lets a person into a knock-only course without an admin's approval: the code is the authorization. A class code makes a student a member; an admin code, a claim code or an additional-instructor invitation makes an educator a course admin. The server checks the code and invites the person itself, so the course stays knock-only for everyone else. Nobody knocks; `knock_with_code` is a historical name. Every way into a course is in the org [joining courses](../../../.github/.github/instructions/joining-courses.instructions.md) doc. This doc runs over the 800-word budget because the claim's ownership and recovery rules have no other home.

## Entering a code

1. The app sends whatever the student typed or the class link carried. Input that is not a code (seven letters and digits, at least one a digit) is answered as the person's own mistake, usually a course name in the code box. A well-formed code that opens nothing is answered "code not found", so the app asks the student to check it rather than reporting a fault ([#197](https://github.com/pangeachat/synapse-pangea-chat/issues/197)).
2. For each course the code opens: a member is told they are already in; a banned account is told so, so the ban does not read as a wrong code; an account every admin has blocked gets a refusal that hides the block ([blocked-join gate](blocked-join-gate.instructions.md)); an account already invited gets no second invite; anyone else is invited by a joined admin of the course, and the app joins.
3. A failed invite, including a course with no eligible admin, is reported to Sentry without stopping the other courses. When every course fails, the answer is a server error, so a fault does not look like a wrong code.

Each account can try a limited number of codes per minute.

## Codes

- Generated codes leave out characters people confuse when copying from a board (0/o, 1/i/l, q/g, t/y). Any letters and digits are accepted, ignoring case, so older codes keep working. The required digit tells a code apart from a page address in the app's links.
- A class code and a client-set admin code live in the course's join rules. An admin code makes its first user a course admin and is then removed.
- Class codes, claim codes and student invitation codes share one namespace. A student invitation's code opens its course like the class code, is not spent, and records each join against its invitation ([student invitations](student-invitations.instructions.md#invitations-sent-before-seats)).
- A lookup confirms the course's current join rules before acting, so a changed or used code is refused. It does not scan every room, because that load on Synapse's shared database slowed messaging for everyone ([#163](https://github.com/pangeachat/synapse-pangea-chat/issues/163)).

## Claiming a course

Preparing a requested course and its emails are governed by [create-course-space](create-course-space.instructions.md). The course has no room until the educator claims it, and its claim codes live only in a private record, because every member can read join rules.

1. The educator opens the claim link from the first email or any reminder. Opening or previewing a link reserves, creates and spends nothing.
2. The first signed-in account to use a valid link wins, whatever its email address, and no other account can then create the course or gain rights. An account holding the requesting address as a verified email claims at sign-in without the link ([create-course-space](create-course-space.instructions.md#the-experience)); only the link resumes a partial claim.
3. The winner creates the course as its first admin, with the prepared settings and a class code. The bot joins separately and cannot hold up the claim.
4. The claim completes once the educator's membership, admin rights and course settings are verified, and the app receives the course as already joined.

After completion the links stop granting rights. The winner can use one to reach the course while still a joined admin; anyone else gets the unknown-code answer, so an old link cannot undo a removal or demotion.

A retry after a crash or timeout recovers the room already created. A revocation, a leave or a demotion wins over recovery, which does not restore withdrawn rights; when it cannot tell an unfinished step from a withdrawal, it stops for an operator. Only a separate admin action gives the claim to another account. A failed creation or rights check is a server error, and the winner can resume without a new email.

## Codes, share kit and existing courses

Claim codes are stored only as fingerprints, and raw codes and access tokens are never logged. The class code grants only membership. The first email carries the claim button; the share kit follows once the educator owns the course.

Completion records that the share kit is owed to the requesting address, or the `send_to` address a reminder last gave, even when another account claimed through a forwarded link. The share kit is the email the caller stored with the invitation (`claimed_email`), with its links filled in, or the module's own template when none was stored; if the stored email cannot be read, it stays owed and is retried. It does not name the claiming account, and it is sent once, though a mail timeout may duplicate it. The address is cleared once it is delivered.

An additional-instructor invitation adds an educator to an existing course. A server admin prepares it only for a course with a joined admin, who sends the invite; the caller sends every email for it. Claiming it works like a first claim, except that it creates no course, mints no class code and sends no share kit: the account is joined if needed, or promoted if already a student, and verified as an admin. A banned or blocked account gets the unknown-code answer. Analytics access follows the role ([grant-instructor-analytics-access](grant-instructor-analytics-access.instructions.md)).

Courses created before invitations existed keep their room-backed claims. An empty one needs verified repair or migration before another claim is promised.
