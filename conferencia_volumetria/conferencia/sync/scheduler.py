from __future__ import annotations

import logging
import threading

from conferencia.sync.service import SyncService


class SyncScheduler:
    """One stoppable daemon loop; the browser never schedules synchronization."""

    def __init__(self, service: SyncService, *, interval_seconds: float = 30 * 60) -> None:
        self.service = service
        self.interval_seconds = max(1.0, float(interval_seconds))
        self._stop_event = threading.Event()
        self._start_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.logger = logging.getLogger("conferencia.sync.scheduler")

    def start(self) -> bool:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="conference-sync-scheduler",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self, timeout_seconds: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout_seconds))

    def _run(self) -> None:
        try:
            self.service.prepare()
        except Exception as error:
            self.logger.warning(
                "sync_startup_check_failed error_type=%s", type(error).__name__
            )
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self.service.sync_all_pending(trigger="automatic")
            except Exception as error:
                self.logger.error(
                    "sync_cycle_failed error_type=%s", type(error).__name__
                )
