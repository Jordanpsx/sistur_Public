# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Persistência de preço e desconto entre versões de uma reserva.

Duas garantias, em camadas:

1. **A edição não reprecifica o que não mudou.** Trocar status, corrigir nome
   ou anexar observação preservam o valor acordado. Só itens, quantidades,
   datas ou o desconto pedido disparam recálculo.
2. **Quando há recálculo, o desconto repassado é só o resíduo** — o que o motor
   de preços não reproduz sozinho. Um desconto automático não pode viajar como
   se fosse manual, senão é aplicado duas vezes.

Regressão do caso real (reserva 4056 em produção): a reserva tinha 40% de
"Reserva Antecipada" — regra automática, com `desconto_percentual_manual`
nulo. Ao mudar o status para PAID, `desconto_efetivo_pct` derivou 40% da razão
`discount_amount/subtotal` e devolveu isso como se fosse manual. O motor
recalculou os 40% automáticos por conta própria e somou os 40% "manuais" por
cima: 360,00 de subtotal viraram 288,00 de desconto e 72,00 de total, quando
o correto eram 144,00 e 216,00. O desconto dobrava a cada versão.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.models.reservas import (
    BillingType,
    CalendarDiscount,
    CalendarDiscountAdvanceTier,
    Reserva,
    ReservaCategory,
    ReservaItem,
    ReservaItemLink,
    ReservaOrigin,
    ReservaSource,
    ReservaStatus,
)
from app.services.reserva_pricing_service import ReservaPricingService
from app.services.reserva_service import ReservaService


# ---------------------------------------------------------------------------
# Cenário: diária de 90,00, 2 unidades, 2 diárias → subtotal 360,00
# ---------------------------------------------------------------------------

def _montar_catalogo(
    db, *, com_regra_antecipada: bool, pct: float = 40.0, dias_antecedencia: int = 3,
):
    """Cria fonte, categoria e item de 90,00/dia; opcionalmente a regra automática.

    `dias_antecedencia` desloca o check-in: com 1 dia, a regra de antecipação
    (que exige 2) já não se aplica no momento da edição.
    """
    src = ReservaSource(name=f"fonte-{uuid.uuid4().hex[:6]}", display_name="Teste")
    db.session.add(src)
    db.session.flush()

    cat = ReservaCategory(source_id=src.id, name=f"cat-{uuid.uuid4().hex[:6]}")
    db.session.add(cat)
    db.session.flush()

    item = ReservaItem(
        category_id=cat.id,
        name="Inteira",
        price=Decimal("90.00"),
        billing_type=BillingType.PER_DAY,
    )
    db.session.add(item)
    db.session.flush()

    check_in = datetime.combine(
        date.today() + timedelta(days=dias_antecedencia), datetime.min.time()
    ).replace(hour=16)
    check_out = check_in + timedelta(days=2)

    if com_regra_antecipada:
        agora = datetime.now(timezone.utc)
        cd = CalendarDiscount(
            source_id=src.id,
            category_id=None,
            discount_date=check_in.date(),
            label="Reserva Antecipada",
            is_active=True,
            criado_em=agora,
            atualizado_em=agora,
        )
        db.session.add(cd)
        db.session.flush()
        db.session.add(CalendarDiscountAdvanceTier(
            calendar_discount_id=cd.id,
            min_days_in_advance=2,
            min_stay_days=None,
            discount_percentage=Decimal(str(pct)),
        ))
        db.session.flush()

    db.session.commit()
    return src, cat, item, check_in, check_out


def _montar_reserva(db, src, cat, item, check_in, check_out, *, desconto, pct_manual=None):
    """Cria a v1 já precificada, como o plugin/WordPress gravaria."""
    subtotal = Decimal("360.00")
    reserva = Reserva(
        group_id=str(uuid.uuid4()),
        version=1,
        source_id=src.id,
        category_id=cat.id,
        status=ReservaStatus.PENDING,
        origin=ReservaOrigin.WEB,
        customer_name="Cliente Teste",
        check_in_date=check_in,
        check_out_date=check_out,
        subtotal_amount=subtotal,
        discount_amount=Decimal(str(desconto)),
        service_fee_amount=Decimal("0.00"),
        total_amount=subtotal - Decimal(str(desconto)),
        desconto_percentual_manual=(
            Decimal(str(pct_manual)) if pct_manual is not None else None
        ),
    )
    db.session.add(reserva)
    db.session.flush()
    db.session.add(ReservaItemLink(
        reserva_id=reserva.id,
        item_id=item.id,
        quantity=2,
        locked_price=subtotal - Decimal(str(desconto)),
    ))
    db.session.commit()
    return reserva


# ---------------------------------------------------------------------------
# desconto_efetivo_pct
# ---------------------------------------------------------------------------

class TestDescontoEfetivoPct:

    def test_desconto_automatico_nao_vira_manual(self, app, db):
        """O 40% de antecipação é reproduzido pelo motor — não deve ser repassado.

        É o defeito da reserva 4056: devolver 40 aqui faz o abatimento ser
        aplicado duas vezes na versão seguinte.
        """
        with app.app_context():
            src, cat, item, ci, co = _montar_catalogo(db, com_regra_antecipada=True)
            # Confere o cenário: o motor sozinho já produz os 144,00
            preco = ReservaPricingService.calculate(
                source_id=src.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 2}],
                category_id=cat.id, manual_discount_percent=0.0,
            )
            assert preco["subtotal"] == 360.00
            assert preco["discount_amount"] == 144.00

            reserva = _montar_reserva(db, src, cat, item, ci, co, desconto="144.00")
            assert ReservaService.desconto_efetivo_pct(reserva) == 0.0

    def test_desconto_embutido_sem_regra_automatica_e_preservado(self, app, db):
        """Cupom do plugin, sem regra automática: o resíduo é todo o desconto."""
        with app.app_context():
            src, cat, item, ci, co = _montar_catalogo(db, com_regra_antecipada=False)
            reserva = _montar_reserva(db, src, cat, item, ci, co, desconto="144.00")
            assert ReservaService.desconto_efetivo_pct(reserva) == 40.0

    def test_percentual_gravado_tem_precedencia(self, app, db):
        """Com `desconto_percentual_manual` preenchido, ele é a verdade."""
        with app.app_context():
            src, cat, item, ci, co = _montar_catalogo(db, com_regra_antecipada=True)
            reserva = _montar_reserva(
                db, src, cat, item, ci, co, desconto="180.00", pct_manual="10.00",
            )
            assert ReservaService.desconto_efetivo_pct(reserva) == 10.0

    def test_residuo_sobre_desconto_automatico(self, app, db):
        """Automático 144 + cupom 36 → só os 36 (10%) seguem como manual."""
        with app.app_context():
            src, cat, item, ci, co = _montar_catalogo(db, com_regra_antecipada=True)
            reserva = _montar_reserva(db, src, cat, item, ci, co, desconto="180.00")
            assert ReservaService.desconto_efetivo_pct(reserva) == 10.0

    def test_sem_desconto_algum(self, app, db):
        with app.app_context():
            src, cat, item, ci, co = _montar_catalogo(db, com_regra_antecipada=False)
            reserva = _montar_reserva(db, src, cat, item, ci, co, desconto="0.00")
            assert ReservaService.desconto_efetivo_pct(reserva) == 0.0


# ---------------------------------------------------------------------------
# Efeito ponta a ponta: mudar de status não pode mexer no valor
# ---------------------------------------------------------------------------

class TestMudancaDeStatusPreservaValor:

    def test_status_para_paid_mantem_total_com_regra_automatica(self, app, db):
        """O caso relatado: PENDING → PAID não pode alterar o total da reserva."""
        with app.app_context():
            src, cat, item, ci, co = _montar_catalogo(db, com_regra_antecipada=True)
            reserva = _montar_reserva(db, src, cat, item, ci, co, desconto="144.00")
            total_antes = float(reserva.total_amount)
            assert total_antes == 216.00

            # Reproduz o que a rota de edição faz: resolve o desconto efetivo e
            # envia price_override com os totais de tabela (sem desconto).
            pct = ReservaService.desconto_efetivo_pct(reserva)
            preco_tabela = ReservaPricingService.calculate(
                source_id=src.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 2}],
                category_id=cat.id,
            )
            breakdown = {e["item_id"]: e["item_total"] for e in preco_tabela["items_breakdown"]}

            nova = ReservaService.atualizar(
                reserva.id,
                items_payload=[{
                    "item_id": item.id,
                    "quantity": 2,
                    "price_override": breakdown[item.id],
                }],
                status="PAID",
                manual_discount_percent=pct,
                ator_id=None,
            )

            assert float(nova.subtotal_amount) == 360.00
            assert float(nova.discount_amount) == 144.00
            assert float(nova.total_amount) == total_antes

    def test_duas_mudancas_seguidas_nao_acumulam(self, app, db):
        """O desconto dobrava a cada versão — três versões precisam ficar iguais."""
        with app.app_context():
            src, cat, item, ci, co = _montar_catalogo(db, com_regra_antecipada=True)
            reserva = _montar_reserva(db, src, cat, item, ci, co, desconto="144.00")

            preco_tabela = ReservaPricingService.calculate(
                source_id=src.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 2}], category_id=cat.id,
            )
            override = {e["item_id"]: e["item_total"] for e in preco_tabela["items_breakdown"]}[item.id]

            atual = reserva
            for novo_status in ("PAID", "COMPLETED"):
                atual = ReservaService.atualizar(
                    atual.id,
                    items_payload=[{
                        "item_id": item.id, "quantity": 2, "price_override": override,
                    }],
                    status=novo_status,
                    manual_discount_percent=ReservaService.desconto_efetivo_pct(atual),
                    ator_id=None,
                )
                assert float(atual.discount_amount) == 144.00
                assert float(atual.total_amount) == 216.00

    def test_cupom_do_plugin_sobrevive_a_mudanca_de_status(self, app, db):
        """Sem regra automática, o desconto embutido continua sendo preservado.

        Guarda contra a correção jogar fora o comportamento que ela protege:
        desconto que o motor não recria precisa seguir para a próxima versão.
        """
        with app.app_context():
            src, cat, item, ci, co = _montar_catalogo(db, com_regra_antecipada=False)
            reserva = _montar_reserva(db, src, cat, item, ci, co, desconto="144.00")

            preco_tabela = ReservaPricingService.calculate(
                source_id=src.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 2}], category_id=cat.id,
            )
            override = {e["item_id"]: e["item_total"] for e in preco_tabela["items_breakdown"]}[item.id]

            nova = ReservaService.atualizar(
                reserva.id,
                items_payload=[{
                    "item_id": item.id, "quantity": 2, "price_override": override,
                }],
                status="PAID",
                manual_discount_percent=ReservaService.desconto_efetivo_pct(reserva),
                ator_id=None,
            )
            assert float(nova.discount_amount) == 144.00
            assert float(nova.total_amount) == 216.00


# ---------------------------------------------------------------------------
# A edição não reprecifica o que não mudou
# ---------------------------------------------------------------------------

class TestEdicaoNaoReprecifica:
    """Uma reserva é um acordo: o valor não muda porque alguém editou metadados.

    `ReservaPricingService.calculate()` avalia as regras no momento da edição —
    preço de catálogo, taxa de serviço e janela de antecipação contada a partir
    de hoje. Sem esta proteção, editar uma reserva antiga a submete às regras
    vigentes agora, e não às que valiam quando foi fechada.
    """

    @staticmethod
    def _payload(item, qtd=2, override=360.00, resource_id=None):
        entrada = {"item_id": item.id, "quantity": qtd, "price_override": override}
        if resource_id is not None:
            entrada["resource_id"] = resource_id
        return [entrada]

    def test_janela_de_antecipacao_vencida_nao_apaga_o_desconto(self, app, db):
        """Check-in amanhã: a regra exige 2 dias e já não vale — o desconto fica.

        Antes, uma reserva fechada com 40% de antecipação perdia o desconto se
        fosse editada depois que a janela passou; o total subia de 216 para 360
        numa simples troca de status.
        """
        with app.app_context():
            src, cat, item, ci, co = _montar_catalogo(
                db, com_regra_antecipada=True, dias_antecedencia=1,
            )
            # Confirma que a regra realmente não se aplica mais
            preco = ReservaPricingService.calculate(
                source_id=src.id, check_in_date=ci, check_out_date=co,
                items=[{"item_id": item.id, "quantity": 2}], category_id=cat.id,
            )
            assert preco["discount_amount"] == 0.00

            reserva = _montar_reserva(db, src, cat, item, ci, co, desconto="144.00")
            nova = ReservaService.atualizar(
                reserva.id,
                items_payload=self._payload(item),
                status="PAID",
                manual_discount_percent=ReservaService.desconto_efetivo_pct(reserva),
                ator_id=None,
            )
            assert float(nova.discount_amount) == 144.00
            assert float(nova.total_amount) == 216.00

    def test_mudanca_no_preco_de_catalogo_nao_entra_retroativamente(self, app, db):
        """Item sobe de 90 para 120 depois de fechada: a reserva mantém o valor."""
        with app.app_context():
            src, cat, item, ci, co = _montar_catalogo(db, com_regra_antecipada=False)
            reserva = _montar_reserva(db, src, cat, item, ci, co, desconto="0.00")
            assert float(reserva.total_amount) == 360.00

            item.price = Decimal("120.00")
            db.session.commit()

            nova = ReservaService.atualizar(
                reserva.id,
                items_payload=self._payload(item),
                status="PAID",
                manual_discount_percent=0.0,
                ator_id=None,
            )
            assert float(nova.subtotal_amount) == 360.00
            assert float(nova.total_amount) == 360.00

    def test_link_preserva_valores_monetarios(self, app, db):
        """Os snapshots por item também são herdados, não recalculados."""
        with app.app_context():
            src, cat, item, ci, co = _montar_catalogo(db, com_regra_antecipada=False)
            reserva = _montar_reserva(db, src, cat, item, ci, co, desconto="144.00")
            locked_anterior = reserva.items[0].locked_price

            item.price = Decimal("500.00")
            db.session.commit()

            nova = ReservaService.atualizar(
                reserva.id,
                items_payload=self._payload(item),
                status="PAID",
                manual_discount_percent=ReservaService.desconto_efetivo_pct(reserva),
                ator_id=None,
            )
            assert nova.items[0].locked_price == locked_anterior

    def test_mudar_itens_reprecifica(self, app, db):
        """Mudou o que se vende → o preço tem de ser recalculado."""
        with app.app_context():
            src, cat, item, ci, co = _montar_catalogo(db, com_regra_antecipada=False)
            reserva = _montar_reserva(db, src, cat, item, ci, co, desconto="0.00")

            nova = ReservaService.atualizar(
                reserva.id,
                items_payload=self._payload(item, qtd=3, override=540.00),
                manual_discount_percent=0.0,
                ator_id=None,
            )
            assert float(nova.subtotal_amount) == 540.00

    def test_mudar_datas_reprecifica(self, app, db):
        """Mais uma diária → subtotal maior."""
        with app.app_context():
            src, cat, item, ci, co = _montar_catalogo(db, com_regra_antecipada=False)
            reserva = _montar_reserva(db, src, cat, item, ci, co, desconto="0.00")

            nova = ReservaService.atualizar(
                reserva.id,
                items_payload=self._payload(item, override=540.00),
                check_out_date=co + timedelta(days=1),
                manual_discount_percent=0.0,
                ator_id=None,
            )
            assert float(nova.subtotal_amount) == 540.00

    def test_mudar_desconto_reprecifica(self, app, db):
        """O operador aplicar um desconto novo é, por definição, reprecificar."""
        with app.app_context():
            src, cat, item, ci, co = _montar_catalogo(db, com_regra_antecipada=False)
            reserva = _montar_reserva(db, src, cat, item, ci, co, desconto="0.00")

            nova = ReservaService.atualizar(
                reserva.id,
                items_payload=self._payload(item),
                manual_discount_percent=25.0,
                ator_id=None,
            )
            assert float(nova.discount_amount) == 90.00
            assert float(nova.total_amount) == 270.00
            assert float(nova.desconto_percentual_manual) == 25.0

    def test_forcar_reprecificacao_recalcula(self, app, db):
        """A porta de saída explícita, para quando reprecificar é o objetivo."""
        with app.app_context():
            src, cat, item, ci, co = _montar_catalogo(db, com_regra_antecipada=False)
            reserva = _montar_reserva(db, src, cat, item, ci, co, desconto="0.00")

            item.price = Decimal("120.00")
            db.session.commit()

            nova = ReservaService.atualizar(
                reserva.id,
                items_payload=self._payload(item, override=480.00),
                manual_discount_percent=0.0,
                forcar_reprecificacao=True,
                ator_id=None,
            )
            assert float(nova.subtotal_amount) == 480.00
