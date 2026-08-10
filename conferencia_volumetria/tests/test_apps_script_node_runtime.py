from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


class AppsScriptNodeRuntimeTests(unittest.TestCase):
    def test_apps_script_executes_against_in_memory_google_mocks(self) -> None:
        node = shutil.which("node") or shutil.which("nodejs")
        if node is None:
            self.skipTest("Node.js indisponivel; harness Apps Script nao executado.")

        root = Path(__file__).resolve().parents[1]
        harness = root / "tests" / "apps_script_runtime_harness.cjs"
        completed = subprocess.run(
            [node, str(harness)],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        diagnostics = (
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
        self.assertEqual(0, completed.returncode, diagnostics)
        output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(output_lines, diagnostics)
        result = json.loads(output_lines[-1])
        self.assertEqual({"ok": True, "scenarios": 7}, result)


if __name__ == "__main__":
    unittest.main()
