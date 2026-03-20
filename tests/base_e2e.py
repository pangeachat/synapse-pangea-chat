import asyncio
import gc
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import warnings
from typing import IO, Any, Dict, Optional, Tuple, Union
from unittest.mock import patch

import aiounittest
import psycopg2
import requests
import testing.postgresql
import yaml
from psycopg2.extensions import parse_dsn

logger = logging.getLogger(__name__)


class BaseSynapseE2ETest(aiounittest.AsyncTestCase):
    """Base class for Synapse E2E tests with shared infrastructure methods."""

    server_url = "http://localhost:8008"

    def setUp(self) -> None:
        super().setUp()

        self._resource_warning_context = warnings.catch_warnings(record=True)
        self._caught_resource_warnings = self._resource_warning_context.__enter__()
        warnings.simplefilter("always", ResourceWarning)

        original_request = requests.sessions.Session.request

        def request_with_closed_connections(
            session: requests.sessions.Session,
            method: str,
            url: str,
            **kwargs: Any,
        ):
            headers = dict(kwargs.pop("headers", {}) or {})
            headers.setdefault("Connection", "close")
            kwargs["headers"] = headers
            return original_request(session, method, url, **kwargs)

        request_patcher = patch.object(
            requests.sessions.Session,
            "request",
            new=request_with_closed_connections,
        )
        request_patcher.start()
        self.addCleanup(request_patcher.stop)

    def tearDown(self) -> None:
        gc.collect()
        resource_warnings = [
            warning
            for warning in self._caught_resource_warnings
            if issubclass(warning.category, ResourceWarning)
        ]
        self._resource_warning_context.__exit__(None, None, None)
        super().tearDown()

        if resource_warnings:
            warning_messages = "\n".join(
                f"{warning.filename}:{warning.lineno}: {warning.message}"
                for warning in resource_warnings
            )
            self.fail(
                "ResourceWarning emitted during E2E test:\n" f"{warning_messages}"
            )

    async def start_test_synapse(
        self,
        *,
        module_config: Optional[Dict[str, Any]] = None,
        synapse_config_overrides: Optional[Dict[str, Any]] = None,
    ) -> Tuple[
        testing.postgresql.Postgresql,
        str,
        str,
        subprocess.Popen,
        threading.Thread,
        threading.Thread,
    ]:
        """Start a test Synapse server backed by PostgreSQL.

        Returns (postgres, synapse_dir, config_path, server_process, stdout_thread, stderr_thread).
        """
        postgres: Optional[testing.postgresql.Postgresql] = None
        synapse_dir: Optional[str] = None
        server_process: Optional[subprocess.Popen] = None
        stdout_thread: Optional[threading.Thread] = None
        stderr_thread: Optional[threading.Thread] = None
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        try:
            postgres, db_url = await self._start_postgres()

            synapse_dir = tempfile.mkdtemp()
            config_path = os.path.join(synapse_dir, "homeserver.yaml")
            generate_config_cmd = [
                sys.executable,
                "-m",
                "synapse.app.homeserver",
                "--server-name=my.domain.name",
                f"--config-path={config_path}",
                "--report-stats=no",
                "--generate-config",
            ]
            subprocess.check_call(generate_config_cmd)

            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            log_config_path = config.get("log_config")

            effective_module_config = {
                "export_user_data_output_dir": os.path.join(
                    synapse_dir, "export-user-data"
                ),
                "cms_base_url": "http://127.0.0.1:9",
                "cms_service_api_key": "test-cms-api-key",
            }
            if module_config:
                effective_module_config.update(module_config)

            config["modules"] = [
                {
                    "module": "synapse_pangea_chat.PangeaChat",
                    "config": effective_module_config,
                }
            ]

            dsn_params = parse_dsn(db_url)
            config["database"] = {
                "name": "psycopg2",
                "args": dsn_params,
            }

            workspace_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..")
            )
            template_dir = os.path.join(
                workspace_root, "synapse-templates", "templates"
            )
            test_template_dir = os.path.join(synapse_dir, "test-templates")
            os.makedirs(test_template_dir, exist_ok=True)

            if os.path.isdir(template_dir):
                for template_name in os.listdir(template_dir):
                    src = os.path.join(template_dir, template_name)
                    dst = os.path.join(test_template_dir, template_name)
                    if os.path.isfile(src):
                        shutil.copyfile(src, dst)

            invite_templates = {
                "course_invite.html": "<html><body>{{ app_name }}</body></html>",
                "course_invite.txt": "{{ app_name }}",
            }
            for template_name, template_contents in invite_templates.items():
                template_path = os.path.join(test_template_dir, template_name)
                if not os.path.exists(template_path):
                    with open(template_path, "w", encoding="utf-8") as template_file:
                        template_file.write(template_contents)

            templates_config = config.get("templates", {})
            if not isinstance(templates_config, dict):
                templates_config = {}
            templates_config["custom_template_directory"] = test_template_dir
            config["templates"] = templates_config

            if synapse_config_overrides:
                for key, value in synapse_config_overrides.items():
                    config[key] = value

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f)
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
            with open(log_config_path, "r", encoding="utf-8") as f:
                log_config = yaml.safe_load(f)
            log_config["root"]["handlers"] = ["console"]
            log_config["root"]["level"] = "DEBUG"
            with open(log_config_path, "w", encoding="utf-8") as f:
                yaml.dump(log_config, f)

            run_server_cmd = [
                sys.executable,
                "-m",
                "synapse.app.homeserver",
                "--config-path",
                config_path,
            ]
            server_process = subprocess.Popen(
                run_server_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=synapse_dir,
                text=True,
            )

            def read_output(pipe: Union[IO[str], None], sink: list[str]) -> None:
                if pipe is None:
                    return
                for line in iter(pipe.readline, ""):
                    sink.append(line.rstrip("\n"))
                    logger.debug(line)
                pipe.close()

            stdout_thread = threading.Thread(
                target=read_output, args=(server_process.stdout, stdout_lines)
            )
            stderr_thread = threading.Thread(
                target=read_output, args=(server_process.stderr, stderr_lines)
            )
            stdout_thread.start()
            stderr_thread.start()

            max_wait_time = 30
            wait_interval = 1
            total_wait_time = 0
            server_ready = False
            while not server_ready and total_wait_time < max_wait_time:
                if server_process.poll() is not None:
                    break
                try:
                    response = requests.get(f"{self.server_url}/health", timeout=10)
                    if response.status_code == 200:
                        server_ready = True
                        break
                except requests.exceptions.ConnectionError:
                    pass
                finally:
                    await asyncio.sleep(wait_interval)
                    total_wait_time += wait_interval

            if not server_ready:
                if server_process.poll() is None:
                    server_process.terminate()
                    try:
                        server_process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server_process.kill()
                        server_process.wait(timeout=10)

                if stdout_thread is not None:
                    stdout_thread.join(timeout=5)
                if stderr_thread is not None:
                    stderr_thread.join(timeout=5)

                stdout_tail = "\n".join(stdout_lines[-20:])
                stderr_tail = "\n".join(stderr_lines[-20:])
                raise RuntimeError(
                    "Synapse server did not start successfully. "
                    f"exit_code={server_process.returncode}. "
                    f"stdout_tail=\n{stdout_tail}\n"
                    f"stderr_tail=\n{stderr_tail}"
                )

            return (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            )
        except Exception as e:
            if server_process is not None:
                server_process.terminate()
                try:
                    server_process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    server_process.kill()
                    server_process.wait(timeout=30)
            if stdout_thread is not None:
                stdout_thread.join(timeout=10)
            if stderr_thread is not None:
                stderr_thread.join(timeout=10)
            if synapse_dir is not None and os.path.exists(synapse_dir):
                shutil.rmtree(synapse_dir)
            if postgres is not None:
                postgres.stop()
            raise e

    async def _start_postgres(
        self,
    ) -> Tuple[testing.postgresql.Postgresql, str]:
        postgresql = None
        try:
            postgresql = testing.postgresql.Postgresql()
            postgres_url = postgresql.url()

            max_waiting_time = 10
            wait_interval = 1
            total_wait_time = 0
            postgres_is_up = False
            while total_wait_time < max_waiting_time and not postgres_is_up:
                try:
                    conn = psycopg2.connect(postgres_url)
                    conn.close()
                    postgres_is_up = True
                    break
                except psycopg2.OperationalError:
                    await asyncio.sleep(wait_interval)
                    total_wait_time += wait_interval

            if not postgres_is_up:
                postgresql.stop()
                self.fail("Postgres did not start successfully")

            dbname = "testdb"
            conn = psycopg2.connect(postgres_url)
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute(
                f"""
                CREATE DATABASE {dbname}
                WITH TEMPLATE template0
                LC_COLLATE 'C'
                LC_CTYPE 'C';
            """
            )
            cursor.close()
            conn.close()

            dsn_params = parse_dsn(postgres_url)
            dsn_params["dbname"] = dbname
            postgres_url_testdb = psycopg2.extensions.make_dsn(**dsn_params)

            return postgresql, postgres_url_testdb
        except Exception as e:
            if postgresql is not None:
                postgresql.stop()
            raise e

    async def register_user(
        self, config_path: str, dir: str, user: str, password: str, admin: bool
    ) -> None:
        register_user_cmd = [
            sys.executable,
            "-m",
            "synapse._scripts.register_new_matrix_user",
            f"-c={config_path}",
            f"--user={user}",
            f"--password={password}",
        ]
        if admin:
            register_user_cmd.append("--admin")
        else:
            register_user_cmd.append("--no-admin")
        subprocess.check_call(register_user_cmd, cwd=dir)

    async def login_user(self, user: str, password: str) -> Tuple[str, str]:
        """Returns (user_id, access_token)."""
        login_url = f"{self.server_url}/_matrix/client/v3/login"
        login_data = {
            "type": "m.login.password",
            "user": user,
            "password": password,
        }
        response = requests.post(login_url, json=login_data)
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        access_token = response_json["access_token"]
        user_id = response_json["user_id"]
        self.assertIsInstance(access_token, str)
        self.assertIsInstance(user_id, str)
        return (user_id, access_token)

    def stop_synapse(
        self,
        *,
        server_process: Optional[subprocess.Popen] = None,
        stdout_thread: Optional[threading.Thread] = None,
        stderr_thread: Optional[threading.Thread] = None,
        synapse_dir: Optional[str] = None,
        postgres: Any = None,
    ) -> None:
        """Clean up Synapse server resources. Call in finally blocks."""
        if server_process is not None:
            server_process.terminate()
            try:
                server_process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server_process.kill()
                server_process.wait(timeout=30)
        if stdout_thread is not None:
            stdout_thread.join(timeout=10)
        if stderr_thread is not None:
            stderr_thread.join(timeout=10)
        if synapse_dir is not None and os.path.exists(synapse_dir):
            shutil.rmtree(synapse_dir)
        if postgres is not None:
            postgres.stop()

    async def create_private_room(self, access_token: str) -> str:
        headers = {"Authorization": f"Bearer {access_token}"}
        create_room_url = f"{self.server_url}/_matrix/client/v3/createRoom"
        create_room_data = {"visibility": "private", "preset": "private_chat"}
        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["room_id"]

    async def create_private_room_knock_allowed_room(self, access_token: str) -> str:
        headers = {"Authorization": f"Bearer {access_token}"}
        create_room_url = f"{self.server_url}/_matrix/client/v3/createRoom"
        create_room_data = {
            "visibility": "private",
            "preset": "private_chat",
            "initial_state": [
                {
                    "type": "m.room.join_rules",
                    "state_key": "",
                    "content": {"join_rule": "knock"},
                }
            ],
        }
        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["room_id"]

    async def invite_user_to_room(
        self, room_id: str, user_id: str, access_token: str
    ) -> bool:
        invite_url = f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/invite"
        invite_data = {"user_id": user_id}
        response = requests.post(
            invite_url,
            json=invite_data,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        return response.status_code == 200

    async def accept_room_invitation(self, room_id: str, access_token: str) -> bool:
        join_url = f"{self.server_url}/_matrix/client/v3/join/{room_id}"
        response = requests.post(
            join_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        return response.status_code == 200
