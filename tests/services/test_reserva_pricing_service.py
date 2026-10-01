# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes para ReservaPricingService — holiday pricing e tiered pricing.

Cobre:
- _load_holidays: filtro por afeta_precificacao, feriado recorrente, range single-day
- _price_for_day: prioridade feriado > fim de semana > dia útil, fallbacks para NULL
- calculate (integração): preço correto aplicado em reservas de feriado single-day
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.calendario import GlobalEvent, TipoEvento
from app.models.reservas import (
    BillingType,
    ReservaCategory,
    ReservaItem,
    ReservaResource,
    ReservaSource,
)
from app.services.reserva_pricing_service import ReservaPricingService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _criar_feriado(db, data: date, afeta_precificacao: bool = True,
                   recorrente: bool = False, tipo=TipoEvento.NATIONAL_HOLIDAY) -> GlobalEvent:
    """Cria e persiste um GlobalEvent de feriado."""
    ev = GlobalEvent(
        titulo="Feriado Teste",
        data_evento=data,
        tipo=tipo,
        afeta_precificacao=afeta_precificacao,
        afeta_folha=True,
        recorrente_anual=recorrente,
    )
    db.session.add(ev)
    db.session.commit()
    return ev


def _criar_item(db, price=100.0, price_weekday=None, price_weekend=None,
                price_holiday=None, billing_type=BillingType.FIXED) -> ReservaItem:
    """Cria um ReservaItem com tiered pricing configurável."""
    source = ReservaSource(name=f"Source-{id(price)}")
    cat = ReservaCategory(name="Cat", source=source)
    item = ReservaItem(
        name="Item Teste",
        billing_type=billing_type,
        price=price,
        price_weekday=price_weekday,
        price_weekend=price_weekend,
        price_holiday=price_holiday,
        requires_deposit=False,
        category=cat,
    )
    db.session.add(item)
    db.session.commit()
    return item


# ---------------------------------------------------------------------------
# _load_holidays
# ---------------------------------------------------------------------------

class TestLoadHolidays:

    def test_feriado_com_afeta_precificacao_true_incluido(self, app, db):
        """Feriado marcado afeta_precificacao=True deve entrar no holiday_set."""
        with app.app_context():
            target = date(2026, 4, 5)
            _criar_feriado(db, target, afeta_precificacao=True)

            result = ReservaPricingService._load_holidays(target, target + timedelta(days=1))

            assert target in result

    def test_feriado_com_afeta_precificacao_false_excluido(self, app, db):
        """Feriado com afeta_precificacao=False NÃO deve afetar precificação."""
        with app.app_context():
            target = date(2026, 4, 5)
            _criar_feriado(db, target, afeta_precificacao=False)

            result = ReservaPricingService._load_holidays(target, target + timedelta(days=1))

            assert target not in result

    def test_range_single_day_nao_retorna_vazio(self, app, db):
        """Quando check_in == check_out, o feriado deve ser detectado mesmo assim.

        Caso de regressão: antes do fix, a query usava data < check_out e
        o loop while current < co_date, ambos ignoravam o check_in day.
        """
        with app.app_context():
            target = date(2026, 4, 5)
            _criar_feriado(db, target, afeta_precificacao=True)

            # Simula o _holiday_end calculado em calculate() para single-day
            result = ReservaPricingService._load_holidays(target, target + timedelta(days=1))

            assert target in result, (
                "Feriado no dia de check_in deve ser detectado em reservas de dia único."
            )

    def test_feriado_recorrente_detectado_em_outro_ano(self, app, db):
        """Feriado recorrente cadastrado em 2025 deve casar com 2026 pelo mês+dia."""
        with app.app_context():
            # Cadastrado em 2025 mas recorrente_anual=True
            _criar_feriado(db, date(2025, 12, 25), afeta_precificacao=True, recorrente=True)

            # Consulta em 2026
            resultado = ReservaPricingService._load_holidays(
                date(2026, 12, 24), date(2026, 12, 27)
            )

            assert date(2026, 12, 25) in resultado

    def test_feriado_fora_do_range_nao_incluido(self, app, db):
        """Feriado exato fora do intervalo não deve aparecer no set."""
        with app.app_context():
            _criar_feriado(db, date(2026, 4, 10), afeta_precificacao=True)

            result = ReservaPricingService._load_holidays(
                date(2026, 4, 5), date(2026, 4, 8)
            )

            assert date(2026, 4, 10) not in result

    def test_feriado_tipo_local_incluido(self, app, db):
        """Feriado LOCAL com afeta_precificacao=True também deve ser incluído."""
        with app.app_context():
            target = date(2026, 6, 13)
            _criar_feriado(db, target, afeta_precificacao=True, tipo=TipoEvento.LOCAL_HOLIDAY)

            result = ReservaPricingService._load_holidays(target, target + timedelta(days=1))

            assert target in result

    def test_multiplos_feriados_no_range(self, app, db):
        """Todos os feriados com afeta_precificacao=True no range devem ser retornados."""
        with app.app_context():
            d1 = date(2026, 4, 5)
            d2 = date(2026, 4, 6)
            d3 = date(2026, 4, 8)  # fora do range abaixo
            _criar_feriado(db, d1, afeta_precificacao=True)
            _criar_feriado(db, d2, afeta_precificacao=True)
            _criar_feriado(db, d3, afeta_precificacao=True)

            result = ReservaPricingService._load_holidays(d1, date(2026, 4, 7))

            assert d1 in result
            assert d2 in result
            assert d3 not in result


# ---------------------------------------------------------------------------
# _price_for_day
# ---------------------------------------------------------------------------

class TestPriceForDay:

    def test_feriado_retorna_price_holiday(self, app, db):
        """Dia de feriado deve retornar price_holiday."""
        with app.app_context():
            item = _criar_item(db, price=100, price_holiday=200)
            holiday_set = {date(2026, 4, 5)}

            result = ReservaPricingService._price_for_day(date(2026, 4, 5), item, holiday_set)

            assert result == Decimal("200")

    def test_feriado_sem_price_holiday_usa_price_base(self, app, db):
        """Feriado sem price_holiday configurado deve fazer fallback para price base."""
        with app.app_context():
            item = _criar_item(db, price=100, price_holiday=None)
            holiday_set = {date(2026, 4, 5)}

            result = ReservaPricingService._price_for_day(date(2026, 4, 5), item, holiday_set)

            assert result == Decimal("100")

    def test_sabado_retorna_price_weekend(self, app, db):
        """Sábado deve retornar price_weekend."""
        with app.app_context():
            item = _criar_item(db, price=100, price_weekend=150)
            # 2026-04-04 é sábado
            sabado = date(2026, 4, 4)
            assert sabado.weekday() == 5, "Data deve ser sábado para este teste"

            result = ReservaPricingService._price_for_day(sabado, item, set())

            assert result == Decimal("150")

    def test_domingo_retorna_price_weekend(self, app, db):
        """Domingo deve retornar price_weekend."""
        with app.app_context():
            item = _criar_item(db, price=100, price_weekend=150)
            # 2026-04-05 é domingo
            domingo = date(2026, 4, 5)
            assert domingo.weekday() == 6, "Data deve ser domingo para este teste"

            result = ReservaPricingService._price_for_day(domingo, item, set())

            assert result == Decimal("150")

    def test_sexta_feira_e_dia_util(self, app, db):
        """Sexta-feira deve ser tratada como dia útil (weekday price), não fim de semana."""
        with app.app_context():
            item = _criar_item(db, price=100, price_weekday=80, price_weekend=150)
            # 2026-04-03 é sexta-feira
            sexta = date(2026, 4, 3)
            assert sexta.weekday() == 4, "Data deve ser sexta-feira para este teste"

            result = ReservaPricingService._price_for_day(sexta, item, set())

            assert result == Decimal("80")

    def test_dia_util_retorna_price_weekday(self, app, db):
        """Segunda a sexta deve retornar price_weekday."""
        with app.app_context():
            item = _criar_item(db, price=100, price_weekday=70)
            # 2026-04-07 é terça-feira
            terca = date(2026, 4, 7)
            assert terca.weekday() == 1

            result = ReservaPricingService._price_for_day(terca, item, set())

            assert result == Decimal("70")

    def test_fim_de_semana_sem_price_weekend_usa_base(self, app, db):
        """Fim de semana sem price_weekend configurado deve usar price base."""
        with app.app_context():
            item = _criar_item(db, price=100, price_weekend=None)
            sabado = date(2026, 4, 4)

            result = ReservaPricingService._price_for_day(sabado, item, set())

            assert result == Decimal("100")

    def test_prioridade_feriado_sobre_fim_de_semana(self, app, db):
        """Feriado em fim de semana deve usar price_holiday, não price_weekend."""
        with app.app_context():
            item = _criar_item(db, price=100, price_weekend=150, price_holiday=200)
            # domingo que também é feriado
            domingo_feriado = date(2026, 4, 5)
            assert domingo_feriado.weekday() == 6

            result = ReservaPricingService._price_for_day(domingo_feriado, item, {domingo_feriado})

            assert result == Decimal("200"), (
                "price_holiday deve ter prioridade máxima sobre price_weekend."
            )


# ---------------------------------------------------------------------------
# calculate — integração single-day com feriado
# ---------------------------------------------------------------------------

class TestCalculateFeriadoSingleDay:

    def test_reserva_single_day_feriado_usa_price_holiday(self, app, db):
        """Reserva de dia único em feriado deve usar price_holiday no calculate().

        Cobre o bug corrigido: check_in == check_out gerava holiday_set vazio,
        fazendo o sistema ignorar o price_holiday e usar price_weekend.
        """
        with app.app_context():
            feriado = date(2026, 4, 5)  # domingo (fim de semana E feriado)
            assert feriado.weekday() == 6

            _criar_feriado(db, feriado, afeta_precificacao=True)

            source = ReservaSource(name="Cachoeira Test")
            cat = ReservaCategory(name="Day Use", source=source)
            item = ReservaItem(
                name="Inteira",
                billing_type=BillingType.FIXED,
                price=30.00,
                price_weekday=30.13,
                price_weekend=35.13,
                price_holiday=45.13,
                requires_deposit=False,
                category=cat,
            )
            db.session.add(item)
            db.session.commit()

            resultado = ReservaPricingService.calculate(
                source_id=source.id,
                check_in_date=feriado,
                check_out_date=feriado,  # same day
                items=[{"item_id": item.id, "quantity": 1}],
            )

            assert resultado["subtotal"] == pytest.approx(45.13), (
                "Reserva de dia único em feriado deve usar price_holiday=45.13, "
                f"não price_weekend=35.13. Subtotal obtido: {resultado['subtotal']}"
            )

    def test_reserva_single_day_fim_de_semana_sem_feriado(self, app, db):
        """Fim de semana sem feriado cadastrado deve usar price_weekend."""
        with app.app_context():
            sabado = date(2026, 4, 4)
            assert sabado.weekday() == 5

            source = ReservaSource(name="Cachoeira Test FDS")
            cat = ReservaCategory(name="Day Use", source=source)
            item = ReservaItem(
                name="Inteira",
                billing_type=BillingType.FIXED,
                price=30.00,
                price_weekday=30.13,
                price_weekend=35.13,
                price_holiday=45.13,
                requires_deposit=False,
                category=cat,
            )
            db.session.add(item)
            db.session.commit()

            resultado = ReservaPricingService.calculate(
                source_id=source.id,
                check_in_date=sabado,
                check_out_date=sabado,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            assert resultado["subtotal"] == pytest.approx(35.13)


# ---------------------------------------------------------------------------
# calculate — DAILY_PRORATED_HOURLY (camping / estadas com horário variável)
# ---------------------------------------------------------------------------
#
# Modelo de cobrança do camping:
#   - Primeiras 24 horas → 1 diária cheia (tarifa tiered pelo DIA de check-in).
#   - Horas excedentes  → (tarifa / 24) por hora, somadas à diária.
#   - Quantidade >1 (ex: várias barracas) multiplica o unit_total.
#
# Testes usam datetime (não date) para exercitar a precisão horária — usar
# date puro cai no caminho de "diárias inteiras" (total_hours = dias × 24),
# que também é coberto num teste dedicado abaixo.

class TestCalculateDailyProratedHourly:
    """Testes para o billing type DAILY_PRORATED_HOURLY usado em camping.

    Cobrança = 1 diária cheia (primeiras 24h) + pró-rata horário para o excedente.
    A tarifa base é tiered (feriado > fim-de-semana > dia-útil) e vem do
    DIA DE CHECK-IN — mesmo que a estadia atravesse dias com tarifas diferentes.
    """

    # ── Dias de referência já verificados via `datetime.weekday()` ──
    SEGUNDA = date(2026, 4, 13)   # weekday=0 (dia útil)
    QUARTA  = date(2026, 4, 15)   # weekday=2 (dia útil)
    SEXTA   = date(2026, 4, 17)   # weekday=4 (dia útil)
    SABADO  = date(2026, 4, 18)   # weekday=5 (fim de semana)
    DOMINGO = date(2026, 4, 19)   # weekday=6 (fim de semana)

    def _setup_camping_item(self, db, source_name: str = "Cachoeira"):
        """Cria source + category + item DAILY_PRORATED_HOURLY com tiered pricing.

        Tarifas escolhidas para aritmética limpa:
          price_weekday = 120.00  (dia útil)
          price_weekend = 144.00  (fim de semana)
          price_holiday = 168.00  (feriado)
        """
        source = ReservaSource(name=source_name)
        cat = ReservaCategory(name="Camping", source=source)
        item = ReservaItem(
            name="Site com Barraca",
            billing_type=BillingType.DAILY_PRORATED_HOURLY,
            price=120.00,
            price_weekday=120.00,
            price_weekend=144.00,
            price_holiday=168.00,
            requires_deposit=False,
            category=cat,
        )
        db.session.add(item)
        db.session.commit()
        return source, item

    # ─────────────────────────────────────────────────────────────────────
    # Boundary 1: estadia ≤ 24h cobra exatamente uma diária cheia
    # ─────────────────────────────────────────────────────────────────────

    def test_estadia_menor_que_24h_cobra_uma_diaria_cheia(self, app, db):
        """Camper chega 14h e sai 22h do mesmo dia (8h): cobra tarifa cheia."""
        with app.app_context():
            source, item = self._setup_camping_item(db, "Camping-8h")
            ci = datetime.combine(self.QUARTA, datetime.min.time()).replace(hour=14)
            co = datetime.combine(self.QUARTA, datetime.min.time()).replace(hour=22)

            r = ReservaPricingService.calculate(
                source_id=source.id,
                check_in_date=ci,
                check_out_date=co,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            bd = r["items_breakdown"][0]
            assert bd["total_hours"] == pytest.approx(8.0)
            assert bd["unit_total"] == pytest.approx(120.00), (
                "Estadia de 8h deve cobrar 1 diária cheia (R$120), "
                f"não proratada. Obtido: {bd['unit_total']}"
            )
            assert r["subtotal"] == pytest.approx(120.00)

    def test_estadia_exatamente_24h_cobra_uma_diaria(self, app, db):
        """Boundary: 14h dia 1 → 14h dia 2 (24h exatas): ainda uma diária."""
        with app.app_context():
            source, item = self._setup_camping_item(db, "Camping-24h")
            ci = datetime.combine(self.QUARTA, datetime.min.time()).replace(hour=14)
            co = ci + timedelta(hours=24)

            r = ReservaPricingService.calculate(
                source_id=source.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            bd = r["items_breakdown"][0]
            assert bd["total_hours"] == pytest.approx(24.0)
            assert bd["unit_total"] == pytest.approx(120.00), (
                "24h exatas ainda estão dentro da primeira diária — "
                "não deve ativar pró-rata."
            )

    # ─────────────────────────────────────────────────────────────────────
    # Boundary 2: estadia > 24h ativa pró-rata horário
    # ─────────────────────────────────────────────────────────────────────

    def test_estadia_25h_cobra_diaria_mais_uma_hora_prorata(self, app, db):
        """25h = 1 diária + 1h × (120/24) = 120 + 5 = 125.00."""
        with app.app_context():
            source, item = self._setup_camping_item(db, "Camping-25h")
            ci = datetime.combine(self.QUARTA, datetime.min.time()).replace(hour=14)
            co = ci + timedelta(hours=25)

            r = ReservaPricingService.calculate(
                source_id=source.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            bd = r["items_breakdown"][0]
            assert bd["total_hours"] == pytest.approx(25.0)
            assert bd["unit_total"] == pytest.approx(125.00)

    def test_estadia_30h_cobra_diaria_mais_seis_horas_prorata(self, app, db):
        """30h = 1 diária + 6h × (120/24) = 120 + 30 = 150.00."""
        with app.app_context():
            source, item = self._setup_camping_item(db, "Camping-30h")
            ci = datetime.combine(self.QUARTA, datetime.min.time()).replace(hour=14)
            co = ci + timedelta(hours=30)

            r = ReservaPricingService.calculate(
                source_id=source.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            bd = r["items_breakdown"][0]
            assert bd["unit_total"] == pytest.approx(150.00)

    def test_estadia_48h_equivale_a_duas_diarias(self, app, db):
        """48h = 1 diária + 24h × (120/24) = 120 + 120 = 240.00 (duas diárias)."""
        with app.app_context():
            source, item = self._setup_camping_item(db, "Camping-48h")
            ci = datetime.combine(self.SEGUNDA, datetime.min.time()).replace(hour=14)
            co = ci + timedelta(hours=48)

            r = ReservaPricingService.calculate(
                source_id=source.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            bd = r["items_breakdown"][0]
            assert bd["unit_total"] == pytest.approx(240.00), (
                "48h de DAILY_PRORATED_HOURLY = exatamente 2 diárias"
            )

    def test_horas_fracionarias_sao_contabilizadas(self, app, db):
        """25h30min = 1 diária + 1.5h × (120/24) = 120 + 7.50 = 127.50."""
        with app.app_context():
            source, item = self._setup_camping_item(db, "Camping-25h30")
            ci = datetime.combine(self.QUARTA, datetime.min.time()).replace(hour=14)
            co = ci + timedelta(hours=25, minutes=30)

            r = ReservaPricingService.calculate(
                source_id=source.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 1}],
            )

            bd = r["items_breakdown"][0]
            assert bd["total_hours"] == pytest.approx(25.5)
            assert bd["unit_total"] == pytest.approx(127.50)

    # ─────────────────────────────────────────────────────────────────────
    # Quantidade múltipla (várias barracas/sites na mesma reserva)
    # ─────────────────────────────────────────────────────────────────────

    def test_quantidade_multipla_multiplica_unit_total(self, app, db):
        """3 sites × 30h = 3 × R$150 = R$450."""
        with app.app_context():
            source, item = self._setup_camping_item(db, "Camping-3sites")
            ci = datetime.combine(self.QUARTA, datetime.min.time()).replace(hour=14)
            co = ci + timedelta(hours=30)

            r = ReservaPricingService.calculate(
                source_id=source.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 3}],
            )

            bd = r["items_breakdown"][0]
            assert bd["unit_total"] == pytest.approx(150.00)
            assert bd["item_total"] == pytest.approx(450.00)
            assert r["subtotal"] == pytest.approx(450.00)

    def test_cotacao_composta_prorrateia_entrada_e_locacao_mas_nao_consumivel(
        self, app, db
    ):
        """Entrada e barraca acompanham 30h; lenha FIXED permanece unitária."""
        with app.app_context():
            source = ReservaSource(name="Camping-itens-mistos")
            category = ReservaCategory(name="Camping", source=source)
            entrada = ReservaItem(
                name="Inteira",
                billing_type=BillingType.DAILY_PRORATED_HOURLY,
                price=90.00,
                requires_deposit=False,
                is_entry_ticket=True,
                category=category,
            )
            barraca = ReservaItem(
                name="Barraca Pequena",
                billing_type=BillingType.DAILY_PRORATED_HOURLY,
                price=95.00,
                requires_deposit=False,
                category=category,
            )
            lenha = ReservaItem(
                name="Lenha",
                billing_type=BillingType.FIXED,
                price=38.00,
                requires_deposit=False,
                category=category,
            )
            db.session.add_all([entrada, barraca, lenha])
            db.session.commit()

            check_in = datetime(2026, 4, 15, 14, 0)
            check_out = check_in + timedelta(hours=30)
            result = ReservaPricingService.calculate(
                source_id=source.id,
                check_in_date=check_in,
                check_out_date=check_out,
                items=[
                    {"item_id": entrada.id, "quantity": 1},
                    {"item_id": barraca.id, "quantity": 1},
                    {"item_id": lenha.id, "quantity": 1},
                ],
            )

            breakdown = {
                item["item_name"]: item for item in result["items_breakdown"]
            }
            assert breakdown["Inteira"]["unit_total"] == pytest.approx(112.50)
            assert breakdown["Inteira"]["total_hours"] == pytest.approx(30.0)
            assert breakdown["Barraca Pequena"]["unit_total"] == pytest.approx(118.75)
            assert breakdown["Barraca Pequena"]["total_hours"] == pytest.approx(30.0)
            assert breakdown["Lenha"]["item_total"] == pytest.approx(38.00)
            assert breakdown["Lenha"]["billing_type"] == BillingType.FIXED.value
            assert result["subtotal"] == pytest.approx(269.25)

    # ─────────────────────────────────────────────────────────────────────
    # Tiered pricing — check-in determina a tarifa base
    # ─────────────────────────────────────────────────────────────────────

    def test_checkin_em_dia_util_usa_price_weekday(self, app, db):
        """Check-in quarta → base = R$120 (price_weekday)."""
        with app.app_context():
            source, item = self._setup_camping_item(db, "Camping-weekday")
            ci = datetime.combine(self.QUARTA, datetime.min.time()).replace(hour=14)
            co = ci + timedelta(hours=20)

            r = ReservaPricingService.calculate(
                source_id=source.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 1}],
            )
            bd = r["items_breakdown"][0]
            assert bd["unit_price"] == pytest.approx(120.00)
            assert bd["unit_total"] == pytest.approx(120.00)

    def test_checkin_em_fim_de_semana_usa_price_weekend(self, app, db):
        """Check-in sábado → base = R$144 (price_weekend)."""
        with app.app_context():
            source, item = self._setup_camping_item(db, "Camping-weekend")
            ci = datetime.combine(self.SABADO, datetime.min.time()).replace(hour=14)
            co = ci + timedelta(hours=20)

            r = ReservaPricingService.calculate(
                source_id=source.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 1}],
            )
            bd = r["items_breakdown"][0]
            assert bd["unit_price"] == pytest.approx(144.00)
            assert bd["unit_total"] == pytest.approx(144.00)

    def test_checkin_em_feriado_usa_price_holiday(self, app, db):
        """Check-in em feriado → base = R$168 (price_holiday), sobre tudo."""
        with app.app_context():
            source, item = self._setup_camping_item(db, "Camping-holiday")
            feriado = self.SEGUNDA  # segunda pode ser dia útil, vira feriado aqui
            _criar_feriado(db, feriado, afeta_precificacao=True)

            ci = datetime.combine(feriado, datetime.min.time()).replace(hour=14)
            co = ci + timedelta(hours=20)

            r = ReservaPricingService.calculate(
                source_id=source.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 1}],
            )
            bd = r["items_breakdown"][0]
            assert bd["unit_price"] == pytest.approx(168.00), (
                "Feriado tem prioridade máxima no tiered pricing"
            )
            assert bd["unit_total"] == pytest.approx(168.00)

    def test_tarifa_base_do_checkin_se_estende_para_horas_extras(self, app, db):
        """Check-in sábado 14h → check-out segunda 20h (54h).

        Mesmo que a estadia atravesse dias úteis, a tarifa base é do CHECK-IN
        (price_weekend = R$144). Pró-rata = 30h × (144/24) = R$180.
        Total = 144 + 180 = R$324.
        """
        with app.app_context():
            source, item = self._setup_camping_item(db, "Camping-crossover")
            ci = datetime.combine(self.SABADO, datetime.min.time()).replace(hour=14)
            co = ci + timedelta(hours=54)  # segunda 20h

            r = ReservaPricingService.calculate(
                source_id=source.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 1}],
            )
            bd = r["items_breakdown"][0]
            assert bd["unit_price"] == pytest.approx(144.00)
            assert bd["total_hours"] == pytest.approx(54.0)
            assert bd["unit_total"] == pytest.approx(324.00), (
                "Tarifa base do check-in (weekend) deve ser aplicada também "
                "às horas excedentes, ignorando mudança de dia útil no meio."
            )

    # ─────────────────────────────────────────────────────────────────────
    # date puro (sem hora) — comportamento documentado
    # ─────────────────────────────────────────────────────────────────────

    def test_date_puro_cai_em_diarias_inteiras(self, app, db):
        """Check-in e check-out como `date` (sem hora) → total_hours = dias × 24.

        Segunda → Quarta = 2 dias = 48h → 1 diária + 24h*(tarifa/24) = 2 diárias.
        """
        with app.app_context():
            source, item = self._setup_camping_item(db, "Camping-datepuro")

            r = ReservaPricingService.calculate(
                source_id=source.id,
                check_in_date=self.SEGUNDA,
                check_out_date=self.QUARTA,
                items=[{"item_id": item.id, "quantity": 1}],
            )
            bd = r["items_breakdown"][0]
            assert bd["total_hours"] == pytest.approx(48.0)
            assert bd["unit_total"] == pytest.approx(240.00)

    # ─────────────────────────────────────────────────────────────────────
    # Arredondamento decimal (HALF_UP para duas casas)
    # ─────────────────────────────────────────────────────────────────────

    def test_arredondamento_half_up_em_hora_fracionaria_indivisivel(self, app, db):
        """Tarifa R$120, 1h extra → 120 + 120/24 = 120 + 5 = 125.00 (exato).

        Agora com 1 minuto extra (25h01): 120 + (1 + 1/60) × 5 = 125.0833…
        → arredonda HALF_UP para 2 casas = R$125.08.
        """
        with app.app_context():
            source, item = self._setup_camping_item(db, "Camping-round")
            ci = datetime.combine(self.QUARTA, datetime.min.time()).replace(hour=14)
            co = ci + timedelta(hours=25, minutes=1)

            r = ReservaPricingService.calculate(
                source_id=source.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 1}],
            )
            bd = r["items_breakdown"][0]
            # 120 + (61/60) × (120/24) = 120 + 1.01666... × 5 = 125.0833...
            # HALF_UP para 2 casas = 125.08
            assert bd["unit_total"] == pytest.approx(125.08)

    # ─────────────────────────────────────────────────────────────────────
    # Pipeline completo — valida que prorata é compatível com taxa/desconto
    # ─────────────────────────────────────────────────────────────────────

    def test_prorata_passa_por_pipeline_de_taxa_e_desconto(self, app, db):
        """Pipeline full: prorata → subtotal → desconto manual → taxa de serviço.

        Estadia 25h em dia útil (R$125) − desconto manual R$25 = subtotal pós-desconto
        R$100. Taxa de serviço 10% = R$10. Total = R$110.
        """
        with app.app_context():
            from app.models.reservas import ReservaConfig

            source, item = self._setup_camping_item(db, "Camping-pipeline")

            # Configura taxa de serviço 10% para esse source
            db.session.add(ReservaConfig(
                source_id=source.id,
                config_key="service_fee_percent",
                config_value=10.0,
            ))
            db.session.commit()

            ci = datetime.combine(self.QUARTA, datetime.min.time()).replace(hour=14)
            co = ci + timedelta(hours=25)

            r = ReservaPricingService.calculate(
                source_id=source.id,
                check_in_date=ci,
                check_out_date=co,
                items=[{"item_id": item.id, "quantity": 1}],
                manual_discount_percent=20.0,
            )
            assert r["subtotal"] == pytest.approx(125.00)
            assert r["discount_amount"] == pytest.approx(25.00)
            assert r["service_fee"] == pytest.approx(10.00)
            assert r["total"] == pytest.approx(110.00)


# ---------------------------------------------------------------------------
# Separação de calendários — alta temporada passa a valer para o preço
# ---------------------------------------------------------------------------

class TestAltaTemporadaAfetaPreco:

    def test_high_season_com_flag_aplica_tarifa_especial(self, app, db):
        """Alta temporada com afeta_precificacao=True usa price_holiday.

        Antes da separação de calendários, _load_holidays filtrava por tipo e
        HIGH_SEASON não mudava preço nenhum.
        """
        with app.app_context():
            target = date(2027, 7, 15)
            ev = GlobalEvent(
                titulo="Alta Temporada Julho",
                data_evento=date(2027, 7, 1),
                data_fim=date(2027, 7, 31),
                tipo=TipoEvento.HIGH_SEASON,
                afeta_precificacao=True,
                afeta_folha=False,
                recorrente_anual=False,
                origem=GlobalEvent.ORIGEM_RESERVAS,
            )
            db.session.add(ev)
            db.session.commit()

            item = _criar_item(db, price=100.0, price_weekday=90.0,
                               price_weekend=120.0, price_holiday=150.0)
            especiais = ReservaPricingService._load_holidays(
                target, target + timedelta(days=1))

            assert target in especiais
            assert ReservaPricingService._price_for_day(
                target, item, especiais) == Decimal("150.00")

    def test_high_season_sem_flag_nao_muda_preco(self, app, db):
        """Sem afeta_precificacao, a alta temporada é só rótulo no calendário."""
        with app.app_context():
            target = date(2027, 7, 15)
            ev = GlobalEvent(
                titulo="Alta Temporada Julho",
                data_evento=date(2027, 7, 1),
                data_fim=date(2027, 7, 31),
                tipo=TipoEvento.HIGH_SEASON,
                afeta_precificacao=False,
                afeta_folha=False,
                recorrente_anual=False,
            )
            db.session.add(ev)
            db.session.commit()

            assert ReservaPricingService._load_holidays(
                target, target + timedelta(days=1)) == set()

    def test_evento_de_periodo_encarece_todas_as_diarias(self, app, db):
        """Um evento de 5 dias cobre as 5 diárias, não só a de check-in."""
        with app.app_context():
            ev = GlobalEvent(
                titulo="Semana Santa",
                data_evento=date(2027, 3, 22),
                data_fim=date(2027, 3, 26),
                tipo=TipoEvento.NATIONAL_HOLIDAY,
                afeta_precificacao=True,
                afeta_folha=True,
                recorrente_anual=False,
            )
            db.session.add(ev)
            db.session.commit()

            especiais = ReservaPricingService._load_holidays(
                date(2027, 3, 22), date(2027, 3, 27))

            assert especiais == {
                date(2027, 3, 22), date(2027, 3, 23), date(2027, 3, 24),
                date(2027, 3, 25), date(2027, 3, 26),
            }
