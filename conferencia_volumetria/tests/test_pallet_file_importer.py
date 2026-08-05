from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from conferencia.domain.box_codes import content_hash_caixa_estoque
from conferencia.readers.excel_reader import ExcelReadError, PalletFileImporter


class PalletFileImporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name)
        self.importer = PalletFileImporter()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _csv(self, name: str, text: str) -> Path:
        path = self.path / name
        path.write_text(text, encoding="utf-8-sig")
        return path

    def _xlsx(self, name: str, values: list[object], header: str = "CAIXA_ESTOQUE") -> Path:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append([header])
        for value in values:
            sheet.append([value])
        path = self.path / name
        workbook.save(path)
        workbook.close()
        return path

    def test_csv_utf8_bom_semicolon_and_comma_keep_twenty_digits(self) -> None:
        code = "00009999990462159232"
        for name, separator in (("semicolon.csv", ";"), ("comma.csv", ",")):
            source = self._csv(name, f"CAIXA_ESTOQUE{separator}OUTRA\n{code}{separator}A\n")
            self.assertEqual([code], self.importer.read_carton_codes(source, ".csv"))

    def test_xlsx_text_preserves_zeros_and_varchar2_header(self) -> None:
        code = "00009999990462159232"
        source = self._xlsx("text.xlsx", [code], "  CAIXA_ESTOQUE   VARCHAR2 ")
        self.assertEqual([code], self.importer.read_carton_codes(source, ".xlsx"))

    def test_real_duplicates_are_preserved_as_individual_expected_items(self) -> None:
        source = self._csv("duplicate.csv", "CAIXA_ESTOQUE\n0001\n0002\n0001\n")
        self.assertEqual(["0001", "0002", "0001"], self.importer.read_carton_codes(source, ".csv"))

    def test_empty_cells_do_not_create_cartons(self) -> None:
        source = self._csv("empty.csv", "CAIXA_ESTOQUE\n\n  \n0001\n")
        self.assertEqual(["0001"], self.importer.read_carton_codes(source, ".csv"))

    def test_long_numeric_xlsx_is_rejected_as_precision_risk(self) -> None:
        source = self._xlsx("numeric.xlsx", [9999999904621592320])
        with self.assertRaisesRegex(ExcelReadError, r"codigos longos armazenados como numero"):
            self.importer.read_carton_codes(source, ".xlsx")

    def test_signature_is_order_and_filename_independent_but_count_sensitive(self) -> None:
        values = ["0002", "0001", "0003"]
        self.assertEqual(content_hash_caixa_estoque(values), content_hash_caixa_estoque(list(reversed(values))))
        self.assertNotEqual(content_hash_caixa_estoque(values), content_hash_caixa_estoque(["0001", "0002", "0002"]))
        self.assertNotEqual(content_hash_caixa_estoque(values), content_hash_caixa_estoque(["0001", "0002", "0004"]))


if __name__ == "__main__":
    unittest.main()
