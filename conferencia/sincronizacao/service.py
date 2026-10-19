from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from conferencia.sincronizacao.client import GoogleSheetsClient, GoogleSyncError
from conferencia.sincronizacao.repository import SincronizacaoRepository


class SincronizacaoEmAndamentoError(RuntimeError):
    pass


@dataclass(slots=True)
class SincronizacaoService:
    repository: SincronizacaoRepository
    client: GoogleSheetsClient
    batch_size: int = 10
    max_attempts: int = 3
    retry_delay_seconds: float = 1.0

    _lock = threading.Lock()

    def sincronizar_todos_pendentes(self) -> dict[str, int | str]:
        if not self._lock.acquire(blocking=False):
            raise SincronizacaoEmAndamentoError("Uma sincronizacao ja esta em andamento.")
        try:
            pending = self.repository.pending_conference_ids()
            result: dict[str, int | str] = {
                "found": len(pending), "synchronized": 0, "already_existing": 0,
                "failed": 0, "error_message": "", "message": "Nenhuma conferencia pendente de sincronizacao.",
            }
            for offset in range(0, len(pending), max(1, self.batch_size)):
                batch = pending[offset:offset + max(1, self.batch_size)]
                try:
                    response = self._send_with_retry(self.repository.build_payload(batch))
                    confirmed_ids = response.get("confirmed_conference_ids", [])
                    existing_ids = response.get("already_existing_conference_ids", [])
                    if not isinstance(confirmed_ids, list) or not isinstance(existing_ids, list):
                        raise GoogleSyncError("O Google Sheets retornou uma confirmacao invalida.")
                    confirmed = {str(item) for item in confirmed_ids}
                    if not confirmed.issubset(set(batch)):
                        raise GoogleSyncError("O Google Sheets confirmou uma conferencia fora do lote enviado.")
                    now = self._now()
                    self.repository.mark_synchronized(confirmed, now)
                    missing = set(batch) - confirmed
                    if missing:
                        self.repository.mark_failed(missing, "Confirmacao incompleta do Google Sheets.", now)
                        result["error_message"] = "O Google Sheets confirmou somente parte do lote."
                    result["synchronized"] += len(confirmed)
                    result["already_existing"] += len(existing_ids)
                    result["failed"] += len(missing)
                except GoogleSyncError as error:
                    self.repository.mark_failed(batch, str(error), self._now())
                    result["failed"] += len(batch)
                    result["error_message"] = str(error)
            if result["found"]:
                result["message"] = "Sincronizacao concluida." if not result["failed"] else "Alguns dados permaneceram salvos localmente e pendentes para nova tentativa."
            return result
        finally:
            self._lock.release()

    def _send_with_retry(self, payload: dict[str, object]) -> dict[str, object]:
        error: GoogleSyncError | None = None
        for attempt in range(max(1, self.max_attempts)):
            try:
                return self.client.send_batch(payload)
            except GoogleSyncError as caught:
                error = caught
                if attempt + 1 < max(1, self.max_attempts):
                    time.sleep(max(0.0, self.retry_delay_seconds))
        raise error or GoogleSyncError("Falha na sincronizacao.")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
