from __future__ import annotations

import unittest
from pathlib import Path


class WindowsEntrypointContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.installer = (root / "INSTALAR_PEDAGIO.bat").read_text(encoding="utf-8")
        cls.launcher = (root / "INICIAR_PEDAGIO.bat").read_text(encoding="utf-8")

    def test_installer_preserves_operational_data_and_supports_repair(self) -> None:
        self.assertIn("--repair", self.installer)
        self.assertIn(".venv.backup", self.installer)
        self.assertIn('move "%VENV%" "%VENV_BACKUP%"', self.installer)
        self.assertIn('if not exist "%ROOT%\\.env"', self.installer)
        self.assertIn("data\\temporary_uploads", self.installer)
        self.assertNotIn("rmdir /s", self.installer.lower())
        self.assertNotIn("del /", self.installer.lower())

    def test_installer_checks_supported_python_and_dependencies(self) -> None:
        self.assertIn("3.13 3.12 3.11 3.14", self.installer)
        self.assertIn("sys.version_info >= (3, 11)", self.installer)
        self.assertIn("-m venv", self.installer)
        self.assertIn("-m pip install", self.installer)
        self.assertIn("-m pip check", self.installer)
        self.assertIn("import openpyxl, tzdata, dotenv", self.installer)

    def test_portable_distribution_uses_the_bundled_executable(self) -> None:
        self.assertIn("PORTABLE_EXE", self.installer)
        self.assertIn("nenhuma biblioteca sera instalada", self.installer)
        self.assertIn("APP_EXE", self.launcher)
        self.assertIn("PEDAGIO_PORTAL.exe", self.launcher)

    def test_installer_uses_per_user_shortcut_without_policy_bypass(self) -> None:
        self.assertIn("DesktopDirectory", self.installer)
        self.assertIn("WScript.Shell", self.installer)
        self.assertIn("$shortcut.TargetPath=$launcher", self.installer)
        self.assertIn("alerta_logistica.ico", self.installer)
        self.assertNotIn("ExecutionPolicy Bypass", self.installer)

    def test_launcher_uses_configured_port_and_identifies_its_server(self) -> None:
        self.assertIn("CONFERENCE_PORT", self.launcher)
        self.assertIn("access-form", self.launcher)
        self.assertIn("static/js/access.js", self.launcher)
        self.assertIn("Get-NetTCPConnection", self.launcher)
        self.assertIn("servidor.pid", self.launcher)
        self.assertNotIn("Stop-Process", self.launcher)
        self.assertNotIn("taskkill", self.launcher.lower())
        self.assertNotIn("ExecutionPolicy Bypass", self.launcher)

    def test_launcher_does_not_open_the_external_senior_url(self) -> None:
        self.assertNotIn("platform.senior.com.br", self.launcher)


if __name__ == "__main__":
    unittest.main()
