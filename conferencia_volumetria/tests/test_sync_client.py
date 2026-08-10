from __future__ import annotations

import json
import socket
import threading
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from conferencia.sync.client import AppsScriptClient, AppsScriptError


class _LocalScenario:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.responder = lambda _request: (
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {},
            {"ok": False},
        )

    def handle(self, request: dict[str, object]) -> tuple[int, dict[str, str], object]:
        self.requests.append(request)
        return self.responder(request)


class AppsScriptClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = _LocalScenario()
        scenario = self.scenario

        class LocalHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                self._respond()

            def do_POST(self) -> None:
                self._respond()

            def _respond(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                request = {
                    "method": self.command,
                    "path": self.path,
                    "headers": {name.lower(): value for name, value in self.headers.items()},
                    "body": body,
                }
                status, headers, payload = scenario.handle(request)
                if isinstance(payload, (dict, list)):
                    response_body = json.dumps(payload).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                elif isinstance(payload, str):
                    response_body = payload.encode("utf-8")
                    content_type = "text/plain; charset=utf-8"
                else:
                    response_body = bytes(payload)
                    content_type = "application/octet-stream"
                self.send_response(int(status))
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(response_body)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                if response_body:
                    self.wfile.write(response_body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/exec"
        self.secret = "test-shared-secret"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def client(self, url: str | None = None) -> AppsScriptClient:
        return AppsScriptClient(
            url or self.url,
            self.secret,
            connect_timeout_seconds=0.2,
            read_timeout_seconds=0.2,
        )

    @staticmethod
    def valid_health() -> dict[str, object]:
        return {"ok": True, "service": "apps-script", "sheets": True}

    def test_health_check_accepts_complete_operational_response(self) -> None:
        self.scenario.responder = lambda _request: (200, {}, self.valid_health())

        result = self.client().health_check()

        self.assertTrue(result["ok"])
        self.assertEqual("apps-script", result["service"])
        self.assertTrue(result["sheets"])
        self.assertEqual(1, len(self.scenario.requests))
        self.assertEqual("POST", self.scenario.requests[0]["method"])
        payload = json.loads(self.scenario.requests[0]["body"])
        self.assertEqual(
            {"action": "health", "shared_secret": self.secret},
            payload,
        )

    def test_health_check_rejects_remote_authentication_error_without_retry(self) -> None:
        self.scenario.responder = lambda _request: (
            200,
            {},
            {
                "ok": False,
                "service": "apps-script",
                "sheets": False,
                "error": {"code": "UNAUTHORIZED"},
            },
        )

        with self.assertRaises(AppsScriptError) as captured:
            self.client().health_check()

        self.assertEqual("UNAUTHORIZED", captured.exception.code)
        self.assertFalse(captured.exception.retryable)
        self.assertNotIn(self.secret, str(captured.exception))

    def test_permanent_contract_errors_are_not_retryable(self) -> None:
        for code in ("BATCH_TOO_LARGE", "INVALID_PAYLOAD", "SHEET_SCHEMA_MISMATCH"):
            with self.subTest(code=code):
                self.scenario.responder = lambda _request, value=code: (
                    200,
                    {},
                    {"ok": False, "error": {"code": value}},
                )
                with self.assertRaises(AppsScriptError) as captured:
                    self.client().health_check()
                self.assertEqual(code, captured.exception.code)
                self.assertFalse(captured.exception.retryable)

    def test_health_check_rejects_incomplete_success_shape_as_retryable(self) -> None:
        self.scenario.responder = lambda _request: (
            200,
            {},
            {"ok": True, "service": "unexpected", "sheets": False},
        )

        with self.assertRaises(AppsScriptError) as captured:
            self.client().health_check()

        self.assertEqual("INVALID_RESPONSE", captured.exception.code)
        self.assertTrue(captured.exception.retryable)

    def test_sync_posts_contract_and_never_places_secret_in_url(self) -> None:
        conferences = [{"id_conferencia": "CONF-1", "conference": {"local_id": 1}}]
        expected_results = [{"id_conferencia": "CONF-1", "status": "INSERTED"}]
        self.scenario.responder = lambda _request: (
            200,
            {},
            {"ok": True, "results": expected_results},
        )
        client = self.client(f"{self.url}?source=portal")

        results = client.send_batch(conferences)

        self.assertEqual(expected_results, results)
        request = self.scenario.requests[0]
        self.assertEqual("POST", request["method"])
        self.assertEqual("/exec?source=portal", request["path"])
        self.assertNotIn(self.secret, request["path"])
        self.assertEqual("application/json; charset=utf-8", request["headers"]["content-type"])
        self.assertEqual(
            {
                "action": "sync",
                "conferences": conferences,
                "shared_secret": self.secret,
            },
            json.loads(request["body"]),
        )

    def test_malformed_json_is_retryable_protocol_error(self) -> None:
        self.scenario.responder = lambda _request: (200, {}, b"{invalid-json")

        with self.assertRaises(AppsScriptError) as captured:
            self.client().health_check()

        self.assertEqual("INVALID_JSON", captured.exception.code)
        self.assertTrue(captured.exception.retryable)

    def test_timeout_too_early_rate_limit_and_5xx_are_retryable(self) -> None:
        for status in (408, 425, 429, 500, 503):
            with self.subTest(status=status):
                self.scenario.responder = lambda _request, value=status: (
                    value,
                    {},
                    {"ok": False},
                )
                with self.assertRaises(AppsScriptError) as captured:
                    self.client().health_check()
                self.assertEqual(f"HTTP_{status}", captured.exception.code)
                self.assertTrue(captured.exception.retryable)

    def test_4xx_is_non_retryable(self) -> None:
        self.scenario.responder = lambda _request: (403, {}, {"ok": False})

        with self.assertRaises(AppsScriptError) as captured:
            self.client().health_check()

        self.assertEqual("HTTP_403", captured.exception.code)
        self.assertFalse(captured.exception.retryable)

    def test_302_changes_post_to_get_without_resending_secret(self) -> None:
        def responder(request: dict[str, object]) -> tuple[int, dict[str, str], object]:
            if request["path"] == "/exec":
                return 302, {"Location": "/result?ticket=public"}, b""
            self.assertEqual("/result?ticket=public", request["path"])
            return 200, {}, self.valid_health()

        self.scenario.responder = responder

        result = self.client().health_check()

        self.assertTrue(result["ok"])
        self.assertEqual(2, len(self.scenario.requests))
        first, redirected = self.scenario.requests
        self.assertEqual("POST", first["method"])
        self.assertEqual(self.secret, json.loads(first["body"])["shared_secret"])
        self.assertEqual("GET", redirected["method"])
        self.assertEqual(b"", redirected["body"])
        self.assertNotIn("content-type", redirected["headers"])
        self.assertNotIn(self.secret, redirected["path"])

    def test_socket_timeout_is_deterministically_retryable(self) -> None:
        class TimeoutConnection:
            sock = None

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def request(self, *_args: object, **_kwargs: object) -> None:
                raise socket.timeout("controlled timeout")

            def close(self) -> None:
                pass

        with patch(
            "conferencia.sync.client.http.client.HTTPConnection",
            TimeoutConnection,
        ):
            with self.assertRaises(AppsScriptError) as captured:
                self.client().health_check()

        self.assertEqual("TIMEOUT", captured.exception.code)
        self.assertTrue(captured.exception.retryable)

    def test_connection_failure_is_deterministically_retryable(self) -> None:
        class FailedConnection:
            sock = None

            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def request(self, *_args: object, **_kwargs: object) -> None:
                raise ConnectionRefusedError("controlled refusal")

            def close(self) -> None:
                pass

        with patch(
            "conferencia.sync.client.http.client.HTTPConnection",
            FailedConnection,
        ):
            with self.assertRaises(AppsScriptError) as captured:
                self.client().health_check()

        self.assertEqual("CONNECTION_ERROR", captured.exception.code)
        self.assertTrue(captured.exception.retryable)

    def test_plain_http_is_only_allowed_for_loopback(self) -> None:
        client = AppsScriptClient("http://example.com/exec", self.secret)

        self.assertFalse(client.configured)
        with self.assertRaises(AppsScriptError) as captured:
            client.health_check()

        self.assertEqual("NOT_CONFIGURED", captured.exception.code)
        self.assertFalse(captured.exception.retryable)

    def test_url_credentials_are_rejected_before_any_request(self) -> None:
        client = AppsScriptClient("https://user:password@example.com/exec", self.secret)

        self.assertFalse(client.configured)
        with self.assertRaises(AppsScriptError) as captured:
            client.health_check()

        self.assertEqual("NOT_CONFIGURED", captured.exception.code)

    def test_307_does_not_resend_secret_to_a_different_port(self) -> None:
        other_port = self.server.server_port + 1
        self.scenario.responder = lambda _request: (
            307,
            {"Location": f"http://127.0.0.1:{other_port}/elsewhere"},
            b"",
        )

        with self.assertRaises(AppsScriptError) as captured:
            self.client().health_check()

        self.assertEqual("UNSAFE_REDIRECT", captured.exception.code)
        self.assertFalse(captured.exception.retryable)
        self.assertEqual(1, len(self.scenario.requests))

    def test_connect_and_read_timeouts_are_applied_separately(self) -> None:
        observed: dict[str, float] = {}

        class SocketProbe:
            def settimeout(self, value: float) -> None:
                observed["read"] = value

        class ResponseProbe:
            status = 200

            @staticmethod
            def read(_limit: int) -> bytes:
                return json.dumps(AppsScriptClientTests.valid_health()).encode("utf-8")

            @staticmethod
            def getheaders() -> list[tuple[str, str]]:
                return []

        class ConnectionProbe:
            def __init__(self, *_args: object, timeout: float, **_kwargs: object) -> None:
                observed["connect"] = timeout
                self.sock = SocketProbe()

            def request(self, *_args: object, **_kwargs: object) -> None:
                return

            @staticmethod
            def getresponse() -> ResponseProbe:
                return ResponseProbe()

            def close(self) -> None:
                return

        client = AppsScriptClient(
            self.url,
            self.secret,
            connect_timeout_seconds=1.25,
            read_timeout_seconds=4.5,
        )
        with patch(
            "conferencia.sync.client.http.client.HTTPConnection",
            ConnectionProbe,
        ):
            client.health_check()

        self.assertEqual(1.25, observed["connect"])
        self.assertEqual(4.5, observed["read"])


if __name__ == "__main__":
    unittest.main()
