from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conferencia.infrastructure.database import SQLiteDatabase
from conferencia.sincronizacao.client import GoogleSyncError
from conferencia.sincronizacao.repository import SincronizacaoRepository, TABLES
from conferencia.sincronizacao.service import SincronizacaoEmAndamentoError, SincronizacaoService


class FakeRepository:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids[:]
        self.sent: list[list[str]] = []
        self.failed: list[list[str]] = []

    def pending_conference_ids(self) -> list[str]:
        return self.ids[:]

    def build_payload(self, ids: list[str]) -> dict[str, object]:
        return {"conference_ids": ids, "tables": {name: [] for name in TABLES}}

    def mark_synchronized(self, ids: set[str], _timestamp: str) -> None:
        self.sent.append(sorted(ids))
        self.ids = [item for item in self.ids if item not in ids]

    def mark_failed(self, ids: set[str] | list[str], _message: str, _timestamp: str) -> None:
        self.failed.append(sorted(ids))


class FakeClient:
    configured = True

    def __init__(self, responses: list[dict[str, object] | Exception]) -> None:
        self.responses = responses
        self.payloads: list[dict[str, object]] = []

    def send_batch(self, payload: dict[str, object]) -> dict[str, object]:
        self.payloads.append(payload)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class SincronizacaoServiceTests(unittest.TestCase):
    def test_sends_all_pending_in_batches_and_is_idempotent_after_confirmation(self) -> None:
        ids = [f"CONF-{number:02d}" for number in range(10)]
        repository = FakeRepository(ids)
        client = FakeClient([
            {"confirmed_conference_ids": ids[:4], "already_existing_conference_ids": []},
            {"confirmed_conference_ids": ids[4:8], "already_existing_conference_ids": []},
            {"confirmed_conference_ids": ids[8:], "already_existing_conference_ids": []},
        ])
        service = SincronizacaoService(repository, client, batch_size=4, max_attempts=1)

        result = service.sincronizar_todos_pendentes()
        again = service.sincronizar_todos_pendentes()

        self.assertEqual(10, result["found"])
        self.assertEqual(10, result["synchronized"])
        self.assertEqual(0, result["failed"])
        self.assertEqual([4, 4, 2], [len(item["conference_ids"]) for item in client.payloads])
        self.assertEqual(0, again["found"])

    def test_partial_confirmation_keeps_only_missing_conference_pending(self) -> None:
        repository = FakeRepository(["A", "B"])
        client = FakeClient([{"confirmed_conference_ids": ["A"], "already_existing_conference_ids": ["A"]}])
        service = SincronizacaoService(repository, client, max_attempts=1)

        result = service.sincronizar_todos_pendentes()

        self.assertEqual(1, result["synchronized"])
        self.assertEqual(1, result["already_existing"])
        self.assertEqual(1, result["failed"])
        self.assertEqual([["B"]], repository.failed)

    def test_connection_failure_retries_and_preserves_pending_data(self) -> None:
        repository = FakeRepository(["A"])
        client = FakeClient([GoogleSyncError("offline"), GoogleSyncError("offline"), GoogleSyncError("offline")])
        service = SincronizacaoService(repository, client, max_attempts=3, retry_delay_seconds=0)

        result = service.sincronizar_todos_pendentes()

        self.assertEqual(3, len(client.payloads))
        self.assertEqual(1, result["failed"])
        self.assertEqual(["A"], repository.ids)

    def test_rejects_a_second_local_execution(self) -> None:
        service = SincronizacaoService(FakeRepository([]), FakeClient([]))
        self.assertTrue(service._lock.acquire(blocking=False))
        try:
            with self.assertRaises(SincronizacaoEmAndamentoError):
                service.sincronizar_todos_pendentes()
        finally:
            service._lock.release()


class SincronizacaoRepositoryTests(unittest.TestCase):
    def test_payload_keeps_mixed_class_surplus_and_leading_zero_code_as_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = SQLiteDatabase(Path(temporary) / "sync.db")
            database.initialize()
            with database.connection(immediate=True) as connection:
                collaborator = connection.execute(
                    "INSERT INTO colaboradores(matricula, nome, turno) VALUES ('0001', 'OPERADOR', 'T1') RETURNING id"
                ).fetchone()[0]
                pallet = connection.execute(
                    """
                    INSERT INTO pallets(code, public_id, source_filename, status, conference_status,
                                        finished_at, updated_at, import_operation, import_origin,
                                        created_by_collaborator_id)
                    VALUES ('PAL-1', 'CONF-001', 'wms.csv', 'COMPLETED', 'FINALIZADA',
                            '2026-01-01T10:00:00+00:00', '2026-01-01T10:00:00+00:00',
                            'DIGITAL', 'WMS', ?)
                    RETURNING id
                    """,
                    (collaborator,),
                ).fetchone()[0]
                connection.execute("INSERT INTO expected_cartons(pallet_id, code, ds_classe) VALUES (?, '00009999990462163048', 'A')", (pallet,))
                connection.execute("INSERT INTO expected_cartons(pallet_id, code, ds_classe) VALUES (?, '0002', 'B')", (pallet,))
                attempt = connection.execute(
                    "INSERT INTO conference_attempts(pallet_id, attempt_number, status) VALUES (?, 1, 'COMPLETED') RETURNING id", (pallet,)
                ).fetchone()[0]
                scan = connection.execute(
                    "INSERT INTO scan_events(pallet_id, scanned_code, classification, attempt_id, collaborator_id) VALUES (?, '00009999990462163048', 'EXTRA', ?, ?) RETURNING id",
                    (pallet, attempt, collaborator),
                ).fetchone()[0]
                unexpected = connection.execute(
                    "INSERT INTO unexpected_cartons(pallet_id, attempt_id, code) VALUES (?, ?, '00009999990462163048') RETURNING id", (pallet, attempt)
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO surplus_events(pallet_id, attempt_id, unexpected_carton_id, scan_event_id,
                                               code, scanned_at, scan_order, treatment_status,
                                               corrected_by_collaborator_id, corrected_by_registration,
                                               corrected_by_name, corrected_at, treatment_method)
                    VALUES (?, ?, ?, ?, '00009999990462163048', '2026-01-01T10:00:00+00:00', 1, 'CORRIGIDA',
                            ?, '0001', 'OPERADOR', '2026-01-01T10:05:00+00:00', 'REBIPAGEM')
                    """,
                    (pallet, attempt, unexpected, scan, collaborator),
                )
            repository = SincronizacaoRepository(database)
            self.assertEqual(["CONF-001"], repository.pending_conference_ids())
            payload = repository.build_payload(["CONF-001"])

        conference = payload["tables"]["CONFERENCIAS"][0]
        expected = next(item for item in payload["tables"]["CAIXAS_CONFERENCIA"] if item["TIPO_ITEM"] == "ESPERADA")
        surplus = next(item for item in payload["tables"]["CAIXAS_CONFERENCIA"] if item["TIPO_ITEM"] == "SOBRA")
        self.assertEqual("MISTO", conference["CLASSE_PALETE"])
        self.assertEqual("00009999990462163048", expected["CODE"])
        self.assertEqual("CORRIGIDA", surplus["TREATMENT_STATUS"])
        self.assertEqual("0001", surplus["CORRECTED_BY_REGISTRATION"])
