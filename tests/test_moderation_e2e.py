"""E2E tests for server-side chat moderation against a real local Synapse.

Tier 1: a message containing a phone number is rejected at send time
(M_FORBIDDEN); a clean message lands. Tier 2: a message the (mocked) choreo
moderation endpoint flags is redacted after the fact, sent as the offender;
activity rooms are left to the orchestrator.
"""

import asyncio
import logging
import time
from typing import Any, Dict, Optional

import requests

from .base_e2e import BaseSynapseE2ETest
from .mock_moderation_server import FLAG_MARKER, MockModerationServer

logger = logging.getLogger(__name__)


class TestModerationE2E(BaseSynapseE2ETest):
    def _send_message(
        self, room_id: str, token: str, body: str, txn: str
    ) -> requests.Response:
        url = (
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}"
            f"/send/m.room.message/{txn}"
        )
        return requests.put(
            url,
            json={"msgtype": "m.text", "body": body},
            headers={"Authorization": f"Bearer {token}"},
        )

    def _get_event(
        self, room_id: str, event_id: str, token: str
    ) -> Optional[Dict[str, Any]]:
        url = (
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}" f"/event/{event_id}"
        )
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            return None
        return resp.json()

    def _wait_for_redaction(
        self, room_id: str, event_id: str, token: str, timeout_s: float = 30.0
    ) -> Dict[str, Any]:
        """Poll until the event's content is emptied by a redaction."""
        deadline = time.monotonic() + timeout_s
        last: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            event = self._get_event(room_id, event_id, token)
            if event is not None:
                last = event
                if not event.get("content"):
                    return event
            time.sleep(0.5)
        self.fail(f"event {event_id} was never redacted; last seen: {last}")

    async def test_tier1_blocks_and_tier2_redacts(self) -> None:
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        mock_moderation = MockModerationServer().start()

        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config={
                    "moderation": {
                        "tier1_enabled": True,
                        "tier1_phone_regions": ["US"],
                        "tier2_enabled": True,
                        "choreo_base_url": mock_moderation.base_url,
                        "choreo_access_token": "syt_mock_service_token",
                        "exempt_user_id_patterns": [r"@exemptbot:.*"],
                    },
                }
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="learner",
                password="p4ssword!",
                admin=False,
            )
            user_id, token = await self.login_user("learner", "p4ssword!")
            room_id = await self.create_private_room(token)

            # --- Tier 1: phone number rejected at send time ---
            resp = self._send_message(
                room_id, token, "text me at 415-555-2671", "txn-t1-block"
            )
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.json().get("errcode"), "M_FORBIDDEN")

            # --- Tier 1: clean message lands ---
            resp = self._send_message(room_id, token, "hola, ¿qué tal?", "txn-clean")
            self.assertEqual(resp.status_code, 200)
            clean_event_id = resp.json()["event_id"]

            # --- Tier 2: flagged message is redacted after the fact ---
            resp = self._send_message(
                room_id, token, f"something awful {FLAG_MARKER}", "txn-t2-flag"
            )
            self.assertEqual(resp.status_code, 200)
            flagged_event_id = resp.json()["event_id"]

            redacted = self._wait_for_redaction(room_id, flagged_event_id, token)
            redacted_because = redacted.get("unsigned", {}).get("redacted_because", {})
            self.assertIn(
                "Removed by Pangea content moderation",
                redacted_because.get("content", {}).get("reason", ""),
            )
            # Self-redaction: the redaction is authored by the offender.
            self.assertEqual(redacted_because.get("sender"), user_id)

            # --- Tier 2: the clean message was checked but survives ---
            # Give the background check a moment before asserting content.
            await asyncio.sleep(2)
            clean_event = self._get_event(room_id, clean_event_id, token)
            assert clean_event is not None
            self.assertEqual(clean_event["content"].get("body"), "hola, ¿qué tal?")
            self.assertTrue(
                any("hola" in t for t in mock_moderation.seen_texts),
                f"clean message never reached moderation: {mock_moderation.seen_texts}",
            )

        finally:
            mock_moderation.stop()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )


if __name__ == "__main__":
    import unittest

    unittest.main()
