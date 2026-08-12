from __future__ import annotations

from conferencia.domain.entities import CollaboratorContext, ConferenceImport
from conferencia.services.conference_service import ConferenceService, ValidationError
from conferencia.services.automatic_report_service import AutomaticReportService
from conferencia.services.limpeza_downloads import limpar_relatorios_wms
from base64 import b64encode


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
        agenda = payload.get("agenda", "")
        origin = payload.get("origin")
        operation = payload.get("operation")
        if not all(isinstance(value, str) for value in (filename, content_base64, origin, operation)) or not isinstance(agenda, str):
            raise ValidationError(
                "Informe o nome e o conteúdo do arquivo para carregar o palete."
            )
        command = ConferenceImport(
            collaborator=collaborator,
            filename=filename,
            content_base64=content_base64,
            agenda=agenda,
            origin=origin,
            operation=operation,
        )
        return self.service.import_pallet(command)

    def import_automatic_pallet(
        self, payload: dict[str, object], collaborator: CollaboratorContext, reports: AutomaticReportService
    ) -> dict:
        filename, content, downloads_directory = reports.read_for_import(
            payload.get("automatic_file_id")
        )
        command_payload = dict(payload)
        command_payload["filename"] = filename
        command_payload["content_base64"] = b64encode(content).decode("ascii")
        result = self.import_pallet(command_payload, collaborator)
        limpar_relatorios_wms(
            arquivo_em_uso=filename,
            downloads_directory=downloads_directory,
        )
        return result

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
