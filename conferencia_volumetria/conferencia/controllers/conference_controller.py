from __future__ import annotations

from conferencia.domain.entities import CollaboratorContext
from conferencia.services.conference_service import ConferenceService
from conferencia.services.upload import UploadService


class ConferenceController:
    def __init__(self, service: ConferenceService, upload_service: UploadService) -> None:
        self.service = service
        self.upload_service = upload_service

    def latest_wms_report(self) -> dict[str, object]:
        return self.upload_service.localizar_ultimo_relatorio()

    def import_manual_pallet(
        self,
        payload: dict[str, object],
        collaborator: CollaboratorContext,
    ) -> dict:
        return self.upload_service.importar_manual(payload, collaborator)

    def import_automatic_pallet(
        self, payload: dict[str, object], collaborator: CollaboratorContext
    ) -> dict:
        return self.upload_service.importar_automaticamente(payload, collaborator)

    def get_pallet(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> dict:
        return self.service.get_pallet_for_collaborator(public_id, collaborator)

    def get_active_pallet(self, collaborator: CollaboratorContext) -> dict:
        return self.service.active_pallet(collaborator)

    def get_boxes(
        self, public_id: str, collaborator: CollaboratorContext
    ) -> list[dict]:
        return self.service.get_pallet_for_collaborator(public_id, collaborator)["cartons"]

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
        self,
        public_id: str,
        payload: dict[str, object],
        collaborator: CollaboratorContext,
    ) -> dict:
        return self.service.finish_pallet(
            public_id,
            collaborator,
            confirm_surpluses=payload.get("confirmar_sobras") is True,
        )

    def correct_surplus(
        self,
        public_id: str,
        payload: dict[str, object],
        collaborator: CollaboratorContext,
    ) -> dict:
        return self.service.correct_surplus(
            public_id,
            payload.get("surplus_event_id"),
            payload.get("caixa_estoque"),
            collaborator,
        )

    def authorize_reconference(
        self, public_id: str, payload: dict[str, object], collaborator: CollaboratorContext
    ) -> dict:
        return self.service.authorize_reconference(public_id, payload.get("justificativa"), collaborator)
