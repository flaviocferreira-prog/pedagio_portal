"""Resolve bundled resources separately from persistent operational data."""

from __future__ import annotations

import sys
from pathlib import Path


def runtime_root() -> Path:
    """Directory beside the executable when frozen, otherwise the source root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_root() -> Path:
    """Directory containing PyInstaller bundled HTML and static assets."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", runtime_root()))
    return runtime_root()
