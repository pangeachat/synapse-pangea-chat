"""The one write path from the logged-out unsubscribe pages into the refusal
store.

Both pages share it, so one person's writes are serialized across them, and a
missed-message refusal always takes the person's email pushers with it:
Synapse's missed-message mail is sent by those pushers, not by this module.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional

from synapse.module_api import ModuleApi
from synapse.util.async_helpers import Linearizer

from synapse_pangea_chat.notice_delivery.categories import (
    COMMUNICATION_PREFERENCES_ACCOUNT_DATA_TYPE,
    MISSED_MESSAGE_CATEGORY,
    SOURCE_UNSUBSCRIBE_LINK,
    parse_preferences,
    with_refusal,
)
from synapse_pangea_chat.notice_delivery.common import now_ms

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.notice_delivery.refusal_store"
)

EMAIL_PUSHER_APP_ID = "m.email"


class RefusalStore:
    def __init__(self, api: ModuleApi):
        self._api = api
        # The module runs on the main process, so a process-local lock suffices.
        self._write_lock = Linearizer(
            name="notice_refusal_store", clock=api._hs.get_clock()
        )

    async def add_refusals(
        self,
        user_id: str,
        *,
        categories: Iterable[str],
        all_off: Optional[bool] = None,
    ) -> Dict[str, Any]:
        refused = frozenset(categories)
        manager = self._api.account_data_manager
        async with self._write_lock.queue(user_id):
            current = parse_preferences(
                await manager.get_global(
                    user_id, COMMUNICATION_PREFERENCES_ACCOUNT_DATA_TYPE
                )
            )
            updated = with_refusal(
                current,
                categories=refused,
                all_off=all_off,
                now_ms=now_ms(self._api),
                source=SOURCE_UNSUBSCRIBE_LINK,
            )
            await manager.put_global(
                user_id, COMMUNICATION_PREFERENCES_ACCOUNT_DATA_TYPE, updated
            )
        if MISSED_MESSAGE_CATEGORY in refused:
            await self._remove_email_pushers(user_id)
        logger.info(
            "communication preference updated via unsubscribe link: categories=%s all_off=%s",
            sorted(refused),
            all_off,
        )
        return updated

    async def _remove_email_pushers(self, user_id: str) -> None:
        hs = self._api._hs
        pusher_pool = hs.get_pusherpool()
        removed = 0
        for pusher in await hs.get_datastores().main.get_pushers_by_user_id(user_id):
            if pusher.app_id != EMAIL_PUSHER_APP_ID:
                continue
            await pusher_pool.remove_pusher(pusher.app_id, pusher.pushkey, user_id)
            removed += 1
        # What Synapse's own unsubscribe does after removing a pusher, so the
        # pusher worker stops it too.
        hs.get_notifier().on_new_replication_data()
        logger.info(
            "removed %d email pusher(s) after a missed-message refusal", removed
        )
