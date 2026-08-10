from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from conferencia.infrastructure.database import SQLiteDatabase
from conferencia.sync.repository import SyncRepository


class SyncRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="test-sync-")
        self.database = SQLiteDatabase(Path(self.tempdir.name) / "sync.db")
        self.database.initialize()
        self.repository = SyncRepository(self.database)
        with self.database.connection(immediate=True) as connection:
            collaborator = connection.execute(
                """
                INSERT INTO colaboradores(matricula, nome, turno)
                VALUES ('000123', 'OPERADOR SYNC', 'T1')
                """
            )
            self.collaborator_id = int(collaborator.lastrowid)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _create_pallet(
        self,
        public_id: str,
        *,
        conference_status: str = "FINALIZADA",
        sync_status: str = "PENDENTE",
        finished_at: str = "2026-08-10T12:00:00Z",
    ) -> int:
        with self.database.connection(immediate=True) as connection:
            pallet = connection.execute(
                """
                INSERT INTO pallets(
                    code, public_id, collaborator_id, source_filename, status,
                    created_at, started_at, finished_at, updated_at,
                    duration_seconds, total_expected, import_origin,
                    import_operation, imported_shift, imported_at,
                    conference_status, productivity_boxes_per_hour,
                    print_status, content_hash, import_agenda,
                    created_by_collaborator_id, started_by_collaborator_id,
                    finished_by_collaborator_id
                ) VALUES (
                    ?, ?, '000123', ?, ?,
                    '2026-08-10T10:00:00Z', '2026-08-10T10:05:00Z', ?, ?,
                    3600, 0, 'PORTAL',
                    'DIGITAL', 'T1', '2026-08-10T10:00:00Z',
                    ?, 60.0,
                    'AVAILABLE', ?, 'AGENDA TESTE',
                    ?, ?, ?
                )
                """,
                (
                    f"PALLET-{public_id}",
                    public_id,
                    f"{public_id}.csv",
                    "COMPLETED" if conference_status == "FINALIZADA" else "IN_PROGRESS",
                    finished_at if conference_status == "FINALIZADA" else None,
                    finished_at if conference_status == "FINALIZADA" else "2026-08-10T10:05:00Z",
                    conference_status,
                    f"hash-{public_id}",
                    self.collaborator_id,
                    self.collaborator_id,
                    self.collaborator_id if conference_status == "FINALIZADA" else None,
                ),
            )
            pallet_id = int(pallet.lastrowid)
            connection.execute(
                """
                INSERT INTO conference_sync_state(
                    pallet_id, status, created_at, updated_at
                ) VALUES (?, ?, '2026-08-10T12:00:00Z', '2026-08-10T12:00:00Z')
                """,
                (pallet_id, sync_status),
            )
        return pallet_id

    def _sync_state(self, public_id: str) -> dict[str, object]:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT state.*
                FROM conference_sync_state state
                INNER JOIN pallets pallet ON pallet.id = state.pallet_id
                WHERE pallet.public_id = ?
                """,
                (public_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def test_fresh_database_applies_migration_22_with_queue_constraints(self) -> None:
        with self.database.connection() as connection:
            migration = connection.execute(
                "SELECT description FROM schema_migrations WHERE version = 22"
            ).fetchone()
            table = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'conference_sync_state'"
            ).fetchone()
            indexes = {
                str(row[1])
                for row in connection.execute("PRAGMA index_list(conference_sync_state)")
            }
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()

        self.assertIsNotNone(migration)
        self.assertEqual("fila autoritativa de sincronizacao backend", migration["description"])
        self.assertIsNotNone(table)
        table_sql = str(table["sql"])
        for state in ("PENDENTE", "SINCRONIZANDO", "SINCRONIZADA"):
            self.assertIn(state, table_sql)
        self.assertIn("idx_conference_sync_status", indexes)
        self.assertEqual([], violations)

    def test_migration_22_backfills_only_preexisting_finalized_conferences(self) -> None:
        with self.database.connection(immediate=True) as connection:
            connection.execute("DROP TABLE conference_sync_state")
            connection.execute("DELETE FROM schema_migrations WHERE version = 22")
            finalized = connection.execute(
                """
                INSERT INTO pallets(
                    code, public_id, collaborator_id, source_filename, status,
                    created_at, updated_at, finished_at, conference_status,
                    created_by_collaborator_id, finished_by_collaborator_id
                ) VALUES (
                    'MIGRATION-FINAL', 'CONF-MIGRATION-FINAL', '000123', 'final.csv',
                    'COMPLETED', '2026-08-10T10:00:00Z', '2026-08-10T12:00:00Z',
                    '2026-08-10T12:00:00Z', 'FINALIZADA', ?, ?
                )
                """,
                (self.collaborator_id, self.collaborator_id),
            )
            connection.execute(
                """
                INSERT INTO pallets(
                    code, public_id, collaborator_id, source_filename, status,
                    created_at, updated_at, conference_status,
                    created_by_collaborator_id
                ) VALUES (
                    'MIGRATION-OPEN', 'CONF-MIGRATION-OPEN', '000123', 'open.csv',
                    'IN_PROGRESS', '2026-08-10T10:00:00Z', '2026-08-10T12:00:00Z',
                    'EM_ABERTO', ?
                )
                """,
                (self.collaborator_id,),
            )
            finalized_id = int(finalized.lastrowid)

        self.database.initialize()

        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT pallet_id, status FROM conference_sync_state ORDER BY pallet_id"
            ).fetchall()
        self.assertEqual([(finalized_id, "PENDENTE")], [(row["pallet_id"], row["status"]) for row in rows])

    def test_pending_selection_uses_only_finalized_pending_rows_in_stable_order(self) -> None:
        second = self._create_pallet(
            "CONF-PENDING-SECOND", finished_at="2026-08-10T12:02:00Z"
        )
        first = self._create_pallet(
            "CONF-PENDING-FIRST", finished_at="2026-08-10T12:01:00Z"
        )
        self._create_pallet("CONF-SYNCING", sync_status="SINCRONIZANDO")
        self._create_pallet("CONF-DONE", sync_status="SINCRONIZADA")
        self._create_pallet("CONF-OPEN", conference_status="EM_ABERTO")
        self._create_pallet("CONF-CANCELLED", conference_status="CANCELADA")

        self.assertEqual(
            ["CONF-PENDING-FIRST", "CONF-PENDING-SECOND"],
            self.repository.pending_ids(),
        )
        # The earlier finish must win even though it has the later local id.
        self.assertGreater(first, second)
        self.assertEqual(
            {"pending": 2, "syncing": 1, "synchronized": 1, "last_error": None},
            self.repository.summary(),
        )

    def test_reserve_release_and_confirmation_enforce_state_transitions(self) -> None:
        self._create_pallet("CONF-A", finished_at="2026-08-10T12:01:00Z")
        self._create_pallet("CONF-B", finished_at="2026-08-10T12:02:00Z")

        reservations = self.repository.reserve(
            ["CONF-B", "CONF-A", "CONF-A", "CONF-UNKNOWN"],
            now="2026-08-10T13:00:00Z",
        )
        self.assertEqual(["CONF-A", "CONF-B"], [item.public_id for item in reservations])
        for public_id in ("CONF-A", "CONF-B"):
            state = self._sync_state(public_id)
            self.assertEqual("SINCRONIZANDO", state["status"])
            self.assertEqual("2026-08-10T13:00:00Z", state["reserved_at"])
            self.assertEqual("2026-08-10T13:00:00Z", state["last_attempt_at"])
            self.assertEqual(1, state["attempt_count"])

        long_error = "temporary-" + ("x" * 600)
        self.assertEqual(
            1,
            self.repository.release(
                ["CONF-A"], long_error, now="2026-08-10T13:01:00Z"
            ),
        )
        released = self._sync_state("CONF-A")
        self.assertEqual("PENDENTE", released["status"])
        self.assertIsNone(released["reserved_at"])
        self.assertEqual(500, len(str(released["last_error"])))
        self.assertEqual(0, self.repository.mark_synchronized(["CONF-A"]))

        self.assertEqual(
            1,
            self.repository.mark_synchronized(
                ["CONF-B", "CONF-B"], now="2026-08-10T13:02:00Z"
            ),
        )
        synchronized = self._sync_state("CONF-B")
        self.assertEqual("SINCRONIZADA", synchronized["status"])
        self.assertEqual("2026-08-10T13:02:00Z", synchronized["synchronized_at"])
        self.assertIsNone(synchronized["reserved_at"])

        self.assertEqual(1, len(self.repository.reserve(["CONF-A"])))
        self.assertEqual(2, self._sync_state("CONF-A")["attempt_count"])
        self.assertEqual(1, self.repository.mark_synchronized(["CONF-A"]))
        self.assertEqual([], self.repository.pending_ids())

    def test_recover_stale_resets_only_old_or_missing_reservations(self) -> None:
        for public_id in ("CONF-OLD", "CONF-RECENT", "CONF-NO-TIMESTAMP"):
            self._create_pallet(public_id)
            self.repository.reserve([public_id], now="2026-08-10T11:59:50Z")
        with self.database.connection(immediate=True) as connection:
            connection.execute(
                """
                UPDATE conference_sync_state SET reserved_at = '2026-08-10T11:58:00Z'
                WHERE pallet_id = (SELECT id FROM pallets WHERE public_id = 'CONF-OLD')
                """
            )
            connection.execute(
                """
                UPDATE conference_sync_state SET reserved_at = NULL
                WHERE pallet_id = (SELECT id FROM pallets WHERE public_id = 'CONF-NO-TIMESTAMP')
                """
            )

        recovered = self.repository.recover_stale(
            60, now=datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
        )

        self.assertEqual(2, recovered)
        self.assertEqual("PENDENTE", self._sync_state("CONF-OLD")["status"])
        self.assertEqual("PENDENTE", self._sync_state("CONF-NO-TIMESTAMP")["status"])
        self.assertEqual("SINCRONIZANDO", self._sync_state("CONF-RECENT")["status"])
        self.assertIn(
            "recuperada automaticamente",
            str(self._sync_state("CONF-OLD")["last_error"]),
        )

    def test_payload_preserves_all_scan_occurrences_and_operational_relations(self) -> None:
        pallet_id = self._create_pallet("CONF-COMPLETE")
        with self.database.connection(immediate=True) as connection:
            connection.execute(
                "UPDATE pallets SET total_expected = 2 WHERE id = ?", (pallet_id,)
            )
            attempt = connection.execute(
                """
                INSERT INTO conference_attempts(
                    pallet_id, attempt_number, status, started_at, finished_at,
                    started_by_collaborator_id, finished_by_collaborator_id, created_at
                ) VALUES (?, 1, 'COMPLETED', '2026-08-10T10:05:00Z',
                          '2026-08-10T12:00:00Z', ?, ?, '2026-08-10T10:00:00Z')
                """,
                (pallet_id, self.collaborator_id, self.collaborator_id),
            )
            attempt_id = int(attempt.lastrowid)
            item_ids: list[int] = []
            for item_number in range(2):
                item = connection.execute(
                    """
                    INSERT INTO expected_cartons(
                        pallet_id, code, ds_classe, status, confirmed_at,
                        confirmed_by_collaborator_id, confirmed_attempt_id
                    ) VALUES (?, '000001', 'CLASSE A', 'CONFIRMED',
                              '2026-08-10T10:10:00Z', ?, ?)
                    """,
                    (pallet_id, self.collaborator_id, attempt_id),
                )
                item_ids.append(int(item.lastrowid))
                connection.execute(
                    """
                    INSERT INTO attempt_confirmations(
                        attempt_id, expected_carton_id, collaborator_id, confirmed_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        item.lastrowid,
                        self.collaborator_id,
                        f"2026-08-10T10:10:{item_number:02d}Z",
                    ),
                )

            scan_ids: list[int] = []
            for index in range(60):
                scan = connection.execute(
                    """
                    INSERT INTO scan_events(
                        pallet_id, scanned_code, classification, scanned_at,
                        attempt_id, collaborator_id, collaborator_registration
                    ) VALUES (?, '000001', ?, ?, ?, ?, '000123')
                    """,
                    (
                        pallet_id,
                        "MATCHED" if index == 0 else "DUPLICATE",
                        f"2026-08-10T10:{10 + index // 60:02d}:{index % 60:02d}Z",
                        attempt_id,
                        self.collaborator_id,
                    ),
                )
                scan_ids.append(int(scan.lastrowid))
            extra_scan = connection.execute(
                """
                INSERT INTO scan_events(
                    pallet_id, scanned_code, classification, scanned_at,
                    attempt_id, collaborator_id, collaborator_registration
                ) VALUES (?, '009999', 'EXTRA', '2026-08-10T10:20:00Z', ?, ?, '000123')
                """,
                (pallet_id, attempt_id, self.collaborator_id),
            )
            extra_scan_id = int(extra_scan.lastrowid)
            unexpected = connection.execute(
                """
                INSERT INTO unexpected_cartons(
                    pallet_id, attempt_id, code, first_seen_at, last_seen_at,
                    attempts, first_scanned_by_collaborator_id
                ) VALUES (?, ?, '009999', '2026-08-10T10:20:00Z',
                          '2026-08-10T10:20:00Z', 1, ?)
                """,
                (pallet_id, attempt_id, self.collaborator_id),
            )
            unexpected_id = int(unexpected.lastrowid)
            connection.execute(
                """
                INSERT INTO surplus_events(
                    pallet_id, attempt_id, unexpected_carton_id, scan_event_id,
                    code, scanned_at, scan_order, treatment_status,
                    corrected_by_collaborator_id, corrected_by_registration,
                    corrected_by_name, corrected_at, treatment_method
                ) VALUES (?, ?, ?, ?, '009999', '2026-08-10T10:20:00Z', ?,
                          'CORRIGIDA', ?, '000123', 'OPERADOR SYNC',
                          '2026-08-10T11:00:00Z', 'CONFIRMACAO_FINAL')
                """,
                (
                    pallet_id,
                    attempt_id,
                    unexpected_id,
                    extra_scan_id,
                    extra_scan_id,
                    self.collaborator_id,
                ),
            )
            treatment = connection.execute(
                """
                INSERT INTO surplus_treatment_confirmations(
                    pallet_id, attempt_id, collaborator_id,
                    collaborator_registration, collaborator_name, surplus_count,
                    confirmed_at, final_conference_status
                ) VALUES (?, ?, ?, '000123', 'OPERADOR SYNC', 1,
                          '2026-08-10T11:00:00Z', 'FINALIZADA')
                """,
                (pallet_id, attempt_id, self.collaborator_id),
            )
            treatment_id = int(treatment.lastrowid)
            connection.execute(
                """
                INSERT INTO surplus_treatment_items(
                    confirmation_id, unexpected_carton_id, code, scanned_at, scan_order
                ) VALUES (?, ?, '009999', '2026-08-10T10:20:00Z', ?)
                """,
                (treatment_id, unexpected_id, extra_scan_id),
            )
            connection.execute(
                """
                INSERT INTO conference_divergences(
                    pallet_id, attempt_id, divergence_type, carton_code,
                    description, status, found_at, found_by_collaborator_id
                ) VALUES (?, ?, 'SOBRA', '009999', 'Caixa excedente.',
                          'RESOLVIDA', '2026-08-10T10:20:00Z', ?)
                """,
                (pallet_id, attempt_id, self.collaborator_id),
            )
            connection.execute(
                """
                INSERT INTO conference_audit_events(
                    pallet_id, collaborator_id, registration, profile,
                    action, content_hash, result, created_at
                ) VALUES (?, ?, '000123', 'OPERADOR', 'FINALIZAR',
                          'hash-CONF-COMPLETE', 'SUCESSO', '2026-08-10T12:00:00Z')
                """,
                (pallet_id, self.collaborator_id),
            )

        reservations = self.repository.reserve(["CONF-COMPLETE"])
        payload = self.repository.payloads(reservations)[0]

        self.assertEqual("CONF-COMPLETE", payload["id_conferencia"])
        self.assertEqual("CONF-COMPLETE", payload["conference"]["id_conferencia"])
        self.assertEqual("000123", payload["conference"]["collaborator_registration"])
        self.assertEqual("CLASSE A", payload["conference"]["pallet_class"])
        self.assertEqual(61, payload["conference"]["total_scans"])

        self.assertEqual(2, len(payload["items"]))
        self.assertEqual(["000001", "000001"], [item["caixa_estoque"] for item in payload["items"]])
        self.assertEqual(item_ids, [item["local_id"] for item in payload["items"]])
        self.assertEqual(2, len(payload["confirmations"]))

        scans = payload["scans"]
        self.assertEqual(61, len(scans))
        self.assertEqual(scan_ids + [extra_scan_id], [scan["local_id"] for scan in scans])
        self.assertEqual(60, sum(scan["caixa_estoque"] == "000001" for scan in scans))
        self.assertEqual("009999", scans[-1]["caixa_estoque"])
        self.assertEqual("EXTRA", scans[-1]["classification"])

        self.assertEqual(1, len(payload["attempts"]))
        self.assertEqual(1, len(payload["divergences"]))
        self.assertEqual(1, len(payload["surplus_summaries"]))
        self.assertEqual(1, len(payload["surpluses"]))
        self.assertEqual(unexpected_id, payload["surpluses"][0]["unexpected_carton_id"])
        self.assertEqual(extra_scan_id, payload["surpluses"][0]["scan_event_id"])
        self.assertEqual(1, len(payload["treatments"]))
        self.assertEqual(1, len(payload["treatment_items"]))
        self.assertEqual(treatment_id, payload["treatment_items"][0]["confirmation_id"])
        self.assertEqual(1, len(payload["audit"]))


if __name__ == "__main__":
    unittest.main()
