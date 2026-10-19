"""Fluxo central de descoberta e importação dos relatórios WMS."""

from __future__ import annotations

import ctypes
import os
import re
import secrets
import sqlite3
import tempfile
from base64 import b64decode
from binascii import Error as Base64Error
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from conferencia.domain.box_codes import content_hash_caixa_estoque, normalize_caixa_estoque
from conferencia.domain.entities import CollaboratorContext
from conferencia.infrastructure.settings import AppSettings
from conferencia.readers.excel_reader import PalletFileImporter
from conferencia.repositories.pallet_repository import (
    ActiveConferenceError,
    ContentInProgressError,
    PalletRepository,
)
from conferencia.services.conference_service import (
    ConflictError,
    ConferenceService,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
)


REPORT_NAME = "relatorio_ConsultaPaleteDistribuicaoAgrupada"
REPORT_PATTERN = re.compile(rf"^{re.escape(REPORT_NAME)}(?: \([0-9]+\))?\.csv$", re.IGNORECASE)
REPORTS_TO_KEEP = 10
ORIGINS = frozenset({"PORTAL", "TL"})
OPERATIONS = frozenset({"DIGITAL", "NIKESTORE", "CROSS"})
SHIFTS = frozenset({"T1", "T2", "T3", "ADM"})


@dataclass(frozen=True, slots=True)
class _SelectedReport:
    path: Path
    downloads_directory: Path
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _ReportData:
    filename: str
    content: bytes
    downloads_directory: Path | None = None


class UploadService:
    """Owns automatic discovery, manual upload, validation and Downloads retention."""

    def __init__(
        self,
        conference_service: ConferenceService,
        file_importer: PalletFileImporter,
        settings: AppSettings | None = None,
        downloads_directories: tuple[Path, ...] | None = None,
    ) -> None:
        self.conference_service = conference_service
        self.repository: PalletRepository = conference_service.repository
        self.file_importer = file_importer
        self.settings = settings or AppSettings()
        self._downloads_directories = downloads_directories
        self._selected_reports: dict[str, _SelectedReport] = {}

    def localizar_downloads(self) -> tuple[Path, ...]:
        """Returns configured, redirected and default Downloads directories once each."""
        if self._downloads_directories is not None:
            return self._unique_directories(self._downloads_directories)

        directories: list[Path] = []
        configured = os.getenv("CONFERENCE_DOWNLOADS_DIRECTORY", "").strip()
        if configured:
            directories.append(Path(os.path.expandvars(configured)))
        if os.name == "nt":
            try:
                import winreg

                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
                ) as key:
                    value, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
                if isinstance(value, str) and value.strip():
                    directories.append(Path(os.path.expandvars(value)))
            except (FileNotFoundError, OSError):
                pass
        directories.append(Path.home() / "Downloads")
        return self._unique_directories(directories)

    @staticmethod
    def _unique_directories(directories: tuple[Path, ...] | list[Path]) -> tuple[Path, ...]:
        unique: dict[str, Path] = {}
        for directory in directories:
            try:
                resolved = directory.resolve()
            except OSError:
                continue
            unique[str(resolved).casefold()] = resolved
        return tuple(unique.values())

    def listar_relatorios(self) -> list[_SelectedReport]:
        """Lists each matching report once, newest first."""
        reports: dict[str, _SelectedReport] = {}
        for directory in self.localizar_downloads():
            if not directory.is_dir():
                continue
            try:
                for path in directory.iterdir():
                    if not path.is_file() or REPORT_PATTERN.fullmatch(path.name) is None:
                        continue
                    resolved = path.resolve()
                    stats = resolved.stat()
                    reports[str(resolved).casefold()] = _SelectedReport(
                        resolved, directory.resolve(), stats.st_size, stats.st_mtime_ns
                    )
            except OSError:
                continue
        return sorted(
            reports.values(),
            key=lambda report: (report.mtime_ns, report.path.name.casefold()),
            reverse=True,
        )

    def localizar_ultimo_relatorio(self) -> dict[str, object]:
        reports = self.listar_relatorios()
        if not reports:
            return {"found": False}
        report = reports[0]
        token = secrets.token_urlsafe(32)
        self._selected_reports[token] = report
        self._selected_reports = dict(list(self._selected_reports.items())[-100:])
        return {
            "found": True,
            "file_id": token,
            "filename": report.path.name,
            "downloaded_at": datetime.fromtimestamp(report.mtime_ns / 1_000_000_000).strftime("%d/%m/%Y %H:%M:%S"),
        }

    def obter_dados_arquivo(self, file_id: object) -> _ReportData:
        """Reads the immutable automatic selection exactly once after revalidation."""
        if not isinstance(file_id, str):
            raise ValidationError("O arquivo automático selecionado é inválido.", code="AUTOMATIC_FILE_INVALID")
        report = self._selected_reports.pop(file_id, None)
        if report is None:
            raise NotFoundError("O arquivo automático não está mais disponível. Procure novamente.", code="AUTOMATIC_FILE_NOT_FOUND")
        try:
            path = report.path.resolve(strict=True)
            stats = path.stat()
        except OSError as error:
            raise NotFoundError("O arquivo automático foi removido. Selecione-o manualmente.", code="AUTOMATIC_FILE_NOT_FOUND") from error
        if path.parent != report.downloads_directory or REPORT_PATTERN.fullmatch(path.name) is None:
            raise ValidationError("O arquivo automático não passou na validação de segurança.", code="AUTOMATIC_FILE_INVALID")
        if stats.st_size != report.size or stats.st_mtime_ns != report.mtime_ns:
            raise ValidationError("O arquivo automático foi alterado. Procure novamente ou selecione-o manualmente.", code="AUTOMATIC_FILE_CHANGED")
        if stats.st_size > self.settings.max_upload_bytes:
            raise PayloadTooLargeError("O arquivo automático excede o limite permitido.")
        try:
            return _ReportData(path.name, path.read_bytes(), report.downloads_directory)
        except OSError as error:
            raise NotFoundError("Não foi possível ler o arquivo automático. Selecione-o manualmente.", code="AUTOMATIC_FILE_NOT_FOUND") from error

    def importar_automaticamente(self, payload: dict[str, object], collaborator: CollaboratorContext) -> dict:
        report = self.obter_dados_arquivo(payload.get("automatic_file_id"))
        result = self._importar_arquivo(report.filename, report.content, payload, collaborator)
        self.limpar_downloads(report.downloads_directory, arquivo_em_uso=report.filename)
        return result

    def importar_manual(self, payload: dict[str, object], collaborator: CollaboratorContext) -> dict:
        filename = payload.get("filename")
        content_base64 = payload.get("content_base64")
        if not isinstance(filename, str) or not isinstance(content_base64, str):
            raise ValidationError("Informe o nome e o conteúdo do arquivo para carregar o palete.")
        return self._importar_arquivo(filename, self._decode_base64(content_base64), payload, collaborator)

    def _importar_arquivo(
        self,
        filename: str,
        content: bytes,
        payload: dict[str, object],
        collaborator: CollaboratorContext,
    ) -> dict:
        collaborator = self._validate_collaborator(collaborator)
        agenda = str(payload.get("agenda", "") or "").strip().upper()
        origin = self._choice(payload.get("origin"), ORIGINS, "origem")
        operation = self._choice(payload.get("operation"), OPERATIONS, "operação")
        extension = self._safe_extension(filename)
        if len(content) > self.settings.max_upload_bytes:
            raise PayloadTooLargeError("Arquivo excede o limite configurado de 10 MB.")
        expected_items = self._read_expected_items(content, extension)
        carton_codes = [normalize_caixa_estoque(item["caixa_estoque"]) for item in expected_items]
        carton_codes = [code for code in carton_codes if code]
        if not carton_codes:
            raise ValidationError("O arquivo não possui caixas estoque válidas.")
        fingerprint = getattr(self.file_importer, "fingerprint", content_hash_caixa_estoque)
        content_hash = fingerprint(carton_codes)
        return self.validar_conferencia(
            filename=filename,
            expected_items=expected_items,
            source_fingerprint=sha256(content + f"|{origin}|{operation}".encode()).hexdigest(),
            agenda=agenda,
            origin=origin,
            operation=operation,
            collaborator=collaborator,
            content_hash=content_hash,
            total_lines=len(carton_codes),
        )

    def _read_expected_items(self, content: bytes, extension: str) -> list[dict[str, str]]:
        self.settings.temporary_directory.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=extension, prefix="conference_",
                dir=self.settings.temporary_directory, delete=False,
            ) as temporary_file:
                temporary_file.write(content)
                temporary_path = Path(temporary_file.name)
            read_items = getattr(self.file_importer, "read_expected_items", None)
            return read_items(temporary_path, extension) if read_items else [
                {"caixa_estoque": code, "ds_classe": ""}
                for code in self.file_importer.read_carton_codes(temporary_path, extension)
            ]
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def validar_conferencia(
        self,
        *,
        filename: str,
        expected_items: list[dict[str, str]],
        source_fingerprint: str,
        agenda: str,
        origin: str,
        operation: str,
        collaborator: CollaboratorContext,
        content_hash: str,
        total_lines: int,
    ) -> dict:
        public_id = f"CONF-{uuid4().hex[:12].upper()}"
        try:
            decision = self.repository.create(
                public_id, collaborator, filename, expected_items, source_fingerprint,
                agenda, origin, operation, collaborator.shift, content_hash,
            )
        except ActiveConferenceError as error:
            raise ConflictError(
                "Já existe uma conferência ativa. Finalize ou cancele a conferência atual antes de iniciar uma nova importação.",
                code="CONFERENCIA_ATIVA", details={"public_id": error.public_id},
            ) from error
        except ContentInProgressError as error:
            owner = error.owner_name or "outro colaborador"
            registration = error.owner_registration or "não informada"
            timestamp = PalletRepository._format_local_datetime(error.started_at or error.imported_at)
            message = f"Este conteúdo já está sendo conferido por {owner}. Matrícula: {registration}."
            if timestamp:
                message += f" Iniciada/importada em: {timestamp}."
            raise ConflictError(
                message,
                code="CONTEUDO_EM_CONFERENCIA",
                details={
                    "owner_registration": registration,
                    "started_at": timestamp,
                },
            ) from error
        except sqlite3.IntegrityError as error:
            raise ConflictError(
                "Não foi possível criar a conferência sem duplicidades.",
                code="CONFERENCE_CREATION_CONFLICT",
            ) from error

        result = self.conference_service.get_pallet(str(decision["public_id"]))
        action = str(decision["action"])
        if action == "already_completed":
            result.update({
                "action": action,
                "already_completed": True,
                "message": "Este arquivo já foi conferido e finalizado. Não é permitido iniciar uma nova conferência com o mesmo conteúdo.",
                "can_authorize_reconference": collaborator.shift == "ADM",
            })
            return result
        if action == "resumed":
            owner = result["collaborator"]
            result.update({
                "action": action,
                "message": (
                    "Conferência já em andamento. Este palete já possui uma conferência aberta. "
                    f"Iniciado por: {owner['name']}. Matrícula: {owner['registration']}. "
                    f"Iniciado em: {result['importation']['imported_at']}. O progresso existente foi carregado."
                ),
            })
            return result
        result.update({
            "importacao_id": result["public_id"],
            "total_linhas": total_lines,
            "total_importadas": total_lines,
            "total_duplicadas": 0,
            "caixas": result["cartons"],
            "action": action,
            "content_hash": content_hash,
            "previous_public_id": decision.get("previous_public_id"),
            "message": "Nova conferência criada a partir de conteúdo anteriormente cancelado."
            if action == "created_after_cancellation" else "Nova conferência criada com sucesso.",
        })
        return result

    def limpar_downloads(
        self, downloads_directory: Path, *, arquivo_em_uso: str | Path | None = None
    ) -> None:
        """Keeps ten newest matching reports; cleanup failures never affect an import."""
        try:
            reports = sorted(
                (
                    path for path in downloads_directory.iterdir()
                    if path.is_file() and REPORT_PATTERN.fullmatch(path.name) is not None
                ),
                key=lambda path: (path.stat().st_mtime_ns, path.name.casefold()),
            )
        except OSError:
            return
        if len(reports) <= REPORTS_TO_KEEP:
            return

        protected_name = Path(arquivo_em_uso).name.casefold() if arquivo_em_uso else None
        remaining_to_remove = len(reports) - REPORTS_TO_KEEP
        for path in reports:
            if remaining_to_remove == 0:
                return
            if protected_name is not None and path.name.casefold() == protected_name:
                continue
            try:
                if self._arquivo_em_uso(path):
                    continue
                path.unlink()
            except OSError:
                continue
            remaining_to_remove -= 1

    @staticmethod
    def _arquivo_em_uso(path: Path) -> bool:
        if os.name != "nt":
            try:
                import fcntl

                with path.open("rb") as file:
                    fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(file.fileno(), fcntl.LOCK_UN)
            except (BlockingIOError, OSError):
                return True
            return False

        generic_read_write = 0x80000000 | 0x40000000
        invalid_handle_value = ctypes.c_void_p(-1).value
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        )
        create_file.restype = ctypes.c_void_p
        handle = create_file(str(path), generic_read_write, 0, None, 3, 0x80, None)
        if handle == invalid_handle_value:
            return True
        kernel32.CloseHandle(handle)
        return False

    @staticmethod
    def _validate_collaborator(collaborator: CollaboratorContext | None) -> CollaboratorContext:
        if (
            collaborator is None or collaborator.id is None or not collaborator.registration
            or not collaborator.name or collaborator.shift not in SHIFTS
        ):
            raise ValidationError(
                "Identifique o colaborador antes de realizar esta operação.",
                code="COLABORADOR_NAO_IDENTIFICADO",
            )
        return collaborator

    @staticmethod
    def _choice(value: object, allowed: frozenset[str], label: str) -> str:
        normalized = value.strip().upper() if isinstance(value, str) else ""
        if normalized not in allowed:
            raise ValidationError(f"Selecione uma {label} válida.")
        return normalized

    @staticmethod
    def _safe_extension(filename: str) -> str:
        if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
            raise ValidationError("Nome de arquivo inválido.")
        extension = Path(filename).suffix.lower()
        if extension not in {".xlsx", ".csv"}:
            raise ValidationError("Envie somente arquivos .xlsx ou .csv; .xls não é aceito.")
        return extension

    def _decode_base64(self, value: str) -> bytes:
        if not value:
            raise ValidationError("Conteúdo do arquivo é obrigatório.")
        encoded = value.split(",", 1)[-1]
        if len(encoded) > ((self.settings.max_upload_bytes + 2) // 3) * 4:
            raise PayloadTooLargeError("Arquivo excede o limite configurado de 10 MB.")
        try:
            content = b64decode(encoded, validate=True)
        except Base64Error as error:
            raise ValidationError("Conteúdo Base64 do arquivo é inválido.") from error
        if len(content) > self.settings.max_upload_bytes:
            raise PayloadTooLargeError("Arquivo excede o limite configurado de 10 MB.")
        return content
