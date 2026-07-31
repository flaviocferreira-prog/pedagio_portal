from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer

from conferencia.controllers.conference_controller import ConferenceController
from conferencia.infrastructure.database import SQLiteDatabase
from conferencia.infrastructure.settings import AppSettings
from conferencia.readers.excel_reader import OpenpyxlPalletReader
from conferencia.repositories.pallet_repository import PalletRepository
from conferencia.repositories.colaborador_repository import ColaboradorRepository
from conferencia.services.acesso_service import AcessoService
from conferencia.infrastructure.session_manager import SessionManager
from conferencia.services.conference_service import ConferenceService
from conferencia.web import ApplicationHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="Conferência de Volumetria")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    database = SQLiteDatabase()
    database.initialize()
    repository = PalletRepository(database)
    ApplicationHandler.controller = ConferenceController(ConferenceService(repository, OpenpyxlPalletReader(), AppSettings()))
    ApplicationHandler.acesso_service = AcessoService(ColaboradorRepository(database))
    ApplicationHandler.sessions = SessionManager()
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
