from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from conferencia.services.automatic_report_service import AutomaticReportService
from conferencia.services.conference_service import NotFoundError, ValidationError


class AutomaticReportServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.downloads = Path(self.tempdir.name) / "Downloads"
        self.downloads.mkdir()
        self.service = AutomaticReportService(1024, (self.downloads,))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_selects_the_newest_allowed_csv_by_timestamp(self) -> None:
        older = self.downloads / "relatorio_ConsultaPaleteDistribuicaoAgrupada (9).csv"
        newest = self.downloads / "relatorio_ConsultaPaleteDistribuicaoAgrupada (1).csv"
        older.write_text("old", encoding="utf-8")
        newest.write_text("new", encoding="utf-8")
        os.utime(older, ns=(1_000_000_000, 1_000_000_000))
        os.utime(newest, ns=(2_000_000_000, 2_000_000_000))

        result = self.service.latest()

        self.assertTrue(result["found"])
        self.assertEqual(newest.name, result["filename"])
        self.assertRegex(str(result["downloaded_at"]), r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}$")

    def test_ignores_invalid_names_and_temporary_extensions(self) -> None:
        for name in (
            "relatorio_ConsultaPaleteDistribuicaoAgrupada (x).csv",
            "relatorio_ConsultaPaleteDistribuicaoAgrupada (1).crdownload",
            "relatorio_ConsultaPaleteDistribuicaoAgrupada.csv.part",
            "outro.csv",
        ):
            (self.downloads / name).write_text("x", encoding="utf-8")
        self.assertEqual({"found": False}, self.service.latest())

    def test_read_rejects_file_changed_after_discovery(self) -> None:
        path = self.downloads / "relatorio_ConsultaPaleteDistribuicaoAgrupada.csv"
        path.write_text("first", encoding="utf-8")
        result = self.service.latest()
        path.write_text("changed", encoding="utf-8")

        with self.assertRaises(ValidationError) as error:
            self.service.read(result["file_id"])
        self.assertEqual("AUTOMATIC_FILE_CHANGED", error.exception.code)

    def test_token_is_single_use_and_does_not_accept_paths(self) -> None:
        path = self.downloads / "relatorio_ConsultaPaleteDistribuicaoAgrupada.csv"
        path.write_text("ok", encoding="utf-8")
        result = self.service.latest()
        self.assertEqual((path.name, b"ok"), self.service.read(result["file_id"]))
        with self.assertRaises(NotFoundError):
            self.service.read(result["file_id"])
        with self.assertRaises(NotFoundError):
            self.service.read(str(path))
