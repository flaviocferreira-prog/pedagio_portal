from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from base64 import b64encode
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
from conferencia.controllers.conference_controller import ConferenceController
from conferencia.infrastructure.database import SQLiteDatabase
from conferencia.infrastructure.session_manager import SessionManager
from conferencia.infrastructure.settings import AppSettings
from conferencia.readers.excel_reader import PalletFileImporter
from conferencia.repositories.colaborador_repository import ColaboradorRepository
from conferencia.repositories.pallet_repository import PalletRepository
from conferencia.services.acesso_service import AcessoService
from conferencia.services.automatic_report_service import AutomaticReportService
from conferencia.services.conference_service import ConferenceService
from conferencia.sync.repository import SyncRepository
from conferencia.sync.scheduler import SyncScheduler
from conferencia.sync.service import SyncService
from conferencia.web import ApplicationHandler


class ExplodingConfiguredClient:
    configured = True

    def __init__(self) -> None:
        self.health_calls = 0
        self.send_calls = 0

    def health_check(self) -> dict[str, object]:
        self.health_calls += 1
        raise AssertionError("finalization must not call the remote health check")

    def send_batch(self, _payloads: list[dict[str, object]]) -> list[dict[str, object]]:
        self.send_calls += 1
        raise AssertionError("finalization must not send data to the remote service")


class JsonClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.cookie: str | None = None

    def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        body = json.dumps(payload or {}).encode("utf-8")
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        content = response.read()
        set_cookie = response.getheader("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";", 1)[0]
        status = response.status
        connection.close()
        return status, json.loads(content)


class OfflineFinalizationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.database = SQLiteDatabase(root / "offline.db")
        self.database.initialize()
        collaborators = ColaboradorRepository(self.database)
        collaborators.create("001234", "COLABORADOR OFFLINE")
        settings = AppSettings(temporary_directory=root / "uploads")

        class TestHandler(ApplicationHandler):
            pass

        TestHandler.controller = ConferenceController(
            ConferenceService(
                PalletRepository(self.database),
                PalletFileImporter(),
                settings,
            )
        )
        TestHandler.acesso_service = AcessoService(collaborators, settings)
        TestHandler.sessions = SessionManager()
        TestHandler.max_json_bytes = settings.max_json_bytes
        TestHandler.automatic_report_service = AutomaticReportService(
            settings.max_upload_bytes,
            (root / "Downloads",),
        )
        self.remote = ExplodingConfiguredClient()
        TestHandler.sync_service = SyncService(
            SyncRepository(self.database),
            self.remote,
            sleeper=lambda _seconds: None,
        )

        from http.server import ThreadingHTTPServer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = JsonClient("127.0.0.1", self.server.server_port)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tempdir.cleanup()

    @staticmethod
    def _import_payload(filename: str, carton: str) -> dict[str, object]:
        return {
            "filename": filename,
            "content_base64": b64encode(
                f"CAIXA_ESTOQUE\n{carton}\n".encode("utf-8")
            ).decode("ascii"),
            "agenda": "AGENDA TESTE",
            "origin": "PORTAL",
            "operation": "DIGITAL",
        }

    def test_finish_is_offline_and_releases_a_genuinely_different_import(self) -> None:
        self.assertEqual(
            200,
            self.client.request("POST", "/api/access", {"matricula": "001234"})[0],
        )
        created_status, created_body = self.client.request(
            "POST",
            "/api/conferences",
            self._import_payload("primeira.csv", "000001"),
        )
        self.assertEqual(201, created_status)
        first_id = str(created_body["data"]["public_id"])
        self.assertEqual(
            200,
            self.client.request("POST", f"/api/conferences/{first_id}/start")[0],
        )
        self.assertEqual(
            200,
            self.client.request(
                "POST",
                f"/api/conferences/{first_id}/scan",
                {"caixa_estoque": "000001"},
            )[0],
        )

        finish_status, finish_body = self.client.request(
            "POST", f"/api/conferences/{first_id}/finish"
        )

        self.assertEqual(200, finish_status)
        self.assertEqual("FINALIZADA", finish_body["data"]["workflow_status"])
        self.assertEqual(0, self.remote.health_calls)
        self.assertEqual(0, self.remote.send_calls)
        with self.database.connection() as connection:
            state = connection.execute(
                """
                SELECT state.status
                FROM conference_sync_state state
                INNER JOIN pallets pallet ON pallet.id = state.pallet_id
                WHERE pallet.public_id = ?
                """,
                (first_id,),
            ).fetchone()
        self.assertEqual("PENDENTE", state["status"])

        second_status, second_body = self.client.request(
            "POST",
            "/api/conferences",
            self._import_payload("segunda.csv", "000002"),
        )

        self.assertEqual(201, second_status)
        self.assertEqual("created", second_body["data"]["action"])
        self.assertNotEqual(first_id, second_body["data"]["public_id"])
        self.assertEqual("000002", second_body["data"]["cartons"][0]["caixa_estoque"])
        self.assertEqual(0, self.remote.health_calls)
        self.assertEqual(0, self.remote.send_calls)


class ScriptedStopEvent:
    def __init__(self, wait_results: list[bool]) -> None:
        self.wait_results = deque(wait_results)

    def wait(self, _timeout: float) -> bool:
        return self.wait_results.popleft() if self.wait_results else True


class SchedulerStartupFailureTests(unittest.TestCase):
    def test_prepare_failure_does_not_prevent_the_next_automatic_cycle(self) -> None:
        class Service:
            def __init__(self) -> None:
                self.prepare_calls = 0
                self.triggers: list[str] = []

            def prepare(self) -> None:
                self.prepare_calls += 1
                raise ConnectionError("Google is unavailable during startup")

            def sync_all_pending(self, *, trigger: str = "manual") -> object:
                self.triggers.append(trigger)
                return object()

        service = Service()
        scheduler = SyncScheduler(service, interval_seconds=1800)
        scheduler._stop_event = ScriptedStopEvent([False, True])

        with self.assertLogs("conferencia.sync.scheduler", level="WARNING") as captured:
            scheduler._run()

        self.assertEqual(1, service.prepare_calls)
        self.assertEqual(["automatic"], service.triggers)
        self.assertIn("sync_startup_check_failed", "\n".join(captured.output))


class RecordingDatabase:
    def __init__(self) -> None:
        self.initialize_calls = 0

    def initialize(self) -> None:
        self.initialize_calls += 1


class RecordingScheduler:
    instances: list["RecordingScheduler"] = []

    def __init__(self, service: SyncService, *, interval_seconds: float) -> None:
        self.service = service
        self.interval_seconds = interval_seconds
        self.start_calls = 0
        self.stop_calls = 0
        self.__class__.instances.append(self)

    def start(self) -> bool:
        self.start_calls += 1
        return True

    def stop(self) -> None:
        self.stop_calls += 1


class RecordingServer:
    instances: list["RecordingServer"] = []

    def __init__(self, address: tuple[str, int], handler: type) -> None:
        self.address = address
        self.handler = handler
        self.serve_calls = 0
        self.close_calls = 0
        self.__class__.instances.append(self)

    def serve_forever(self) -> None:
        self.serve_calls += 1

    def server_close(self) -> None:
        self.close_calls += 1


class AppCompositionTests(unittest.TestCase):
    def test_main_shares_one_sync_service_and_starts_one_scheduler(self) -> None:
        settings = SimpleNamespace(
            server_port=8080,
            max_upload_bytes=10 * 1024 * 1024,
            max_json_bytes=15 * 1024 * 1024,
            temporary_directory=Path("unused-test-uploads"),
            apps_script_url="https://example.invalid/exec",
            sync_shared_secret="integration-test-secret",
            sync_connect_timeout_seconds=5.0,
            sync_read_timeout_seconds=30.0,
            sync_batch_size=10,
            sync_max_attempts=3,
            sync_retry_delay_seconds=2.0,
            sync_reservation_timeout_seconds=900,
            sync_interval_minutes=30,
        )
        database = RecordingDatabase()

        class TestApplicationHandler:
            pass

        RecordingScheduler.instances.clear()
        RecordingServer.instances.clear()
        with (
            patch.object(app, "is_supported_python", return_value=True),
            patch.object(
                app.argparse.ArgumentParser,
                "parse_args",
                return_value=SimpleNamespace(host="127.0.0.1", port=8080),
            ),
            patch.object(app, "ThreadingHTTPServer", RecordingServer),
            patch(
                "conferencia.infrastructure.database.SQLiteDatabase",
                return_value=database,
            ),
            patch(
                "conferencia.infrastructure.settings.AppSettings",
                return_value=settings,
            ),
            patch("conferencia.infrastructure.logging_utils.configure_logging"),
            patch("conferencia.sync.scheduler.SyncScheduler", RecordingScheduler),
            patch("conferencia.web.ApplicationHandler", TestApplicationHandler),
        ):
            result = app.main()

        self.assertEqual(0, result)
        self.assertEqual(1, database.initialize_calls)
        self.assertEqual(1, len(RecordingScheduler.instances))
        scheduler = RecordingScheduler.instances[0]
        self.assertIs(TestApplicationHandler.sync_service, scheduler.service)
        self.assertEqual(1800, scheduler.interval_seconds)
        self.assertEqual(1, scheduler.start_calls)
        self.assertEqual(1, scheduler.stop_calls)
        self.assertEqual(1, len(RecordingServer.instances))
        server = RecordingServer.instances[0]
        self.assertIs(TestApplicationHandler, server.handler)
        self.assertEqual(1, server.serve_calls)
        self.assertEqual(1, server.close_calls)


if __name__ == "__main__":
    unittest.main()
