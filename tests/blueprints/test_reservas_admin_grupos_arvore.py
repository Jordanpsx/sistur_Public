# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Testes da tela /reservas/admin/grupos (árvore, multi-local e herança).

Cobre a árvore recursiva de ReservaGroup, o filtro de venue, a exigência de
ao menos um local em grupos raiz, e a herança dinâmica de locais pelos
sub-grupos (que nunca guardam `sources` próprios — sempre herdam do
ancestral raiz via ReservaGroup.effective_sources).
"""

from __future__ import annotations

from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.reservas import ReservaGroup, ReservaSource
from app.models.role import Role, RolePermission


def _role_com_create() -> Role:
    role = Role(nome="reservas_admin", ativo=True)
    _db.session.add(role)
    _db.session.flush()
    _db.session.add(RolePermission(role_id=role.id, modulo="reservas", acao="create"))
    _db.session.commit()
    return role


def _funcionario(role: Role, cpf: str = "52998224725") -> Funcionario:
    funcionario = Funcionario(nome="Operador Reservas", cpf=cpf, role_id=role.id, ativo=True)
    _db.session.add(funcionario)
    _db.session.commit()
    return funcionario


def _login(client, funcionario_id: int) -> None:
    with client.session_transaction() as sess:
        sess["funcionario_id"] = funcionario_id


def _source(name: str = "Cachoeira") -> ReservaSource:
    source = ReservaSource(name=name, display_name=name, is_active=True)
    _db.session.add(source)
    _db.session.commit()
    return source


def _setup_operador(app, db, cpf: str = "52998224725"):
    with app.app_context():
        role = _role_com_create()
        funcionario = _funcionario(role, cpf)
        return funcionario.id


class TestCriarGrupoRaizMultiLocal:
    def test_cria_grupo_raiz_com_multiplos_locais(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            s1 = _source("Cachoeira")
            s2 = _source("Vinhedo")
            s1_id, s2_id = s1.id, s2.id
        _login(client, funcionario_id)

        resp = client.post(
            "/reservas/admin/grupos/novo",
            data={"name": "Esportes", "source_ids": [str(s1_id), str(s2_id)]},
        )

        assert resp.status_code == 302
        with app.app_context():
            grupo = _db.session.query(ReservaGroup).filter_by(name="Esportes").first()
            assert grupo is not None
            assert {s.id for s in grupo.sources} == {s1_id, s2_id}

    def test_grupo_raiz_sem_local_retorna_erro(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        _login(client, funcionario_id)

        resp = client.post("/reservas/admin/grupos/novo", data={"name": "Sem Local"})

        assert resp.status_code == 400
        assert "Selecione ao menos um local" in resp.get_data(as_text=True)
        with app.app_context():
            assert _db.session.query(ReservaGroup).filter_by(name="Sem Local").first() is None

    def test_subgrupo_criado_sem_campo_de_local_herda_do_pai(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            source = _source()
            pai = ReservaGroup(name="Churrasqueiras", sources=[source])
            _db.session.add(pai)
            _db.session.commit()
            pai_id = pai.id

        _login(client, funcionario_id)
        resp = client.post(
            "/reservas/admin/grupos/novo",
            data={"name": "Pequenas", "parent_id": str(pai_id), "source_ids": ["999"]},
        )

        assert resp.status_code == 302
        with app.app_context():
            filho = _db.session.query(ReservaGroup).filter_by(name="Pequenas").first()
            assert filho is not None
            assert filho.sources == []
            assert [s.name for s in filho.effective_sources] == ["Cachoeira"]

    def test_editar_reparenteia_grupo_raiz_para_subgrupo_limpa_sources_proprias(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            source = _source()
            pai = ReservaGroup(name="Ingressos", sources=[source])
            outro_root = ReservaGroup(name="Outro Raiz", sources=[source])
            _db.session.add_all([pai, outro_root])
            _db.session.commit()
            pai_id, outro_root_id = pai.id, outro_root.id

        _login(client, funcionario_id)
        resp = client.post(
            f"/reservas/admin/grupos/{outro_root_id}/editar",
            data={"name": "Outro Raiz", "parent_id": str(pai_id)},
        )

        assert resp.status_code == 302
        with app.app_context():
            atualizado = _db.session.get(ReservaGroup, outro_root_id)
            assert atualizado.parent_id == pai_id
            assert atualizado.sources == []
            assert [s.name for s in atualizado.effective_sources] == ["Cachoeira"]


class TestArvoreDeGrupos:
    def test_arvore_renderiza_subgrupo_aninhado(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            source = _source()
            pai = ReservaGroup(name="Churrasqueiras", sources=[source])
            _db.session.add(pai)
            _db.session.commit()
            filho = ReservaGroup(name="Pequenas", parent_id=pai.id)
            _db.session.add(filho)
            _db.session.commit()

        _login(client, funcionario_id)
        resp = client.get("/reservas/admin/grupos")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Churrasqueiras" in html
        assert "Pequenas" in html

    def test_filtro_por_local_mostra_apenas_raizes_vinculadas(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            s1 = _source("Cachoeira")
            s2 = _source("Vinhedo")
            s1_id = s1.id
            _db.session.add_all([
                ReservaGroup(name="Grupo Cachoeira", sources=[s1]),
                ReservaGroup(name="Grupo Vinhedo", sources=[s2]),
            ])
            _db.session.commit()

        _login(client, funcionario_id)
        resp = client.get(f"/reservas/admin/grupos?source_id={s1_id}")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Grupo Cachoeira" in html
        assert "Grupo Vinhedo" not in html

    def test_botao_criar_grupo_filho_aparece_por_grupo(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            grupo = ReservaGroup(name="Entradas", sources=[_source()])
            _db.session.add(grupo)
            _db.session.commit()
            grupo_id = grupo.id

        _login(client, funcionario_id)
        resp = client.get("/reservas/admin/grupos")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert f"/reservas/admin/grupos/novo?parent_id={grupo_id}" in html


class TestAcessoNegado:
    def test_sem_login_redireciona_para_portal_login(self, app, db, client):
        resp = client.get("/reservas/admin/grupos")
        assert resp.status_code == 302
        assert "/portal/login" in resp.headers["Location"]
