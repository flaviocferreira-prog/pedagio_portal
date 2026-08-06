from __future__ import annotations

import tempfile
import unittest
from base64 import b64encode
from pathlib import Path

from conferencia.domain.entities import CollaboratorContext, ConferenceImport
from conferencia.infrastructure.database import SQLiteDatabase
from conferencia.infrastructure.settings import AppSettings
from conferencia.readers.excel_reader import PalletFileImporter
from conferencia.repositories.colaborador_repository import ColaboradorRepository
from conferencia.repositories.pallet_repository import PalletRepository
from conferencia.services.conference_service import ConferenceService
from conferencia.services.google_sheets_sync_service import GoogleSheetsSyncService


class ManualSyncFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.database = SQLiteDatabase(Path(self.directory.name) / "flow.db")
        self.database.initialize()
        self.settings = AppSettings(
            temporary_directory=Path(self.directory.name) / "uploads",
            google_apps_script_url="https://script.google.com/a/macros/fisia.com.br/s/DEPLOYMENT/exec",
            google_sync_secret="test-secret",
            sync_retry_after_seconds=1,
        )
        collaborators = ColaboradorRepository(self.database)
        first = collaborators.create("100", "CONFERENTE", "T1")
        second = collaborators.create("200", "SINCRONIZADOR", "T2")
        self.owner = CollaboratorContext(int(first["id"]), "100", "CONFERENTE", "T1")
        self.other = CollaboratorContext(int(second["id"]), "200", "SINCRONIZADOR", "T2")
        self.repository = PalletRepository(self.database)
        self.service = ConferenceService(self.repository, PalletFileImporter(), self.settings)
        self.sync = GoogleSheetsSyncService(self.database, self.settings)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def finished(self, suffix: str) -> str:
        code = f"000{suffix}"
        created = self.service.import_pallet(ConferenceImport(self.owner, f"{suffix}.csv", b64encode(f"CAIXA_ESTOQUE\n{code}\n".encode()).decode(), "PORTAL", "DIGITAL"))
        public_id = created["public_id"]
        self.service.start_pallet(public_id, self.owner)
        self.service.scan_carton(public_id, code, self.owner)
        finished = self.service.finish_pallet(public_id, self.owner)
        self.assertEqual("FINALIZADA", finished["status"])
        self.assertEqual("PENDENTE", self.repository.details(public_id)["synchronization"]["status"])
        return public_id


class ManualSyncInterfaceTests(ManualSyncFlowTests):
    def test_sync_is_manual_non_blocking_and_has_no_automatic_sync_timer(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "index.html").read_text(encoding="utf-8")
        javascript = (root / "static" / "js" / "conference.js").read_text(encoding="utf-8")
        styles = (root / "static" / "styles.css").read_text(encoding="utf-8")
        notifications = (root / "static" / "js" / "notifications.js").read_text(encoding="utf-8")
        self.assertIn('id="actions-card"', page)
        self.assertIn('id="sync-pending-button"', page)
        self.assertIn("Sincronizar pendentes", page)
        self.assertIn('id="new-import-button"', page)
        self.assertNotIn('id="sync-card"', page)
        self.assertLess(page.index('id="sync-pending-button"'), page.index('id="new-import-button"'))
        self.assertIn("conference-metadata", page)
        self.assertLess(page.index('id="summary-imported-at"'), page.index('id="summary-collaborator"'))
        self.assertLess(page.index('id="summary-collaborator"'), page.index('id="summary-attempt"'))
        self.assertLess(page.index('id="summary-attempt"'), page.index('id="summary-timer"'))
        self.assertIn("#actions-card", javascript)
        self.assertIn("importações pendentes", javascript)
        self.assertIn(".conference-metadata span + span::before", styles)
        self.assertIn('class="scan-controls"', page)
        self.assertLess(page.index('id="carton-form"'), page.index('class="conference-actions"'))
        self.assertIn("grid-template-columns: repeat(7, minmax(0, 1fr))", styles)
        self.assertNotIn("<h1>", page)
        self.assertIn("export function showNotification", notifications)
        self.assertIn('id: "pending-sync-result"', javascript)
        self.assertNotIn('id="pending-sync-result"', page)
        self.assertIn("toast-container", styles)
        self.assertNotIn('id="sync-modal"', page)
        self.assertIn('pendingSynchronizations: "/api/sincronizacao/pendentes"', (root / "static" / "js" / "api-client.js").read_text(encoding="utf-8"))
        self.assertIn("routes.pendingSynchronizations", javascript)
        self.assertIn("routes.preparePendingSynchronizations", javascript)
        self.assertIn("window.open", javascript)
        self.assertNotIn("setInterval", javascript)
        self.assertNotIn("BroadcastChannel", javascript)
        self.assertNotIn("awaitingSynchronization", javascript)
        self.assertIn("finishModal.close(); showImportCard();", javascript)
    def test_manual_queue_reserves_all_records_one_at_a_time_and_preserves_owner(self) -> None:
        first, second = self.finished("1"), self.finished("2")
        self.assertEqual(2, self.sync.pending_summary()["pending"])
        prepared = self.sync.prepare_next_pending(self.other, "http://127.0.0.1:8080/sincronizacao/confirmar/")["prepared"]
        self.assertIn(prepared["public_id"], {first, second})
        self.assertEqual("100", __import__("json").loads(prepared["payload"])["conferencia"]["matricula"])
        self.assertEqual(1, self.sync.pending_summary()["pending"])

    def test_failure_returns_finalized_record_to_pending_and_stale_reservation_recovers(self) -> None:
        public_id = self.finished("1")
        prepared = self.sync.prepare_next_pending(self.other, "http://127.0.0.1:8080/sincronizacao/confirmar/")["prepared"]
        self.sync.fail(public_id, "GOOGLE_SYNC_FAILED", prepared["attempt_id"], prepared["nonce"], self.other)
        self.assertEqual("FINALIZADA", self.repository.details(public_id)["status"])
        self.assertEqual("PENDENTE", self.repository.details(public_id)["synchronization"]["status"])
        prepared = self.sync.prepare_next_pending(self.other, "http://127.0.0.1:8080/sincronizacao/confirmar/")["prepared"]
        with self.database.connection(immediate=True) as connection:
            connection.execute("UPDATE pallets SET sincronizacao_iniciada_em = '2000-01-01T00:00:00Z' WHERE public_id = ?", (public_id,))
        self.assertEqual(1, self.sync.recover_stale_attempts())
        self.assertEqual("PENDENTE", self.repository.details(public_id)["synchronization"]["status"])
