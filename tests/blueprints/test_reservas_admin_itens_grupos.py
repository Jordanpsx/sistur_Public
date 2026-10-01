# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Testes das rotas admin de Itens/Tarifas e Agrupamentos (Reservas).

Cobre a árvore recursiva de ReservaGroup em /reservas/admin/itens (mesmo padrão
de /reservas/admin/recursos), o filtro de venue via join em ReservaCategory
(ReservaItem não tem source_id direto), e a pré-seleção/redirect de contexto
adicionados para os botões "Criar item aqui", "Criar grupo filho" e "Novo Grupo".
"""

from __future__ import annotations

import re

from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.reservas import BillingType, ReservaCategory, ReservaGroup, ReservaItem, ReservaSource
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


def _category(source: ReservaSource, name: str = "Day Use") -> ReservaCategory:
    category = ReservaCategory(source_id=source.id, name=name)
    _db.session.add(category)
    _db.session.commit()
    return category


def _setup_operador(app, db, cpf: str = "52998224725"):
    with app.app_context():
        role = _role_com_create()
        funcionario = _funcionario(role, cpf)
        return funcionario.id


class TestCriarItem:
    def test_cria_item_vinculado_a_grupo(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            category = _category(_source())
            category_id = category.id
            grupo = ReservaGroup(name="Entradas")
            _db.session.add(grupo)
            _db.session.commit()
            grupo_id = grupo.id

        _login(client, funcionario_id)
        resp = client.post(
            "/reservas/admin/itens/novo",
            data={
                "category_id": str(category_id),
                "name": "Day Use Inteira",
                "group_id": str(grupo_id),
                "price": "150.00",
                "billing_type": "FIXED",
            },
        )

        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/reservas/admin/itens")
        with app.app_context():
            item = _db.session.query(ReservaItem).filter_by(name="Day Use Inteira").first()
            assert item is not None
            assert item.group_id == grupo_id
            assert item.category_id == category_id

    def test_form_get_prefila_grupo_da_query_string(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            _category(_source())
            grupo = ReservaGroup(name="Entradas")
            _db.session.add(grupo)
            _db.session.commit()
            grupo_id = grupo.id

        _login(client, funcionario_id)
        resp = client.get(f"/reservas/admin/itens/novo?group_id={grupo_id}")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert _option_is_selected(html, grupo_id)


class TestCriarGrupoParaItens:
    def test_get_prefila_parent_id_e_hidden_next_itens(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            pai = ReservaGroup(name="Ingressos")
            _db.session.add(pai)
            _db.session.commit()
            pai_id = pai.id

        _login(client, funcionario_id)
        resp = client.get(f"/reservas/admin/grupos/novo?parent_id={pai_id}&next=itens")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert _option_is_selected(html, pai_id)
        assert '<input type="hidden" name="next" value="itens" />' in html

    def test_next_itens_redireciona_para_tela_de_itens(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            source_id = _source().id
        _login(client, funcionario_id)

        resp = client.post(
            "/reservas/admin/grupos/novo",
            data={"name": "Ingressos", "next": "itens", "source_ids": [str(source_id)]},
        )

        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/reservas/admin/itens")

    def test_cria_subgrupo_a_partir_da_tela_de_itens(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            pai = ReservaGroup(name="Ingressos")
            _db.session.add(pai)
            _db.session.commit()
            pai_id = pai.id

        _login(client, funcionario_id)
        resp = client.post(
            "/reservas/admin/grupos/novo",
            data={"name": "Meia-Entrada", "parent_id": str(pai_id), "next": "itens"},
        )

        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/reservas/admin/itens")
        with app.app_context():
            filho = _db.session.query(ReservaGroup).filter_by(name="Meia-Entrada").first()
            assert filho is not None
            assert filho.parent_id == pai_id
            assert filho.display_path == "Ingressos > Meia-Entrada"

    def test_next_recursos_continua_funcionando_apos_generalizacao(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            source_id = _source().id
        _login(client, funcionario_id)

        resp = client.post(
            "/reservas/admin/grupos/novo",
            data={"name": "Churrasqueiras", "next": "recursos", "source_ids": [str(source_id)]},
        )

        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/reservas/admin/recursos")


class TestListarItensArvoreEFiltro:
    def test_arvore_renderiza_subgrupo_aninhado_dentro_do_grupo_pai(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            category = _category(_source())
            pai = ReservaGroup(name="Ingressos")
            _db.session.add(pai)
            _db.session.commit()
            filho = ReservaGroup(name="Meia-Entrada", parent_id=pai.id)
            _db.session.add(filho)
            _db.session.commit()
            item = ReservaItem(
                category_id=category.id,
                group_id=filho.id,
                name="Day Use Meia",
                price=75,
                billing_type=BillingType.FIXED,
            )
            _db.session.add(item)
            _db.session.commit()

        _login(client, funcionario_id)
        resp = client.get("/reservas/admin/itens")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Ingressos" in html
        assert "Meia-Entrada" in html
        assert "Day Use Meia" in html

    def test_sem_filtro_mostra_itens_de_todos_os_locais(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            cat1 = _category(_source("Cachoeira"), "Day Use")
            cat2 = _category(_source("Vinhedo"), "Day Use")
            _db.session.add_all([
                ReservaItem(category_id=cat1.id, name="Item A", price=10, billing_type=BillingType.FIXED),
                ReservaItem(category_id=cat2.id, name="Item B", price=20, billing_type=BillingType.FIXED),
            ])
            _db.session.commit()

        _login(client, funcionario_id)
        resp = client.get("/reservas/admin/itens")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Item A" in html
        assert "Item B" in html

    def test_filtro_por_source_id_usa_join_em_categoria(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            s1 = _source("Cachoeira")
            s2 = _source("Vinhedo")
            cat1 = _category(s1, "Day Use")
            cat2 = _category(s2, "Day Use")
            s1_id = s1.id
            _db.session.add_all([
                ReservaItem(category_id=cat1.id, name="Item A", price=10, billing_type=BillingType.FIXED),
                ReservaItem(category_id=cat2.id, name="Item B", price=20, billing_type=BillingType.FIXED),
            ])
            _db.session.commit()

        _login(client, funcionario_id)
        resp = client.get(f"/reservas/admin/itens?source_id={s1_id}")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Item A" in html
        assert "Item B" not in html

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
        resp = client.get(f"/reservas/admin/itens?source_id={s1_id}")

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
        resp = client.get(f"/reservas/admin/itens?source_id={s1_id}")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Sub Cachoeira" in html
        assert "Grupo Vinhedo" not in html

    def test_botoes_criar_item_e_criar_grupo_filho_aparecem_por_grupo(self, app, db, client):
        funcionario_id = _setup_operador(app, db)
        with app.app_context():
            grupo = ReservaGroup(name="Ingressos")
            _db.session.add(grupo)
            _db.session.commit()
            grupo_id = grupo.id

        _login(client, funcionario_id)
        resp = client.get("/reservas/admin/itens")

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert f"/reservas/admin/itens/novo?group_id={grupo_id}" in html
        assert f"/reservas/admin/grupos/novo?parent_id={grupo_id}&amp;next=itens" in html
