from __future__ import annotations

import sqlite3

from conferencia.infrastructure.settings import AppSettings
from conferencia.repositories.colaborador_repository import ColaboradorRepository
from conferencia.services.conference_service import ConflictError, ValidationError


class AcessoService:
    SHIFTS = frozenset({"T1", "T2", "T3", "ADM"})
    def __init__(
        self,
        repository: ColaboradorRepository,
        settings: AppSettings | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings or AppSettings()

    def authorize(self, registration: object) -> dict[str, int | str]:
        normalized = self._registration(registration)
        row = self.repository.active_by_registration(normalized)
        if row is None:
            raise ValidationError(
                "Matrícula não encontrada ou sem autorização para acessar o sistema."
            )
        return {"id": row["id"], "matricula": row["matricula"], "nome": row["nome"], "turno": row["turno"]}

    def quick_register(self, matricula: object, nome: object, turno: object) -> dict[str, int | str]:
        registration = self._registration(matricula)
        normalized_name = self._name(nome)
        normalized_shift = self._shift(turno)
        if self.repository.by_registration(registration) is not None:
            raise ConflictError(
                "Esta matrícula já está cadastrada.",
                code="MATRICULA_JA_CADASTRADA",
            )
        try:
            return self.repository.create(registration, normalized_name, normalized_shift)
        except sqlite3.IntegrityError as error:
            raise ConflictError(
                "Esta matrícula já está cadastrada.",
                code="MATRICULA_JA_CADASTRADA",
            ) from error

    def collaborator(self, matricula: object) -> dict[str, int | str]:
        registration = self._registration(matricula)
        row = self.repository.by_registration(registration)
        if row is None:
            raise ValidationError("Colaborador não encontrado.")
        return {"id": row["id"], "matricula": row["matricula"], "nome": row["nome"], "turno": row["turno"] or "ADM"}

    def update_collaborator(self, matricula: object, nome: object, turno: object) -> dict[str, int | str]:
        registration = self._registration(matricula)
        updated = self.repository.update(registration, self._name(nome), self._shift(turno))
        if updated is None:
            raise ValidationError("Colaborador não encontrado.")
        return updated

    @staticmethod
    def _name(value: object) -> str:
        normalized_name = " ".join(value.split()).upper() if isinstance(value, str) else ""
        if not normalized_name:
            raise ValidationError("Informe o nome do colaborador.")
        return normalized_name

    def _shift(self, value: object) -> str:
        shift = value.strip().upper() if isinstance(value, str) else ""
        if shift not in self.SHIFTS:
            raise ValidationError("Selecione um turno válido: T1, T2, T3 ou ADM.")
        return shift

    def _registration(self, value: object) -> str:
        registration = value.strip() if isinstance(value, str) else ""
        if not registration.isdigit():
            raise ValidationError("Informe uma matrícula numérica válida.")
        if not (
            self.settings.collaborator_registration_min_length
            <= len(registration)
            <= self.settings.collaborator_registration_max_length
        ):
            raise ValidationError(
                "A matrícula deve possuir entre "
                f"{self.settings.collaborator_registration_min_length} e "
                f"{self.settings.collaborator_registration_max_length} dígitos."
            )
        return registration
