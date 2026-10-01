# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes de integração das rotas da Timeline de Equipes (2.1b).

Cobre: acesso (sessão/permissão), render das três visões, agendar via POST,
cancelar via POST e o breakdown de composição (detalhe).
"""

from __future__ import annotations

from datetime import date, timedelta

from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.organizacao import EquipeAgendamento
from app.models.role import Role, RolePermission
from app.services.organizacao_service import OrganizacaoService


def _operador_com_permissao(acoes=("view", "edit")) -> Funcionario:
    role = Role(nome="op_equipes", descricao="Equipes", is_super_admin=False, ativo=True)
    _db.session.add(role)
    _db.session.flush()
    for acao in acoes:
        _db.session.add(RolePermission(role_id=role.id, modulo="equipes", acao=acao))
    f = Funcionario(nome="Operador", cpf="52998224725", ativo=True, role_id=role.id)
    _db.session.add(f)
    _db.session.commit()
    return f


def _login(client, funcionario_id: int) -> None:
    with client.session_transaction() as sess:
        sess["funcionario_id"] = funcionario_id


class TestTimelineAcesso:

    def test_sem_sessao_redireciona(self, app, db, client):
        resp = client.get("/rh/organizacao/equipes/timeline")
        assert resp.status_code == 302

    def test_sem_permissao_403(self, app, db, client):
        with app.app_context():
            # Super admin existente desliga o bootstrap mode (senão qualquer
            # funcionário sem role ganharia acesso total).
            _db.session.add(Role(nome="root", descricao="x", is_super_admin=True, ativo=True))
            f = Funcionario(nome="Sem Permissao", cpf="52998224725", ativo=True)
            _db.session.add(f)
            _db.session.commit()
            fid = f.id
        _login(client, fid)
        resp = client.get("/rh/organizacao/equipes/timeline")
        assert resp.status_code == 403

    def test_com_permissao_200_nas_tres_visoes(self, app, db, client):
        with app.app_context():
            fid = _operador_com_permissao().id
        _login(client, fid)
        for visao in ("dia", "semana", "mes"):
            resp = client.get(f"/rh/organizacao/equipes/timeline?visao={visao}")
            assert resp.status_code == 200

    def test_visao_invalida_cai_em_semana(self, app, db, client):
        with app.app_context():
            fid = _operador_com_permissao().id
        _login(client, fid)
        resp = client.get("/rh/organizacao/equipes/timeline?visao=xxx&data=lixo")
        assert resp.status_code == 200


class TestTimelineFluxo:

    def test_agendar_via_post_e_exibir(self, app, db, client):
        with app.app_context():
            fid = _operador_com_permissao().id
            eq = OrganizacaoService.criar_equipe("Equipe Web", ator_id=None)
            eq_id = eq.id
        _login(client, fid)

        inicio = date(2026, 7, 6)
        fim = date(2026, 7, 12)
        resp = client.post("/rh/organizacao/equipes/agendar", data={
            "equipe_id": str(eq_id),
            "data_inicio": inicio.isoformat(),
            "data_fim": fim.isoformat(),
            "observacao": "Semana teste",
        }, follow_redirects=False)
        assert resp.status_code == 302

        with app.app_context():
            ag = _db.session.query(EquipeAgendamento).filter_by(equipe_id=eq_id).one()
            assert ag.data_inicio == inicio

        resp = client.get(f"/rh/organizacao/equipes/timeline?visao=semana&data={inicio.isoformat()}")
        assert resp.status_code == 200
        assert "Equipe Web".encode() in resp.data

    def test_detalhe_composicao_renderiza(self, app, db, client):
        with app.app_context():
            fid = _operador_com_permissao().id
            eq = OrganizacaoService.criar_equipe("Equipe Detalhe", ator_id=None)
            membro = Funcionario(nome="Maria Souza", cpf="11144477735", ativo=True)
            _db.session.add(membro)
            _db.session.flush()
            OrganizacaoService.adicionar_membro(eq.id, membro.id, ator_id=None)
            ag = OrganizacaoService.agendar_equipe(
                eq.id, date(2026, 7, 6), date(2026, 7, 12), ator_id=None, notificar=False
            )
            ag_id = ag.id
        _login(client, fid)
        resp = client.get(
            f"/rh/organizacao/equipes/timeline?visao=semana&data=2026-07-06&detalhe={ag_id}"
        )
        assert resp.status_code == 200
        assert b"Maria" in resp.data

    def test_cancelar_via_post(self, app, db, client):
        with app.app_context():
            fid = _operador_com_permissao().id
            eq = OrganizacaoService.criar_equipe("Equipe Cancel", ator_id=None)
            ag = OrganizacaoService.agendar_equipe(
                eq.id, date(2026, 7, 6), date(2026, 7, 12), ator_id=None, notificar=False
            )
            ag_id = ag.id
        _login(client, fid)
        resp = client.post(
            f"/rh/organizacao/agendamentos/{ag_id}/cancelar",
            data={"data": "2026-07-06"},
        )
        assert resp.status_code == 302
        with app.app_context():
            assert _db.session.get(EquipeAgendamento, ag_id).deleted_at is not None

    def test_agendar_sem_permissao_edit_403(self, app, db, client):
        with app.app_context():
            fid = _operador_com_permissao(acoes=("view",)).id
        _login(client, fid)
        resp = client.post("/rh/organizacao/equipes/agendar", data={
            "equipe_id": "1", "data_inicio": "2026-07-06", "data_fim": "2026-07-12",
        })
        assert resp.status_code == 403
