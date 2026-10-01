# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Soft lock de recurso físico por reservas PENDING.

Regressão de um bug real: ``PENDING`` está configurada com
``blocks_resource=false``, então uma reserva não paga não segurava recurso
nenhum. Dava para reservar a mesma churrasqueira de estoque 1 quantas vezes se
quisesse, e os clientes só descobriam no balcão.

``verificar_disponibilidade`` já contava PENDING dentro do prazo, então as duas
funções discordavam: a tela dizia esgotado e a criação aceitava.

Três coisas precisam valer ao mesmo tempo, e é fácil consertar uma quebrando
outra:

  1. PENDING **dentro** do prazo bloqueia;
  2. PENDING **vencida** não bloqueia — senão as 530 abandonadas travariam
     recursos para sempre;
  3. PAID bloqueia mesmo com prazo vencido — ``expires_at`` nunca é limpo no
     pagamento, então todas as pagas têm prazo no passado.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.extensions import db as _db
from app.models.reservas import (
    BillingType,
    Reserva,
    ReservaCategory,
    ReservaItem,
    ReservaItemLink,
    ReservaOrigin,
    ReservaResource,
    ReservaSource,
    ReservaStatus,
)
from app.services.reserva_service import ReservaService

DIA = date(2027, 7, 15)


@pytest.fixture
def cenario(app, db):
    """Um recurso físico de estoque 1, com um item que o exige."""
    with app.app_context():
        fonte = ReservaSource(name="Venue Lock", is_active=True)
        _db.session.add(fonte)
        _db.session.flush()

        cat = ReservaCategory(source_id=fonte.id, name="Day use")
        recurso = ReservaResource(
            source_id=fonte.id, name="Churrasqueira Única", group_id=90, stock_quantity=1
        )
        _db.session.add_all([cat, recurso])
        _db.session.flush()

        item = ReservaItem(
            category_id=cat.id,
            name="Churrasqueira",
            price=100,
            billing_type=BillingType.FIXED,
            requires_resource=True,
            group_id=90,
        )
        _db.session.add(item)
        _db.session.commit()
        yield {
            "fonte": fonte.name,
            "categoria": cat.name,
            "categoria_id": cat.id,
            "item_id": item.id,
            "recurso": recurso,
        }


def _ocupar(cenario, status, expires_at):
    """Grava uma reserva que consome o recurso, no status e prazo pedidos."""
    r = Reserva(
        group_id=f"g-{status}-{expires_at}",
        version=1,
        # `verificar_disponibilidade_recursos` filtra por fonte; sem isto a
        # reserva não entra na conta e o teste passa por engano.
        source_id=cenario["recurso"].source_id,
        customer_name="Ocupante",
        origin=ReservaOrigin.WEB,
        status=status,
        check_in_date=DIA,
        check_out_date=DIA,
        expires_at=expires_at,
    )
    _db.session.add(r)
    _db.session.flush()
    _db.session.add(
        ReservaItemLink(
            reserva_id=r.id,
            item_id=cenario["item_id"],
            quantity=1,
            resource_id=cenario["recurso"].id,
            locked_price=100,
        )
    )
    _db.session.commit()
    return r


def _tentar_reservar(cenario):
    """Tenta criar uma segunda reserva do mesmo recurso no mesmo dia."""
    return ReservaService.criar(
        source_name=cenario["fonte"],
        category_name=cenario["categoria"],
        customer_name="Segundo Cliente",
        customer_document=None,
        origin=ReservaOrigin.WEB,
        check_in_date=DIA,
        check_out_date=DIA,
        items_payload=[
            {"item_id": cenario["item_id"], "quantity": 1, "price_override": 100.0}
        ],
        ator_id=None,
    )


class TestSoftLock:
    def test_pending_no_prazo_bloqueia(self, app, db, cenario):
        """O caso do bug: reserva não paga precisa segurar o recurso."""
        with app.app_context():
            futuro = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
            _ocupar(cenario, ReservaStatus.PENDING, futuro)
            with pytest.raises(ValueError):
                _tentar_reservar(cenario)

    def test_pending_vencida_libera(self, app, db, cenario):
        """Sem isto, as 530 abandonadas travariam recursos para sempre."""
        with app.app_context():
            passado = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
            _ocupar(cenario, ReservaStatus.PENDING, passado)
            assert _tentar_reservar(cenario) is not None

    def test_paga_bloqueia_mesmo_com_prazo_vencido(self, app, db, cenario):
        """expires_at não é limpo no pagamento: toda PAID tem prazo no passado.

        Aplicar o prazo a ela soltaria todas as reservas já pagas.
        """
        with app.app_context():
            passado = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
            _ocupar(cenario, ReservaStatus.PAID, passado)
            with pytest.raises(ValueError):
                _tentar_reservar(cenario)

    def test_cancelada_nao_bloqueia(self, app, db, cenario):
        with app.app_context():
            futuro = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
            _ocupar(cenario, ReservaStatus.CANCELED, futuro)
            assert _tentar_reservar(cenario) is not None

    def test_pending_sem_prazo_nao_bloqueia(self, app, db, cenario):
        """expires_at nulo = sem lock. É o que um template com timeout_hours
        vazio produz, e a ausência de prazo não pode virar bloqueio eterno."""
        with app.app_context():
            _ocupar(cenario, ReservaStatus.PENDING, None)
            assert _tentar_reservar(cenario) is not None

    def test_dia_diferente_nao_bloqueia(self, app, db, cenario):
        """O lock é por período, não global no recurso."""
        with app.app_context():
            futuro = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
            r = _ocupar(cenario, ReservaStatus.PENDING, futuro)
            r.check_in_date = date(2027, 9, 1)
            r.check_out_date = date(2027, 9, 1)
            _db.session.commit()
            assert _tentar_reservar(cenario) is not None


class TestCoerenciaComDisponibilidade:
    def test_as_duas_funcoes_concordam(self, app, db, cenario):
        """A tela e a criação precisam dar a mesma resposta.

        Era exatamente onde divergiam: verificar_disponibilidade contava PENDING
        dentro do prazo e _verificar_estoque não.
        """
        with app.app_context():
            futuro = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
            _ocupar(cenario, ReservaStatus.PENDING, futuro)

            linhas = ReservaService.verificar_disponibilidade(
                category_id=cenario["categoria_id"], check_in=DIA, check_out=DIA
            )
            meu = next(l for l in linhas if l["item_id"] == cenario["item_id"])
            disponivel = (meu.get("available") or 0) > 0
            criou = True
            try:
                _tentar_reservar(cenario)
            except ValueError:
                criou = False

            assert bool(disponivel) == criou, (
                "verificar_disponibilidade e criar() precisam concordar"
            )


class TestDisponibilidadeDeRecursos:
    """A terceira função que precisa concordar com as outras duas.

    ``verificar_disponibilidade_recursos`` alimenta o painel do atendente e,
    desde a reforma do formulário, também a tela do cliente — é ela que diz se a
    Churrasqueira A4 está livre. Ela excluía PENDING "por política", então
    mostraria a A4 disponível enquanto alguém estivesse pagando por ela: os dois
    clientes chegariam ao fim e o segundo seria recusado só na criação.
    """

    def _disponivel(self, cenario) -> int | None:
        linhas = ReservaService.verificar_disponibilidade_recursos(
            source_id=cenario["recurso"].source_id, check_in=DIA, check_out=DIA
        )
        meu = next(l for l in linhas if l["resource_id"] == cenario["recurso"].id)
        return meu["available"]

    def test_pending_no_prazo_ocupa(self, app, db, cenario):
        with app.app_context():
            futuro = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
            _ocupar(cenario, ReservaStatus.PENDING, futuro)
            assert self._disponivel(cenario) == 0

    def test_pending_vencida_libera(self, app, db, cenario):
        with app.app_context():
            passado = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
            _ocupar(cenario, ReservaStatus.PENDING, passado)
            assert self._disponivel(cenario) == 1

    def test_paga_ocupa_mesmo_com_prazo_vencido(self, app, db, cenario):
        with app.app_context():
            passado = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
            _ocupar(cenario, ReservaStatus.PAID, passado)
            assert self._disponivel(cenario) == 0

    def test_concorda_com_a_criacao(self, app, db, cenario):
        """A tela e a criação precisam dar a mesma resposta sobre o recurso."""
        with app.app_context():
            futuro = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
            _ocupar(cenario, ReservaStatus.PENDING, futuro)

            livre = (self._disponivel(cenario) or 0) > 0
            criou = True
            try:
                _tentar_reservar(cenario)
            except ValueError:
                criou = False

            assert livre == criou, (
                "verificar_disponibilidade_recursos e criar() precisam concordar"
            )
