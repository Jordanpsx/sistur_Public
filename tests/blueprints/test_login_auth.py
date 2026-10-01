# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes para autenticação por CPF + senha no Portal do Colaborador.

Cobre os cenários:
  - Login sem senha configurada → bloqueado com mensagem orientativa
  - Login com CPF incorreto → 401
  - Login com senha errada → 401
  - Login correto (CPF + senha) → 302 para dashboard
  - POST /portal/minha-senha → altera senha com verificação da atual
  - POST /portal/minha-senha → validações de erro (senha atual errada, senhas divergentes, curta demais)
"""

from __future__ import annotations

import pytest
from werkzeug.security import generate_password_hash

from app.core.models import AuditLog
from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.role import Role


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CPF_VALIDO = "52998224725"   # CPF fictício matematicamente válido


def _criar_funcionario(senha: str | None = None) -> Funcionario:
    """Cria e persiste um Funcionario ativo, com ou sem senha hash."""
    f = Funcionario(
        nome="Ana Teste",
        cpf=CPF_VALIDO,
        ativo=True,
        senha_hash=generate_password_hash(senha) if senha else None,
    )
    _db.session.add(f)
    _db.session.commit()
    return f


def _login(client, cpf: str = CPF_VALIDO, senha: str = "") -> object:
    return client.post(
        "/portal/login",
        data={"cpf": cpf, "senha": senha},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

class TestLoginComSenha:
    """Testes do fluxo de autenticação com CPF + senha."""

    def test_login_sem_senha_configurada_retorna_401(self, app, db, client):
        """Funcionário sem senha_hash não deve conseguir logar."""
        with app.app_context():
            _criar_funcionario(senha=None)

        resp = _login(client, senha="qualquer")
        assert resp.status_code == 401

    def test_login_sem_senha_configurada_exibe_mensagem_orientativa(self, app, db, client):
        """Mensagem deve instruir o operador a configurar senha via RH."""
        with app.app_context():
            _criar_funcionario(senha=None)

        resp = _login(client, senha="qualquer")
        assert b"ainda n\xc3\xa3o possui senha" in resp.data

    def test_login_cpf_inexistente_retorna_401(self, app, db, client):
        """CPF não cadastrado deve retornar 401."""
        resp = _login(client, cpf="11144477735", senha="qualquer")
        assert resp.status_code == 401

    def test_login_senha_errada_retorna_401(self, app, db, client):
        """Senha incorreta deve retornar 401."""
        with app.app_context():
            _criar_funcionario(senha="correta123")

        resp = _login(client, senha="errada999")
        assert resp.status_code == 401

    def test_login_senha_errada_mensagem_ambigua(self, app, db, client):
        """Erro de senha deve usar mensagem genérica para evitar enumeração de usuários."""
        with app.app_context():
            _criar_funcionario(senha="correta123")

        resp = _login(client, senha="errada")
        assert b"CPF ou senha incorretos" in resp.data

    def test_login_correto_redireciona_dashboard(self, app, db, client):
        """CPF + senha corretos devem redirecionar para /portal/dashboard."""
        with app.app_context():
            _criar_funcionario(senha="minha_senha_segura")

        resp = _login(client, senha="minha_senha_segura")
        assert resp.status_code == 302
        assert "/portal/dashboard" in resp.headers["Location"]

    def test_login_correto_cria_sessao(self, app, db, client):
        """Após login bem-sucedido, session deve conter funcionario_id."""
        with app.app_context():
            f = _criar_funcionario(senha="segura123")
            fid = f.id

        _login(client, senha="segura123")
        with client.session_transaction() as sess:
            assert sess.get("funcionario_id") == fid

    def test_login_correto_registra_audit_sucesso(self, app, db, client):
        """Login bem-sucedido deve gerar exatamente 1 AuditLog de login."""
        with app.app_context():
            f = _criar_funcionario(senha="segura123")
            fid = f.id

        _login(client, senha="segura123")

        with app.app_context():
            from app.core.models import AuditAction
            count = _db.session.query(AuditLog).filter_by(
                action=AuditAction.login, module="auth", user_id=fid
            ).count()
        assert count == 1

    def test_login_sem_cpf_retorna_400(self, app, db, client):
        """Requisição sem CPF deve retornar 400."""
        resp = client.post("/portal/login", data={"cpf": "", "senha": "abc"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Minha Senha
# ---------------------------------------------------------------------------

class TestMinhaSenha:
    """Testes do endpoint POST /portal/minha-senha."""

    def _autenticar(self, client, funcionario_id: int) -> None:
        with client.session_transaction() as sess:
            sess["funcionario_id"] = funcionario_id

    def test_alterar_senha_com_sucesso(self, app, db, client):
        """Funcionário autenticado pode trocar a senha com a atual correta."""
        with app.app_context():
            f = _criar_funcionario(senha="antiga123")
            fid = f.id

        self._autenticar(client, fid)
        resp = client.post(
            "/portal/minha-senha",
            data={
                "senha_atual":    "antiga123",
                "nova_senha":     "nova_segura456",
                "confirmar_senha": "nova_segura456",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302

        # Nova senha deve funcionar para login
        with client.session_transaction() as sess:
            sess.clear()
        resp2 = _login(client, senha="nova_segura456")
        assert resp2.status_code == 302

    def test_senha_atual_errada_retorna_erro(self, app, db, client):
        """Senha atual incorreta deve bloquear a alteração."""
        with app.app_context():
            f = _criar_funcionario(senha="certa123")
            fid = f.id

        self._autenticar(client, fid)
        resp = client.post(
            "/portal/minha-senha",
            data={
                "senha_atual":    "errada999",
                "nova_senha":     "nova456",
                "confirmar_senha": "nova456",
            },
            follow_redirects=True,
        )
        assert b"Senha atual incorreta" in resp.data

    def test_senhas_divergentes_retorna_erro(self, app, db, client):
        """Nova senha e confirmação divergentes devem ser rejeitadas."""
        with app.app_context():
            f = _criar_funcionario(senha="certa123")
            fid = f.id

        self._autenticar(client, fid)
        resp = client.post(
            "/portal/minha-senha",
            data={
                "senha_atual":    "certa123",
                "nova_senha":     "nova456",
                "confirmar_senha": "diferente789",
            },
            follow_redirects=True,
        )
        assert b"n\xc3\xa3o coincidem" in resp.data

    def test_senha_curta_retorna_erro(self, app, db, client):
        """Nova senha com menos de 6 caracteres deve ser rejeitada."""
        with app.app_context():
            f = _criar_funcionario(senha="certa123")
            fid = f.id

        self._autenticar(client, fid)
        resp = client.post(
            "/portal/minha-senha",
            data={
                "senha_atual":    "certa123",
                "nova_senha":     "ab",
                "confirmar_senha": "ab",
            },
            follow_redirects=True,
        )
        assert b"pelo menos 6" in resp.data

    def test_alterar_senha_registra_audit(self, app, db, client):
        """Troca de senha bem-sucedida deve gerar 1 AuditLog de update."""
        with app.app_context():
            f = _criar_funcionario(senha="antiga123")
            fid = f.id

        self._autenticar(client, fid)
        client.post(
            "/portal/minha-senha",
            data={
                "senha_atual":    "antiga123",
                "nova_senha":     "nova_segura456",
                "confirmar_senha": "nova_segura456",
            },
        )

        with app.app_context():
            count = _db.session.query(AuditLog).filter_by(
                action="update", module="funcionarios", entity_id=fid
            ).count()
        assert count == 1
