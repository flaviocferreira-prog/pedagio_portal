from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Iterable

from conferencia.infrastructure.database import SQLiteDatabase
from conferencia.sync.models import SyncReservation, utc_now


class SyncRepository:
    """Owns the authoritative local queue and complete operational snapshots."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def pending_ids(self) -> list[str]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT p.public_id
                FROM conference_sync_state state
                INNER JOIN pallets p ON p.id = state.pallet_id
                WHERE p.conference_status = 'FINALIZADA'
                  AND state.status = 'PENDENTE'
                ORDER BY p.finished_at, p.id
                """
            ).fetchall()
        return [str(row["public_id"]) for row in rows]

    def summary(self) -> dict[str, int | str | None]:
        with self.database.connection() as connection:
            counts = {
                str(row["status"]): int(row["total"])
                for row in connection.execute(
                    """
                    SELECT state.status, COUNT(*) AS total
                    FROM conference_sync_state state
                    INNER JOIN pallets p ON p.id = state.pallet_id
                    WHERE p.conference_status = 'FINALIZADA'
                    GROUP BY state.status
                    """
                )
            }
            last_error = connection.execute(
                """
                SELECT state.last_error
                FROM conference_sync_state state
                INNER JOIN pallets p ON p.id = state.pallet_id
                WHERE p.conference_status = 'FINALIZADA'
                  AND state.last_error IS NOT NULL
                ORDER BY state.updated_at DESC, state.pallet_id DESC
                LIMIT 1
                """
            ).fetchone()
        return {
            "pending": counts.get("PENDENTE", 0),
            "syncing": counts.get("SINCRONIZANDO", 0),
            "synchronized": counts.get("SINCRONIZADA", 0),
            "last_error": str(last_error["last_error"]) if last_error else None,
        }

    def reserve(self, public_ids: Iterable[str], *, now: str | None = None) -> list[SyncReservation]:
        requested = list(dict.fromkeys(str(value) for value in public_ids if value))
        if not requested:
            return []
        reserved_at = now or utc_now()
        marks = ",".join("?" for _ in requested)
        with self.database.connection(immediate=True) as connection:
            rows = connection.execute(
                f"""
                SELECT p.id, p.public_id
                FROM conference_sync_state state
                INNER JOIN pallets p ON p.id = state.pallet_id
                WHERE p.public_id IN ({marks})
                  AND p.conference_status = 'FINALIZADA'
                  AND state.status = 'PENDENTE'
                ORDER BY p.finished_at, p.id
                """,
                requested,
            ).fetchall()
            if rows:
                pallet_ids = [int(row["id"]) for row in rows]
                id_marks = ",".join("?" for _ in pallet_ids)
                connection.execute(
                    f"""
                    UPDATE conference_sync_state
                    SET status = 'SINCRONIZANDO', reserved_at = ?,
                        last_attempt_at = ?, attempt_count = attempt_count + 1,
                        last_error = NULL, updated_at = ?
                    WHERE pallet_id IN ({id_marks}) AND status = 'PENDENTE'
                    """,
                    (reserved_at, reserved_at, reserved_at, *pallet_ids),
                )
        return [SyncReservation(int(row["id"]), str(row["public_id"])) for row in rows]

    def mark_synchronized(self, public_ids: Iterable[str], *, now: str | None = None) -> int:
        values = list(dict.fromkeys(str(value) for value in public_ids if value))
        if not values:
            return 0
        synchronized_at = now or utc_now()
        marks = ",".join("?" for _ in values)
        with self.database.connection(immediate=True) as connection:
            cursor = connection.execute(
                f"""
                UPDATE conference_sync_state
                SET status = 'SINCRONIZADA', synchronized_at = ?, reserved_at = NULL,
                    last_error = NULL, updated_at = ?
                WHERE pallet_id IN (
                    SELECT id FROM pallets WHERE public_id IN ({marks})
                ) AND status = 'SINCRONIZANDO'
                """,
                (synchronized_at, synchronized_at, *values),
            )
        return cursor.rowcount

    def release(self, public_ids: Iterable[str], error: str, *, now: str | None = None) -> int:
        values = list(dict.fromkeys(str(value) for value in public_ids if value))
        if not values:
            return 0
        released_at = now or utc_now()
        safe_error = str(error).strip()[:500] or "Falha temporária de sincronização."
        marks = ",".join("?" for _ in values)
        with self.database.connection(immediate=True) as connection:
            cursor = connection.execute(
                f"""
                UPDATE conference_sync_state
                SET status = 'PENDENTE', reserved_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE pallet_id IN (
                    SELECT id FROM pallets WHERE public_id IN ({marks})
                ) AND status = 'SINCRONIZANDO'
                """,
                (safe_error, released_at, *values),
            )
        return cursor.rowcount

    def recover_stale(self, timeout_seconds: int, *, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        threshold = (current - timedelta(seconds=max(1, timeout_seconds))).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        recovered_at = current.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with self.database.connection(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE conference_sync_state
                SET status = 'PENDENTE', reserved_at = NULL,
                    last_error = 'Reserva interrompida recuperada automaticamente.',
                    updated_at = ?
                WHERE status = 'SINCRONIZANDO'
                  AND (reserved_at IS NULL OR reserved_at < ?)
                """,
                (recovered_at, threshold),
            )
        return cursor.rowcount

    def payloads(self, reservations: Iterable[SyncReservation]) -> list[dict[str, object]]:
        items = list(reservations)
        if not items:
            return []
        payloads: list[dict[str, object]] = []
        with self.database.connection() as connection:
            for reservation in items:
                payloads.append(self._payload(connection, reservation))
        return payloads

    @staticmethod
    def _dicts(rows: object) -> list[dict[str, object]]:
        return [dict(row) for row in rows]

    def _payload(self, connection: object, reservation: SyncReservation) -> dict[str, object]:
        conference = connection.execute(
            """
            SELECT p.id AS local_id, p.public_id AS id_conferencia, p.code AS codigo_palete,
                   p.source_filename, p.status, p.conference_status, p.created_at,
                   p.started_at, p.finished_at, p.cancelled_at, p.updated_at,
                   p.duration_seconds, p.productivity_boxes_per_hour, p.total_expected,
                   p.final_justification, p.source_fingerprint, p.content_hash,
                   p.import_agenda, p.import_origin, p.import_operation,
                   p.imported_shift, p.imported_at, p.print_status,
                   p.is_reconference, p.reconference_reason,
                   p.reconference_authorized_at, previous.public_id AS previous_id_conferencia,
                   COALESCE(creator.matricula, p.collaborator_id)
                       AS collaborator_registration,
                   creator.nome AS collaborator_name, creator.turno AS collaborator_shift,
                   starter.matricula AS started_by, finisher.matricula AS finished_by,
                   canceller.matricula AS cancelled_by,
                   authorizer.matricula AS reconference_authorized_by,
                   (SELECT COUNT(*) FROM expected_cartons item
                    WHERE item.pallet_id = p.id AND item.status = 'CONFIRMED') AS total_confirmed,
                   (SELECT COUNT(*) FROM expected_cartons item
                    WHERE item.pallet_id = p.id AND item.status = 'PENDING') AS total_missing,
                   (SELECT COUNT(*) FROM scan_events scan
                    WHERE scan.pallet_id = p.id) AS total_scans,
                   (SELECT COUNT(*) FROM scan_events scan
                    WHERE scan.pallet_id = p.id
                      AND scan.classification IN ('DUPLICATE', 'DUPLICATE_EXTRA')) AS total_duplicates,
                   (SELECT COUNT(*) FROM surplus_events surplus
                    WHERE surplus.pallet_id = p.id) AS total_surpluses,
                   (SELECT COUNT(*) FROM surplus_events surplus
                    WHERE surplus.pallet_id = p.id AND surplus.treatment_status = 'PENDENTE')
                       AS pending_surpluses,
                   CASE
                     WHEN (SELECT COUNT(DISTINCT UPPER(TRIM(COALESCE(item.ds_classe, ''))))
                           FROM expected_cartons item
                           WHERE item.pallet_id = p.id AND TRIM(COALESCE(item.ds_classe, '')) <> '') = 0
                       THEN 'NÃO INFORMADO'
                     WHEN (SELECT COUNT(DISTINCT UPPER(TRIM(COALESCE(item.ds_classe, ''))))
                           FROM expected_cartons item
                           WHERE item.pallet_id = p.id AND TRIM(COALESCE(item.ds_classe, '')) <> '') = 1
                       THEN (SELECT UPPER(TRIM(item.ds_classe)) FROM expected_cartons item
                             WHERE item.pallet_id = p.id AND TRIM(COALESCE(item.ds_classe, '')) <> '' LIMIT 1)
                     ELSE 'MISTO'
                   END AS pallet_class
            FROM pallets p
            LEFT JOIN pallets previous ON previous.id = p.previous_conference_id
            LEFT JOIN colaboradores creator ON creator.id = p.created_by_collaborator_id
            LEFT JOIN colaboradores starter ON starter.id = p.started_by_collaborator_id
            LEFT JOIN colaboradores finisher ON finisher.id = p.finished_by_collaborator_id
            LEFT JOIN colaboradores canceller ON canceller.id = p.cancelled_by_collaborator_id
            LEFT JOIN colaboradores authorizer ON authorizer.id = p.reconference_authorized_by
            WHERE p.id = ? AND p.public_id = ? AND p.conference_status = 'FINALIZADA'
            """,
            (reservation.pallet_id, reservation.public_id),
        ).fetchone()
        if conference is None:
            raise RuntimeError(f"Conferência reservada não encontrada: {reservation.public_id}")
        pallet_id = reservation.pallet_id
        relations = {
            "items": self._dicts(connection.execute(
                """
                SELECT item.id AS local_id, item.code AS caixa_estoque, item.ds_classe,
                       item.status, item.confirmed_at,
                       actor.matricula AS confirmed_by, attempt.attempt_number AS confirmed_attempt
                FROM expected_cartons item
                LEFT JOIN colaboradores actor ON actor.id = item.confirmed_by_collaborator_id
                LEFT JOIN conference_attempts attempt ON attempt.id = item.confirmed_attempt_id
                WHERE item.pallet_id = ? ORDER BY item.id
                """, (pallet_id,)).fetchall()),
            "scans": self._dicts(connection.execute(
                """
                SELECT scan.id AS local_id, scan.scanned_code AS caixa_estoque,
                       scan.classification, scan.scanned_at,
                       attempt.attempt_number, scan.collaborator_registration,
                       actor.nome AS collaborator_name
                FROM scan_events scan
                LEFT JOIN conference_attempts attempt ON attempt.id = scan.attempt_id
                LEFT JOIN colaboradores actor ON actor.id = scan.collaborator_id
                WHERE scan.pallet_id = ? ORDER BY scan.id
                """, (pallet_id,)).fetchall()),
            "attempts": self._dicts(connection.execute(
                """
                SELECT attempt.id AS local_id, attempt.attempt_number, attempt.status,
                       attempt.started_at, attempt.finished_at, attempt.created_at,
                       starter.matricula AS started_by, restarter.matricula AS restarted_by,
                       finisher.matricula AS finished_by
                FROM conference_attempts attempt
                LEFT JOIN colaboradores starter ON starter.id = attempt.started_by_collaborator_id
                LEFT JOIN colaboradores restarter ON restarter.id = attempt.restarted_by_collaborator_id
                LEFT JOIN colaboradores finisher ON finisher.id = attempt.finished_by_collaborator_id
                WHERE attempt.pallet_id = ? ORDER BY attempt.attempt_number, attempt.id
                """, (pallet_id,)).fetchall()),
            "confirmations": self._dicts(connection.execute(
                """
                SELECT confirmation.id AS local_id, attempt.attempt_number,
                       item.id AS item_local_id, item.code AS caixa_estoque,
                       actor.matricula AS collaborator_registration,
                       actor.nome AS collaborator_name, confirmation.confirmed_at
                FROM attempt_confirmations confirmation
                INNER JOIN conference_attempts attempt ON attempt.id = confirmation.attempt_id
                INNER JOIN expected_cartons item ON item.id = confirmation.expected_carton_id
                LEFT JOIN colaboradores actor ON actor.id = confirmation.collaborator_id
                WHERE attempt.pallet_id = ? ORDER BY confirmation.id
                """, (pallet_id,)).fetchall()),
            "divergences": self._dicts(connection.execute(
                """
                SELECT divergence.id AS local_id, attempt.attempt_number,
                       divergence.divergence_type, divergence.carton_code AS caixa_estoque,
                       divergence.description, divergence.status, divergence.found_at,
                       finder.matricula AS found_by, divergence.resolved_at,
                       resolver.matricula AS resolved_by, divergence.resolution_note
                FROM conference_divergences divergence
                LEFT JOIN conference_attempts attempt ON attempt.id = divergence.attempt_id
                LEFT JOIN colaboradores finder ON finder.id = divergence.found_by_collaborator_id
                LEFT JOIN colaboradores resolver ON resolver.id = divergence.resolved_by_collaborator_id
                WHERE divergence.pallet_id = ? ORDER BY divergence.id
                """, (pallet_id,)).fetchall()),
            "surplus_summaries": self._dicts(connection.execute(
                """
                SELECT unexpected.id AS local_id, attempt.attempt_number,
                       unexpected.code AS caixa_estoque, unexpected.first_seen_at,
                       unexpected.last_seen_at, unexpected.attempts,
                       actor.matricula AS first_scanned_by
                FROM unexpected_cartons unexpected
                LEFT JOIN conference_attempts attempt ON attempt.id = unexpected.attempt_id
                LEFT JOIN colaboradores actor ON actor.id = unexpected.first_scanned_by_collaborator_id
                WHERE unexpected.pallet_id = ? ORDER BY unexpected.id
                """, (pallet_id,)).fetchall()),
            "surpluses": self._dicts(connection.execute(
                """
                SELECT surplus.id AS local_id, attempt.attempt_number,
                       surplus.unexpected_carton_id, surplus.scan_event_id,
                       surplus.code AS caixa_estoque, surplus.scanned_at, surplus.scan_order,
                       surplus.treatment_status, surplus.corrected_by_registration,
                       surplus.corrected_by_name, surplus.corrected_at, surplus.treatment_method
                FROM surplus_events surplus
                LEFT JOIN conference_attempts attempt ON attempt.id = surplus.attempt_id
                WHERE surplus.pallet_id = ? ORDER BY surplus.scan_order, surplus.id
                """, (pallet_id,)).fetchall()),
            "treatments": self._dicts(connection.execute(
                """
                SELECT treatment.id AS local_id, attempt.attempt_number,
                       treatment.collaborator_registration, treatment.collaborator_name,
                       treatment.surplus_count, treatment.confirmed_at,
                       treatment.final_conference_status
                FROM surplus_treatment_confirmations treatment
                LEFT JOIN conference_attempts attempt ON attempt.id = treatment.attempt_id
                WHERE treatment.pallet_id = ? ORDER BY treatment.id
                """, (pallet_id,)).fetchall()),
            "treatment_items": self._dicts(connection.execute(
                """
                SELECT item.id AS local_id, item.confirmation_id,
                       item.unexpected_carton_id, item.code AS caixa_estoque,
                       item.scanned_at, item.scan_order
                FROM surplus_treatment_items item
                INNER JOIN surplus_treatment_confirmations treatment
                    ON treatment.id = item.confirmation_id
                WHERE treatment.pallet_id = ? ORDER BY item.id
                """, (pallet_id,)).fetchall()),
            "audit": self._dicts(connection.execute(
                """
                SELECT audit.id AS local_id, audit.registration, audit.profile,
                       audit.ip_address, audit.action, audit.content_hash,
                       audit.justification, audit.result, audit.created_at,
                       actor.nome AS collaborator_name
                FROM conference_audit_events audit
                LEFT JOIN colaboradores actor ON actor.id = audit.collaborator_id
                WHERE audit.pallet_id = ? ORDER BY audit.id
                """, (pallet_id,)).fetchall()),
        }
        return {
            "id_conferencia": reservation.public_id,
            "conference": dict(conference),
            **relations,
        }
