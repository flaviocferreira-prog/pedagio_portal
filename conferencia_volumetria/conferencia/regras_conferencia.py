from __future__ import annotations
from collections.abc import Iterable
from conferencia.domain.box_codes import normalize_caixa_estoque


CLASSE_MISTO = "MISTO"
CLASSE_NAO_INFORMADA = "NÃO INFORMADO"
OPERACAO_CROSS = "CROSS"
_OPERACOES_BLOQUEADAS_COM_MISTO = frozenset({"DIGITAL", "NIKESTORE"})
_NOMES_OPERACOES = {
    "DIGITAL": "DIGITAL",
    "NIKESTORE": "NIKE STORE",
}


def identificar_classe_palete(classes: Iterable[object]) -> str:
    """Retorna a classe única do palete ou a classificação operacional MISTO."""
    classes_validas = {
        normalize_caixa_estoque(classe).upper()
        for classe in classes
        if normalize_caixa_estoque(classe)
    }
    if not classes_validas:
        return CLASSE_NAO_INFORMADA
    if len(classes_validas) == 1:
        return next(iter(classes_validas))
    return CLASSE_MISTO


def mensagem_bloqueio_finalizacao_por_classe(
    operation: object, pallet_class: object
) -> str | None:
    """Centraliza a regra oficial de finalização para paletes de classe MISTO."""
    operacao = normalize_caixa_estoque(operation).upper()
    classe = normalize_caixa_estoque(pallet_class).upper()
    if classe != CLASSE_MISTO or operacao == OPERACAO_CROSS:
        return None
    if operacao in _OPERACOES_BLOQUEADAS_COM_MISTO:
        return (
            f"A operação {_NOMES_OPERACOES[operacao]} não permite finalizar palete "
            "com classe MISTO. O palete precisa possuir uma única classe."
        )
    return None
