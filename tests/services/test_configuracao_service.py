# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes unitários para ConfiguracaoService.

Cobertura:
- get: retorna default se chave não cadastrada
- get: retorna o valor do _METADADOS como padrão para chaves conhecidas
- get: converte bool corretamente ('true'/'false')
- set: cria nova configuração e gera AuditLog com action=create
- set: atualiza configuração existente e gera AuditLog com action=update
- set: rejeita chave vazia com ValueError
- is_module_enabled: retorna True por padrão (chave sem registro)
- is_module_enabled: retorna False após set com False
- is_module_enabled: retorna True após set com True
- get_all: inclui todas as chaves conhecidas mesmo sem registros no banco
"""

import pytest

from app.core.models import AuditAction, AuditLog
from app.extensions import db as _db
from app.models.configuracoes import SettingType, SystemSetting
from app.services.configuracao_service import (
    CHAVE_BRANDING_EMPRESA_LOGO,
    CHAVE_BRANDING_EMPRESA_NOME,
    CHAVE_MODULO_PONTO,
    ConfiguracaoService,
)


# ---------------------------------------------------------------------------
# get — comportamento padrão
# ---------------------------------------------------------------------------

def test_get_retorna_default_se_chave_desconhecida(app, db):
    """get() com chave inexistente e sem metadados retorna o default fornecido."""
    with app.app_context():
        valor = ConfiguracaoService.get("chave.inexistente", default="fallback")
        assert valor == "fallback"


def test_get_retorna_none_se_sem_default(app, db):
    """get() sem default explícito retorna None para chaves desconhecidas."""
    with app.app_context():
        valor = ConfiguracaoService.get("outra.chave")
        assert valor is None


def test_get_retorna_padrao_de_metadados_para_chave_conhecida(app, db):
    """Chaves conhecidas retornam o padrão definido nos metadados quando não há registro."""
    with app.app_context():
        # Módulos são True por padrão (sem registro = ativo)
        assert ConfiguracaoService.get(CHAVE_MODULO_PONTO) is True
        # Branding é None por padrão (usa env var)
        assert ConfiguracaoService.get(CHAVE_BRANDING_EMPRESA_NOME) is None


def test_get_converte_bool_true(app, db):
    """get() deve retornar True para valores 'true', '1', 'yes', 'sim'."""
    with app.app_context():
        for v in ("true", "1", "yes", "sim", "True", "TRUE"):
            s = SystemSetting(chave=f"teste.bool_{v}", valor=v, tipo=SettingType.bool)
            _db.session.add(s)
            _db.session.commit()
            assert ConfiguracaoService.get(f"teste.bool_{v}") is True


def test_get_converte_bool_false(app, db):
    """get() deve retornar False para valores não reconhecidos como True."""
    with app.app_context():
        for v in ("false", "0", "no", "nao"):
            s = SystemSetting(chave=f"teste.boolfalse_{v}", valor=v, tipo=SettingType.bool)
            _db.session.add(s)
            _db.session.commit()
            assert ConfiguracaoService.get(f"teste.boolfalse_{v}") is False


def test_get_converte_string(app, db):
    """get() deve retornar string como str para tipo SettingType.string."""
    with app.app_context():
        s = SystemSetting(chave="branding.empresa_nome", valor="SISTUR Ltda", tipo=SettingType.string)
        _db.session.add(s)
        _db.session.commit()
        assert ConfiguracaoService.get("branding.empresa_nome") == "SISTUR Ltda"


def test_get_converte_int(app, db):
    """get() deve retornar valor inteiro para tipo SettingType.int."""
    with app.app_context():
        s = SystemSetting(chave="regras.tolerancia_minutos", valor="10", tipo=SettingType.int)
        _db.session.add(s)
        _db.session.commit()
        assert ConfiguracaoService.get("regras.tolerancia_minutos") == 10


# ---------------------------------------------------------------------------
# set — criação
# ---------------------------------------------------------------------------

def test_set_cria_nova_configuracao(app, db):
    """set() deve criar um registro em sistur_system_settings."""
    with app.app_context():
        ConfiguracaoService.set(CHAVE_MODULO_PONTO, False)
        s = _db.session.query(SystemSetting).filter_by(chave=CHAVE_MODULO_PONTO).first()
        assert s is not None
        assert s.valor == "false"
        assert s.tipo == SettingType.bool


def test_set_cria_gera_audit_create(app, db):
    """Criação de configuração nova deve gerar AuditLog com action=create."""
    with app.app_context():
        ConfiguracaoService.set(CHAVE_MODULO_PONTO, False, ator_id=None)
        count = (
            _db.session.query(AuditLog)
            .filter_by(action=AuditAction.create, module="configuracoes")
            .count()
        )
        assert count == 1


def test_set_rejeita_chave_vazia(app, db):
    """set() com chave vazia deve levantar ValueError."""
    with app.app_context():
        with pytest.raises(ValueError):
            ConfiguracaoService.set("", True)


# ---------------------------------------------------------------------------
# set — atualização
# ---------------------------------------------------------------------------

def test_set_atualiza_configuracao_existente(app, db):
    """set() sobre chave já existente deve atualizar o valor."""
    with app.app_context():
        ConfiguracaoService.set(CHAVE_MODULO_PONTO, True)
        ConfiguracaoService.set(CHAVE_MODULO_PONTO, False)

        s = _db.session.query(SystemSetting).filter_by(chave=CHAVE_MODULO_PONTO).first()
        assert s.valor == "false"
        # Deve haver apenas 1 registro (upsert, não duplicata)
        count = _db.session.query(SystemSetting).filter_by(chave=CHAVE_MODULO_PONTO).count()
        assert count == 1


def test_set_atualiza_gera_audit_update(app, db):
    """Atualização de configuração existente deve gerar AuditLog com action=update."""
    with app.app_context():
        ConfiguracaoService.set(CHAVE_MODULO_PONTO, True)
        ConfiguracaoService.set(CHAVE_MODULO_PONTO, False)

        update_count = (
            _db.session.query(AuditLog)
            .filter_by(action=AuditAction.update, module="configuracoes")
            .count()
        )
        assert update_count == 1


# ---------------------------------------------------------------------------
# is_module_enabled
# ---------------------------------------------------------------------------

def test_is_module_enabled_default_true(app, db):
    """Módulo sem configuração explícita deve ser considerado habilitado."""
    with app.app_context():
        assert ConfiguracaoService.is_module_enabled("ponto") is True
        assert ConfiguracaoService.is_module_enabled("reservas") is True


def test_is_module_enabled_quando_desativado(app, db):
    """Após set com False, is_module_enabled deve retornar False."""
    with app.app_context():
        ConfiguracaoService.set(CHAVE_MODULO_PONTO, False)
        assert ConfiguracaoService.is_module_enabled("ponto") is False


def test_is_module_enabled_quando_reativado(app, db):
    """Após desativar e reativar, is_module_enabled deve retornar True."""
    with app.app_context():
        ConfiguracaoService.set(CHAVE_MODULO_PONTO, False)
        ConfiguracaoService.set(CHAVE_MODULO_PONTO, True)
        assert ConfiguracaoService.is_module_enabled("ponto") is True


def test_is_module_enabled_modulo_desconhecido_retorna_true(app, db):
    """Módulo com identificador desconhecido retorna True por segurança."""
    with app.app_context():
        assert ConfiguracaoService.is_module_enabled("modulo_inexistente") is True


# ---------------------------------------------------------------------------
# get_all
# ---------------------------------------------------------------------------

def test_get_all_inclui_todas_chaves_conhecidas(app, db):
    """get_all() deve retornar um dict com todas as chaves dos _METADADOS."""
    with app.app_context():
        resultado = ConfiguracaoService.get_all()

        assert CHAVE_MODULO_PONTO in resultado
        assert CHAVE_BRANDING_EMPRESA_NOME in resultado
        assert CHAVE_BRANDING_EMPRESA_LOGO in resultado


def test_get_all_reflete_valores_salvos(app, db):
    """get_all() deve retornar o valor real para chaves com registro no banco."""
    with app.app_context():
        ConfiguracaoService.set(CHAVE_MODULO_PONTO, False)
        resultado = ConfiguracaoService.get_all()
        assert resultado[CHAVE_MODULO_PONTO] is False


def test_get_all_usa_padrao_para_chaves_sem_registro(app, db):
    """get_all() deve retornar o valor padrão para chaves sem registro no banco."""
    with app.app_context():
        resultado = ConfiguracaoService.get_all()
        # Módulos são True por padrão
        assert resultado[CHAVE_MODULO_PONTO] is True
        # Branding é None por padrão
        assert resultado[CHAVE_BRANDING_EMPRESA_NOME] is None


# ---------------------------------------------------------------------------
# 2.12 — Nomes de exibição dos módulos (white-label interno)
# ---------------------------------------------------------------------------

class TestNomesModulos:

    def test_padrao_sem_customizacao(self, app, db):
        from app.services.configuracao_service import ConfiguracaoService
        with app.app_context():
            assert ConfiguracaoService.get_nome_modulo("reservas") == "Reservas"
            assert ConfiguracaoService.get_nome_modulo("rh") == "Funcionários"

    def test_customizado_sobrepoe_padrao(self, app, db):
        from app.services.configuracao_service import ConfiguracaoService
        with app.app_context():
            ConfiguracaoService.set("modulo.reservas.nome", "Times", ator_id=None)
            assert ConfiguracaoService.get_nome_modulo("reservas") == "Times"

    def test_vazio_volta_ao_padrao(self, app, db):
        from app.services.configuracao_service import ConfiguracaoService
        with app.app_context():
            ConfiguracaoService.set("modulo.reservas.nome", "Times", ator_id=None)
            ConfiguracaoService.set("modulo.reservas.nome", None, ator_id=None)
            assert ConfiguracaoService.get_nome_modulo("reservas") == "Reservas"

    def test_modulo_desconhecido_capitaliza(self, app, db):
        from app.services.configuracao_service import ConfiguracaoService
        with app.app_context():
            assert ConfiguracaoService.get_nome_modulo("xyz") == "Xyz"

    def test_get_nomes_modulos_mescla(self, app, db):
        from app.services.configuracao_service import ConfiguracaoService
        with app.app_context():
            ConfiguracaoService.set("modulo.ponto.nome", "Relógio", ator_id=None)
            nomes = ConfiguracaoService.get_nomes_modulos()
            assert nomes["ponto"] == "Relógio"
            assert nomes["reservas"] == "Reservas"
            assert set(nomes) == set(ConfiguracaoService.NOMES_MODULOS_PADRAO)

    def test_set_audita(self, app, db):
        from app.core.models import AuditLog
        from app.services.configuracao_service import ConfiguracaoService
        with app.app_context():
            ConfiguracaoService.set("modulo.reservas.nome", "Times", ator_id=None)
            assert _db.session.query(AuditLog).filter_by(module="configuracoes").count() >= 1
