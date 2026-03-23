import asyncio
import gc
import tempfile
import warnings

import requests

from .base_e2e import BaseSynapseE2ETest
from .mock_cms_server import MockCmsServer


class TestE2EResourceWarnings(BaseSynapseE2ETest):
    async def test_mock_cms_export_flow_emits_no_resource_warnings(self):
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        mock_cms = MockCmsServer()

        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always", ResourceWarning)

            try:
                cms_url = mock_cms.start()
                with tempfile.TemporaryDirectory() as export_dir:
                    (
                        postgres,
                        synapse_dir,
                        config_path,
                        server_process,
                        stdout_thread,
                        stderr_thread,
                    ) = await self.start_test_synapse(
                        module_config={
                            "export_user_data_processor_interval_seconds": 1,
                            "export_user_data_output_dir": export_dir,
                            "cms_base_url": cms_url,
                            "cms_service_api_key": "test-cms-api-key",
                        }
                    )

                    await self.register_user(
                        config_path=config_path,
                        dir=synapse_dir,
                        user="warningcheck",
                        password="pw1",
                        admin=False,
                    )
                    user_id, access_token = await self.login_user("warningcheck", "pw1")
                    matrix_user = mock_cms.seed_matrix_user(user_id)

                    schedule_response = requests.post(
                        f"{self.server_url}/_synapse/client/pangea/v1/export_user_data",
                        json={"action": "schedule"},
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                    self.assertEqual(schedule_response.status_code, 200)
                    schedule_response = None

                    await asyncio.sleep(3)
                    self.assertEqual(
                        len(mock_cms.get_exports_for_matrix_user_id(matrix_user["id"])),
                        1,
                    )
            finally:
                self.stop_synapse(
                    server_process=server_process,
                    stdout_thread=stdout_thread,
                    stderr_thread=stderr_thread,
                    synapse_dir=synapse_dir,
                    postgres=postgres,
                )
                mock_cms.stop()

            gc.collect()

        resource_warnings = [
            warning
            for warning in caught_warnings
            if issubclass(warning.category, ResourceWarning)
        ]
        self.assertEqual(
            resource_warnings,
            [],
            "Expected no ResourceWarning instances during the E2E HTTP flow",
        )
