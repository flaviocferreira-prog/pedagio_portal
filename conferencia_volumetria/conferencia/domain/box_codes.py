from __future__ import annotations

from hashlib import sha256
from unicodedata import category


def normalize_caixa_estoque(value: object) -> str:
    """Normaliza o identificador textual sem perder zeros à esquerda."""
    if value is None:
        return ""
    text = str(value)
    text = "".join(char for char in text if category(char) != "Cf")
    return text.strip().upper()


def content_hash_caixa_estoque(values: list[object]) -> str:
    """SHA-256 determinístico de todas as ocorrências de CAIXA_ESTOQUE."""
    normalized_codes = [code for value in values if (code := normalize_caixa_estoque(value))]
    normalized_codes.sort()
    return sha256("\n".join(normalized_codes).encode("utf-8")).hexdigest()
