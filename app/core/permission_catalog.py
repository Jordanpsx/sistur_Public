# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Catálogo central de permissões RBAC do Sistur.

Este módulo é a fonte única para permissões exibidas no editor de roles,
seeds e dashboard. Permissões continuam armazenadas em
`RolePermission(modulo, acao)`, mas o catálogo evita listas divergentes.
"""

from __future__ import annotations

PERMISSOES_DO_SISTEMA: dict[str, list[str]] = {
    "dashboard": ["view"],
    # view_all = enxerga funcionários de todas as áreas (padrão audit.view/view_all);
    # sem view_all, supervisor não-super-admin fica restrito à própria área (D17)
    "funcionarios": ["view", "view_all", "create", "edit", "desativar"],
    "ponto": ["view", "create", "edit_admin"],
    "audit": ["view", "view_all"],
    "rh": ["view", "edit"],
    "folha_ponto": ["view", "edit", "deducao", "imprimir"],
    "qrcode": ["view", "regenerar"],
    "equipes": ["view", "create", "edit", "delete"],
    "funcoes": ["view", "create", "edit", "delete"],
    "escala": ["view", "edit"],
    "ajuste_ponto": ["aprovar"],
    "avisos": ["view", "receber_alertas"],
    "relatorios": ["view", "view_all", "imprimir"],
    "roles": [
        "manage_reservas",
        "manage_rh",
    ],
    # configurar = administrar o MÓDULO (telas /reservas/admin/config/*): status,
    # banimentos, ajustes de preço, avisos e o calendário de datas especiais.
    # Distinta de `edit`, que é mexer numa reserva de um cliente. Quem opera o
    # balcão não deveria poder mudar a tabela de preços do venue inteiro.
    "reservas": [
        "view",
        "view_contact",
        "view_financial",
        "view_payments",
        "create",
        "edit",
        "delete",
        "export",
        "configurar",
    ],
    "pdv": ["operate", "manage_shift", "view_reports"],
}

_LABELS_ACAO: dict[str, str] = {
    "view": "Visualizar",
    "view_all": "Visualizar Tudo",
    "view_contact": "Ver Contatos",
    "view_financial": "Ver Valores",
    "view_payments": "Ver Pagamentos",
    "create": "Criar",
    "edit": "Editar",
    "delete": "Excluir",
    "desativar": "Desativar",
    "edit_admin": "Editar (Admin)",
    "deducao": "Registrar Dedução",
    "imprimir": "Imprimir / Exportar PDF",
    "aprovar": "Aprovar",
    "regenerar": "Regenerar QR Code",
    "receber_alertas": "Receber Alertas",
    "manage_reservas": "Gerenciar Permissões de Reservas",
    "manage_rh": "Gerenciar Permissões de RH",
    "export": "Exportar",
    "operate": "Operar Caixa",
    "manage_shift": "Abrir/Fechar Turno",
    "view_reports": "Ver Relatórios de Caixa",
    "configurar": "Configurar Módulo",
}

# Rótulos que dependem do módulo. O slug de ação é compartilhado entre grupos
# (ex.: `configurar` existe em `ia` e em `reservas`), então quando o texto
# genérico de `_LABELS_ACAO` não descreve bem o caso, a especialização entra aqui.
_LABELS_ACAO_POR_MODULO: dict[tuple[str, str], str] = {
    ("reservas", "configurar"): "Configurar Módulo (preços, status, calendário)",
}

_LABELS_MODULO: dict[str, str] = {
    "dashboard": "Dashboard",
    "funcionarios": "Funcionários",
    "ponto": "Ponto Eletrônico",
    "audit": "Auditoria",
    "rh": "RH",
    "folha_ponto": "Folha de Ponto",
    "qrcode": "QR Code de Ponto",
    "equipes": "Equipes",
    "funcoes": "Funções",
    "ajuste_ponto": "Ajuste de Ponto",
    "avisos": "Avisos",
    "relatorios": "Relatórios",
    "roles": "Administração de Permissões",
    "reservas": "Reservas",
    "pdv": "PDV (Balcão)",
}


def label_modulo(modulo: str) -> str:
    """Retorna o rótulo legível de um módulo de permissão.

    Args:
        modulo: Slug do módulo (ex.: ``reservas``).

    Returns:
        Rótulo legível para UI; fallback para o próprio slug.
    """
    return _LABELS_MODULO.get(modulo, modulo)


def label_acao(acao: str, modulo: str | None = None) -> str:
    """Retorna o rótulo legível de uma ação de permissão.

    Quando `modulo` é informado, um rótulo específico do módulo tem precedência
    sobre o genérico — necessário porque o mesmo slug de ação aparece em grupos
    diferentes com significados diferentes (ex.: ``configurar``).

    Args:
        acao: Slug da ação (ex.: ``view_financial``).
        modulo: Slug do módulo, para desambiguar ações homônimas.

    Returns:
        Rótulo legível para UI; fallback para o próprio slug.
    """
    if modulo is not None:
        especifico = _LABELS_ACAO_POR_MODULO.get((modulo, acao))
        if especifico:
            return especifico
    return _LABELS_ACAO.get(acao, acao)


def permissoes_dashboard() -> dict[str, list[str]]:
    """Retorna as permissões relevantes para o mapa do dashboard do portal.

    Returns:
        Cópia superficial do catálogo para montagem de ``perms`` no template.
    """
    return {modulo: list(acoes) for modulo, acoes in PERMISSOES_DO_SISTEMA.items()}
