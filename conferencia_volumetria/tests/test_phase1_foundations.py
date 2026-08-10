from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from conferencia.infrastructure.settings import _sync_float, _sync_int
from conferencia.infrastructure.logging_utils import (
    log_http_access,
    log_sanitized_exception,
    redact_for_log,
)
from conferencia.infrastructure.runtime import (
    is_supported_python,
    unsupported_python_message,
)


class RuntimeRequirementTests(unittest.TestCase):
    def test_python_310_is_rejected_with_actionable_message(self) -> None:
        self.assertFalse(is_supported_python((3, 10, 14)))
        message = unsupported_python_message((3, 10, 14))
        self.assertIn("Python 3.10.14 encontrado", message)
        self.assertIn("Python 3.11 ou superior", message)
        self.assertIn("instalar.bat", message)

    def test_python_311_and_newer_are_supported(self) -> None:
        self.assertTrue(is_supported_python((3, 11, 0)))
        self.assertTrue(is_supported_python((3, 14, 5)))

    def test_documentation_and_windows_entrypoints_enforce_python_311(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.assertIn("Python 3.11 ou superior", (root / "README.md").read_text(encoding="utf-8"))
        self.assertIn("Python >= 3.11", (root / "requirements.txt").read_text(encoding="utf-8"))
        for filename in ("instalar.bat", "iniciar.bat"):
            content = (root / filename).read_text(encoding="utf-8")
            self.assertIn("sys.version_info >= (3, 11)", content)
            self.assertIn("Python %PYTHON_VERSION% encontrado", content)


class SanitizedLoggingTests(unittest.TestCase):
    def test_redaction_removes_values_from_urls_headers_and_payloads(self) -> None:
        secret_values = ("url-secret", "api-secret", "auth-secret", "cookie-secret", "pass-secret")
        redacted = redact_for_log(
            "GET /api?token=url-secret&api_key=api-secret "
            "Authorization: Bearer auth-secret\n"
            "Cookie: cookie-secret password=pass-secret"
        )
        self.assertIn("[REDACTED]", redacted)
        for value in secret_values:
            self.assertNotIn(value, redacted)

    def test_operational_logs_do_not_emit_secret_values(self) -> None:
        secret = "never-log-this-token"
        with self.assertLogs("conferencia", level="INFO") as captured:
            log_http_access('"%s" %s', (f"POST /api?token={secret}", 200))
        with self.assertLogs("conferencia", level="ERROR") as errors:
            log_sanitized_exception("request", RuntimeError(f"password={secret}"))
        self.assertNotIn(secret, "\n".join(captured.output + errors.output))

    def test_sync_shared_secret_name_is_redacted(self) -> None:
        secret = "sync-secret-must-never-appear"
        redacted = redact_for_log(f"SYNC_SHARED_SECRET={secret}")
        self.assertIn("[REDACTED]", redacted)
        self.assertNotIn(secret, redacted)


class SyncSettingsTests(unittest.TestCase):
    def test_invalid_numeric_values_fall_back_without_breaking_portal_import(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TEST_SYNC_INT": "not-a-number",
                "TEST_SYNC_FLOAT": "not-a-number",
            },
        ):
            self.assertEqual(30, _sync_int("TEST_SYNC_INT", 30, minimum=1))
            self.assertEqual(2.0, _sync_float("TEST_SYNC_FLOAT", 2.0, minimum=0.0))

    def test_non_finite_and_out_of_range_values_are_safely_bounded(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TEST_SYNC_NAN": "nan",
                "TEST_SYNC_INFINITY": "inf",
                "TEST_SYNC_BATCH": "1000",
            },
        ):
            self.assertEqual(2.0, _sync_float("TEST_SYNC_NAN", 2.0, minimum=0.0))
            self.assertEqual(2.0, _sync_float("TEST_SYNC_INFINITY", 2.0, minimum=0.0))
            self.assertEqual(
                100,
                _sync_int("TEST_SYNC_BATCH", 10, minimum=1, maximum=100),
            )
