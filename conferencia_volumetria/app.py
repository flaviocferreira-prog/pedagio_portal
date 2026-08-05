from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer

from conferencia.controllers.conference_controller import ConferenceController
from conferencia.infrastructure.database import SQLiteDatabase
from conferencia.infrastructure.settings import AppSettings
from conferencia.readers.excel_reader import PalletFileImporter
from conferencia.repositories.pallet_repository import PalletRepository
from conferencia.repositories.colaborador_repository import ColaboradorRepository
from conferencia.services.acesso_service import AcessoService
from conferencia.infrastructure.session_manager import SessionManager
from conferencia.services.conference_service import ConferenceService
from conferencia.services.google_sheets_sync_service import GoogleSheetsSyncService
from conferencia.services.automatic_report_service import AutomaticReportService
from conferencia.web import ApplicationHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="Conferência de Volumetria")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    database = SQLiteDatabase()
    database.initialize()
    repository = PalletRepository(database)
    settings = AppSettings()
    ApplicationHandler.controller = ConferenceController(ConferenceService(repository, PalletFileImporter(), settings))
    ApplicationHandler.acesso_service = AcessoService(ColaboradorRepository(database))
    ApplicationHandler.sessions = SessionManager()
    ApplicationHandler.max_json_bytes = settings.max_json_bytes
    ApplicationHandler.google_sync_service = GoogleSheetsSyncService(database, settings)
    ApplicationHandler.automatic_report_service = AutomaticReportService(settings.max_upload_bytes)
    server = ThreadingHTTPServer((args.host, args.port), ApplicationHandler)
    print(f"Conferência de Volumetria disponível em http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
