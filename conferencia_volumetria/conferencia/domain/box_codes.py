from __future__ import annotations

from collections import Counter
from hashlib import sha256


def normalize_caixa_estoque(value: object) -> str:
    """Keep a carton identifier textual; only outer whitespace is removed."""
    if value is None:
        return ""
    return str(value).strip()


def content_hash_caixa_estoque(values: list[object]) -> str:
    """Stable SHA-256 for the carton multiset, independent of row order."""
    counter = Counter(
        code for value in values if (code := normalize_caixa_estoque(value))
    )
    payload = "\n".join(
        f"{code}|{count}" for code, count in sorted(counter.items())
    )
    return sha256(payload.encode("utf-8")).hexdigest()
