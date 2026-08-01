from __future__ import annotations

import sqlite3
import tempfile
from base64 import b64decode
from binascii import Error as Base64Error
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from conferencia.domain.box_codes import content_hash_caixa_estoque, normalize_caixa_estoque
from conferencia.domain.entities import (
    CollaboratorContext,
    ConferenceImport,
    ScanClassification,
)
from conferencia.infrastructure.settings import AppSettings
from conferencia.readers.excel_reader import OpenpyxlPalletReader
from conferencia.repositories.pallet_repository import (
    ActiveConferenceError,
    PalletRepository,
    PendingConferenceError,
    RepositoryStateError,
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
    ORIGINS = frozenset({"PORTAL", "TL"})
    OPERATIONS = frozenset({"DIGITAL", "NIKESTORE", "CROSS"})
    SHIFTS = frozenset({"T1", "T2", "T3", "ADM"})
    def __init__(
        self,
        repository: PalletRepository,
        excel_reader: OpenpyxlPalletReader,
        settings: AppSettings | None = None,
    ) -> None:
        self.repository = repository
        self.excel_reader = excel_reader
        self.settings = settings or AppSettings()

    def import_pallet(self, import_data: ConferenceImport) -> dict:
        collaborator = self._validate_collaborator(import_data.collaborator)
        origin = self._choice(import_data.origin, self.ORIGINS, "origem")
        operation = self._choice(import_data.operation, self.OPERATIONS, "operação")
        extension = self._safe_extension(import_data.filename)
        content = self._decode_base64(
            import_data.content_base64, self.settings.max_upload_bytes
        )
        self.settings.temporary_directory.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                suffix=extension,
                prefix="conference_",
                dir=self.settings.temporary_directory,
                delete=False,
            ) as temporary_file:
                temporary_file.write(content)
                temporary_path = Path(temporary_file.name)
            carton_codes = self.excel_reader.read_carton_codes(
                temporary_path, extension
            )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        carton_codes = [normalize_caixa_estoque(code) for code in carton_codes]
        carton_codes = [code for code in carton_codes if code]
        if not carton_codes:
            raise ValidationError("O arquivo não possui caixas estoque válidas.")
        # Signature comes solely from the textual CAIXA_ESTOQUE multiset;
        # filename, physical bytes, line order and workstation do not affect it.
        fingerprint = getattr(self.excel_reader, "fingerprint", content_hash_caixa_estoque)
        content_hash = fingerprint(carton_codes)
        source_fingerprint = sha256(content + f"|{origin}|{operation}".encode()).hexdigest()
        public_id = f"CONF-{uuid4().hex[:12].upper()}"
        try:
            decision = self.repository.create(
                public_id,
                collaborator,
                import_data.filename,
                carton_codes,
                source_fingerprint,
                origin,
                operation,
                collaborator.shift,
                content_hash,
            )
        except ActiveConferenceError as error:
            raise self._active_conference_error(error.public_id) from error
        except sqlite3.IntegrityError as error:
            raise ConflictError(
                "Não foi possível criar a conferência sem duplicidades.",
                code="CONFERENCE_CREATION_CONFLICT",
            ) from error
        result = self.get_pallet(str(decision["public_id"]))
        action = str(decision["action"])
        if action == "already_completed":
            owner = result["collaborator"]
            result.update({
                "action": action,
                "message": (
                    "Conferência já realizada. Este palete já foi conferido anteriormente e não pode ser iniciado novamente. "
                    f"Conferente: {owner['name']}. Matrícula: {owner['registration']}. "
                    f"Finalizado em: {result['finalization']['finished_at']}. "
                    f"Total conferido: {result['summary']['total_confirmed']} caixas."
                ),
                "can_authorize_reconference": collaborator.shift == "ADM",
            })
            return result
        if action == "resumed":
            owner = result["collaborator"]
            result.update({
                "action": action,
                "message": (
                    "Conferência já em andamento. Este palete já possui uma conferência aberta. "
                    f"Iniciado por: {owner['name']}. Matrícula: {owner['registration']}. "
                    f"Iniciado em: {result['importation']['imported_at']}. O progresso existente foi carregado."
                ),
            })
            return result
        return self._import_response(
            result,
            total_lines=len(carton_codes),
            total_imported=len(carton_codes),
            total_duplicates=0,
            message=("Nova conferência criada a partir de conteúdo anteriormente cancelado." if action == "created_after_cancellation" else "Nova conferência criada com sucesso."),
            action=action,
            content_hash=content_hash,
            previous_public_id=decision.get("previous_public_id"),
        )

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

    @staticmethod
    def _import_response(
        result: dict,
        *,
        total_lines: int,
        total_imported: int,
        total_duplicates: int,
        message: str,
        action: str = "created",
        content_hash: str | None = None,
        previous_public_id: object = None,
    ) -> dict:
        """Expõe a lista já confirmada no banco e os totais do upload."""
        result.update(
            {
                "message": message,
                "importacao_id": result["public_id"],
                "total_linhas": total_lines,
                "total_importadas": total_imported,
                "total_duplicadas": total_duplicates,
                "caixas": result["cartons"],
                "action": action,
                "content_hash": content_hash or result.get("content_hash"),
                "previous_public_id": previous_public_id,
            }
        )
        return result

    def get_pallet(self, public_id: str) -> dict:
        pallet = self.repository.details(
            self._required(public_id, "ID público da conferência")
        )
        if pallet is None:
            raise NotFoundError("Conferência não encontrada.")
        return pallet

    def start_pallet(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> dict:
        actor = self._validate_collaborator(collaborator)
        pallet = self._find(public_id)
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
        actor = self._validate_collaborator(collaborator)
        code = normalize_caixa_estoque(carton_code)
        if not code:
            raise InvalidScanError("Código da caixa é obrigatório.")
        pallet = self._find(public_id)
        try:
            classification, first_seen = self.repository.process_scan(
                pallet["id"], code, actor
            )
        except RepositoryStateError as error:
            self._raise_state_error(error, action="scan")
        result = self.get_pallet(pallet["public_id"])
        public_result, message = self._scan_response(classification)
        result.update(
            {
                "last_classification": classification,
                "result": public_result,
                "message": message,
                "caixa_estoque": code,
                "first_confirmation_at": first_seen,
            }
        )
        return result

    def finish_pallet(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> dict:
        actor = self._validate_collaborator(collaborator)
        pallet = self._find(public_id)
        if pallet["conference_status"] == "CANCELADA":
            self._raise_state_error(RepositoryStateError("CANCELLED"), action="finish")
        if pallet["conference_status"] == "FINALIZADA":
            result = self.get_pallet(pallet["public_id"])
            result["message"] = "A conferência já está finalizada."
            return result
        try:
            self.repository.finish(pallet["id"], actor)
        except PendingConferenceError as error:
            raise ConflictError(
                "Não é possível finalizar a conferência.",
                code="CONFERENCIA_COM_PENDENCIAS",
                details={
                    "faltantes": error.missing,
                    "divergentes": error.divergent,
                    "sobras": error.extra,
                    "duplicidades": error.duplicate,
                },
            ) from error
        except RepositoryStateError as error:
            self._raise_state_error(error, action="finish")
        result = self.get_pallet(pallet["public_id"])
        result["message"] = "Conferência finalizada com sucesso."
        return result

    def restart_pallet(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> dict:
        self._validate_collaborator(collaborator)
        raise ConflictError(
            "O reinÃ­cio foi removido para preservar a conferÃªncia. Cancele-a e importe uma nova planilha.",
            code="REINICIO_NAO_PERMITIDO",
        )
        actor = self._validate_collaborator(collaborator)
        pallet = self._find(public_id)
        try:
            self.repository.restart(pallet["id"], actor)
        except RepositoryStateError as error:
            self._raise_state_error(error, action="restart")
        result = self.get_pallet(pallet["public_id"])
        result["message"] = "Conferência reiniciada. Uma nova tentativa foi criada."
        return result

    def cancel_pallet(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> dict:
        actor = self._validate_collaborator(collaborator)
        pallet = self._find(public_id)
        try:
            self.repository.cancel(pallet["id"], actor)
        except RepositoryStateError as error:
            self._raise_state_error(error, action="cancel")
        result = self.get_pallet(pallet["public_id"])
        result["message"] = "ConferÃªncia cancelada. O histÃ³rico foi preservado e uma nova importaÃ§Ã£o estÃ¡ liberada."
        return result
        return {
            "public_id": pallet["public_id"],
            "status": "CANCELLED",
            "redirect_url": "/conference",
            "message": "Conferência cancelada. Importe um novo arquivo para continuar.",
        }

    def list_pallets(self) -> list[dict]:
        return self.repository.list_recent()

    def sync_pallet(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> dict:
        actor = self._validate_collaborator(collaborator)
        conference = self.get_pallet(public_id)
        if conference["workflow_status"] != "FINALIZADA":
            raise ConflictError(
                "A sincronização só é permitida após a finalização."
            )
        message = "Integração com Google Sheets ainda não está configurada."
        pallet = self._find(public_id)
        self.repository.record_sync_not_configured(pallet["id"], actor, message)
        return {
            "public_id": conference["public_id"],
            "sync_status": "NOT_CONFIGURED",
            "message": message,
        }

    def _find(self, public_id: str) -> sqlite3.Row:
        pallet = self.repository.find_by_public_id(
            self._required(public_id, "ID público da conferência")
        )
        if pallet is None:
            raise NotFoundError("Conferência não encontrada.")
        return pallet

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
            return "CONFERIDA", "Conferência OK."
        if classification == ScanClassification.EXTRA:
            return (
                "DIVERGENTE",
                "Caixa divergente — código não esperado para este palete.",
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
                "O reinÃ­cio foi removido para preservar a conferÃªncia.",
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
    def _choice(value: object, allowed: frozenset[str], label: str) -> str:
        normalized = value.strip().upper() if isinstance(value, str) else ""
        if normalized not in allowed:
            raise ValidationError(f"Selecione uma {label} válida.")
        return normalized

    @staticmethod
    def _required(value: str, label: str) -> str:
        cleaned = value.strip() if isinstance(value, str) else ""
        if not cleaned:
            raise ValidationError(f"{label} é obrigatório.")
        return cleaned

    @staticmethod
    def _decode_base64(value: str, max_bytes: int) -> bytes:
        if not isinstance(value, str) or not value:
            raise ValidationError("Conteúdo do arquivo é obrigatório.")
        encoded = value.split(",", 1)[-1]
        if len(encoded) > ((max_bytes + 2) // 3) * 4:
            raise PayloadTooLargeError(
                "Arquivo excede o limite configurado de 10 MB."
            )
        try:
            content = b64decode(encoded, validate=True)
        except Base64Error as error:
            raise ValidationError("Conteúdo Base64 do arquivo é inválido.") from error
        if len(content) > max_bytes:
            raise PayloadTooLargeError(
                "Arquivo excede o limite configurado de 10 MB."
            )
        return content

    @staticmethod
    def _safe_extension(filename: str) -> str:
        if (
            not filename
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
        ):
            raise ValidationError("Nome de arquivo inválido.")
        extension = Path(filename).suffix.lower()
        if extension not in {".xlsx", ".csv"}:
            raise ValidationError(
                "Envie somente arquivos .xlsx ou .csv; .xls não é aceito."
            )
        return extension
