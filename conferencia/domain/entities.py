from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CartonStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"


class ConferenceStatus(StrEnum):
    """Estados oficiais e imutáveis do ciclo de uma conferência."""

    OPEN = "EM_ABERTO"
    FINISHED = "FINALIZADA"
    CANCELLED = "CANCELADA"


class ScanClassification(StrEnum):
    MATCHED = "MATCHED"
    DUPLICATE = "DUPLICATE"
    EXTRA = "EXTRA"
    DUPLICATE_EXTRA = "DUPLICATE_EXTRA"


@dataclass(frozen=True, slots=True)
class CollaboratorContext:
    id: int | None
    registration: str
    name: str
    shift: str = "ADM"


@dataclass(frozen=True, slots=True)
class ConferenceImport:
    collaborator: CollaboratorContext
    filename: str
    content_base64: str
    origin: str
    operation: str
