# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Testes da tela /reservas/predefinicoes — favoritos do PDV e sua ordem.

A ordem de exibição dos favoritos no PDV é a própria ordem do array
``ReservaPreferencia.pdv_favorite_item_ids`` (PdvService deriva o rank da
posição). A tela reordena as linhas no DOM (arrastar-e-soltar / setas) e o
formulário submete os IDs nessa ordem — estes testes travam esse contrato.
"""

from __future__ import annotations

import re

from app.core.models import AuditAction, AuditLog
from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.reservas import (
    BillingType,
    ReservaCategory,
    ReservaItem,
    ReservaPreferencia,
    ReservaSource,
)
from app.models.role import Role, RolePermission

URL = "/reservas/predefinicoes"


def _login(client, funcionario_id: int) -> None:
    with client.session_transaction() as sess:
        sess["funcionario_id"] = funcionario_id


def _cenario(app, *, nomes=("Day Use Inteira", "Camping Inteira", "Tirolesa")):
    """Cria operador com permissão + um venue/categoria e N itens ativos.

    Returns:
        Tupla (funcionario_id, [item_ids na ordem de `nomes`]).
    """
    with app.app_context():
        role = Role(nome="reservas_operador", ativo=True)
        _db.session.add(role)
        _db.session.flush()
        _db.session.add(RolePermission(role_id=role.id, modulo="reservas", acao="view"))

        funcionario = Funcionario(
            nome="Operador PDV", cpf="52998224725", role_id=role.id, ativo=True
        )
        source = ReservaSource(name="Cachoeira", display_name="Cachoeira", is_active=True)
        _db.session.add_all([funcionario, source])
        _db.session.flush()

        categoria = ReservaCategory(source_id=source.id, name="Day Use")
        _db.session.add(categoria)
        _db.session.flush()

        item_ids = []
        for nome in nomes:
            item = ReservaItem(
                category_id=categoria.id,
                name=nome,
                price=50,
                billing_type=BillingType.FIXED,
            )
            _db.session.add(item)
            _db.session.flush()
            item_ids.append(item.id)

        _db.session.commit()
        return funcionario.id, item_ids


def _favoritos_salvos(app, funcionario_id: int):
    with app.app_context():
        pref = (
            _db.session.query(ReservaPreferencia)
            .filter_by(funcionario_id=funcionario_id)
            .first()
        )
        return pref.pdv_favorite_item_ids if pref else None


def _post(client, favoritos):
    return client.post(URL, data={"pdv_favorite_item_ids": [str(i) for i in favoritos]})


class TestOrdemDosFavoritos:
    def test_salva_favoritos_na_ordem_de_envio(self, app, db, client):
        funcionario_id, ids = _cenario(app)
        _login(client, funcionario_id)

        # Ordem deliberadamente diferente da ordem de criação/alfabética
        resp = _post(client, [ids[2], ids[0], ids[1]])

        assert resp.status_code == 302
        assert _favoritos_salvos(app, funcionario_id) == [ids[2], ids[0], ids[1]]

    def test_reordenar_atualiza_ordem_persistida(self, app, db, client):
        funcionario_id, ids = _cenario(app)
        _login(client, funcionario_id)

        _post(client, [ids[0], ids[1], ids[2]])
        assert _favoritos_salvos(app, funcionario_id) == [ids[0], ids[1], ids[2]]

        _post(client, [ids[2], ids[1], ids[0]])
        assert _favoritos_salvos(app, funcionario_id) == [ids[2], ids[1], ids[0]]

    def test_duplicatas_sao_colapsadas_mantendo_a_primeira_posicao(self, app, db, client):
        funcionario_id, ids = _cenario(app)
        _login(client, funcionario_id)

        _post(client, [ids[0], ids[1], ids[0]])

        assert _favoritos_salvos(app, funcionario_id) == [ids[0], ids[1]]

    def test_ids_invalidos_sao_descartados(self, app, db, client):
        funcionario_id, ids = _cenario(app)
        _login(client, funcionario_id)

        resp = client.post(
            URL,
            data={"pdv_favorite_item_ids": [str(ids[0]), "999999", "abc", str(ids[1])]},
        )

        assert resp.status_code == 302
        assert _favoritos_salvos(app, funcionario_id) == [ids[0], ids[1]]

    def test_lista_vazia_limpa_os_favoritos(self, app, db, client):
        funcionario_id, ids = _cenario(app)
        _login(client, funcionario_id)

        _post(client, [ids[0], ids[1]])
        assert _favoritos_salvos(app, funcionario_id) == [ids[0], ids[1]]

        client.post(URL, data={})
        assert _favoritos_salvos(app, funcionario_id) == []

    def test_auditlog_registra_mudanca_de_ordem(self, app, db, client):
        funcionario_id, ids = _cenario(app)
        _login(client, funcionario_id)

        _post(client, [ids[0], ids[1]])
        _post(client, [ids[1], ids[0]])

        with app.app_context():
            logs = (
                _db.session.query(AuditLog)
                .filter_by(action=AuditAction.update, module="reservas")
                .all()
            )
            assert len(logs) == 2
            ultimo = logs[-1]
            assert ultimo.previous_state["pdv_favorite_item_ids"] == [ids[0], ids[1]]
            assert ultimo.new_state["pdv_favorite_item_ids"] == [ids[1], ids[0]]


class TestRenderizacao:
    def test_get_renderiza_favoritos_na_ordem_salva(self, app, db, client):
        funcionario_id, ids = _cenario(app)
        _login(client, funcionario_id)
        # "Tirolesa" é o último alfabeticamente; salvo em primeiro lugar,
        # precisa aparecer em primeiro na tela (e não em ordem alfabética).
        _post(client, [ids[2], ids[0], ids[1]])

        resp = client.get(URL)

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        renderizados = [
            int(v)
            for v in re.findall(r'name="pdv_favorite_item_ids" value="(\d*)"', html)
            if v
        ]
        assert renderizados == [ids[2], ids[0], ids[1]]

    def test_linhas_do_catalogo_sao_buscaveis_por_nome_local_categoria_e_codigo(
        self, app, db, client
    ):
        funcionario_id, ids = _cenario(app)
        _login(client, funcionario_id)

        html = client.get(URL).get_data(as_text=True)

        busca = re.search(
            r'data-item-id="%d"[^>]*data-busca="([^"]*)"' % ids[0], html, re.S
        )
        assert busca, "linha do catálogo sem data-busca"
        texto = busca.group(1)
        assert "day use inteira" in texto  # nome
        assert "cachoeira" in texto        # local
        assert "day use" in texto          # categoria
        assert str(ids[0]) in texto        # código

    def test_esconder_linha_do_catalogo_vence_o_display_do_tailwind(
        self, app, db, client
    ):
        """O filtro de busca esconde linhas via `hidden`, mas `.cat-row` usa a
        classe `flex` do Tailwind — mesma especificidade de `[hidden]` e vindo
        depois no CSS, o utilitário vencia e nada era escondido. A regra de
        reforço precisa continuar existindo."""
        funcionario_id, _ids = _cenario(app)
        _login(client, funcionario_id)

        html = client.get(URL).get_data(as_text=True)

        regra = re.search(
            r"\.cat-row\[hidden\][^{]*\{[^}]*display:\s*none\s*!important", html, re.S
        )
        assert regra, "faltou o reforço de [hidden] para as linhas do catálogo"

    def test_get_sem_favoritos_mostra_estado_vazio(self, app, db, client):
        funcionario_id, _ids = _cenario(app)
        _login(client, funcionario_id)

        resp = client.get(URL)

        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Nenhum favorito ainda" in html
        assert "0 favoritos" in html


class TestAcessoNegado:
    def test_sem_login_redireciona_para_portal_login(self, app, db, client):
        resp = client.get(URL)
        assert resp.status_code == 302
        assert "/portal/login" in resp.headers["Location"]
