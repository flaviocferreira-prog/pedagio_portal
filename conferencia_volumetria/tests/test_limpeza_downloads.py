from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from conferencia.controllers.conference_controller import ConferenceController
from conferencia.services.limpeza_downloads import limpar_relatorios_wms
from conferencia.services.conference_service import ValidationError


REPORT = "relatorio_ConsultaPaleteDistribuicaoAgrupada"


class LimpezaDownloadsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.downloads = Path(self.tempdir.name) / "Downloads"
        self.downloads.mkdir()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _report(self, number: int, *, suffix: str = "") -> Path:
        path = self.downloads / f"{REPORT}{suffix}.csv"
        path.write_text("CAIXA_ESTOQUE\n001\n", encoding="utf-8")
        timestamp = 1_700_000_000 + number
        os.utime(path, (timestamp, timestamp))
        return path

    def test_does_nothing_when_there_are_fewer_than_ten_reports(self) -> None:
        reports = [self._report(index, suffix=f" ({index})") for index in range(9)]

        result = limpar_relatorios_wms(downloads_directory=self.downloads)

        self.assertEqual(9, result.encontrados)
        self.assertEqual(0, result.removidos)
        self.assertTrue(all(path.exists() for path in reports))

    def test_keeps_the_nine_most_recent_matching_reports_only(self) -> None:
        oldest = self._report(0)
        reports = [self._report(index, suffix=f" ({index})") for index in range(1, 12)]
        unrelated = self.downloads / "outro_relatorio.csv"
        unrelated.write_text("preservar", encoding="utf-8")

        result = limpar_relatorios_wms(downloads_directory=self.downloads)

        self.assertEqual(12, result.encontrados)
        self.assertEqual(3, result.removidos)
        self.assertFalse(oldest.exists())
        self.assertFalse(reports[0].exists())
        self.assertFalse(reports[1].exists())
        self.assertTrue(all(path.exists() for path in reports[2:]))
        self.assertTrue(unrelated.exists())

    def test_preserves_the_report_used_by_the_current_import(self) -> None:
        current = self._report(0)
        reports = [self._report(index, suffix=f" ({index})") for index in range(1, 10)]

        result = limpar_relatorios_wms(
            arquivo_em_uso=current.name,
            downloads_directory=self.downloads,
        )

        self.assertEqual(10, result.encontrados)
        self.assertEqual(1, result.removidos)
        self.assertEqual(1, result.ignorados_em_uso)
        self.assertTrue(current.exists())
        self.assertFalse(reports[0].exists())

    def test_continues_when_a_report_is_in_use(self) -> None:
        locked = self._report(0)
        removable = self._report(1, suffix=" (1)")
        for index in range(2, 10):
            self._report(index, suffix=f" ({index})")

        with patch(
            "conferencia.services.limpeza_downloads._arquivo_em_uso",
            side_effect=lambda path: path == locked,
        ):
            result = limpar_relatorios_wms(downloads_directory=self.downloads)

        self.assertEqual(1, result.removidos)
        self.assertEqual(1, result.ignorados_em_uso)
        self.assertTrue(locked.exists())
        self.assertFalse(removable.exists())

    def test_automatic_import_cleans_only_after_the_import_succeeds(self) -> None:
        downloads = self.downloads
        reports = Mock()
        reports.read_for_import.return_value = (f"{REPORT}.csv", b"conteudo", downloads)
        controller = ConferenceController(Mock())

        with (
            patch.object(controller, "import_pallet", return_value={"public_id": "CONF-1"}),
            patch("conferencia.controllers.conference_controller.limpar_relatorios_wms") as cleanup,
        ):
            result = controller.import_automatic_pallet(
                {"automatic_file_id": "opaque"}, Mock(), reports
            )

        self.assertEqual({"public_id": "CONF-1"}, result)
        cleanup.assert_called_once_with(
            arquivo_em_uso=f"{REPORT}.csv",
            downloads_directory=downloads,
        )

    def test_automatic_import_does_not_clean_when_import_fails(self) -> None:
        reports = Mock()
        reports.read_for_import.return_value = (f"{REPORT}.csv", b"conteudo", self.downloads)
        controller = ConferenceController(Mock())

        with (
            patch.object(controller, "import_pallet", side_effect=ValidationError("inválido")),
            patch("conferencia.controllers.conference_controller.limpar_relatorios_wms") as cleanup,
            self.assertRaises(ValidationError),
        ):
            controller.import_automatic_pallet(
                {"automatic_file_id": "opaque"}, Mock(), reports
            )

        cleanup.assert_not_called()
