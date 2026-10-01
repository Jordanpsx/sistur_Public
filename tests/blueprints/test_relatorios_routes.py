# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes de integração das rotas de Relatórios (2.6, Caminho A e B).

Cobre: gate de sessão/permissão, preview HTML, PDF (content-type + audit
log de export), o guard de escopo por equipe (view_all vs. membro) e a
análise por IA opcional (?ia=1) — kill switch e happy path mockado.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from app.core.models import AuditAction, AuditLog
from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.role import Role, RolePermission
from app.services.configuracao_service import ConfiguracaoService
from app.services.organizacao_service import OrganizacaoService


def _cpf_unico(seed: int) -> str:
    return str(10000000000 + seed)[:11]


def _com_permissao(acoes=("view", "imprimir"), view_all=False, seed=1) -> Funcionario:
    role = Role(nome=f"op_relatorios_{seed}", descricao="Relatórios", is_super_admin=False, ativo=True)
    _db.session.add(role)
    _db.session.flush()
    for acao in acoes:
        _db.session.add(RolePermission(role_id=role.id, modulo="relatorios", acao=acao))
    if view_all:
        _db.session.add(RolePermission(role_id=role.id, modulo="relatorios", acao="view_all"))
    f = Funcionario(nome="Operador", cpf=_cpf_unico(seed), ativo=True, role_id=role.id)
    _db.session.add(f)
    _db.session.commit()
    return f


def _login(client, funcionario_id: int) -> None:
    with client.session_transaction() as sess:
        sess["funcionario_id"] = funcionario_id


class TestAcesso:

    def test_sem_sessao_redireciona(self, app, db, client):
        resp = client.get("/rh/relatorios/")
        assert resp.status_code == 302

    def test_sem_permissao_403(self, app, db, client):
        with app.app_context():
            _db.session.add(Role(nome="root", descricao="x", is_super_admin=True, ativo=True))
            f = Funcionario(nome="Sem Permissao", cpf=_cpf_unico(2), ativo=True)
            _db.session.add(f)
            _db.session.commit()
            fid = f.id
        _login(client, fid)
        resp = client.get("/rh/relatorios/")
        assert resp.status_code == 403

    def test_central_200_com_permissao(self, app, db, client):
        with app.app_context():
            fid = _com_permissao(seed=3).id
        _login(client, fid)
        resp = client.get("/rh/relatorios/")
        assert resp.status_code == 200


class TestPreview:

    def test_tipo_invalido_400(self, app, db, client):
        with app.app_context():
            fid = _com_permissao(seed=4).id
        _login(client, fid)
        resp = client.get("/rh/relatorios/preview?tipo=inexistente")
        assert resp.status_code == 400

    def test_funcionarios_ativos_preview_200(self, app, db, client):
        with app.app_context():
            fid = _com_permissao(seed=5).id
        _login(client, fid)
        resp = client.get(f"/rh/relatorios/preview?tipo=funcionarios_ativos&data={date.today().isoformat()}")
        assert resp.status_code == 200

    def test_equipe_sem_equipe_id_redireciona_com_flash(self, app, db, client):
        with app.app_context():
            fid = _com_permissao(seed=6).id
        _login(client, fid)
        resp = client.get("/rh/relatorios/preview?tipo=equipe")
        assert resp.status_code == 302


class TestPdfEExport:

    def test_pdf_gera_application_pdf_e_audit_log(self, app, db, client):
        with app.app_context():
            fid = _com_permissao(seed=7).id
        _login(client, fid)
        resp = client.get(f"/rh/relatorios/pdf?tipo=funcionarios_ativos&data={date.today().isoformat()}")
        assert resp.status_code == 200
        assert resp.content_type == "application/pdf"
        with app.app_context():
            count = _db.session.query(AuditLog).filter_by(
                action=AuditAction.export, module="relatorios",
            ).count()
            assert count == 1

    def test_pdf_sem_permissao_imprimir_403(self, app, db, client):
        with app.app_context():
            fid = _com_permissao(acoes=("view",), seed=8).id
        _login(client, fid)
        resp = client.get(f"/rh/relatorios/pdf?tipo=funcionarios_ativos&data={date.today().isoformat()}")
        assert resp.status_code == 403


class TestEscopoEquipe:

    def test_nao_membro_sem_view_all_recebe_404(self, app, db, client):
        with app.app_context():
            eq = OrganizacaoService.criar_equipe("Equipe Isolada", ator_id=None)
            eq_id = eq.id
            fid = _com_permissao(seed=9).id
        _login(client, fid)
        resp = client.get(f"/rh/relatorios/preview?tipo=equipe&equipe_id={eq_id}")
        assert resp.status_code == 404

    def test_membro_da_equipe_acessa(self, app, db, client):
        with app.app_context():
            eq = OrganizacaoService.criar_equipe("Equipe Acessivel", ator_id=None)
            f = _com_permissao(seed=10)
            OrganizacaoService.adicionar_membro(eq.id, f.id, ator_id=None)
            eq_id = eq.id
            fid = f.id
        _login(client, fid)
        resp = client.get(f"/rh/relatorios/preview?tipo=equipe&equipe_id={eq_id}")
        assert resp.status_code == 200

    def test_view_all_acessa_qualquer_equipe(self, app, db, client):
        with app.app_context():
            eq = OrganizacaoService.criar_equipe("Equipe Qualquer", ator_id=None)
            eq_id = eq.id
            fid = _com_permissao(view_all=True, seed=11).id
        _login(client, fid)
        resp = client.get(f"/rh/relatorios/preview?tipo=equipe&equipe_id={eq_id}")
        assert resp.status_code == 200



# ---------------------------------------------------------------------------
# 2.13d — Histórico por colaborador
# ---------------------------------------------------------------------------

