# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Mascaramento de documentos pessoais para trilhas de auditoria.

O CPF completo é guardado apenas onde há finalidade que o exija — emissão
fiscal, cobrança e conferência de banimento, todas em ``sistur_reservas`` e
``sistur_funcionarios``. A trilha de auditoria tem outra finalidade: mostrar
*o que mudou*. Para isso o prefixo basta, e guardar o número inteiro apenas
multiplicaria as cópias do dado (LGPD, Art. 6º, III — necessidade).

Este módulo não importa Flask nem toca no banco: ``BaseService`` precisa
seguir compilável via Cython.
"""

from __future__ import annotations

import re
from typing import Any

# Campos cujo valor é documento pessoal e deve ser mascarado ao virar snapshot.
CAMPOS_DOCUMENTO: frozenset[str] = frozenset(
    {
        "customer_document",
        "cpf",
        "documento",
    }
)

_NAO_DIGITO = re.compile(r"\D+")

# Dígitos revelados no início do documento. Três, e não os seis do meio que o
# mercado usa: mascarar os verificadores não protege nada (são deriváveis dos
# nove primeiros), e exibi-los junto de dígitos ocultos serve de filtro para
# quem tentar reconstruir o número.
_PREFIXO_VISIVEL = 3


def normalizar_documento(valor: Any) -> str:
    """Reduz um documento a seus dígitos.

    Args:
        valor: Documento em qualquer formatação, ou None.

    Returns:
        Somente os dígitos; string vazia se não houver nenhum.
    """
    if valor is None:
        return ""
    return _NAO_DIGITO.sub("", str(valor))


def mascarar_documento(valor: Any) -> Any:
    """Mascara um documento preservando os três primeiros dígitos.

    Um CPF vira ``123.***.***-**``. Documentos de outro comprimento — CNPJ de
    reserva corporativa, passaporte, RG — caem no formato conservador
    ``123*****``, que não revela sequer o tamanho original.

    Args:
        valor: Documento a mascarar, ou None.

    Returns:
        Documento mascarado. ``None`` e string vazia passam intactos, para não
        transformar ausência de dado em dado.

    Example:
        >>> mascarar_documento("529.982.247-25")
        '529.***.***-**'
    """
    if valor is None or valor == "":
        return valor

    digitos = normalizar_documento(valor)
    if not digitos:
        return valor

    prefixo = digitos[:_PREFIXO_VISIVEL]
    if len(digitos) == 11:
        return f"{prefixo}.***.***-**"
    return f"{prefixo}{'*' * 5}"


def mascarar_estado(estado: dict[str, Any] | None) -> dict[str, Any] | None:
    """Aplica o mascaramento aos campos de documento de um snapshot.

    Args:
        estado: Dict destinado a ``AuditLog.previous_state`` / ``new_state``.

    Returns:
        O mesmo dict com os campos de ``CAMPOS_DOCUMENTO`` mascarados. Retorna
        None se receber None.
    """
    if not estado:
        return estado
    for campo in CAMPOS_DOCUMENTO & estado.keys():
        estado[campo] = mascarar_documento(estado[campo])
    return estado
