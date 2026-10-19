from __future__ import annotations

import sqlite3


class ConferenceAttemptRepository:
    def latest(self, connection: sqlite3.Connection, pallet_id: int) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM conference_attempts WHERE pallet_id = ? ORDER BY attempt_number DESC LIMIT 1", (pallet_id,)).fetchone()

    def create_ready(self, connection: sqlite3.Connection, pallet_id: int) -> None:
        connection.execute("INSERT INTO conference_attempts(pallet_id, attempt_number, status) VALUES (?, 1, 'READY')", (pallet_id,))

    def start(self, connection: sqlite3.Connection, pallet_id: int, now: str, collaborator_id: int | None) -> None:
        connection.execute("UPDATE conference_attempts SET status = 'IN_PROGRESS', started_at = ?, started_by_collaborator_id = ? WHERE pallet_id = ? AND attempt_number = 1 AND status = 'READY'", (now, collaborator_id, pallet_id))

    def cancel(self, connection: sqlite3.Connection, pallet_id: int, now: str, collaborator_id: int | None) -> None:
        connection.execute("UPDATE conference_attempts SET status = 'CANCELLED', finished_at = ?, finished_by_collaborator_id = ? WHERE pallet_id = ? AND status IN ('READY', 'IN_PROGRESS')", (now, collaborator_id, pallet_id))

    def finish(self, connection: sqlite3.Connection, attempt_id: int, now: str, collaborator_id: int | None) -> None:
        connection.execute("UPDATE conference_attempts SET status = 'COMPLETED', finished_at = ?, finished_by_collaborator_id = ? WHERE id = ? AND status = 'IN_PROGRESS'", (now, collaborator_id, attempt_id))
