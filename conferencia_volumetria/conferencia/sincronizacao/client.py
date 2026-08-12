from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class GoogleSyncError(RuntimeError):
    """Failure that must leave the local conference pending."""


@dataclass(frozen=True, slots=True)
class GoogleSheetsClient:
    url: str
    token: str
    connect_timeout_seconds: float
    read_timeout_seconds: float

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def send_batch(self, payload: dict[str, object]) -> dict[str, object]:
        if not self.configured:
            raise GoogleSyncError("A sincronizacao com Google Sheets nao esta configurada.")
        body = json.dumps(
            {"token": self.token, **payload}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        request = Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=max(self.connect_timeout_seconds, self.read_timeout_seconds)) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            raise GoogleSyncError(f"O Google Sheets respondeu HTTP {error.code}.") from error
        except (URLError, TimeoutError, OSError) as error:
            raise GoogleSyncError("Nao foi possivel conectar ao Google Sheets.") from error
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise GoogleSyncError("O Google Sheets retornou uma resposta invalida.") from error
        if not isinstance(data, dict) or data.get("success") is not True:
            message = data.get("error") if isinstance(data, dict) else None
            raise GoogleSyncError(str(message or "O Google Sheets recusou a sincronizacao."))
        return data
