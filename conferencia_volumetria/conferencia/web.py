from __future__ import annotations

import json
import logging
import mimetypes
import re
from html import escape
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

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
from conferencia.services.google_sheets_sync_service import GoogleSheetsSyncService
from conferencia.services.automatic_report_service import AutomaticReportService

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = PROJECT_ROOT / "static"


class InvalidJsonError(Exception):
    pass


class ApplicationHandler(BaseHTTPRequestHandler):
    controller: ConferenceController
    acesso_service: AcessoService
    sessions: SessionManager
    max_json_bytes = 15 * 1024 * 1024
    google_sync_service: GoogleSheetsSyncService | None = None
    automatic_report_service: AutomaticReportService | None = None

    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        path = unquote(parsed_url.path)
        if path == "/":
            return self._serve_file(PROJECT_ROOT / "access.html")
        if path == "/conference":
            if self._session() is None:
                return self._redirect("/")
            return self._serve_file(PROJECT_ROOT / "index.html")
        if path.rstrip("/") == "/sincronizacao/confirmar":
            return self._confirm_google_sync(parse_qs(parsed_url.query))
        if path == "/api/sincronizacao/pendentes":
            if self._session() is None:
                return self._collaborator_not_identified()
            if self.google_sync_service is None:
                return self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "SINCRONIZACAO_NAO_CONFIGURADA", "A sincronização não está configurada.")
            return self._handle(self.google_sync_service.pending_summary)
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
            if path == "/api/conferences/latest-wms-report":
                if self.automatic_report_service is None:
                    return self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "AUTOMATIC_REPORT_UNAVAILABLE", "A busca automática não está configurada.")
                return self._handle(self.automatic_report_service.latest)
            route = self._conference_route(path)
            if route is None:
                return self._not_found()
            public_id, action = route
            if action == "boxes":
                return self._handle(
                    lambda: self.controller.get_boxes(
                        public_id, self._collaborator(self._session())
                    )
                )
            if action is None:
                return self._handle(
                    lambda: self.controller.get_pallet(
                        public_id, self._collaborator(self._session())
                    )
                )
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
        if path.startswith("/sincronizacao/"):
            if path.rstrip("/") == "/sincronizacao/pendentes/preparar":
                return self._prepare_pending_google_sync()
            if path.rstrip("/").endswith("/preparar"):
                return self._prepare_google_sync(path)
            if path.rstrip("/").endswith("/confirmar"):
                return self._confirm_google_sync_json(path)
            if path.rstrip("/").endswith("/erro"):
                return self._mark_google_sync_error(path)
            return self._not_found()
        if path != "/api/conferences" and not path.startswith("/api/conferences/"):
            return self._not_found()
        session = self._session()
        if session is None:
            return self._collaborator_not_identified()
        collaborator = self._collaborator(session)
        try:
            payload = self._json_body()
        except PayloadTooLargeError as error:
            return self._json_application_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, error
            )
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
        if path == "/api/conferences/import-automatic":
            if self.automatic_report_service is None:
                return self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "AUTOMATIC_REPORT_UNAVAILABLE", "A busca automática não está configurada.")
            return self._handle(
                lambda: self.controller.import_automatic_pallet(payload, collaborator, self.automatic_report_service),
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
            "reconference": lambda: self.controller.authorize_reconference(
                public_id, payload, collaborator
            ),
            "cancel": lambda: self.controller.cancel_pallet(public_id, collaborator),
        }
        operation = operations.get(action or "")
        if operation is None:
            return self._not_found()
        return self._handle(operation)

    def _prepare_pending_google_sync(self) -> None:
        session = self._session()
        if session is None:
            return self._collaborator_not_identified()
        if self.google_sync_service is None:
            return self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "SINCRONIZACAO_NAO_CONFIGURADA", "A sincronização não está configurada.")
        return_url = f"{self._request_origin()}/sincronizacao/confirmar/"
        return self._handle(
            lambda: self.google_sync_service.prepare_next_pending(
                self._collaborator(session), return_url
            )
        )

    def _prepare_google_sync(self, path: str) -> None:
        session = self._session()
        if session is None:
            return self._collaborator_not_identified()
        parts = [part for part in path.split("/") if part]
        if len(parts) != 3 or parts[0] != "sincronizacao" or parts[2] != "preparar":
            return self._not_found()
        if self.google_sync_service is None:
            return self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "SINCRONIZACAO_NAO_CONFIGURADA", "A sincronização não está configurada.")
        public_id = parts[1]
        return_url = f"{self._request_origin()}/sincronizacao/confirmar/"
        return self._handle(
            lambda: self.google_sync_service.prepare(
                public_id, self._collaborator(session), return_url
            )
        )

    def _confirm_google_sync_json(self, path: str) -> None:
        session = self._session()
        if session is None:
            return self._collaborator_not_identified()
        public_id = self._sync_public_id(path, "confirmar")
        if public_id is None:
            return self._not_found()
        if self.google_sync_service is None:
            return self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "SINCRONIZACAO_NAO_CONFIGURADA", "A sincronização não está configurada.")
        payload = self._sync_json_body()
        if payload is None:
            return

        def confirm() -> dict[str, object]:
            receipt_public_id = str(payload.get("conference_id") or "")
            if receipt_public_id != public_id:
                self.google_sync_service.fail(
                    public_id,
                    "GOOGLE_RECEIPT_INVALID",
                    str(payload.get("attempt_id") or ""), str(payload.get("nonce") or ""),
                    self._collaborator(session),
                )
                raise ValidationError(
                    "O recibo não pertence à conferência atual.",
                    code="RECIBO_DE_OUTRA_CONFERENCIA",
                )
            try:
                result = self.google_sync_service.confirm(
                    public_id,
                    str(payload.get("status") or ""),
                    str(payload.get("sincronizado_em") or ""),
                    str(payload.get("assinatura_recibo") or ""),
                    str(payload.get("attempt_id") or ""), str(payload.get("nonce") or ""),
                    bool(payload.get("ja_sincronizado")),
                    self._collaborator(session),
                )
            except (ValidationError, ConflictError):
                self.google_sync_service.fail(
                    public_id,
                    "GOOGLE_RECEIPT_INVALID",
                    str(payload.get("attempt_id") or ""), str(payload.get("nonce") or ""),
                    self._collaborator(session),
                )
                raise
            return {**result, "status": "SINCRONIZADO"}

        return self._handle(confirm)

    def _mark_google_sync_error(self, path: str) -> None:
        session = self._session()
        if session is None:
            return self._collaborator_not_identified()
        public_id = self._sync_public_id(path, "erro")
        if public_id is None:
            return self._not_found()
        if self.google_sync_service is None:
            return self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "SINCRONIZACAO_NAO_CONFIGURADA", "A sincronização não está configurada.")
        payload = self._sync_json_body()
        if payload is None:
            return
        return self._handle(
            lambda: self.google_sync_service.fail(
                public_id,
                str(payload.get("code") or ""),
                str(payload.get("attempt_id") or ""), str(payload.get("nonce") or ""),
                self._collaborator(session),
            )
        )

    def _sync_json_body(self) -> dict[str, object] | None:
        try:
            return self._json_body()
        except PayloadTooLargeError as error:
            self._json_application_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, error)
        except InvalidJsonError as error:
            self._json_error(HTTPStatus.BAD_REQUEST, "INVALID_JSON", str(error))
        return None

    @staticmethod
    def _sync_public_id(path: str, expected_action: str) -> str | None:
        parts = [part for part in path.split("/") if part]
        if len(parts) != 3 or parts[0] != "sincronizacao" or parts[2] != expected_action:
            return None
        return parts[1] or None

    def _confirm_google_sync(self, query: dict[str, list[str]]) -> None:
        session = self._session()
        if session is None:
            return self._collaborator_not_identified()
        if self.google_sync_service is None:
            return self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "SINCRONIZACAO_NAO_CONFIGURADA", "A sincronização não está configurada.")
        value = lambda name: query.get(name, [""])[0]
        try:
            nonce = value("nonce")
            attempt_id = value("attempt_id")
            conference_id = value("conference_id")
            status = value("status")
            error_code = value("error_code")
            if re.fullmatch(r"[A-Za-z0-9_-]{20,200}", nonce) is None:
                raise ValidationError(
                    "A identificação da tentativa de sincronização é inválida.",
                    code="RECIBO_INVALIDO",
                )
            if status == "ERROR":
                result = self.google_sync_service.fail(
                    conference_id, error_code or "GOOGLE_SYNC_FAILED", attempt_id, nonce,
                    self._collaborator(session),
                )
                return self._write_html(
                    HTTPStatus.OK,
                    self._sync_result_html(conference_id, attempt_id, nonce, "ERROR", result["message"], error_code),
                )
            if status not in ("SUCCESS", "ALREADY_SYNCED"):
                raise ValidationError("O status do resultado é inválido.", code="RECIBO_INVALIDO")
            result = self.google_sync_service.confirm(
                conference_id, "SINCRONIZADO", value("sincronizado_em"),
                value("assinatura_recibo"), attempt_id, nonce,
                status == "ALREADY_SYNCED" or value("ja_sincronizado") == "true", self._collaborator(session),
            )
            already_synced = status == "ALREADY_SYNCED" or value("ja_sincronizado") == "true"
            self._write_html(
                HTTPStatus.OK,
                self._sync_result_html(
                    str(result["public_id"]), attempt_id, nonce,
                    "ALREADY_SYNCED" if already_synced else "SUCCESS",
                    "A conferência já estava sincronizada no Google Sheets." if already_synced
                    else "Sincronização concluída com sucesso.",
                    "", str(result["synchronized_at"]),
                ),
            )
        except NotFoundError as error:
            self._write_error_html(HTTPStatus.NOT_FOUND, str(error))
        except (ValidationError, ConflictError) as error:
            self._write_error_html(HTTPStatus.UNPROCESSABLE_ENTITY, str(error))

    @staticmethod
    def _sync_result_html(
        public_id: str, attempt_id: str, nonce: str, status: str, message: str,
        error_code: str = "", synchronized_at: str = "",
    ) -> str:
        result = json.dumps(
            {
                "source": "google-sheets-sync",
                "type": "GOOGLE_SYNC_RESULT",
                "conference_id": public_id,
                "attempt_id": attempt_id,
                "status": status,
                "error_code": error_code,
                "message": message,
                "sincronizado_em": synchronized_at,
                "nonce": nonce,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).replace("<", "\\u003c")
        return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Resultado da sincronização</title></head><body>
<main><h1>Resultado da sincronização</h1><p>Esta janela será fechada automaticamente.</p>
<p id="close-warning" hidden>A sincronização foi concluída. Você já pode fechar esta janela.</p></main>
<script>(function(){{"use strict";
const resultado={result};
function notifyAndClose(){{
  const targetOrigin=window.location.origin;
  if(window.opener&&!window.opener.closed){{window.opener.postMessage(resultado,targetOrigin);}}
  setTimeout(function(){{
    if(window.opener&&!window.opener.closed){{window.opener.postMessage(resultado,targetOrigin);}}
  }},120);
  setTimeout(function(){{window.close();}},550);
}}
notifyAndClose();
}})();</script></body></html>"""

    def _request_origin(self) -> str:
        host = self.headers.get("Host", "127.0.0.1:8080")
        if not host or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:[]" for character in host):
            host = "127.0.0.1:8080"
        forwarded_proto = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
        protocol = forwarded_proto if forwarded_proto in ("http", "https") else "http"
        return f"{protocol}://{host}"

    def _write_error_html(self, status: HTTPStatus, message: str) -> None:
        self._write_html(status, f"<!doctype html><meta charset='utf-8'><h1>Não foi possível sincronizar</h1><p>{escape(message)}</p><a href='/conference'>Voltar ao sistema</a>")

    def _write_html(self, status: HTTPStatus, html: str) -> None:
        content = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

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
            if content_length < 0 or content_length > self.max_json_bytes:
                raise PayloadTooLargeError("A requisição excede o limite permitido.")
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
        except PayloadTooLargeError as error:
            self._json_application_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, error)
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
        except PayloadTooLargeError as error:
            self._json_application_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, error)
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
        except PayloadTooLargeError as error:
            self._json_application_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, error)
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
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")
