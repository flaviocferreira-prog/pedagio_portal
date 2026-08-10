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
    from conferencia.sync.client import AppsScriptClient
    from conferencia.sync.repository import SyncRepository
    from conferencia.sync.scheduler import SyncScheduler
    from conferencia.sync.service import SyncService
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
    sync_service = SyncService(
        SyncRepository(database),
        AppsScriptClient(
            settings.apps_script_url,
            settings.sync_shared_secret,
            connect_timeout_seconds=settings.sync_connect_timeout_seconds,
            read_timeout_seconds=settings.sync_read_timeout_seconds,
        ),
        batch_size=settings.sync_batch_size,
        max_attempts=settings.sync_max_attempts,
        retry_delay_seconds=settings.sync_retry_delay_seconds,
        reservation_timeout_seconds=settings.sync_reservation_timeout_seconds,
    )
    sync_scheduler = SyncScheduler(
        sync_service,
        interval_seconds=settings.sync_interval_minutes * 60,
    )
    if args.port != settings.server_port:
        settings = replace(settings, server_port=args.port)
    ApplicationHandler.controller = ConferenceController(ConferenceService(repository, PalletFileImporter(), settings))
    ApplicationHandler.acesso_service = AcessoService(ColaboradorRepository(database))
    ApplicationHandler.sessions = SessionManager()
    ApplicationHandler.max_json_bytes = settings.max_json_bytes
    ApplicationHandler.automatic_report_service = AutomaticReportService(settings.max_upload_bytes)
    ApplicationHandler.sync_service = sync_service
    server = ThreadingHTTPServer((args.host, args.port), ApplicationHandler)
    sync_scheduler.start()
    logger.info("server_started host=%s port=%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("server_stopped reason=keyboard_interrupt")
    finally:
        sync_scheduler.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
