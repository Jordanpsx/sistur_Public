# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Testes do endpoint de criação de reserva pelo site público.

O que estes testes protegem:
  - a chave de serviço realmente barrar quem não a tem;
  - CPF, e-mail e telefone serem validados antes de virar linha no banco;
  - IDs de fonte/categoria não poderem ser cruzados entre venues;
  - a divergência de preço do ``criar`` continuar chegando ao cliente como 400.
"""

from __future__ import annotations

import pytest

from app.models.reservas import (
    BillingType,
    Reserva,
    ReservaCategory,
    ReservaItem,
    ReservaOrigin,
    ReservaSource,
    ReservaStatus,
)
from app.extensions import db as _db

CHAVE = "chave-de-teste"


@pytest.fixture
def cenario(app, db, monkeypatch):
    """Cria um venue com uma categoria e um item de preço conhecido."""
    monkeypatch.setattr(
        "app.blueprints.api_public.routes._WEB_API_KEY", CHAVE, raising=False
    )
    with app.app_context():
        fonte = ReservaSource(name="Cachoeira Teste", is_active=True)
        _db.session.add(fonte)
        _db.session.flush()

        cat = ReservaCategory(source_id=fonte.id, name="Day use")
        outra_fonte = ReservaSource(name="Vinhedo Teste", is_active=True)
        _db.session.add_all([cat, outra_fonte])
        _db.session.flush()

        cat_outra = ReservaCategory(source_id=outra_fonte.id, name="Enoturismo")
        item = ReservaItem(
            category_id=cat.id,
            name="Inteira",
            price=100,
            billing_type=BillingType.FIXED,
        )
        _db.session.add_all([cat_outra, item])
        _db.session.commit()

        yield {
            "source_id": fonte.id,
            "category_id": cat.id,
            "category_outra": cat_outra.id,
            "item_id": item.id,
        }


def _payload(c, **over):
    base = {
        "source_id": c["source_id"],
        "category_id": c["category_id"],
        "customer_name": "Maria Aparecida Souza",
        "customer_document": "52998224725",
        "email": "maria@exemplo.com.br",
        "telefone": "(64) 99999-1234",
        "check_in_date": "2027-01-10",
        "check_out_date": "2027-01-10",
        "items": [{"item_id": c["item_id"], "quantity": 2, "price_override": 200.0}],
    }
    base.update(over)
    return base


def _post(client, corpo, chave=CHAVE):
    headers = {"X-Web-Api-Key": chave} if chave is not None else {}
    return client.post("/api/public/reservas/criar", json=corpo, headers=headers)


class TestAutenticacao:
    def test_sem_chave(self, app, db, client, cenario):
        assert _post(client, _payload(cenario), chave=None).status_code == 401

    def test_chave_errada(self, app, db, client, cenario):
        assert _post(client, _payload(cenario), chave="errada").status_code == 401

    def test_servico_sem_chave_configurada(self, app, db, client, cenario, monkeypatch):
        """Sem WEB_API_KEY no ambiente o endpoint recusa em vez de liberar geral."""
        monkeypatch.setattr(
            "app.blueprints.api_public.routes._WEB_API_KEY", "", raising=False
        )
        assert _post(client, _payload(cenario)).status_code == 503


class TestValidacao:
    @pytest.mark.parametrize(
        "campo,valor",
        [
            ("customer_name", "Maria"),            # só primeiro nome
            ("customer_document", "11111111111"),  # dígitos repetidos
            ("customer_document", "52998224726"),  # dígito verificador errado
            ("customer_document", ""),             # CPF é obrigatório na web
            ("email", "maria@exemplo"),            # sem TLD
            ("email", "sem-arroba.com"),
            ("telefone", "1234"),                  # curto demais
            ("check_in_date", "10/01/2027"),       # formato errado
            ("items", []),                         # nada selecionado
        ],
    )
    def test_rejeita(self, app, db, client, cenario, campo, valor):
        r = _post(client, _payload(cenario, **{campo: valor}))
        assert r.status_code == 400, f"{campo}={valor!r} deveria ser recusado"
        assert r.get_json()["erro"]

    def test_nada_e_gravado_quando_invalido(self, app, db, client, cenario):
        _post(client, _payload(cenario, customer_document="11111111111"))
        with app.app_context():
            assert db.session.query(Reserva).count() == 0

    def test_categoria_de_outro_venue(self, app, db, client, cenario):
        """Cruzar IDs não pode criar a reserva no lugar errado."""
        r = _post(client, _payload(cenario, category_id=cenario["category_outra"]))
        assert r.status_code == 404
        assert "não pertence" in r.get_json()["erro"]

    def test_fonte_inexistente(self, app, db, client, cenario):
        assert _post(client, _payload(cenario, source_id=99999)).status_code == 404


class TestCriacao:
    def test_happy_path(self, app, db, client, cenario):
        r = _post(client, _payload(cenario))
        assert r.status_code == 201, r.get_data(as_text=True)
        corpo = r.get_json()
        assert corpo["group_id"] and corpo["reserva_id"]
        assert corpo["total"] == pytest.approx(200.0)

        with app.app_context():
            reserva = db.session.get(Reserva, corpo["reserva_id"])
            assert reserva.customer_name == "Maria Aparecida Souza"
            assert reserva.customer_document == "52998224725"
            assert reserva.origin.name == "WEB"
            assert reserva.status.name == "PENDING"
            # Soft lock — é o que solta a vaga sozinho se ninguém pagar.
            assert reserva.expires_at is not None

    def test_contato_vai_para_metadata(self, app, db, client, cenario):
        """E-mail e telefone não têm coluna: vivem em metadata_info.

        As chaves seguem as que o plugin do WordPress já grava, para os
        relatórios existentes não precisarem de um segundo caso.
        """
        r = _post(client, _payload(cenario))
        with app.app_context():
            reserva = db.session.get(Reserva, r.get_json()["reserva_id"])
            assert reserva.metadata_info["email"] == "maria@exemplo.com.br"
            assert reserva.metadata_info["telefone"] == "64999991234"
            assert reserva.metadata_info["origem_canal"] == "site"

    def test_cpf_formatado_e_aceito(self, app, db, client, cenario):
        r = _post(client, _payload(cenario, customer_document="529.982.247-25"))
        assert r.status_code == 201
        with app.app_context():
            reserva = db.session.get(Reserva, r.get_json()["reserva_id"])
            assert reserva.customer_document == "52998224725"

    def test_divergencia_de_preco_e_recusada(self, app, db, client, cenario):
        """A anti-fraude do criar() precisa chegar ao cliente como 400.

        Cobre o caso real de a tabela mudar entre a cotação e o envio.
        """
        r = _post(
            client,
            _payload(
                cenario,
                items=[
                    {"item_id": cenario["item_id"], "quantity": 2, "price_override": 10.0}
                ],
            ),
        )
        assert r.status_code == 400
        assert "Divergência de preço" in r.get_json()["erro"]
        with app.app_context():
            assert db.session.query(Reserva).count() == 0


class TestPoolCompartilhado:
    """Dois ingressos da mesma reserva dividindo um recurso de capacidade.

    Regressão de um bug real: o grupo "Capacidade Day Use" tem um único recurso
    de estoque ilimitado, e Inteira e Meia-Entrada o dividem por projeto (modelo
    de 3 camadas, CLAUDE.md). A auto-seleção excluía recursos já vinculados no
    mesmo payload, então o segundo ingresso não achava nada e a reserva mais
    comum que existe — dois adultos e uma criança — era recusada com
    "Nenhum recurso físico disponível".
    """

    @pytest.fixture
    def com_pool(self, app, db, cenario):
        from app.models.reservas import ReservaResource

        with app.app_context():
            # stock_quantity=None = capacidade ilimitada, compartilhável.
            pool = ReservaResource(
                source_id=cenario["source_id"],
                name="Capacidade",
                group_id=77,
                stock_quantity=None,
            )
            _db.session.add(pool)
            _db.session.flush()

            meia = ReservaItem(
                category_id=cenario["category_id"],
                name="Meia",
                price=50,
                billing_type=BillingType.FIXED,
                requires_resource=True,
                group_id=77,
            )
            inteira = _db.session.get(ReservaItem, cenario["item_id"])
            inteira.requires_resource = True
            inteira.group_id = 77
            _db.session.add(meia)
            _db.session.commit()
            yield {**cenario, "meia_id": meia.id, "pool_id": pool.id}

    def test_dois_itens_dividem_o_pool(self, app, db, client, com_pool):
        r = _post(
            client,
            _payload(
                com_pool,
                items=[
                    {"item_id": com_pool["item_id"], "quantity": 2, "price_override": 200.0},
                    {"item_id": com_pool["meia_id"], "quantity": 1, "price_override": 50.0},
                ],
            ),
        )
        assert r.status_code == 201, r.get_data(as_text=True)

    def test_apenas_um_link_carrega_o_recurso(self, app, db, client, com_pool):
        """uq_reserva_resource_link proíbe (reserva_id, resource_id) repetido.

        O segundo item precisa ficar com resource_id NULL — que é exatamente o
        que as reservas antigas já gravam.
        """
        from app.models.reservas import ReservaItemLink

        r = _post(
            client,
            _payload(
                com_pool,
                items=[
                    {"item_id": com_pool["item_id"], "quantity": 2, "price_override": 200.0},
                    {"item_id": com_pool["meia_id"], "quantity": 1, "price_override": 50.0},
                ],
            ),
        )
        assert r.status_code == 201
        with app.app_context():
            links = (
                db.session.query(ReservaItemLink)
                .filter_by(reserva_id=r.get_json()["reserva_id"])
                .all()
            )
            assert len(links) == 2
            com_recurso = [l for l in links if l.resource_id is not None]
            assert len(com_recurso) == 1
            assert com_recurso[0].resource_id == com_pool["pool_id"]


class TestUmaChurrasqueiraPorReserva:
    """Regra de negócio da Cachoeira: um espaço físico por reserva.

    Vive no servidor e não só no formulário — o site limita a seleção a uma, mas
    uma URL com dois `r<id>` chegaria aqui sem passar pela tela.
    """

    @pytest.fixture
    def com_recursos(self, app, db, cenario):
        from app.models.reservas import ReservaResource

        with app.app_context():
            a = ReservaResource(
                source_id=cenario["source_id"], name="Churrasqueira A",
                group_id=50, stock_quantity=1,
            )
            b = ReservaResource(
                source_id=cenario["source_id"], name="Churrasqueira B",
                group_id=50, stock_quantity=1,
            )
            _db.session.add_all([a, b])
            _db.session.flush()
            item = _db.session.get(ReservaItem, cenario["item_id"])
            item.requires_resource = True
            item.group_id = 50
            _db.session.commit()
            yield {**cenario, "a": a.id, "b": b.id}

    def test_uma_e_aceita(self, app, db, client, com_recursos):
        r = _post(
            client,
            _payload(
                com_recursos,
                items=[{
                    "item_id": com_recursos["item_id"], "quantity": 1,
                    "resource_id": com_recursos["a"], "price_override": 100.0,
                }],
            ),
        )
        assert r.status_code == 201, r.get_data(as_text=True)

    def test_duas_sao_recusadas(self, app, db, client, com_recursos):
        r = _post(
            client,
            _payload(
                com_recursos,
                items=[
                    {"item_id": com_recursos["item_id"], "quantity": 1,
                     "resource_id": com_recursos["a"], "price_override": 100.0},
                    {"item_id": com_recursos["item_id"], "quantity": 1,
                     "resource_id": com_recursos["b"], "price_override": 100.0},
                ],
            ),
        )
        assert r.status_code == 400
        assert "uma churrasqueira" in r.get_json()["erro"]

    def test_nada_e_gravado_quando_recusado(self, app, db, client, com_recursos):
        _post(
            client,
            _payload(
                com_recursos,
                items=[
                    {"item_id": com_recursos["item_id"], "quantity": 1,
                     "resource_id": com_recursos["a"], "price_override": 100.0},
                    {"item_id": com_recursos["item_id"], "quantity": 1,
                     "resource_id": com_recursos["b"], "price_override": 100.0},
                ],
            ),
        )
        with app.app_context():
            assert db.session.query(Reserva).count() == 0

    def test_itens_sem_recurso_nao_contam_no_limite(self, app, db, client, cenario):
        """Vários ingressos continuam permitidos — o limite é de espaço físico."""
        r = _post(
            client,
            _payload(cenario, items=[
                {"item_id": cenario["item_id"], "quantity": 5, "price_override": 500.0},
            ]),
        )
        assert r.status_code == 201


class TestCancelamento:
    """O endpoint que o cliente usa para desistir — e a suíte do site, para se limpar.

    Existe porque os testes de integração do Next.js criam reservas de verdade
    (é o objetivo deles) e precisavam de um jeito de desfazê-las; sem isso cada
    rodada deixava meia dúzia de "Regressao ... Silva" pendentes na lista do
    operador.
    """

    def _criar(self, client, cenario):
        r = _post(client, _payload(cenario))
        assert r.status_code == 201, r.get_json()
        return r.get_json()["group_id"]

    def _cancelar(self, client, group_id, chave=CHAVE):
        headers = {"X-Web-Api-Key": chave} if chave is not None else {}
        return client.post(
            f"/api/public/reservas/{group_id}/cancelar", headers=headers
        )

    def test_cancela_pendente(self, app, db, client, cenario):
        gid = self._criar(client, cenario)
        assert self._cancelar(client, gid).status_code == 200

        with app.app_context():
            versoes = (
                _db.session.query(Reserva)
                .filter(Reserva.group_id == gid)
                .order_by(Reserva.version)
                .all()
            )
            # Versionada, não apagada: a reserva original continua no banco como
            # ARCHIVED_VERSION. É isso que preserva a taxa de conversão.
            assert len(versoes) == 2
            assert versoes[-1].status == ReservaStatus.CANCELED

    def test_sem_chave(self, app, db, client, cenario):
        gid = self._criar(client, cenario)
        assert self._cancelar(client, gid, chave=None).status_code == 401

    def test_inexistente(self, app, db, client, cenario):
        assert self._cancelar(client, "nao-existe").status_code == 404

    def test_nao_cancela_duas_vezes(self, app, db, client, cenario):
        gid = self._criar(client, cenario)
        assert self._cancelar(client, gid).status_code == 200
        assert self._cancelar(client, gid).status_code == 409

    def test_nao_cancela_paga(self, app, db, client, cenario):
        """Reserva paga é caso de reembolso, decidido por gente — não por API."""
        gid = self._criar(client, cenario)
        with app.app_context():
            r = (
                _db.session.query(Reserva)
                .filter(Reserva.group_id == gid)
                .order_by(Reserva.version.desc())
                .first()
            )
            r.status = ReservaStatus.PAID
            _db.session.commit()
        assert self._cancelar(client, gid).status_code == 409

    def test_nao_cancela_reserva_de_balcao(self, app, db, client, cenario):
        """A chave do site não alcança o que o balcão criou, nem com o group_id."""
        gid = self._criar(client, cenario)
        with app.app_context():
            r = (
                _db.session.query(Reserva)
                .filter(Reserva.group_id == gid)
                .order_by(Reserva.version.desc())
                .first()
            )
            r.origin = ReservaOrigin.BALCAO
            _db.session.commit()
        assert self._cancelar(client, gid).status_code == 409


class TestJanelaDeHorario:
    """A hora sobrevive à criação — no camping ela é preço.

    A diária do camping é pró-rata por hora: das 08:00 às 17:00 do dia seguinte
    são 33 horas, não 24. Truncar a hora fazia a reserva nascer mais barata que
    o total simulado, ou seja, o cliente veria um preço e a portaria cobraria
    outro.
    """

    def test_criar_preserva_a_hora(self, app, db, client, cenario):
        corpo = _payload(
            cenario,
            check_in_date="2027-01-10T08:00",
            check_out_date="2027-01-11T17:00",
        )
        r = _post(client, corpo)
        assert r.status_code == 201, r.get_json()

        with app.app_context():
            reserva = (
                _db.session.query(Reserva)
                .filter(Reserva.group_id == r.get_json()["group_id"])
                .first()
            )
            assert reserva.check_in_date.hour == 8
            assert reserva.check_out_date.hour == 17

    def test_data_pura_continua_aceita(self, app, db, client, cenario):
        """Day use não tem hora, e não pode ser obrigado a inventar uma."""
        assert _post(client, _payload(cenario)).status_code == 201

    def test_data_malformada_ainda_e_recusada(self, app, db, client, cenario):
        r = _post(client, _payload(cenario, check_in_date="10/01/2027T08:00"))
        assert r.status_code == 400

    def test_disponibilidade_aceita_hora(self, app, db, client, cenario):
        """Recusar o "T" devolvia zero recursos e o site caía no modo antigo,
        oferecendo a tarifa em vez da churrasqueira física."""
        base = (
            f"/api/public/reservas/disponibilidade?source_id={cenario['source_id']}"
            f"&category_id={cenario['category_id']}"
        )
        com_hora = client.get(f"{base}&check_in=2027-01-10T08:00&check_out=2027-01-11T17:00")
        so_data = client.get(f"{base}&check_in=2027-01-10&check_out=2027-01-11")
        assert com_hora.status_code == 200
        assert com_hora.get_json() == so_data.get_json()


class TestCatalogoExpoeJanela:
    def test_categoria_sem_janela_vem_nula(self, app, db, client, cenario):
        """Sem os quatro campos o site pede só a data — o caso do Day use."""
        cat = _catalogo(client, cenario)
        assert cat["check_in_from"] is None
        assert cat["min_stay_hours"] is None

    def test_categoria_com_janela_chega_ao_site(self, app, db, client, cenario):
        from datetime import time as _time

        # Sem contexto aninhado: a fixture já cede dentro de um, e abrir outro
        # dava uma sessão cuja escrita o catálogo não enxergava.
        c = _db.session.get(ReservaCategory, cenario["category_id"])
        c.check_in_from = _time(8, 0)
        c.check_in_until = _time(17, 0)
        c.check_out_until = _time(17, 0)
        c.min_stay_hours = 24
        _db.session.commit()

        cat = _catalogo(client, cenario)
        assert cat["check_in_from"] == "08:00"
        assert cat["check_in_until"] == "17:00"
        assert cat["check_out_until"] == "17:00"
        assert cat["min_stay_hours"] == 24


def _catalogo(client, cenario) -> dict:
    """A categoria do cenário, como o site a recebe."""
    dados = client.get("/api/public/reservas/catalogo").get_json()
    for s in dados["sources"]:
        for c in s["categories"]:
            if c["id"] == cenario["category_id"]:
                return c
    raise AssertionError("categoria do cenário não veio no catálogo")
