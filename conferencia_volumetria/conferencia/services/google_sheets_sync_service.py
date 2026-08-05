from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import socket
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

from conferencia.domain.box_codes import comparison_code_without_leading_zeros, normalize_caixa_estoque
from conferencia.domain.entities import CollaboratorContext, ConferenceStatus
from conferencia.infrastructure.database import SQLiteDatabase
from conferencia.infrastructure.settings import AppSettings
from conferencia.repositories.pallet_repository import PalletRepository
from conferencia.services.conference_service import ConflictError, NotFoundError, ValidationError


class GoogleSheetsSyncService:
    def __init__(self, database: SQLiteDatabase, settings: AppSettings) -> None:
        self.database = database
        self.settings = settings

    def prepare(
        self, public_id: str, collaborator: CollaboratorContext, return_url: str
    ) -> dict[str, object]:
        configuration_error: ConflictError | None = None
        with self.database.connection(immediate=True) as connection:
            pallet = self._finalized(connection, public_id)
            synchronization_status = str(pallet["status_sincronizacao"] or "PENDENTE")
            if synchronization_status == "SINCRONIZADO":
                return {
                    "already_synced": True,
                    "public_id": public_id,
                    "synchronized_at": PalletRepository._format_local_datetime(pallet["sincronizado_em"]),
                }
            configuration_error = self._configuration_error()
            if configuration_error is not None:
                self._record_failure(
                    connection,
                    pallet,
                    collaborator,
                    str(configuration_error),
                )
            elif synchronization_status == "SINCRONIZANDO" and not self._retry_is_available(
                pallet["sincronizacao_iniciada_em"]
            ):
                raise ConflictError(
                    "A sincronização desta conferência já está em andamento.",
                    code="SINCRONIZACAO_EM_ANDAMENTO",
                )
            else:
                payload = self._payload(connection, pallet)
                self._validate_payload(payload)
                payload_text = self.serialize_payload(payload)
                signature = self.sign(payload_text)
                application_origin = self._application_origin(return_url)
                attempt_id = f"SYNC-{secrets.token_urlsafe(18)}"
                nonce = secrets.token_urlsafe(32)
                popup_token = self.sign(
                    f"POPUP|{public_id}|{application_origin}|{return_url}|{nonce}"
                )
                now = self._now(connection)
                if synchronization_status == "SINCRONIZANDO":
                    connection.execute(
                        """
                        UPDATE synchronization_attempts
                        SET status = 'FAILED',
                            message = 'Tentativa anterior abandonada; reenvio autorizado.',
                            finished_at = ?
                        WHERE id = (
                            SELECT id FROM synchronization_attempts
                            WHERE pallet_id = ? AND status = 'PENDING'
                            ORDER BY id DESC LIMIT 1
                        )
                        """,
                        (now, pallet["id"]),
                    )
                connection.execute(
                    """
                    UPDATE pallets
                    SET status_sincronizacao = 'SINCRONIZANDO',
                        sincronizacao_iniciada_em = ?,
                        tentativas_sincronizacao = tentativas_sincronizacao + 1,
                        ultimo_erro_sincronizacao = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, pallet["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO synchronization_attempts(
                        pallet_id, collaborator_id, status, message, started_at,
                        attempt_id, conference_id, nonce, popup_token
                    ) VALUES (?, ?, 'PENDING', 'Envio preparado para o Google Sheets.', ?, ?, ?, ?, ?)
                    """,
                    (pallet["id"], collaborator.id, now, attempt_id, public_id, nonce, popup_token),
                )
        if configuration_error is not None:
            raise configuration_error
        return {
            "already_synced": False,
            "public_id": public_id,
            "attempt_id": attempt_id,
            "apps_script_url": self.settings.google_apps_script_url,
            "payload": payload_text,
            "signature": signature,
            "application_origin": application_origin,
            "return_url": return_url,
            "nonce": nonce,
            "popup_token": popup_token,
        }

    def confirm(
        self,
        public_id: str,
        status: str,
        synchronized_at: str,
        receipt_signature: str,
        attempt_id: str,
        nonce: str,
        already_synced: bool,
        collaborator: CollaboratorContext,
    ) -> dict[str, object]:
        self._validate_configuration()
        public_id = public_id.strip()
        status = status.strip()
        synchronized_at = synchronized_at.strip()
        receipt_signature = receipt_signature.strip().lower()
        if not public_id or status != "SINCRONIZADO" or not synchronized_at or not receipt_signature:
            raise ValidationError("O recibo de sincronização está incompleto.", code="RECIBO_INCOMPLETO")
        try:
            parsed = datetime.fromisoformat(synchronized_at)
        except ValueError as error:
            raise ValidationError("A data do recibo é inválida.", code="RECIBO_INVALIDO") from error
        if parsed.tzinfo is None:
            raise ValidationError("A data do recibo não possui fuso horário.", code="RECIBO_INVALIDO")
        signed_text = f"{public_id}|SINCRONIZADO|{synchronized_at}"
        if not hmac.compare_digest(self.sign(signed_text), receipt_signature):
            raise ValidationError("O recibo de sincronização é inválido.", code="ASSINATURA_RECIBO_INVALIDA")
        receipt = json.dumps(
            {
                "id_conferencia": public_id,
                "status": status,
                "sincronizado_em": synchronized_at,
                "assinatura_recibo": receipt_signature,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self.database.connection(immediate=True) as connection:
            pallet = self._finalized(connection, public_id)
            attempt = self._pending_attempt(connection, pallet["id"], attempt_id, public_id, nonce)
            connection.execute(
                """
                UPDATE pallets
                SET status_sincronizacao = 'SINCRONIZADO', sincronizado_em = ?,
                    recibo_sincronizacao = ?, ultimo_erro_sincronizacao = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (synchronized_at, receipt, self._now(connection), pallet["id"]),
            )
            connection.execute(
                """
                    UPDATE synchronization_attempts
                    SET status = 'SUCCESS', message = ?, finished_at = ?, error_code = NULL,
                        receipt = ?, result_consumed_at = ?, reconciled = ?
                    WHERE id = ?
                    """,
                (
                    "Reconciliação confirmada por recibo assinado." if already_synced else "Recibo assinado validado.",
                    self._now(connection), receipt, self._now(connection), int(already_synced), attempt["id"],
                ),
            )
        return {"public_id": public_id, "synchronized_at": synchronized_at, "attempt_id": attempt_id}

    def fail(
        self,
        public_id: str,
        error_code: str,
        attempt_id: str | None,
        nonce: str | None,
        collaborator: CollaboratorContext,
    ) -> dict[str, object]:
        messages = {
            "GOOGLE_POPUP_BLOCKED": "A janela auxiliar do Google foi bloqueada pelo navegador.",
            "GOOGLE_AUTH_REQUIRED": "A conta Google corporativa precisa ser autenticada.",
            "GOOGLE_DOMAIN_NOT_ALLOWED": "A conta Google não pertence ao domínio corporativo permitido.",
            "GOOGLE_APPS_SCRIPT_UNAVAILABLE": "O Google Apps Script está indisponível no momento.",
            "GOOGLE_PAYLOAD_INVALID": "Os dados enviados ao Google são inválidos.",
            "GOOGLE_SIGNATURE_INVALID": "A assinatura dos dados foi rejeitada pelo Google.",
            "GOOGLE_TOKEN_INVALID": "O token do popup foi rejeitado pelo Google.",
            "GOOGLE_WINDOW_CLOSED": "A janela auxiliar foi fechada antes da conclusão.",
            "GOOGLE_BRIDGE_TIMEOUT": "O Google Apps Script não respondeu no tempo esperado.",
            "GOOGLE_ORIGIN_INVALID": "A resposta veio de uma origem Google não autorizada.",
            "GOOGLE_RECEIPT_INVALID": "O recibo retornado pelo Google Apps Script é inválido.",
            "GOOGLE_CONFERENCE_ID_MISMATCH": "O resultado não pertence à conferência atual.",
            "GOOGLE_ATTEMPT_ID_MISMATCH": "O resultado não pertence à tentativa atual.",
            "GOOGLE_NONCE_MISMATCH": "A confirmação não corresponde à tentativa atual.",
            "GOOGLE_SHEETS_WRITE_FAILED": "O Google Sheets não concluiu a gravação.",
            "SYNC_RECONCILIATION_REQUIRED": "A conferência existente no Google Sheets possui dados incompatíveis.",
            "GOOGLE_SYNC_FAILED": "O Google Apps Script não concluiu a gravação.",
            "LOCAL_CONFIRMATION_FAILED": "O recibo retornado não pôde ser confirmado localmente.",
            "NETWORK_ERROR": "A conexão foi interrompida durante a sincronização.",
        }
        message = messages.get(error_code, "A sincronização não foi concluída.")
        with self.database.connection(immediate=True) as connection:
            pallet = self._finalized(connection, public_id)
            if pallet["status_sincronizacao"] == "SINCRONIZADO":
                return {"public_id": public_id, "status": "SINCRONIZADO"}
            now = self._now(connection)
            attempt = self._pending_attempt(connection, pallet["id"], attempt_id, public_id, nonce)
            connection.execute(
                """
                UPDATE pallets
                SET status_sincronizacao = 'PENDENTE',
                    ultimo_erro_sincronizacao = ?, updated_at = ?
                WHERE id = ?
                """,
                (message, now, pallet["id"]),
            )
            connection.execute(
                """
                    UPDATE synchronization_attempts
                    SET status = 'FAILED', message = ?, error_code = ?, finished_at = ?, result_consumed_at = ?
                    WHERE id = ?
                    """,
                (message, error_code, now, now, attempt["id"]),
            )
        return {"public_id": public_id, "status": "ERRO", "message": message}

    def _pending_attempt(
        self, connection: sqlite3.Connection, pallet_id: int, attempt_id: str | None,
        public_id: str, nonce: str | None,
    ) -> sqlite3.Row:
        if not attempt_id:
            raise ValidationError("A tentativa de sincronização não foi informada.", code="GOOGLE_ATTEMPT_ID_MISMATCH")
        attempt = connection.execute(
            "SELECT * FROM synchronization_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if attempt is None or attempt["pallet_id"] != pallet_id or attempt["conference_id"] != public_id:
            raise ValidationError("A tentativa não pertence à conferência atual.", code="GOOGLE_ATTEMPT_ID_MISMATCH")
        if not nonce or not hmac.compare_digest(str(attempt["nonce"] or ""), nonce):
            raise ValidationError("O nonce da tentativa é inválido.", code="GOOGLE_NONCE_MISMATCH")
        if attempt["status"] != "PENDING" or attempt["result_consumed_at"]:
            raise ConflictError("Esta tentativa de sincronização já foi concluída.", code="SYNC_ATTEMPT_ALREADY_CONSUMED")
        return attempt

    @staticmethod
    def serialize_payload(payload: dict[str, object]) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def sign(self, text: str) -> str:
        return hmac.new(
            self.settings.google_sync_secret.encode("utf-8"),
            text.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _payload(self, connection: sqlite3.Connection, pallet: sqlite3.Row) -> dict[str, object]:
        cartons = connection.execute(
            "SELECT id, code, ds_classe, status, confirmed_at FROM expected_cartons WHERE pallet_id = ? ORDER BY id",
            (pallet["id"],),
        ).fetchall()
        scans = connection.execute(
            "SELECT id, scanned_code, classification, scanned_at FROM scan_events WHERE pallet_id = ? ORDER BY id",
            (pallet["id"],),
        ).fetchall()
        extras = connection.execute(
            "SELECT code FROM unexpected_cartons WHERE pallet_id = ? ORDER BY id",
            (pallet["id"],),
        ).fetchall()
        first_match: dict[str, tuple[str, int]] = {}
        for order, row in enumerate(scans, 1):
            matched_code = normalize_caixa_estoque(row["scanned_code"])
            if row["classification"] == "MATCHED" and matched_code not in first_match:
                first_match[matched_code] = (str(row["scanned_at"]), order)
        expected = len(cartons)
        confirmed = sum(row["status"] == "CONFIRMED" for row in cartons)
        duplicates = sum(row["classification"] in ("DUPLICATE", "DUPLICATE_EXTRA") for row in scans)
        conference = {
            "id_conferencia": pallet["public_id"],
            "assinatura_arquivo": pallet["content_hash"] or "",
            "palete": pallet["code"],
            "nome_arquivo": pallet["source_filename"],
            "matricula": pallet["collaborator_registration"] or pallet["collaborator_id"] or "",
            "colaborador": pallet["collaborator_name"] or "",
            "turno": pallet["imported_shift"] or "",
            "importado_em": PalletRepository._format_local_datetime(pallet["imported_at"]) or "",
            "iniciado_em": PalletRepository._format_local_datetime(pallet["started_at"]) or "",
            "finalizado_em": PalletRepository._format_local_datetime(pallet["finished_at"]) or "",
            "duracao_segundos": int(pallet["duration_seconds"] or 0),
            "total_esperadas": expected,
            "total_bipagens": len(scans),
            "total_ok": confirmed,
            "total_faltas": max(0, expected - confirmed),
            "total_sobras": len(extras),
            "total_duplicadas": duplicates,
            "status_conferencia": str(pallet["conference_status"]),
            "computador": socket.gethostname(),
        }
        box_payload = []
        for sequence, row in enumerate(cartons, 1):
            code = normalize_caixa_estoque(row["code"])
            matched_at, scan_order = first_match.get(code, ("", 0))
            box_payload.append(
                {
                    "id_item": f"{pallet['public_id']}-C{row['id']}",
                    "id_conferencia": pallet["public_id"],
                    "sequencia": sequence,
                    "codigo_original": code,
                    "codigo_normalizado": comparison_code_without_leading_zeros(code),
                    "ds_classe": normalize_caixa_estoque(row["ds_classe"]).upper() or "NÃO INFORMADO",
                    "status_final": row["status"],
                    "bipado_em": PalletRepository._format_local_datetime(
                        matched_at or row["confirmed_at"]
                    ) or "",
                    "ordem_bipagem": scan_order,
                }
            )
        scan_payload = []
        for order, row in enumerate(scans, 1):
            code = normalize_caixa_estoque(row["scanned_code"])
            scan_payload.append(
                {
                    "id_evento": f"{pallet['public_id']}-B{row['id']}",
                    "id_conferencia": pallet["public_id"],
                    "ordem": order,
                    "codigo_lido": code,
                    "codigo_normalizado": comparison_code_without_leading_zeros(code),
                    "resultado": row["classification"],
                    "bipado_em": PalletRepository._format_local_datetime(row["scanned_at"]) or "",
                }
            )
        return {"conferencia": conference, "caixas": box_payload, "bipagens": scan_payload}

    @staticmethod
    def _validate_payload(payload: dict[str, object]) -> None:
        conference = payload.get("conferencia")
        cartons = payload.get("caixas")
        scans = payload.get("bipagens")
        if not isinstance(conference, dict):
            raise ConflictError(
                "Os dados principais da conferência não estão disponíveis.",
                code="PAYLOAD_SINCRONIZACAO_INVALIDO",
            )
        public_id = conference.get("id_conferencia")
        if not isinstance(public_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", public_id) is None:
            raise ConflictError(
                "O identificador da conferência é inválido.",
                code="PAYLOAD_SINCRONIZACAO_INVALIDO",
            )
        if conference.get("status_conferencia") != "FINALIZADA":
            raise ConflictError(
                "Somente conferências finalizadas podem ser sincronizadas.",
                code="CONFERENCIA_NAO_FINALIZADA",
            )
        if not isinstance(cartons, list) or not cartons:
            raise ConflictError(
                "As caixas da conferência não estão disponíveis.",
                code="PAYLOAD_SINCRONIZACAO_INVALIDO",
            )
        if not isinstance(scans, list) or not scans:
            raise ConflictError(
                "As bipagens da conferência não estão disponíveis.",
                code="PAYLOAD_SINCRONIZACAO_INVALIDO",
            )

    def pending_summary(self) -> dict[str, int]:
        with self.database.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM pallets WHERE conference_status = 'FINALIZADA' AND status_sincronizacao = 'PENDENTE'"
            ).fetchone()[0]
        return {"pending": int(count)}

    def prepare_next_pending(
        self, collaborator: CollaboratorContext, return_url: str
    ) -> dict[str, object]:
        self.recover_stale_attempts()
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT public_id FROM pallets
                WHERE conference_status = 'FINALIZADA' AND status_sincronizacao = 'PENDENTE'
                ORDER BY finished_at, id
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return {"prepared": None, **self.pending_summary()}
        prepared = self.prepare(str(row["public_id"]), collaborator, return_url)
        return {"prepared": prepared, **self.pending_summary()}

    def recover_stale_attempts(self) -> int:
        with self.database.connection(immediate=True) as connection:
            now = self._now(connection)
            threshold = connection.execute(
                "SELECT datetime('now', ?) AS value", (f"-{self.settings.sync_retry_after_seconds} seconds",)
            ).fetchone()["value"].replace(" ", "T") + "Z"
            rows = connection.execute(
                """
                SELECT id FROM pallets
                WHERE conference_status = 'FINALIZADA' AND status_sincronizacao = 'SINCRONIZANDO'
                  AND (sincronizacao_iniciada_em IS NULL OR sincronizacao_iniciada_em < ?)
                """,
                (threshold,),
            ).fetchall()
            if not rows:
                return 0
            pallet_ids = [row["id"] for row in rows]
            marks = ",".join("?" for _ in pallet_ids)
            connection.execute(
                f"UPDATE pallets SET status_sincronizacao = 'PENDENTE', ultimo_erro_sincronizacao = ?, updated_at = ? WHERE id IN ({marks})",
                ("Tentativa anterior interrompida; reenvio manual disponível.", now, *pallet_ids),
            )
            connection.execute(
                f"UPDATE synchronization_attempts SET status = 'FAILED', message = ?, error_code = 'SYNC_TIMEOUT', finished_at = ? WHERE pallet_id IN ({marks}) AND status = 'PENDING'",
                ("Tentativa anterior interrompida; reenvio manual disponível.", now, *pallet_ids),
            )
            return len(pallet_ids)

    def _finalized(
        self, connection: sqlite3.Connection, public_id: str
    ) -> sqlite3.Row:
        pallet = connection.execute(
            """
            SELECT p.*, c.matricula AS collaborator_registration, c.nome AS collaborator_name
            FROM pallets p
            LEFT JOIN colaboradores c ON c.id = p.created_by_collaborator_id
            WHERE p.public_id = ?
            """,
            (public_id,),
        ).fetchone()
        if pallet is None:
            raise NotFoundError("Conferência não encontrada.")
        if pallet["conference_status"] != ConferenceStatus.FINISHED:
            raise ConflictError(
                "Somente conferências finalizadas podem ser sincronizadas.",
                code="CONFERENCIA_NAO_FINALIZADA",
            )
        return pallet

    def _retry_is_available(self, started_at: object) -> bool:
        if not isinstance(started_at, str) or not started_at:
            return True
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            now = datetime.now(started.tzinfo)
        except ValueError:
            return True
        return (now - started).total_seconds() >= self.settings.sync_retry_after_seconds

    def _validate_configuration(self) -> None:
        error = self._configuration_error()
        if error is not None:
            raise error

    def _configuration_error(self) -> ConflictError | None:
        if re.fullmatch(
            r"https://script\.google\.com/a/macros/fisia\.com\.br/s/[A-Za-z0-9_-]+/exec",
            self.settings.google_apps_script_url,
        ) is None:
            return ConflictError(
                "A URL de sincronização não está configurada.", code="SINCRONIZACAO_NAO_CONFIGURADA"
            )
        if not self.settings.google_sync_secret:
            return ConflictError(
                "A chave de sincronização não está configurada.", code="SINCRONIZACAO_NAO_CONFIGURADA"
            )
        return None

    @staticmethod
    def _application_origin(return_url: str) -> str:
        parsed = urlparse(return_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.path != "/sincronizacao/confirmar/"
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ConflictError(
                "A origem da aplicação é inválida.",
                code="ORIGEM_SINCRONIZACAO_INVALIDA",
            )
        return f"{parsed.scheme}://{parsed.netloc}"

    def _record_failure(
        self,
        connection: sqlite3.Connection,
        pallet: sqlite3.Row,
        collaborator: CollaboratorContext,
        message: str,
    ) -> None:
        now = self._now(connection)
        connection.execute(
            """
            UPDATE pallets
            SET status_sincronizacao = 'PENDENTE',
                sincronizacao_iniciada_em = ?,
                tentativas_sincronizacao = tentativas_sincronizacao + 1,
                ultimo_erro_sincronizacao = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, message, now, pallet["id"]),
        )
        connection.execute(
            """
            INSERT INTO synchronization_attempts(
                pallet_id, collaborator_id, status, message, started_at, finished_at, error_code
            ) VALUES (?, ?, 'FAILED', ?, ?, ?, 'GOOGLE_APPS_SCRIPT_UNAVAILABLE')
            """,
            (pallet["id"], collaborator.id, message, now, now),
        )

    @staticmethod
    def _now(connection: sqlite3.Connection) -> str:
        return connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now')").fetchone()[0]
