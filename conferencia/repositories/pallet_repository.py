from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from conferencia.domain.box_codes import normalize_caixa_estoque
from conferencia.domain.entities import (
    CartonStatus,
    CollaboratorContext,
    ScanClassification,
)
from conferencia.infrastructure.database import SQLiteDatabase


class RepositoryStateError(Exception):
    def __init__(self, state: str) -> None:
        super().__init__(state)
        self.state = state


class PendingConferenceError(Exception):
    def __init__(self, missing: int, divergent: int) -> None:
        super().__init__("A conferência possui pendências.")
        self.missing = missing
        self.divergent = divergent


class ActiveConferenceError(Exception):
    def __init__(self, public_id: str) -> None:
        super().__init__(public_id)
        self.public_id = public_id


class PalletRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def create(
        self,
        public_id: str,
        collaborator: CollaboratorContext,
        source_filename: str,
        carton_codes: list[str],
        source_fingerprint: str,
        origin: str,
        operation: str,
        shift: str,
    ) -> None:
        with self.database.connection(immediate=True) as connection:
            active = self._active_for_collaborator(connection, collaborator.id)
            if active is not None:
                raise ActiveConferenceError(active["public_id"])
            cursor = connection.execute(
                """
                INSERT INTO pallets(
                    code, public_id, collaborator_id, source_filename,
                    source_fingerprint, import_origin, import_operation, imported_shift,
                    imported_at, created_by_collaborator_id, total_expected, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    public_id,
                    public_id,
                    collaborator.registration,
                    source_filename,
                    source_fingerprint,
                    origin,
                    operation,
                    shift,
                    None,
                    collaborator.id,
                    len(carton_codes),
                    None,
                ),
            )
            pallet_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO expected_cartons(pallet_id, code) VALUES (?, ?)",
                [
                    (pallet_id, normalize_caixa_estoque(carton_code))
                    for carton_code in carton_codes
                ],
            )
            connection.execute(
                """
                INSERT INTO conference_attempts(pallet_id, attempt_number, status)
                VALUES (?, 1, 'READY')
                """,
                (pallet_id,),
            )
            # O instante oficial só é fixado após a conferência, suas caixas e a
            # tentativa terem sido persistidas, ainda antes do commit atômico.
            imported_at = self._now(connection)
            connection.execute(
                "UPDATE pallets SET imported_at = ?, updated_at = ? WHERE id = ?",
                (imported_at, imported_at, pallet_id),
            )

    def find_active_by_collaborator(self, collaborator_id: int) -> sqlite3.Row | None:
        with self.database.connection() as connection:
            return self._active_for_collaborator(connection, collaborator_id)

    def find_latest_finalized_by_collaborator(
        self, collaborator_id: int
    ) -> sqlite3.Row | None:
        with self.database.connection() as connection:
            return connection.execute(
                """
                SELECT public_id
                FROM pallets
                WHERE created_by_collaborator_id = ?
                  AND cancelled_at IS NULL
                  AND status = 'COMPLETED'
                ORDER BY finished_at DESC, id DESC
                LIMIT 1
                """,
                (collaborator_id,),
            ).fetchone()

    def find_by_public_id(self, public_id: str) -> sqlite3.Row | None:
        with self.database.connection() as connection:
            return connection.execute(
                """
                SELECT id, public_id, collaborator_id, source_filename, status,
                       created_at, started_at, finished_at, duration_seconds,
                       sync_status, final_justification,
                       created_by_collaborator_id, started_by_collaborator_id,
                       finished_by_collaborator_id, cancelled_at,
                       cancelled_by_collaborator_id, import_origin, import_operation,
                       imported_shift, imported_at
                FROM pallets
                WHERE public_id = ?
                """,
                (public_id,),
            ).fetchone()

    def details(self, public_id: str) -> dict | None:
        with self.database.connection() as connection:
            pallet = connection.execute(
                """
                SELECT p.*, c.matricula AS collaborator_registration,
                       c.nome AS collaborator_name
                FROM pallets p
                LEFT JOIN colaboradores c ON c.id = p.created_by_collaborator_id
                WHERE p.public_id = ?
                """,
                (public_id,),
            ).fetchone()
            if pallet is None:
                return None
            attempt = self._latest_attempt(connection, pallet["id"])
            if attempt is None and pallet["cancelled_at"] is None:
                raise RuntimeError("Conferência sem tentativa operacional.")
            cartons = connection.execute(
                """
                SELECT code, status, confirmed_at
                FROM expected_cartons
                WHERE pallet_id = ?
                ORDER BY id
                """,
                (pallet["id"],),
            ).fetchall()
            if attempt is None:
                scans = []
                extras = []
                counts = {"expected": 0, "confirmed": 0, "extra": 0, "duplicate": 0}
            else:
                scans = connection.execute(
                    """
                    SELECT scanned_code, classification, scanned_at,
                           collaborator_registration
                    FROM scan_events
                    WHERE attempt_id = ?
                    ORDER BY id DESC
                    LIMIT 50
                    """,
                    (attempt["id"],),
                ).fetchall()
                counts = self._counts(connection, pallet["id"], attempt["id"])
                extras = connection.execute(
                    """
                    SELECT code, first_seen_at, last_seen_at, attempts
                    FROM unexpected_cartons
                    WHERE attempt_id = ?
                    ORDER BY id DESC
                    """,
                    (attempt["id"],),
                ).fetchall()
            attempts = connection.execute(
                """
                SELECT a.id, a.attempt_number, a.status, a.started_at, a.finished_at,
                       starter.matricula AS started_by,
                       restarter.matricula AS restarted_by,
                       finisher.matricula AS finished_by,
                       COUNT(DISTINCT CASE WHEN se.classification = 'MATCHED'
                                          THEN se.scanned_code END) AS confirmed,
                       COUNT(DISTINCT CASE WHEN se.classification = 'EXTRA'
                                          THEN se.scanned_code END) AS divergent
                FROM conference_attempts a
                LEFT JOIN colaboradores starter ON starter.id = a.started_by_collaborator_id
                LEFT JOIN colaboradores restarter ON restarter.id = a.restarted_by_collaborator_id
                LEFT JOIN colaboradores finisher ON finisher.id = a.finished_by_collaborator_id
                LEFT JOIN scan_events se ON se.attempt_id = a.id
                WHERE a.pallet_id = ?
                GROUP BY a.id
                ORDER BY a.attempt_number
                """,
                (pallet["id"],),
            ).fetchall()
        registration = pallet["collaborator_registration"] or pallet["collaborator_id"]
        workflow_status = self._workflow_status(pallet, counts)
        return {
            "public_id": pallet["public_id"],
            "collaborator_id": registration,
            "collaborator": {
                "id": pallet["created_by_collaborator_id"],
                "registration": registration,
                "name": pallet["collaborator_name"] or "",
            },
            "source_filename": pallet["source_filename"],
            "importation": {
                "origin": pallet["import_origin"],
                "operation": pallet["import_operation"],
                "shift": pallet["imported_shift"],
                "imported_at": self._format_local_datetime(pallet["imported_at"]),
                "imported_at_iso": pallet["imported_at"],
            },
            "status": "CANCELLED" if pallet["cancelled_at"] else pallet["status"],
            "workflow_status": workflow_status,
            "display_status": self._display_status(workflow_status),
            "created_at": pallet["created_at"],
            "started_at": pallet["started_at"],
            "finished_at": pallet["finished_at"],
            "finalization": {
                "finished_at": self._format_local_datetime(pallet["finished_at"]),
                "finished_at_iso": pallet["finished_at"],
            },
            "duration_seconds": pallet["duration_seconds"],
            "sync_status": pallet["sync_status"],
            "final_justification": pallet["final_justification"],
            "active_attempt": {
                "id": attempt["id"] if attempt else None,
                "number": attempt["attempt_number"] if attempt else 0,
                "status": attempt["status"] if attempt else "CANCELLED",
                "started_at": attempt["started_at"] if attempt else None,
            },
            "summary": self._summary(
                counts["expected"],
                counts["confirmed"],
                counts["extra"],
                counts["duplicate"],
            ),
            "cartons": [
                {
                    "caixa_estoque": normalize_caixa_estoque(row["code"]),
                    "status": row["status"],
                    "confirmed_at": row["confirmed_at"],
                }
                for row in cartons
            ],
            "recent_scans": [
                {
                    "caixa_estoque": normalize_caixa_estoque(row["scanned_code"]),
                    "classification": row["classification"],
                    "scanned_at": row["scanned_at"],
                    "collaborator_registration": row["collaborator_registration"],
                }
                for row in scans
            ],
            "unexpected_cartons": [
                {
                    "caixa_estoque": normalize_caixa_estoque(row["code"]),
                    "first_seen_at": row["first_seen_at"],
                    "last_seen_at": row["last_seen_at"],
                    "attempts": row["attempts"],
                }
                for row in extras
            ],
            "attempts": [
                {
                    "id": row["id"],
                    "number": row["attempt_number"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "started_by": row["started_by"],
                    "restarted_by": row["restarted_by"],
                    "finished_by": row["finished_by"],
                    "confirmed": row["confirmed"],
                    "divergent": row["divergent"],
                }
                for row in attempts
            ],
        }

    def cancel(self, pallet_id: int, collaborator: CollaboratorContext) -> None:
        """Remove apenas os dados operacionais deste palete em uma transação."""
        with self.database.connection(immediate=True) as connection:
            pallet = connection.execute(
                "SELECT status, cancelled_at FROM pallets WHERE id = ?", (pallet_id,)
            ).fetchone()
            if pallet is None:
                raise RepositoryStateError("NOT_FOUND")
            if pallet["cancelled_at"] is not None:
                raise RepositoryStateError("CANCELLED")
            if pallet["status"] not in ("READY", "IN_PROGRESS"):
                raise RepositoryStateError("FINISHED")
            now = self._now(connection)
            connection.execute(
                "DELETE FROM synchronization_attempts WHERE pallet_id = ?", (pallet_id,)
            )
            connection.execute(
                """
                DELETE FROM attempt_confirmations
                WHERE attempt_id IN (SELECT id FROM conference_attempts WHERE pallet_id = ?)
                """,
                (pallet_id,),
            )
            connection.execute("DELETE FROM scan_events WHERE pallet_id = ?", (pallet_id,))
            connection.execute("DELETE FROM unexpected_cartons WHERE pallet_id = ?", (pallet_id,))
            connection.execute("DELETE FROM expected_cartons WHERE pallet_id = ?", (pallet_id,))
            connection.execute("DELETE FROM conference_attempts WHERE pallet_id = ?", (pallet_id,))
            updated = connection.execute(
                """
                UPDATE pallets
                SET status = 'COMPLETED_WITH_DIVERGENCE', cancelled_at = ?,
                    cancelled_by_collaborator_id = ?, finished_at = ?,
                    final_justification = 'CANCELADA', sync_status = 'NOT_READY',
                    updated_at = ?
                WHERE id = ? AND cancelled_at IS NULL
                """,
                (now, collaborator.id, now, now, pallet_id),
            )
            if updated.rowcount != 1:
                raise RepositoryStateError("CANCELLED")

    def start(self, pallet_id: int, collaborator: CollaboratorContext) -> None:
        with self.database.connection(immediate=True) as connection:
            pallet = connection.execute(
                "SELECT status, cancelled_at FROM pallets WHERE id = ?", (pallet_id,)
            ).fetchone()
            if pallet is None:
                raise RepositoryStateError("NOT_FOUND")
            if pallet["cancelled_at"] is not None:
                raise RepositoryStateError("CANCELLED")
            if pallet["status"] == "IN_PROGRESS":
                return
            if pallet["status"] != "READY":
                raise RepositoryStateError("FINISHED")
            attempt = self._latest_attempt(connection, pallet_id)
            now = self._now(connection)
            updated = connection.execute(
                """
                UPDATE pallets
                SET status = 'IN_PROGRESS', started_at = ?,
                    started_by_collaborator_id = ?, updated_at = ?
                WHERE id = ? AND status = 'READY'
                """,
                (now, collaborator.id, now, pallet_id),
            )
            if updated.rowcount != 1:
                raise RepositoryStateError("CHANGED")
            connection.execute(
                """
                UPDATE conference_attempts
                SET status = 'IN_PROGRESS', started_at = ?,
                    started_by_collaborator_id = ?
                WHERE id = ? AND status = 'READY'
                """,
                (now, collaborator.id, attempt["id"]),
            )

    def process_scan(
        self,
        pallet_id: int,
        code: str,
        collaborator: CollaboratorContext,
    ) -> tuple[ScanClassification, str | None]:
        with self.database.connection(immediate=True) as connection:
            pallet = connection.execute(
                "SELECT status, started_at, cancelled_at FROM pallets WHERE id = ?", (pallet_id,)
            ).fetchone()
            if pallet is None:
                raise RepositoryStateError("NOT_FOUND")
            if pallet["cancelled_at"] is not None:
                raise RepositoryStateError("CANCELLED")
            if pallet["status"] != "IN_PROGRESS" or pallet["started_at"] is None:
                raise RepositoryStateError(
                    "FINISHED" if pallet["status"].startswith("COMPLETED") else "NOT_STARTED"
                )
            attempt = self._latest_attempt(connection, pallet_id)
            if attempt is None or attempt["status"] != "IN_PROGRESS":
                raise RepositoryStateError("NOT_STARTED")
            expected = connection.execute(
                """
                SELECT id, code, status, confirmed_at
                FROM expected_cartons
                WHERE pallet_id = ? AND code = ? COLLATE NOCASE
                """,
                (pallet_id, code),
            ).fetchone()
            now = self._now(connection)
            first_seen: str | None = None
            if expected is not None and expected["status"] == CartonStatus.PENDING:
                try:
                    connection.execute(
                        """
                        INSERT INTO attempt_confirmations(
                            attempt_id, expected_carton_id, collaborator_id, confirmed_at
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (attempt["id"], expected["id"], collaborator.id, now),
                    )
                except sqlite3.IntegrityError:
                    classification = ScanClassification.DUPLICATE
                    confirmation = connection.execute(
                        """
                        SELECT confirmed_at FROM attempt_confirmations
                        WHERE attempt_id = ? AND expected_carton_id = ?
                        """,
                        (attempt["id"], expected["id"]),
                    ).fetchone()
                    first_seen = confirmation["confirmed_at"] if confirmation else expected["confirmed_at"]
                else:
                    updated = connection.execute(
                        """
                        UPDATE expected_cartons
                        SET status = ?, confirmed_at = ?,
                            confirmed_by_collaborator_id = ?, confirmed_attempt_id = ?
                        WHERE id = ? AND status = ?
                        """,
                        (
                            CartonStatus.CONFIRMED,
                            now,
                            collaborator.id,
                            attempt["id"],
                            expected["id"],
                            CartonStatus.PENDING,
                        ),
                    )
                    classification = (
                        ScanClassification.MATCHED
                        if updated.rowcount == 1
                        else ScanClassification.DUPLICATE
                    )
                    first_seen = None if updated.rowcount == 1 else expected["confirmed_at"]
            elif expected is not None:
                classification = ScanClassification.DUPLICATE
                first_seen = expected["confirmed_at"]
            else:
                extra = connection.execute(
                    """
                    SELECT first_seen_at
                    FROM unexpected_cartons
                    WHERE attempt_id = ? AND code = ? COLLATE NOCASE
                    """,
                    (attempt["id"], code),
                ).fetchone()
                if extra is None:
                    connection.execute(
                        """
                        INSERT INTO unexpected_cartons(
                            pallet_id, attempt_id, code, first_seen_at, last_seen_at,
                            attempts, first_scanned_by_collaborator_id
                        )
                        VALUES (?, ?, ?, ?, ?, 1, ?)
                        """,
                        (pallet_id, attempt["id"], code, now, now, collaborator.id),
                    )
                    classification = ScanClassification.EXTRA
                else:
                    connection.execute(
                        """
                        UPDATE unexpected_cartons
                        SET attempts = attempts + 1, last_seen_at = ?
                        WHERE attempt_id = ? AND code = ? COLLATE NOCASE
                        """,
                        (now, attempt["id"], code),
                    )
                    classification = ScanClassification.DUPLICATE_EXTRA
                    first_seen = extra["first_seen_at"]
            connection.execute(
                """
                INSERT INTO scan_events(
                    pallet_id, attempt_id, scanned_code, classification,
                    scanned_at, collaborator_id, collaborator_registration
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pallet_id,
                    attempt["id"],
                    expected["code"] if expected is not None else code,
                    classification,
                    now,
                    collaborator.id,
                    collaborator.registration,
                ),
            )
            connection.execute(
                "UPDATE pallets SET updated_at = ? WHERE id = ?", (now, pallet_id)
            )
            return classification, first_seen

    def finish(self, pallet_id: int, collaborator: CollaboratorContext) -> None:
        with self.database.connection(immediate=True) as connection:
            pallet = connection.execute(
                "SELECT status, started_at, cancelled_at FROM pallets WHERE id = ?", (pallet_id,)
            ).fetchone()
            if pallet is None:
                raise RepositoryStateError("NOT_FOUND")
            if pallet["cancelled_at"] is not None:
                raise RepositoryStateError("CANCELLED")
            if pallet["status"] != "IN_PROGRESS":
                raise RepositoryStateError("FINISHED")
            attempt = self._latest_attempt(connection, pallet_id)
            counts = self._counts(connection, pallet_id, attempt["id"])
            missing = max(0, counts["expected"] - counts["confirmed"])
            if missing:
                raise PendingConferenceError(missing, 0)
            now = self._now(connection)
            updated = connection.execute(
                """
                UPDATE pallets
                SET status = 'COMPLETED', finished_at = ?,
                    duration_seconds = MAX(
                        0, CAST((julianday(?) - julianday(started_at)) * 86400 AS INTEGER)
                    ),
                    finished_by_collaborator_id = ?, sync_status = 'PENDING',
                    final_justification = NULL, updated_at = ?
                WHERE id = ? AND status = 'IN_PROGRESS'
                """,
                (now, now, collaborator.id, now, pallet_id),
            )
            if updated.rowcount != 1:
                raise RepositoryStateError("CHANGED")
            connection.execute(
                """
                UPDATE conference_attempts
                SET status = 'COMPLETED', finished_at = ?,
                    finished_by_collaborator_id = ?
                WHERE id = ? AND status = 'IN_PROGRESS'
                """,
                (now, collaborator.id, attempt["id"]),
            )

    def restart(self, pallet_id: int, collaborator: CollaboratorContext) -> None:
        with self.database.connection(immediate=True) as connection:
            pallet = connection.execute(
                "SELECT status, cancelled_at FROM pallets WHERE id = ?", (pallet_id,)
            ).fetchone()
            if pallet is None:
                raise RepositoryStateError("NOT_FOUND")
            if pallet["cancelled_at"] is not None:
                raise RepositoryStateError("CANCELLED")
            if pallet["status"] != "IN_PROGRESS":
                raise RepositoryStateError(
                    "FINISHED" if pallet["status"].startswith("COMPLETED") else "NOT_STARTED"
                )
            current = self._latest_attempt(connection, pallet_id)
            if current is None or current["status"] != "IN_PROGRESS":
                raise RepositoryStateError("CHANGED")
            now = self._now(connection)
            updated = connection.execute(
                """
                UPDATE conference_attempts
                SET status = 'RESTARTED', finished_at = ?,
                    restarted_by_collaborator_id = ?
                WHERE id = ? AND status = 'IN_PROGRESS'
                """,
                (now, collaborator.id, current["id"]),
            )
            if updated.rowcount != 1:
                raise RepositoryStateError("CHANGED")
            connection.execute(
                """
                UPDATE expected_cartons
                SET status = 'PENDING', confirmed_at = NULL,
                    confirmed_by_collaborator_id = NULL, confirmed_attempt_id = NULL
                WHERE pallet_id = ?
                """,
                (pallet_id,),
            )
            connection.execute(
                """
                INSERT INTO conference_attempts(
                    pallet_id, attempt_number, status, started_at,
                    started_by_collaborator_id
                )
                VALUES (?, ?, 'IN_PROGRESS', ?, ?)
                """,
                (
                    pallet_id,
                    int(current["attempt_number"]) + 1,
                    now,
                    collaborator.id,
                ),
            )
            connection.execute(
                """
                UPDATE pallets
                SET status = 'IN_PROGRESS', started_at = ?, finished_at = NULL,
                    duration_seconds = NULL, final_justification = NULL,
                    sync_status = 'NOT_READY', started_by_collaborator_id = ?,
                    finished_by_collaborator_id = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, collaborator.id, now, pallet_id),
            )

    def list_recent(self) -> list[dict]:
        with self.database.connection() as connection:
            public_ids = [
                row["public_id"]
                for row in connection.execute(
                    "SELECT public_id FROM pallets ORDER BY id DESC LIMIT 100"
                )
            ]
        return [
            details
            for public_id in public_ids
            if (details := self.details(public_id)) is not None
        ]

    @staticmethod
    def _active_for_collaborator(
        connection: sqlite3.Connection, collaborator_id: int | None
    ) -> sqlite3.Row | None:
        if collaborator_id is None:
            return None
        return connection.execute(
            """
            SELECT public_id
            FROM pallets
            WHERE created_by_collaborator_id = ?
              AND cancelled_at IS NULL
              AND status IN ('READY', 'IN_PROGRESS')
            ORDER BY id DESC
            LIMIT 1
            """,
            (collaborator_id,),
        ).fetchone()

    def record_sync_not_configured(
        self,
        pallet_id: int,
        collaborator: CollaboratorContext,
        message: str,
    ) -> None:
        with self.database.connection(immediate=True) as connection:
            pallet = connection.execute(
                "SELECT status, cancelled_at FROM pallets WHERE id = ?", (pallet_id,)
            ).fetchone()
            if pallet is None:
                raise RepositoryStateError("NOT_FOUND")
            if pallet["cancelled_at"] is not None:
                raise RepositoryStateError("CANCELLED")
            if pallet["status"] != "COMPLETED":
                raise RepositoryStateError("NOT_FINISHED")
            now = self._now(connection)
            connection.execute(
                """
                INSERT INTO synchronization_attempts(
                    pallet_id, collaborator_id, status, message,
                    started_at, finished_at
                )
                VALUES (?, ?, 'NOT_CONFIGURED', ?, ?, ?)
                """,
                (pallet_id, collaborator.id, message, now, now),
            )
            connection.execute(
                """
                UPDATE pallets
                SET sync_status = 'NOT_CONFIGURED', updated_at = ?
                WHERE id = ?
                """,
                (now, pallet_id),
            )

    @staticmethod
    def _latest_attempt(
        connection: sqlite3.Connection, pallet_id: int
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT *
            FROM conference_attempts
            WHERE pallet_id = ?
            ORDER BY attempt_number DESC
            LIMIT 1
            """,
            (pallet_id,),
        ).fetchone()

    @staticmethod
    def _counts(
        connection: sqlite3.Connection, pallet_id: int, attempt_id: int
    ) -> dict[str, int]:
        expected = connection.execute(
            """
            SELECT COUNT(*) AS expected,
                   SUM(CASE WHEN status = 'CONFIRMED' THEN 1 ELSE 0 END) AS confirmed
            FROM expected_cartons
            WHERE pallet_id = ?
            """,
            (pallet_id,),
        ).fetchone()
        extra = connection.execute(
            "SELECT COUNT(*) AS total FROM unexpected_cartons WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()["total"]
        duplicate = connection.execute(
            """
            SELECT COUNT(*) AS total
            FROM scan_events
            WHERE attempt_id = ?
              AND classification IN ('DUPLICATE', 'DUPLICATE_EXTRA')
            """,
            (attempt_id,),
        ).fetchone()["total"]
        return {
            "expected": int(expected["expected"] or 0),
            "confirmed": int(expected["confirmed"] or 0),
            "extra": int(extra or 0),
            "duplicate": int(duplicate or 0),
        }

    @staticmethod
    def _now(connection: sqlite3.Connection) -> str:
        return connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AS now"
        ).fetchone()["now"]

    @staticmethod
    def _format_local_datetime(value: object) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo("America/Sao_Paulo")).strftime(
            "%d/%m/%Y %H:%M:%S"
        )

    @staticmethod
    def _display_status(status: str) -> str:
        return {
            "READY": "PRONTA",
            "IN_PROGRESS": "EM CONFERÊNCIA",
            "AGUARDANDO_FINALIZACAO": "100% CONFERIDO — AGUARDANDO FINALIZAÇÃO",
            "FINALIZADA": "CONFERÊNCIA FINALIZADA",
            "CANCELADA": "CANCELADA",
            "COMPLETED": "CONFERÊNCIA FINALIZADA",
            "COMPLETED_WITH_DIVERGENCE": "FINALIZADA COM DIVERGÊNCIA",
        }.get(status, status)

    @staticmethod
    def _workflow_status(pallet: sqlite3.Row, counts: dict[str, int]) -> str:
        if pallet["cancelled_at"]:
            return "CANCELADA"
        if pallet["status"] == "COMPLETED":
            return "FINALIZADA"
        if (
            pallet["status"] == "IN_PROGRESS"
            and counts["expected"] > 0
            and counts["confirmed"] == counts["expected"]
        ):
            return "AGUARDANDO_FINALIZACAO"
        return str(pallet["status"])

    @staticmethod
    def _summary(
        expected: int, confirmed: int, extra: int, duplicate: int
    ) -> dict[str, int | float]:
        missing = max(0, expected - confirmed)
        coverage = min(
            100.0,
            max(0.0, (confirmed / expected * 100) if expected else 0.0),
        )
        return {
            "total_expected": expected,
            "total_confirmed": confirmed,
            "total_missing": missing,
            "total_extra": extra,
            "total_duplicate_reads": duplicate,
            "coverage_percent": round(coverage, 2),
            "expected_quantity": expected,
            "confirmed_quantity": confirmed,
            "pending_quantity": missing,
            "surplus_quantity": extra,
            "duplicate_quantity": duplicate,
        }
