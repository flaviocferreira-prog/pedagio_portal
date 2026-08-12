from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from http.server import ThreadingHTTPServer

from conferencia.infrastructure.runtime import is_supported_python, unsupported_python_message


def main() -> int:
    if not is_supported_python():
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")
        logging.getLogger("conferencia").error(unsupported_python_message())
        return 1

    from conferencia.controllers.conference_controller import ConferenceController
    from conferencia.infrastructure.database import SQLiteDatabase
    from conferencia.infrastructure.logging_utils import configure_logging
    from conferencia.infrastructure.session_manager import SessionManager
    from conferencia.infrastructure.settings import AppSettings
    from conferencia.readers.excel_reader import PalletFileImporter
    from conferencia.repositories.colaborador_repository import ColaboradorRepository
    from conferencia.repositories.pallet_repository import PalletRepository
    from conferencia.services.acesso_service import AcessoService
    from conferencia.services.automatic_report_service import AutomaticReportService
    from conferencia.services.conference_service import ConferenceService
    from conferencia.sincronizacao.client import GoogleSheetsClient
    from conferencia.sincronizacao.repository import SincronizacaoRepository
    from conferencia.sincronizacao.service import SincronizacaoService
    from conferencia.web import ApplicationHandler

    configure_logging()
    logger = logging.getLogger("conferencia")
    settings = AppSettings()
    parser = argparse.ArgumentParser(description="Conferência de Volumetria")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=settings.server_port)
    args = parser.parse_args()

    database = SQLiteDatabase()
    database.initialize()
    repository = PalletRepository(database)
    if args.port != settings.server_port:
        settings = replace(settings, server_port=args.port)
    ApplicationHandler.controller = ConferenceController(ConferenceService(repository, PalletFileImporter(), settings))
    ApplicationHandler.acesso_service = AcessoService(ColaboradorRepository(database))
    ApplicationHandler.sessions = SessionManager()
    ApplicationHandler.max_json_bytes = settings.max_json_bytes
    ApplicationHandler.automatic_report_service = AutomaticReportService(settings.max_upload_bytes)
    ApplicationHandler.sincronizacao_service = SincronizacaoService(
        SincronizacaoRepository(database),
        GoogleSheetsClient(
            settings.google_sync_url,
            settings.google_sync_token,
            settings.google_sync_connect_timeout_seconds,
            settings.google_sync_read_timeout_seconds,
        ),
        batch_size=max(1, settings.google_sync_batch_size),
        max_attempts=max(1, settings.google_sync_max_attempts),
        retry_delay_seconds=max(0.0, settings.google_sync_retry_delay_seconds),
    )
    server = ThreadingHTTPServer((args.host, args.port), ApplicationHandler)
    logger.info("server_started host=%s port=%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("server_stopped reason=keyboard_interrupt")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
