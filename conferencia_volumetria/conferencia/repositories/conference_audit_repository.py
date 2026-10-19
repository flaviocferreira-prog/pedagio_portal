from __future__ import annotations

import sqlite3

from conferencia.domain.entities import CollaboratorContext


class ConferenceAuditRepository:
    def record(self, connection: sqlite3.Connection, pallet_id: int | None, collaborator: CollaboratorContext, action: str, content_hash: str, result: str, now: str, justification: str | None = None) -> None:
        connection.execute("INSERT INTO conference_audit_events(pallet_id, collaborator_id, registration, profile, action, content_hash, justification, result, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (pallet_id, collaborator.id, collaborator.registration, collaborator.shift, action, content_hash, justification, result, now))
