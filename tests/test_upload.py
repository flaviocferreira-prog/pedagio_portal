from __future__ import annotations

import os
import tempfile
import unittest
from base64 import b64encode
from pathlib import Path
from unittest.mock import patch

from conferencia.domain.entities import CollaboratorContext
from conferencia.infrastructure.database import SQLiteDatabase
from conferencia.infrastructure.settings import AppSettings
from conferencia.readers.excel_reader import PalletFileImporter
from conferencia.repositories.colaborador_repository import ColaboradorRepository
from conferencia.repositories.pallet_repository import PalletRepository
from conferencia.services.conference_service import NotFoundError
from conferencia.services.conference_service import ConferenceService
from conferencia.services.upload import REPORT_NAME, UploadService


class UploadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.downloads = root / "Downloads"
        self.downloads.mkdir()
        database = SQLiteDatabase(root / "test.db")
        database.initialize()
        collaborator = ColaboradorRepository(database).create("000123", "OPERADOR TESTE")
        self.actor = CollaboratorContext(
            int(collaborator["id"]), str(collaborator["matricula"]), str(collaborator["nome"])
        )
        self.conferences = ConferenceService(PalletRepository(database))
        self.upload = UploadService(
            self.conferences,
            PalletFileImporter(),
            AppSettings(temporary_directory=root / "uploads"),
            (self.downloads,),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def _content(code: str) -> bytes:
        return f"CAIXA_ESTOQUE\n{code}\n".encode()

    def _report(self, position: int, code: str | None = None) -> Path:
        suffix = "" if position == 0 else f" ({position})"
        path = self.downloads / f"{REPORT_NAME}{suffix}.csv"
        path.write_bytes(self._content(code or f"{position:06d}"))
        timestamp = 1_700_000_000 + position
        os.utime(path, ns=(timestamp * 1_000_000_000, timestamp * 1_000_000_000))
        return path

    @staticmethod
    def _payload(filename: str, content: bytes) -> dict[str, str]:
        return {
            "filename": filename,
            "content_base64": b64encode(content).decode(),
            "origin": "PORTAL",
            "operation": "DIGITAL",
        }

    def _finish(self, public_id: str, code: str) -> None:
        self.conferences.start_pallet(public_id, self.actor)
        self.conferences.scan_carton(public_id, code, self.actor)
        self.conferences.finish_pallet(public_id, self.actor)

    def test_no_report_returns_not_found(self) -> None:
        self.assertEqual({"found": False}, self.upload.localizar_ultimo_relatorio())

    def test_one_and_multiple_reports_select_the_newest_mtime(self) -> None:
        only = self._report(1)
        result = self.upload.localizar_ultimo_relatorio()
        self.assertEqual(only.name, result["filename"])

        newest = self._report(3)
        self._report(2)
        self.assertEqual(newest.name, self.upload.localizar_ultimo_relatorio()["filename"])
        self.assertEqual([newest.name, f"{REPORT_NAME} (2).csv", only.name], [
            report.path.name for report in self.upload.listar_relatorios()
        ])

    def test_automatic_selection_stays_fixed_after_discovery(self) -> None:
        selected = self._report(1, "000001")
        discovery = self.upload.localizar_ultimo_relatorio()
        self._report(2, "000002")

        result = self.upload.importar_automaticamente(
            {"automatic_file_id": discovery["file_id"], "origin": "PORTAL", "operation": "DIGITAL"},
            self.actor,
        )

        self.assertEqual(selected.name, result["source_filename"])
        self.assertEqual(["000001"], [box["caixa_estoque"] for box in result["cartons"]])

    def test_latest_finalized_report_is_reported_without_falling_back(self) -> None:
        older = self._report(1, "000001")
        opened = self.upload.importar_manual(self._payload(older.name, older.read_bytes()), self.actor)
        self._finish(opened["public_id"], "000001")
        latest = self._report(2, "000001")
        self._report(0, "000002")

        discovery = self.upload.localizar_ultimo_relatorio()
        result = self.upload.importar_automaticamente(
            {"automatic_file_id": discovery["file_id"], "origin": "PORTAL", "operation": "DIGITAL"},
            self.actor,
        )

        self.assertEqual(latest.name, discovery["filename"])
        self.assertEqual("already_completed", result["action"])
        self.assertEqual(opened["public_id"], result["public_id"])

    def test_open_content_is_resumed_and_manual_upload_preserves_zeros(self) -> None:
        content = self._content("000123")
        first = self.upload.importar_manual(self._payload("manual.csv", content), self.actor)
        second = self.upload.importar_manual(self._payload("renamed.csv", content), self.actor)

        self.assertEqual("created", first["action"])
        self.assertEqual("resumed", second["action"])
        self.assertEqual(first["public_id"], second["public_id"])
        self.assertEqual("000123", first["cartons"][0]["caixa_estoque"])

    def test_automatic_token_is_single_use(self) -> None:
        self._report(1)
        discovery = self.upload.localizar_ultimo_relatorio()
        self.upload.obter_dados_arquivo(discovery["file_id"])
        with self.assertRaises(NotFoundError):
            self.upload.obter_dados_arquivo(discovery["file_id"])

    def test_manual_upload_does_not_clean_downloads(self) -> None:
        reports = [self._report(index) for index in range(11)]
        self.upload.importar_manual(self._payload("manual.csv", self._content("009999")), self.actor)
        self.assertTrue(all(path.exists() for path in reports))

    def test_cleanup_keeps_all_reports_at_exactly_ten(self) -> None:
        reports = [self._report(index) for index in range(10)]
        self.upload.limpar_downloads(self.downloads)
        self.assertTrue(all(path.exists() for path in reports))

    def test_cleanup_removes_one_at_eleven_and_only_matching_reports(self) -> None:
        reports = [self._report(index) for index in range(11)]
        other_csv = self.downloads / "outro.csv"
        other_csv.write_text("preservar", encoding="utf-8")

        self.upload.limpar_downloads(self.downloads)

        self.assertFalse(reports[0].exists())
        self.assertTrue(all(path.exists() for path in reports[1:]))
        self.assertTrue(other_csv.exists())

    def test_cleanup_removes_oldest_reports_above_ten(self) -> None:
        reports = [self._report(index) for index in range(15)]

        self.upload.limpar_downloads(self.downloads)

        self.assertTrue(all(not path.exists() for path in reports[:5]))
        self.assertTrue(all(path.exists() for path in reports[5:]))

    def test_cleanup_never_removes_a_locked_report(self) -> None:
        locked = self._report(0)
        removable = self._report(1)
        for index in range(2, 11):
            self._report(index)

        with patch.object(self.upload, "_arquivo_em_uso", side_effect=lambda path: path == locked):
            self.upload.limpar_downloads(self.downloads)

        self.assertTrue(locked.exists())
        self.assertFalse(removable.exists())

    def test_onedrive_redirected_downloads_directory_is_supported(self) -> None:
        onedrive_downloads = Path(self.tempdir.name) / "OneDrive" / "Downloads"
        onedrive_downloads.mkdir(parents=True)
        report = onedrive_downloads / f"{REPORT_NAME}.csv"
        report.write_bytes(self._content("000777"))
        redirected = UploadService(
            self.conferences,
            PalletFileImporter(),
            AppSettings(temporary_directory=Path(self.tempdir.name) / "uploads"),
            (onedrive_downloads,),
        )

        result = redirected.localizar_ultimo_relatorio()

        self.assertEqual(report.name, result["filename"])
        self.assertEqual((onedrive_downloads.resolve(),), redirected.localizar_downloads())
