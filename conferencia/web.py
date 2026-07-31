from __future__ import annotations

import json
import logging
import mimetypes
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlparse

from conferencia.controllers.conference_controller import ConferenceController
from conferencia.domain.entities import CollaboratorContext
from conferencia.infrastructure.session_manager import (
    SESSION_COLLABORATOR_ID,
    SESSION_COLLABORATOR_NAME,
    SESSION_COLLABORATOR_REGISTRATION,
    SESSION_COLLABORATOR_SHIFT,
    SessionData,
    SessionManager,
)
from conferencia.readers.excel_reader import ExcelReadError
from conferencia.services.acesso_service import AcessoService
from conferencia.services.conference_service import (
    ApplicationError,
    ConflictError,
    InvalidScanError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = PROJECT_ROOT / "static"


class InvalidJsonError(Exception):
    pass


class ApplicationHandler(BaseHTTPRequestHandler):
    controller: ConferenceController
    acesso_service: AcessoService
    sessions: SessionManager

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/":
            return self._serve_file(PROJECT_ROOT / "access.html")
        if path == "/conference":
            if self._session() is None:
                return self._redirect("/")
            return self._serve_file(PROJECT_ROOT / "index.html")
        if path.startswith("/api/colaboradores/"):
            registration = path.removeprefix("/api/colaboradores/").strip("/")
            if not registration or "/" in registration:
                return self._not_found()
            return self._handle(lambda: self.acesso_service.collaborator(registration))
        if path.startswith("/api/conferences/"):
            if self._session() is None:
                return self._collaborator_not_identified()
            if path == "/api/conferences/active":
                return self._handle(
                    lambda: self.controller.get_active_pallet(
                        self._collaborator(self._session())
                    )
                )
            route = self._conference_route(path)
            if route is None:
                return self._not_found()
            public_id, action = route
            if action == "boxes":
                return self._handle(lambda: self.controller.get_boxes(public_id))
            if action is None:
                return self._handle(lambda: self.controller.get_pallet(public_id))
            return self._not_found()
        if path.startswith("/static/"):
            target = (STATIC_ROOT / path.removeprefix("/static/")).resolve()
            if target == STATIC_ROOT.resolve() or STATIC_ROOT.resolve() not in target.parents:
                return self._not_found()
            return self._serve_file(target)
        return self._not_found()

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/api/access":
            return self._access()
        if path == "/api/colaboradores/cadastro-rapido":
            return self._quick_register()
        if path.startswith("/api/colaboradores/"):
            return self._update_collaborator(path)
        if path == "/api/logout":
            return self._logout()
        if path != "/api/conferences" and not path.startswith("/api/conferences/"):
            return self._not_found()
        session = self._session()
        if session is None:
            return self._collaborator_not_identified()
        collaborator = self._collaborator(session)
        try:
            payload = self._json_body()
        except InvalidJsonError as error:
            return self._json_error(
                HTTPStatus.BAD_REQUEST,
                "INVALID_JSON",
                str(error),
            )
        if path == "/api/conferences":
            return self._handle(
                lambda: self.controller.import_pallet(payload, collaborator),
                HTTPStatus.CREATED,
            )
        route = self._conference_route(path)
        if route is None:
            return self._not_found()
        public_id, action = route
        operations: dict[str, Callable[[], object]] = {
            "start": lambda: self.controller.start_pallet(public_id, collaborator),
            "scan": lambda: self.controller.scan_carton(
                public_id, payload, collaborator
            ),
            "finish": lambda: self.controller.finish_pallet(
                public_id, collaborator
            ),
            "restart": lambda: self.controller.restart_pallet(
                public_id, collaborator
            ),
            "sync": lambda: self.controller.sync_pallet(public_id, collaborator),
            "cancel": lambda: self.controller.cancel_pallet(public_id, collaborator),
        }
        operation = operations.get(action or "")
        if operation is None:
            return self._not_found()
        return self._handle(operation)

    def _handle(
        self,
        operation: Callable[[], object],
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        try:
            result = operation()
            message = (
                result.get("message", "Operação realizada com sucesso.")
                if isinstance(result, dict)
                else "Operação realizada com sucesso."
            )
            self._json_success(status, result, str(message))
        except PayloadTooLargeError as error:
            self._json_application_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, error)
        except InvalidScanError as error:
            self._json_application_error(HTTPStatus.UNPROCESSABLE_ENTITY, error)
        except (ValidationError, ExcelReadError) as error:
            if isinstance(error, ApplicationError):
                self._json_application_error(HTTPStatus.UNPROCESSABLE_ENTITY, error)
            else:
                self._json_error(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    "VALIDATION_ERROR",
                    str(error),
                )
        except NotFoundError as error:
            self._json_application_error(HTTPStatus.NOT_FOUND, error)
        except ConflictError as error:
            self._json_application_error(HTTPStatus.CONFLICT, error)
        except Exception:
            logging.exception("Erro inesperado na API")
            self._json_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "INTERNAL_ERROR",
                "Erro interno do servidor.",
            )

    def _json_body(self) -> dict[str, object]:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(content_length) if content_length else b"{}"
            payload = json.loads(raw)
        except (ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise InvalidJsonError("O corpo precisa ser um JSON válido.") from error
        if not isinstance(payload, dict):
            raise InvalidJsonError("O corpo precisa ser um objeto JSON.")
        return payload

    def _access(self) -> None:
        try:
            payload = self._json_body()
            collaborator = self.acesso_service.authorize(payload.get("matricula"))
            token = self.sessions.create(collaborator)
            self._json_success(
                HTTPStatus.OK,
                {"colaborador": collaborator, "redirect_url": "/conference"},
                "Colaborador identificado.",
                cookie=token,
            )
        except InvalidJsonError as error:
            self._json_error(HTTPStatus.BAD_REQUEST, "INVALID_JSON", str(error))
        except ValidationError as error:
            self._json_application_error(HTTPStatus.UNPROCESSABLE_ENTITY, error)

    def _logout(self) -> None:
        self.sessions.destroy(self._cookie())
        self._json_success(
            HTTPStatus.OK,
            {"redirect_url": "/"},
            "Identificação encerrada.",
            cookie="",
        )

    def _quick_register(self) -> None:
        try:
            payload = self._json_body()
            collaborator = self.acesso_service.quick_register(
                payload.get("matricula"), payload.get("nome")
                , payload.get("turno")
            )
            self._json_success(
                HTTPStatus.CREATED,
                {"colaborador": collaborator},
                "Colaborador cadastrado com sucesso.",
            )
        except InvalidJsonError as error:
            self._json_error(HTTPStatus.BAD_REQUEST, "INVALID_JSON", str(error))
        except ValidationError as error:
            self._json_application_error(HTTPStatus.UNPROCESSABLE_ENTITY, error)
        except ConflictError as error:
            self._json_application_error(HTTPStatus.CONFLICT, error)
        except Exception:
            logging.exception("Erro inesperado no cadastro rápido")
            self._json_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "INTERNAL_ERROR",
                "Erro interno do servidor.",
            )

    def _update_collaborator(self, path: str) -> None:
        registration = path.removeprefix("/api/colaboradores/").strip("/")
        if not registration or "/" in registration:
            return self._not_found()
        try:
            payload = self._json_body()
            collaborator = self.acesso_service.update_collaborator(
                registration, payload.get("nome"), payload.get("turno")
            )
            self._json_success(
                HTTPStatus.OK,
                {"colaborador": collaborator},
                "Colaborador atualizado com sucesso.",
            )
        except InvalidJsonError as error:
            self._json_error(HTTPStatus.BAD_REQUEST, "INVALID_JSON", str(error))
        except ValidationError as error:
            self._json_application_error(HTTPStatus.UNPROCESSABLE_ENTITY, error)

    def _cookie(self) -> str | None:
        for value in self.headers.get("Cookie", "").split(";"):
            name, separator, content = value.strip().partition("=")
            if separator and name == "cv_session":
                return content
        return None

    def _session(self) -> SessionData | None:
        return self.sessions.get(self._cookie())

    @staticmethod
    def _collaborator(session: SessionData) -> CollaboratorContext:
        return CollaboratorContext(
            id=session[SESSION_COLLABORATOR_ID],
            registration=session[SESSION_COLLABORATOR_REGISTRATION],
            name=session[SESSION_COLLABORATOR_NAME],
            shift=session[SESSION_COLLABORATOR_SHIFT],
        )

    @staticmethod
    def _conference_route(path: str) -> tuple[str, str | None] | None:
        parts = [part for part in path.split("/") if part]
        if len(parts) not in (3, 4) or parts[:2] != ["api", "conferences"]:
            return None
        public_id = parts[2].strip()
        if not public_id:
            return None
        return public_id, parts[3] if len(parts) == 4 else None

    def _collaborator_not_identified(self) -> None:
        self._json_error(
            HTTPStatus.UNAUTHORIZED,
            "COLABORADOR_NAO_IDENTIFICADO",
            "Identifique o colaborador antes de realizar o upload.",
            {"redirect_url": "/"},
        )

    def _not_found(self) -> None:
        self._json_error(
            HTTPStatus.NOT_FOUND,
            "NOT_FOUND",
            "Rota não encontrada.",
        )

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json_application_error(
        self, status: HTTPStatus, error: ApplicationError
    ) -> None:
        self._json_error(status, error.code, str(error), error.details)

    def _json_success(
        self,
        status: HTTPStatus,
        data: object,
        message: str,
        *,
        cookie: str | None = None,
    ) -> None:
        self._write_json(
            status,
            {"success": True, "data": data, "message": message},
            cookie=cookie,
        )

    def _json_error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        self._write_json(
            status,
            {
                "success": False,
                "error": {
                    "code": code,
                    "message": message,
                    "details": details or {},
                },
            },
        )

    def _write_json(
        self,
        status: HTTPStatus,
        payload: object,
        *,
        cookie: str | None = None,
    ) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            max_age = 0 if cookie == "" else self.sessions.timeout_seconds
            self.send_header(
                "Set-Cookie",
                f"cv_session={cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}",
            )
        self.end_headers()
        self.wfile.write(content)

    def _serve_file(self, file_path: Path) -> None:
        if not file_path.is_file():
            return self._not_found()
        content = file_path.read_bytes()
        content_type = (
            mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        )
        if content_type.startswith("text/") or content_type in (
            "application/javascript",
            "application/json",
        ):
            content_type = f"{content_type}; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")
