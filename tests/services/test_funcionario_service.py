# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Unit tests for FuncionarioService.

Test isolation: every test receives a fresh in-memory SQLite schema
via the autouse `db` fixture in conftest.py.  No production data is
ever read or written.

Coverage targets:
    - criar(): happy path, duplicate CPF, invalid CPF
    - atualizar(): happy path, not found, field whitelist enforcement
    - desativar(): happy path, not found, already inactive
    - AuditLog: every mutation must produce exactly one AuditLog row
"""

import pytest

from app.core.models import AuditLog, AuditAction
from app.services.funcionario_service import FuncionarioService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CPF_VALIDO   = "52998224725"   # passes check-digit algorithm
_CPF_VALIDO_2 = "11144477735"   # second valid CPF for uniqueness tests
_CPF_INVALIDO = "11111111111"   # all same digits — structurally invalid


def _count_audit(db, action: AuditAction, entity_id: int | None = None) -> int:
    """Count AuditLog rows matching action and optional entity_id."""
    q = db.session.query(AuditLog).filter_by(action=action)
    if entity_id is not None:
        q = q.filter_by(entity_id=entity_id)
    return q.count()


# ---------------------------------------------------------------------------
# criar()
# ---------------------------------------------------------------------------

class TestCriar:

    def test_cria_funcionario_com_sucesso(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(
                nome="Maria Souza",
                cpf=_CPF_VALIDO,
                cargo="Recepcionista",
            )
            assert f.id is not None
            assert f.nome == "Maria Souza"
            assert f.cpf == _CPF_VALIDO
            assert f.cargo == "Recepcionista"
            assert f.ativo is True

    def test_cria_com_cpf_formatado(self, app, db):
        """CPF with punctuation must be accepted and stored clean."""
        with app.app_context():
            f = FuncionarioService.criar(nome="João Silva", cpf="529.982.247-25")
            assert f.cpf == _CPF_VALIDO

    def test_cria_dispara_audit_log(self, app, db):
        """Auditoria obrigatória — criar() must produce one AuditLog(create)."""
        with app.app_context():
            f = FuncionarioService.criar(nome="Ana Lima", cpf=_CPF_VALIDO)
            count = _count_audit(db, AuditAction.create, entity_id=f.id)

        assert count == 1

    def test_cria_com_ator_registra_user_id_no_audit(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(
                nome="Carlos Ramos", cpf=_CPF_VALIDO, ator_id=99
            )
            log = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.create, entity_id=f.id)
                .first()
            )

        assert log.user_id == 99

    def test_cria_falha_cpf_duplicado(self, app, db):
        with app.app_context():
            FuncionarioService.criar(nome="Pedro Costa", cpf=_CPF_VALIDO)
            with pytest.raises(ValueError, match="já está cadastrado"):
                FuncionarioService.criar(nome="Outro Nome", cpf=_CPF_VALIDO)

    def test_cria_falha_cpf_invalido(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="CPF inválido"):
                FuncionarioService.criar(nome="Nome Qualquer", cpf=_CPF_INVALIDO)

    def test_cria_falha_nome_vazio(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="obrigatório"):
                FuncionarioService.criar(nome="", cpf=_CPF_VALIDO)

    def test_cria_remove_espacos_do_nome(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="  Luisa Fernanda  ", cpf=_CPF_VALIDO)
            assert f.nome == "Luisa Fernanda"

    def test_cria_defaults_jornada(self, app, db):
        """Default schedule must be 480 min work + 60 min lunch."""
        with app.app_context():
            f = FuncionarioService.criar(nome="Teste", cpf=_CPF_VALIDO)
            assert f.minutos_esperados_dia == 480
            assert f.minutos_almoco == 60


# ---------------------------------------------------------------------------
# atualizar()
# ---------------------------------------------------------------------------

class TestAtualizar:

    def _criar_funcionario(self, app, db):
        with app.app_context():
            return FuncionarioService.criar(nome="Original", cpf=_CPF_VALIDO)

    def test_atualiza_nome_e_cargo(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Original", cpf=_CPF_VALIDO)
            f = FuncionarioService.atualizar(
                f.id, {"nome": "Atualizado", "cargo": "Gerente"}
            )
            assert f.nome == "Atualizado"
            assert f.cargo == "Gerente"

    def test_atualiza_dispara_audit_log(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Original", cpf=_CPF_VALIDO)
            FuncionarioService.atualizar(f.id, {"cargo": "Novo Cargo"})
            count = _count_audit(db, AuditAction.update, entity_id=f.id)

        assert count == 1

    def test_audit_captura_estado_anterior(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Original", cpf=_CPF_VALIDO)
            FuncionarioService.atualizar(f.id, {"nome": "Novo Nome"})
            log = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.update, entity_id=f.id)
                .first()
            )

        assert log.previous_state["nome"] == "Original"
        assert log.new_state["nome"] == "Novo Nome"

    def test_atualiza_ignora_campos_nao_permitidos(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Alvo", cpf=_CPF_VALIDO)
            original_cpf = f.cpf
            FuncionarioService.atualizar(f.id, {"cpf": _CPF_VALIDO_2, "ativo": False})
            f_atualizado = FuncionarioService.buscar_por_id(f.id)

        # cpf and ativo are not in the whitelist — must not change
        assert f_atualizado.cpf == original_cpf
        assert f_atualizado.ativo is True

    def test_atualiza_falha_nao_encontrado(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrado"):
                FuncionarioService.atualizar(9999, {"nome": "X"})


# ---------------------------------------------------------------------------
# desativar()
# ---------------------------------------------------------------------------

class TestDesativar:

    def test_desativa_funcionario(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Ativo", cpf=_CPF_VALIDO)
            f = FuncionarioService.desativar(f.id)
            assert f.ativo is False

    def test_desativa_dispara_audit_log(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Ativo", cpf=_CPF_VALIDO)
            FuncionarioService.desativar(f.id)
            count = _count_audit(db, AuditAction.update, entity_id=f.id)

        assert count == 1

    def test_desativa_falha_ja_inativo(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Ativo", cpf=_CPF_VALIDO)
            FuncionarioService.desativar(f.id)
            with pytest.raises(ValueError, match="já está inativo"):
                FuncionarioService.desativar(f.id)

    def test_desativa_falha_nao_encontrado(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrado"):
                FuncionarioService.desativar(9999)


# ---------------------------------------------------------------------------
# reativar()
# ---------------------------------------------------------------------------

class TestReativar:

    def test_reativa_funcionario(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Inativo", cpf=_CPF_VALIDO)
            FuncionarioService.desativar(f.id)
            f = FuncionarioService.reativar(f.id)
            assert f.ativo is True

    def test_reativa_dispara_audit_log(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Inativo", cpf=_CPF_VALIDO)
            FuncionarioService.desativar(f.id)
            FuncionarioService.reativar(f.id, ator_id=1)
            # desativar + reativar = 2 AuditLog de update; queremos só o reativar (o último)
            logs = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.update, entity_id=f.id)
                .all()
            )
            assert len(logs) == 2
            ultimo = logs[-1]
            assert ultimo.previous_state["ativo"] is False
            assert ultimo.new_state["ativo"] is True

    def test_reativa_falha_ja_ativo(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Ativo", cpf=_CPF_VALIDO)
            with pytest.raises(ValueError, match="já está ativo"):
                FuncionarioService.reativar(f.id)

    def test_reativa_falha_nao_encontrado(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrado"):
                FuncionarioService.reativar(9999)


# ---------------------------------------------------------------------------
# definir_senha()
# ---------------------------------------------------------------------------

class TestDefinirSenha:

    def test_senha_hash_verificavel(self, app, db):
        from werkzeug.security import check_password_hash
        with app.app_context():
            f = FuncionarioService.criar(nome="Senha", cpf=_CPF_VALIDO)
            f = FuncionarioService.definir_senha(f.id, "SenhaSegura123")
            assert f.senha_hash is not None
            assert check_password_hash(f.senha_hash, "SenhaSegura123")

    def test_falha_senha_vazia(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Vazio", cpf=_CPF_VALIDO)
            with pytest.raises(ValueError):
                FuncionarioService.definir_senha(f.id, "")

    def test_audit_nao_expoe_senha(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Auditado", cpf=_CPF_VALIDO)
            FuncionarioService.definir_senha(f.id, "SenhaSegura123", ator_id=1)
            log = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.update, entity_id=f.id)
                .one()
            )
            prev = log.previous_state or {}
            novo = log.new_state or {}
            assert "SenhaSegura123" not in str(prev)
            assert "SenhaSegura123" not in str(novo)
            assert prev.get("senha_hash") == "[protegido]"
            assert novo.get("senha_hash") == "[redefinida]"


# ---------------------------------------------------------------------------
# regenerar_token_qr()
# ---------------------------------------------------------------------------

class TestRegenerarTokenQr:

    def test_token_muda(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="TokenQr", cpf=_CPF_VALIDO)
            token_antigo = f.token_qr
            f = FuncionarioService.regenerar_token_qr(f.id)
            assert f.token_qr is not None
            assert f.token_qr != token_antigo

    def test_audit_log_criado(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="TokenAudit", cpf=_CPF_VALIDO)
            FuncionarioService.regenerar_token_qr(f.id, ator_id=1)
            log = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.update, entity_id=f.id)
                .one()
            )
            assert "token_qr" in (log.new_state or {})

    def test_falha_nao_encontrado(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrado"):
                FuncionarioService.regenerar_token_qr(9999)


class TestDefinirTema:

    def test_define_dark(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Tema Dark", cpf=_CPF_VALIDO)
            FuncionarioService.definir_tema(f.id, "dark")
            assert db.session.get(type(f), f.id).tema_preferido == "dark"

    def test_define_light(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Tema Light", cpf=_CPF_VALIDO)
            FuncionarioService.definir_tema(f.id, "light")
            assert db.session.get(type(f), f.id).tema_preferido == "light"

    def test_define_none_volta_ao_padrao(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Tema Reset", cpf=_CPF_VALIDO)
            FuncionarioService.definir_tema(f.id, "dark")
            FuncionarioService.definir_tema(f.id, None)
            assert db.session.get(type(f), f.id).tema_preferido is None

    def test_nao_gera_audit_log(self, app, db):
        """Preferência cosmética não deve poluir a trilha de auditoria."""
        with app.app_context():
            f = FuncionarioService.criar(nome="Tema SemAudit", cpf=_CPF_VALIDO)
            FuncionarioService.definir_tema(f.id, "dark")
            count = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.update, entity_id=f.id)
                .count()
            )
            assert count == 0

    def test_falha_tema_invalido(self, app, db):
        with app.app_context():
            f = FuncionarioService.criar(nome="Tema Inv", cpf=_CPF_VALIDO)
            with pytest.raises(ValueError, match="tema inválido"):
                FuncionarioService.definir_tema(f.id, "roxo")

    def test_falha_nao_encontrado(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrado"):
                FuncionarioService.definir_tema(9999, "dark")
