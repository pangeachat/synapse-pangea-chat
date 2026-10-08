"""Notice delivery: push-or-email delivery of bot notices, the learner's
communication preferences, the logged-out unsubscribe surface, and the
first-party click record.

Design: the org user-communication-controls and engagement-decisions docs;
contracts: instructions/notice-delivery.instructions.md.
"""

from synapse_pangea_chat.notice_delivery.click import NoticeClick
from synapse_pangea_chat.notice_delivery.deliver import DeliverNotice
from synapse_pangea_chat.notice_delivery.missed_message_unsubscribe import (
    MISSED_MESSAGE_UNSUBSCRIBE_PATH,
    MissedMessageUnsubscribe,
)
from synapse_pangea_chat.notice_delivery.prepare import PrepareNotice
from synapse_pangea_chat.notice_delivery.refusal_store import RefusalStore
from synapse_pangea_chat.notice_delivery.unsubscribe import NoticeUnsubscribe

__all__ = [
    "DeliverNotice",
    "MISSED_MESSAGE_UNSUBSCRIBE_PATH",
    "MissedMessageUnsubscribe",
    "NoticeClick",
    "NoticeUnsubscribe",
    "PrepareNotice",
    "RefusalStore",
]
