from __future__ import annotations

import sqlite3

from conferencia.domain.entities import CollaboratorContext


class DivergenceRepository:
    def record(self, connection: sqlite3.Connection, pallet_id: int, attempt_id: int, divergence_type: str, code: str, description: str, collaborator: CollaboratorContext, now: str, *, deduplicate: bool = False) -> None:
        if deduplicate and connection.execute(
            "SELECT 1 FROM conference_divergences WHERE pallet_id = ? AND divergence_type = ? AND carton_code = ? AND status = 'PENDENTE'",
            (pallet_id, divergence_type, code),
        ).fetchone() is not None:
            return
        connection.execute(
            "INSERT INTO conference_divergences(pallet_id, attempt_id, divergence_type, carton_code, description, found_at, found_by_collaborator_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pallet_id, attempt_id, divergence_type, code, description, now, collaborator.id),
        )

    def counts(self, connection: sqlite3.Connection, pallet_id: int) -> dict:
        by_type = {key: 0 for key in ("FALTA", "SOBRA", "DUPLICIDADE", "CAIXA_NAO_ESPERADA", "INCONSISTENCIA")}
        total = pending = resolved = 0
        for row in connection.execute("SELECT divergence_type, status, COUNT(*) AS total FROM conference_divergences WHERE pallet_id = ? GROUP BY divergence_type, status", (pallet_id,)):
            total += row["total"]
            if row["status"] == "PENDENTE":
                pending += row["total"]
                by_type[row["divergence_type"]] += row["total"]
            else:
                resolved += row["total"]
        return {"total": total, "pending": pending, "resolved": resolved, "by_type": by_type}
