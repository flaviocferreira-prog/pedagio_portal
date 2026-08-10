from __future__ import annotations

import threading
import unittest
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from conferencia.sync.client import AppsScriptError
from conferencia.sync.models import SyncReservation
from conferencia.sync.service import SyncService


class FakeSyncRepository:
    def __init__(self, public_ids: list[str]) -> None:
        self.order = list(public_ids)
        self.states = {public_id: "PENDENTE" for public_id in public_ids}
        self.reserve_calls: list[list[str]] = []
        self.payload_calls: list[list[str]] = []
        self.mark_calls: list[list[str]] = []
        self.release_calls: list[tuple[list[str], str]] = []
        self.recover_calls: list[int] = []

    def pending_ids(self) -> list[str]:
        return [public_id for public_id in self.order if self.states[public_id] == "PENDENTE"]

    def reserve(self, public_ids: list[str]) -> list[SyncReservation]:
        self.reserve_calls.append(list(public_ids))
        reservations: list[SyncReservation] = []
        for public_id in public_ids:
            if self.states.get(public_id) != "PENDENTE":
                continue
            self.states[public_id] = "SINCRONIZANDO"
            reservations.append(SyncReservation(self.order.index(public_id) + 1, public_id))
        return reservations

    def payloads(self, reservations: list[SyncReservation]) -> list[dict[str, object]]:
        public_ids = [reservation.public_id for reservation in reservations]
        self.payload_calls.append(public_ids)
        return [
            {
                "id_conferencia": public_id,
                "conference": {"id_conferencia": public_id},
                "items": [],
                "scans": [],
            }
            for public_id in public_ids
        ]

    def mark_synchronized(self, public_ids: list[str]) -> int:
        values = list(public_ids)
        self.mark_calls.append(values)
        updated = 0
        for public_id in values:
            if self.states.get(public_id) == "SINCRONIZANDO":
                self.states[public_id] = "SINCRONIZADA"
                updated += 1
        return updated

    def release(self, public_ids: list[str], error: str) -> int:
        values = list(public_ids)
        self.release_calls.append((values, error))
        updated = 0
        for public_id in values:
            if self.states.get(public_id) == "SINCRONIZANDO":
                self.states[public_id] = "PENDENTE"
                updated += 1
        return updated

    def recover_stale(self, timeout_seconds: int) -> int:
        self.recover_calls.append(timeout_seconds)
        return 0

    def summary(self) -> dict[str, int | str | None]:
        return {
            "pending": sum(value == "PENDENTE" for value in self.states.values()),
            "syncing": sum(value == "SINCRONIZANDO" for value in self.states.values()),
            "synchronized": sum(value == "SINCRONIZADA" for value in self.states.values()),
            "last_error": None,
        }


class ScriptedAppsScriptClient:
    def __init__(
        self,
        send_events: list[
            list[dict[str, object]]
            | BaseException
            | Callable[[list[dict[str, object]]], list[dict[str, object]]]
        ]
        | None = None,
        *,
        configured: bool = True,
    ) -> None:
        self.configured = configured
        self.send_events = deque(send_events or [])
        self.health_calls = 0
        self.send_calls: list[list[dict[str, object]]] = []

    def health_check(self) -> dict[str, object]:
        self.health_calls += 1
        return {"ok": True, "service": "apps-script", "sheets": True}

    def send_batch(self, payloads: list[dict[str, object]]) -> list[dict[str, object]]:
        self.send_calls.append(payloads)
        if self.send_events:
            event = self.send_events.popleft()
            if isinstance(event, BaseException):
                raise event
            if callable(event):
                return event(payloads)
            return event
        return [
            {"id_conferencia": payload["id_conferencia"], "status": "INSERTED"}
            for payload in payloads
        ]


class SyncServiceTests(unittest.TestCase):
    @staticmethod
    def _service(
        repository: FakeSyncRepository,
        client: ScriptedAppsScriptClient,
        *,
        batch_size: int = 10,
        max_attempts: int = 3,
        retry_delay_seconds: float = 2.0,
        sleeps: list[float] | None = None,
    ) -> SyncService:
        recorded_sleeps = sleeps if sleeps is not None else []
        return SyncService(
            repository,
            client,
            batch_size=batch_size,
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
            sleeper=recorded_sleeps.append,
        )

    def test_zero_pending_returns_without_health_check_or_send(self) -> None:
        repository = FakeSyncRepository([])
        client = ScriptedAppsScriptClient()

        result = self._service(repository, client).sync_all_pending()

        self.assertEqual(0, result.found)
        self.assertEqual(0, result.batches)
        self.assertEqual(0, result.failed)
        self.assertEqual(0, client.health_calls)
        self.assertEqual([], client.send_calls)
        self.assertEqual([900], repository.recover_calls)
        self.assertIn("pendentes", result.message)
        public_status = self._service(repository, client).status()
        self.assertNotIn("last_error", public_status)
        self.assertNotIn("last_health_error", public_status)

    def test_one_pending_is_reserved_sent_and_confirmed(self) -> None:
        repository = FakeSyncRepository(["CONF-001"])
        client = ScriptedAppsScriptClient()

        result = self._service(repository, client, max_attempts=1).sync_all_pending()

        self.assertEqual(1, result.found)
        self.assertEqual(1, result.synchronized)
        self.assertEqual(1, result.batches)
        self.assertEqual("SINCRONIZADA", repository.states["CONF-001"])
        self.assertEqual([["CONF-001"]], repository.reserve_calls)
        self.assertEqual(1, client.health_calls)
        self.assertEqual("CONF-001", client.send_calls[0][0]["id_conferencia"])

    def test_multiple_pending_are_all_processed_in_controlled_batches(self) -> None:
        public_ids = [f"CONF-{index:03d}" for index in range(1, 6)]
        repository = FakeSyncRepository(public_ids)
        client = ScriptedAppsScriptClient()

        result = self._service(repository, client, batch_size=2).sync_all_pending()

        self.assertEqual(5, result.found)
        self.assertEqual(5, result.synchronized)
        self.assertEqual(3, result.batches)
        self.assertEqual([2, 2, 1], [len(batch) for batch in client.send_calls])
        self.assertEqual(
            [["CONF-001", "CONF-002"], ["CONF-003", "CONF-004"], ["CONF-005"]],
            repository.reserve_calls,
        )
        self.assertTrue(all(state == "SINCRONIZADA" for state in repository.states.values()))

    def test_batch_size_is_capped_to_apps_script_limit(self) -> None:
        public_ids = [f"CONF-{index:03d}" for index in range(205)]
        repository = FakeSyncRepository(public_ids)
        client = ScriptedAppsScriptClient()

        result = self._service(repository, client, batch_size=1000).sync_all_pending()

        self.assertEqual(205, result.synchronized)
        self.assertEqual([100, 100, 5], [len(batch) for batch in client.send_calls])

    def test_retry_uses_exponential_backoff_then_succeeds(self) -> None:
        repository = FakeSyncRepository(["CONF-RETRY"])
        client = ScriptedAppsScriptClient(
            [
                AppsScriptError("temporÃ¡rio 1", retryable=True),
                TimeoutError("temporÃ¡rio 2"),
                [{"id_conferencia": "CONF-RETRY", "status": "INSERTED"}],
            ]
        )
        sleeps: list[float] = []

        result = self._service(
            repository,
            client,
            max_attempts=3,
            retry_delay_seconds=2,
            sleeps=sleeps,
        ).sync_all_pending()

        self.assertEqual(1, result.synchronized)
        self.assertEqual(3, len(client.send_calls))
        self.assertEqual([2.0, 4.0], sleeps)
        self.assertEqual("SINCRONIZADA", repository.states["CONF-RETRY"])

    def test_exhausted_retries_release_the_batch_as_pending(self) -> None:
        repository = FakeSyncRepository(["CONF-FAIL"])
        client = ScriptedAppsScriptClient(
            [
                AppsScriptError("indisponÃ­vel", retryable=True),
                AppsScriptError("indisponÃ­vel", retryable=True),
                AppsScriptError("indisponÃ­vel", retryable=True),
            ]
        )
        sleeps: list[float] = []

        service = self._service(repository, client, sleeps=sleeps)
        result = service.sync_all_pending()

        self.assertEqual(1, result.failed)
        self.assertEqual(["CONF-FAIL"], result.failed_ids)
        self.assertEqual("PENDENTE", repository.states["CONF-FAIL"])
        self.assertEqual(3, len(client.send_calls))
        self.assertEqual([2.0, 4.0], sleeps)
        self.assertEqual(["CONF-FAIL"], repository.release_calls[0][0])
        self.assertEqual("unavailable", service.status()["health"])

    def test_already_exists_is_a_positive_idempotent_confirmation(self) -> None:
        repository = FakeSyncRepository(["CONF-EXISTS"])
        client = ScriptedAppsScriptClient(
            [[{"id_conferencia": "CONF-EXISTS", "status": "ALREADY_EXISTS"}]]
        )

        result = self._service(repository, client, max_attempts=1).sync_all_pending()

        self.assertEqual(0, result.synchronized)
        self.assertEqual(1, result.already_exists)
        self.assertEqual(0, result.failed)
        self.assertEqual("SINCRONIZADA", repository.states["CONF-EXISTS"])

    def test_partial_response_confirms_only_explicit_successes(self) -> None:
        repository = FakeSyncRepository(["CONF-A", "CONF-B", "CONF-C"])
        client = ScriptedAppsScriptClient(
            [[
                {"id_conferencia": "CONF-A", "status": "INSERTED"},
                {
                    "id_conferencia": "CONF-C",
                    "status": "ERROR",
                    "error_code": "INVALID_PAYLOAD",
                },
            ]]
        )

        result = self._service(repository, client, max_attempts=1).sync_all_pending()

        self.assertEqual(1, result.synchronized)
        self.assertEqual(2, result.failed)
        self.assertEqual(["CONF-B", "CONF-C"], result.failed_ids)
        self.assertEqual("SINCRONIZADA", repository.states["CONF-A"])
        self.assertEqual("PENDENTE", repository.states["CONF-B"])
        self.assertEqual("PENDENTE", repository.states["CONF-C"])
        self.assertEqual([["CONF-A"]], repository.mark_calls)
        self.assertEqual(["CONF-B", "CONF-C"], repository.release_calls[0][0])

    def test_transient_remote_item_error_retries_the_batch(self) -> None:
        repository = FakeSyncRepository(["CONF-REMOTE-RETRY"])
        client = ScriptedAppsScriptClient(
            [
                [{
                    "id_conferencia": "CONF-REMOTE-RETRY",
                    "status": "ERROR",
                    "error_code": "SHEETS_WRITE_FAILED",
                }],
                [{"id_conferencia": "CONF-REMOTE-RETRY", "status": "INSERTED"}],
            ]
        )
        sleeps: list[float] = []

        result = self._service(repository, client, sleeps=sleeps).sync_all_pending()

        self.assertEqual(1, result.synchronized)
        self.assertEqual(2, len(client.send_calls))
        self.assertEqual([2.0], sleeps)

    def test_duplicate_and_missing_results_are_not_accepted(self) -> None:
        repository = FakeSyncRepository(["CONF-A", "CONF-B"])
        client = ScriptedAppsScriptClient(
            [[
                {"id_conferencia": "CONF-A", "status": "INSERTED"},
                {"id_conferencia": "CONF-A", "status": "INSERTED"},
                {"id_conferencia": "CONF-UNKNOWN", "status": "INSERTED"},
            ]]
        )

        result = self._service(repository, client, max_attempts=1).sync_all_pending()

        self.assertEqual(0, result.synchronized)
        self.assertEqual(2, result.failed)
        self.assertEqual(["CONF-A", "CONF-B"], result.failed_ids)
        self.assertEqual([], repository.mark_calls)
        self.assertEqual(["CONF-A", "CONF-B"], repository.release_calls[0][0])
        self.assertTrue(all(state == "PENDENTE" for state in repository.states.values()))

    def test_missing_result_is_retried_before_final_classification(self) -> None:
        repository = FakeSyncRepository(["CONF-A", "CONF-B"])
        client = ScriptedAppsScriptClient(
            [
                [{"id_conferencia": "CONF-A", "status": "INSERTED"}],
                [
                    {"id_conferencia": "CONF-A", "status": "ALREADY_EXISTS"},
                    {"id_conferencia": "CONF-B", "status": "INSERTED"},
                ],
            ]
        )
        sleeps: list[float] = []

        result = self._service(repository, client, sleeps=sleeps).sync_all_pending()

        self.assertEqual(1, result.synchronized)
        self.assertEqual(1, result.already_exists)
        self.assertEqual(0, result.failed)
        self.assertEqual(2, len(client.send_calls))
        self.assertEqual([2.0], sleeps)

    def test_lost_response_then_already_exists_is_confirmed_without_duplicate(self) -> None:
        repository = FakeSyncRepository(["CONF-LOST"])
        client = ScriptedAppsScriptClient(
            [
                TimeoutError("response lost after remote write"),
                [{"id_conferencia": "CONF-LOST", "status": "ALREADY_EXISTS"}],
            ]
        )

        result = self._service(repository, client).sync_all_pending()

        self.assertEqual(0, result.synchronized)
        self.assertEqual(1, result.already_exists)
        self.assertEqual("SINCRONIZADA", repository.states["CONF-LOST"])
        self.assertEqual(2, len(client.send_calls))

    def test_mixed_retry_reconciles_previous_insert_and_new_success(self) -> None:
        repository = FakeSyncRepository(["CONF-A", "CONF-B"])
        client = ScriptedAppsScriptClient(
            [
                [
                    {"id_conferencia": "CONF-A", "status": "INSERTED"},
                    {
                        "id_conferencia": "CONF-B",
                        "status": "ERROR",
                        "error_code": "SHEETS_WRITE_FAILED",
                    },
                ],
                [
                    {"id_conferencia": "CONF-A", "status": "ALREADY_EXISTS"},
                    {"id_conferencia": "CONF-B", "status": "INSERTED"},
                ],
            ]
        )

        result = self._service(repository, client).sync_all_pending()

        self.assertEqual(1, result.synchronized)
        self.assertEqual(1, result.already_exists)
        self.assertEqual(0, result.failed)
        self.assertEqual(2, len(client.send_calls))

    def test_remote_ack_is_not_reported_when_local_transition_updates_zero_rows(self) -> None:
        repository = FakeSyncRepository(["CONF-LOCAL-FAIL"])
        client = ScriptedAppsScriptClient()
        repository.mark_synchronized = lambda _public_ids: 0  # type: ignore[method-assign]

        result = self._service(repository, client).sync_all_pending()

        self.assertEqual(0, result.synchronized)
        self.assertEqual(1, result.failed)
        self.assertEqual("PENDENTE", repository.states["CONF-LOCAL-FAIL"])

    def test_concurrent_request_is_rejected_without_second_reservation_or_send(self) -> None:
        repository = FakeSyncRepository(["CONF-LOCK"])
        entered = threading.Event()
        release = threading.Event()

        def blocking_success(payloads: list[dict[str, object]]) -> list[dict[str, object]]:
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("test release was not signalled")
            return [{"id_conferencia": "CONF-LOCK", "status": "INSERTED"}]

        client = ScriptedAppsScriptClient([blocking_success])
        service = self._service(repository, client)

        with ThreadPoolExecutor(max_workers=1) as executor:
            first_future = executor.submit(service.sync_all_pending, trigger="automatic")
            self.assertTrue(entered.wait(timeout=2))
            second = service.sync_all_pending(trigger="manual")
            release.set()
            first = first_future.result(timeout=2)

        self.assertTrue(second.busy)
        self.assertEqual(1, first.synchronized)
        self.assertEqual(1, len(repository.reserve_calls))
        self.assertEqual(1, len(client.send_calls))


if __name__ == "__main__":
    unittest.main()
