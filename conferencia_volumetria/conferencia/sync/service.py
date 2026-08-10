from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import islice
from time import monotonic

from conferencia.sync.client import AppsScriptClient, AppsScriptError
from conferencia.sync.models import RemoteResult, SyncRunResult
from conferencia.sync.repository import SyncRepository


class SyncService:
    """Single synchronization path shared by the HTTP action and scheduler."""

    _PERMANENT_REMOTE_CODES = {
        "BATCH_TOO_LARGE",
        "CONFERENCE_ID_MISMATCH",
        "CONFERENCE_NOT_FINISHED",
        "DUPLICATE_CHILD",
        "DUPLICATE_CONFERENCE",
        "EXISTING_DATA_MISMATCH",
        "INVALID_CHILD_ID",
        "INVALID_CONFERENCE_ID",
        "INVALID_PAYLOAD",
        "SHEET_SCHEMA_MISMATCH",
        "UNAUTHORIZED",
    }

    def __init__(
        self,
        repository: SyncRepository,
        client: AppsScriptClient,
        *,
        batch_size: int = 10,
        max_attempts: int = 3,
        retry_delay_seconds: float = 2.0,
        reservation_timeout_seconds: int = 15 * 60,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repository = repository
        self.client = client
        self.batch_size = min(100, max(1, int(batch_size)))
        self.max_attempts = max(1, int(max_attempts))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        self.reservation_timeout_seconds = max(1, int(reservation_timeout_seconds))
        self._sleep = sleeper
        self._run_lock = threading.Lock()
        self._health_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._running = False
        self._health = "unknown" if client.configured else "not_configured"
        self._last_health_at: str | None = None
        self.logger = logging.getLogger("conferencia.sync")

    def prepare(self) -> None:
        """Recover interrupted work and check Google without blocking server startup."""
        recovered = self.repository.recover_stale(self.reservation_timeout_seconds)
        if recovered:
            self.logger.info("sync_recovered_stale count=%s", recovered)
        self.refresh_health()

    def refresh_health(self) -> bool:
        with self._health_lock:
            if not self.client.configured:
                self._set_health("not_configured")
                self.logger.info("sync_health status=not_configured")
                return False
            try:
                self._with_retry(self.client.health_check, operation="health")
            except Exception as error:
                self._set_health("unavailable")
                self.logger.warning(
                    "sync_health status=unavailable error_type=%s error=%s",
                    type(error).__name__,
                    self._safe_error(error),
                )
                return False
            self._set_health("healthy")
            self.logger.info("sync_health status=healthy")
            return True

    def status(self) -> dict[str, object]:
        summary = self.repository.summary()
        summary.pop("last_error", None)
        with self._state_lock:
            runtime = {
                "configured": self.client.configured,
                "health": self._health,
                "running": self._running,
                "last_health_at": self._last_health_at,
            }
        return {**summary, **runtime}

    def sync_all_pending(self, *, trigger: str = "manual") -> SyncRunResult:
        if not self._run_lock.acquire(blocking=False):
            return SyncRunResult(
                busy=True,
                configured=self.client.configured,
                message="Uma sincronização já está em andamento.",
            )
        started = monotonic()
        self._set_running(True)
        result = SyncRunResult(configured=self.client.configured)
        try:
            recovered = self.repository.recover_stale(self.reservation_timeout_seconds)
            if recovered:
                self.logger.info("sync_recovered_stale count=%s", recovered)
            pending_snapshot = self.repository.pending_ids()
            result.found = len(pending_snapshot)
            self.logger.info(
                "sync_started trigger=%s found=%s batch_size=%s",
                trigger,
                result.found,
                self.batch_size,
            )
            if not pending_snapshot:
                result.message = "Não há conferências pendentes para sincronizar."
                return result
            if not self.client.configured:
                result.failed = result.found
                result.failed_ids = pending_snapshot
                result.message = "Sincronização Google ainda não configurada."
                self._set_health("not_configured")
                return result
            if not self.refresh_health():
                result.failed = result.found
                result.failed_ids = pending_snapshot
                result.message = "Google Sheets indisponível. As conferências continuam pendentes."
                return result

            iterator = iter(pending_snapshot)
            while batch_ids := list(islice(iterator, self.batch_size)):
                reservations = self.repository.reserve(batch_ids)
                if not reservations:
                    continue
                result.batches += 1
                reserved_ids = [item.public_id for item in reservations]
                try:
                    payloads = self.repository.payloads(reservations)
                    remote_results = self._send_with_retry(payloads, reserved_ids)
                    remote_degraded = self._should_retry_results(
                        remote_results, reserved_ids
                    )
                    inserted, already_exists, failed = self._classify_results(
                        reserved_ids, remote_results
                    )
                    confirmed_inserted, local_insert_failures = self._confirm_locally(
                        inserted, trigger
                    )
                    confirmed_existing, local_existing_failures = self._confirm_locally(
                        already_exists, trigger
                    )
                    failed = list(
                        dict.fromkeys(
                            [*failed, *local_insert_failures, *local_existing_failures]
                        )
                    )
                    if failed:
                        self.repository.release(
                            failed,
                            "Resposta remota não confirmou a conferência.",
                        )
                        self.logger.warning(
                            "sync_records_pending trigger=%s ids=%s reason=remote_not_confirmed",
                            trigger,
                            ",".join(failed),
                        )
                    result.synchronized += len(confirmed_inserted)
                    result.already_exists += len(confirmed_existing)
                    result.failed += len(failed)
                    result.failed_ids.extend(failed)
                    if remote_degraded:
                        self._set_health("unavailable")
                except Exception as error:
                    self.repository.release(reserved_ids, self._safe_error(error))
                    if self._is_remote_availability_failure(error):
                        self._set_health("unavailable")
                    result.failed += len(reserved_ids)
                    result.failed_ids.extend(reserved_ids)
                    self.logger.warning(
                        "sync_batch_failed trigger=%s size=%s ids=%s error_type=%s error=%s",
                        trigger,
                        len(reserved_ids),
                        ",".join(reserved_ids),
                        type(error).__name__,
                        self._safe_error(error),
                    )

            completed = result.synchronized + result.already_exists
            if result.failed and completed:
                result.message = (
                    f"Sincronização parcial: {completed} confirmada(s) e "
                    f"{result.failed} mantida(s) como pendente(s)."
                )
            elif result.failed:
                result.message = "Não foi possível sincronizar. Os registros continuam pendentes."
            else:
                result.message = f"{completed} conferência(s) sincronizada(s) com sucesso."
            return result
        finally:
            self._set_running(False)
            self._run_lock.release()
            self.logger.info(
                "sync_finished trigger=%s found=%s synchronized=%s already_exists=%s failed=%s batches=%s duration_ms=%s",
                trigger,
                result.found,
                result.synchronized,
                result.already_exists,
                result.failed,
                result.batches,
                round((monotonic() - started) * 1000),
            )

    def _with_retry(self, action: Callable[[], object], *, operation: str):
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return action()
            except AppsScriptError as error:
                last_error = error
                retryable = error.retryable
            except (TimeoutError, ConnectionError, OSError) as error:
                last_error = error
                retryable = True
            if not retryable or attempt >= self.max_attempts:
                assert last_error is not None
                raise last_error
            delay = self.retry_delay_seconds * (2 ** (attempt - 1))
            self.logger.warning(
                "sync_retry operation=%s attempt=%s max_attempts=%s delay_seconds=%s error_type=%s",
                operation,
                attempt,
                self.max_attempts,
                delay,
                type(last_error).__name__,
            )
            self._sleep(delay)
        raise AssertionError("retry loop exhausted")

    def _send_with_retry(
        self, payloads: list[dict[str, object]], expected_ids: list[str]
    ) -> list[dict[str, object]]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                results = self.client.send_batch(payloads)
            except AppsScriptError as error:
                last_error = error
                retryable = error.retryable
            except (TimeoutError, ConnectionError, OSError) as error:
                last_error = error
                retryable = True
            else:
                if not self._should_retry_results(results, expected_ids):
                    return results
                if attempt >= self.max_attempts:
                    self.logger.warning(
                        "sync_invalid_response operation=send attempt=%s ids=%s",
                        attempt,
                        ",".join(expected_ids),
                    )
                    return results
                last_error = AppsScriptError(
                    "O Google Sheets informou uma falha temporária.",
                    retryable=True,
                    code="REMOTE_ITEM_ERROR",
                )
                retryable = True

            if not retryable or attempt >= self.max_attempts:
                assert last_error is not None
                raise last_error
            delay = self.retry_delay_seconds * (2 ** (attempt - 1))
            self.logger.warning(
                "sync_retry operation=send attempt=%s max_attempts=%s delay_seconds=%s error_type=%s",
                attempt,
                self.max_attempts,
                delay,
                type(last_error).__name__,
            )
            self._sleep(delay)
        raise AssertionError("send retry loop exhausted")

    @classmethod
    def _should_retry_results(cls, results: object, expected_ids: list[str]) -> bool:
        if not isinstance(results, list):
            return True
        expected = set(expected_ids)
        seen: set[str] = set()
        for item in results:
            if not isinstance(item, dict):
                return True
            public_id = item.get("id_conferencia")
            if not isinstance(public_id, str) or public_id not in expected or public_id in seen:
                return True
            seen.add(public_id)
            status = item.get("status")
            if status not in {member.value for member in RemoteResult}:
                return True
            if status != RemoteResult.ERROR.value:
                continue
            code = str(item.get("error_code") or "").upper()
            if code not in cls._PERMANENT_REMOTE_CODES:
                return True
        return seen != expected

    def _confirm_locally(
        self, public_ids: list[str], trigger: str
    ) -> tuple[list[str], list[str]]:
        confirmed: list[str] = []
        failed: list[str] = []
        for public_id in public_ids:
            try:
                updated = self.repository.mark_synchronized([public_id])
            except Exception as error:
                updated = 0
                self.logger.warning(
                    "sync_local_confirmation_failed trigger=%s id=%s error_type=%s error=%s",
                    trigger,
                    public_id,
                    type(error).__name__,
                    self._safe_error(error),
                )
            if updated == 1:
                confirmed.append(public_id)
            else:
                failed.append(public_id)
        return confirmed, failed

    @staticmethod
    def _classify_results(
        expected_ids: list[str], remote_results: object
    ) -> tuple[list[str], list[str], list[str]]:
        if not isinstance(remote_results, list):
            raise AppsScriptError(
                "Resposta remota inválida.", retryable=True, code="INVALID_RESPONSE"
            )
        expected = set(expected_ids)
        by_id: dict[str, list[str]] = {}
        for item in remote_results:
            if not isinstance(item, dict):
                continue
            public_id = item.get("id_conferencia")
            status = item.get("status")
            if not isinstance(public_id, str) or public_id not in expected:
                continue
            by_id.setdefault(public_id, []).append(str(status))

        inserted: list[str] = []
        already_exists: list[str] = []
        failed: list[str] = []
        for public_id in expected_ids:
            values = by_id.get(public_id, [])
            if values == [RemoteResult.INSERTED.value]:
                inserted.append(public_id)
            elif values == [RemoteResult.ALREADY_EXISTS.value]:
                already_exists.append(public_id)
            else:
                failed.append(public_id)
        return inserted, already_exists, failed

    def _set_health(self, health: str) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self._state_lock:
            self._health = health
            self._last_health_at = now

    def _set_running(self, running: bool) -> None:
        with self._state_lock:
            self._running = running

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        if isinstance(error, AppsScriptError):
            return str(error)[:500]
        if isinstance(error, (TimeoutError, ConnectionError, OSError)):
            return "Falha temporária de conexão com o serviço remoto."
        return f"Falha interna de sincronização ({type(error).__name__})."

    @staticmethod
    def _is_remote_availability_failure(error: BaseException) -> bool:
        if isinstance(error, AppsScriptError):
            return error.retryable
        return isinstance(error, (TimeoutError, ConnectionError, OSError))
