from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class SyncState(StrEnum):
    PENDING = "PENDENTE"
    SYNCING = "SINCRONIZANDO"
    SYNCHRONIZED = "SINCRONIZADA"


class RemoteResult(StrEnum):
    INSERTED = "INSERTED"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class SyncReservation:
    pallet_id: int
    public_id: str


@dataclass(slots=True)
class SyncRunResult:
    found: int = 0
    synchronized: int = 0
    already_exists: int = 0
    failed: int = 0
    batches: int = 0
    busy: bool = False
    configured: bool = True
    message: str = ""
    failed_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "found": self.found,
            "synchronized": self.synchronized,
            "already_exists": self.already_exists,
            "failed": self.failed,
            "batches": self.batches,
            "busy": self.busy,
            "configured": self.configured,
            "message": self.message,
        }


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
