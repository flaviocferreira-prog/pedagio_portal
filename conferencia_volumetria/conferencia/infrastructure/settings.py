from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def _sync_int(name: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (AttributeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(maximum, value) if maximum is not None else value


def _sync_float(name: str, default: float, *, minimum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except (AttributeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(minimum, value)


@dataclass(frozen=True, slots=True)
class AppSettings:
    max_upload_bytes: int = int(os.getenv("CONFERENCE_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
    collaborator_registration_min_length: int = int(os.getenv("CONFERENCE_REGISTRATION_MIN_LENGTH", "1"))
    collaborator_registration_max_length: int = int(os.getenv("CONFERENCE_REGISTRATION_MAX_LENGTH", "20"))
    temporary_directory: Path = Path(__file__).resolve().parents[2] / "data" / "temporary_uploads"
    logs_directory: Path = PROJECT_ROOT / "logs"
    backups_directory: Path = PROJECT_ROOT / "backups"
    server_port: int = int(os.getenv("CONFERENCE_PORT", "8080"))
    apps_script_url: str = os.getenv("APPS_SCRIPT_URL", "").strip()
    sync_shared_secret: str = os.getenv("SYNC_SHARED_SECRET", "").strip()
    sync_interval_minutes: int = _sync_int(
        "SYNC_INTERVAL_MINUTES", 30, minimum=1
    )
    sync_batch_size: int = _sync_int(
        "SYNC_BATCH_SIZE", 10, minimum=1, maximum=100
    )
    sync_max_attempts: int = _sync_int("SYNC_MAX_RETRIES", 3, minimum=1)
    sync_retry_delay_seconds: float = _sync_float(
        "SYNC_RETRY_DELAY", 2.0, minimum=0.0
    )
    sync_connect_timeout_seconds: float = _sync_float(
        "SYNC_CONNECT_TIMEOUT", 5.0, minimum=0.1
    )
    sync_read_timeout_seconds: float = _sync_float(
        "SYNC_READ_TIMEOUT", 30.0, minimum=0.1
    )
    sync_reservation_timeout_minutes: int = _sync_int(
        "SYNC_RESERVATION_TIMEOUT_MINUTES", 15, minimum=1
    )

    @property
    def max_json_bytes(self) -> int:
        """Allow the Base64 envelope without accepting an unbounded HTTP body."""
        return ((self.max_upload_bytes + 2) // 3) * 4 + 1024 * 1024

    @property
    def sync_reservation_timeout_seconds(self) -> int:
        return self.sync_reservation_timeout_minutes * 60
