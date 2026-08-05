from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True, slots=True)
class AppSettings:
    max_upload_bytes: int = int(os.getenv("CONFERENCE_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
    collaborator_registration_min_length: int = int(os.getenv("CONFERENCE_REGISTRATION_MIN_LENGTH", "1"))
    collaborator_registration_max_length: int = int(os.getenv("CONFERENCE_REGISTRATION_MAX_LENGTH", "20"))
    temporary_directory: Path = Path(__file__).resolve().parents[2] / "data" / "temporary_uploads"
    google_apps_script_url: str = os.getenv("GOOGLE_APPS_SCRIPT_URL", "").strip()
    google_sync_secret: str = os.getenv("GOOGLE_SYNC_SECRET", "").strip()
    sync_retry_after_seconds: int = int(os.getenv("GOOGLE_SYNC_RETRY_AFTER_SECONDS", "300"))

    @property
    def max_json_bytes(self) -> int:
        """Allow the Base64 envelope without accepting an unbounded HTTP body."""
        return ((self.max_upload_bytes + 2) // 3) * 4 + 1024 * 1024
