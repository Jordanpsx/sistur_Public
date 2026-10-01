# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes de integração para desconto no cálculo de preço (ReservaPricingService.calculate).

Garante que os dois mecanismos de desconto automático — Early Bird (AdvanceDiscountRule)
e datas especiais (CalendarDiscount + tiers de antecedência) — são corretamente aplicados
ao total de uma reserva, com a hierarquia de prioridade correta.

Cobertura:
  calculate() — Early Bird aplica quando antecedência qualifica
  calculate() — Early Bird não aplica quando antecedência insuficiente
  calculate() — Early Bird aplica tier de estadia correto
  calculate() — CalendarDiscount aplica quando tier de antecedência qualifica
  calculate() — CalendarDiscount sem tiers não produz desconto
  calculate() — CalendarDiscount sobrepõe Early Bird (prioridade máxima)
  calculate() — CalendarDiscount sem tier qualificado faz fallback para Early Bird
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models.reservas import (
    AdvanceDiscountRule,
    AdvanceDiscountStayTier,
    BillingType,
    CalendarDiscount,
    CalendarDiscountAdvanceTier,
    ReservaCategory,
    ReservaItem,
    ReservaSource,
)
from app.services.reserva_pricing_service import ReservaPricingService


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_source(db) -> ReservaSource:
    src = ReservaSource(name=f"src-{uuid.uuid4().hex[:6]}", display_name="Teste")
    db.session.add(src)
    db.session.flush()
    return src


def _make_item(db, source: ReservaSource, price: float = 200.0) -> ReservaItem:
    """Cria category + item FIXED com preço fixo dentro do source."""
    cat = ReservaCategory(source_id=source.id, name=f"cat-{uuid.uuid4().hex[:4]}")
    db.session.add(cat)
    db.session.flush()
    item = ReservaItem(
        category_id=cat.id,
        name="Inteira",
        billing_type=BillingType.FIXED,
        price=Decimal(str(price)),
        requires_deposit=False,
    )
    db.session.add(item)
    db.session.flush()
    return item


def _make_advance_rule(
    db,
    source: ReservaSource,
    *,
    min_days: int,
    pct: float,
    category_id: int | None = None,
) -> AdvanceDiscountRule:
    rule = AdvanceDiscountRule(
        source_id=source.id,
        category_id=category_id,
        min_days_in_advance=min_days,
        discount_percentage=Decimal(str(pct)),
        is_active=True,
    )
    db.session.add(rule)
    db.session.flush()
    return rule


def _make_calendar_discount(
    db,
    source: ReservaSource,
    *,
    target: date,
    label: str | None = None,
    category_id: int | None = None,
) -> CalendarDiscount:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    cd = CalendarDiscount(
        source_id=source.id,
        category_id=category_id,
        discount_date=target,
        label=label,
        is_active=True,
        criado_em=now,
        atualizado_em=now,
    )
    db.session.add(cd)
    db.session.flush()
    return cd


def _add_advance_tier(
    db,
    cd: CalendarDiscount,
    *,
    min_days: int,
    pct: float,
    min_stay: int | None = None,
) -> CalendarDiscountAdvanceTier:
    tier = CalendarDiscountAdvanceTier(
        calendar_discount_id=cd.id,
        min_days_in_advance=min_days,
        min_stay_days=min_stay,
        discount_percentage=Decimal(str(pct)),
    )
    db.session.add(tier)
    db.session.flush()
    return tier


# ---------------------------------------------------------------------------
# Early Bird — AdvanceDiscountRule
# ---------------------------------------------------------------------------


class TestCalculateEarlyBird:
    """Verifica que AdvanceDiscountRule é corretamente aplicado no calculate()."""

    def test_early_bird_aplica_quando_qualifica(self, app, db):
        """Reserva com antecedência suficiente deve ter desconto no total.

        Item FIXED R$200. Regra: 30+ dias → 10%. Reserva com 45 dias: desconto = R$20.
        """
        with app.app_context():
            src = _make_source(db)
            item = _make_item(db, src, price=200.0)
            _make_advance_rule(db, src, min_days=30, pct=10.0)
            db.session.commit()

            check_in = date.today() + timedelta(days=45)
            check_out = check_in + timedelta(days=1)

            result = ReservaPricingService.calculate(
                source_id=src.id,
                check_in_date=check_in,
                check_out_date=check_out,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            assert result["subtotal"] == pytest.approx(200.0)
            assert result["discount_amount"] == pytest.approx(20.0)
            assert result["total"] == pytest.approx(180.0)
            details = result["discount_details"].get("advance_booking", {})
            assert details["percent"] == pytest.approx(10.0)

    def test_early_bird_nao_aplica_quando_antecedencia_insuficiente(self, app, db):
        """Reserva com menos dias do que o mínimo não deve ter desconto."""
        with app.app_context():
            src = _make_source(db)
            item = _make_item(db, src, price=200.0)
            _make_advance_rule(db, src, min_days=30, pct=15.0)
            db.session.commit()

            check_in = date.today() + timedelta(days=10)  # só 10 dias, mínimo é 30
            check_out = check_in + timedelta(days=1)

            result = ReservaPricingService.calculate(
                source_id=src.id,
                check_in_date=check_in,
                check_out_date=check_out,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            assert result["discount_amount"] == pytest.approx(0.0)
            assert result["total"] == pytest.approx(result["subtotal"])

    def test_early_bird_seleciona_melhor_regra_entre_multiplas(self, app, db):
        """Com duas regras ativas, deve usar a de maior antecedência que qualifica."""
        with app.app_context():
            src = _make_source(db)
            item = _make_item(db, src, price=200.0)
            _make_advance_rule(db, src, min_days=15, pct=5.0)
            _make_advance_rule(db, src, min_days=30, pct=20.0)
            _make_advance_rule(db, src, min_days=60, pct=35.0)  # não qualifica
            db.session.commit()

            check_in = date.today() + timedelta(days=45)  # qualifica 15 e 30, não 60
            check_out = check_in + timedelta(days=1)

            result = ReservaPricingService.calculate(
                source_id=src.id,
                check_in_date=check_in,
                check_out_date=check_out,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            # Regra de 30 dias deve vencer (maior antecedência ainda aplicável)
            details = result["discount_details"]["advance_booking"]
            assert details["percent"] == pytest.approx(20.0)
            assert result["discount_amount"] == pytest.approx(40.0)  # 200 * 20%

    def test_early_bird_com_stay_tier_aplica_tier_correto(self, app, db):
        """Stay tier de estadia substitui percentual base da regra quando estadia qualifica."""
        with app.app_context():
            src = _make_source(db)
            item = _make_item(db, src, price=100.0)
            rule = _make_advance_rule(db, src, min_days=30, pct=10.0)

            # Tier: 3+ dias de estadia → 25%
            tier = AdvanceDiscountStayTier(
                rule_id=rule.id,
                min_stay_days=3,
                discount_percentage=Decimal("25.0"),
            )
            db.session.add(tier)
            db.session.commit()

            check_in  = date.today() + timedelta(days=45)
            check_out = check_in + timedelta(days=5)  # 5 dias de estadia ≥ 3

            result = ReservaPricingService.calculate(
                source_id=src.id,
                check_in_date=check_in,
                check_out_date=check_out,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            details = result["discount_details"]["advance_booking"]
            assert details["percent"] == pytest.approx(25.0)  # tier, não base 10%

    def test_early_bird_stay_tier_nao_aplica_com_estadia_curta(self, app, db):
        """Stay tier não qualifica quando estadia é menor que o mínimo — usa base."""
        with app.app_context():
            src = _make_source(db)
            item = _make_item(db, src, price=100.0)
            rule = _make_advance_rule(db, src, min_days=30, pct=10.0)

            tier = AdvanceDiscountStayTier(
                rule_id=rule.id,
                min_stay_days=7,
                discount_percentage=Decimal("30.0"),
            )
            db.session.add(tier)
            db.session.commit()

            check_in  = date.today() + timedelta(days=45)
            check_out = check_in + timedelta(days=2)  # 2 dias < mínimo 7

            result = ReservaPricingService.calculate(
                source_id=src.id,
                check_in_date=check_in,
                check_out_date=check_out,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            details = result["discount_details"]["advance_booking"]
            assert details["percent"] == pytest.approx(10.0)  # base, não tier 30%


# ---------------------------------------------------------------------------
# CalendarDiscount — datas especiais com tiers de antecedência
# ---------------------------------------------------------------------------


class TestCalculateCalendarDiscount:
    """Verifica que CalendarDiscount é aplicado corretamente no calculate()."""

    def test_calendar_aplica_quando_tier_qualifica(self, app, db):
        """CalendarDiscount com tier de antecedência qualificado deve descontar o total.

        Item R$200. Tier: 20+ dias de antecedência → 30%. Reserva 40 dias antes:
        desconto = R$60.
        """
        with app.app_context():
            src = _make_source(db)
            item = _make_item(db, src, price=200.0)
            target = date.today() + timedelta(days=40)
            cd = _make_calendar_discount(db, src, target=target, label="Black Friday")
            _add_advance_tier(db, cd, min_days=20, pct=30.0)
            db.session.commit()

            check_out = target + timedelta(days=1)

            result = ReservaPricingService.calculate(
                source_id=src.id,
                check_in_date=target,
                check_out_date=check_out,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            assert result["subtotal"] == pytest.approx(200.0)
            assert result["discount_amount"] == pytest.approx(60.0)
            assert result["total"] == pytest.approx(140.0)
            details = result["discount_details"]["advance_booking"]
            assert details["percent"] == pytest.approx(30.0)
            assert details["name"] == "Black Friday"
            assert details["calendar_discount_id"] == cd.id

    def test_calendar_sem_tiers_nao_produz_desconto(self, app, db):
        """CalendarDiscount ativo mas sem tiers não deve aplicar desconto."""
        with app.app_context():
            src = _make_source(db)
            item = _make_item(db, src, price=200.0)
            target = date.today() + timedelta(days=40)
            _make_calendar_discount(db, src, target=target)
            db.session.commit()

            check_out = target + timedelta(days=1)

            result = ReservaPricingService.calculate(
                source_id=src.id,
                check_in_date=target,
                check_out_date=check_out,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            assert result["discount_amount"] == pytest.approx(0.0)

    def test_calendar_sobrepoe_early_bird(self, app, db):
        """CalendarDiscount deve substituir Early Bird quando ambos qualificam.

        Early Bird: 30+ dias → 10%. CalendarDiscount: 15+ dias → 35%.
        Resultado esperado: 35% (calendar vence).
        """
        with app.app_context():
            src = _make_source(db)
            item = _make_item(db, src, price=200.0)
            _make_advance_rule(db, src, min_days=30, pct=10.0)

            target = date.today() + timedelta(days=45)
            cd = _make_calendar_discount(db, src, target=target, label="Carnaval")
            _add_advance_tier(db, cd, min_days=15, pct=35.0)
            db.session.commit()

            check_out = target + timedelta(days=1)

            result = ReservaPricingService.calculate(
                source_id=src.id,
                check_in_date=target,
                check_out_date=check_out,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            # 35% de R$200 = R$70 de desconto
            assert result["discount_amount"] == pytest.approx(70.0)
            details = result["discount_details"]["advance_booking"]
            assert details["percent"] == pytest.approx(35.0)
            assert details["name"] == "Carnaval"

    def test_calendar_sem_tier_qualificado_faz_fallback_early_bird(self, app, db):
        """CalendarDiscount existe mas antecedência insuficiente: Early Bird assume.

        CalendarDiscount exige 60+ dias, reserva feita com 30 dias.
        Early Bird exige 20+ dias → qualifica (30 > 20).
        """
        with app.app_context():
            src = _make_source(db)
            item = _make_item(db, src, price=200.0)
            _make_advance_rule(db, src, min_days=20, pct=10.0)

            target = date.today() + timedelta(days=30)
            cd = _make_calendar_discount(db, src, target=target)
            _add_advance_tier(db, cd, min_days=60, pct=40.0)  # não qualifica (30 < 60)
            db.session.commit()

            check_out = target + timedelta(days=1)

            result = ReservaPricingService.calculate(
                source_id=src.id,
                check_in_date=target,
                check_out_date=check_out,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            # Early Bird 10% deve ser aplicado (CalendarDiscount não qualificou)
            assert result["discount_amount"] == pytest.approx(20.0)
            details = result["discount_details"]["advance_booking"]
            assert details["percent"] == pytest.approx(10.0)

    def test_calendar_seleciona_maior_tier_aplicavel(self, app, db):
        """Com múltiplos tiers de antecedência, usa o de maior mínimo que qualifica."""
        with app.app_context():
            src = _make_source(db)
            item = _make_item(db, src, price=200.0)
            target = date.today() + timedelta(days=50)
            cd = _make_calendar_discount(db, src, target=target)
            _add_advance_tier(db, cd, min_days=10, pct=10.0)
            _add_advance_tier(db, cd, min_days=30, pct=25.0)   # ← deve vencer (50 ≥ 30)
            _add_advance_tier(db, cd, min_days=60, pct=40.0)   # não qualifica (50 < 60)
            db.session.commit()

            check_out = target + timedelta(days=1)

            result = ReservaPricingService.calculate(
                source_id=src.id,
                check_in_date=target,
                check_out_date=check_out,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            details = result["discount_details"]["advance_booking"]
            assert details["percent"] == pytest.approx(25.0)
            assert result["discount_amount"] == pytest.approx(50.0)  # 200 * 25%


# ---------------------------------------------------------------------------
# CalendarDiscount — dimensão de estadia nos tiers
# ---------------------------------------------------------------------------


class TestCalculateCalendarDiscountStayTier:
    """Verifica que min_stay_days dos tiers de CalendarDiscount é respeitado no calculate()."""

    def test_stay_tier_aplica_quando_estadia_qualifica(self, app, db):
        """Tier com min_stay=3 e estadia de 5 noites deve aplicar o percentual do tier.

        Item R$100. Tier: 20+ dias de antecedência + 3+ noites → 30%.
        check_out = check_in + 5 dias: discount = R$30.
        """
        with app.app_context():
            src = _make_source(db)
            item = _make_item(db, src, price=100.0)
            target = date.today() + timedelta(days=40)
            cd = _make_calendar_discount(db, src, target=target)
            _add_advance_tier(db, cd, min_days=20, pct=10.0, min_stay=None)   # base
            _add_advance_tier(db, cd, min_days=20, pct=30.0, min_stay=3)      # stay tier
            db.session.commit()

            check_out = target + timedelta(days=5)  # 5 noites ≥ 3

            result = ReservaPricingService.calculate(
                source_id=src.id,
                check_in_date=target,
                check_out_date=check_out,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            details = result["discount_details"]["advance_booking"]
            assert details["percent"] == pytest.approx(30.0)  # stay tier, não base 10%
            assert result["discount_amount"] == pytest.approx(30.0)

    def test_stay_tier_nao_aplica_com_estadia_curta_usa_base(self, app, db):
        """Tier com min_stay=3 não qualifica para estadia de 2 noites: usa base (None)."""
        with app.app_context():
            src = _make_source(db)
            item = _make_item(db, src, price=100.0)
            target = date.today() + timedelta(days=40)
            cd = _make_calendar_discount(db, src, target=target)
            tier_base = _add_advance_tier(db, cd, min_days=20, pct=10.0, min_stay=None)
            _add_advance_tier(db, cd, min_days=20, pct=30.0, min_stay=3)
            db.session.commit()

            check_out = target + timedelta(days=2)  # 2 noites < 3

            result = ReservaPricingService.calculate(
                source_id=src.id,
                check_in_date=target,
                check_out_date=check_out,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            details = result["discount_details"]["advance_booking"]
            assert details["percent"] == pytest.approx(10.0)  # base, não stay tier
            assert result["discount_amount"] == pytest.approx(10.0)

    def test_sem_tier_base_e_stay_nao_qualifica_sem_desconto(self, app, db):
        """Apenas stay tier cadastrado e estadia insuficiente: sem desconto."""
        with app.app_context():
            src = _make_source(db)
            item = _make_item(db, src, price=100.0)
            target = date.today() + timedelta(days=40)
            cd = _make_calendar_discount(db, src, target=target)
            _add_advance_tier(db, cd, min_days=20, pct=30.0, min_stay=5)  # precisa 5 noites
            db.session.commit()

            check_out = target + timedelta(days=2)  # 2 noites < 5

            result = ReservaPricingService.calculate(
                source_id=src.id,
                check_in_date=target,
                check_out_date=check_out,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            assert result["discount_amount"] == pytest.approx(0.0)
