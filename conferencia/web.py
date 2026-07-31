from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlparse

from conferencia.database import Database
from conferencia.services import ConferenceService, ConflictError, NotFoundError, ValidationError


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = PROJECT_ROOT / "static"


class ApplicationHandler(BaseHTTPRequestHandler):
    database: Database

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/":
            return self._serve_file(PROJECT_ROOT / "index.html")
        if path == "/api/health":
            return self._json(HTTPStatus.OK, {"status": "ok"})
        if path == "/api/pallets":
            return self._handle(lambda service: service.list_pallets())
        if path.startswith("/api/pallets/"):
            return self._handle(lambda service: service.get_pallet(path.removeprefix("/api/pallets/")))
        if path.startswith("/static/"):
            relative = path.removeprefix("/static/")
            target = (STATIC_ROOT / relative).resolve()
            if STATIC_ROOT.resolve() in target.parents:
                return self._serve_file(target)
        self._json(HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        try:
            payload = self._body_json()
        except ValidationError as exc:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        if path == "/api/pallets":
            return self._handle(lambda service: service.create_pallet(payload), HTTPStatus.CREATED)
        if path.startswith("/api/pallets/") and path.endswith("/cartons"):
            code = path.removeprefix("/api/pallets/").removesuffix("/cartons").rstrip("/")
            return self._handle(lambda service: service.scan_carton(code, payload))
        if path.startswith("/api/pallets/") and path.endswith("/finish"):
            code = path.removeprefix("/api/pallets/").removesuffix("/finish").rstrip("/")
            return self._handle(lambda service: service.finish_pallet(code, payload))
        self._json(HTTPStatus.NOT_FOUND, {"error": "Rota não encontrada."})

    def _handle(self, operation, status=HTTPStatus.OK) -> None:
        try:
            result = operation(ConferenceService(self.database))
            self._json(status, result)
        except ValidationError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except NotFoundError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except ConflictError as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})

    def _body_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            raise ValidationError("O corpo da requisição deve ser um JSON válido.")
        if not isinstance(data, dict):
            raise ValidationError("O corpo da requisição deve ser um objeto JSON.")
        return data

    def _json(self, status: HTTPStatus, payload: object) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_file(self, file_path: Path) -> None:
        if not file_path.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Arquivo não encontrado."})
        content = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")
