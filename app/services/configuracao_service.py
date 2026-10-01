# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
ConfiguracaoService — lógica de negócio para configurações globais do sistema.

Auditoria obrigatória: toda mutação chama AuditService.
Arquitetura em camadas: sem imports do Flask — puro Python, compatível com Cython.

Chaves conhecidas (constantes):
    CHAVE_MODULO_*    → toggles de habilitação de módulos (bool, default True)
    CHAVE_BRANDING_*  → substituições de identidade visual (string, default None)
    CHAVE_EMPRESA_*   → dados cadastrais da empresa para Folha de Ponto (string, default None)
"""

from __future__ import annotations

from typing import Any

from app.core.audit import AuditService
from app.extensions import db
from app.models.configuracoes import SettingType, SystemSetting
from app.services.base import BaseService


# ---------------------------------------------------------------------------
# Chaves conhecidas
# ---------------------------------------------------------------------------

CHAVE_MODULO_PONTO = "modulo.ponto"
CHAVE_BRANDING_EMPRESA_NOME = "branding.empresa_nome"
CHAVE_BRANDING_EMPRESA_LOGO = "branding.empresa_logo"
CHAVE_EMPRESA_RAZAO_SOCIAL  = "empresa.razao_social"
CHAVE_EMPRESA_CNPJ          = "empresa.cnpj"
CHAVE_EMPRESA_ENDERECO      = "empresa.endereco"
CHAVE_MP_ACCESS_TOKEN       = "mercadopago.access_token"
CHAVE_MP_PUBLIC_KEY         = "mercadopago.public_key"
CHAVE_SYSTEM_TIMEZONE       = "system.timezone"

# Metadados descritivos por chave — usados para popular a UI de configurações
_METADADOS: dict[str, dict] = {
    CHAVE_MODULO_PONTO: {
        "tipo": SettingType.bool,
        "descricao": "Habilita o módulo de Ponto Eletrônico para todos os colaboradores.",
        "padrao": True,
    },
    CHAVE_BRANDING_EMPRESA_NOME: {
        "tipo": SettingType.string,
        "descricao": "Nome da empresa exibido no portal. Deixe vazio para usar a variável de ambiente COMPANY_NAME.",
        "padrao": None,
    },
    CHAVE_BRANDING_EMPRESA_LOGO: {
        "tipo": SettingType.string,
        "descricao": "URL do logotipo da empresa. Deixe vazio para usar a variável de ambiente COMPANY_LOGO.",
        "padrao": None,
    },
    CHAVE_EMPRESA_RAZAO_SOCIAL: {
        "tipo": SettingType.string,
        "descricao": "Razão Social da empresa (para Folha de Ponto).",
        "padrao": None,
    },
    CHAVE_EMPRESA_CNPJ: {
        "tipo": SettingType.string,
        "descricao": "CNPJ da empresa para exibição no cabeçalho da Folha de Ponto (formato: XX.XXX.XXX/XXXX-XX).",
        "padrao": None,
    },
    CHAVE_EMPRESA_ENDERECO: {
        "tipo": SettingType.string,
        "descricao": "Endereço completo da empresa para exibição no cabeçalho da Folha de Ponto.",
        "padrao": None,
    },
    CHAVE_MP_ACCESS_TOKEN: {
        "tipo": SettingType.string,
        "descricao": "Access Token do Mercado Pago (começa com APP_USR- ou TEST-). Usado para processar pagamentos no servidor.",
        "padrao": None,
    },
    CHAVE_MP_PUBLIC_KEY: {
        "tipo": SettingType.string,
        "descricao": "Public Key do Mercado Pago (começa com APP_USR- ou TEST-). Usada pelo frontend para inicializar o MP Bricks.",
        "padrao": None,
    },
    CHAVE_SYSTEM_TIMEZONE: {
        "tipo": SettingType.string,
        "descricao": "Fuso horário do sistema (ex: America/Sao_Paulo). Usado em todos os módulos para exibição e agendamento.",
        "padrao": "America/Sao_Paulo",
    },
}

# Campos capturados nos snapshots de auditoria
_AUDIT_FIELDS = ["id", "chave", "valor", "tipo"]


class ConfiguracaoService(BaseService):
    """
    Gerencia configurações globais armazenadas na tabela sistur_system_settings.

    Todas as mutações são auditadas e o método set() é idempotente
    (cria ou atualiza conforme necessário).

    Todos os métodos são estáticos — sem estado de instância — para
    compatibilidade com Cython e importação sem efeitos colaterais.
    """

    # Módulos gerenciados pelo master switch
    MODULOS = ["ponto", "reservas"]

    # Módulos com nome de exibição configurável (2.12) — chave interna: rótulo padrão.
    # A chave interna nunca muda (permissões/rotas/auditoria estáveis); só o rótulo.
    NOMES_MODULOS_PADRAO = {
        "rh": "Funcionários",
        "ponto": "Ponto Eletrônico",
        "reservas": "Reservas",
        "avisos": "Notificações",
    }

    # ------------------------------------------------------------------
    # Queries (somente leitura)
    # ------------------------------------------------------------------

    @staticmethod
    def get(chave: str, default: Any = None) -> Any:
        """
        Retorna o valor da configuração convertido para o tipo nativo.

        Conversão por tipo:
            bool   → True se valor in ('true', '1', 'yes'), False caso contrário
            int    → int(valor)
            string → str(valor)

        Args:
            chave:   Chave da configuração (e.g. "modulo.ponto").
            default: Valor retornado se a chave não existir no banco.

        Returns:
            Valor convertido ao tipo nativo da configuração, ou 'default'
            se a chave não estiver cadastrada.
        """
        setting = (
            db.session.query(SystemSetting).filter_by(chave=chave).first()
        )
        if setting is None or setting.valor is None:
            # Tenta o padrão definido nos metadados antes de usar o default do caller
            meta = _METADADOS.get(chave)
            if meta is not None:
                return meta["padrao"]
            return default

        return ConfiguracaoService._converter(setting.valor, setting.tipo)

    @staticmethod
    def get_all() -> dict[str, Any]:
        """
        Retorna todas as configurações cadastradas como dicionário.

        Chaves sem registro no banco são incluídas com seus valores padrão
        definidos em _METADADOS.

        Returns:
            Dicionário {chave: valor_tipado} com todas as configurações
            conhecidas do sistema.
        """
        # Carrega todas as rows do banco em um único query
        rows = {s.chave: s for s in db.session.query(SystemSetting).all()}

        result: dict[str, Any] = {}
        for chave, meta in _METADADOS.items():
            if chave in rows:
                s = rows[chave]
                result[chave] = ConfiguracaoService._converter(s.valor, s.tipo)
            else:
                result[chave] = meta["padrao"]

        return result

    @staticmethod
    def is_module_enabled(modulo: str) -> bool:
        """
        Retorna True se o módulo estiver habilitado no sistema.

        Módulos sem configuração explícita são considerados habilitados
        por padrão para não quebrar sistemas recém-instalados.

        Args:
            modulo: Identificador do módulo (e.g. "ponto", "reservas").

        Returns:
            True se o módulo estiver ativo, False se desabilitado.
        """
        chave = f"modulo.{modulo}"
        valor = ConfiguracaoService.get(chave, default=True)
        # Garante bool mesmo se o banco retornar algo inesperado
        return bool(valor)

    @staticmethod
    def get_nome_modulo(modulo: str, default: str | None = None) -> str:
        """Retorna o nome de exibição configurado de um módulo (2.12).

        Args:
            modulo:  Chave interna do módulo (e.g. "ponto").
            default: Rótulo usado se não houver nome customizado nem padrão
                     conhecido. None usa o catálogo NOMES_MODULOS_PADRAO.

        Returns:
            Nome de exibição — o customizado pelo super admin, ou o padrão.
        """
        padrao = default or ConfiguracaoService.NOMES_MODULOS_PADRAO.get(
            modulo, modulo.capitalize()
        )
        valor = ConfiguracaoService.get(f"modulo.{modulo}.nome")
        if isinstance(valor, str) and valor.strip():
            return valor.strip()
        return padrao

    @staticmethod
    def get_nomes_modulos() -> dict[str, str]:
        """Dicionário {modulo: nome_exibicao} de todos os módulos renomeáveis.

        Uma única query (LIKE em ``modulo.%.nome``) — usado pelo context
        processor em toda requisição.

        Returns:
            Dict com todos os módulos de NOMES_MODULOS_PADRAO, aplicando o
            nome customizado quando existir e não estiver vazio.
        """
        rows = (
            db.session.query(SystemSetting)
            .filter(SystemSetting.chave.like("modulo.%.nome"))
            .all()
        )
        customizados: dict[str, str] = {}
        for s in rows:
            partes = s.chave.split(".")
            if len(partes) == 3 and s.valor and s.valor.strip():
                customizados[partes[1]] = s.valor.strip()

        return {
            modulo: customizados.get(modulo, padrao)
            for modulo, padrao in ConfiguracaoService.NOMES_MODULOS_PADRAO.items()
        }

    # ------------------------------------------------------------------
    # Mutações
    # ------------------------------------------------------------------

    @staticmethod
    def set(
        chave: str,
        valor: Any,
        ator_id: int | None = None,
    ) -> SystemSetting:
        """
        Cria ou atualiza uma configuração e registra o evento no log de auditoria.

        Se a chave já existir, o valor é atualizado (upsert).
        Se for uma chave nova, a linha é criada com o tipo inferido dos metadados
        ou como SettingType.string por padrão.

        Args:
            chave:   Chave da configuração (e.g. "modulo.ponto").
            valor:   Novo valor (qualquer tipo; será serializado como string).
            ator_id: ID do funcionário que está realizando a alteração.
                     None para ações de sistema.

        Returns:
            Instância de SystemSetting persistida.

        Raises:
            ValueError: Se a chave for vazia.
        """
        BaseService._require(chave, "chave")

        valor_str = ConfiguracaoService._serializar(valor)
        meta = _METADADOS.get(chave, {})
        tipo = meta.get("tipo", SettingType.string)
        descricao = meta.get("descricao")

        existing = (
            db.session.query(SystemSetting).filter_by(chave=chave).first()
        )

        if existing:
            # Atualização
            snapshot_antes = BaseService._snapshot(existing, _AUDIT_FIELDS)
            existing.valor = valor_str
            existing.tipo = tipo
            db.session.flush()

            AuditService.log_update(
                "configuracoes",
                entity_id=existing.id,
                previous_state=snapshot_antes,
                new_state=BaseService._snapshot(existing, _AUDIT_FIELDS),
                actor_id=ator_id,
            )
            db.session.commit()
            return existing

        # Criação
        setting = SystemSetting(
            chave=chave,
            valor=valor_str,
            tipo=tipo,
            descricao=descricao,
        )
        db.session.add(setting)
        db.session.flush()

        AuditService.log_create(
            "configuracoes",
            entity_id=setting.id,
            new_state=BaseService._snapshot(setting, _AUDIT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()
        return setting

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    @staticmethod
    def _converter(valor_str: str | None, tipo: SettingType) -> Any:
        """
        Converte o valor textual do banco para o tipo Python nativo.

        Args:
            valor_str: String armazenada no banco.
            tipo:      Tipo de destino (SettingType enum).

        Returns:
            Valor convertido: bool, int ou str conforme o tipo.
        """
        if valor_str is None:
            return None
        if tipo == SettingType.bool:
            return valor_str.lower() in ("true", "1", "yes", "sim")
        if tipo == SettingType.int:
            try:
                return int(valor_str)
            except (ValueError, TypeError):
                return 0
        return str(valor_str)

    @staticmethod
    def _serializar(valor: Any) -> str | None:
        """
        Serializa um valor Python para string antes de gravar no banco.

        Args:
            valor: Valor a serializar (bool, int, str ou None).

        Returns:
            Representação em string do valor, ou None se o valor for None.
        """
        if valor is None:
            return None
        if isinstance(valor, bool):
            return "true" if valor else "false"
        return str(valor)
