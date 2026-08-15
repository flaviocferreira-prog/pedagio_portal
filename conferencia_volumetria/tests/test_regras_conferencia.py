from __future__ import annotations

import tempfile
import unittest
from base64 import b64encode
from pathlib import Path

from conferencia.domain.entities import CollaboratorContext
from conferencia.infrastructure.database import SQLiteDatabase
from conferencia.infrastructure.settings import AppSettings
from conferencia.regras_conferencia import identificar_classe_palete
from conferencia.repositories.colaborador_repository import ColaboradorRepository
from conferencia.repositories.pallet_repository import PalletRepository
from conferencia.services.conference_service import ConferenceService, ConflictError
from conferencia.services.upload import UploadService


class ItemReader:
    def __init__(self, items: list[dict[str, str]]) -> None:
        self.items = items

    def read_expected_items(
        self, file_path: Path, extension: str
    ) -> list[dict[str, str]]:
        return self.items


class RegrasConferenciaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        database = SQLiteDatabase(Path(self.tempdir.name) / "test.db")
        database.initialize()
        collaborator = ColaboradorRepository(database).create("000123", "OPERADOR TESTE")
        self.actor = CollaboratorContext(
            int(collaborator["id"]),
            str(collaborator["matricula"]),
            str(collaborator["nome"]),
        )
        self.repository = PalletRepository(database)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _completed_conference(
        self, operation: str, prefix: str, classes: list[str]
    ) -> tuple[ConferenceService, str]:
        items = [
            {"caixa_estoque": f"{prefix}-{index}", "ds_classe": classe}
            for index, classe in enumerate(classes, start=1)
        ]
        service = ConferenceService(self.repository)
        created = UploadService(
            service,
            ItemReader(items),
            AppSettings(temporary_directory=Path(self.tempdir.name) / "uploads"),
        ).importar_manual(
            {
                "filename": f"{prefix}.csv",
                "content_base64": b64encode(prefix.encode()).decode(),
                "origin": "PORTAL",
                "operation": operation,
            }, self.actor,
        )
        public_id = created["public_id"]
        service.start_pallet(public_id, self.actor)
        for item in items:
            service.scan_carton(public_id, item["caixa_estoque"], self.actor)
        return service, public_id

    def test_identifies_unique_and_mixed_classes_in_one_place(self) -> None:
        self.assertEqual(
            "VESTUARIO", identificar_classe_palete(["vestuario", " VESTUARIO "])
        )
        self.assertEqual(
            "MISTO", identificar_classe_palete(["vestuario", "CALCADOS", ""])
        )

    def test_cross_can_finish_a_mixed_class_pallet(self) -> None:
        service, public_id = self._completed_conference(
            "CROSS", "CROSS", ["VESTUARIO", "CALCADOS"]
        )

        finished = service.finish_pallet(public_id, self.actor)

        self.assertFalse(finished["summary"]["class_requires_treatment"])
        self.assertTrue(finished["summary"]["can_finish"])
        self.assertEqual("MISTO", finished["pallet_class"])
        self.assertEqual("FINALIZADA", finished["status"])

    def test_digital_cannot_finish_a_mixed_class_pallet(self) -> None:
        service, public_id = self._completed_conference(
            "DIGITAL", "DIGITAL", ["VESTUARIO", "CALCADOS"]
        )

        with self.assertRaises(ConflictError) as raised:
            service.finish_pallet(public_id, self.actor)

        summary = service.get_pallet(public_id)["summary"]
        self.assertTrue(summary["class_requires_treatment"])
        self.assertFalse(summary["can_finish"])
        self.assertEqual("PALLET_MISTO_NAO_PERMITIDO", raised.exception.code)
        self.assertIn("DIGITAL não permite finalizar palete com classe MISTO", str(raised.exception))
        self.assertEqual("EM_ABERTO", service.get_pallet(public_id)["status"])

    def test_nike_store_cannot_finish_a_mixed_class_pallet(self) -> None:
        service, public_id = self._completed_conference(
            "NIKESTORE", "NIKE", ["VESTUARIO", "CALCADOS"]
        )

        with self.assertRaises(ConflictError) as raised:
            service.finish_pallet(public_id, self.actor)

        summary = service.get_pallet(public_id)["summary"]
        self.assertTrue(summary["class_requires_treatment"])
        self.assertFalse(summary["can_finish"])
        self.assertEqual("PALLET_MISTO_NAO_PERMITIDO", raised.exception.code)
        self.assertIn("NIKE STORE não permite finalizar palete com classe MISTO", str(raised.exception))
        self.assertEqual("EM_ABERTO", service.get_pallet(public_id)["status"])

    def test_digital_and_nike_store_can_finish_a_single_class_pallet(self) -> None:
        for operation, prefix in (("DIGITAL", "DIGITAL-UNICA"), ("NIKESTORE", "NIKE-UNICA")):
            with self.subTest(operation=operation):
                service, public_id = self._completed_conference(
                    operation, prefix, ["VESTUARIO", "VESTUARIO"]
                )

                finished = service.finish_pallet(public_id, self.actor)

                self.assertEqual("VESTUARIO", finished["pallet_class"])
                self.assertEqual("FINALIZADA", finished["status"])


if __name__ == "__main__":
    unittest.main()
