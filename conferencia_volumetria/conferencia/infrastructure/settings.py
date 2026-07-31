from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppSettings:
    max_upload_bytes: int = int(os.getenv("CONFERENCE_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
    collaborator_registration_min_length: int = int(os.getenv("CONFERENCE_REGISTRATION_MIN_LENGTH", "1"))
    collaborator_registration_max_length: int = int(os.getenv("CONFERENCE_REGISTRATION_MAX_LENGTH", "20"))
    temporary_directory: Path = Path(__file__).resolve().parents[2] / "data" / "temporary_uploads"
