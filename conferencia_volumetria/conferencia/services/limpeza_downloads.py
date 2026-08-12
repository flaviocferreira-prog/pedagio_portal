"""Limpeza segura dos relatórios CSV baixados pelo WMS."""

from __future__ import annotations

import ctypes
import os
import re
from dataclasses import dataclass
from pathlib import Path


REPORT_NAME = "relatorio_ConsultaPaleteDistribuicaoAgrupada"
REPORT_PATTERN = re.compile(
    rf"^{re.escape(REPORT_NAME)}(?: \([0-9]+\))?\.csv$", re.IGNORECASE
)
REPORTS_TO_KEEP = 9


@dataclass(frozen=True, slots=True)
class LimpezaDownloadsResultado:
    """Resumo da execução, sem expor falhas de limpeza ao fluxo de importação."""

    encontrados: int
    removidos: int
    ignorados_em_uso: int
    falhas: int


def limpar_relatorios_wms(
    *,
    arquivo_em_uso: str | Path | None = None,
    downloads_directory: Path | None = None,
) -> LimpezaDownloadsResultado:
    """Mantém os nove relatórios WMS mais recentes da pasta Downloads.

    O arquivo da importação que acabou de concluir é sempre preservado. Falhas
    de acesso são contabilizadas, mas nunca interrompem a importação.
    """

    directory = downloads_directory or localizar_pasta_downloads()
    try:
        reports = sorted(
            (
                path
                for path in directory.iterdir()
                if path.is_file() and REPORT_PATTERN.fullmatch(path.name) is not None
            ),
            key=lambda path: (path.stat().st_mtime_ns, path.name.casefold()),
        )
    except OSError:
        return LimpezaDownloadsResultado(0, 0, 0, 1)

    if len(reports) < 10:
        return LimpezaDownloadsResultado(len(reports), 0, 0, 0)

    protected_name = Path(arquivo_em_uso).name.casefold() if arquivo_em_uso else None
    remaining_to_remove = len(reports) - REPORTS_TO_KEEP
    removed = 0
    in_use = 0
    failures = 0

    for path in reports:
        if remaining_to_remove == 0:
            break
        if protected_name is not None and path.name.casefold() == protected_name:
            in_use += 1
            continue
        if _arquivo_em_uso(path):
            in_use += 1
            continue
        try:
            path.unlink()
        except OSError:
            failures += 1
            continue
        removed += 1
        remaining_to_remove -= 1

    return LimpezaDownloadsResultado(len(reports), removed, in_use, failures)


def localizar_pasta_downloads() -> Path:
    """Resolve a pasta Downloads do usuário atual, inclusive quando redirecionada."""

    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                value, _ = winreg.QueryValueEx(
                    key, "{374DE290-123F-4565-9164-39C4925E467B}"
                )
            if isinstance(value, str) and value.strip():
                return Path(os.path.expandvars(value))
        except (FileNotFoundError, OSError):
            pass
    return Path.home() / "Downloads"


def _arquivo_em_uso(path: Path) -> bool:
    """Confirma se o arquivo pode receber bloqueio exclusivo antes de removê-lo."""

    if os.name == "nt":
        return _arquivo_em_uso_windows(path)
    try:
        import fcntl

        with path.open("rb") as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)
    except (BlockingIOError, OSError):
        return True
    return False


def _arquivo_em_uso_windows(path: Path) -> bool:
    generic_read_write = 0x80000000 | 0x40000000
    open_existing = 3
    file_attribute_normal = 0x80
    invalid_handle_value = ctypes.c_void_p(-1).value
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        generic_read_write,
        0,
        None,
        open_existing,
        file_attribute_normal,
        None,
    )
    if handle == invalid_handle_value:
        return True
    kernel32.CloseHandle(handle)
    return False
