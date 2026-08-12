from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from conferencia.infrastructure.database import SQLiteDatabase
from conferencia.regras_conferencia import identificar_classe_palete


TABLES = (
    "CONFERENCIAS",
    "CAIXAS_CONFERENCIA",
    "BIPAGENS",
    "DIVERGENCIAS",
    "TENTATIVAS",
    "AUDITORIA",
    "COLABORADORES",
)


class SincronizacaoRepository:
    """Read-only operational export plus the small local delivery state."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def pending_conference_ids(self) -> list[str]:
        with self.database.connection(immediate=True) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO conference_google_sync_state(pallet_id, status, created_at, updated_at)
                SELECT id, 'PENDENTE', COALESCE(finished_at, updated_at, created_at),
                       COALESCE(finished_at, updated_at, created_at)
                FROM pallets WHERE conference_status = 'FINALIZADA'
                """
            )
            rows = connection.execute(
                """
                SELECT p.public_id
                FROM pallets p
                INNER JOIN conference_google_sync_state state ON state.pallet_id = p.id
                WHERE p.conference_status = 'FINALIZADA' AND state.status = 'PENDENTE'
                ORDER BY COALESCE(p.finished_at, p.updated_at, p.created_at), p.id
                """
            ).fetchall()
        return [str(row["public_id"]) for row in rows]

    def mark_synchronized(self, public_ids: Iterable[str], timestamp: str) -> None:
        ids = tuple(public_ids)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.database.connection(immediate=True) as connection:
            connection.execute(
                f"""
                UPDATE conference_google_sync_state
                SET status = 'SINCRONIZADO', synchronized_at = ?, last_attempt_at = ?,
                    last_error = NULL, updated_at = ?
                WHERE pallet_id IN (
                    SELECT id FROM pallets WHERE public_id IN ({placeholders})
                )
                """,
                (timestamp, timestamp, timestamp, *ids),
            )

    def mark_failed(self, public_ids: Iterable[str], message: str, timestamp: str) -> None:
        ids = tuple(public_ids)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.database.connection(immediate=True) as connection:
            connection.execute(
                f"""
                UPDATE conference_google_sync_state
                SET status = 'PENDENTE', attempt_count = attempt_count + 1,
                    last_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE pallet_id IN (
                    SELECT id FROM pallets WHERE public_id IN ({placeholders})
                )
                """,
                (timestamp, message[:500], timestamp, *ids),
            )

    def build_payload(self, public_ids: Iterable[str]) -> dict[str, Any]:
        ids = tuple(public_ids)
        if not ids:
            return {"conference_ids": [], "tables": {name: [] for name in TABLES}}
        placeholders = ",".join("?" for _ in ids)
        with self.database.connection() as connection:
            pallets = connection.execute(
                f"SELECT * FROM pallets WHERE public_id IN ({placeholders}) ORDER BY id", ids
            ).fetchall()
            pallet_ids = tuple(int(row["id"]) for row in pallets)
            tables = {name: [] for name in TABLES}
            for pallet in pallets:
                pallet_id = int(pallet["id"])
                public_id = str(pallet["public_id"])
                classes = [
                    row["ds_classe"] for row in connection.execute(
                        "SELECT DISTINCT ds_classe FROM expected_cartons WHERE pallet_id = ? AND TRIM(COALESCE(ds_classe, '')) <> '' ORDER BY ds_classe",
                        (pallet_id,),
                    )
                ]
                tables["CONFERENCIAS"].append(self._record(pallet, {
                    "ID_CONFERENCIA": public_id,
                    "ID_PALETE": str(pallet_id),
                    "CLASSE_PALETE": identificar_classe_palete(classes),
                    "CLASSES_ENCONTRADAS": " | ".join(str(item) for item in classes),
                }))
                for row in connection.execute("SELECT * FROM expected_cartons WHERE pallet_id = ? ORDER BY id", (pallet_id,)):
                    tables["CAIXAS_CONFERENCIA"].append(self._record(row, {
                        "ID_ITEM": f"CAIXA-{row['id']}", "ID_CONFERENCIA": public_id,
                        "TIPO_ITEM": "ESPERADA",
                    }))
                for row in connection.execute("SELECT * FROM unexpected_cartons WHERE pallet_id = ? ORDER BY id", (pallet_id,)):
                    tables["CAIXAS_CONFERENCIA"].append(self._record(row, {
                        "ID_ITEM": f"NAO_ESPERADA-{row['id']}", "ID_CONFERENCIA": public_id,
                        "TIPO_ITEM": "NAO_ESPERADA",
                    }))
                for row in connection.execute("SELECT * FROM surplus_events WHERE pallet_id = ? ORDER BY id", (pallet_id,)):
                    tables["CAIXAS_CONFERENCIA"].append(self._record(row, {
                        "ID_ITEM": f"SOBRA-{row['id']}", "ID_CONFERENCIA": public_id,
                        "TIPO_ITEM": "SOBRA",
                    }))
                for row in connection.execute("SELECT * FROM scan_events WHERE pallet_id = ? ORDER BY id", (pallet_id,)):
                    tables["BIPAGENS"].append(self._record(row, {
                        "ID_EVENTO": str(row["id"]), "ID_CONFERENCIA": public_id,
                    }))
                for row in connection.execute("SELECT * FROM conference_divergences WHERE pallet_id = ? ORDER BY id", (pallet_id,)):
                    tables["DIVERGENCIAS"].append(self._record(row, {
                        "ID_DIVERGENCIA": str(row["id"]), "ID_CONFERENCIA": public_id,
                    }))
                for row in connection.execute("SELECT * FROM conference_attempts WHERE pallet_id = ? ORDER BY id", (pallet_id,)):
                    tables["TENTATIVAS"].append(self._record(row, {
                        "ID_TENTATIVA": f"TENTATIVA-{row['id']}", "ID_CONFERENCIA": public_id,
                        "TIPO_REGISTRO": "TENTATIVA",
                    }))
                confirmations = connection.execute(
                    "SELECT c.* FROM attempt_confirmations c INNER JOIN conference_attempts a ON a.id = c.attempt_id WHERE a.pallet_id = ? ORDER BY c.id",
                    (pallet_id,),
                )
                for row in confirmations:
                    tables["TENTATIVAS"].append(self._record(row, {
                        "ID_TENTATIVA": f"CONFIRMACAO-{row['id']}", "ID_CONFERENCIA": public_id,
                        "TIPO_REGISTRO": "CONFIRMACAO_CAIXA",
                    }))
                for row in connection.execute("SELECT * FROM conference_audit_events WHERE pallet_id = ? ORDER BY id", (pallet_id,)):
                    tables["AUDITORIA"].append(self._record(row, {
                        "ID_AUDITORIA": f"AUDITORIA-{row['id']}", "ID_CONFERENCIA": public_id,
                        "TIPO_REGISTRO": "AUDITORIA",
                    }))
                for row in connection.execute("SELECT * FROM surplus_treatment_confirmations WHERE pallet_id = ? ORDER BY id", (pallet_id,)):
                    tables["AUDITORIA"].append(self._record(row, {
                        "ID_AUDITORIA": f"TRATAMENTO-SOBRA-{row['id']}", "ID_CONFERENCIA": public_id,
                        "TIPO_REGISTRO": "CONFIRMACAO_TRATAMENTO_SOBRA",
                    }))
                treatment_items = connection.execute(
                    """
                    SELECT item.*
                    FROM surplus_treatment_items item
                    INNER JOIN surplus_treatment_confirmations confirmation
                        ON confirmation.id = item.confirmation_id
                    WHERE confirmation.pallet_id = ?
                    ORDER BY item.id
                    """,
                    (pallet_id,),
                )
                for row in treatment_items:
                    tables["AUDITORIA"].append(self._record(row, {
                        "ID_AUDITORIA": f"ITEM-TRATAMENTO-SOBRA-{row['id']}", "ID_CONFERENCIA": public_id,
                        "TIPO_REGISTRO": "ITEM_TRATAMENTO_SOBRA",
                    }))
            if pallet_ids:
                collaborator_rows = connection.execute(
                    "SELECT * FROM colaboradores ORDER BY id"
                ).fetchall()
                tables["COLABORADORES"] = [self._record(row, {"ID_COLABORADOR": str(row["id"])}) for row in collaborator_rows]
        return {"conference_ids": list(ids), "tables": tables}

    @staticmethod
    def _record(row: Any, extra: dict[str, str]) -> dict[str, Any]:
        record = {key.upper(): value for key, value in dict(row).items()}
        record.update(extra)
        return record
