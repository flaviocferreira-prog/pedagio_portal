from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from conferencia.domain.box_codes import content_hash_caixa_estoque


class SQLiteDatabase:
    """Conexões SQLite e migrações incrementais, sem reset destrutivo."""

    def __init__(self, database_path: Path | None = None) -> None:
        self.database_path = database_path or Path(__file__).resolve().parents[2] / "data" / "conferencia.db"

    def connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    @contextmanager
    def connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        connection = self.connect()
        try:
            # Migrações que trocam uma tabela precisam desligar as FKs antes de
            # abrir a transação. As conexões normais continuam com FKs ativos.
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    description TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            if 1 not in applied:
                self._migration_001_base_schema(connection)
                self._record(connection, 1, "esquema base da conferencia")
            if 2 not in applied:
                self._migration_002_attempts_and_audit(connection)
                self._record(connection, 2, "tentativas e auditoria por colaborador")
            if 3 not in applied:
                self._migration_003_totals_and_synchronization(connection)
                self._record(connection, 3, "total importado e tentativas de sincronizacao")
            if 4 not in applied:
                self._migration_004_caixa_estoque_as_text(connection)
                self._record(connection, 4, "caixa estoque preservada como texto")
            if 5 not in applied:
                self._migration_005_import_fingerprint(connection)
                self._record(connection, 5, "idempotencia de importacao por arquivo")
            if 6 not in applied:
                self._migration_006_turno_e_cancelamento(connection)
                self._record(connection, 6, "turno do colaborador e cancelamento de conferencia")
            if 7 not in applied:
                self._migration_007_identificacao_importacao(connection)
                self._record(connection, 7, "origem e operacao da importacao")
            if 8 not in applied:
                self._migration_008_estado_ativo_e_horario_importacao(connection)
                self._record(connection, 8, "estado ativo por colaborador e horario UTC da importacao")
            if 9 not in applied:
                self._migration_009_ciclo_fechado_por_conferencia(connection)
                self._record(connection, 9, "ciclo fechado por conferencia e historico preservado")
            if 10 not in applied:
                self._migration_010_assinatura_e_auditoria_de_palete(connection)
                self._record(connection, 10, "assinatura de conteudo, reconferencia e auditoria")
            if 11 not in applied:
                self._migration_011_divergencias_auditaveis(connection)
                self._record(connection, 11, "divergencias pendentes e resolvidas por conferencia")
            if 12 not in applied:
                self._migration_012_agenda_importacao(connection)
                self._record(connection, 12, "agenda obrigatoria da importacao")
            if 13 not in applied:
                self._migration_013_cancelamento_de_tentativa(connection)
                self._record(connection, 13, "cancelamento terminal de tentativa")
            if 14 not in applied:
                self._migration_014_google_sheets_sync(connection)
                self._record(connection, 14, "sincronizacao assinada com Google Sheets")
            if 15 not in applied:
                self._migration_015_sync_attempt_contract(connection)
                self._record(connection, 15, "tentativas de sincronizacao vinculadas por nonce")
            if 16 not in applied:
                self._migration_016_manual_sync_statuses(connection)
                self._record(connection, 16, "sincronizacao manual pendente e recuperavel")
            if 17 not in applied:
                self._migration_017_expected_carton_class(connection)
                self._record(connection, 17, "classe por caixa esperada")
            if 18 not in applied:
                self._migration_018_repeated_expected_cartons(connection)
                self._record(connection, 18, "ocorrencias repetidas de caixa esperada")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("A migração de caixa estoque violou chaves estrangeiras.")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _record(connection: sqlite3.Connection, version: int, description: str) -> None:
        connection.execute(
            "INSERT INTO schema_migrations(version, description) VALUES (?, ?)",
            (version, description),
        )

    @staticmethod
    def _migration_001_base_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS colaboradores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                matricula TEXT NOT NULL UNIQUE,
                nome TEXT NOT NULL,
                ativo INTEGER NOT NULL DEFAULT 1 CHECK(ativo IN (0, 1)),
                data_criacao TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                data_atualizacao TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS pallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE COLLATE NOCASE,
                public_id TEXT UNIQUE,
                collaborator_id TEXT,
                source_filename TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'READY'
                    CHECK(status IN ('READY', 'IN_PROGRESS', 'COMPLETED', 'COMPLETED_WITH_DIVERGENCE')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                finished_at TEXT,
                duration_seconds INTEGER,
                final_justification TEXT
            );
            CREATE TABLE IF NOT EXISTS expected_cartons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_id INTEGER NOT NULL REFERENCES pallets(id) ON DELETE RESTRICT,
                code TEXT NOT NULL COLLATE NOCASE,
                status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK(status IN ('PENDING', 'CONFIRMED')),
                confirmed_at TEXT,
                UNIQUE(pallet_id, code)
            );
            CREATE TABLE IF NOT EXISTS scan_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_id INTEGER NOT NULL REFERENCES pallets(id) ON DELETE RESTRICT,
                scanned_code TEXT NOT NULL COLLATE NOCASE,
                classification TEXT NOT NULL
                    CHECK(classification IN ('MATCHED', 'DUPLICATE', 'EXTRA', 'DUPLICATE_EXTRA')),
                scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS unexpected_cartons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_id INTEGER NOT NULL REFERENCES pallets(id) ON DELETE RESTRICT,
                code TEXT NOT NULL COLLATE NOCASE,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                attempts INTEGER NOT NULL DEFAULT 1,
                UNIQUE(pallet_id, code)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pallets_public_id ON pallets(public_id);
            CREATE INDEX IF NOT EXISTS idx_expected_cartons_pallet ON expected_cartons(pallet_id);
            CREATE INDEX IF NOT EXISTS idx_scan_events_pallet ON scan_events(pallet_id);
            """
        )
        SQLiteDatabase._add_column_if_missing(connection, "pallets", "public_id", "TEXT")
        SQLiteDatabase._add_column_if_missing(connection, "pallets", "collaborator_id", "TEXT")
        SQLiteDatabase._add_column_if_missing(connection, "pallets", "started_at", "TEXT")
        SQLiteDatabase._add_column_if_missing(connection, "pallets", "finished_at", "TEXT")
        SQLiteDatabase._add_column_if_missing(connection, "pallets", "duration_seconds", "INTEGER")
        SQLiteDatabase._add_column_if_missing(connection, "pallets", "final_justification", "TEXT")

    @staticmethod
    def _migration_002_attempts_and_audit(connection: sqlite3.Connection) -> None:
        SQLiteDatabase._add_column_if_missing(
            connection, "pallets", "created_by_collaborator_id", "INTEGER REFERENCES colaboradores(id)"
        )
        SQLiteDatabase._add_column_if_missing(
            connection, "pallets", "started_by_collaborator_id", "INTEGER REFERENCES colaboradores(id)"
        )
        SQLiteDatabase._add_column_if_missing(
            connection, "pallets", "finished_by_collaborator_id", "INTEGER REFERENCES colaboradores(id)"
        )
        SQLiteDatabase._add_column_if_missing(connection, "pallets", "updated_at", "TEXT")
        SQLiteDatabase._add_column_if_missing(
            connection, "expected_cartons", "confirmed_by_collaborator_id", "INTEGER REFERENCES colaboradores(id)"
        )
        SQLiteDatabase._add_column_if_missing(
            connection, "expected_cartons", "confirmed_attempt_id", "INTEGER"
        )
        SQLiteDatabase._add_column_if_missing(connection, "scan_events", "attempt_id", "INTEGER")
        SQLiteDatabase._add_column_if_missing(
            connection, "scan_events", "collaborator_id", "INTEGER REFERENCES colaboradores(id)"
        )
        SQLiteDatabase._add_column_if_missing(
            connection, "scan_events", "collaborator_registration", "TEXT"
        )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conference_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_id INTEGER NOT NULL REFERENCES pallets(id) ON DELETE RESTRICT,
                attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
                status TEXT NOT NULL CHECK(status IN ('READY', 'IN_PROGRESS', 'RESTARTED', 'COMPLETED')),
                started_at TEXT,
                finished_at TEXT,
                started_by_collaborator_id INTEGER REFERENCES colaboradores(id),
                restarted_by_collaborator_id INTEGER REFERENCES colaboradores(id),
                finished_by_collaborator_id INTEGER REFERENCES colaboradores(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(pallet_id, attempt_number)
            );
            CREATE TABLE IF NOT EXISTS attempt_confirmations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL REFERENCES conference_attempts(id) ON DELETE RESTRICT,
                expected_carton_id INTEGER NOT NULL REFERENCES expected_cartons(id) ON DELETE RESTRICT,
                collaborator_id INTEGER NOT NULL REFERENCES colaboradores(id) ON DELETE RESTRICT,
                confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(attempt_id, expected_carton_id)
            );
            """
        )
        connection.execute(
            """
            INSERT INTO conference_attempts(
                pallet_id, attempt_number, status, started_at, finished_at,
                started_by_collaborator_id, finished_by_collaborator_id
            )
            SELECT p.id, 1,
                CASE
                    WHEN p.status = 'READY' THEN 'READY'
                    WHEN p.status = 'IN_PROGRESS' THEN 'IN_PROGRESS'
                    ELSE 'COMPLETED'
                END,
                p.started_at, p.finished_at,
                c.id,
                CASE WHEN p.finished_at IS NOT NULL THEN c.id END
            FROM pallets p
            LEFT JOIN colaboradores c ON c.matricula = p.collaborator_id
            WHERE NOT EXISTS (
                SELECT 1 FROM conference_attempts a WHERE a.pallet_id = p.id
            )
            """
        )
        connection.execute(
            """
            UPDATE pallets
            SET created_by_collaborator_id = COALESCE(
                    created_by_collaborator_id,
                    (SELECT id FROM colaboradores c WHERE c.matricula = pallets.collaborator_id)
                ),
                started_by_collaborator_id = CASE
                    WHEN started_at IS NULL THEN started_by_collaborator_id
                    ELSE COALESCE(
                        started_by_collaborator_id,
                        (SELECT id FROM colaboradores c WHERE c.matricula = pallets.collaborator_id)
                    )
                END,
                finished_by_collaborator_id = CASE
                    WHEN finished_at IS NULL THEN finished_by_collaborator_id
                    ELSE COALESCE(
                        finished_by_collaborator_id,
                        (SELECT id FROM colaboradores c WHERE c.matricula = pallets.collaborator_id)
                    )
                END,
                updated_at = COALESCE(updated_at, created_at)
            """
        )
        connection.execute(
            """
            UPDATE expected_cartons
            SET confirmed_attempt_id = (
                    SELECT a.id FROM conference_attempts a
                    WHERE a.pallet_id = expected_cartons.pallet_id
                    ORDER BY a.attempt_number DESC LIMIT 1
                ),
                confirmed_by_collaborator_id = (
                    SELECT p.created_by_collaborator_id FROM pallets p
                    WHERE p.id = expected_cartons.pallet_id
                )
            WHERE status = 'CONFIRMED' AND confirmed_attempt_id IS NULL
            """
        )
        connection.execute(
            """
            UPDATE scan_events
            SET attempt_id = (
                    SELECT a.id FROM conference_attempts a
                    WHERE a.pallet_id = scan_events.pallet_id
                    ORDER BY a.attempt_number DESC LIMIT 1
                ),
                collaborator_id = (
                    SELECT p.created_by_collaborator_id FROM pallets p
                    WHERE p.id = scan_events.pallet_id
                ),
                collaborator_registration = (
                    SELECT p.collaborator_id FROM pallets p
                    WHERE p.id = scan_events.pallet_id
                )
            WHERE attempt_id IS NULL
            """
        )
        SQLiteDatabase._migrate_unexpected_cartons(connection)
        connection.execute(
            """
            INSERT OR IGNORE INTO attempt_confirmations(
                attempt_id, expected_carton_id, collaborator_id, confirmed_at
            )
            SELECT ec.confirmed_attempt_id, ec.id, ec.confirmed_by_collaborator_id, ec.confirmed_at
            FROM expected_cartons ec
            WHERE ec.status = 'CONFIRMED'
              AND ec.confirmed_attempt_id IS NOT NULL
              AND ec.confirmed_by_collaborator_id IS NOT NULL
              AND ec.confirmed_at IS NOT NULL
            """
        )
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_attempts_pallet ON conference_attempts(pallet_id, attempt_number);
            CREATE INDEX IF NOT EXISTS idx_confirmations_attempt ON attempt_confirmations(attempt_id);
            CREATE INDEX IF NOT EXISTS idx_scan_events_attempt ON scan_events(attempt_id, scanned_code);
            CREATE INDEX IF NOT EXISTS idx_unexpected_attempt ON unexpected_cartons(attempt_id, code);
            """
        )

    @staticmethod
    def _migrate_unexpected_cartons(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(unexpected_cartons)")
        }
        if "attempt_id" in columns:
            return
        connection.executescript(
            """
            CREATE TABLE unexpected_cartons_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_id INTEGER NOT NULL REFERENCES pallets(id) ON DELETE RESTRICT,
                attempt_id INTEGER NOT NULL REFERENCES conference_attempts(id) ON DELETE RESTRICT,
                code TEXT NOT NULL COLLATE NOCASE,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                attempts INTEGER NOT NULL DEFAULT 1 CHECK(attempts > 0),
                first_scanned_by_collaborator_id INTEGER REFERENCES colaboradores(id),
                UNIQUE(attempt_id, code)
            );
            INSERT INTO unexpected_cartons_new(
                id, pallet_id, attempt_id, code, first_seen_at, last_seen_at,
                attempts, first_scanned_by_collaborator_id
            )
            SELECT u.id, u.pallet_id,
                (SELECT a.id FROM conference_attempts a
                 WHERE a.pallet_id = u.pallet_id
                 ORDER BY a.attempt_number DESC LIMIT 1),
                u.code, u.first_seen_at, u.last_seen_at, u.attempts,
                (SELECT p.created_by_collaborator_id FROM pallets p WHERE p.id = u.pallet_id)
            FROM unexpected_cartons u;
            DROP TABLE unexpected_cartons;
            ALTER TABLE unexpected_cartons_new RENAME TO unexpected_cartons;
            """
        )

    @staticmethod
    def _migration_003_totals_and_synchronization(
        connection: sqlite3.Connection,
    ) -> None:
        SQLiteDatabase._add_column_if_missing(
            connection,
            "pallets",
            "total_expected",
            "INTEGER NOT NULL DEFAULT 0 CHECK(total_expected >= 0)",
        )
        connection.execute(
            """
            UPDATE pallets
            SET total_expected = (
                SELECT COUNT(*) FROM expected_cartons ec
                WHERE ec.pallet_id = pallets.id
            )
            """
        )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS synchronization_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_id INTEGER NOT NULL REFERENCES pallets(id) ON DELETE RESTRICT,
                collaborator_id INTEGER NOT NULL REFERENCES colaboradores(id) ON DELETE RESTRICT,
                status TEXT NOT NULL CHECK(status IN ('PENDING', 'SUCCESS', 'FAILED', 'NOT_CONFIGURED')),
                message TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_synchronization_attempts_pallet
                ON synchronization_attempts(pallet_id, id);
            """
        )

    @staticmethod
    def _migration_004_caixa_estoque_as_text(connection: sqlite3.Connection) -> None:
        """Converte apenas o schema legado; nunca tenta adivinhar zeros perdidos."""
        columns = {
            row["name"]: str(row["type"]).upper()
            for row in connection.execute("PRAGMA table_info(expected_cartons)")
        }
        table_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'expected_cartons'"
        ).fetchone()
        table_sql = str(table_sql_row["sql"] or "").upper() if table_sql_row else ""
        if columns.get("code") == "TEXT" and "COLLATE NOCASE" not in table_sql:
            return

        # A cópia conserva os ids, vínculos e o valor textual existente. Se um
        # banco legado já havia gravado um número, não há como reconstruir seus
        # zeros; o registro precisa ser reimportado do CSV original.
        connection.executescript(
            """
            CREATE TABLE expected_cartons_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_id INTEGER NOT NULL REFERENCES pallets(id) ON DELETE RESTRICT,
                code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK(status IN ('PENDING', 'CONFIRMED')),
                confirmed_at TEXT,
                confirmed_by_collaborator_id INTEGER REFERENCES colaboradores(id),
                confirmed_attempt_id INTEGER,
                UNIQUE(pallet_id, code)
            );
            INSERT INTO expected_cartons_new(
                id, pallet_id, code, status, confirmed_at,
                confirmed_by_collaborator_id, confirmed_attempt_id
            )
            SELECT id, pallet_id, CAST(code AS TEXT), status, confirmed_at,
                   confirmed_by_collaborator_id, confirmed_attempt_id
            FROM expected_cartons;

            CREATE TABLE attempt_confirmations_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL REFERENCES conference_attempts(id) ON DELETE RESTRICT,
                expected_carton_id INTEGER NOT NULL REFERENCES expected_cartons_new(id) ON DELETE RESTRICT,
                collaborator_id INTEGER NOT NULL REFERENCES colaboradores(id) ON DELETE RESTRICT,
                confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(attempt_id, expected_carton_id)
            );
            INSERT INTO attempt_confirmations_new(
                id, attempt_id, expected_carton_id, collaborator_id, confirmed_at
            )
            SELECT id, attempt_id, expected_carton_id, collaborator_id, confirmed_at
            FROM attempt_confirmations;

            DROP TABLE attempt_confirmations;
            DROP TABLE expected_cartons;
            ALTER TABLE expected_cartons_new RENAME TO expected_cartons;
            ALTER TABLE attempt_confirmations_new RENAME TO attempt_confirmations;
            CREATE INDEX IF NOT EXISTS idx_expected_cartons_pallet ON expected_cartons(pallet_id);
            CREATE INDEX IF NOT EXISTS idx_confirmations_attempt ON attempt_confirmations(attempt_id);
            """
        )

    @staticmethod
    def _migration_005_import_fingerprint(connection: sqlite3.Connection) -> None:
        SQLiteDatabase._add_column_if_missing(connection, "pallets", "source_fingerprint", "TEXT")
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_pallets_source_fingerprint
            ON pallets(source_fingerprint)
            WHERE source_fingerprint IS NOT NULL
            """
        )

    @staticmethod
    def _migration_006_turno_e_cancelamento(connection: sqlite3.Connection) -> None:
        # Registros anteriores continuam utilizáveis e recebem ADM de forma
        # explícita; novos cadastros passam pela validação estrita do serviço.
        SQLiteDatabase._add_column_if_missing(connection, "colaboradores", "turno", "TEXT")
        connection.execute(
            "UPDATE colaboradores SET turno = 'ADM' WHERE turno IS NULL OR turno NOT IN ('T1', 'T2', 'T3', 'ADM')"
        )

    @staticmethod
    def _migration_007_identificacao_importacao(connection: sqlite3.Connection) -> None:
        SQLiteDatabase._add_column_if_missing(connection, "pallets", "import_origin", "TEXT")
        SQLiteDatabase._add_column_if_missing(connection, "pallets", "import_operation", "TEXT")
        SQLiteDatabase._add_column_if_missing(connection, "pallets", "imported_shift", "TEXT")
        SQLiteDatabase._add_column_if_missing(connection, "pallets", "imported_at", "TEXT")
        connection.execute("UPDATE pallets SET imported_at = COALESCE(imported_at, created_at)")
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_pallets_import_origin ON pallets(import_origin);
            CREATE INDEX IF NOT EXISTS idx_pallets_import_operation ON pallets(import_operation);
            CREATE INDEX IF NOT EXISTS idx_pallets_imported_at ON pallets(imported_at);
            """
        )
        SQLiteDatabase._add_column_if_missing(connection, "pallets", "cancelled_at", "TEXT")
        SQLiteDatabase._add_column_if_missing(
            connection,
            "pallets",
            "cancelled_by_collaborator_id",
            "INTEGER REFERENCES colaboradores(id)",
        )

    @staticmethod
    def _migration_008_estado_ativo_e_horario_importacao(
        connection: sqlite3.Connection,
    ) -> None:
        """Normaliza apenas a representação UTC e indexa a consulta do estado ativo.

        SQLite não possui um tipo datetime com timezone. O contrato persistido é
        ISO-8601 UTC com o sufixo ``Z``; a conversão para São Paulo ocorre somente
        ao montar a resposta de leitura.
        """
        connection.execute("DROP INDEX IF EXISTS idx_pallets_source_fingerprint")
        connection.execute(
            """
            UPDATE pallets
            SET imported_at = REPLACE(imported_at, ' ', 'T') || 'Z'
            WHERE imported_at IS NOT NULL
              AND INSTR(imported_at, 'T') = 0
            """
        )

    @staticmethod
    def _migration_009_ciclo_fechado_por_conferencia(
        connection: sqlite3.Connection,
    ) -> None:
        """Adiciona o estado oficial sem apagar o histórico legado.

        A coluna ``status`` antiga possuía um CHECK incompatível com os nomes
        operacionais. ``conference_status`` passa a ser a fonte canônica e o
        status legado continua somente para compatibilidade da base existente.
        """
        SQLiteDatabase._add_column_if_missing(
            connection, "pallets", "conference_status", "TEXT"
        )
        SQLiteDatabase._add_column_if_missing(
            connection, "pallets", "productivity_boxes_per_hour", "REAL"
        )
        SQLiteDatabase._add_column_if_missing(
            connection, "pallets", "print_status", "TEXT NOT NULL DEFAULT 'AVAILABLE'"
        )
        connection.execute(
            """
            UPDATE pallets
            SET conference_status = CASE
                WHEN cancelled_at IS NOT NULL THEN 'CANCELADA'
                WHEN status = 'COMPLETED' THEN 'FINALIZADA'
                ELSE 'EM_ABERTO'
            END
            WHERE conference_status IS NULL
            """
        )

    @staticmethod
    def _migration_010_assinatura_e_auditoria_de_palete(
        connection: sqlite3.Connection,
    ) -> None:
        for column, definition in (
            ("content_hash", "TEXT"),
            ("previous_conference_id", "INTEGER REFERENCES pallets(id)"),
            ("is_reconference", "INTEGER NOT NULL DEFAULT 0 CHECK(is_reconference IN (0, 1))"),
            ("reconference_authorized_by", "INTEGER REFERENCES colaboradores(id)"),
            ("reconference_reason", "TEXT"),
            ("reconference_authorized_at", "TEXT"),
        ):
            SQLiteDatabase._add_column_if_missing(connection, "pallets", column, definition)
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conference_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_id INTEGER REFERENCES pallets(id) ON DELETE RESTRICT,
                collaborator_id INTEGER REFERENCES colaboradores(id) ON DELETE SET NULL,
                registration TEXT,
                profile TEXT,
                ip_address TEXT,
                action TEXT NOT NULL,
                content_hash TEXT,
                justification TEXT,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_pallets_content_hash ON pallets(content_hash, id DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_content_hash ON conference_audit_events(content_hash, id DESC);
            """
        )
        pallets = connection.execute(
            "SELECT id FROM pallets WHERE content_hash IS NULL"
        ).fetchall()
        for pallet in pallets:
            codes = [
                row["code"]
                for row in connection.execute(
                    "SELECT code FROM expected_cartons WHERE pallet_id = ? ORDER BY id",
                    (pallet["id"],),
                )
            ]
            if codes:
                connection.execute(
                    "UPDATE pallets SET content_hash = ? WHERE id = ?",
                    (content_hash_caixa_estoque(codes), pallet["id"]),
                )
        connection.execute(
            """
            UPDATE pallets
            SET status = 'IN_PROGRESS',
                started_at = COALESCE(started_at, imported_at),
                updated_at = COALESCE(updated_at, imported_at)
            WHERE conference_status = 'EM_ABERTO' AND status = 'READY'
            """
        )

    @staticmethod
    def _migration_011_divergencias_auditaveis(
        connection: sqlite3.Connection,
    ) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conference_divergences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_id INTEGER NOT NULL REFERENCES pallets(id) ON DELETE RESTRICT,
                attempt_id INTEGER REFERENCES conference_attempts(id) ON DELETE RESTRICT,
                divergence_type TEXT NOT NULL CHECK(divergence_type IN (
                    'FALTA', 'SOBRA', 'DUPLICIDADE', 'CAIXA_NAO_ESPERADA', 'INCONSISTENCIA'
                )),
                carton_code TEXT,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDENTE' CHECK(status IN ('PENDENTE', 'RESOLVIDA')),
                found_at TEXT NOT NULL,
                found_by_collaborator_id INTEGER REFERENCES colaboradores(id) ON DELETE SET NULL,
                resolved_at TEXT,
                resolved_by_collaborator_id INTEGER REFERENCES colaboradores(id) ON DELETE SET NULL,
                resolution_note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_divergences_pallet_status
                ON conference_divergences(pallet_id, status, id);
            CREATE INDEX IF NOT EXISTS idx_divergences_attempt_status
                ON conference_divergences(attempt_id, status, id);
            """
        )

    @staticmethod
    def _migration_012_agenda_importacao(connection: sqlite3.Connection) -> None:
        SQLiteDatabase._add_column_if_missing(connection, "pallets", "import_agenda", "TEXT")
        connection.execute(
            """
            UPDATE conference_attempts
            SET status = 'IN_PROGRESS',
                started_at = COALESCE(started_at, (
                    SELECT p.started_at FROM pallets p WHERE p.id = conference_attempts.pallet_id
                ))
            WHERE status = 'READY'
              AND EXISTS (
                  SELECT 1 FROM pallets p
                  WHERE p.id = conference_attempts.pallet_id
                    AND p.conference_status = 'EM_ABERTO'
              )
            """
        )
        # A unicidade de conferência aberta e a transação de criação protegem
        # contra duplo clique sem impedir uma nova importação após encerramento.
        connection.execute("DROP INDEX IF EXISTS idx_pallets_source_fingerprint")
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_conference_per_collaborator
            ON pallets(created_by_collaborator_id)
            WHERE conference_status = 'EM_ABERTO'
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pallets_active_collaborator
            ON pallets(created_by_collaborator_id, status, cancelled_at, id DESC)
            """
        )

    @staticmethod
    def _migration_013_cancelamento_de_tentativa(connection: sqlite3.Connection) -> None:
        """Inclui o estado terminal CANCELLED sem descartar o histórico."""
        connection.executescript(
            """
            CREATE TABLE conference_attempts_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_id INTEGER NOT NULL REFERENCES pallets(id) ON DELETE RESTRICT,
                attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
                status TEXT NOT NULL CHECK(status IN ('READY', 'IN_PROGRESS', 'RESTARTED', 'COMPLETED', 'CANCELLED')),
                started_at TEXT,
                finished_at TEXT,
                started_by_collaborator_id INTEGER REFERENCES colaboradores(id),
                restarted_by_collaborator_id INTEGER REFERENCES colaboradores(id),
                finished_by_collaborator_id INTEGER REFERENCES colaboradores(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(pallet_id, attempt_number)
            );
            INSERT INTO conference_attempts_new
            SELECT * FROM conference_attempts;

            CREATE TABLE attempt_confirmations_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL REFERENCES conference_attempts_new(id) ON DELETE RESTRICT,
                expected_carton_id INTEGER NOT NULL REFERENCES expected_cartons(id) ON DELETE RESTRICT,
                collaborator_id INTEGER NOT NULL REFERENCES colaboradores(id) ON DELETE RESTRICT,
                confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(attempt_id, expected_carton_id)
            );
            INSERT INTO attempt_confirmations_new
            SELECT * FROM attempt_confirmations;

            DROP TABLE attempt_confirmations;
            DROP TABLE conference_attempts;
            ALTER TABLE conference_attempts_new RENAME TO conference_attempts;
            ALTER TABLE attempt_confirmations_new RENAME TO attempt_confirmations;
            CREATE INDEX IF NOT EXISTS idx_attempts_pallet ON conference_attempts(pallet_id, attempt_number);
            CREATE INDEX IF NOT EXISTS idx_confirmations_attempt ON attempt_confirmations(attempt_id);
            """
        )

    @staticmethod
    def _migration_014_google_sheets_sync(connection: sqlite3.Connection) -> None:
        for column, definition in (
            ("status_sincronizacao", "TEXT NOT NULL DEFAULT 'PENDENTE'"),
            ("sincronizacao_iniciada_em", "TEXT"),
            ("sincronizado_em", "TEXT"),
            ("tentativas_sincronizacao", "INTEGER NOT NULL DEFAULT 0"),
            ("ultimo_erro_sincronizacao", "TEXT"),
            ("recibo_sincronizacao", "TEXT"),
        ):
            SQLiteDatabase._add_column_if_missing(connection, "pallets", column, definition)
        connection.execute(
            """
            UPDATE pallets
            SET status_sincronizacao = CASE
                WHEN status_sincronizacao IN ('PENDENTE', 'SINCRONIZANDO', 'SINCRONIZADO', 'ERRO')
                    THEN status_sincronizacao
                ELSE 'PENDENTE'
            END
            """
        )

    @staticmethod
    def _migration_015_sync_attempt_contract(connection: sqlite3.Connection) -> None:
        """Keeps old database files compatible while making attempts authoritative."""
        for column, definition in (
            ("attempt_id", "TEXT"),
            ("conference_id", "TEXT"),
            ("nonce", "TEXT"),
            ("popup_token", "TEXT"),
            ("error_code", "TEXT"),
            ("receipt", "TEXT"),
            ("result_consumed_at", "TEXT"),
            ("reconciled", "INTEGER NOT NULL DEFAULT 0"),
        ):
            SQLiteDatabase._add_column_if_missing(
                connection, "synchronization_attempts", column, definition
            )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_attempts_attempt_id
            ON synchronization_attempts(attempt_id)
            WHERE attempt_id IS NOT NULL
            """
        )
    @staticmethod
    def _migration_016_manual_sync_statuses(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE pallets
            SET status_sincronizacao = 'PENDENTE'
            WHERE conference_status = 'FINALIZADA'
              AND status_sincronizacao IN ('ERRO', '', NULL)
            """
        )

    @staticmethod
    def _migration_017_expected_carton_class(connection: sqlite3.Connection) -> None:
        SQLiteDatabase._add_column_if_missing(connection, "expected_cartons", "ds_classe", "TEXT")

    @staticmethod
    def _migration_018_repeated_expected_cartons(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE expected_cartons_repeated (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_id INTEGER NOT NULL REFERENCES pallets(id) ON DELETE RESTRICT,
                code TEXT NOT NULL, ds_classe TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'CONFIRMED')),
                confirmed_at TEXT, confirmed_by_collaborator_id INTEGER REFERENCES colaboradores(id), confirmed_attempt_id INTEGER
            );
            INSERT INTO expected_cartons_repeated(id,pallet_id,code,ds_classe,status,confirmed_at,confirmed_by_collaborator_id,confirmed_attempt_id)
            SELECT id,pallet_id,code,ds_classe,status,confirmed_at,confirmed_by_collaborator_id,confirmed_attempt_id FROM expected_cartons;
            DROP TABLE expected_cartons;
            ALTER TABLE expected_cartons_repeated RENAME TO expected_cartons;
            CREATE INDEX idx_expected_cartons_pallet ON expected_cartons(pallet_id);
            """
        )
    @staticmethod
    def _add_column_if_missing(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        existing = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
