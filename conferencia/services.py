from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from conferencia.database import Database


class ValidationError(Exception):
    pass


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


def _clean(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationError(f"{field} é obrigatório.")
    return text


@dataclass
class ConferenceService:
    database: Database

    def create_pallet(self, payload: dict) -> dict:
        code = _clean(payload.get("code"), "Código do palete")
        reference = _clean(payload.get("volumetry_reference"), "Referência da volumetria")
        try:
            expected = int(payload.get("expected_quantity"))
        except (TypeError, ValueError):
            raise ValidationError("Quantidade prevista deve ser um número inteiro.")
        if expected <= 0:
            raise ValidationError("Quantidade prevista deve ser maior que zero.")
        try:
            with self.database.connection() as conn:
                conn.execute(
                    "INSERT INTO pallets(code, volumetry_reference, expected_quantity) VALUES (?, ?, ?)",
                    (code, reference, expected),
                )
        except sqlite3.IntegrityError:
            raise ConflictError("Já existe um palete com este código.")
        return self.get_pallet(code)

    def get_pallet(self, code: str) -> dict:
        code = _clean(code, "Código do palete")
        with self.database.connection() as conn:
            row = conn.execute(
                """
                SELECT p.*, COUNT(c.id) AS scanned_quantity
                FROM pallets p LEFT JOIN cartons c ON c.pallet_id = p.id
                WHERE p.code = ? GROUP BY p.id
                """,
                (code,),
            ).fetchone()
            if row is None:
                raise NotFoundError("Palete não encontrado.")
            cartons = conn.execute(
                "SELECT code, scanned_at FROM cartons WHERE pallet_id = ? ORDER BY id DESC",
                (row["id"],),
            ).fetchall()
        return self._serialize_pallet(row, cartons)

    def scan_carton(self, pallet_code: str, payload: dict) -> dict:
        pallet_code = _clean(pallet_code, "Código do palete")
        carton_code = _clean(payload.get("code"), "Código da caixa")
        with self.database.connection() as conn:
            pallet = conn.execute("SELECT id, status FROM pallets WHERE code = ?", (pallet_code,)).fetchone()
            if pallet is None:
                raise NotFoundError("Palete não encontrado.")
            if pallet["status"] != "OPEN":
                raise ConflictError("Este palete já foi encerrado.")
            try:
                conn.execute("INSERT INTO cartons(pallet_id, code) VALUES (?, ?)", (pallet["id"], carton_code))
            except sqlite3.IntegrityError:
                raise ConflictError("Esta caixa já foi conferida neste palete.")
        return self.get_pallet(pallet_code)

    def finish_pallet(self, code: str, payload: dict) -> dict:
        pallet = self.get_pallet(code)
        if pallet["status"] != "OPEN":
            raise ConflictError("Este palete já foi encerrado.")
        reason = str(payload.get("discrepancy_reason") or "").strip()
        if pallet["difference"] != 0 and not reason:
            raise ValidationError("Informe a justificativa para encerrar com divergência.")
        with self.database.connection() as conn:
            conn.execute(
                "UPDATE pallets SET status = 'FINISHED', discrepancy_reason = ?, finished_at = CURRENT_TIMESTAMP WHERE code = ?",
                (reason or None, code),
            )
        return self.get_pallet(code)

    def list_pallets(self) -> list[dict]:
        with self.database.connection() as conn:
            rows = conn.execute(
                """
                SELECT p.*, COUNT(c.id) AS scanned_quantity
                FROM pallets p LEFT JOIN cartons c ON c.pallet_id = p.id
                GROUP BY p.id ORDER BY p.created_at DESC, p.id DESC LIMIT 100
                """
            ).fetchall()
        return [self._serialize_pallet(row, []) for row in rows]

    @staticmethod
    def _serialize_pallet(row: sqlite3.Row, cartons: list[sqlite3.Row]) -> dict:
        scanned = row["scanned_quantity"]
        expected = row["expected_quantity"]
        return {
            "code": row["code"], "volumetry_reference": row["volumetry_reference"],
            "expected_quantity": expected, "scanned_quantity": scanned,
            "difference": scanned - expected, "status": row["status"],
            "discrepancy_reason": row["discrepancy_reason"], "created_at": row["created_at"],
            "finished_at": row["finished_at"],
            "cartons": [{"code": carton["code"], "scanned_at": carton["scanned_at"]} for carton in cartons],
        }
