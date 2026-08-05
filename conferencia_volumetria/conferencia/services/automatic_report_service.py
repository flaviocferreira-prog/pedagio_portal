from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from conferencia.services.conference_service import NotFoundError, PayloadTooLargeError, ValidationError


REPORT_NAME = "relatorio_ConsultaPaleteDistribuicaoAgrupada"
REPORT_PATTERN = re.compile(rf"^{re.escape(REPORT_NAME)}(?: \([0-9]+\))?\.csv$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _AutomaticReport:
    path: Path
    downloads_directory: Path
    size: int
    mtime_ns: int


class AutomaticReportService:
    """Finds the latest WMS report without exposing a local path to the browser."""

    def __init__(self, max_upload_bytes: int, downloads_directories: tuple[Path, ...] | None = None) -> None:
        self.max_upload_bytes = max_upload_bytes
        self._downloads_directories = downloads_directories
        self._reports: dict[str, _AutomaticReport] = {}

    def latest(self) -> dict[str, object]:
        candidates: list[tuple[Path, Path, os.stat_result]] = []
        for directory in self._directories():
            if not directory.is_dir():
                continue
            try:
                for path in directory.iterdir():
                    if not path.is_file() or REPORT_PATTERN.fullmatch(path.name) is None:
                        continue
                    stats = path.stat()
                    candidates.append((path.resolve(), directory.resolve(), stats))
            except OSError:
                continue
        if not candidates:
            return {"found": False}
        path, directory, stats = max(candidates, key=lambda item: item[2].st_mtime_ns)
        token = secrets.token_urlsafe(32)
        self._reports[token] = _AutomaticReport(path, directory, stats.st_size, stats.st_mtime_ns)
        self._reports = dict(list(self._reports.items())[-100:])
        return {
            "found": True,
            "file_id": token,
            "filename": path.name,
            "downloaded_at": datetime.fromtimestamp(stats.st_mtime).strftime("%d/%m/%Y %H:%M:%S"),
        }

    def read(self, file_id: object) -> tuple[str, bytes]:
        if not isinstance(file_id, str):
            raise ValidationError("O arquivo automático selecionado é inválido.", code="AUTOMATIC_FILE_INVALID")
        report = self._reports.pop(file_id, None)
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
        if stats.st_size > self.max_upload_bytes:
            raise PayloadTooLargeError("O arquivo automático excede o limite permitido.")
        try:
            return path.name, path.read_bytes()
        except OSError as error:
            raise NotFoundError("Não foi possível ler o arquivo automático. Selecione-o manualmente.", code="AUTOMATIC_FILE_NOT_FOUND") from error

    def _directories(self) -> tuple[Path, ...]:
        if self._downloads_directories is not None:
            return self._downloads_directories
        directories: list[Path] = []
        configured = os.getenv("CONFERENCE_DOWNLOADS_DIRECTORY", "").strip()
        if configured:
            directories.append(Path(os.path.expandvars(configured)))
        if os.name == "nt":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                    value, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
                    if isinstance(value, str) and value.strip():
                        directories.append(Path(os.path.expandvars(value)))
            except (FileNotFoundError, OSError):
                pass
        directories.append(Path.home() / "Downloads")
        unique: dict[str, Path] = {}
        for directory in directories:
            try:
                resolved = directory.resolve()
            except OSError:
                continue
            unique[str(resolved).casefold()] = resolved
        return tuple(unique.values())
