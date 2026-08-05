from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import zipfile
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from conferencia.controllers.conference_controller import ConferenceController
from conferencia.domain.box_codes import content_hash_caixa_estoque
from conferencia.domain.entities import CollaboratorContext, ConferenceImport
from conferencia.infrastructure.database import SQLiteDatabase
from conferencia.infrastructure.session_manager import SessionManager
from conferencia.infrastructure.settings import AppSettings
from conferencia.infrastructure.timezones import SAO_PAULO_TIMEZONE_NAME, get_sao_paulo_timezone
from conferencia.readers.excel_reader import ExcelReadError, PalletFileImporter
from conferencia.repositories.colaborador_repository import ColaboradorRepository
from conferencia.repositories.pallet_repository import PalletRepository
from conferencia.services.acesso_service import AcessoService
from conferencia.services.automatic_report_service import AutomaticReportService
from conferencia.services.google_sheets_sync_service import GoogleSheetsSyncService
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


class ConfiguredPalletReader:
    def __init__(self, codes: list[str]) -> None:
        self.codes = codes

    def read_carton_codes(self, file_path: Path, extension: str) -> list[str]:
        return self.codes


class ItemReader:
    def __init__(self, items: list[dict[str, str]]) -> None:
        self.items = items

    def read_expected_items(self, file_path: Path, extension: str) -> list[dict[str, str]]:
        return self.items


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
                agenda="AGENDA TESTE",
            )
        )
        self.public_id = self.created["public_id"]

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def start(self) -> dict:
        return self.service.start_pallet(self.public_id, self.actor)

    def import_codes(self, codes: list[str]) -> tuple[ConferenceService, dict]:
        service = ConferenceService(
            self.repository,
            ConfiguredPalletReader(codes),
            AppSettings(temporary_directory=Path(self.tempdir.name) / "uploads"),
        )
        created = service.import_pallet(
            ConferenceImport(
                self.actor, "caixas.csv", b64encode("|".join(codes).encode()).decode(),
                "PORTAL", "DIGITAL", "AGENDA TESTE",
            )
        )
        service.start_pallet(created["public_id"], self.actor)
        return service, created

    def test_import_opens_and_preserves_session_actor(self) -> None:
        self.assertEqual("EM_ABERTO", self.created["status"])
        self.assertIsNone(self.created["started_at"])
        self.assertTrue(self.created["importation"]["imported_at_iso"].endswith("Z"))
        self.assertRegex(self.created["importation"]["imported_at"], r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}$")
        self.assertEqual("000123", self.created["collaborator"]["registration"])
        self.assertEqual("palete.xlsx", self.created["source_filename"])
        self.assertEqual("AGENDA TESTE", self.created["importation"]["agenda"])
        self.assertEqual(
            {"origin": "PORTAL", "operation": "DIGITAL", "shift": "ADM"},
            {key: self.created["importation"][key] for key in ("origin", "operation", "shift")},
        )

    def test_start_preserves_import_timestamp_and_sets_only_started_at(self) -> None:
        imported_at = self.created["importation"]["imported_at_iso"]
        started = self.start()
        self.assertEqual(imported_at, started["importation"]["imported_at_iso"])
        self.assertIsNotNone(started["started_at"])

    def test_imported_at_uses_the_sqlite_utc_contract_and_brazilian_format(self) -> None:
        self.assertEqual(
            "03/08/2026 20:06:00",
            self.repository._format_local_datetime("2026-08-03T23:06:00Z"),
        )
        self.assertEqual(
            "03/08/2026 20:06:00",
            self.repository._format_local_datetime("2026-08-03T23:06:00"),
        )

    def test_datetime_with_timezone_is_converted_once_and_empty_values_are_safe(self) -> None:
        sao_paulo_value = datetime(2026, 8, 3, 20, 6, tzinfo=timezone(timedelta(hours=-3)))
        self.assertEqual("03/08/2026 20:06:00", self.repository._format_local_datetime(sao_paulo_value))
        self.assertEqual("03/08/2026 20:06:00", self.repository._format_local_datetime("2026-08-03T20:06:00-03:00"))
        self.assertIsNone(self.repository._format_local_datetime(None))
        self.assertIsNone(self.repository._format_local_datetime(""))
        self.assertIsNone(self.repository._format_local_datetime("data-invalida"))

    def test_content_hash_is_order_independent_but_preserves_duplicates_and_zeros(self) -> None:
        first = content_hash_caixa_estoque([" 00123 ", "cx-2", "CX-2"])
        same_content = content_hash_caixa_estoque(["cx-2", "00123", "CX-2"])
        without_duplicate = content_hash_caixa_estoque(["00123", "CX-2"])
        without_zero = content_hash_caixa_estoque(["123", "CX-2", "CX-2"])
        self.assertEqual(64, len(first))
        self.assertEqual(first, same_content)
        self.assertNotEqual(first, without_duplicate)
        self.assertNotEqual(first, without_zero)

    def test_one_scan_confirms_all_repeated_rows_and_preserves_classes(self) -> None:
        self.service.cancel_pallet(self.public_id, self.actor)
        service = ConferenceService(
            self.repository,
            ItemReader([
                {"caixa_estoque": "000099", "ds_classe": "vestuario"},
                {"caixa_estoque": "000099", "ds_classe": "calçados"},
                {"caixa_estoque": "000099", "ds_classe": ""},
            ]),
            AppSettings(temporary_directory=Path(self.tempdir.name) / "uploads"),
        )
        created = service.import_pallet(ConferenceImport(self.actor, "repetido.csv", b64encode(b"x").decode(), "PORTAL", "DIGITAL"))
        service.start_pallet(created["public_id"], self.actor)
        scanned = service.scan_carton(created["public_id"], "000099", self.actor)
        self.assertEqual("CONFERIDA", scanned["result"])
        self.assertEqual(3, scanned["summary"]["total_confirmed"])
        self.assertEqual(0, scanned["summary"]["total_missing"])
        self.assertEqual(1, len(scanned["recent_scans"]))
        self.assertEqual(["VESTUARIO", "CALÇADOS", "NÃO INFORMADO"], [box["ds_classe"] for box in scanned["cartons"]])
        self.assertEqual("MISTO", scanned["pallet_class"])
        self.assertEqual("DUPLICADA", service.scan_carton(created["public_id"], "000099", self.actor)["result"])

    def test_same_open_content_is_resumed_and_cancelled_content_creates_linked_record(self) -> None:
        resumed = self.service.import_pallet(
            ConferenceImport(self.actor, "renomeado.xlsx", b64encode(b"outro arquivo").decode(), "PORTAL", "DIGITAL", "AGENDA TESTE")
        )
        self.assertEqual("resumed", resumed["action"])
        self.assertEqual(self.public_id, resumed["public_id"])
        self.service.cancel_pallet(self.public_id, self.actor)
        recreated = self.service.import_pallet(
            ConferenceImport(self.actor, "novo.csv", b64encode(b"other-content").decode(), "PORTAL", "DIGITAL", "AGENDA TESTE")
        )
        self.assertEqual("created_after_cancellation", recreated["action"])
        self.assertNotEqual(self.public_id, recreated["public_id"])
        self.assertEqual(self.public_id, recreated["previous_public_id"])

    def test_open_content_owned_by_another_collaborator_is_not_resumed(self) -> None:
        other = ColaboradorRepository(self.database).create("000456", "OUTRO OPERADOR")
        other_actor = CollaboratorContext(int(other["id"]), str(other["matricula"]), str(other["nome"]))
        with self.assertRaises(ConflictError) as raised:
            self.service.import_pallet(
                ConferenceImport(other_actor, "mesmo-conteudo.xlsx", b64encode(b"outro").decode(), "PORTAL", "DIGITAL", "AGENDA TESTE")
            )
        self.assertEqual("CONTEUDO_EM_CONFERENCIA", raised.exception.code)
        self.assertEqual("000123", raised.exception.details["owner_registration"])
        self.assertIn("OPERADOR TESTE", str(raised.exception))

    def test_finished_content_blocks_and_adm_reconference_preserves_hash(self) -> None:
        self.start()
        for code in ("000001", "CX-002", "CX-003"):
            self.service.scan_carton(self.public_id, code, self.actor)
        finished = self.service.finish_pallet(self.public_id, self.actor)
        blocked = self.service.import_pallet(
            ConferenceImport(self.actor, "renomeado.csv", b64encode(b"outro").decode(), "PORTAL", "DIGITAL", "AGENDA TESTE")
        )
        self.assertEqual("already_completed", blocked["action"])
        self.assertTrue(blocked["already_completed"])
        self.assertEqual(self.public_id, blocked["public_id"])
        other = ColaboradorRepository(self.database).create("000456", "OUTRO OPERADOR")
        other_actor = CollaboratorContext(
            int(other["id"]), str(other["matricula"]), str(other["nome"]),
        )
        blocked_for_other = self.service.import_pallet(
            ConferenceImport(other_actor, "arquivo-com-outro-nome.csv", b64encode(b"conteudo diferente").decode(), "PORTAL", "DIGITAL", "000123")
        )
        self.assertEqual("already_completed", blocked_for_other["action"])
        self.assertEqual(self.public_id, blocked_for_other["public_id"])
        self.assertEqual("000123", blocked_for_other["collaborator"]["registration"])
        reconference = self.service.authorize_reconference(self.public_id, "Solicitação formal da gestão.", self.actor)
        self.assertEqual("admin_reconference_created", reconference["action"])
        self.assertTrue(reconference["is_reconference"])
        self.assertEqual(finished["content_hash"], reconference["content_hash"])
        self.assertIsNotNone(finished["importation"]["imported_at"])
        self.assertIsNone(reconference["started_at"])
        self.assertEqual("READY", reconference["active_attempt"]["status"])
        reconference_imported_at = reconference["importation"]["imported_at_iso"]
        reconference_started = self.service.start_pallet(reconference["public_id"], self.actor)
        self.assertEqual(reconference_imported_at, reconference_started["importation"]["imported_at_iso"])
        self.assertIsNotNone(reconference_started["started_at"])
        self.assertEqual(
            ["000001", "CX-002", "CX-003"],
            [box["caixa_estoque"] for box in self.created["cartons"]],
        )

    def test_non_adm_cannot_authorize_reconference_or_skip_reason(self) -> None:
        self.start()
        for code in ("000001", "CX-002", "CX-003"):
            self.service.scan_carton(self.public_id, code, self.actor)
        self.service.finish_pallet(self.public_id, self.actor)
        user = CollaboratorContext(self.actor.id, self.actor.registration, self.actor.name, "T1")
        with self.assertRaises(ConflictError) as denied:
            self.service.authorize_reconference(self.public_id, "Justificativa válida", user)
        self.assertEqual("RECONFERENCIA_SEM_PERMISSAO", denied.exception.code)
        with self.assertRaises(ValidationError) as invalid:
            self.service.authorize_reconference(self.public_id, "curta", self.actor)
        self.assertEqual("JUSTIFICATIVA_OBRIGATORIA", invalid.exception.code)

    def test_start_after_render_contract_is_idempotent(self) -> None:
        first = self.start()
        second = self.service.start_pallet(self.public_id, self.actor)
        self.assertEqual("EM_ABERTO", first["status"])
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
                        agenda="AGENDA TESTE",
                    )
                )["public_id"]
            except ConflictError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _item: import_once(), range(2)))
        self.assertEqual(2, sum(result.startswith("CONF-") for result in results))
        self.assertTrue(self.service.active_pallet(self.actor)["has_active_conference"])

    def test_expected_duplicate_and_divergent_results(self) -> None:
        self.start()
        matched = self.service.scan_carton(self.public_id, "  000001\r\n", self.actor)
        duplicate = self.service.scan_carton(self.public_id, "000001", self.actor)
        divergent = self.service.scan_carton(self.public_id, "FORA-001", self.actor)
        repeated_extra = self.service.scan_carton(self.public_id, "FORA-001", self.actor)

        self.assertEqual(("CONFERIDA", "Caixa conferida com sucesso."), (matched["result"], matched["message"]))
        self.assertEqual(("EXACT", "000001", "000001"), (matched["match_type"], matched["scanned_code"], matched["expected_code"]))
        self.assertEqual(("DUPLICADA", "Caixa já conferida."), (duplicate["result"], duplicate["message"]))
        self.assertEqual("DIVERGENTE", divergent["result"])
        self.assertIn("código não esperado", divergent["message"])
        self.assertEqual("DUPLICADA", repeated_extra["result"])
        self.assertEqual(1, repeated_extra["summary"]["total_confirmed"])
        self.assertEqual(1, repeated_extra["summary"]["total_extra"])
        self.assertEqual(2, repeated_extra["summary"]["total_duplicate_reads"])

    def test_scan_matches_variable_leading_zero_counts_without_changing_stored_code(self) -> None:
        self.service.cancel_pallet(self.public_id, self.actor)
        for expected in ("00012345", "000012345", "0000012345", "00000012345"):
            service, created = self.import_codes([expected])
            scanned = service.scan_carton(created["public_id"], "  12345\r\n", self.actor)
            self.assertEqual("CONFERIDA", scanned["result"])
            self.assertEqual("NORMALIZED", scanned["match_type"])
            self.assertEqual("12345", scanned["scanned_code"])
            self.assertEqual(expected, scanned["expected_code"])
            self.assertEqual(expected, scanned["caixa_estoque"])
            self.assertEqual(expected, scanned["cartons"][0]["caixa_estoque"])
            with self.database.connection() as connection:
                stored = connection.execute(
                    "SELECT scanned_code FROM scan_events WHERE pallet_id = (SELECT id FROM pallets WHERE public_id = ?)",
                    (created["public_id"],),
                ).fetchone()["scanned_code"]
            self.assertEqual(expected, stored)
            service.cancel_pallet(created["public_id"], self.actor)

    def test_scan_rejects_ambiguous_normalized_code_without_recording_scan(self) -> None:
        self.service.cancel_pallet(self.public_id, self.actor)
        service, created = self.import_codes(["00012345", "0000012345"])
        with self.assertRaises(ConflictError) as raised:
            service.scan_carton(created["public_id"], "12345", self.actor)
        self.assertEqual("AMBIGUOUS_BOX_CODE", raised.exception.code)
        self.assertEqual(["00012345", "0000012345"], raised.exception.details["matches"])
        state = service.get_pallet(created["public_id"])
        self.assertEqual(0, state["summary"]["total_confirmed"])
        self.assertEqual(0, state["summary"]["total_extra"])

    def test_divergences_are_display_only_and_do_not_block_finish(self) -> None:
        self.start()
        for code in ("000001", "CX-002", "CX-003"):
            self.service.scan_carton(self.public_id, code, self.actor)
        state = self.service.scan_carton(self.public_id, "FORA", self.actor)
        state = self.service.scan_carton(self.public_id, "CX-003", self.actor)
        self.assertEqual(100.0, state["summary"]["coverage_percent"])
        self.assertTrue(state["summary"]["can_finish"])
        finished = self.service.finish_pallet(self.public_id, self.actor)
        self.assertEqual("FINALIZADA", finished["status"])

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
        self.assertEqual(2, raised.exception.details["faltantes"])
        self.assertEqual(0, raised.exception.details["divergentes"])
        with self.database.connection() as connection:
            missing_count = connection.execute(
                "SELECT COUNT(*) FROM conference_divergences WHERE pallet_id = ? AND divergence_type = 'FALTA'",
                (self.repository.find_by_public_id(self.public_id)["id"],),
            ).fetchone()[0]
        self.assertEqual(2, missing_count)
        with self.assertRaises(ConflictError):
            self.service.finish_pallet(self.public_id, self.actor)
        with self.database.connection() as connection:
            repeated_missing_count = connection.execute(
                "SELECT COUNT(*) FROM conference_divergences WHERE pallet_id = ? AND divergence_type = 'FALTA'",
                (self.repository.find_by_public_id(self.public_id)["id"],),
            ).fetchone()[0]
        self.assertEqual(2, repeated_missing_count)

    def test_full_coverage_waits_for_manual_finish_and_accepts_extra_scan(self) -> None:
        self.start()
        for code in ("000001", "CX-002", "CX-003"):
            self.service.scan_carton(self.public_id, code, self.actor)
        awaiting = self.service.get_pallet(self.public_id)
        self.assertEqual("EM_ABERTO", awaiting["status"])
        self.assertEqual("EM_ABERTO", awaiting["workflow_status"])
        self.assertIsNone(awaiting["finished_at"])
        extra = self.service.scan_carton(self.public_id, "FORA", self.actor)
        self.assertEqual("DIVERGENTE", extra["result"])
        self.assertEqual("EM_ABERTO", extra["status"])
        self.assertEqual("EM_ABERTO", extra["workflow_status"])
        self.assertEqual(1, extra["summary"]["total_extra"])
        self.assertEqual("FINALIZADA", self.service.finish_pallet(self.public_id, self.actor)["status"])

    def test_duplicate_scan_does_not_block_finish_at_full_coverage(self) -> None:
        self.start()
        for code in ("000001", "CX-002", "CX-003"):
            self.service.scan_carton(self.public_id, code, self.actor)
        self.service.scan_carton(self.public_id, "CX-003", self.actor)
        open_conference = self.service.get_pallet(self.public_id)
        self.assertEqual(100.0, open_conference["summary"]["coverage_percent"])
        self.assertEqual("EM_ABERTO", open_conference["status"])
        self.assertEqual("FINALIZADA", self.service.finish_pallet(self.public_id, self.actor)["status"])

    def test_valid_finish_blocks_later_scan_and_cancel(self) -> None:
        self.start()
        for code in ("000001", "CX-002", "CX-003"):
            self.service.scan_carton(self.public_id, code, self.actor)
        finished = self.service.finish_pallet(self.public_id, self.actor)
        self.assertEqual("FINALIZADA", finished["status"])
        self.assertEqual("FINALIZADA", finished["workflow_status"])
        self.assertEqual("CONFERÊNCIA FINALIZADA", finished["display_status"])
        self.assertIsNotNone(finished["finished_at"])
        self.assertRegex(finished["finalization"]["finished_at"], r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}$")
        self.assertTrue(finished["finalization"]["finished_at_iso"].endswith("Z"))
        self.assertIsNotNone(finished["duration_seconds"])
        repeated = self.service.finish_pallet(self.public_id, self.actor)
        self.assertEqual(finished["finished_at"], repeated["finished_at"])
        self.assertEqual("A conferência já está finalizada.", repeated["message"])
        with self.assertRaises(ConflictError) as scan_error:
            self.service.scan_carton(self.public_id, "000001", self.actor)
        self.assertEqual("CONFERENCIA_ENCERRADA", scan_error.exception.code)
        with self.assertRaises(ConflictError) as cancel_error:
            self.service.cancel_pallet(self.public_id, self.actor)
        self.assertEqual("CONFERENCIA_FINALIZADA", cancel_error.exception.code)

    def test_cancel_preserves_conference_history_and_blocks_future_actions(self) -> None:
        self.start()
        self.service.scan_carton(self.public_id, "000001", self.actor)
        self.service.scan_carton(self.public_id, "FORA", self.actor)
        cancelled = self.service.cancel_pallet(self.public_id, self.actor)
        self.assertEqual("CANCELADA", cancelled["status"])
        details = self.service.get_pallet(self.public_id)
        self.assertEqual("CANCELADA", details["status"])
        self.assertEqual(3, len(details["cartons"]))
        self.assertEqual(3, details["summary"]["total_expected"])
        with self.database.connection() as connection:
            counts = {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE pallet_id = ?", (self.repository.find_by_public_id(self.public_id)["id"],)
                ).fetchone()[0]
                for table in ("expected_cartons", "scan_events", "unexpected_cartons", "conference_attempts")
            }
        self.assertGreater(counts["expected_cartons"], 0)
        self.assertGreater(counts["scan_events"], 0)
        self.assertGreater(counts["conference_attempts"], 0)
        self.assertEqual("CANCELLED", details["active_attempt"]["status"])
        for action in (
            lambda: self.service.scan_carton(self.public_id, "000001", self.actor),
            lambda: self.service.finish_pallet(self.public_id, self.actor),
            lambda: self.service.cancel_pallet(self.public_id, self.actor),
        ):
            with self.assertRaises(ConflictError) as raised:
                action()
            self.assertEqual("CONFERENCIA_CANCELADA", raised.exception.code)

    def test_cancel_before_start_closes_ready_attempt(self) -> None:
        cancelled = self.service.cancel_pallet(self.public_id, self.actor)
        self.assertEqual("CANCELADA", cancelled["status"])
        self.assertEqual("CANCELLED", cancelled["active_attempt"]["status"])
        with self.database.connection() as connection:
            finished_at = connection.execute(
                "SELECT finished_at FROM conference_attempts WHERE pallet_id = ?",
                (self.repository.find_by_public_id(self.public_id)["id"],),
            ).fetchone()["finished_at"]
        self.assertIsNotNone(finished_at)

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
        self.service.cancel_pallet(self.public_id, self.actor)
        without_agenda = ConferenceService(
            self.repository,
            ConfiguredPalletReader(["SEM-AGENDA"]),
            AppSettings(temporary_directory=Path(self.tempdir.name) / "uploads"),
        ).import_pallet(
            ConferenceImport(self.actor, "sem-agenda.csv", b64encode(b"x").decode(), "PORTAL", "DIGITAL")
        )
        self.assertEqual("", without_agenda["importation"]["agenda"])
        with self.assertRaisesRegex(ValidationError, "Identifique o colaborador"):
            self.service.import_pallet(
                ConferenceImport(
                    collaborator=None,  # type: ignore[arg-type]
                    filename="palete.csv",
                    content_base64=b64encode(b"x").decode(),
                    origin="PORTAL",
                    operation="DIGITAL",
                    agenda="AGENDA TESTE",
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
        self.assertEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18], versions)
        with self.assertRaises(ValidationError):
            self.service.import_pallet(
                ConferenceImport(
                    collaborator=self.actor,
                    filename="palete.xls",
                    content_base64=b64encode(b"x").decode(),
                    origin="PORTAL",
                    operation="DIGITAL",
                    agenda="AGENDA TESTE",
                )
            )


class ReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.directory = Path(self.tempdir.name)
        self.reader = PalletFileImporter()

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

    def test_csv_keeps_exact_duplicate_codes_as_distinct_rows(self) -> None:
        path = self.directory / "duplicado.csv"
        path.write_text(
            "CAIXA_ESTOQUE;OUTRA\n000047985660090489108;A\n000047985660090489108;B\n",
            encoding="utf-8",
        )
        self.assertEqual(
            ["000047985660090489108", "000047985660090489108"],
            self.reader.read_carton_codes(path, ".csv"),
        )

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
            request_payload.setdefault("agenda", "AGENDA TESTE")
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


class TimezoneTests(unittest.TestCase):
    def test_sao_paulo_zoneinfo_is_available(self) -> None:
        timezone_value = get_sao_paulo_timezone()
        self.assertEqual(SAO_PAULO_TIMEZONE_NAME, timezone_value.key)

    def test_sao_paulo_timezone_falls_back_when_iana_data_is_unavailable(self) -> None:
        from zoneinfo import ZoneInfoNotFoundError

        with patch("conferencia.infrastructure.timezones.ZoneInfo", side_effect=ZoneInfoNotFoundError("missing")):
            fallback = get_sao_paulo_timezone()
        self.assertEqual(SAO_PAULO_TIMEZONE_NAME, fallback.tzname(None))
        self.assertEqual(timedelta(hours=-3), fallback.utcoffset(None))


class OfficialHttpFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.tempdir.name) / "http.db")
        self.database.initialize()
        self.collaborators = ColaboradorRepository(self.database)
        self.collaborators.create("001234", "COLABORADOR HTTP")
        repository = PalletRepository(self.database)
        settings = AppSettings(temporary_directory=Path(self.tempdir.name) / "uploads")
        service = ConferenceService(repository, PalletFileImporter(), settings)

        class TestHandler(ApplicationHandler):
            pass

        TestHandler.controller = ConferenceController(service)
        TestHandler.acesso_service = AcessoService(self.collaborators, settings)
        TestHandler.sessions = SessionManager()
        self.downloads = Path(self.tempdir.name) / "Downloads"
        self.downloads.mkdir()
        TestHandler.automatic_report_service = AutomaticReportService(settings.max_upload_bytes, (self.downloads,))
        TestHandler.google_sync_service = GoogleSheetsSyncService(self.database, settings)

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

    def test_finish_has_no_follow_up_404_and_returns_explicit_inactive_state(self) -> None:
        self.client.request("POST", "/api/access", {"matricula": "001234"})
        uploaded_status, uploaded, _ = self.client.request(
            "POST", "/api/conferences",
            {"filename": "finalizar.csv", "content_base64": b64encode(b"CAIXA_ESTOQUE\n0001\n").decode()},
        )
        self.assertEqual(201, uploaded_status)
        public_id = uploaded["data"]["public_id"]
        self.assertEqual(200, self.client.request("POST", f"/api/conferences/{public_id}/start")[0])
        self.assertEqual(200, self.client.request("POST", f"/api/conferences/{public_id}/scan", {"caixa_estoque": "0001"})[0])

        finished_status, finished, _ = self.client.request("POST", f"/api/conferences/{public_id}/finish")
        active_status, active, _ = self.client.request("GET", "/api/conferences/active")
        pending_status, pending, _ = self.client.request("GET", "/api/sincronizacao/pendentes")

        self.assertEqual(200, finished_status)
        self.assertEqual("FINALIZADA", finished["data"]["status"])
        self.assertEqual("PENDENTE", finished["data"]["synchronization"]["status"])
        self.assertEqual(200, active_status)
        self.assertFalse(active["data"]["has_active_conference"])
        self.assertIsNone(active["data"]["conference"])
        self.assertEqual(200, pending_status)
        self.assertEqual(1, pending["data"]["pending"])

    def test_latest_wms_report_is_discovered_and_imported_by_opaque_id(self) -> None:
        report = self.downloads / "relatorio_ConsultaPaleteDistribuicaoAgrupada (3).csv"
        report.write_text("CAIXA_ESTOQUE\n000001\n", encoding="utf-8")
        self.client.request("POST", "/api/access", {"matricula": "001234"})

        status, body, _ = self.client.request("GET", "/api/conferences/latest-wms-report")

        self.assertEqual(200, status)
        self.assertTrue(body["data"]["found"])
        self.assertEqual(report.name, body["data"]["filename"])
        self.assertNotIn(str(report), json.dumps(body, ensure_ascii=False))
        status, body, _ = self.client.request(
            "POST", "/api/conferences/import-automatic",
            {"automatic_file_id": body["data"]["file_id"], "origin": "PORTAL", "operation": "DIGITAL"},
        )
        self.assertEqual(201, status)
        self.assertEqual(["000001"], [item["caixa_estoque"] for item in body["data"]["cartons"]])

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
        self.assertEqual("EM_ABERTO", start_body["data"]["status"])
        self.assertEqual("001234", start_body["data"]["collaborator"]["registration"])
        self.assertEqual(["000001", "CX-002"], [
            carton["caixa_estoque"] for carton in start_body["data"]["cartons"]
        ])
        self.assertRegex(
            start_body["data"]["importation"]["imported_at"],
            r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}$",
        )
        self.assertTrue(start_body["data"]["importation"]["imported_at_iso"].endswith("Z"))
        self.assertIsNotNone(start_body["data"]["active_attempt"]["started_at"])
        self.assertEqual(
            {"total_expected": 2, "total_confirmed": 0, "coverage_percent": 0.0},
            {key: start_body["data"]["summary"][key] for key in ("total_expected", "total_confirmed", "coverage_percent")},
        )

        valid_status, valid_body, _ = self.client.request(
            "POST",
            f"/api/conferences/{public_id}/scan",
            {"caixa_estoque": "000001"},
        )
        self.assertEqual(200, valid_status)
        self.assertEqual("CONFERIDA", valid_body["data"]["result"])
        self.assertEqual("Caixa conferida com sucesso.", valid_body["data"]["message"])

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
        self.assertEqual(1, finish_body["error"]["details"]["faltantes"])
        self.assertEqual(0, finish_body["error"]["details"]["duplicidades"])

        restart_status, restart_body, _ = self.client.request(
            "POST", f"/api/conferences/{public_id}/restart"
        )
        self.assertEqual(404, restart_status)
        self.assertEqual("NOT_FOUND", restart_body["error"]["code"])
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
        self.assertEqual(201, repeated_status)
        self.assertEqual("resumed", repeated_body["data"]["action"])
        self.assertEqual(public_id, repeated_body["data"]["public_id"])
        with self.database.connection() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM expected_cartons WHERE pallet_id = (SELECT id FROM pallets WHERE public_id = ?)",
                (public_id,),
            ).fetchone()[0]
        self.assertEqual(len(codes), total)

    def test_http_scan_reports_ambiguous_normalized_code_without_recording_it(self) -> None:
        self.client.request("POST", "/api/access", {"matricula": "001234"})
        payload = {
            "filename": "ambigua.csv",
            "content_base64": b64encode(
                b"CAIXA_ESTOQUE\n00012345\n0000012345\n"
            ).decode(),
        }
        status, imported, _ = self.client.request("POST", "/api/conferences", payload)
        self.assertEqual(201, status)
        public_id = imported["data"]["public_id"]
        self.client.request("POST", f"/api/conferences/{public_id}/start")
        status, response, _ = self.client.request(
            "POST", f"/api/conferences/{public_id}/scan", {"caixa_estoque": "12345"}
        )
        self.assertEqual(409, status)
        self.assertEqual("AMBIGUOUS_BOX_CODE", response["error"]["code"])
        self.assertEqual(["00012345", "0000012345"], response["error"]["details"]["matches"])
        with self.database.connection() as connection:
            scans = connection.execute(
                "SELECT COUNT(*) FROM scan_events WHERE pallet_id = (SELECT id FROM pallets WHERE public_id = ?)",
                (public_id,),
            ).fetchone()[0]
        self.assertEqual(0, scans)

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
        self.assertIsNotNone(conference["importation"]["imported_at"])
        self.client.request("POST", f"/api/conferences/{conference['public_id']}/start")

        status, recovered, _ = self.client.request("GET", "/api/conferences/active")
        self.assertEqual(200, status)
        self.assertTrue(recovered["data"]["has_active_conference"])
        restored = recovered["data"]["conference"]
        self.assertEqual(conference["public_id"], restored["public_id"])
        self.assertEqual(codes, [box["caixa_estoque"] for box in restored["cartons"]])

        status, blocked, _ = self.client.request("POST", "/api/conferences", payload)
        self.assertEqual(201, status)
        self.assertEqual("resumed", blocked["data"]["action"])
        self.assertEqual(conference["public_id"], blocked["data"]["public_id"])

    def test_finish_or_cancel_releases_active_state_and_import_timestamp_is_immutable(self) -> None:
        self.client.request("POST", "/api/access", {"matricula": "001234"})
        payload = {
            "filename": "uma-caixa.csv",
            "content_base64": b64encode(b"CAIXA_ESTOQUE;ROTA\n000001;A\n").decode(),
        }
        status, imported, _ = self.client.request("POST", "/api/conferences", payload)
        self.assertEqual(201, status)
        public_id = imported["data"]["public_id"]
        self.assertTrue(imported["data"]["importation"]["imported_at_iso"].endswith("Z"))
        _, started, _ = self.client.request("POST", f"/api/conferences/{public_id}/start")
        imported_at = started["data"]["importation"]["imported_at_iso"]
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
        self.assertEqual(public_id, next_import["data"]["public_id"])
        self.assertEqual("already_completed", next_import["data"]["action"])


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
        self.assertIn("routes.activeConference", self.conference_js)
        self.assertIn('$("#upload-card").hidden = isActiveConference(data)', self.conference_js)
        self.assertIn('$("#upload-card").hidden = false', self.conference_js)
        self.assertIn('await load(created.public_id, created)', self.upload_js)
        self.assertNotIn("new Date().toLocaleString", self.upload_js)

    def test_finished_conference_is_cleared_and_duplicate_remains_consultation_only(self) -> None:
        self.assertIn('$("#carton-list").replaceChildren()', self.conference_js)
        self.assertIn('$("#extra-list").replaceChildren()', self.conference_js)
        self.assertIn('$("#progress").value = 0', self.conference_js)
        self.assertIn('finishModal.close(); showImportCard();', self.conference_js)
        self.assertNotIn('if (state.latest_conference)', self.conference_js)

    def test_divergences_are_statuses_only_without_resolution_flow(self) -> None:
        for status in ("DUPLICADO", "FALTA"):
            self.assertIn(f'"{status}"', self.conference_js)
        self.assertIn('data.unexpected_cartons || []', self.conference_js)
        self.assertIn('item.append(code)', self.conference_js)
        self.assertNotIn('Resolver', self.conference_js)
        self.assertNotIn('/divergences/', self.conference_js)
        self.assertNotIn('summary-divergences-', self.conference)

    def test_manual_finish_modal_and_open_status_contract_exist(self) -> None:
        self.assertIn('id="finish-modal"', self.conference)
        self.assertIn('id="finish-form"', self.conference)
        self.assertIn("100% conferido — aguardando finalização", self.conference)
        self.assertIn('data.status === "EM_ABERTO"', self.conference_js)
        self.assertIn('finishModal.close(); showImportCard();', self.conference_js)
        self.assertIn('addEventListener("submit", async (event)', self.conference_js)

    def test_scan_keeps_caixa_estoque_as_string_and_restart_is_absent(self) -> None:
        self.assertIn('String(value ?? "").trim()', self.conference_js)
        self.assertIn("JSON.stringify({ caixa_estoque: caixaEstoque })", self.conference_js)
        self.assertNotIn("Number(caixaEstoque", self.conference_js)
        self.assertNotIn("parseInt(", self.conference_js)
        self.assertNotIn("parseFloat(", self.conference_js)
        self.assertNotIn("matricula", self.conference_js.casefold())
        self.assertNotIn('id="restart-modal"', self.conference)
        self.assertNotIn("Reiniciar conferência", self.conference)
        self.assertIn("Cancelar conferência", self.conference)
        self.assertIn('addEventListener("input", scheduleScan)', self.conference_js)
        self.assertIn('addEventListener("paste"', self.conference_js)
        self.assertIn("AUTO_SCAN_DELAY_MS", self.conference_js)


if __name__ == "__main__":
    unittest.main()
