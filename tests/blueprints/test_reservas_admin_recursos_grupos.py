# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Testes das rotas admin de Recursos Físicos e Agrupamentos (Reservas).

Cobre criação de ReservaResource, ReservaGroup (raiz e sub-grupo), o filtro
de venue na listagem e a pré-seleção/redirect de contexto adicionados para
a tela `/reservas/admin/recursos` (botões "Criar recurso aqui",
"Criar grupo filho" e "Novo Grupo").
"""

from __future__ import annotations

import re

from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.reservas import ReservaGroup, ReservaResource, ReservaSource
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


def _option_is_selected(html: str, value) -> bool:
    """Verifica se a <option value="value"> está marcada como selected,
    independente de como o Jinja formata o espaçamento em torno da tag."""
    match = re.search(rf'<option value="{value}"[^>]*>', html)
    assert match, f"option value={value!r} não encontrada no HTML"
    return "selected" in match.group(0)


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


class TestCriarRecurso:
    def test_cria_recurso_vinculado_a_grupo(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            source = _source()
            source_id = source.id
            grupo = ReservaGroup(name="Churrasqueiras", sources=[source])
            _db.session.add(grupo)
            _db.session.commit()
            grupo_id = grupo.id

        _login(client, funcionario_id)
        resp = client.post(
            "/reservas/admin/recursos/novo",
            data={
                "source_id": str(source_id),
                "name": "Churrasqueira A1",
                "group_id": str(grupo_id),
                "stock_quantity": "1",
            },
        )

        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/reservas/admin/recursos")
        with app.app_context():
            recurso = _db.session.query(ReservaResource).filter_by(name="Churrasqueira A1").first()
            assert recurso is not None
            assert recurso.group_id == grupo_id
            assert recurso.source_id == source_id
            assert recurso.stock_quantity == 1

    def test_cria_recurso_sem_grupo_ilimitado(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            source_id = _source().id

        _login(client, funcionario_id)
        resp = client.post(
            "/reservas/admin/recursos/novo",
            data={
                "source_id": str(source_id),
                "name": "Capacidade Day Use",
                "is_unlimited": "on",
            },
        )

        assert resp.status_code == 302
        with app.app_context():
            recurso = _db.session.query(ReservaResource).filter_by(name="Capacidade Day Use").first()
            assert recurso is not None
            assert recurso.group_id is None
            assert recurso.stock_quantity is None

    def test_form_get_prefila_grupo_e_source_da_query_string(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            source_id = _source().id
            grupo = ReservaGroup(name="Entradas")
            _db.session.add(grupo)
            _db.session.commit()
            grupo_id = grupo.id

        _login(client, funcionario_id)
        resp = client.get(f"/reservas/admin/recursos/novo?group_id={grupo_id}&source_id={source_id}")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert _option_is_selected(html, grupo_id)


class TestCriarGrupo:
    def test_cria_grupo_raiz(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            source_id = _source().id
        _login(client, funcionario_id)

        resp = client.post(
            "/reservas/admin/grupos/novo",
            data={"name": "Camping", "source_ids": [str(source_id)]},
        )

        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/reservas/admin/grupos")
        with app.app_context():
            grupo = _db.session.query(ReservaGroup).filter_by(name="Camping").first()
            assert grupo is not None
            assert grupo.parent_id is None
            assert [s.id for s in grupo.sources] == [source_id]

    def test_grupo_raiz_sem_local_retorna_erro(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        _login(client, funcionario_id)

        resp = client.post("/reservas/admin/grupos/novo", data={"name": "Camping"})

        assert resp.status_code == 400
        assert "Selecione ao menos um local" in resp.get_data(as_text=True)

    def test_cria_subgrupo_vinculado_ao_pai(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            pai = ReservaGroup(name="Churrasqueiras", sources=[_source()])
            _db.session.add(pai)
            _db.session.commit()
            pai_id = pai.id

        _login(client, funcionario_id)
        resp = client.post(
            "/reservas/admin/grupos/novo",
            data={"name": "Pequenas", "parent_id": str(pai_id)},
        )

        assert resp.status_code == 302
        with app.app_context():
            filho = _db.session.query(ReservaGroup).filter_by(name="Pequenas").first()
            assert filho is not None
            assert filho.parent_id == pai_id
            assert filho.display_path == "Churrasqueiras > Pequenas"
            assert filho.sources == []
            assert [s.name for s in filho.effective_sources] == ["Cachoeira"]

    def test_nome_duplicado_no_mesmo_escopo_retorna_erro(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            source_id = _source().id
            _db.session.add(ReservaGroup(name="Camping", sources=[_db.session.get(ReservaSource, source_id)]))
            _db.session.commit()

        _login(client, funcionario_id)
        resp = client.post(
            "/reservas/admin/grupos/novo",
            data={"name": "Camping", "source_ids": [str(source_id)]},
        )

        assert resp.status_code == 400
        assert "já existe" in resp.get_data(as_text=True)

    def test_get_prefila_parent_id_da_query_string(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            pai = ReservaGroup(name="Churrasqueiras", sources=[_source()])
            _db.session.add(pai)
            _db.session.commit()
            pai_id = pai.id

        _login(client, funcionario_id)
        resp = client.get(f"/reservas/admin/grupos/novo?parent_id={pai_id}&next=recursos")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert _option_is_selected(html, pai_id)
        assert '<input type="hidden" name="next" value="recursos" />' in html

    def test_next_recursos_redireciona_para_tela_de_recursos(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            source_id = _source().id
        _login(client, funcionario_id)

        resp = client.post(
            "/reservas/admin/grupos/novo",
            data={"name": "Camping", "next": "recursos", "source_ids": [str(source_id)]},
        )

        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/reservas/admin/recursos")

    def test_sem_next_redireciona_para_listagem_de_grupos(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            source_id = _source().id
        _login(client, funcionario_id)

        resp = client.post(
            "/reservas/admin/grupos/novo",
            data={"name": "Trilhas", "source_ids": [str(source_id)]},
        )

        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/reservas/admin/grupos")


class TestListarRecursosComFiltroDeVenue:
    def test_sem_filtro_mostra_recursos_de_todos_os_locais(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            s1 = _source("Cachoeira")
            s2 = _source("Vinhedo")
            _db.session.add_all([
                ReservaResource(source_id=s1.id, name="Recurso A", stock_quantity=1),
                ReservaResource(source_id=s2.id, name="Recurso B", stock_quantity=1),
            ])
            _db.session.commit()

        _login(client, funcionario_id)
        resp = client.get("/reservas/admin/recursos")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Recurso A" in html
        assert "Recurso B" in html

    def test_filtro_por_source_id_mostra_apenas_recursos_daquele_local(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            s1 = _source("Cachoeira")
            s2 = _source("Vinhedo")
            s1_id = s1.id
            _db.session.add_all([
                ReservaResource(source_id=s1.id, name="Recurso A", stock_quantity=1),
                ReservaResource(source_id=s2.id, name="Recurso B", stock_quantity=1),
            ])
            _db.session.commit()

        _login(client, funcionario_id)
        resp = client.get(f"/reservas/admin/recursos?source_id={s1_id}")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Recurso A" in html
        assert "Recurso B" not in html

    def test_filtro_por_source_id_mostra_apenas_grupos_daquele_local_ou_com_vinculo(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            s1 = _source("Cachoeira")
            s2 = _source("Vinhedo")
            s1_id = s1.id
            _db.session.add_all([
                ReservaGroup(name="Grupo Cachoeira", sources=[s1]),
                ReservaGroup(name="Grupo Vinhedo", sources=[s2]),
                ReservaGroup(name="Grupo Compartilhado", sources=[s1, s2]),
            ])
            _db.session.commit()

        _login(client, funcionario_id)
        resp = client.get(f"/reservas/admin/recursos?source_id={s1_id}")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Grupo Cachoeira" in html
        assert "Grupo Compartilhado" in html
        assert "Grupo Vinhedo" not in html

    def test_filtro_por_source_id_mostra_arvore_inteira_do_subgrupo_herdado(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            s1 = _source("Cachoeira")
            s2 = _source("Vinhedo")
            s1_id = s1.id
            pai = ReservaGroup(name="Grupo Cachoeira", sources=[s1])
            _db.session.add(pai)
            _db.session.commit()
            filho = ReservaGroup(name="Sub Cachoeira", parent_id=pai.id)
            _db.session.add(filho)
            _db.session.add(ReservaGroup(name="Grupo Vinhedo", sources=[s2]))
            _db.session.commit()

        _login(client, funcionario_id)
        resp = client.get(f"/reservas/admin/recursos?source_id={s1_id}")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Sub Cachoeira" in html
        assert "Grupo Vinhedo" not in html

    def test_botoes_criar_recurso_e_criar_grupo_filho_aparecem_por_grupo(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            grupo = ReservaGroup(name="Entradas")
            _db.session.add(grupo)
            _db.session.commit()
            grupo_id = grupo.id

        _login(client, funcionario_id)
        resp = client.get("/reservas/admin/recursos")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert f"/reservas/admin/recursos/novo?group_id={grupo_id}" in html
        assert f"/reservas/admin/grupos/novo?parent_id={grupo_id}&amp;next=recursos" in html


class TestAcessoNegado:
    def test_sem_login_redireciona_para_portal_login(self, app, db, client):
        resp = client.get("/reservas/admin/recursos")
        assert resp.status_code == 302
        assert "/portal/login" in resp.headers["Location"]

    def test_sem_permissao_create_retorna_403(self, app, db, client):
        with app.app_context():
            role = Role(nome="somente_view", ativo=True)
            _db.session.add(role)
            _db.session.flush()
            _db.session.add(RolePermission(role_id=role.id, modulo="reservas", acao="view"))
            _db.session.commit()
            funcionario = _funcionario(role)
            funcionario_id = funcionario.id

        _login(client, funcionario_id)
        resp = client.get("/reservas/admin/recursos")
        assert resp.status_code == 403
