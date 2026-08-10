from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Optional


MINIMUM_PYTHON_VERSION = (3, 11)


def is_supported_python(version: Optional[Sequence[int]] = None) -> bool:
    current = tuple(version or sys.version_info)
    return current[:2] >= MINIMUM_PYTHON_VERSION


def format_python_version(version: Optional[Sequence[int]] = None) -> str:
    current = tuple(version or sys.version_info)
    return ".".join(str(part) for part in current[:3])


def unsupported_python_message(version: Optional[Sequence[int]] = None) -> str:
    return (
        f"Python {format_python_version(version)} encontrado. "
        "Esta ferramenta exige Python 3.11 ou superior. "
        "Instale o Python 3.11+ e execute instalar.bat novamente."
    )
