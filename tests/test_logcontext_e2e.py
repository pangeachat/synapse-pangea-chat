"""Regression test: module HTTP resources must not leak their logcontext.

Handlers launched with a bare ``defer.ensureDeferred`` leave the request's
logcontext set on the reactor. Synapse logs "Expected logging context ... was
lost" for every such request, and since 1.159 a hardened ``clock.py`` asserts
on the leaked context and permanently kills any ``looping_call`` that fires in
the leaked window ("Looping call died"). Resources must launch handlers via
``synapse.logging.context.run_in_background`` instead.
"""

import asyncio

import requests

from .base_e2e import BaseSynapseE2ETest

LEAK_MARKERS = ("Expected logging context", "Looping call died")


class TestLogcontextLeak(BaseSynapseE2ETest):
    async def test_module_endpoints_do_not_leak_logcontext(self):
        postgres = synapse_dir = config_path = None
        server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse()

            await self.register_user(
                config_path, synapse_dir, "leakuser", "leakpass", admin=False
            )
            _, access_token = await self.login_user("leakuser", "leakpass")
            headers = {"Authorization": f"Bearer {access_token}"}

            # One GET and one POST module endpoint, plus an auth-rejected
            # request — pre-fix, all three leaked (the leak sits in the shared
            # render plumbing, not any one handler).
            requests.get(
                f"{self.server_url}/_synapse/client/pangea/v1/public_courses",
                headers=headers,
            )
            requests.post(
                f"{self.server_url}/_synapse/client/pangea/v1/knock_with_code",
                json={"access_code": "no-such-code"},
                headers=headers,
            )
            requests.get(
                f"{self.server_url}/_synapse/client/pangea/v1/public_courses",
            )

            # Let the server flush its log output through the reader threads.
            await asyncio.sleep(2)

            leaked = [
                line
                for line in self.server_stdout_lines + self.server_stderr_lines
                if any(marker in line for marker in LEAK_MARKERS)
            ]
            self.assertEqual(
                leaked,
                [],
                "module requests leaked their logcontext to the reactor:\n"
                + "\n".join(leaked),
            )
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
