from __future__ import annotations

import secrets
import time
from threading import RLock
from typing import TypedDict


SESSION_COLLABORATOR_ID = "colaborador_id"
SESSION_COLLABORATOR_REGISTRATION = "colaborador_matricula"
SESSION_COLLABORATOR_NAME = "colaborador_nome"
SESSION_COLLABORATOR_SHIFT = "colaborador_turno"


class SessionData(TypedDict):
    colaborador_id: int
    colaborador_matricula: str
    colaborador_nome: str
    colaborador_turno: str
    data_hora_acesso: float
    last: float


class SessionManager:
    def __init__(self, timeout_seconds: int = 1800) -> None:
        self.timeout_seconds = timeout_seconds
        self.sessions: dict[str, SessionData] = {}
        self._lock = RLock()

    def create(self, collaborator: dict) -> str:
        now = time.time()
        token = secrets.token_urlsafe(32)
        with self._lock:
            self.sessions[token] = {
                SESSION_COLLABORATOR_ID: int(collaborator["id"]),
                SESSION_COLLABORATOR_REGISTRATION: str(collaborator["matricula"]),
                SESSION_COLLABORATOR_NAME: str(collaborator["nome"]),
                SESSION_COLLABORATOR_SHIFT: str(collaborator["turno"]),
                "data_hora_acesso": now,
                "last": now,
            }
        return token

    def get(self, token: str | None) -> SessionData | None:
        with self._lock:
            data = self.sessions.get(token or "")
            if data is None:
                return None
            if time.time() - data["last"] > self.timeout_seconds:
                self.sessions.pop(token or "", None)
                return None
            data["last"] = time.time()
            return data

    def destroy(self, token: str | None) -> None:
        with self._lock:
            self.sessions.pop(token or "", None)
