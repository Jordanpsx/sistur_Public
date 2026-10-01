# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes para a persistência e herança do desconto percentual manual em reservas.

Cobre os dois bugs reportados:
  Ex. 1: Desconto perdido ao mudar status (PAID → outro status)
  Ex. 2: Desconto não aplicado proporcionalmente ao adicionar novos itens

Regras de negócio:
  - desconto_percentual_manual é salvo como coluna na Reserva (não em metadata)
  - Toda nova versão herda o percentual da versão anterior automaticamente
  - price_override em atualizar() = preço bruto do item (sem desconto);
    o desconto é aplicado server-side sobre o subtotal completo
  - Em criar(), price_override = preço final (com desconto já incorporado)
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models.reservas import (
    BillingType,
    Reserva,
    ReservaCategory,
    ReservaItem,
    ReservaSource,
    ReservaStatus,
)
from app.extensions import db as _db
from app.services.reserva_service import ReservaService
from app.services.reserva_pricing_service import ReservaPricingService
from app.core.audit import AuditLog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def item_fixo(db):
    """Cria um ReservaItem FIXED de R$500 para os testes de desconto."""
    source = ReservaSource(name="Parque")
    cat = ReservaCategory(name="Day Use", source=source)
    item = ReservaItem(
        name="Entrada Inteira",
        billing_type=BillingType.FIXED,
        price=500.00,
        requires_deposit=False,
        category=cat,
    )
    db.session.add(item)
    db.session.commit()
    return item


@pytest.fixture
def item_adicional(db, item_fixo):
    """Cria um segundo ReservaItem de R$50 na mesma categoria."""
    item = ReservaItem(
        name="Aluguel de Equipamento",
        billing_type=BillingType.FIXED,
        price=50.00,
        requires_deposit=False,
        category=item_fixo.category,
    )
    db.session.add(item)
    db.session.commit()
    return item


def _criar_reserva_com_desconto(item_fixo, desconto_pct: float, db) -> Reserva:
    """Helper: cria reserva R$500 com desconto percentual.

    Em criar(): price_override = preço FINAL com desconto aplicado.
    O sistema valida sum(price_overrides) == server_total.
    """
    preco_final = round(500.0 * (1 - desconto_pct / 100), 2)
    payload = [{
        "item_id": item_fixo.id,
        "quantity": 1,
        "price_override": preco_final,
    }]
    return ReservaService.criar(
        source_name=item_fixo.category.source.name,
        category_name=item_fixo.category.name,
        customer_name="Ana Lima",
        customer_document=None,
        origin="BALCAO",
        check_in_date=date.today(),
        check_out_date=date.today(),
        items_payload=payload,
        manual_discount_percent=desconto_pct,
    )


# ---------------------------------------------------------------------------
# ReservaPricingService — desconto manual em %
# ---------------------------------------------------------------------------

class TestPricingManualDesconto:
    """Testes unitários do cálculo de desconto manual por percentual."""

    def test_desconto_20_pct_calculado_corretamente(self, app, db, item_fixo):
        """Desconto de 20% sobre R$500 deve resultar em R$100 de desconto e total R$400."""
        with app.app_context():
            result = ReservaPricingService.calculate(
                source_id=item_fixo.category.source_id,
                check_in_date=date.today(),
                check_out_date=date.today(),
                items=[{"item_id": item_fixo.id, "quantity": 1}],
                manual_discount_percent=20.0,
            )

            assert result["subtotal"] == pytest.approx(500.0)
            assert result["discount_amount"] == pytest.approx(100.0)
            assert result["total"] == pytest.approx(400.0)
            assert result["discount_details"]["manual_discount"]["percent"] == pytest.approx(20.0)
            assert result["discount_details"]["manual_discount"]["amount"] == pytest.approx(100.0)

    def test_desconto_zero_sem_efeito(self, app, db, item_fixo):
        """Desconto de 0% não deve alterar o total."""
        with app.app_context():
            result = ReservaPricingService.calculate(
                source_id=item_fixo.category.source_id,
                check_in_date=date.today(),
                check_out_date=date.today(),
                items=[{"item_id": item_fixo.id, "quantity": 1}],
                manual_discount_percent=0.0,
            )

            assert result["total"] == pytest.approx(500.0)
            assert "manual_discount" not in result["discount_details"]

    def test_desconto_proporcional_apos_adicionar_item(self, app, db, item_fixo, item_adicional):
        """20% de desconto sobre subtotal R$550 (R$500 + R$50) deve gerar R$110 de desconto."""
        with app.app_context():
            result = ReservaPricingService.calculate(
                source_id=item_fixo.category.source_id,
                check_in_date=date.today(),
                check_out_date=date.today(),
                items=[
                    {"item_id": item_fixo.id, "quantity": 1},
                    {"item_id": item_adicional.id, "quantity": 1},
                ],
                manual_discount_percent=20.0,
            )

            assert result["subtotal"] == pytest.approx(550.0)
            assert result["discount_amount"] == pytest.approx(110.0)
            assert result["total"] == pytest.approx(440.0)


# ---------------------------------------------------------------------------
# ReservaService — criar com desconto percentual
# ---------------------------------------------------------------------------

class TestCriarReservaComDesconto:
    """Testes de criação de reserva com desconto manual."""

    def test_criar_salva_desconto_percentual_na_coluna(self, app, db, item_fixo):
        """Ao criar com 20% de desconto, Reserva.desconto_percentual_manual deve ser 20.00."""
        with app.app_context():
            reserva = _criar_reserva_com_desconto(item_fixo, desconto_pct=20.0, db=db)

            assert reserva.desconto_percentual_manual == Decimal("20.00")
            assert reserva.discount_amount == Decimal("100.00")
            assert reserva.total_amount == Decimal("400.00")

    def test_criar_sem_desconto_coluna_nula(self, app, db, item_fixo):
        """Reserva sem desconto manual deve ter desconto_percentual_manual IS NULL."""
        with app.app_context():
            payload = [{"item_id": item_fixo.id, "quantity": 1, "price_override": 500.00}]
            reserva = ReservaService.criar(
                source_name=item_fixo.category.source.name,
                category_name=item_fixo.category.name,
                customer_name="Pedro Costa",
                customer_document=None,
                origin="WEB",
                check_in_date=date.today(),
                check_out_date=date.today(),
                items_payload=payload,
            )

            assert reserva.desconto_percentual_manual is None

    def test_criar_dispara_audit_com_desconto(self, app, db, item_fixo):
        """Criação de reserva com desconto deve registrar exatamente 1 AuditLog."""
        with app.app_context():
            reserva = _criar_reserva_com_desconto(item_fixo, desconto_pct=10.0, db=db)
            count = db.session.query(AuditLog).filter_by(
                action="create", module="reservas", entity_id=reserva.id
            ).count()

        assert count == 1


# ---------------------------------------------------------------------------
# Exemplo 1 — Desconto preservado ao mudar status (PAID → novo status)
# ---------------------------------------------------------------------------

class TestDescontoPreservadoMudancaStatus:
    """Reproduz o Bug Ex. 1: desconto desaparecia ao mudar status.

    Reserva R$500 com 20% desconto → total R$400.
    Operador marca como PAID (pagou R$400).
    Muda status para COMPLETED.
    Sistema deve continuar mostrando total R$400, não R$500.
    """

    def test_status_change_preserva_discount_amount(self, app, db, item_fixo):
        """Após mudança de status, discount_amount deve ser idêntico à versão anterior."""
        with app.app_context():
            reserva_v1 = _criar_reserva_com_desconto(item_fixo, desconto_pct=20.0, db=db)
            reserva_v1.status = ReservaStatus.PAID
            db.session.commit()

            # Mudança de status (ex: PAID → COMPLETED) — sem novos itens
            reserva_v2 = ReservaService.atualizar(
                reserva_v1.id,
                items_payload=[],
                status="COMPLETED",
            )

            assert reserva_v2.desconto_percentual_manual == Decimal("20.00")
            assert reserva_v2.discount_amount == Decimal("100.00")
            assert reserva_v2.total_amount == Decimal("400.00")

    def test_status_change_preserva_versao_anterior_auditada(self, app, db, item_fixo):
        """A versão anterior deve ser arquivada após mudança de status."""
        with app.app_context():
            reserva_v1 = _criar_reserva_com_desconto(item_fixo, desconto_pct=20.0, db=db)
            reserva_v1.status = ReservaStatus.PAID
            db.session.commit()

            reserva_v2 = ReservaService.atualizar(
                reserva_v1.id,
                items_payload=[],
                status="COMPLETED",
            )
            v1_atualizada = db.session.get(Reserva, reserva_v1.id)

            assert v1_atualizada.status == ReservaStatus.ARCHIVED_VERSION
            assert reserva_v2.version == 2
            assert reserva_v2.group_id == reserva_v1.group_id


# ---------------------------------------------------------------------------
# Exemplo 2 — Desconto aplicado proporcionalmente ao adicionar itens
# ---------------------------------------------------------------------------

class TestDescontoProporcionadoNovosItens:
    """Reproduz o Bug Ex. 2: desconto desaparecia ao adicionar novos itens.

    Reserva R$500 com 20% desconto → total R$400. Operador pagou.
    Cliente adiciona item de R$50 → subtotal R$550.
    Com 20% de desconto: R$550 × 0.80 = R$440.
    Saldo devedor: R$440 - R$400 = R$40 (apenas o item novo com desconto).
    O sistema NÃO deve mostrar saldo de R$150 (como se o desconto sumisse).

    Nota sobre price_override em atualizar():
      price_override = preço BRUTO do item (sem desconto).
      O desconto é aplicado server-side sobre o subtotal.
      A validação verifica: sum(price_overrides) == pricing["subtotal"].
    """

    def test_novo_item_aplica_desconto_proporcional(self, app, db, item_fixo, item_adicional):
        """Ao adicionar item R$50, total deve ser R$440 (20% sobre R$550), não R$550."""
        with app.app_context():
            reserva_v1 = _criar_reserva_com_desconto(item_fixo, desconto_pct=20.0, db=db)
            assert reserva_v1.total_amount == Decimal("400.00")

            # price_override = preço BRUTO (sem desconto) — o servidor aplica o desconto
            items_payload = [
                {"item_id": item_fixo.id, "quantity": 1, "price_override": 500.0},
                {"item_id": item_adicional.id, "quantity": 1, "price_override": 50.0},
            ]
            # sum(price_overrides) = 550 == pricing["subtotal"] → validação passa
            reserva_v2 = ReservaService.atualizar(
                reserva_v1.id,
                items_payload=items_payload,
            )

            assert reserva_v2.desconto_percentual_manual == Decimal("20.00")
            assert reserva_v2.subtotal_amount == Decimal("550.00")
            assert reserva_v2.discount_amount == Decimal("110.00")
            assert reserva_v2.total_amount == Decimal("440.00")

    def test_desconto_herdado_mesmo_sem_passar_parametro(self, app, db, item_fixo, item_adicional):
        """atualizar() deve herdar desconto_percentual_manual da versão anterior sem argumento explícito."""
        with app.app_context():
            reserva_v1 = _criar_reserva_com_desconto(item_fixo, desconto_pct=15.0, db=db)

            # price_override = preços BRUTOS; não passa manual_discount_percent — herda 15% da v1
            items_payload = [
                {"item_id": item_fixo.id, "quantity": 1, "price_override": 500.0},
                {"item_id": item_adicional.id, "quantity": 1, "price_override": 50.0},
            ]
            reserva_v2 = ReservaService.atualizar(
                reserva_v1.id,
                items_payload=items_payload,
            )

            assert reserva_v2.desconto_percentual_manual == Decimal("15.00")
            # 15% de 550 = 82.50
            assert reserva_v2.discount_amount == Decimal("82.50")
            assert reserva_v2.total_amount == Decimal("467.50")

    def test_desconto_zerado_explicitamente(self, app, db, item_fixo):
        """Passar manual_discount_percent=0.0 deve remover o desconto explicitamente."""
        with app.app_context():
            reserva_v1 = _criar_reserva_com_desconto(item_fixo, desconto_pct=20.0, db=db)

            # price_override = preço bruto; sem desconto → sum == subtotal
            items_payload = [
                {"item_id": item_fixo.id, "quantity": 1, "price_override": 500.0},
            ]
            reserva_v2 = ReservaService.atualizar(
                reserva_v1.id,
                items_payload=items_payload,
                manual_discount_percent=0.0,
            )

            assert reserva_v2.desconto_percentual_manual is None
            assert reserva_v2.discount_amount == Decimal("0.00")
            assert reserva_v2.total_amount == Decimal("500.00")


# ---------------------------------------------------------------------------
# cancelar — desconto herdado na versão cancelada
# ---------------------------------------------------------------------------

class TestCancelarReservaMantemDesconto:
    """Ao cancelar, a nova versão CANCELED deve herdar desconto_percentual_manual."""

    def test_cancelar_herda_desconto(self, app, db, item_fixo):
        """Reserva cancelada deve manter o mesmo percentual de desconto para auditoria."""
        with app.app_context():
            reserva_v1 = _criar_reserva_com_desconto(item_fixo, desconto_pct=10.0, db=db)
            reserva_cancelada = ReservaService.cancelar(
                reserva_v1.id,
                motivo="Teste de cancelamento",
            )

            assert reserva_cancelada.desconto_percentual_manual == Decimal("10.00")
            assert reserva_cancelada.status == ReservaStatus.CANCELED
            assert reserva_cancelada.total_amount == reserva_v1.total_amount
