from __future__ import annotations

import json
import unittest
from pathlib import Path


class AppsScriptContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.code = (root / "google_apps_script" / "Code.gs").read_text(encoding="utf-8")
        cls.manifest = json.loads(
            (root / "google_apps_script" / "appsscript.json").read_text(encoding="utf-8")
        )

    def test_manifest_and_backend_only_security_contract(self) -> None:
        self.assertEqual("V8", self.manifest["runtimeVersion"])
        self.assertIn('getProperty("SPREADSHEET_ID")', self.code)
        self.assertIn('getProperty("SYNC_SHARED_SECRET")', self.code)
        self.assertIn("constantTimeEquals_", self.code)
        self.assertIn("safeCellValue_", self.code)
        self.assertIn("/^\\s*[=+\\-@]/", self.code)
        self.assertIn("LockService.getScriptLock()", self.code)
        for forbidden in ("HtmlService", "OAuth", "postMessage", "BroadcastChannel"):
            self.assertNotIn(forbidden, self.code)

    def test_all_relational_tabs_and_sources_are_declared(self) -> None:
        expected = {
            "CONFERENCIAS": "conference",
            "ITENS": "items",
            "BIPAGENS": "scans",
            "TENTATIVAS": "attempts",
            "CONFIRMACOES": "confirmations",
            "DIVERGENCIAS": "divergences",
            "SOBRAS_RESUMO": "surplus_summaries",
            "SOBRAS": "surpluses",
            "TRATAMENTOS_SOBRA": "treatments",
            "ITENS_TRATAMENTO": "treatment_items",
            "AUDITORIA": "audit",
        }
        for sheet, source in expected.items():
            self.assertIn(f"{sheet}: {{", self.code)
            self.assertIn(f'source: "{source}"', self.code)

    def test_idempotent_commit_marker_is_written_after_children(self) -> None:
        child_write = self.code.index(
            "appendRelationsBatch_(sheets, payloads);"
        )
        marker_write = self.code.index(
            "appendConferencesBatch_(sheets.CONFERENCIAS, payloads);"
        )
        self.assertLess(child_write, marker_write)
        self.assertIn('committedState === "COMPLETE"', self.code)
        self.assertIn('status: "ALREADY_EXISTS"', self.code)
        self.assertIn('"EXISTING_DATA_MISMATCH"', self.code)
        self.assertIn("removeConferenceRows_(sheets, snapshots, conferenceIds)", self.code)
        self.assertIn("snapshotSheets_(sheets)", self.code)
        self.assertIn("sheet.getRangeList(textRanges)", self.code)
        self.assertIn(
            "synchronizeConferences_(sheets, retrySnapshots, [payload])",
            self.code,
        )

    def test_sync_response_is_per_conference_json(self) -> None:
        for status in ("INSERTED", "ALREADY_EXISTS", "ERROR"):
            self.assertIn(f'"{status}"', self.code)
        self.assertIn("id_conferencia: conferenceId", self.code)
        self.assertIn("ContentService.MimeType.JSON", self.code)


if __name__ == "__main__":
    unittest.main()
