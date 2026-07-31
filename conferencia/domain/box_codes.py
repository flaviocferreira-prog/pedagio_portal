from __future__ import annotations


def normalize_caixa_estoque(value: object) -> str:
    """Mantém o identificador textual do WMS sem qualquer conversão numérica."""
    if value is None:
        return ""
    return str(value).strip()
