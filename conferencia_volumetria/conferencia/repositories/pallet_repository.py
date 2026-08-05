from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from conferencia.domain.box_codes import comparison_code_without_leading_zeros, normalize_caixa_estoque
from conferencia.domain.entities import (
    CartonStatus,
    CollaboratorContext,
    ConferenceStatus,
    ScanClassification,
)
from conferencia.infrastructure.database import SQLiteDatabase
from conferencia.infrastructure.timezones import SAO_PAULO_TIMEZONE
from conferencia.repositories.conference_attempt_repository import ConferenceAttemptRepository
from conferencia.repositories.conference_audit_repository import ConferenceAuditRepository
from conferencia.repositories.divergence_repository import DivergenceRepository


class RepositoryStateError(Exception):
    def __init__(self, state: str) -> None:
        super().__init__(state)
        self.state = state


class PendingConferenceError(Exception):
    def __init__(self, missing: int, divergent: int, extra: int, duplicate: int, by_type: dict[str, int] | None = None) -> None:
        super().__init__("A conferência possui pendências.")
        self.missing = missing
        self.divergent = divergent
        self.extra = extra
        self.duplicate = duplicate
        self.by_type = by_type or {}


class ActiveConferenceError(Exception):
    def __init__(self, public_id: str) -> None:
        super().__init__(public_id)
        self.public_id = public_id


class ContentInProgressError(Exception):
    def __init__(
        self, owner_name: str, owner_registration: str, started_at: str | None,
        imported_at: str | None,
    ) -> None:
        super().__init__("CONTENT_IN_PROGRESS")
        self.owner_name = owner_name
        self.owner_registration = owner_registration
        self.started_at = started_at
        self.imported_at = imported_at


class AmbiguousBoxCodeError(Exception):
    def __init__(self, matches: list[str]) -> None:
        super().__init__("AMBIGUOUS_BOX_CODE")
        self.matches = matches


class PalletRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database
        self.attempts = ConferenceAttemptRepository()
        self.divergences = DivergenceRepository()
        self.audit = ConferenceAuditRepository()

    def create(
        self,
        public_id: str,
        collaborator: CollaboratorContext,
        source_filename: str,
        carton_codes: list[str],
        source_fingerprint: str,
        agenda: str,
        origin: str,
        operation: str,
        shift: str,
        content_hash: str,
    ) -> dict[str, object]:
        with self.database.connection(immediate=True) as connection:
            history = self._history_by_content_hash(connection, content_hash)
            finished = next((row for row in history if row["conference_status"] == ConferenceStatus.FINISHED), None)
            if finished is not None:
                self._audit(connection, finished["id"], collaborator, "IMPORT_FINALIZED_CONTENT", content_hash, "BLOCKED")
                return {"action": "already_completed", "public_id": finished["public_id"]}
            opened = next((row for row in history if row["conference_status"] == ConferenceStatus.OPEN), None)
            if opened is not None:
                if opened["created_by_collaborator_id"] != collaborator.id:
                    raise ContentInProgressError(
                        str(opened["owner_name"] or ""),
                        str(opened["owner_registration"] or ""),
                        opened["started_at"],
                        opened["imported_at"],
                    )
                self._audit(connection, opened["id"], collaborator, "IMPORT_OPEN_CONTENT", content_hash, "RESUMED")
                return {"action": "resumed", "public_id": opened["public_id"]}
            active = self._active_for_collaborator(connection, collaborator.id)
            if active is not None:
                raise ActiveConferenceError(active["public_id"])
            previous = next((row for row in history if row["conference_status"] == ConferenceStatus.CANCELLED), None)
            imported_at = self._now(connection)
            cursor = connection.execute(
                """
                INSERT INTO pallets(
                    code, public_id, collaborator_id, source_filename,
                    source_fingerprint, import_agenda, import_origin, import_operation, imported_shift,
                    created_by_collaborator_id, total_expected, imported_at,
                    conference_status, print_status, content_hash, previous_conference_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    public_id,
                    public_id,
                    collaborator.registration,
                    source_filename,
                    source_fingerprint,
                    agenda,
                    origin,
                    operation,
                    shift,
                    collaborator.id,
                    len(carton_codes),
                    imported_at,
                    ConferenceStatus.OPEN,
                    "AVAILABLE",
                    content_hash,
                    previous["id"] if previous is not None else None,
                ),
            )
            pallet_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO expected_cartons(pallet_id, code, ds_classe) VALUES (?, ?, ?)",
                [
                    (pallet_id, normalize_caixa_estoque(item["caixa_estoque"]), normalize_caixa_estoque(item.get("ds_classe", "")).upper() or None)
                    for item in carton_codes
                ],
            )
            self.attempts.create_ready(connection, pallet_id)
            action = "created_after_cancellation" if previous is not None else "created"
            self._audit(
                connection, pallet_id, collaborator,
                "CREATED_AFTER_CANCELLATION" if previous is not None else "CONTENT_IMPORT_CREATED",
                content_hash, "CREATED",
            )
            return {
                "action": action,
                "public_id": public_id,
                "previous_public_id": previous["public_id"] if previous is not None else None,
            }

    @staticmethod
    def _history_by_content_hash(
        connection: sqlite3.Connection, content_hash: str
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT p.id, p.public_id, p.conference_status,
                   p.created_by_collaborator_id, p.imported_at, p.started_at,
                   COALESCE(c.matricula, p.collaborator_id) AS owner_registration,
                   COALESCE(c.nome, '') AS owner_name
            FROM pallets p
            LEFT JOIN colaboradores c ON c.id = p.created_by_collaborator_id
            WHERE p.content_hash = ?
            ORDER BY CASE p.conference_status
                WHEN 'FINALIZADA' THEN 1 WHEN 'EM_ABERTO' THEN 2
                WHEN 'CANCELADA' THEN 3 ELSE 4 END, p.id DESC
            """,
            (content_hash,),
        ).fetchall()

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        pallet_id: int | None,
        collaborator: CollaboratorContext,
        action: str,
        content_hash: str,
        result: str,
        justification: str | None = None,
    ) -> None:
        ConferenceAuditRepository().record(connection, pallet_id, collaborator, action, content_hash, result, PalletRepository._now(connection), justification)

    def find_active_by_collaborator(self, collaborator_id: int) -> sqlite3.Row | None:
        with self.database.connection() as connection:
            return self._active_for_collaborator(connection, collaborator_id)

    def create_reconference(
        self, previous_public_id: str, public_id: str,
        collaborator: CollaboratorContext, reason: str,
    ) -> str:
        with self.database.connection(immediate=True) as connection:
            previous = connection.execute(
                "SELECT * FROM pallets WHERE public_id = ?", (previous_public_id,)
            ).fetchone()
            if previous is None:
                raise RepositoryStateError("NOT_FOUND")
            if previous["conference_status"] != ConferenceStatus.FINISHED:
                raise RepositoryStateError("NOT_FINISHED")
            active = self._active_for_collaborator(connection, collaborator.id)
            if active is not None:
                raise ActiveConferenceError(active["public_id"])
            codes = list(connection.execute(
                "SELECT code, ds_classe FROM expected_cartons WHERE pallet_id = ? ORDER BY id", (previous["id"],)
            ))
            now = self._now(connection)
            cursor = connection.execute(
                """
                INSERT INTO pallets(
                    code, public_id, collaborator_id, source_filename, source_fingerprint,
                    import_origin, import_operation, imported_shift, imported_at,
                    created_by_collaborator_id, total_expected, updated_at, status,
                    conference_status, print_status, content_hash, previous_conference_id,
                    is_reconference, reconference_authorized_by, reconference_reason,
                    reconference_authorized_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY', ?, 'AVAILABLE', ?, ?, 1, ?, ?, ?)
                """,
                (public_id, public_id, collaborator.registration, previous["source_filename"],
                 previous["source_fingerprint"], previous["import_origin"], previous["import_operation"],
                 collaborator.shift, now, collaborator.id, len(codes), now, ConferenceStatus.OPEN,
                 previous["content_hash"], previous["id"], collaborator.id, reason, now),
            )
            pallet_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO expected_cartons(pallet_id, code, ds_classe) VALUES (?, ?, ?)",
                [(pallet_id, normalize_caixa_estoque(row["code"]), row["ds_classe"]) for row in codes],
            )
            self.attempts.create_ready(connection, pallet_id)
            self._audit(connection, pallet_id, collaborator, "RECONFERENCIA_AUTORIZADA",
                        previous["content_hash"], "CREATED", reason)
            return public_id

    def record_audit_for_public_id(
        self, public_id: str, collaborator: CollaboratorContext, action: str,
        result: str, justification: str | None = None,
    ) -> None:
        with self.database.connection(immediate=True) as connection:
            pallet = connection.execute(
                "SELECT id, content_hash FROM pallets WHERE public_id = ?", (public_id,)
            ).fetchone()
            self._audit(connection, pallet["id"] if pallet else None, collaborator, action,
                        pallet["content_hash"] if pallet else "", result, justification)

    def find_latest_finalized_by_collaborator(
        self, collaborator_id: int
    ) -> sqlite3.Row | None:
        with self.database.connection() as connection:
            return connection.execute(
                """
                SELECT public_id
                FROM pallets
                WHERE created_by_collaborator_id = ?
                  AND conference_status IN ('FINALIZADA', 'CANCELADA')
                ORDER BY COALESCE(finished_at, cancelled_at) DESC, id DESC
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
                       final_justification,
                       created_by_collaborator_id, started_by_collaborator_id,
                       finished_by_collaborator_id, cancelled_at,
                       cancelled_by_collaborator_id, import_origin, import_operation,
                       imported_shift, imported_at, conference_status,
                       productivity_boxes_per_hour, print_status, content_hash,
                       previous_conference_id, is_reconference,
                       reconference_authorized_by, reconference_reason,
                       reconference_authorized_at
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
                SELECT code, ds_classe, status, confirmed_at
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
            divergences = connection.execute(
                """SELECT d.*, finder.matricula AS found_by, resolver.matricula AS resolved_by
                   FROM conference_divergences d
                   LEFT JOIN colaboradores finder ON finder.id = d.found_by_collaborator_id
                   LEFT JOIN colaboradores resolver ON resolver.id = d.resolved_by_collaborator_id
                   WHERE d.pallet_id = ? ORDER BY d.id DESC""",
                (pallet["id"],),
            ).fetchall()
            divergence_counts = self._divergence_counts(connection, pallet["id"])
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
        workflow_status = str(pallet["conference_status"])
        return {
            "public_id": pallet["public_id"],
            "collaborator_id": registration,
            "collaborator": {
                "id": pallet["created_by_collaborator_id"],
                "registration": registration,
                "name": pallet["collaborator_name"] or "",
            },
            "source_filename": pallet["source_filename"],
            "content_hash": pallet["content_hash"],
            "pallet_class": self._pallet_class(cartons),
            "previous_conference_id": pallet["previous_conference_id"],
            "is_reconference": bool(pallet["is_reconference"]),
            "reconference_reason": pallet["reconference_reason"],
            "importation": {
                "agenda": pallet["import_agenda"],
                "origin": pallet["import_origin"],
                "operation": pallet["import_operation"],
                "shift": pallet["imported_shift"],
                "imported_at": self._format_local_datetime(pallet["imported_at"]),
                "imported_at_iso": pallet["imported_at"],
            },
            "status": workflow_status,
            "workflow_status": workflow_status,
            "display_status": self._display_status(workflow_status),
            "created_at": pallet["created_at"],
            "started_at": pallet["started_at"],
            "finished_at": pallet["finished_at"],
            "finalization": {
                "finished_at": self._format_local_datetime(pallet["finished_at"]),
                "finished_at_iso": pallet["finished_at"],
            },
            "cancellation": {
                "cancelled_at": self._format_local_datetime(pallet["cancelled_at"]),
                "cancelled_at_iso": pallet["cancelled_at"],
            },
            "duration_seconds": pallet["duration_seconds"],
            "productivity_boxes_per_hour": pallet["productivity_boxes_per_hour"],
            "print_status": pallet["print_status"],
            "synchronization": {
                "status": pallet["status_sincronizacao"],
                "started_at": self._format_local_datetime(pallet["sincronizacao_iniciada_em"]),
                "started_at_iso": pallet["sincronizacao_iniciada_em"],
                "synchronized_at": self._format_local_datetime(pallet["sincronizado_em"]),
                "synchronized_at_iso": pallet["sincronizado_em"],
                "attempts": int(pallet["tentativas_sincronizacao"] or 0),
                "last_error": pallet["ultimo_erro_sincronizacao"],
            },
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
                divergence_counts,
            ),
            "cartons": [
                {
                    "caixa_estoque": normalize_caixa_estoque(row["code"]),
                    "ds_classe": normalize_caixa_estoque(row["ds_classe"]).upper() or "NÃO INFORMADO",
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
            "divergences": [
                {
                    "id": row["id"], "type": row["divergence_type"],
                    "caixa_estoque": normalize_caixa_estoque(row["carton_code"]),
                    "description": row["description"], "status": row["status"],
                    "found_at": row["found_at"], "found_by": row["found_by"],
                    "resolved_at": row["resolved_at"], "resolved_by": row["resolved_by"],
                    "resolution_note": row["resolution_note"],
                } for row in divergences
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
        """Cancela sem apagar caixas, leituras, divergências ou auditoria."""
        with self.database.connection(immediate=True) as connection:
            pallet = connection.execute(
                "SELECT conference_status, started_at FROM pallets WHERE id = ?", (pallet_id,)
            ).fetchone()
            if pallet is None:
                raise RepositoryStateError("NOT_FOUND")
            if pallet["conference_status"] == ConferenceStatus.CANCELLED:
                raise RepositoryStateError("CANCELLED")
            if pallet["conference_status"] != ConferenceStatus.OPEN:
                raise RepositoryStateError("FINISHED")
            now = self._now(connection)
            updated = connection.execute(
                """
                UPDATE pallets
                SET status = 'COMPLETED_WITH_DIVERGENCE', conference_status = ?,
                    cancelled_at = ?, cancelled_by_collaborator_id = ?, finished_at = ?,
                    duration_seconds = MAX(0, CAST((julianday(?) - julianday(COALESCE(started_at, imported_at, created_at))) * 86400 AS INTEGER)),
                    final_justification = 'CANCELADA', updated_at = ?
                WHERE id = ? AND conference_status = ?
                """,
                (ConferenceStatus.CANCELLED, now, collaborator.id, now, now, now, pallet_id, ConferenceStatus.OPEN),
            )
            if updated.rowcount != 1:
                raise RepositoryStateError("CHANGED")
            self.attempts.cancel(connection, pallet_id, now, collaborator.id)

    def start(self, pallet_id: int, collaborator: CollaboratorContext) -> None:
        """Start exactly once after the client has rendered the imported boxes."""
        with self.database.connection(immediate=True) as connection:
            pallet = connection.execute(
                "SELECT conference_status, started_at FROM pallets WHERE id = ?", (pallet_id,)
            ).fetchone()
            if pallet is None:
                raise RepositoryStateError("NOT_FOUND")
            if pallet["conference_status"] == ConferenceStatus.CANCELLED:
                raise RepositoryStateError("CANCELLED")
            if pallet["conference_status"] != ConferenceStatus.OPEN:
                raise RepositoryStateError("FINISHED")
            if pallet["started_at"] is not None:
                return
            now = self._now(connection)
            updated = connection.execute(
                """
                UPDATE pallets
                SET status = 'IN_PROGRESS', started_at = ?,
                    started_by_collaborator_id = ?, updated_at = ?
                WHERE id = ? AND conference_status = ? AND started_at IS NULL
                """,
                (now, collaborator.id, now, pallet_id, ConferenceStatus.OPEN),
            )
            if updated.rowcount != 1:
                raise RepositoryStateError("CHANGED")
            self.attempts.start(connection, pallet_id, now, collaborator.id)

    def process_scan(
        self,
        pallet_id: int,
        code: str,
        collaborator: CollaboratorContext,
    ) -> tuple[ScanClassification, str | None, str | None, str | None]:
        with self.database.connection(immediate=True) as connection:
            pallet = connection.execute(
                "SELECT status, conference_status, started_at, cancelled_at FROM pallets WHERE id = ?", (pallet_id,)
            ).fetchone()
            if pallet is None:
                raise RepositoryStateError("NOT_FOUND")
            if pallet["cancelled_at"] is not None:
                raise RepositoryStateError("CANCELLED")
            if pallet["conference_status"] != ConferenceStatus.OPEN:
                raise RepositoryStateError("FINISHED")
            if pallet["status"] != "IN_PROGRESS" or pallet["started_at"] is None:
                raise RepositoryStateError(
                    "FINISHED" if pallet["status"].startswith("COMPLETED") else "NOT_STARTED"
                )
            attempt = self._latest_attempt(connection, pallet_id)
            if attempt is None or attempt["status"] != "IN_PROGRESS":
                raise RepositoryStateError("NOT_STARTED")
            expected_rows = connection.execute(
                """
                SELECT id, code, status, confirmed_at
                FROM expected_cartons
                WHERE pallet_id = ? AND code = ?
                """,
                (pallet_id, code),
            ).fetchall()
            expected = expected_rows[0] if expected_rows else None
            match_type: str | None = "EXACT" if expected is not None else None
            if expected is None:
                comparison_code = comparison_code_without_leading_zeros(code)
                normalized_matches = [
                    row
                    for row in connection.execute(
                        """
                        SELECT id, code, status, confirmed_at
                        FROM expected_cartons
                        WHERE pallet_id = ?
                        """,
                        (pallet_id,),
                    ).fetchall()
                    if comparison_code_without_leading_zeros(row["code"]) == comparison_code
                ]
                distinct_codes = list(dict.fromkeys(str(row["code"]) for row in normalized_matches))
                if len(distinct_codes) > 1:
                    raise AmbiguousBoxCodeError(distinct_codes)
                if normalized_matches:
                    expected = normalized_matches[0]
                    expected_rows = normalized_matches
                    match_type = "NORMALIZED"
            now = self._now(connection)
            first_seen: str | None = None
            pending_rows = [row for row in expected_rows if row["status"] == CartonStatus.PENDING]
            if expected is not None and pending_rows:
                try:
                    connection.executemany(
                        """
                        INSERT INTO attempt_confirmations(
                            attempt_id, expected_carton_id, collaborator_id, confirmed_at
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        [(attempt["id"], row["id"], collaborator.id, now) for row in pending_rows],
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
                        WHERE pallet_id = ? AND code = ? AND status = ?
                        """,
                        (
                            CartonStatus.CONFIRMED,
                            now,
                            collaborator.id,
                            attempt["id"],
                            pallet_id, expected["code"],
                            CartonStatus.PENDING,
                        ),
                    )
                    classification = (
                        ScanClassification.MATCHED
                        if updated.rowcount == len(pending_rows)
                        else ScanClassification.DUPLICATE
                    )
                    first_seen = None if updated.rowcount == len(pending_rows) else expected["confirmed_at"]
                    if classification == ScanClassification.DUPLICATE:
                        self._record_divergence(
                            connection, pallet_id, attempt["id"], "DUPLICIDADE", code,
                            "Leitura duplicada de caixa esperada.", collaborator,
                        )
            elif expected is not None:
                classification = ScanClassification.DUPLICATE
                first_seen = expected["confirmed_at"]
                self._record_divergence(
                    connection, pallet_id, attempt["id"], "DUPLICIDADE", code,
                    "Leitura duplicada de caixa esperada.", collaborator,
                )
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
                    self._record_divergence(
                        connection, pallet_id, attempt["id"], "CAIXA_NAO_ESPERADA", code,
                        "Caixa não esperada para este palete.", collaborator,
                    )
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
            return classification, first_seen, match_type, (str(expected["code"]) if expected is not None else None)

    def finish(self, pallet_id: int, collaborator: CollaboratorContext) -> None:
        pending_error: PendingConferenceError | None = None
        with self.database.connection(immediate=True) as connection:
            pallet = connection.execute(
                "SELECT conference_status, started_at FROM pallets WHERE id = ?", (pallet_id,)
            ).fetchone()
            if pallet is None:
                raise RepositoryStateError("NOT_FOUND")
            if pallet["conference_status"] == ConferenceStatus.CANCELLED:
                raise RepositoryStateError("CANCELLED")
            if pallet["conference_status"] == ConferenceStatus.FINISHED:
                return
            if pallet["conference_status"] != ConferenceStatus.OPEN or not pallet["started_at"]:
                raise RepositoryStateError("NOT_STARTED")
            attempt = self._latest_attempt(connection, pallet_id)
            if attempt is None or attempt["status"] != "IN_PROGRESS":
                raise RepositoryStateError("CHANGED")
            counts = self._counts(connection, pallet_id, attempt["id"])
            missing = max(0, counts["expected"] - counts["confirmed"])
            if missing:
                for row in connection.execute(
                    "SELECT code FROM expected_cartons WHERE pallet_id = ? AND status = 'PENDING'",
                    (pallet_id,),
                ):
                    self._record_divergence(
                        connection, pallet_id, attempt["id"], "FALTA", row["code"],
                        "Caixa esperada ainda não conferida.", collaborator,
                        deduplicate=True,
                    )
            if missing:
                pending_error = PendingConferenceError(missing, 0, 0, 0)
            else:
                now = self._now(connection)
                duration = connection.execute(
                    "SELECT MAX(0, CAST((julianday(?) - julianday(?)) * 86400 AS INTEGER)) AS seconds",
                    (now, pallet["started_at"]),
                ).fetchone()["seconds"]
                productivity = (counts["confirmed"] * 3600 / duration) if duration else float(counts["confirmed"])
                updated = connection.execute(
                    """
                    UPDATE pallets
                    SET status = 'COMPLETED', conference_status = ?, finished_at = ?,
                        duration_seconds = ?, productivity_boxes_per_hour = ?,
                        finished_by_collaborator_id = ?,
                        final_justification = NULL, status_sincronizacao = 'PENDENTE',
                        sincronizacao_iniciada_em = NULL, sincronizado_em = NULL,
                        recibo_sincronizacao = NULL, ultimo_erro_sincronizacao = NULL,
                        updated_at = ?
                    WHERE id = ? AND conference_status = ?
                    """,
                    (ConferenceStatus.FINISHED, now, duration, productivity, collaborator.id, now, pallet_id, ConferenceStatus.OPEN),
                )
                if updated.rowcount != 1:
                    raise RepositoryStateError("CHANGED")
                connection.execute(
                    """
                    UPDATE conference_attempts SET status = 'COMPLETED', finished_at = ?,
                        finished_by_collaborator_id = ?
                    WHERE id = ? AND status = 'IN_PROGRESS'
                    """,
                    (now, collaborator.id, attempt["id"]),
                )
        if pending_error is not None:
            raise pending_error

    @staticmethod
    def _record_divergence(
        connection: sqlite3.Connection, pallet_id: int, attempt_id: int,
        divergence_type: str, code: str, description: str,
        collaborator: CollaboratorContext, *, deduplicate: bool = False,
    ) -> None:
        DivergenceRepository().record(
            connection, pallet_id, attempt_id, divergence_type, code, description,
            collaborator, PalletRepository._now(connection), deduplicate=deduplicate,
        )

    @staticmethod
    def _divergence_counts(connection: sqlite3.Connection, pallet_id: int) -> dict:
        return DivergenceRepository().counts(connection, pallet_id)

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
              AND conference_status = 'EM_ABERTO'
            ORDER BY id DESC
            LIMIT 1
            """,
            (collaborator_id,),
        ).fetchone()

    @staticmethod
    def _latest_attempt(
        connection: sqlite3.Connection, pallet_id: int
    ) -> sqlite3.Row | None:
        return ConferenceAttemptRepository().latest(connection, pallet_id)

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
        """Format persisted UTC timestamps for the Brazilian operational UI.

        SQLite stores the application timestamps as ISO-8601 UTC strings with
        ``Z`` (see migration 008 and ``_now``).  Naive values therefore follow
        that same UTC persistence contract before a single conversion to São
        Paulo.  Aware datetimes retain their own offset and are never shifted
        twice.
        """
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                iso_value = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
                parsed = datetime.fromisoformat(iso_value)
            except ValueError:
                return None
        else:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(SAO_PAULO_TIMEZONE).strftime(
            "%d/%m/%Y %H:%M:%S"
        )

    @staticmethod
    def _display_status(status: str) -> str:
        return {
            "EM_ABERTO": "EM ABERTO",
            "FINALIZADA": "CONFERÊNCIA FINALIZADA",
            "CANCELADA": "CANCELADA",
        }.get(status, status)

    @staticmethod
    def _pallet_class(cartons: list[sqlite3.Row]) -> str:
        classes = {normalize_caixa_estoque(row["ds_classe"]).upper() for row in cartons if normalize_caixa_estoque(row["ds_classe"])}
        return next(iter(classes)) if len(classes) == 1 else "MISTO" if classes else "NÃO INFORMADO"

    @staticmethod
    def _summary(
        expected: int, confirmed: int, extra: int, duplicate: int,
        divergence_counts: dict | None = None,
    ) -> dict[str, int | float]:
        missing = max(0, expected - confirmed)
        coverage = min(
            100.0,
            max(0.0, (confirmed / expected * 100) if expected else 0.0),
        )
        divergence_counts = divergence_counts or {"total": 0, "pending": 0, "resolved": 0, "by_type": {}}
        return {
            "total_expected": expected,
            "total_confirmed": confirmed,
            "total_missing": missing,
            "total_extra": extra,
            "total_divergent": extra,
            "total_duplicate_reads": duplicate,
            "coverage_percent": round(coverage, 2),
            "expected_quantity": expected,
            "confirmed_quantity": confirmed,
            "pending_quantity": missing,
            "surplus_quantity": extra,
            "divergent_quantity": extra,
            "duplicate_quantity": duplicate,
            "divergences_found": divergence_counts["total"],
            "divergences_pending": divergence_counts["pending"],
            "divergences_resolved": divergence_counts["resolved"],
            "pending_divergences_by_type": divergence_counts["by_type"],
            "can_finish": confirmed == expected,
        }
