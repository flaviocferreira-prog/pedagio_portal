from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from conferencia.infrastructure.paths import runtime_root


PROJECT_ROOT = runtime_root()
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True, slots=True)
class AppSettings:
    max_upload_bytes: int = int(os.getenv("CONFERENCE_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
    collaborator_registration_min_length: int = int(os.getenv("CONFERENCE_REGISTRATION_MIN_LENGTH", "1"))
    collaborator_registration_max_length: int = int(os.getenv("CONFERENCE_REGISTRATION_MAX_LENGTH", "20"))
    temporary_directory: Path = PROJECT_ROOT / "data" / "temporary_uploads"
    logs_directory: Path = PROJECT_ROOT / "logs"
    backups_directory: Path = PROJECT_ROOT / "backups"
    server_port: int = int(os.getenv("CONFERENCE_PORT", "8080"))
    google_sync_url: str = os.getenv("GOOGLE_SYNC_URL", "").strip()
    google_sync_token: str = os.getenv("GOOGLE_SYNC_TOKEN", "").strip()
    google_sync_batch_size: int = int(os.getenv("GOOGLE_SYNC_BATCH_SIZE", "10"))
    google_sync_max_attempts: int = int(os.getenv("GOOGLE_SYNC_MAX_ATTEMPTS", "3"))
    google_sync_retry_delay_seconds: float = float(os.getenv("GOOGLE_SYNC_RETRY_DELAY_SECONDS", "1"))
    google_sync_connect_timeout_seconds: float = float(os.getenv("GOOGLE_SYNC_CONNECT_TIMEOUT_SECONDS", "5"))
    google_sync_read_timeout_seconds: float = float(os.getenv("GOOGLE_SYNC_READ_TIMEOUT_SECONDS", "30"))

    @property
    def max_json_bytes(self) -> int:
        """Allow the Base64 envelope without accepting an unbounded HTTP body."""
        return ((self.max_upload_bytes + 2) // 3) * 4 + 1024 * 1024
