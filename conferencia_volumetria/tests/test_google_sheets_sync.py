from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import tempfile
import threading
import unittest
from base64 import b64encode
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

from conferencia.controllers.conference_controller import ConferenceController
from conferencia.domain.entities import CollaboratorContext, ConferenceImport
from conferencia.infrastructure.database import SQLiteDatabase
from conferencia.infrastructure.session_manager import SessionManager
from conferencia.infrastructure.settings import AppSettings
from conferencia.repositories.colaborador_repository import ColaboradorRepository
from conferencia.repositories.pallet_repository import PalletRepository
from conferencia.services.acesso_service import AcessoService
from conferencia.services.conference_service import (
    ConferenceService,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from conferencia.services.google_sheets_sync_service import GoogleSheetsSyncService
from conferencia.web import ApplicationHandler


class SyncReader:
    def __init__(self, codes: list[str]) -> None:
        self.codes = codes

    def read_carton_codes(self, file_path: Path, extension: str) -> list[str]:
        return self.codes


class GoogleSheetsSyncServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.tempdir.name) / "sync.db")
        self.database.initialize()
        collaborator = ColaboradorRepository(self.database).create(
            "000321", "OPERADOR SINCRONIZACAO", "T2"
        )
        self.actor = CollaboratorContext(
            int(collaborator["id"]),
            str(collaborator["matricula"]),
            str(collaborator["nome"]),
            str(collaborator["turno"]),
        )
        self.settings = AppSettings(
            temporary_directory=Path(self.tempdir.name) / "uploads",
            google_apps_script_url="https://script.google.com/a/macros/fisia.com.br/s/DEPLOYMENT/exec",
            google_sync_secret="segredo-de-teste",
            sync_retry_after_seconds=300,
        )
        self.repository = PalletRepository(self.database)
        self.conference_service = ConferenceService(
            self.repository, SyncReader(["000001", "CX-002"]), self.settings
        )
        created = self.conference_service.import_pallet(
            ConferenceImport(
                self.actor,
                "palete.csv",
                b64encode(b"arquivo-real-local").decode(),
                "PORTAL",
                "DIGITAL",
                "AGENDA TESTE",
            )
        )
        self.public_id = created["public_id"]
        self.sync_service = GoogleSheetsSyncService(self.database, self.settings)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def finalize(self) -> None:
        self.conference_service.start_pallet(self.public_id, self.actor)
        self.conference_service.scan_carton(self.public_id, "1", self.actor)
        self.conference_service.scan_carton(self.public_id, "000001", self.actor)
        self.conference_service.scan_carton(self.public_id, "SOBRA-01", self.actor)
        self.conference_service.scan_carton(self.public_id, "CX-002", self.actor)
        self.conference_service.finish_pallet(self.public_id, self.actor)

    def prepare(self) -> dict[str, object]:
        return self.sync_service.prepare(
            self.public_id,
            self.actor,
            "http://127.0.0.1:8080/sincronizacao/confirmar/",
        )

    def test_finalized_conference_builds_complete_deterministic_signed_payload(self) -> None:
        self.finalize()
        prepared = self.prepare()
        payload_text = str(prepared["payload"])
        payload = json.loads(payload_text)
        self.assertEqual(
            payload_text,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        self.assertEqual({"conferencia", "caixas", "bipagens"}, set(payload))
        conference = payload["conferencia"]
        self.assertEqual(self.public_id, conference["id_conferencia"])
        self.assertEqual("000321", conference["matricula"])
        self.assertEqual("T2", conference["turno"])
        self.assertEqual("FINALIZADA", conference["status_conferencia"])
        self.assertEqual(2, conference["total_esperadas"])
        self.assertEqual(4, conference["total_bipagens"])
        self.assertEqual(2, conference["total_ok"])
        self.assertEqual(1, conference["total_sobras"])
        self.assertEqual(1, conference["total_duplicadas"])
        self.assertEqual(["000001", "CX-002"], [item["codigo_original"] for item in payload["caixas"]])
        self.assertEqual("000001", payload["bipagens"][0]["codigo_lido"])
        expected_signature = hmac.new(
            b"segredo-de-teste", payload_text.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        self.assertEqual(expected_signature, prepared["signature"])
        self.assertEqual("http://127.0.0.1:8080", prepared["application_origin"])
        self.assertTrue(str(prepared["apps_script_url"]).endswith("/exec"))
        self.assertEqual(
            "http://127.0.0.1:8080/sincronizacao/confirmar/",
            prepared["return_url"],
        )
        expected_popup_token = self.sync_service.sign(
            f"POPUP|{self.public_id}|http://127.0.0.1:8080|"
            f"http://127.0.0.1:8080/sincronizacao/confirmar/|{prepared['nonce']}"
        )
        self.assertEqual(expected_popup_token, prepared["popup_token"])
        self.assertNotIn("bridge_url", prepared)
        self.assertNotIn("bridge_token", prepared)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT status_sincronizacao, tentativas_sincronizacao, sincronizacao_iniciada_em FROM pallets WHERE public_id = ?",
                (self.public_id,),
            ).fetchone()
        self.assertEqual("SINCRONIZANDO", row["status_sincronizacao"])
        self.assertEqual(1, row["tentativas_sincronizacao"])
        self.assertTrue(str(row["sincronizacao_iniciada_em"]).endswith("Z"))

    def test_open_and_cancelled_conferences_cannot_prepare_sync(self) -> None:
        with self.assertRaises(ConflictError) as opened:
            self.prepare()
        self.assertEqual("CONFERENCIA_NAO_FINALIZADA", opened.exception.code)
        self.conference_service.cancel_pallet(self.public_id, self.actor)
        with self.assertRaises(ConflictError) as cancelled:
            self.prepare()
        self.assertEqual("CONFERENCIA_NAO_FINALIZADA", cancelled.exception.code)

    def test_other_collaborator_can_prepare_global_pending_sync(self) -> None:
        self.finalize()
        other = ColaboradorRepository(self.database).create("000999", "OUTRO", "T1")
        other_actor = CollaboratorContext(
            int(other["id"]), str(other["matricula"]), str(other["nome"]), str(other["turno"])
        )
        prepared = self.sync_service.prepare(
            self.public_id, other_actor, "http://127.0.0.1:8080/sincronizacao/confirmar/"
        )
        self.assertEqual(self.public_id, prepared["public_id"])
        self.assertEqual("000321", json.loads(prepared["payload"])["conferencia"]["matricula"])

    def test_valid_receipt_updates_status_date_and_stored_receipt(self) -> None:
        self.finalize()
        prepared = self.prepare()
        synchronized_at = "2026-08-04T11:45:12-03:00"
        signature = self.sync_service.sign(
            f"{self.public_id}|SINCRONIZADO|{synchronized_at}"
        )
        self.sync_service.confirm(
            self.public_id, "SINCRONIZADO", synchronized_at, signature,
            prepared["attempt_id"], prepared["nonce"], False, self.actor
        )
        details = self.repository.details(self.public_id)
        self.assertEqual("SINCRONIZADO", details["synchronization"]["status"])
        self.assertEqual("04/08/2026 11:45:12", details["synchronization"]["synchronized_at"])
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT sincronizado_em, recibo_sincronizacao, ultimo_erro_sincronizacao FROM pallets WHERE public_id = ?",
                (self.public_id,),
            ).fetchone()
        self.assertEqual(synchronized_at, row["sincronizado_em"])
        self.assertEqual(signature, json.loads(row["recibo_sincronizacao"])["assinatura_recibo"])
        self.assertIsNone(row["ultimo_erro_sincronizacao"])

    def test_invalid_or_other_conference_receipt_is_rejected_without_data_loss(self) -> None:
        self.finalize()
        prepared = self.prepare()
        synchronized_at = "2026-08-04T11:45:12-03:00"
        with self.assertRaises(ValidationError):
            self.sync_service.confirm(
                self.public_id, "SINCRONIZADO", synchronized_at, "0" * 64,
                prepared["attempt_id"], prepared["nonce"], False, self.actor
            )
        signature_for_other = self.sync_service.sign(
            f"CONF-OUTRA|SINCRONIZADO|{synchronized_at}"
        )
        with self.assertRaises(ValidationError):
            self.sync_service.confirm(
                self.public_id,
                "SINCRONIZADO",
                synchronized_at,
                signature_for_other,
                prepared["attempt_id"], prepared["nonce"], False, self.actor,
            )
        with self.database.connection() as connection:
            counts = connection.execute(
                """
                SELECT p.status_sincronizacao,
                       (SELECT COUNT(*) FROM expected_cartons WHERE pallet_id = p.id) AS caixas,
                       (SELECT COUNT(*) FROM scan_events WHERE pallet_id = p.id) AS bipagens
                FROM pallets p WHERE p.public_id = ?
                """,
                (self.public_id,),
            ).fetchone()
        self.assertEqual("SINCRONIZANDO", counts["status_sincronizacao"])
        self.assertEqual((2, 4), (counts["caixas"], counts["bipagens"]))

    def test_repeated_click_is_blocked_and_already_synced_does_not_send_again(self) -> None:
        self.finalize()
        prepared = self.prepare()
        with self.assertRaises(ConflictError) as repeated:
            self.prepare()
        self.assertEqual("SINCRONIZACAO_EM_ANDAMENTO", repeated.exception.code)
        synchronized_at = "2026-08-04T11:45:12-03:00"
        signature = self.sync_service.sign(
            f"{self.public_id}|SINCRONIZADO|{synchronized_at}"
        )
        self.sync_service.confirm(
            self.public_id, "SINCRONIZADO", synchronized_at, signature,
            prepared["attempt_id"], prepared["nonce"], False, self.actor
        )
        prepared = self.prepare()
        self.assertTrue(prepared["already_synced"])
        with self.database.connection() as connection:
            attempts = connection.execute(
                "SELECT tentativas_sincronizacao FROM pallets WHERE public_id = ?",
                (self.public_id,),
            ).fetchone()[0]
        self.assertEqual(1, attempts)

    def test_known_configuration_error_is_recorded_and_can_be_retried(self) -> None:
        self.finalize()
        unconfigured = GoogleSheetsSyncService(
            self.database,
            AppSettings(
                temporary_directory=Path(self.tempdir.name) / "uploads",
                google_apps_script_url="",
                google_sync_secret="",
            ),
        )
        with self.assertRaises(ConflictError):
            unconfigured.prepare(
                self.public_id,
                self.actor,
                "http://127.0.0.1:8080/sincronizacao/confirmar/",
            )
        details = self.repository.details(self.public_id)
        self.assertEqual("PENDENTE", details["synchronization"]["status"])
        self.assertEqual(1, details["synchronization"]["attempts"])
        self.assertIn("não está configurada", details["synchronization"]["last_error"])
        prepared = self.prepare()
        self.assertFalse(prepared["already_synced"])

    def test_known_remote_error_preserves_data_and_allows_immediate_retry(self) -> None:
        self.finalize()
        prepared = self.prepare()
        failed = self.sync_service.fail(
            self.public_id, "GOOGLE_SYNC_FAILED", prepared["attempt_id"], prepared["nonce"], self.actor
        )
        self.assertEqual("ERRO", failed["status"])
        with self.database.connection() as connection:
            state = connection.execute(
                """
                SELECT p.status_sincronizacao, p.ultimo_erro_sincronizacao,
                       (SELECT COUNT(*) FROM expected_cartons WHERE pallet_id = p.id) AS caixas,
                       (SELECT COUNT(*) FROM scan_events WHERE pallet_id = p.id) AS bipagens
                FROM pallets p WHERE p.public_id = ?
                """,
                (self.public_id,),
            ).fetchone()
        self.assertEqual("PENDENTE", state["status_sincronizacao"])
        self.assertEqual((2, 4), (state["caixas"], state["bipagens"]))
        self.assertIn("não concluiu", state["ultimo_erro_sincronizacao"])
        self.assertFalse(self.prepare()["already_synced"])

    def test_invalid_payload_structure_is_rejected_before_sending(self) -> None:
        with self.assertRaises(ConflictError):
            self.sync_service._validate_payload(
                {"conferencia": {"id_conferencia": "INVÁLIDO"}, "caixas": [], "bipagens": []}
            )


class GoogleSheetsSyncHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.tempdir.name) / "http-sync.db")
        self.database.initialize()
        collaborator = ColaboradorRepository(self.database).create(
            "000321", "OPERADOR SINCRONIZACAO", "T2"
        )
        self.actor = CollaboratorContext(
            int(collaborator["id"]), str(collaborator["matricula"]),
            str(collaborator["nome"]), str(collaborator["turno"]),
        )
        self.settings = AppSettings(
            temporary_directory=Path(self.tempdir.name) / "uploads",
            google_apps_script_url="https://script.google.com/a/macros/fisia.com.br/s/DEPLOYMENT/exec",
            google_sync_secret="segredo-de-teste",
            sync_retry_after_seconds=300,
        )
        repository = PalletRepository(self.database)
        self.conference_service = ConferenceService(
            repository, SyncReader(["000001"]), self.settings
        )
        created = self.conference_service.import_pallet(
            ConferenceImport(
                self.actor, "palete.csv", b64encode(b"http-sync").decode(),
                "PORTAL", "DIGITAL", "AGENDA TESTE",
            )
        )
        self.public_id = created["public_id"]
        self.conference_service.start_pallet(self.public_id, self.actor)
        self.conference_service.scan_carton(self.public_id, "000001", self.actor)
        self.conference_service.finish_pallet(self.public_id, self.actor)
        self.sync_service = GoogleSheetsSyncService(self.database, self.settings)

        class TestHandler(ApplicationHandler):
            pass

        TestHandler.controller = ConferenceController(self.conference_service)
        TestHandler.acesso_service = AcessoService(
            ColaboradorRepository(self.database), self.settings
        )
        TestHandler.sessions = SessionManager()
        TestHandler.google_sync_service = self.sync_service
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = "127.0.0.1"
        self.port = self.server.server_port
        self.cookie: str | None = None

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tempdir.cleanup()

    def request(
        self, method: str, path: str, body: bytes = b"", content_type: str = "application/json"
    ) -> tuple[int, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers = {"Content-Type": content_type}
        if self.cookie:
            headers["Cookie"] = self.cookie
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        content = response.read()
        response_headers = dict(response.getheaders())
        if "Set-Cookie" in response_headers:
            self.cookie = response_headers["Set-Cookie"].split(";", 1)[0]
        connection.close()
        return response.status, content, response_headers

    def authenticate(self) -> None:
        status, _, _ = self.request(
            "POST", "/api/access", json.dumps({"matricula": "000321"}).encode()
        )
        self.assertEqual(200, status)

    def test_session_is_required_for_prepare_and_confirmation(self) -> None:
        status, body, _ = self.request(
            "POST", f"/sincronizacao/{self.public_id}/preparar/"
        )
        self.assertEqual(401, status)
        self.assertEqual("COLABORADOR_NAO_IDENTIFICADO", json.loads(body)["error"]["code"])
        status, body, _ = self.request("GET", "/sincronizacao/confirmar/")
        self.assertEqual(401, status)
        self.assertEqual("COLABORADOR_NAO_IDENTIFICADO", json.loads(body)["error"]["code"])
        status, body, _ = self.request(
            "POST", f"/sincronizacao/{self.public_id}/confirmar/", b"{}"
        )
        self.assertEqual(401, status)
        self.assertEqual("COLABORADOR_NAO_IDENTIFICADO", json.loads(body)["error"]["code"])

    def test_prepare_json_contains_popup_data_but_never_secret(self) -> None:
        self.authenticate()
        status, body, headers = self.request(
            "POST", f"/sincronizacao/{self.public_id}/preparar/"
        )
        response_text = body.decode("utf-8")
        response = json.loads(response_text)
        self.assertEqual(200, status)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertTrue(response["success"])
        self.assertIn("payload", response["data"])
        self.assertIn("signature", response["data"])
        self.assertIn("apps_script_url", response["data"])
        self.assertIn("popup_token", response["data"])
        self.assertTrue(response["data"]["apps_script_url"].endswith("/exec"))
        self.assertNotIn("bridge_url", response["data"])
        self.assertNotIn("bridge_token", response["data"])
        self.assertNotIn("segredo-de-teste", response_text)

    def test_valid_http_receipt_updates_sqlite_and_returns_local_completion_page(self) -> None:
        self.authenticate()
        prepare_status, prepare_body, _ = self.request(
            "POST", f"/sincronizacao/{self.public_id}/preparar/"
        )
        self.assertEqual(200, prepare_status)
        prepared = json.loads(prepare_body)["data"]
        synchronized_at = "2026-08-04T11:45:12-03:00"
        signature = self.sync_service.sign(
            f"{self.public_id}|SINCRONIZADO|{synchronized_at}"
        )
        query = urlencode(
            {
                "conference_id": self.public_id,
                "attempt_id": prepared["attempt_id"],
                "status": "SUCCESS",
                "sincronizado_em": synchronized_at,
                "assinatura_recibo": signature,
                "ja_sincronizado": "false",
                "nonce": prepared["nonce"],
            }
        )
        status, body, headers = self.request("GET", f"/sincronizacao/confirmar/?{query}")
        page = body.decode("utf-8")
        self.assertEqual(200, status)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn('"type":"GOOGLE_SYNC_RESULT"', page)
        self.assertIn("window.opener.postMessage(resultado,targetOrigin)", page)
        self.assertNotIn("BroadcastChannel", page)
        self.assertIn("window.close()", page)
        self.assertIn("function notifyAndClose()", page)
        self.assertIn("setTimeout(function(){window.close();},550)", page)
        self.assertNotIn('postMessage(resultado,"*")', page)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT status_sincronizacao, sincronizado_em, recibo_sincronizacao, ultimo_erro_sincronizacao FROM pallets WHERE public_id = ?",
                (self.public_id,),
            ).fetchone()
        self.assertEqual("SINCRONIZADO", row["status_sincronizacao"])
        self.assertEqual(synchronized_at, row["sincronizado_em"])
        self.assertIn(signature, row["recibo_sincronizacao"])
        self.assertIsNone(row["ultimo_erro_sincronizacao"])

        self.assertIn("window.close()", page)

    def test_invalid_get_receipt_is_rejected_without_deleting_local_data(self) -> None:
        self.authenticate()
        prepare_status, prepare_body, _ = self.request(
            "POST", f"/sincronizacao/{self.public_id}/preparar/"
        )
        self.assertEqual(200, prepare_status)
        prepared = json.loads(prepare_body)["data"]
        query = urlencode(
            {
                "conference_id": self.public_id,
                "attempt_id": prepared["attempt_id"],
                "status": "SUCCESS",
                "sincronizado_em": "2026-08-04T11:45:12-03:00",
                "assinatura_recibo": "0" * 64,
                "ja_sincronizado": "false",
                "nonce": prepared["nonce"],
            }
        )
        status, _, _ = self.request("GET", f"/sincronizacao/confirmar/?{query}")
        self.assertEqual(422, status)
        with self.database.connection() as connection:
            state = connection.execute(
                """
                SELECT p.status_sincronizacao,
                       (SELECT COUNT(*) FROM expected_cartons WHERE pallet_id = p.id) AS caixas,
                       (SELECT COUNT(*) FROM scan_events WHERE pallet_id = p.id) AS bipagens
                FROM pallets p WHERE p.public_id = ?
                """,
                (self.public_id,),
            ).fetchone()
        self.assertNotEqual("SINCRONIZADO", state["status_sincronizacao"])
        self.assertEqual((1, 1), (state["caixas"], state["bipagens"]))

    def test_valid_modal_receipt_returns_json_and_invalid_receipt_marks_error(self) -> None:
        self.authenticate()
        prepare_status, prepare_body, _ = self.request(
            "POST", f"/sincronizacao/{self.public_id}/preparar/"
        )
        self.assertEqual(200, prepare_status)
        prepared = json.loads(prepare_body)["data"]
        synchronized_at = "2026-08-04T12:30:00-03:00"
        signature = self.sync_service.sign(
            f"{self.public_id}|SINCRONIZADO|{synchronized_at}"
        )
        receipt = {
            "conference_id": self.public_id,
            "attempt_id": prepared["attempt_id"],
            "nonce": prepared["nonce"],
            "status": "SINCRONIZADO",
            "sincronizado_em": synchronized_at,
            "assinatura_recibo": signature,
        }
        status, body, _ = self.request(
            "POST",
            f"/sincronizacao/{self.public_id}/confirmar/",
            json.dumps(receipt).encode(),
        )
        self.assertEqual(200, status)
        self.assertEqual("SINCRONIZADO", json.loads(body)["data"]["status"])

        with self.database.connection(immediate=True) as connection:
            connection.execute(
                "UPDATE pallets SET status_sincronizacao = 'SINCRONIZANDO' WHERE public_id = ?",
                (self.public_id,),
            )
        receipt["assinatura_recibo"] = "0" * 64
        status, body, _ = self.request(
            "POST",
            f"/sincronizacao/{self.public_id}/confirmar/",
            json.dumps(receipt).encode(),
        )
        self.assertEqual(409, status)
        self.assertEqual("SYNC_ATTEMPT_ALREADY_CONSUMED", json.loads(body)["error"]["code"])
        with self.database.connection() as connection:
            state = connection.execute(
                "SELECT status_sincronizacao FROM pallets WHERE public_id = ?",
                (self.public_id,),
            ).fetchone()[0]
        self.assertEqual("SINCRONIZANDO", state)


class AppsScriptContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.code = (root / "google_apps_script" / "Code.gs").read_text(encoding="utf-8")

    def test_apps_script_has_domain_flow_security_and_idempotency_contract(self) -> None:
        for value in (
            "function doGet(event)",
            "function doPost(event)",
            "Session.getActiveUser().getEmail()",
            "@fisia\\.com\\.br$",
            "PropertiesService.getScriptProperties()",
            "SYNC_SECRET",
            "PLANILHA_ID",
            "LockService.getScriptLock()",
            "constantTimeEquals_",
            "SpreadsheetApp.flush()",
            "findConference_",
            "A conferência já estava sincronizada.",
            "function popupRedirectPage_(result, returnUrl, nonce, attemptId)",
            "validateReturnUrl_(parameters.return_url, parentOrigin)",
            "target=\"_top\"",
            "window.top.location.href=url",
            "conference_id: receipt.id_conferencia",
            "assinatura_recibo: receipt.assinatura_recibo",
            "ja_sincronizado: result.already_existing ? 'true' : 'false'",
            "'POPUP|' + conferenceId + '|' + parentOrigin + '|' + returnUrl + '|' + nonce",
        ):
            self.assertIn(value, self.code)
        self.assertNotIn("CHAVE_SECRETA", self.code)
        self.assertLess(
            self.code.index("appendRows_(boxesSheet"),
            self.code.index("appendRows_(conferencesSheet"),
        )
        self.assertLess(
            self.code.index("appendRows_(scansSheet"),
            self.code.index("appendRows_(conferencesSheet"),
        )
        self.assertNotIn('postMessage(message,"*")', self.code)
        self.assertNotIn("XFrameOptionsMode", self.code)
        self.assertNotIn("bridgePage_", self.code)
        self.assertNotIn("synchronizeFromModal", self.code)
        self.assertNotIn("window.opener.postMessage", self.code)
        self.assertIn("window.close()", self.code)

    def test_apps_script_uses_exact_headers_and_text_formats_for_codes(self) -> None:
        for sheet in ("CONFERENCIAS", "CAIXAS_CONFERENCIA", "BIPAGENS"):
            self.assertIn("'" + sheet + "'", self.code)
        for header in (
            "ID_CONFERENCIA",
            "CODIGO_ORIGINAL",
            "CODIGO_NORMALIZADO",
            "CODIGO_LIDO",
            "SINCRONIZADO_EM",
        ):
            self.assertIn("'" + header + "'", self.code)
        self.assertIn("setNumberFormat('@')", self.code)
        self.assertIn("America/Sao_Paulo", self.code)
        self.assertIn("conferenceId + '|SINCRONIZADO|' + synchronizedAt", self.code)

    def test_apps_script_redirects_top_window_to_signed_local_receipt(self) -> None:
        flush = self.code.index("SpreadsheetApp.flush()")
        receipt = self.code.index("const receiptText = conferenceId + '|SINCRONIZADO|'", flush)
        processing = self.code.index("result = performSynchronization_")
        redirect = self.code.index("return popupRedirectPage_(result, returnUrl, nonce, attemptId)")
        form = self.code.index('target="_top"', redirect)
        top = self.code.index("window.top.location.href=url", form)
        self.assertLess(flush, receipt)
        self.assertLess(processing, redirect)
        self.assertLess(redirect, form)
        self.assertLess(form, top)
        self.assertNotIn("VOLTAR AO SISTEMA", self.code)


@unittest.skip("Contrato do popup bloqueante substituído por sincronização manual não bloqueante.")
class SyncModalContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.html = (root / "index.html").read_text(encoding="utf-8")
        cls.javascript = (root / "static" / "js" / "conference.js").read_text(encoding="utf-8")
        cls.styles = (root / "static" / "styles.css").read_text(encoding="utf-8")

    def test_sync_opens_blocking_modal_and_popup_directly_from_click(self) -> None:
        for identifier in (
            "sync-modal", "sync-modal-title", "sync-modal-message", "sync-progress",
            "sync-percent", "sync-spinner", "sync-error", "sync-retry",
            "sync-modal-close",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn("if (!syncDialog.open) syncDialog.showModal()", self.javascript)
        self.assertIn('syncModal.addEventListener("cancel"', self.javascript)
        self.assertNotIn("<iframe", self.html)
        self.assertNotIn("sync-bridge", self.javascript)
        modal_open = self.javascript.index("syncDialog.showModal()")
        popup_open = self.javascript.index("popupSincronizacao = window.open", modal_open)
        first_await = self.javascript.index("await", popup_open)
        self.assertLess(popup_open, first_await)
        self.assertIn('documentRef.createElement("form")', self.javascript)
        self.assertIn('form.method = "POST"', self.javascript)
        self.assertIn("form.action = prepared.apps_script_url", self.javascript)
        self.assertIn("return_url: prepared.return_url", self.javascript)
        self.assertIn('"google_sync"', self.javascript)
        self.assertIn("GOOGLE_POPUP_BLOCKED", self.javascript)

    def test_progress_uses_real_events_and_reaches_100_only_after_local_confirmation(self) -> None:
        for percent in (0, 10, 30, 50, 60, 85, 95, 100):
            self.assertIn(f"setSyncProgress({percent}", self.javascript)
        confirmation = self.javascript.index("const confirmed = await api")
        success = self.javascript.index("await completeSynchronization", confirmation)
        self.assertLess(confirmation, success)
        self.assertIn('confirmed.synchronization?.status !== "SINCRONIZADO"', self.javascript)
        self.assertIn("popupSincronizacao?.closed", self.javascript)
        self.assertNotIn("GOOGLE_AUTH_TIMEOUT_MS", self.javascript)
        self.assertNotIn("GOOGLE_RESPONSE_TIMEOUT", self.javascript)

    def test_success_waits_two_seconds_closes_modal_and_releases_new_import(self) -> None:
        success_function = self.javascript.index("async function completeSynchronization")
        popup_close = self.javascript.index("popupSincronizacao.close()", success_function)
        progress = self.javascript.index("setSyncProgress(100", popup_close)
        delay = self.javascript.index("setTimeout(resolve, 2000)", success_function)
        close = self.javascript.index('$("#sync-modal").close()', delay)
        release = self.javascript.index("showImportCard()", close)
        self.assertLess(popup_close, progress)
        self.assertLess(progress, delay)
        self.assertLess(delay, close)
        self.assertLess(close, release)
        self.assertIn("awaitingSynchronization", self.javascript)
        self.assertIn("state.latest_conference?.synchronization?.status", self.javascript)
        self.assertIn("popupSincronizacao = null", self.javascript)

    def test_local_confirmation_checks_sqlite_before_one_hundred_percent(self) -> None:
        finalizer = self.javascript.index("async function finalizeSynchronizationSuccess")
        confirmation = self.javascript.index("const confirmed = await api", finalizer)
        validation = self.javascript.index('confirmed.synchronization?.status !== "SINCRONIZADO"', confirmation)
        completion = self.javascript.index("await completeSynchronization", validation)
        self.assertLess(confirmation, validation)
        self.assertLess(validation, completion)
        handler = self.javascript.index("async function handleLocalSyncConfirmation")
        self.assertIn("await finalizeSynchronizationSuccess", self.javascript[handler:])

    def test_local_messages_require_exact_origin_popup_source_id_and_nonce(self) -> None:
        self.assertIn("event.source !== popupSincronizacao", self.javascript)
        self.assertIn("event.origin !== window.location.origin", self.javascript)
        self.assertIn("data.nonce !== syncContext.nonce", self.javascript)
        self.assertIn('data.conference_id !== syncContext.publicId', self.javascript)
        self.assertIn('data.attempt_id !== syncContext.prepared?.attempt_id', self.javascript)
        self.assertIn('data.status !== "SUCCESS"', self.javascript)
        self.assertIn('data.type !== "GOOGLE_SYNC_RESULT"', self.javascript)
        self.assertIn("new BroadcastChannel(`google-sync-${publicId}`)", self.javascript)
        self.assertIn("if (!synchronizing || sincronizacaoConfirmada) return", self.javascript)
        self.assertIn("sync-spinner", self.styles)

    def test_distinct_popup_errors_keep_retry_and_close_controls(self) -> None:
        for code in (
            "GOOGLE_POPUP_BLOCKED", "GOOGLE_WINDOW_CLOSED", "GOOGLE_BRIDGE_TIMEOUT",
            "GOOGLE_RECEIPT_INVALID", "LOCAL_CONFIRMATION_FAILED",
        ):
            self.assertIn(code, self.javascript)
        self.assertIn('$("#sync-modal-actions").hidden = false', self.javascript)
        self.assertIn("Tentar sincronizar novamente", self.javascript)
        timeout = self.javascript.index('"GOOGLE_BRIDGE_TIMEOUT"')
        timeout_end = self.javascript.index("}, GOOGLE_BRIDGE_TIMEOUT_MS)", timeout)
        self.assertNotIn("GOOGLE_AUTH_REQUIRED", self.javascript[timeout:timeout_end])


if __name__ == "__main__":
    unittest.main()
