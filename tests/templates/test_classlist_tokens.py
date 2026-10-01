# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Guarda de regressão: tokens de classe CSS passados a `classList`.

`DOMTokenList.add/remove/toggle` rejeitam a chamada inteira com
`InvalidCharacterError` quando **qualquer** token contém espaço — e, pela
spec, nenhuma classe chega a ser aplicada: a validação acontece antes de
qualquer mutação. O efeito prático é pior que um estilo errado: a exceção
aborta o resto do handler, então tudo que viria depois da linha (atualizar
contador, habilitar botão, disparar fetch) simplesmente não roda.

Foi assim que a replicação de descontos em `/reservas/admin/config/ajustes-preco`
quebrou: `classList.add(..., 'bg-blue-50 dark:bg-blue-900/30')` lançava a cada
clique, o contador de dias selecionados nunca subia e o botão "Replicar"
ficava permanentemente desabilitado.

Nenhum teste de backend pega isso — o bug vive no JS do template, que a
suíte pytest nunca executa. Esta verificação estática é o substituto barato:
lê os templates como texto e falha se algum literal de classe tiver espaço.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "app" / "templates"

# Início de uma chamada a classList.add/remove/toggle/replace
_INICIO_CHAMADA = re.compile(r"classList\.(?:add|remove|toggle|replace)\s*\(")

# Literal de string JS (aspas simples ou duplas), sem suporte a escape — os
# templates não usam aspas escapadas dentro de nomes de classe.
_LITERAL = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"")


def _corpo_da_chamada(texto: str, inicio: int) -> str:
    """Extrai os argumentos de uma chamada, do '(' até o ')' correspondente.

    Args:
        texto: Conteúdo completo do arquivo.
        inicio: Índice do '(' que abre a chamada.

    Returns:
        A string entre os parênteses (pode abranger várias linhas).
    """
    profundidade = 0
    for i in range(inicio, len(texto)):
        if texto[i] == "(":
            profundidade += 1
        elif texto[i] == ")":
            profundidade -= 1
            if profundidade == 0:
                return texto[inicio + 1:i]
    return texto[inicio + 1:]


def _tokens_invalidos(caminho: Path) -> list[tuple[int, str]]:
    """Lista tokens de classe com espaço num template.

    Args:
        caminho: Arquivo de template a inspecionar.

    Returns:
        Lista de (linha, token) para cada literal inválido encontrado.
    """
    texto = caminho.read_text(encoding="utf-8")
    achados: list[tuple[int, str]] = []

    for m in _INICIO_CHAMADA.finditer(texto):
        abre = m.end() - 1
        corpo = _corpo_da_chamada(texto, abre)
        for lit in _LITERAL.finditer(corpo):
            token = lit.group(1) if lit.group(1) is not None else lit.group(2)
            if token and token.strip() and " " in token.strip():
                linha = texto.count("\n", 0, abre) + 1
                achados.append((linha, token))

    return achados


def test_nenhum_token_de_classe_com_espaco():
    """Cada classe CSS deve ser um argumento próprio, nunca 'a b' num só token."""
    problemas: list[str] = []

    for template in sorted(TEMPLATES_DIR.rglob("*.html")):
        for linha, token in _tokens_invalidos(template):
            rel = template.relative_to(TEMPLATES_DIR.parents[1])
            problemas.append(f"{rel}:{linha} → {token!r}")

    assert not problemas, (
        "classList.add/remove/toggle recebeu token com espaço — a chamada inteira "
        "lança InvalidCharacterError e nenhuma classe é aplicada.\n"
        "Separe cada classe em seu próprio argumento: 'a b' → 'a', 'b'.\n"
        + "\n".join(problemas)
    )
