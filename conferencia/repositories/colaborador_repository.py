from __future__ import annotations

import sqlite3

from conferencia.infrastructure.database import SQLiteDatabase


class ColaboradorRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def active_by_registration(self, registration: str) -> sqlite3.Row | None:
        with self.database.connection() as connection:
            return connection.execute(
                """
                SELECT id, matricula, nome, turno
                FROM colaboradores
                WHERE matricula = ? AND ativo = 1
                """,
                (registration,),
            ).fetchone()

    def by_registration(self, registration: str) -> sqlite3.Row | None:
        with self.database.connection() as connection:
            return connection.execute(
                "SELECT id, matricula, nome, turno, ativo FROM colaboradores WHERE matricula = ?",
                (registration,),
            ).fetchone()

    def create(self, registration: str, name: str, shift: str = "ADM") -> dict[str, int | str]:
        with self.database.connection(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO colaboradores(matricula, nome, turno, ativo) VALUES (?, ?, ?, 1)",
                (registration, name, shift),
            )
            return {"id": int(cursor.lastrowid), "matricula": registration, "nome": name, "turno": shift}

    def update(self, registration: str, name: str, shift: str) -> dict[str, int | str] | None:
        with self.database.connection(immediate=True) as connection:
            updated = connection.execute(
                "UPDATE colaboradores SET nome = ?, turno = ?, data_atualizacao = CURRENT_TIMESTAMP WHERE matricula = ?",
                (name, shift, registration),
            )
            if updated.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT id, matricula, nome, turno FROM colaboradores WHERE matricula = ?",
                (registration,),
            ).fetchone()
            return dict(row) if row is not None else None
