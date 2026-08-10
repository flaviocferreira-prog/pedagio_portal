from __future__ import annotations

import unittest
from collections import deque
from unittest.mock import patch

from conferencia.sync.scheduler import SyncScheduler


class RecordingSyncService:
    def __init__(self, cycle_events: list[object] | None = None) -> None:
        self.prepare_calls = 0
        self.triggers: list[str] = []
        self.cycle_events = deque(cycle_events or [])

    def prepare(self) -> None:
        self.prepare_calls += 1

    def sync_all_pending(self, *, trigger: str = "manual") -> object:
        self.triggers.append(trigger)
        if self.cycle_events:
            event = self.cycle_events.popleft()
            if isinstance(event, BaseException):
                raise event
            return event
        return object()


class ScriptedStopEvent:
    def __init__(self, wait_results: list[bool]) -> None:
        self.wait_results = deque(wait_results)
        self.wait_calls: list[float] = []
        self.set_calls = 0
        self.clear_calls = 0

    def wait(self, timeout: float) -> bool:
        self.wait_calls.append(timeout)
        return self.wait_results.popleft() if self.wait_results else True

    def set(self) -> None:
        self.set_calls += 1

    def clear(self) -> None:
        self.clear_calls += 1


class FakeThread:
    instances: list["FakeThread"] = []

    def __init__(self, *, target: object, name: str, daemon: bool) -> None:
        self.target = target
        self.name = name
        self.daemon = daemon
        self.start_calls = 0
        self.join_calls: list[float] = []
        self.alive = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.start_calls += 1
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float) -> None:
        self.join_calls.append(timeout)
        self.alive = False


class SyncSchedulerTests(unittest.TestCase):
    def test_cycle_calls_the_same_service_with_automatic_trigger(self) -> None:
        service = RecordingSyncService()
        scheduler = SyncScheduler(service, interval_seconds=1800)
        stop_event = ScriptedStopEvent([False, True])
        scheduler._stop_event = stop_event

        scheduler._run()

        self.assertIs(service, scheduler.service)
        self.assertEqual(1, service.prepare_calls)
        self.assertEqual(["automatic"], service.triggers)
        self.assertEqual([1800.0, 1800.0], stop_event.wait_calls)

    def test_cycle_error_is_logged_and_does_not_kill_the_next_cycle(self) -> None:
        service = RecordingSyncService([RuntimeError("first cycle failed"), object()])
        scheduler = SyncScheduler(service, interval_seconds=30)
        stop_event = ScriptedStopEvent([False, False, True])
        scheduler._stop_event = stop_event

        with self.assertLogs("conferencia.sync.scheduler", level="ERROR") as captured:
            scheduler._run()

        self.assertEqual(1, service.prepare_calls)
        self.assertEqual(["automatic", "automatic"], service.triggers)
        self.assertEqual([30.0, 30.0, 30.0], stop_event.wait_calls)
        self.assertIn("sync_cycle_failed", "\n".join(captured.output))

    def test_start_is_idempotent_and_stop_joins_the_single_daemon_thread(self) -> None:
        service = RecordingSyncService()
        scheduler = SyncScheduler(service, interval_seconds=30)
        fake_stop = ScriptedStopEvent([])
        scheduler._stop_event = fake_stop
        FakeThread.instances.clear()

        with patch("conferencia.sync.scheduler.threading.Thread", FakeThread):
            self.assertTrue(scheduler.start())
            self.assertFalse(scheduler.start())
            scheduler.stop(timeout_seconds=2.5)

        self.assertEqual(1, len(FakeThread.instances))
        thread = FakeThread.instances[0]
        self.assertEqual(1, thread.start_calls)
        self.assertEqual("conference-sync-scheduler", thread.name)
        self.assertTrue(thread.daemon)
        self.assertEqual([2.5], thread.join_calls)
        self.assertEqual(1, fake_stop.clear_calls)
        self.assertEqual(1, fake_stop.set_calls)


if __name__ == "__main__":
    unittest.main()
