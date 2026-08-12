from __future__ import annotations

import unittest
from pathlib import Path


class AppsScriptContractTests(unittest.TestCase):
    def test_google_script_uses_the_same_tabs_keys_lock_and_text_conversion(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "google_apps_script" / "Code.gs").read_text(encoding="utf-8")
        for tab, key in {
            "CONFERENCIAS": "ID_CONFERENCIA",
            "CAIXAS_CONFERENCIA": "ID_ITEM",
            "BIPAGENS": "ID_EVENTO",
            "DIVERGENCIAS": "ID_DIVERGENCIA",
            "TENTATIVAS": "ID_TENTATIVA",
            "AUDITORIA": "ID_AUDITORIA",
            "COLABORADORES": "ID_COLABORADOR",
        }.items():
            self.assertIn(tab, script)
            self.assertIn(key, script)
        self.assertIn("LockService.getScriptLock()", script)
        self.assertIn("GOOGLE_SYNC_TOKEN", script)
        self.assertIn("String(value)", script)
        self.assertNotIn("SpreadsheetApp.create", script)
