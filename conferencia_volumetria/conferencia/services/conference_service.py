from __future__ import annotations

import sqlite3
from uuid import uuid4

from conferencia.domain.box_codes import normalize_caixa_estoque
from conferencia.domain.entities import (
    CollaboratorContext,
    ScanClassification,
)
from conferencia.regras_conferencia import mensagem_bloqueio_finalizacao_por_classe
from conferencia.repositories.pallet_repository import (
    ActiveConferenceError,
    AmbiguousBoxCodeError,
    PalletRepository,
    PendingConferenceError,
    RepositoryStateError,
    SurplusCorrectionMismatchError,
    SurplusTreatmentRequiredError,
)


class ApplicationError(Exception):
    code = "APPLICATION_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code or self.code
        self.details = details or {}


class ValidationError(ApplicationError):
    code = "VALIDATION_ERROR"


class PayloadTooLargeError(ValidationError):
    code = "FILE_TOO_LARGE"


class NotFoundError(ApplicationError):
    code = "NOT_FOUND"


class ConflictError(ApplicationError):
    code = "STATE_CONFLICT"


class InvalidScanError(ValidationError):
    pass


class ConferenceService:
    SHIFTS = frozenset({"T1", "T2", "T3", "ADM"})
    def __init__(self, repository: PalletRepository) -> None:
        self.repository = repository

    def active_pallet(self, collaborator: CollaboratorContext) -> dict:
        actor = self._validate_collaborator(collaborator)
        active = self.repository.find_active_by_collaborator(actor.id)
        if active is None:
            latest = self.repository.find_latest_finalized_by_collaborator(actor.id)
            latest_details = self.get_pallet(latest["public_id"]) if latest is not None else None
            if latest_details is not None:
                latest_details["action"] = "already_completed"
                latest_details["message"] = "Este palete ja foi conferido e finalizado."
            return {
                "has_active_conference": False,
                "conference": None,
                "latest_conference": latest_details,
            }
        return {
            "has_active_conference": True,
            "conference": self.get_pallet(active["public_id"]),
            "latest_conference": None,
        }

    def authorize_reconference(
        self, public_id: str, reason: object, collaborator: CollaboratorContext
    ) -> dict:
        actor = self._validate_collaborator(collaborator)
        if actor.shift != "ADM":
            self.repository.record_audit_for_public_id(public_id, actor, "RECONFERENCIA_SEM_PERMISSAO", "BLOCKED")
            raise ConflictError("Somente usuários ADM podem autorizar reconferência.", code="RECONFERENCIA_SEM_PERMISSAO")
        justification = reason.strip() if isinstance(reason, str) else ""
        if len(justification) < 10:
            self.repository.record_audit_for_public_id(public_id, actor, "RECONFERENCIA_SEM_JUSTIFICATIVA", "BLOCKED")
            raise ValidationError("Informe uma justificativa de pelo menos 10 caracteres.", code="JUSTIFICATIVA_OBRIGATORIA")
        previous = self._find(public_id)
        new_id = f"CONF-{uuid4().hex[:12].upper()}"
        try:
            self.repository.create_reconference(previous["public_id"], new_id, actor, justification)
        except ActiveConferenceError as error:
            raise self._active_conference_error(error.public_id) from error
        except RepositoryStateError as error:
            self._raise_state_error(error, action="reconference")
        result = self.get_pallet(new_id)
        result.update({
            "action": "admin_reconference_created",
            "message": "Reconferência autorizada e criada com sucesso.",
            "previous_public_id": previous["public_id"],
        })
        return result

    def get_pallet(self, public_id: str) -> dict:
        pallet = self.repository.details(
            self._required(public_id, "ID público da conferência")
        )
        if pallet is None:
            raise NotFoundError("Conferência não encontrada.")
        return pallet

    def get_pallet_for_collaborator(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> dict:
        self._pallet_for_collaborator(public_id, collaborator)
        return self.get_pallet(public_id)

    def start_pallet(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> dict:
        actor, pallet = self._pallet_for_collaborator(public_id, collaborator)
        try:
            self.repository.start(pallet["id"], actor)
        except RepositoryStateError as error:
            self._raise_state_error(error, action="start")
        result = self.get_pallet(pallet["public_id"])
        result["message"] = "Conferência iniciada."
        return result

    def scan_carton(
        self,
        public_id: str,
        carton_code: object,
        collaborator: CollaboratorContext,
    ) -> dict:
        actor, pallet = self._pallet_for_collaborator(public_id, collaborator)
        code = normalize_caixa_estoque(carton_code)
        if not code:
            raise InvalidScanError("Código da caixa é obrigatório.")
        try:
            classification, first_seen, match_type, expected_code = self.repository.process_scan(
                pallet["id"], code, actor
            )
        except AmbiguousBoxCodeError as error:
            raise ConflictError(
                "O código informado corresponde a mais de uma caixa esperada. Informe o código completo.",
                code="AMBIGUOUS_BOX_CODE",
                details={"matches": error.matches},
            ) from error
        except RepositoryStateError as error:
            self._raise_state_error(error, action="scan")
        result = self.get_pallet(pallet["public_id"])
        public_result, message = self._scan_response(classification)
        result.update(
            {
                "last_classification": classification,
                "result": public_result,
                "message": message,
                "caixa_estoque": expected_code or code,
                "scanned_code": code,
                "expected_code": expected_code,
                "match_type": match_type,
                "code": "BOX_NOT_FOUND" if classification == ScanClassification.EXTRA else None,
                "first_confirmation_at": first_seen,
            }
        )
        return result

    def finish_pallet(
        self,
        public_id: str,
        collaborator: CollaboratorContext,
        *,
        confirm_surpluses: bool = False,
    ) -> dict:
        actor, pallet = self._pallet_for_collaborator(public_id, collaborator)
        if pallet["conference_status"] == "CANCELADA":
            self._raise_state_error(RepositoryStateError("CANCELLED"), action="finish")
        if pallet["conference_status"] == "FINALIZADA":
            result = self.get_pallet(pallet["public_id"])
            result["message"] = "A conferência já está finalizada."
            return result
        state = self.get_pallet(pallet["public_id"])
        class_block_message = mensagem_bloqueio_finalizacao_por_classe(
            pallet["import_operation"], state["pallet_class"]
        )
        if class_block_message:
            raise ConflictError(
                class_block_message,
                code="PALLET_MISTO_NAO_PERMITIDO",
            )
        try:
            self.repository.finish(
                pallet["id"],
                actor,
                confirm_surpluses=confirm_surpluses is True,
            )
        except SurplusTreatmentRequiredError as error:
            raise ConflictError(
                "Esta conferência possui sobras que ainda não tiveram o tratamento confirmado.",
                code="SOBRAS_SEM_TRATAMENTO",
                details={
                    "sobras": error.surplus_count,
                    "requires_surplus_treatment": True,
                },
            ) from error
        except PendingConferenceError as error:
            raise ConflictError(
                "Não é possível finalizar a conferência.",
                code="CONFERENCIA_COM_PENDENCIAS",
                details={
                    "faltantes": error.missing,
                    "divergentes": error.divergent,
                    "sobras": error.extra,
                    "duplicidades": error.duplicate,
                    "caixas_nao_esperadas": error.by_type.get("CAIXA_NAO_ESPERADA", 0),
                    "faltas": error.by_type.get("FALTA", error.missing),
                },
            ) from error
        except RepositoryStateError as error:
            self._raise_state_error(error, action="finish")
        result = self.get_pallet(pallet["public_id"])
        result["message"] = "Conferência finalizada com sucesso."
        return result

    def correct_surplus(
        self,
        public_id: str,
        surplus_event_id: object,
        carton_code: object,
        collaborator: CollaboratorContext,
    ) -> dict:
        actor, pallet = self._pallet_for_collaborator(public_id, collaborator)
        if isinstance(surplus_event_id, bool):
            raise ValidationError("Informe o evento de sobra para corrigir.", code="SOBRA_EVENTO_INVALIDO")
        try:
            event_id = int(str(surplus_event_id).strip())
        except (TypeError, ValueError):
            raise ValidationError("Informe o evento de sobra para corrigir.", code="SOBRA_EVENTO_INVALIDO") from None
        if event_id <= 0:
            raise ValidationError("Informe o evento de sobra para corrigir.", code="SOBRA_EVENTO_INVALIDO")
        code = normalize_caixa_estoque(carton_code)
        if not code:
            raise InvalidScanError("Código da caixa é obrigatório.")
        try:
            pending = self.repository.correct_surplus(pallet["id"], event_id, code, actor)
        except SurplusCorrectionMismatchError as error:
            raise ConflictError(
                "Código incorreto. Bipe novamente a caixa correta.",
                code="CODIGO_CORRECAO_INCORRETO",
                details={"expected_code": error.expected_code, "scanned_code": error.scanned_code},
            ) from error
        except RepositoryStateError as error:
            if error.state == "SURPLUS_NOT_FOUND":
                raise ConflictError(
                    "A sobra selecionada não pertence à conferência ativa.",
                    code="SOBRA_NAO_ENCONTRADA",
                ) from error
            if error.state == "SURPLUS_ALREADY_CORRECTED":
                raise ConflictError(
                    "Esta sobra já foi corrigida.", code="SOBRA_JA_CORRIGIDA"
                ) from error
            self._raise_state_error(error, action="correct_surplus")
        result = self.get_pallet(pallet["public_id"])
        result.update(
            {
                "message": "Sobra corrigida com sucesso.",
                "corrected_surplus_event_id": event_id,
                "caixa_estoque": code,
                "pending_surpluses": pending,
            }
        )
        return result

    def cancel_pallet(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> dict:
        actor, pallet = self._pallet_for_collaborator(public_id, collaborator)
        try:
            self.repository.cancel(pallet["id"], actor)
        except RepositoryStateError as error:
            self._raise_state_error(error, action="cancel")
        result = self.get_pallet(pallet["public_id"])
        result["message"] = "Conferência cancelada. O histórico foi preservado e uma nova importação está liberada."
        return result

    def _find(self, public_id: str) -> sqlite3.Row:
        pallet = self.repository.find_by_public_id(
            self._required(public_id, "ID público da conferência")
        )
        if pallet is None:
            raise NotFoundError("Conferência não encontrada.")
        return pallet

    def _pallet_for_collaborator(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> tuple[CollaboratorContext, sqlite3.Row]:
        actor = self._validate_collaborator(collaborator)
        pallet = self._find(public_id)
        if pallet["created_by_collaborator_id"] != actor.id:
            raise NotFoundError("Conferência não encontrada.")
        return actor, pallet

    @staticmethod
    def _active_conference_error(public_id: str) -> ConflictError:
        return ConflictError(
            "Já existe uma conferência ativa. Finalize ou cancele a conferência atual antes de iniciar uma nova importação.",
            code="CONFERENCIA_ATIVA",
            details={"public_id": public_id},
        )

    @staticmethod
    def _scan_response(classification: ScanClassification) -> tuple[str, str]:
        if classification == ScanClassification.MATCHED:
            return "CONFERIDA", "Caixa conferida com sucesso."
        if classification == ScanClassification.EXTRA:
            return (
                "SOBRA",
                "SOBRA: código não esperado para este palete.",
            )
        return "DUPLICADA", "Caixa já conferida."

    @staticmethod
    def _raise_state_error(error: RepositoryStateError, *, action: str) -> None:
        if error.state == "NOT_FOUND":
            raise NotFoundError("Conferência não encontrada.") from error
        if error.state == "CANCELLED":
            raise ConflictError(
                "Esta conferência foi cancelada e exige um novo upload.",
                code="CONFERENCIA_CANCELADA",
            ) from error
        if error.state == "RESTART_DISABLED":
            raise ConflictError(
                "O reinício foi removido para preservar a conferência.",
                code="REINICIO_NAO_PERMITIDO",
            ) from error
        if action == "restart" and error.state == "FINISHED":
            raise ConflictError(
                "Uma conferência finalizada não pode ser reiniciada.",
                code="CONFERENCIA_FINALIZADA",
            ) from error
        if action == "cancel" and error.state == "FINISHED":
            raise ConflictError(
                "Não é possível cancelar uma conferência já finalizada.",
                code="CONFERENCIA_FINALIZADA",
            ) from error
        if error.state == "FINISHED":
            raise ConflictError(
                "Esta conferência já foi encerrada.",
                code="CONFERENCIA_ENCERRADA",
            ) from error
        if error.state == "NOT_STARTED":
            raise ConflictError(
                "A conferência ainda não foi iniciada.",
                code="CONFERENCIA_NAO_INICIADA",
            ) from error
        raise ConflictError(
            "O estado da conferência foi alterado por outra requisição. Atualize a tela.",
            code="CONCURRENT_STATE_CHANGE",
        ) from error

    @staticmethod
    def _validate_collaborator(
        collaborator: CollaboratorContext | None,
    ) -> CollaboratorContext:
        if (
            collaborator is None
            or collaborator.id is None
            or not collaborator.registration
            or not collaborator.name
            or collaborator.shift not in ConferenceService.SHIFTS
        ):
            raise ValidationError(
                "Identifique o colaborador antes de realizar esta operação.",
                code="COLABORADOR_NAO_IDENTIFICADO",
            )
        return collaborator

    @staticmethod
    def _required(value: str, label: str) -> str:
        cleaned = value.strip() if isinstance(value, str) else ""
        if not cleaned:
            raise ValidationError(f"{label} é obrigatório.")
        return cleaned

    @staticmethod
    def _required_upper(value: object, label: str) -> str:
        cleaned = " ".join(value.split()).upper() if isinstance(value, str) else ""
        if not cleaned:
            raise ValidationError(f"{label} é obrigatória.")
        return cleaned
