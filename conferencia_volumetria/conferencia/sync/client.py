from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Mapping
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit


class AppsScriptError(RuntimeError):
    """Safe HTTP/protocol failure that never includes credentials or payloads."""

    def __init__(self, message: str, *, retryable: bool, code: str = "REMOTE_ERROR") -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class AppsScriptClient:
    MAX_REDIRECTS = 3
    MAX_RESPONSE_BYTES = 1024 * 1024

    def __init__(
        self,
        url: str,
        shared_secret: str,
        *,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 30.0,
    ) -> None:
        self.url = str(url).strip()
        self._shared_secret = str(shared_secret).strip()
        self.connect_timeout_seconds = max(0.1, float(connect_timeout_seconds))
        self.read_timeout_seconds = max(0.1, float(read_timeout_seconds))

    @property
    def configured(self) -> bool:
        if not self.url or not self._shared_secret:
            return False
        try:
            parts = urlsplit(self.url)
            _ = parts.port
        except ValueError:
            return False
        if not parts.hostname or parts.username or parts.password:
            return False
        if parts.scheme.lower() == "https":
            return True
        return parts.scheme.lower() == "http" and self._is_loopback(parts.hostname)

    def health_check(self) -> dict[str, object]:
        response = self._post({"action": "health"})
        if (
            response.get("ok") is not True
            or response.get("service") != "apps-script"
            or response.get("sheets") is not True
        ):
            self._raise_remote_or_invalid(response, "Health check remoto inválido.")
        return response

    def send_batch(self, conferences: list[dict[str, object]]) -> list[dict[str, object]]:
        if not conferences:
            return []
        response = self._post({"action": "sync", "conferences": conferences})
        if response.get("ok") is not True:
            self._raise_remote_or_invalid(response, "Sincronização remota recusada.")
        results = response.get("results")
        if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
            raise AppsScriptError(
                "Resposta remota sem resultados válidos.",
                retryable=True,
                code="INVALID_RESPONSE",
            )
        return results

    def _post(self, payload: Mapping[str, object]) -> dict[str, object]:
        if not self.configured:
            raise AppsScriptError(
                "Sincronização Google não configurada.",
                retryable=False,
                code="NOT_CONFIGURED",
            )
        envelope = {**payload, "shared_secret": self._shared_secret}
        body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._request_json(self.url, "POST", body)

    def _request_json(self, url: str, method: str, body: bytes | None) -> dict[str, object]:
        current_url = url
        current_method = method
        current_body = body
        original_scheme = urlsplit(url).scheme.lower()

        for redirect_count in range(self.MAX_REDIRECTS + 1):
            status, headers, raw = self._single_request(current_url, current_method, current_body)
            if status in (301, 302, 303, 307, 308):
                if redirect_count >= self.MAX_REDIRECTS:
                    raise AppsScriptError(
                        "Muitos redirecionamentos no serviço remoto.",
                        retryable=True,
                        code="TOO_MANY_REDIRECTS",
                    )
                location = headers.get("location", "").strip()
                if not location:
                    raise AppsScriptError(
                        "Redirecionamento remoto inválido.",
                        retryable=True,
                        code="INVALID_REDIRECT",
                    )
                target = urljoin(current_url, location)
                target_parts = urlsplit(target)
                if target_parts.scheme.lower() not in ("http", "https"):
                    raise AppsScriptError(
                        "Destino remoto inválido.", retryable=False, code="INVALID_REDIRECT"
                    )
                if original_scheme == "https" and target_parts.scheme.lower() != "https":
                    raise AppsScriptError(
                        "Redirecionamento remoto inseguro.",
                        retryable=False,
                        code="INSECURE_REDIRECT",
                    )
                if status in (301, 302, 303):
                    current_method = "GET"
                    current_body = None
                elif current_body is not None:
                    try:
                        target_origin = self._origin(target_parts)
                        current_origin = self._origin(urlsplit(current_url))
                    except ValueError as error:
                        raise AppsScriptError(
                            "Redirecionamento remoto inválido.",
                            retryable=False,
                            code="INVALID_REDIRECT",
                        ) from error
                    if target_origin != current_origin:
                        raise AppsScriptError(
                            "Redirecionamento remoto não autorizado.",
                            retryable=False,
                            code="UNSAFE_REDIRECT",
                        )
                current_url = target
                continue

            if status in (408, 425, 429) or status >= 500:
                raise AppsScriptError(
                    "Serviço remoto temporariamente indisponível.",
                    retryable=True,
                    code=f"HTTP_{status}",
                )
            if status < 200 or status >= 300:
                raise AppsScriptError(
                    "Serviço remoto recusou a requisição.",
                    retryable=False,
                    code=f"HTTP_{status}",
                )
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as error:
                raise AppsScriptError(
                    "Resposta remota não é um JSON válido.",
                    retryable=True,
                    code="INVALID_JSON",
                ) from error
            if not isinstance(decoded, dict):
                raise AppsScriptError(
                    "Resposta remota não é um objeto JSON.",
                    retryable=True,
                    code="INVALID_RESPONSE",
                )
            return decoded
        raise AssertionError("redirect loop exhausted")

    def _single_request(
        self, url: str, method: str, body: bytes | None
    ) -> tuple[int, dict[str, str], bytes]:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        if scheme not in ("http", "https") or not host or parts.username or parts.password:
            raise AppsScriptError(
                "URL do serviço remoto inválida.", retryable=False, code="INVALID_URL"
            )
        if scheme == "http" and not self._is_loopback(host):
            raise AppsScriptError(
                "A URL remota precisa utilizar HTTPS.",
                retryable=False,
                code="INSECURE_URL",
            )
        path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
        connection_type = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        try:
            port = parts.port
        except ValueError as error:
            raise AppsScriptError(
                "URL do serviço remoto inválida.", retryable=False, code="INVALID_URL"
            ) from error
        connection = connection_type(host, port=port, timeout=self.connect_timeout_seconds)
        headers = {
            "Accept": "application/json",
            "User-Agent": "Portal-Pedagio-Sync/1.0",
        }
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            headers["Content-Length"] = str(len(body))
        try:
            connection.request(method, path, body=body, headers=headers)
            if connection.sock is not None:
                connection.sock.settimeout(self.read_timeout_seconds)
            response = connection.getresponse()
            raw = response.read(self.MAX_RESPONSE_BYTES + 1)
            if len(raw) > self.MAX_RESPONSE_BYTES:
                raise AppsScriptError(
                    "Resposta remota excedeu o limite permitido.",
                    retryable=False,
                    code="RESPONSE_TOO_LARGE",
                )
            return response.status, {key.lower(): value for key, value in response.getheaders()}, raw
        except AppsScriptError:
            raise
        except (TimeoutError, socket.timeout) as error:
            raise AppsScriptError(
                "Tempo limite excedido no serviço remoto.",
                retryable=True,
                code="TIMEOUT",
            ) from error
        except (OSError, http.client.HTTPException) as error:
            raise AppsScriptError(
                "Não foi possível conectar ao serviço remoto.",
                retryable=True,
                code="CONNECTION_ERROR",
            ) from error
        finally:
            connection.close()

    @staticmethod
    def _raise_remote_or_invalid(response: Mapping[str, object], fallback: str) -> None:
        error = response.get("error")
        code = "INVALID_RESPONSE"
        if isinstance(error, dict):
            raw_code = error.get("code")
            if isinstance(raw_code, str) and raw_code:
                code = raw_code.upper()
        elif isinstance(response.get("error_code"), str):
            code = str(response["error_code"]).upper()
        non_retryable = code in {
            "UNAUTHORIZED",
            "INVALID_SECRET",
            "NOT_CONFIGURED",
            "INVALID_ACTION",
            "INVALID_REQUEST",
            "INVALID_JSON",
            "INVALID_PAYLOAD",
            "INVALID_CONFERENCE_ID",
            "DUPLICATE_CONFERENCE",
            "BATCH_TOO_LARGE",
            "SHEET_SCHEMA_MISMATCH",
        }
        raise AppsScriptError(fallback, retryable=not non_retryable, code=code)

    @staticmethod
    def _is_loopback(host: str) -> bool:
        return host.strip("[]").lower() in {"127.0.0.1", "localhost", "::1"}

    @staticmethod
    def _origin(parts: SplitResult) -> tuple[str, str, int | None]:
        scheme = str(parts.scheme).lower()
        port = parts.port
        if port is None:
            port = 443 if scheme == "https" else 80 if scheme == "http" else None
        return scheme, str(parts.hostname or "").lower(), port
