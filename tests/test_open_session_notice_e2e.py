import asyncio

import requests

from synapse_pangea_chat.notice_delivery.open_session_notice import notice_request

from .base_e2e import SERVER_NAME, BaseSynapseE2ETest


class TestOpenSessionNoticeE2E(BaseSynapseE2ETest):
    """Starting an activity session against a real Synapse queues the
    open-session notice (notice-delivery.instructions.md, "The open-session
    notice"). Sending it is the scheduled delivery's, covered by its tests."""

    async def _homeserver_and_bot(self):
        bot_id = f"@{self.tag}-bot:{SERVER_NAME}"
        await self.homeserver(
            module_config={
                "send_push_sygnal_url": "https://sygnal.example.test/_matrix/push/v1/notify",
                "open_session_notice_bot_user_id": bot_id,
            }
        )
        registered, bot = await self._register_localpart(f"{self.tag}-bot", "pw", True)
        self.assertEqual(registered, bot_id)
        return bot_id, bot

    def _post(self, token, path, body, status=200):
        response = requests.post(
            f"{self.server_url}{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    async def _wait_for_log(self, text):
        for _ in range(50):
            if any(text in line for line in self.server_log()):
                return
            await asyncio.sleep(0.1)
        self.fail(f"No log line containing {text!r}")

    def _start_session(self, token):
        return self._post(
            token,
            "/_matrix/client/v3/createRoom",
            {
                "preset": "private_chat",
                "initial_state": [
                    {
                        "type": "pangea.activity_plan",
                        "state_key": "",
                        "content": {"activity_id": "activity-1"},
                    }
                ],
            },
        )["room_id"]

    async def test_starting_a_session_queues_the_notice_once(self):
        bot_id, bot = await self._homeserver_and_bot()
        learner_id, learner = await self.new_user("alice")
        dm = await self.create_private_room(learner)
        self.assertTrue(await self.invite_user_to_room(dm, bot_id, learner))
        self.assertTrue(await self.accept_room_invitation(dm, bot))
        requests.put(
            f"{self.server_url}/_matrix/client/v3/user/{learner_id}/account_data/m.direct",
            json={bot_id: [dm]},
            headers={"Authorization": f"Bearer {learner}"},
        ).raise_for_status()

        session = self._start_session(learner)
        await self._wait_for_log(f"Open-session notice queued for {session}")
        state = requests.get(
            f"{self.server_url}/_matrix/client/v3/rooms/{session}/state",
            headers={"Authorization": f"Bearer {learner}"},
        ).json()
        created_ms = next(
            e["origin_server_ts"] for e in state if e["type"] == "m.room.create"
        )

        # The module queued exactly this request: sending it again is a duplicate.
        expected = notice_request(
            learner=learner_id,
            bot_user_id=bot_id,
            dm_room_id=dm,
            session_room_id=session,
            activity_id="activity-1",
            created_ms=created_ms,
        )
        queued = self._post(
            bot, "/_synapse/client/pangea/v1/deliver_notice", expected, status=202
        )
        self.assertTrue(queued["duplicate"], queued)
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["scheduled_at_ms"], created_ms + 30 * 60 * 1000)

    async def test_a_learner_without_a_bot_dm_gets_no_notice(self):
        await self._homeserver_and_bot()
        learner_id, learner = await self.new_user("bob")
        self._start_session(learner)
        await self._wait_for_log(f"{learner_id} has no joined DM with the bot")
