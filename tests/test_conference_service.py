from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import zipfile
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

from conferencia.controllers.conference_controller import ConferenceController
from conferencia.domain.entities import CollaboratorContext, ConferenceImport
from conferencia.infrastructure.database import SQLiteDatabase
from conferencia.infrastructure.session_manager import SessionManager
from conferencia.infrastructure.settings import AppSettings
from conferencia.readers.excel_reader import ExcelReadError, OpenpyxlPalletReader
from conferencia.repositories.colaborador_repository import ColaboradorRepository
from conferencia.repositories.pallet_repository import PalletRepository
from conferencia.services.acesso_service import AcessoService
from conferencia.services.conference_service import (
    ConflictError,
    InvalidScanError,
    ValidationError,
    ConferenceService,
)
from conferencia.web import ApplicationHandler


class FixedPalletReader:
    def read_carton_codes(self, file_path: Path, extension: str) -> list[str]:
        return ["000001", "CX-002", "CX-003"]


def minimal_xlsx(codes: list[str], *, numeric: bool = False) -> bytes:
    rows = [
        '<row r="1"><c r="A1" t="inlineStr"><is><t>CAIXA ESTOQUE</t></is></c></row>'
    ]
    for line, code in enumerate(codes, start=2):
        cell = (
            f'<c r="A{line}"><v>{code}</v></c>'
            if numeric
            else f'<c r="A{line}" t="inlineStr"><is><t>{code}</t></is></c>'
        )
        rows.append(f'<row r="{line}">{cell}</row>')
    parts = {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8"?>
            <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
              <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
              <Default Extension="xml" ContentType="application/xml"/>
              <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
              <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
            </Types>""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
            </Relationships>""",
        "xl/workbook.xml": """<?xml version="1.0" encoding="UTF-8"?>
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets><sheet name="Palete" sheetId="1" r:id="rId1"/></sheets>
            </workbook>""",
        "xl/_rels/workbook.xml.rels": """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
            </Relationships>""",
        "xl/worksheets/sheet1.xml": f"""<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData>{''.join(rows)}</sheetData>
            </worksheet>""",
    }
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return output.getvalue()


class ConferenceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.tempdir.name) / "test.db")
        self.database.initialize()
        collaborator = ColaboradorRepository(self.database).create("000123", "OPERADOR TESTE")
        self.actor = CollaboratorContext(
            int(collaborator["id"]),
            str(collaborator["matricula"]),
            str(collaborator["nome"]),
        )
        self.repository = PalletRepository(self.database)
        settings = AppSettings(temporary_directory=Path(self.tempdir.name) / "uploads")
        self.service = ConferenceService(self.repository, FixedPalletReader(), settings)
        self.created = self.service.import_pallet(
            ConferenceImport(
                collaborator=self.actor,
                filename="palete.xlsx",
                content_base64=b64encode(b"excel").decode(),
                origin="PORTAL",
                operation="DIGITAL",
            )
        )
        self.public_id = self.created["public_id"]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def start(self) -> dict:
        return self.service.start_pallet(self.public_id, self.actor)

    def test_import_is_ready_and_preserves_session_actor(self) -> None:
        self.assertEqual("READY", self.created["status"])
        self.assertIsNone(self.created["started_at"])
        self.assertEqual("000123", self.created["collaborator"]["registration"])
        self.assertEqual("palete.xlsx", self.created["source_filename"])
        self.assertEqual(
            {"origin": "PORTAL", "operation": "DIGITAL", "shift": "ADM"},
            {key: self.created["importation"][key] for key in ("origin", "operation", "shift")},
        )
        self.assertIsNotNone(self.created["importation"]["imported_at"])
        self.assertEqual(
            ["000001", "CX-002", "CX-003"],
            [box["caixa_estoque"] for box in self.created["cartons"]],
        )

    def test_start_after_render_contract_is_idempotent(self) -> None:
        first = self.start()
        second = self.service.start_pallet(self.public_id, self.actor)
        self.assertEqual("IN_PROGRESS", first["status"])
        self.assertEqual(first["started_at"], second["started_at"])
        self.assertEqual(first["active_attempt"]["id"], second["active_attempt"]["id"])

    def test_concurrent_imports_create_only_one_active_conference(self) -> None:
        self.service.cancel_pallet(self.public_id, self.actor)

        def import_once() -> str:
            try:
                return self.service.import_pallet(
                    ConferenceImport(
                        collaborator=self.actor,
                        filename="concorrente.xlsx",
                        content_base64=b64encode(b"conteudo-concorrente").decode(),
                        origin="TL",
                        operation="CROSS",
                    )
                )["public_id"]
            except ConflictError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _item: import_once(), range(2)))
        self.assertEqual(1, sum(result == "CONFERENCIA_ATIVA" for result in results))
        self.assertEqual(1, sum(result.startswith("CONF-") for result in results))
        self.assertTrue(self.service.active_pallet(self.actor)["has_active_conference"])

    def test_expected_duplicate_and_divergent_results(self) -> None:
        self.start()
        matched = self.service.scan_carton(self.public_id, "  000001\r\n", self.actor)
        duplicate = self.service.scan_carton(self.public_id, "000001", self.actor)
        divergent = self.service.scan_carton(self.public_id, "FORA-001", self.actor)
        repeated_extra = self.service.scan_carton(self.public_id, "FORA-001", self.actor)

        self.assertEqual(("CONFERIDA", "Conferência OK."), (matched["result"], matched["message"]))
        self.assertEqual(("DUPLICADA", "Caixa já conferida."), (duplicate["result"], duplicate["message"]))
        self.assertEqual("DIVERGENTE", divergent["result"])
        self.assertIn("código não esperado", divergent["message"])
        self.assertEqual("DUPLICADA", repeated_extra["result"])
        self.assertEqual(1, repeated_extra["summary"]["total_confirmed"])
        self.assertEqual(1, repeated_extra["summary"]["total_extra"])
        self.assertEqual(2, repeated_extra["summary"]["total_duplicate_reads"])

    def test_empty_scan_does_not_create_event(self) -> None:
        self.start()
        with self.assertRaises(InvalidScanError):
            self.service.scan_carton(self.public_id, " \r\n", self.actor)
        with self.database.connection() as connection:
            total = connection.execute("SELECT COUNT(*) FROM scan_events").fetchone()[0]
        self.assertEqual(0, total)

    def test_finish_is_blocked_by_missing_boxes_with_exact_details(self) -> None:
        self.start()
        self.service.scan_carton(self.public_id, "000001", self.actor)
        with self.assertRaises(ConflictError) as raised:
            self.service.finish_pallet(self.public_id, self.actor)
        self.assertEqual("CONFERENCIA_COM_PENDENCIAS", raised.exception.code)
        self.assertEqual({"faltantes": 2, "divergentes": 0}, raised.exception.details)

    def test_full_coverage_waits_for_manual_finish_and_accepts_extra_scan(self) -> None:
        self.start()
        for code in ("000001", "CX-002", "CX-003"):
            self.service.scan_carton(self.public_id, code, self.actor)
        awaiting = self.service.get_pallet(self.public_id)
        self.assertEqual("IN_PROGRESS", awaiting["status"])
        self.assertEqual("AGUARDANDO_FINALIZACAO", awaiting["workflow_status"])
        self.assertIsNone(awaiting["finished_at"])
        extra = self.service.scan_carton(self.public_id, "FORA", self.actor)
        self.assertEqual("DIVERGENTE", extra["result"])
        self.assertEqual("IN_PROGRESS", extra["status"])
        self.assertEqual("AGUARDANDO_FINALIZACAO", extra["workflow_status"])
        self.assertEqual(1, extra["summary"]["total_extra"])
        with self.assertRaises(ConflictError) as raised:
            self.service.sync_pallet(self.public_id, self.actor)
        self.assertEqual("STATE_CONFLICT", raised.exception.code)

    def test_restart_preserves_pallet_boxes_file_and_history(self) -> None:
        self.start()
        self.service.scan_carton(self.public_id, "000001", self.actor)
        self.service.scan_carton(self.public_id, "FORA", self.actor)
        restarted = self.service.restart_pallet(self.public_id, self.actor)

        self.assertEqual(self.public_id, restarted["public_id"])
        self.assertEqual("palete.xlsx", restarted["source_filename"])
        self.assertEqual(2, restarted["active_attempt"]["number"])
        self.assertEqual(0, restarted["summary"]["total_confirmed"])
        self.assertEqual(0, restarted["summary"]["total_extra"])
        self.assertEqual(3, restarted["summary"]["total_missing"])
        self.assertTrue(all(box["status"] == "PENDING" for box in restarted["cartons"]))
        self.assertEqual("RESTARTED", restarted["attempts"][0]["status"])
        self.assertEqual(1, restarted["attempts"][0]["confirmed"])
        self.assertEqual(1, restarted["attempts"][0]["divergent"])

    def test_valid_finish_blocks_later_scan_and_restart(self) -> None:
        self.start()
        for code in ("000001", "CX-002", "CX-003"):
            self.service.scan_carton(self.public_id, code, self.actor)
        finished = self.service.finish_pallet(self.public_id, self.actor)
        self.assertEqual("COMPLETED", finished["status"])
        self.assertEqual("FINALIZADA", finished["workflow_status"])
        self.assertEqual("CONFERÊNCIA FINALIZADA", finished["display_status"])
        self.assertIsNotNone(finished["finished_at"])
        self.assertRegex(finished["finalization"]["finished_at"], r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}$")
        self.assertTrue(finished["finalization"]["finished_at_iso"].endswith("Z"))
        self.assertIsNotNone(finished["duration_seconds"])
        repeated = self.service.finish_pallet(self.public_id, self.actor)
        self.assertEqual(finished["finished_at"], repeated["finished_at"])
        self.assertEqual("A conferência já está finalizada.", repeated["message"])
        synchronization = self.service.sync_pallet(self.public_id, self.actor)
        self.assertEqual("NOT_CONFIGURED", synchronization["sync_status"])
        with self.database.connection() as connection:
            sync_actor = connection.execute(
                """
                SELECT c.matricula
                FROM synchronization_attempts s
                JOIN colaboradores c ON c.id = s.collaborator_id
                """
            ).fetchone()["matricula"]
        self.assertEqual("000123", sync_actor)
        with self.assertRaises(ConflictError) as scan_error:
            self.service.scan_carton(self.public_id, "000001", self.actor)
        self.assertEqual("CONFERENCIA_ENCERRADA", scan_error.exception.code)
        with self.assertRaises(ConflictError) as restart_error:
            self.service.restart_pallet(self.public_id, self.actor)
        self.assertEqual("CONFERENCIA_FINALIZADA", restart_error.exception.code)
        with self.assertRaises(ConflictError) as cancel_error:
            self.service.cancel_pallet(self.public_id, self.actor)
        self.assertEqual("CONFERENCIA_FINALIZADA", cancel_error.exception.code)

    def test_cancel_removes_only_conference_progress_and_blocks_future_actions(self) -> None:
        self.start()
        self.service.scan_carton(self.public_id, "000001", self.actor)
        self.service.scan_carton(self.public_id, "FORA", self.actor)
        cancelled = self.service.cancel_pallet(self.public_id, self.actor)
        self.assertEqual("CANCELLED", cancelled["status"])
        self.assertEqual("/conference", cancelled["redirect_url"])
        details = self.service.get_pallet(self.public_id)
        self.assertEqual("CANCELLED", details["status"])
        self.assertEqual([], details["cartons"])
        self.assertEqual(0, details["summary"]["total_expected"])
        with self.database.connection() as connection:
            counts = {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE pallet_id = ?", (self.repository.find_by_public_id(self.public_id)["id"],)
                ).fetchone()[0]
                for table in ("expected_cartons", "scan_events", "unexpected_cartons", "conference_attempts")
            }
        self.assertEqual({table: 0 for table in counts}, counts)
        for action in (
            lambda: self.service.scan_carton(self.public_id, "000001", self.actor),
            lambda: self.service.finish_pallet(self.public_id, self.actor),
            lambda: self.service.restart_pallet(self.public_id, self.actor),
            lambda: self.service.cancel_pallet(self.public_id, self.actor),
        ):
            with self.assertRaises(ConflictError) as raised:
                action()
            self.assertEqual("CONFERENCIA_CANCELADA", raised.exception.code)

    def test_two_simultaneous_scans_confirm_once(self) -> None:
        self.start()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: self.service.scan_carton(
                        self.public_id, "CX-002", self.actor
                    ),
                    range(2),
                )
            )
        self.assertCountEqual(
            ["CONFERIDA", "DUPLICADA"],
            [result["result"] for result in results],
        )
        latest = self.service.get_pallet(self.public_id)
        self.assertEqual(1, latest["summary"]["total_confirmed"])
        self.assertEqual(1, latest["summary"]["total_duplicate_reads"])
        with self.database.connection() as connection:
            confirmations = connection.execute(
                "SELECT COUNT(*) FROM attempt_confirmations"
            ).fetchone()[0]
        self.assertEqual(1, confirmations)

    def test_import_rejects_missing_collaborator_and_legacy_extension(self) -> None:
        with self.assertRaisesRegex(ValidationError, "Identifique o colaborador"):
            self.service.import_pallet(
                ConferenceImport(
                    collaborator=None,  # type: ignore[arg-type]
                    filename="palete.csv",
                    content_base64=b64encode(b"x").decode(),
                    origin="PORTAL",
                    operation="DIGITAL",
                )
            )

    def test_database_reinitialization_preserves_conference_and_attempt(self) -> None:
        self.start()
        self.service.scan_carton(self.public_id, "000001", self.actor)
        SQLiteDatabase(self.database.database_path).initialize()
        restored = PalletRepository(
            SQLiteDatabase(self.database.database_path)
        ).details(self.public_id)
        self.assertIsNotNone(restored)
        self.assertEqual(1, restored["summary"]["total_confirmed"])
        self.assertEqual(1, len(restored["attempts"]))
        with self.database.connection() as connection:
            versions = [
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
        self.assertEqual([1, 2, 3, 4, 5, 6, 7, 8], versions)
        with self.assertRaises(ValidationError):
            self.service.import_pallet(
                ConferenceImport(
                    collaborator=self.actor,
                    filename="palete.xls",
                    content_base64=b64encode(b"x").decode(),
                    origin="PORTAL",
                    operation="DIGITAL",
                )
            )


class ReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.directory = Path(self.tempdir.name)
        self.reader = OpenpyxlPalletReader()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_csv_preserves_caixa_estoque_character_for_character(self) -> None:
        path = self.directory / "palete.csv"
        path.write_text(
            "CAIXA_ESTOQUE VARCHAR2;OUTRA\n000047985660090489177;A\n0000912014742925015;B\n",
            encoding="utf-8",
        )
        self.assertEqual(
            ["000047985660090489177", "0000912014742925015"],
            self.reader.read_carton_codes(path, ".csv"),
        )

    def test_csv_reports_exact_duplicate_codes_and_lines(self) -> None:
        path = self.directory / "duplicado.csv"
        path.write_text(
            "CAIXA_ESTOQUE;OUTRA\n000047985660090489108;A\n000047985660090489108;B\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ExcelReadError, r"000047985660090489108 \(linhas 2, 3\)"):
            self.reader.read_carton_codes(path, ".csv")

    def test_csv_missing_caixa_estoque_reports_received_headers(self) -> None:
        path = self.directory / "sem-coluna.csv"
        path.write_text("OUTRA;ROTA\nA;B\n", encoding="utf-8")
        with self.assertRaisesRegex(ExcelReadError, r"Cabeçalhos recebidos: \['OUTRA', 'ROTA'\]"):
            self.reader.read_carton_codes(path, ".csv")

    def test_xlsx_imports_text_codes(self) -> None:
        path = self.directory / "palete.xlsx"
        path.write_bytes(minimal_xlsx(["000001", "CX-002"]))
        self.assertEqual(
            ["000001", "CX-002"],
            self.reader.read_carton_codes(path, ".xlsx"),
        )

    def test_xlsx_rejects_numeric_code_without_preservable_format(self) -> None:
        path = self.directory / "numerico.xlsx"
        path.write_bytes(minimal_xlsx(["123"], numeric=True))
        with self.assertRaisesRegex(ExcelReadError, "Exporte CAIXA_ESTOQUE como texto"):
            self.reader.read_carton_codes(path, ".xlsx")


class JsonHttpClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.cookie: str | None = None

    def request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> tuple[int, dict, dict[str, str]]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        request_payload = dict(payload or {})
        if path == "/api/conferences" and request_payload:
            request_payload.setdefault("origin", "PORTAL")
            request_payload.setdefault("operation", "DIGITAL")
        body = json.dumps(request_payload).encode("utf-8")
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = dict(response.getheaders())
        if "Set-Cookie" in response_headers:
            self.cookie = response_headers["Set-Cookie"].split(";", 1)[0]
        connection.close()
        return response.status, json.loads(raw), response_headers


class OfficialHttpFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.tempdir.name) / "http.db")
        self.database.initialize()
        self.collaborators = ColaboradorRepository(self.database)
        self.collaborators.create("001234", "COLABORADOR HTTP")
        repository = PalletRepository(self.database)
        settings = AppSettings(temporary_directory=Path(self.tempdir.name) / "uploads")
        service = ConferenceService(repository, OpenpyxlPalletReader(), settings)

        class TestHandler(ApplicationHandler):
            pass

        TestHandler.controller = ConferenceController(service)
        TestHandler.acesso_service = AcessoService(self.collaborators, settings)
        TestHandler.sessions = SessionManager()

        from http.server import ThreadingHTTPServer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = JsonHttpClient("127.0.0.1", self.server.server_port)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tempdir.cleanup()

    def test_quick_registration_and_duplicate(self) -> None:
        status, body, _ = self.client.request(
            "POST",
            "/api/colaboradores/cadastro-rapido",
            {"matricula": "000007", "nome": "  maria   da silva  ", "turno": "T2"},
        )
        self.assertEqual(201, status)
        self.assertEqual("MARIA DA SILVA", body["data"]["colaborador"]["nome"])
        self.assertEqual("T2", body["data"]["colaborador"]["turno"])
        self.assertIsNone(self.client.cookie)

        status, body, _ = self.client.request(
            "POST",
            "/api/colaboradores/cadastro-rapido",
            {"matricula": "000007", "nome": "OUTRA", "turno": "T2"},
        )
        self.assertEqual(409, status)
        self.assertEqual("MATRICULA_JA_CADASTRADA", body["error"]["code"])

    def test_turno_is_required_validated_and_editable(self) -> None:
        for registration, shift in (("000010", "T1"), ("000011", "T2"), ("000012", "T3"), ("000013", "ADM")):
            status, body, _ = self.client.request(
                "POST",
                "/api/colaboradores/cadastro-rapido",
                {"matricula": registration, "nome": "OPERADOR", "turno": shift},
            )
            self.assertEqual(201, status)
            self.assertEqual(shift, body["data"]["colaborador"]["turno"])
        status, body, _ = self.client.request(
            "POST", "/api/colaboradores/cadastro-rapido", {"matricula": "000014", "nome": "SEM TURNO"}
        )
        self.assertEqual(422, status)
        self.assertIn("turno", body["error"]["message"].casefold())
        status, body, _ = self.client.request(
            "POST", "/api/colaboradores/cadastro-rapido", {"matricula": "000014", "nome": "INVÁLIDO", "turno": "T4"}
        )
        self.assertEqual(422, status)
        status, body, _ = self.client.request("POST", "/api/colaboradores/000010", {"nome": "EDITADO", "turno": "ADM"})
        self.assertEqual(200, status)
        self.assertEqual("ADM", body["data"]["colaborador"]["turno"])
        status, body, _ = self.client.request("GET", "/api/colaboradores/000010")
        self.assertEqual(200, status)
        self.assertEqual("ADM", body["data"]["turno"])

    def test_upload_without_session_has_operational_error_and_redirect(self) -> None:
        status, body, _ = self.client.request(
            "POST",
            "/api/conferences",
            {
                "filename": "palete.csv",
                "content_base64": b64encode(b"CAIXA_ESTOQUE\n0001\n").decode(),
            },
        )
        self.assertEqual(401, status)
        self.assertEqual("COLABORADOR_NAO_IDENTIFICADO", body["error"]["code"])
        self.assertEqual("/", body["error"]["details"]["redirect_url"])
        forbidden_message = "Matrícula é " + "obrigatória"
        self.assertNotIn(forbidden_message, json.dumps(body, ensure_ascii=False))

    def test_complete_real_cookie_flow(self) -> None:
        access_status, access_body, access_headers = self.client.request(
            "POST", "/api/access", {"matricula": "001234"}
        )
        self.assertEqual(200, access_status)
        self.assertEqual("/conference", access_body["data"]["redirect_url"])
        self.assertIsNotNone(self.client.cookie)
        self.assertIn("HttpOnly", access_headers["Set-Cookie"])

        csv_content = "CAIXA ESTOQUE;ROTA\n000001;A\nCX-002;B\n".encode()
        upload_status, upload_body, _ = self.client.request(
            "POST",
            "/api/conferences",
            {
                "filename": "palete-real.csv",
                "content_base64": b64encode(csv_content).decode(),
                "employee" + "_registration": "999999",
                "matricula": "999999",
            },
        )
        self.assertEqual(201, upload_status)
        conference = upload_body["data"]
        public_id = conference["public_id"]
        self.assertEqual("001234", conference["collaborator"]["registration"])
        forbidden_message = "Matrícula é " + "obrigatória"
        self.assertNotIn(forbidden_message, json.dumps(upload_body, ensure_ascii=False))

        start_status, start_body, _ = self.client.request(
            "POST", f"/api/conferences/{public_id}/start"
        )
        self.assertEqual(200, start_status)
        self.assertEqual("IN_PROGRESS", start_body["data"]["status"])

        valid_status, valid_body, _ = self.client.request(
            "POST",
            f"/api/conferences/{public_id}/scan",
            {"caixa_estoque": "000001"},
        )
        self.assertEqual(200, valid_status)
        self.assertEqual("CONFERIDA", valid_body["data"]["result"])
        self.assertEqual("Conferência OK.", valid_body["data"]["message"])

        duplicate_status, duplicate_body, _ = self.client.request(
            "POST",
            f"/api/conferences/{public_id}/scan",
            {"caixa_estoque": "000001"},
        )
        self.assertEqual(200, duplicate_status)
        self.assertEqual("DUPLICADA", duplicate_body["data"]["result"])
        self.assertEqual(1, duplicate_body["data"]["summary"]["total_confirmed"])

        extra_status, extra_body, _ = self.client.request(
            "POST",
            f"/api/conferences/{public_id}/scan",
            {"caixa_estoque": "FORA-DO-PALETE"},
        )
        self.assertEqual(200, extra_status)
        self.assertEqual("DIVERGENTE", extra_body["data"]["result"])
        self.assertEqual(1, extra_body["data"]["summary"]["total_extra"])

        finish_status, finish_body, _ = self.client.request(
            "POST", f"/api/conferences/{public_id}/finish"
        )
        self.assertEqual(409, finish_status)
        self.assertEqual("CONFERENCIA_COM_PENDENCIAS", finish_body["error"]["code"])
        self.assertEqual(
            {"faltantes": 1, "divergentes": 0},
            finish_body["error"]["details"],
        )

        restart_status, restart_body, _ = self.client.request(
            "POST", f"/api/conferences/{public_id}/restart"
        )
        self.assertEqual(200, restart_status)
        restarted = restart_body["data"]
        self.assertEqual(2, restarted["active_attempt"]["number"])
        self.assertEqual(0, restarted["summary"]["total_confirmed"])
        self.assertEqual(0, restarted["summary"]["total_extra"])
        self.assertEqual("palete-real.csv", restarted["source_filename"])
        self.assertEqual("RESTARTED", restarted["attempts"][0]["status"])

        for code in ("000001", "CX-002"):
            status, body, _ = self.client.request(
                "POST",
                f"/api/conferences/{public_id}/scan",
                {"caixa_estoque": code},
            )
            self.assertEqual(200, status)
            self.assertEqual("CONFERIDA", body["data"]["result"])

        finish_status, finish_body, _ = self.client.request(
            "POST", f"/api/conferences/{public_id}/finish"
        )
        self.assertEqual(200, finish_status)
        self.assertEqual("COMPLETED", finish_body["data"]["status"])

        blocked_scan, body, _ = self.client.request(
            "POST",
            f"/api/conferences/{public_id}/scan",
            {"caixa_estoque": "000001"},
        )
        self.assertEqual(409, blocked_scan)
        self.assertEqual("CONFERENCIA_ENCERRADA", body["error"]["code"])

        blocked_restart, body, _ = self.client.request(
            "POST", f"/api/conferences/{public_id}/restart"
        )
        self.assertEqual(409, blocked_restart)
        self.assertEqual("CONFERENCIA_FINALIZADA", body["error"]["code"])

        blocked_cancel, body, _ = self.client.request(
            "POST", f"/api/conferences/{public_id}/cancel"
        )
        self.assertEqual(409, blocked_cancel)
        self.assertEqual("CONFERENCIA_FINALIZADA", body["error"]["code"])

        with self.database.connection() as connection:
            stored_actor = connection.execute(
                """
                SELECT c.matricula
                FROM pallets p
                JOIN colaboradores c ON c.id = p.created_by_collaborator_id
                WHERE p.public_id = ?
                """,
                (public_id,),
            ).fetchone()["matricula"]
            attempts = connection.execute(
                """
                SELECT COUNT(*) FROM conference_attempts a
                JOIN pallets p ON p.id = a.pallet_id
                WHERE p.public_id = ?
                """,
                (public_id,),
            ).fetchone()[0]
            history_events = connection.execute(
                """
                SELECT COUNT(*) FROM scan_events se
                JOIN pallets p ON p.id = se.pallet_id
                WHERE p.public_id = ?
                """,
                (public_id,),
            ).fetchone()[0]
        self.assertEqual("001234", stored_actor)
        self.assertEqual(2, attempts)
        self.assertEqual(5, history_events)

    def test_http_xlsx_upload(self) -> None:
        self.client.request("POST", "/api/access", {"matricula": "001234"})
        status, body, _ = self.client.request(
            "POST",
            "/api/conferences",
            {
                "filename": "palete.xlsx",
                "content_base64": b64encode(
                    minimal_xlsx(["000010", "000011"])
                ).decode(),
            },
        )
        self.assertEqual(201, status)
        self.assertEqual(
            ["000010", "000011"],
            [box["caixa_estoque"] for box in body["data"]["cartons"]],
        )

    def test_csv_caixa_estoque_is_text_in_database_json_and_scan(self) -> None:
        codes = [
            "000047985660090489177",
            "000047985660090489108",
            "0000912014742925015",
        ]
        self.client.request("POST", "/api/access", {"matricula": "001234"})
        csv_content = (
            "\ufeffCAIXA_ESTOQUE VARCHAR2;ROTA\n"
            + "\n".join(f"{code};A" for code in codes)
            + "\n"
        ).encode("utf-8")
        status, body, _ = self.client.request(
            "POST",
            "/api/conferences",
            {
                "filename": "caixas.csv",
                "content_base64": b64encode(csv_content).decode(),
            },
        )
        self.assertEqual(201, status)
        public_id = body["data"]["public_id"]
        self.assertEqual(public_id, body["data"]["importacao_id"])
        self.assertEqual(3, body["data"]["total_linhas"])
        self.assertEqual(3, body["data"]["total_importadas"])
        self.assertEqual(0, body["data"]["total_duplicadas"])
        self.assertEqual(codes, [box["caixa_estoque"] for box in body["data"]["cartons"]])
        self.assertEqual(codes, [box["caixa_estoque"] for box in body["data"]["caixas"]])
        self.assertIn('"caixa_estoque": "000047985660090489177"', json.dumps(body, ensure_ascii=False))
        with self.database.connection() as connection:
            stored = connection.execute(
                """
                SELECT ec.code, typeof(ec.code) AS storage_type
                FROM expected_cartons ec
                JOIN pallets p ON p.id = ec.pallet_id
                WHERE p.public_id = ? ORDER BY ec.id
                """,
                (public_id,),
            ).fetchall()
        self.assertEqual([(code, "text") for code in codes], [(row["code"], row["storage_type"]) for row in stored])
        self.client.request("POST", f"/api/conferences/{public_id}/start")
        scan_status, scan_body, _ = self.client.request(
            "POST",
            f"/api/conferences/{public_id}/scan",
            {"caixa_estoque": codes[0]},
        )
        self.assertEqual(200, scan_status)
        self.assertEqual("CONFERIDA", scan_body["data"]["result"])
        self.assertEqual(codes[0], scan_body["data"]["caixa_estoque"])

        repeated_status, repeated_body, _ = self.client.request(
            "POST",
            "/api/conferences",
            {
                "filename": "caixas.csv",
                "content_base64": b64encode(csv_content).decode(),
            },
        )
        self.assertEqual(409, repeated_status)
        self.assertEqual("CONFERENCIA_ATIVA", repeated_body["error"]["code"])
        self.assertEqual(public_id, repeated_body["error"]["details"]["public_id"])
        with self.database.connection() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM expected_cartons WHERE pallet_id = (SELECT id FROM pallets WHERE public_id = ?)",
                (public_id,),
            ).fetchone()[0]
        self.assertEqual(len(codes), total)

    def test_active_conference_is_recovered_with_boxes_and_blocks_manual_upload(self) -> None:
        self.client.request("POST", "/api/access", {"matricula": "001234"})
        status, state, _ = self.client.request("GET", "/api/conferences/active")
        self.assertEqual(200, status)
        self.assertFalse(state["data"]["has_active_conference"])

        codes = ["000000123456", "000047985660090489177", "CX-003"]
        payload = {
            "filename": "conferencia.csv",
            "content_base64": b64encode(
                ("CAIXA_ESTOQUE;ROTA\n" + "\n".join(f"{code};A" for code in codes)).encode()
            ).decode(),
            "origin": "TL",
            "operation": "CROSS",
        }
        status, imported, _ = self.client.request("POST", "/api/conferences", payload)
        self.assertEqual(201, status)
        conference = imported["data"]
        self.assertEqual(codes, [box["caixa_estoque"] for box in conference["cartons"]])
        self.assertRegex(conference["importation"]["imported_at"], r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}$")
        self.assertTrue(conference["importation"]["imported_at_iso"].endswith("Z"))

        status, recovered, _ = self.client.request("GET", "/api/conferences/active")
        self.assertEqual(200, status)
        self.assertTrue(recovered["data"]["has_active_conference"])
        restored = recovered["data"]["conference"]
        self.assertEqual(conference["public_id"], restored["public_id"])
        self.assertEqual(codes, [box["caixa_estoque"] for box in restored["cartons"]])

        status, blocked, _ = self.client.request("POST", "/api/conferences", payload)
        self.assertEqual(409, status)
        self.assertEqual("CONFERENCIA_ATIVA", blocked["error"]["code"])
        self.assertIn("Já existe uma conferência ativa", blocked["error"]["message"])

    def test_finish_or_cancel_releases_active_state_and_import_timestamp_is_immutable(self) -> None:
        self.client.request("POST", "/api/access", {"matricula": "001234"})
        payload = {
            "filename": "uma-caixa.csv",
            "content_base64": b64encode(b"CAIXA_ESTOQUE;ROTA\n000001;A\n").decode(),
        }
        status, imported, _ = self.client.request("POST", "/api/conferences", payload)
        self.assertEqual(201, status)
        public_id = imported["data"]["public_id"]
        imported_at = imported["data"]["importation"]["imported_at_iso"]
        self.client.request("POST", f"/api/conferences/{public_id}/start")
        self.client.request("POST", f"/api/conferences/{public_id}/scan", {"caixa_estoque": "000001"})
        status, finished, _ = self.client.request("POST", f"/api/conferences/{public_id}/finish")
        self.assertEqual(200, status)
        self.assertEqual(imported_at, finished["data"]["importation"]["imported_at_iso"])
        status, inactive, _ = self.client.request("GET", "/api/conferences/active")
        self.assertEqual(200, status)
        self.assertFalse(inactive["data"]["has_active_conference"])
        self.assertEqual("FINALIZADA", inactive["data"]["latest_conference"]["workflow_status"])
        self.assertEqual(
            finished["data"]["finalization"]["finished_at_iso"],
            inactive["data"]["latest_conference"]["finalization"]["finished_at_iso"],
        )

        status, next_import, _ = self.client.request("POST", "/api/conferences", payload)
        self.assertEqual(201, status)
        self.assertNotEqual(public_id, next_import["data"]["public_id"])
        status, _cancelled, _ = self.client.request(
            "POST", f"/api/conferences/{next_import['data']['public_id']}/cancel"
        )
        self.assertEqual(200, status)
        status, inactive, _ = self.client.request("GET", "/api/conferences/active")
        self.assertEqual(200, status)
        self.assertFalse(inactive["data"]["has_active_conference"])


class StaticInterfaceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.access = (cls.root / "access.html").read_text(encoding="utf-8")
        cls.conference = (cls.root / "index.html").read_text(encoding="utf-8")
        cls.upload_js = (cls.root / "static/js/upload.js").read_text(encoding="utf-8")
        cls.conference_js = (cls.root / "static/js/conference.js").read_text(encoding="utf-8")
        cls.access_js = (cls.root / "static/js/access.js").read_text(encoding="utf-8")

    def test_access_has_visible_quick_registration_control_and_external_module(self) -> None:
        self.assertIn('title="Cadastrar colaborador"', self.access)
        self.assertIn('aria-label="Cadastrar colaborador"', self.access)
        self.assertIn("<svg", self.access)
        self.assertIn('src="/static/js/access.js"', self.access)
        self.assertNotIn("<script type=\"module\">", self.access)
        self.assertIn('id="cadastro-turno"', self.access)
        for shift in ("T1", "T2", "T3", "ADM"):
            self.assertIn(f'value="{shift}"', self.access)
        self.assertIn("btn-editar-colaborador", self.access)
        self.assertIn("turno: shiftInput.value", self.access_js)

    def test_upload_has_only_file_control_and_preserved_button(self) -> None:
        self.assertIn('name="file"', self.conference)
        self.assertIn('accept=".xlsx,.csv"', self.conference)
        self.assertIn("Nova importação", self.conference)
        self.assertNotIn('name="employee' + '_registration"', self.conference)
        self.assertNotIn("matricula", self.upload_js.casefold())
        self.assertNotIn("registration", self.upload_js.casefold())

    def test_active_conference_screen_is_backend_recovered_and_upload_card_is_exclusive(self) -> None:
        self.assertIn('id="upload-card" class="card" hidden', self.conference)
        self.assertIn("loadActiveConference", (self.root / "static/js/main.js").read_text(encoding="utf-8"))
        self.assertIn('api("/api/conferences/active")', self.conference_js)
        self.assertIn('$("#upload-card").hidden = isActiveConference(data)', self.conference_js)
        self.assertIn('$("#upload-card").hidden = false', self.conference_js)
        self.assertIn('await load(created.public_id, created)', self.upload_js)
        self.assertNotIn("new Date().toLocaleString", self.upload_js)

    def test_manual_finish_modal_and_awaiting_finalization_contract_exist(self) -> None:
        self.assertIn('id="finish-modal"', self.conference)
        self.assertIn('id="finish-form"', self.conference)
        self.assertIn("100% conferido — aguardando finalização", self.conference)
        self.assertIn('workflow_status === "AGUARDANDO_FINALIZACAO"', self.conference_js)
        self.assertIn('workflow_status !== "FINALIZADA"', self.conference_js)
        self.assertIn('addEventListener("submit", async (event)', self.conference_js)

    def test_scan_keeps_caixa_estoque_as_string_and_restart_modal_exists(self) -> None:
        self.assertIn('String(value ?? "").trim()', self.conference_js)
        self.assertIn("JSON.stringify({ caixa_estoque: caixaEstoque })", self.conference_js)
        self.assertNotIn("Number(", self.conference_js)
        self.assertNotIn("parseInt(", self.conference_js)
        self.assertNotIn("parseFloat(", self.conference_js)
        self.assertNotIn("matricula", self.conference_js.casefold())
        self.assertIn('id="restart-modal"', self.conference)
        self.assertIn("Reiniciar conferência", self.conference)
        self.assertIn("Cancelar conferência", self.conference)
        self.assertIn('addEventListener("input", scheduleAutomaticScan)', self.conference_js)
        self.assertIn('addEventListener("paste"', self.conference_js)
        self.assertIn("AUTO_SCAN_DELAY_MS", self.conference_js)


if __name__ == "__main__":
    unittest.main()
