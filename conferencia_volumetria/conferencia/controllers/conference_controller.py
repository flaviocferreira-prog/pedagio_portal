from __future__ import annotations

from conferencia.domain.entities import CollaboratorContext, ConferenceImport
from conferencia.services.conference_service import ConferenceService, ValidationError


class ConferenceController:
    def __init__(self, service: ConferenceService) -> None:
        self.service = service

    def import_pallet(
        self,
        payload: dict[str, object],
        collaborator: CollaboratorContext,
    ) -> dict:
        filename = payload.get("filename")
        content_base64 = payload.get("content_base64")
        origin = payload.get("origin")
        operation = payload.get("operation")
        if not all(isinstance(value, str) for value in (filename, content_base64, origin, operation)):
            raise ValidationError(
                "Informe o nome e o conteúdo do arquivo para carregar o palete."
            )
        command = ConferenceImport(
            collaborator=collaborator,
            filename=filename,
            content_base64=content_base64,
            origin=origin,
            operation=operation,
        )
        return self.service.import_pallet(command)

    def get_pallet(self, public_id: str) -> dict:
        return self.service.get_pallet(public_id)

    def get_active_pallet(self, collaborator: CollaboratorContext) -> dict:
        return self.service.active_pallet(collaborator)

    def get_boxes(self, public_id: str) -> list[dict]:
        return self.service.get_pallet(public_id)["cartons"]

    def cancel_pallet(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> dict:
        return self.service.cancel_pallet(public_id, collaborator)

    def scan_carton(
        self,
        public_id: str,
        payload: dict[str, object],
        collaborator: CollaboratorContext,
    ) -> dict:
        return self.service.scan_carton(
            public_id,
            payload.get("caixa_estoque"),
            collaborator,
        )

    def start_pallet(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> dict:
        return self.service.start_pallet(public_id, collaborator)

    def finish_pallet(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> dict:
        return self.service.finish_pallet(public_id, collaborator)

    def restart_pallet(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> dict:
        return self.service.restart_pallet(public_id, collaborator)

    def authorize_reconference(
        self, public_id: str, payload: dict[str, object], collaborator: CollaboratorContext
    ) -> dict:
        return self.service.authorize_reconference(public_id, payload.get("justificativa"), collaborator)

    def sync_pallet(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> dict:
        return self.service.sync_pallet(public_id, collaborator)

    def list_pallets(self) -> list[dict]:
        return self.service.list_pallets()
