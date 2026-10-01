# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes unitários para o sistema de detecção e prevenção de overbooking.

Cobertura:
  - _verificar_estoque: comparação DateTime vs Date (incidentes de abril/18-19 e 25),
    status customizados, force_overbooking, estoque infinito, acumulação intra-payload
  - _verificar_overbooking_ao_pagar: detecção de conflito pós-pagamento
  - _emitir_reembolso_automatico: fluxo PAID → CANCELLED, preservação da txn APPROVED,
    criação de txn REFUNDED, flag overbooking_extornada, resolução de mp_payment_id nulo
  - cancelar_pagamentos_concorrentes: cancelamento proativo de PIX pendentes
  - Fluxo completo Active Defense via webhook (caminho novo e legado)
  - Casos de borda: mp_payment_id nulo, falha na API de estorno, reserva sem resource

Todos os testes usam SQLite in-memory (TestingConfig) e mocam o SDK do Mercado Pago
para evitar chamadas reais à API externa.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.models.payment import PaymentTransaction, TransactionStatus
from app.models.reservas import (
    Reserva,
    ReservaItemLink,
    ReservaOrigin,
    ReservaResource,
    ReservaSource,
    ReservaStatus,
    ReservaStatusConfig,
)
from app.services.payment_service import (
    MercadoPagoService,
    _verificar_overbooking_ao_pagar,
)
from app.services.reserva_service import OverbookingError, ReservaService


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_source(db) -> ReservaSource:
    """Cria ReservaSource mínima para os testes."""
    src = ReservaSource(name=f"fonte-{uuid.uuid4().hex[:6]}", display_name="Teste")
    db.session.add(src)
    db.session.flush()
    return src


def _make_resource(db, source: ReservaSource, *, stock: int | None = 1) -> ReservaResource:
    """Cria ReservaResource com estoque configurável."""
    res = ReservaResource(
        source_id=source.id,
        name=f"recurso-{uuid.uuid4().hex[:6]}",
        stock_quantity=stock,
    )
    db.session.add(res)
    db.session.flush()
    return res


def _make_reserva(
    db,
    *,
    check_in: datetime,
    check_out: datetime | None = None,
    status: ReservaStatus | str = ReservaStatus.PAID,
    total_amount: float = 100.00,
    customer_name: str = "Cliente",
) -> Reserva:
    """Cria Reserva com check_in/check_out como datetime (replicando o schema real)."""
    reserva = Reserva(
        group_id=str(uuid.uuid4()),
        version=1,
        status=status,
        origin=ReservaOrigin.BALCAO,
        customer_name=customer_name,
        total_amount=Decimal(str(total_amount)),
        check_in_date=check_in,
        check_out_date=check_out,
    )
    db.session.add(reserva)
    db.session.flush()
    return reserva


def _make_link(
    db,
    reserva: Reserva,
    resource: ReservaResource,
    *,
    quantity: int = 1,
) -> ReservaItemLink:
    """Cria ReservaItemLink conectando uma Reserva a um ReservaResource."""
    link = ReservaItemLink(
        reserva_id=reserva.id,
        resource_id=resource.id,
        quantity=quantity,
        locked_price=Decimal("0"),
    )
    db.session.add(link)
    db.session.flush()
    return link


def _make_txn(
    db,
    reserva: Reserva,
    *,
    status: TransactionStatus = TransactionStatus.PENDING,
    amount: float = 100.00,
    mp_payment_id: str | None = None,
) -> PaymentTransaction:
    """Cria PaymentTransaction para uso nos testes."""
    txn = PaymentTransaction(
        id=str(uuid.uuid4()),
        reserva_group_id=reserva.group_id,
        reserva_id=reserva.id,
        status=status,
        amount=Decimal(str(amount)),
        payment_method="pix",
        mp_payment_id=mp_payment_id,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(txn)
    db.session.flush()
    return txn


def _mock_sdk_refund(refund_id: str = "refund-mp-001") -> MagicMock:
    """Cria mock do SDK que simula estorno bem-sucedido.

    Retorna resposta no formato da API v1/payments/:id/refunds da MP:
    status HTTP 201 + body com {id, status: "approved"}.
    """
    sdk = MagicMock()
    sdk.refund().create.return_value = {
        "status": 201,
        "response": {"id": refund_id, "status": "approved"},
    }
    sdk.payment().update.return_value = {"response": {"status": "cancelled"}}
    return sdk


def _mock_sdk_refund_fail() -> MagicMock:
    """Cria mock do SDK que simula falha no estorno."""
    sdk = MagicMock()
    sdk.refund().create.side_effect = RuntimeError("MP timeout")
    sdk.payment().update.return_value = {"response": {}}
    return sdk


def _mock_sdk_webhook(mp_response: dict) -> MagicMock:
    """Cria mock do SDK para testes de webhook."""
    sdk = MagicMock()
    sdk.payment().get.return_value = {"response": mp_response}
    sdk.refund().create.return_value = {
        "status": 201,
        "response": {"id": "refund-wh-001", "status": "approved"},
    }
    sdk.payment().update.return_value = {"response": {"status": "cancelled"}}
    return sdk


# ---------------------------------------------------------------------------
# _verificar_estoque
# ---------------------------------------------------------------------------


class TestVerificarEstoque:
    """Testes para ReservaService._verificar_estoque."""

    def test_bugfix_incidente_abril18_checkout_meia_noite(self, db):
        """Incidente 18/04: reserva existente com checkout às 00:00 do dia X.

        Antes do bugfix: effective_checkout (00:00) >= new_check_in (08:00) → False
        → conflito NÃO detectado (overbooking!).

        Com func.date(): date(00:00) = date(08:00) = mesmo dia → True → conflito detectado.
        """
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        # Reserva PAID: check-in 17/04 08:00, check-out 18/04 00:00 (meia-noite)
        reserva_paga = _make_reserva(
            db,
            check_in=datetime(2026, 4, 17, 8, 0),
            check_out=datetime(2026, 4, 18, 0, 0),  # meia-noite — era a armadilha
            status=ReservaStatus.PAID,
        )
        _make_link(db, reserva_paga, resource, quantity=1)
        db.session.commit()

        # Nova reserva tenta check-in em 18/04 08:00 — mesmo dia do checkout existente
        with pytest.raises(OverbookingError):
            ReservaService._verificar_estoque(
                resource,
                datetime(2026, 4, 18, 8, 0),   # new check-in: 18/04 08:00
                datetime(2026, 4, 19, 0, 0),    # new check-out: 19/04
                requested_qty=1,
            )

    def test_bugfix_incidente_abril25_checkin_com_horario(self, db):
        """Incidente 25/04: reserva existente com check-in às 14:00 do dia X.

        Antes do bugfix: existing.check_in_date (14:00) <= new_check_out (date 00:00) → False
        → conflito NÃO detectado (overbooking!).

        Com func.date(): date(14:00) = date(00:00) = mesmo dia → True → conflito detectado.
        """
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        # Reserva PAID: check-in 25/04 14:00 — horário fora do padrão
        reserva_paga = _make_reserva(
            db,
            check_in=datetime(2026, 4, 25, 14, 0),
            check_out=datetime(2026, 4, 26, 0, 0),
            status=ReservaStatus.PAID,
        )
        _make_link(db, reserva_paga, resource, quantity=1)
        db.session.commit()

        # Nova reserva com check-out em 25/04 (mesmo dia do check-in da PAID)
        with pytest.raises(OverbookingError):
            ReservaService._verificar_estoque(
                resource,
                datetime(2026, 4, 24, 8, 0),  # new check-in: 24/04
                datetime(2026, 4, 25, 0, 0),  # new check-out: 25/04 meia-noite
                requested_qty=1,
            )

    def test_sem_conflito_datas_distintas(self, db):
        """Datas completamente separadas não devem gerar conflito."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        reserva_paga = _make_reserva(
            db,
            check_in=datetime(2026, 5, 1, 8, 0),
            check_out=datetime(2026, 5, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        _make_link(db, reserva_paga, resource, quantity=1)
        db.session.commit()

        # Não levanta exceção — períodos não se sobrepõem
        ReservaService._verificar_estoque(
            resource,
            datetime(2026, 5, 10, 8, 0),
            datetime(2026, 5, 12, 8, 0),
            requested_qty=1,
        )

    def test_estoque_esgotado_levanta_overbooking_error(self, db):
        """Estoque=1 totalmente consumido deve levantar OverbookingError."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        reserva_paga = _make_reserva(
            db,
            check_in=datetime(2026, 6, 1, 8, 0),
            check_out=datetime(2026, 6, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        _make_link(db, reserva_paga, resource, quantity=1)
        db.session.commit()

        with pytest.raises(OverbookingError) as exc_info:
            ReservaService._verificar_estoque(
                resource,
                datetime(2026, 6, 2, 8, 0),
                datetime(2026, 6, 4, 8, 0),
                requested_qty=1,
            )

        assert exc_info.value.resource_name == resource.name
        assert len(exc_info.value.conflitos) >= 1
        conflito = exc_info.value.conflitos[0]
        assert conflito["reserva_id"] == reserva_paga.id

    def test_force_overbooking_nao_levanta_excecao(self, db):
        """force_overbooking=True deve silenciar o OverbookingError."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        reserva_paga = _make_reserva(
            db,
            check_in=datetime(2026, 7, 1, 8, 0),
            check_out=datetime(2026, 7, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        _make_link(db, reserva_paga, resource, quantity=1)
        db.session.commit()

        # Deve passar sem levantar exceção (operador forçou overbooking)
        ReservaService._verificar_estoque(
            resource,
            datetime(2026, 7, 2, 8, 0),
            datetime(2026, 7, 4, 8, 0),
            requested_qty=1,
            force_overbooking=True,
        )

    def test_estoque_infinito_nunca_levanta_excecao(self, db):
        """stock_quantity=None (infinito) nunca deve levantar OverbookingError."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=None)  # infinito

        for _ in range(5):
            r = _make_reserva(
                db,
                check_in=datetime(2026, 8, 1, 8, 0),
                check_out=datetime(2026, 8, 3, 8, 0),
                status=ReservaStatus.PAID,
            )
            _make_link(db, r, resource, quantity=1)
        db.session.commit()

        # Não deve levantar exceção — estoque infinito
        ReservaService._verificar_estoque(
            resource,
            datetime(2026, 8, 2, 8, 0),
            datetime(2026, 8, 4, 8, 0),
            requested_qty=999,
        )

    def test_status_customizado_bloqueia_quando_configurado(self, db):
        """Status customizado (ex: EM_USO) deve bloquear se blocks_resource=True no DB."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        # Adiciona status customizado que bloqueia recurso
        config_em_uso = ReservaStatusConfig(
            internal_name="EM_USO",
            label="Em Uso",
            color_hex="#FF0000",
            blocks_resource=True,
        )
        db.session.add(config_em_uso)

        # Reserva com status customizado "EM_USO" (não existe no enum ReservaStatus)
        reserva_em_uso = _make_reserva(
            db,
            check_in=datetime(2026, 9, 1, 8, 0),
            check_out=datetime(2026, 9, 3, 8, 0),
            status="EM_USO",  # status customizado como string
        )
        _make_link(db, reserva_em_uso, resource, quantity=1)
        db.session.commit()

        # Deve detectar o conflito com reserva EM_USO
        with pytest.raises(OverbookingError):
            ReservaService._verificar_estoque(
                resource,
                datetime(2026, 9, 2, 8, 0),
                datetime(2026, 9, 4, 8, 0),
                requested_qty=1,
            )

    def test_status_canceled_nao_bloqueia(self, db):
        """CANCELED tem blocks_resource=False — não deve bloquear novos bookings."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        # Reserva CANCELED não deve contar como ocupação de estoque
        reserva_cancelada = _make_reserva(
            db,
            check_in=datetime(2026, 10, 1, 8, 0),
            check_out=datetime(2026, 10, 3, 8, 0),
            status=ReservaStatus.CANCELED,
        )
        _make_link(db, reserva_cancelada, resource, quantity=1)
        db.session.commit()

        # Não deve levantar exceção — CANCELED não bloqueia o recurso
        ReservaService._verificar_estoque(
            resource,
            datetime(2026, 10, 2, 8, 0),
            datetime(2026, 10, 4, 8, 0),
            requested_qty=1,
        )

    def test_extra_consumed_acumulo_intra_payload(self, db):
        """extra_consumed deve ser somado ao consumido existente (prevenção de race intra-request)."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=2)

        # 1 unidade já consumida por reserva PAID
        reserva_paga = _make_reserva(
            db,
            check_in=datetime(2026, 11, 1, 8, 0),
            check_out=datetime(2026, 11, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        _make_link(db, reserva_paga, resource, quantity=1)
        db.session.commit()

        # extra_consumed=1 simula outro item no mesmo payload já consumindo 1 unidade.
        # stock=2, consumed=1, extra=1, requested=1 → total=3 > 2 → OverbookingError
        with pytest.raises(OverbookingError):
            ReservaService._verificar_estoque(
                resource,
                datetime(2026, 11, 2, 8, 0),
                datetime(2026, 11, 3, 8, 0),
                requested_qty=1,
                extra_consumed=1,
            )

    def test_exclude_reserva_id_nao_conta_propria_reserva(self, db):
        """exclude_reserva_id deve excluir a própria reserva da contagem (edições)."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        # Reserva PAID que está sendo editada (ela própria)
        reserva_propria = _make_reserva(
            db,
            check_in=datetime(2026, 12, 1, 8, 0),
            check_out=datetime(2026, 12, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        _make_link(db, reserva_propria, resource, quantity=1)
        db.session.commit()

        # Com exclude_reserva_id, a própria reserva não conta → sem conflito
        ReservaService._verificar_estoque(
            resource,
            datetime(2026, 12, 1, 8, 0),
            datetime(2026, 12, 3, 8, 0),
            requested_qty=1,
            exclude_reserva_id=reserva_propria.id,
        )


# ---------------------------------------------------------------------------
# _verificar_overbooking_ao_pagar
# ---------------------------------------------------------------------------


class TestVerificarOverbookingAoPagar:
    """Testes para _verificar_overbooking_ao_pagar (chamado após pagamento aprovado)."""

    def test_conflito_detectado_slot_ocupado(self, db):
        """Deve retornar lista de conflitos quando há reserva PAID no mesmo recurso/data."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        # Reserva já paga e aprovada que ocupa o slot
        reserva_paga = _make_reserva(
            db,
            check_in=datetime(2026, 3, 1, 8, 0),
            check_out=datetime(2026, 3, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        _make_link(db, reserva_paga, resource, quantity=1)

        # Nova reserva (ainda PENDING) que foi paga agora — gera conflito
        nova_reserva = _make_reserva(
            db,
            check_in=datetime(2026, 3, 2, 8, 0),
            check_out=datetime(2026, 3, 4, 8, 0),
            status=ReservaStatus.PAID,  # acabou de virar PAID pelo webhook
        )
        _make_link(db, nova_reserva, resource, quantity=1)
        db.session.commit()

        conflitos = _verificar_overbooking_ao_pagar(nova_reserva.id)

        assert len(conflitos) >= 1
        ids_conflito = {c["reserva_id"] for c in conflitos}
        assert reserva_paga.id in ids_conflito

    def test_sem_conflito_slot_disponivel(self, db):
        """Deve retornar lista vazia quando não há sobreposição de reservas."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=2)

        # Reserva PAID que usa 1 de 2 vagas
        reserva_existente = _make_reserva(
            db,
            check_in=datetime(2026, 3, 1, 8, 0),
            check_out=datetime(2026, 3, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        _make_link(db, reserva_existente, resource, quantity=1)

        # Nova reserva usa a segunda vaga — sem conflito
        nova_reserva = _make_reserva(
            db,
            check_in=datetime(2026, 3, 2, 8, 0),
            check_out=datetime(2026, 3, 4, 8, 0),
            status=ReservaStatus.PAID,
        )
        _make_link(db, nova_reserva, resource, quantity=1)
        db.session.commit()

        conflitos = _verificar_overbooking_ao_pagar(nova_reserva.id)
        assert conflitos == []

    def test_sem_resource_link_retorna_vazia(self, db):
        """Reserva sem ReservaItemLink com resource_id deve retornar lista vazia."""
        reserva = _make_reserva(
            db,
            check_in=datetime(2026, 3, 1, 8, 0),
            check_out=datetime(2026, 3, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        # Sem link de recurso — não participa de overbooking check
        db.session.commit()

        conflitos = _verificar_overbooking_ao_pagar(reserva.id)
        assert conflitos == []

    def test_reserva_inexistente_retorna_vazia(self, db):
        """ID de reserva inexistente deve retornar lista vazia sem exceção."""
        conflitos = _verificar_overbooking_ao_pagar(999999)
        assert conflitos == []


# ---------------------------------------------------------------------------
# Transição manual de status com múltiplos recursos
# ---------------------------------------------------------------------------


class TestTransicaoStatusMultiplosRecursos:
    """Revalidação de estoque ao mudar o status para um valor bloqueante.

    `extra_consumed` acumula consumo **dentro do payload** para impedir que
    dois itens da mesma reserva consumam a mesma unidade física duas vezes.
    Ele só pode somar dentro do mesmo `resource_id` — somar num contador
    global faz cada recurso herdar o consumo dos anteriores e inventa um
    overbooking em recurso livre.
    """

    def test_dois_recursos_distintos_livres_permitem_pagar(self, db):
        """Reserva com 2 recursos distintos (estoque 1 cada) e ambos livres → PAID passa.

        Caso real: churrasqueiras B6 + B8 em 10/10/2026, nenhuma ocupada por
        terceiros, e ainda assim a transição para PAID era barrada no segundo
        item com `consumido=0 extra=1 solicitado=1`.
        """
        src = _make_source(db)
        recurso_a = _make_resource(db, src, stock=1)
        recurso_b = _make_resource(db, src, stock=1)

        reserva = _make_reserva(
            db,
            check_in=datetime(2026, 10, 10, 8, 0),
            check_out=datetime(2026, 10, 11, 17, 0),
            status=ReservaStatus.PENDING,
        )
        _make_link(db, reserva, recurso_a, quantity=1)
        _make_link(db, reserva, recurso_b, quantity=1)
        db.session.commit()

        nova = ReservaService.atualizar(
            reserva.id, items_payload=[], status="PAID", ator_id=None,
        )

        assert nova.status == ReservaStatus.PAID or str(nova.status) == "PAID"

    def test_tres_recursos_distintos_livres_permitem_pagar(self, db):
        """O erro crescia com a quantidade de itens — o 3º herdava extra=2."""
        src = _make_source(db)
        recursos = [_make_resource(db, src, stock=1) for _ in range(3)]

        reserva = _make_reserva(
            db,
            check_in=datetime(2026, 10, 10, 8, 0),
            check_out=datetime(2026, 10, 11, 17, 0),
            status=ReservaStatus.PENDING,
        )
        for rec in recursos:
            _make_link(db, reserva, rec, quantity=1)
        db.session.commit()

        nova = ReservaService.atualizar(
            reserva.id, items_payload=[], status="PAID", ator_id=None,
        )

        assert nova.status == ReservaStatus.PAID or str(nova.status) == "PAID"

    def test_recurso_realmente_ocupado_continua_bloqueando(self, db):
        """A proteção real não pode ser afrouxada: recurso tomado por terceiro barra."""
        src = _make_source(db)
        recurso_a = _make_resource(db, src, stock=1)
        recurso_b = _make_resource(db, src, stock=1)

        # Terceiro já pago ocupando o recurso B no mesmo período
        de_outro = _make_reserva(
            db,
            check_in=datetime(2026, 10, 10, 8, 0),
            check_out=datetime(2026, 10, 11, 17, 0),
            status=ReservaStatus.PAID,
            customer_name="Outro cliente",
        )
        _make_link(db, de_outro, recurso_b, quantity=1)

        reserva = _make_reserva(
            db,
            check_in=datetime(2026, 10, 10, 8, 0),
            check_out=datetime(2026, 10, 11, 17, 0),
            status=ReservaStatus.PENDING,
        )
        _make_link(db, reserva, recurso_a, quantity=1)
        _make_link(db, reserva, recurso_b, quantity=1)
        db.session.commit()

        with pytest.raises(OverbookingError):
            ReservaService.atualizar(
                reserva.id, items_payload=[], status="PAID", ator_id=None,
            )

    def test_quantidade_acima_do_estoque_no_mesmo_recurso_bloqueia(self, db):
        """Um único recurso pedindo mais do que tem em estoque continua barrado.

        A acumulação entre itens do mesmo recurso não pôde ser exercitada aqui
        porque `sistur_reserva_item_links` tem unique `(reserva_id, resource_id)`
        — duas linhas para o mesmo recurso não existem. O cenário equivalente é
        uma linha com quantidade acima do estoque, e ele segue bloqueando.
        A soma de `extra_consumed` em si está coberta por
        `test_extra_consumed_acumulo_intra_payload`.
        """
        src = _make_source(db)
        recurso = _make_resource(db, src, stock=1)

        reserva = _make_reserva(
            db,
            check_in=datetime(2026, 10, 10, 8, 0),
            check_out=datetime(2026, 10, 11, 17, 0),
            status=ReservaStatus.PENDING,
        )
        _make_link(db, reserva, recurso, quantity=2)
        db.session.commit()

        with pytest.raises(OverbookingError):
            ReservaService.atualizar(
                reserva.id, items_payload=[], status="PAID", ator_id=None,
            )


# ---------------------------------------------------------------------------
# _emitir_reembolso_automatico
# ---------------------------------------------------------------------------


class TestEmitirReembolsoAutomatico:
    """Testes para MercadoPagoService._emitir_reembolso_automatico."""

    def test_reserva_paid_torna_se_cancelled(self, db):
        """Reserva PAID deve ser revertida para CANCELLED após extorno."""
        reserva = _make_reserva(
            db,
            check_in=datetime(2026, 4, 1, 8, 0),
            check_out=datetime(2026, 4, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        txn = _make_txn(
            db, reserva, status=TransactionStatus.APPROVED, mp_payment_id="mp-abc-001"
        )
        db.session.commit()

        with patch.object(
            MercadoPagoService, "_sdk", return_value=_mock_sdk_refund()
        ), patch("app.services.payment_service._notificar_admins"):
            MercadoPagoService._emitir_reembolso_automatico(
                mp_payment_id="mp-abc-001",
                txn_id=txn.id,
                reserva_id=reserva.id,
                conflitos=[],
            )

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.CANCELED

    def test_flag_overbooking_extornada_setada(self, db):
        """overbooking_extornada deve ser True após o extorno automático."""
        reserva = _make_reserva(
            db,
            check_in=datetime(2026, 4, 1, 8, 0),
            check_out=datetime(2026, 4, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        txn = _make_txn(
            db, reserva, status=TransactionStatus.APPROVED, mp_payment_id="mp-abc-002"
        )
        db.session.commit()

        with patch.object(
            MercadoPagoService, "_sdk", return_value=_mock_sdk_refund()
        ), patch("app.services.payment_service._notificar_admins"):
            MercadoPagoService._emitir_reembolso_automatico(
                mp_payment_id="mp-abc-002",
                txn_id=txn.id,
                reserva_id=reserva.id,
                conflitos=[],
            )

        db.session.refresh(reserva)
        assert reserva.overbooking_extornada is True

    def test_txn_original_aprovada_preservada(self, db):
        """A transação APPROVED original NÃO deve ser mutada para REFUNDED.

        Isso reproduz o bug onde apenas o card de extorno aparecia no carrossel
        sem o card de pagamento original.
        """
        reserva = _make_reserva(
            db,
            check_in=datetime(2026, 4, 1, 8, 0),
            check_out=datetime(2026, 4, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        txn_original = _make_txn(
            db, reserva, status=TransactionStatus.APPROVED, mp_payment_id="mp-abc-003"
        )
        db.session.commit()

        with patch.object(
            MercadoPagoService, "_sdk", return_value=_mock_sdk_refund()
        ), patch("app.services.payment_service._notificar_admins"):
            MercadoPagoService._emitir_reembolso_automatico(
                mp_payment_id="mp-abc-003",
                txn_id=txn_original.id,
                reserva_id=reserva.id,
                conflitos=[],
            )

        db.session.refresh(txn_original)
        # Transação original DEVE continuar como APPROVED (evidência do dinheiro recebido)
        assert txn_original.status == TransactionStatus.APPROVED

    def test_nova_txn_refunded_criada(self, db):
        """Uma nova PaymentTransaction com status REFUNDED deve ser criada."""
        reserva = _make_reserva(
            db,
            check_in=datetime(2026, 4, 1, 8, 0),
            check_out=datetime(2026, 4, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        txn_original = _make_txn(
            db, reserva, status=TransactionStatus.APPROVED, mp_payment_id="mp-abc-004"
        )
        db.session.commit()

        with patch.object(
            MercadoPagoService, "_sdk", return_value=_mock_sdk_refund("refund-mp-999")
        ), patch("app.services.payment_service._notificar_admins"):
            MercadoPagoService._emitir_reembolso_automatico(
                mp_payment_id="mp-abc-004",
                txn_id=txn_original.id,
                reserva_id=reserva.id,
                conflitos=[],
            )

        # Deve existir uma txn REFUNDED adicional (além da APPROVED original)
        txns = (
            db.session.query(PaymentTransaction)
            .filter_by(reserva_group_id=reserva.group_id)
            .all()
        )
        statuses = [t.status for t in txns]
        assert TransactionStatus.APPROVED in statuses
        assert TransactionStatus.REFUNDED in statuses

        refunded_txn = next(t for t in txns if t.status == TransactionStatus.REFUNDED)
        assert refunded_txn.refund_id == "refund-mp-999"
        assert refunded_txn.refunded_at is not None

    def test_mp_payment_id_resolvido_da_txn_persistida(self, db):
        """real_mp_payment_id deve preferir o valor já persistido em txn.mp_payment_id.

        Reproduz o bug onde o parâmetro mp_payment_id era passado como ID bruto do
        webhook, mas a txn já tinha o valor correto salvo pelo processador de notificação.
        """
        reserva = _make_reserva(
            db,
            check_in=datetime(2026, 4, 1, 8, 0),
            check_out=datetime(2026, 4, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        # txn.mp_payment_id é o valor correto persistido
        txn_original = _make_txn(
            db,
            reserva,
            status=TransactionStatus.APPROVED,
            mp_payment_id="mp-correto-persistido",
        )
        db.session.commit()

        captured_refund_calls = []

        def mock_refunds(payment_id, request_options=None, **_kwargs):
            captured_refund_calls.append(payment_id)
            return {
                "status": 201,
                "response": {"id": "refund-ok", "status": "approved"},
            }

        sdk = MagicMock()
        sdk.refund().create.side_effect = mock_refunds

        with patch.object(MercadoPagoService, "_sdk", return_value=sdk), patch(
            "app.services.payment_service._notificar_admins"
        ):
            MercadoPagoService._emitir_reembolso_automatico(
                mp_payment_id="mp-parametro-desatualizado",  # parâmetro stale
                txn_id=txn_original.id,
                reserva_id=reserva.id,
                conflitos=[],
            )

        # Deve ter chamado o refund com o ID persistido (correto), não o parâmetro
        assert captured_refund_calls == ["mp-correto-persistido"]

    def test_falha_na_api_mp_ainda_cancela_localmente(self, db):
        """Falha na API de estorno do MP não deve impedir o cancelamento local da reserva."""
        reserva = _make_reserva(
            db,
            check_in=datetime(2026, 4, 1, 8, 0),
            check_out=datetime(2026, 4, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        txn_original = _make_txn(
            db, reserva, status=TransactionStatus.APPROVED, mp_payment_id="mp-abc-006"
        )
        db.session.commit()

        with patch.object(
            MercadoPagoService, "_sdk", return_value=_mock_sdk_refund_fail()
        ), patch("app.services.payment_service._notificar_admins"):
            MercadoPagoService._emitir_reembolso_automatico(
                mp_payment_id="mp-abc-006",
                txn_id=txn_original.id,
                reserva_id=reserva.id,
                conflitos=[],
            )

        # Reserva deve ser cancelada localmente mesmo com falha no MP
        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.CANCELED
        assert reserva.overbooking_extornada is True

    def test_aviso2_enviado_apos_extorno(self, db):
        """Aviso #2 ('Extorno concluído') deve ser disparado após o cancelamento."""
        reserva = _make_reserva(
            db,
            check_in=datetime(2026, 4, 1, 8, 0),
            check_out=datetime(2026, 4, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        txn = _make_txn(
            db, reserva, status=TransactionStatus.APPROVED, mp_payment_id="mp-abc-007"
        )
        db.session.commit()

        notificar_calls = []

        def fake_notificar(titulo, mensagem, log_tag):
            notificar_calls.append(titulo)

        with patch.object(
            MercadoPagoService, "_sdk", return_value=_mock_sdk_refund()
        ), patch(
            "app.services.payment_service._notificar_admins", side_effect=fake_notificar
        ):
            MercadoPagoService._emitir_reembolso_automatico(
                mp_payment_id="mp-abc-007",
                txn_id=txn.id,
                reserva_id=reserva.id,
                conflitos=[],
            )

        # Deve ter disparado o Aviso #2 (Extorno concluído)
        assert any("Extorno concluído" in titulo for titulo in notificar_calls), (
            f"Aviso #2 não foi disparado. Calls: {notificar_calls}"
        )

    def test_caminho_legado_sem_txn_id(self, db):
        """Caminho legado (txn_id=None) deve funcionar inferindo dados da Reserva."""
        reserva = _make_reserva(
            db,
            check_in=datetime(2026, 4, 1, 8, 0),
            check_out=datetime(2026, 4, 3, 8, 0),
            status=ReservaStatus.PAID,
            total_amount=80.00,
        )
        db.session.commit()

        with patch.object(
            MercadoPagoService, "_sdk", return_value=_mock_sdk_refund()
        ), patch("app.services.payment_service._notificar_admins"):
            MercadoPagoService._emitir_reembolso_automatico(
                mp_payment_id="mp-legado-001",
                txn_id=None,  # caminho legado: sem txn_id
                reserva_id=reserva.id,
                conflitos=[],
            )

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.CANCELED
        assert reserva.overbooking_extornada is True

        # Deve ter criado uma txn REFUNDED com o amount da reserva
        txn_refunded = (
            db.session.query(PaymentTransaction)
            .filter_by(
                reserva_group_id=reserva.group_id,
                status=TransactionStatus.REFUNDED,
            )
            .first()
        )
        assert txn_refunded is not None
        assert float(txn_refunded.amount) == 80.00


# ---------------------------------------------------------------------------
# cancelar_pagamentos_concorrentes
# ---------------------------------------------------------------------------


class TestCancelarPagamentosConcorrentes:
    """Testes para MercadoPagoService.cancelar_pagamentos_concorrentes."""

    def test_reservas_pending_concorrentes_canceladas(self, db):
        """Reservas PENDING no mesmo recurso/período devem ser canceladas."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        # Reserva aprovada (acabou de ser paga)
        reserva_aprovada = _make_reserva(
            db,
            check_in=datetime(2026, 5, 1, 8, 0),
            check_out=datetime(2026, 5, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        _make_link(db, reserva_aprovada, resource, quantity=1)

        # Reserva concorrente PENDING (aguardando PIX) no mesmo período
        reserva_concorrente = _make_reserva(
            db,
            check_in=datetime(2026, 5, 2, 8, 0),
            check_out=datetime(2026, 5, 4, 8, 0),
            status=ReservaStatus.PENDING,
        )
        _make_link(db, reserva_concorrente, resource, quantity=1)
        txn_concorrente = _make_txn(
            db,
            reserva_concorrente,
            status=TransactionStatus.PENDING,
            mp_payment_id="mp-pix-concorrente",
        )
        db.session.commit()

        with patch.object(
            MercadoPagoService, "_sdk", return_value=_mock_sdk_refund()
        ):
            MercadoPagoService.cancelar_pagamentos_concorrentes(reserva_aprovada.id)

        db.session.refresh(reserva_concorrente)
        db.session.refresh(txn_concorrente)

        assert reserva_concorrente.status == ReservaStatus.CANCELED
        assert txn_concorrente.status == TransactionStatus.CANCELLED

    def test_reservas_sem_sobreposicao_nao_afetadas(self, db):
        """Reservas em períodos distintos não devem ser canceladas."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        reserva_aprovada = _make_reserva(
            db,
            check_in=datetime(2026, 6, 1, 8, 0),
            check_out=datetime(2026, 6, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        _make_link(db, reserva_aprovada, resource, quantity=1)

        # Reserva em período diferente — não conflita
        reserva_distante = _make_reserva(
            db,
            check_in=datetime(2026, 6, 10, 8, 0),
            check_out=datetime(2026, 6, 12, 8, 0),
            status=ReservaStatus.PENDING,
        )
        _make_link(db, reserva_distante, resource, quantity=1)
        db.session.commit()

        with patch.object(
            MercadoPagoService, "_sdk", return_value=_mock_sdk_refund()
        ):
            MercadoPagoService.cancelar_pagamentos_concorrentes(reserva_aprovada.id)

        db.session.refresh(reserva_distante)
        assert reserva_distante.status == ReservaStatus.PENDING  # não alterada

    def test_reserva_sem_resource_link_retorna_sem_erro(self, db):
        """Reserva aprovada sem ReservaItemLink deve retornar silenciosamente."""
        reserva_aprovada = _make_reserva(
            db,
            check_in=datetime(2026, 7, 1, 8, 0),
            check_out=datetime(2026, 7, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        # Sem link de recurso — cancelar_pagamentos_concorrentes deve retornar cedo
        db.session.commit()

        with patch.object(
            MercadoPagoService, "_sdk", return_value=_mock_sdk_refund()
        ):
            # Não deve levantar exceção
            MercadoPagoService.cancelar_pagamentos_concorrentes(reserva_aprovada.id)

    def test_falha_na_api_mp_cancela_reserva_mesmo_assim(self, db):
        """Falha ao chamar MP para cancelar PIX deve cancelar a Reserva localmente."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        reserva_aprovada = _make_reserva(
            db,
            check_in=datetime(2026, 8, 1, 8, 0),
            check_out=datetime(2026, 8, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        _make_link(db, reserva_aprovada, resource, quantity=1)

        reserva_concorrente = _make_reserva(
            db,
            check_in=datetime(2026, 8, 2, 8, 0),
            check_out=datetime(2026, 8, 4, 8, 0),
            status=ReservaStatus.PENDING,
        )
        _make_link(db, reserva_concorrente, resource, quantity=1)
        _make_txn(
            db,
            reserva_concorrente,
            status=TransactionStatus.PENDING,
            mp_payment_id="mp-pix-fail",
        )
        db.session.commit()

        sdk = MagicMock()
        sdk.payment().update.side_effect = RuntimeError("MP indisponível")

        with patch.object(MercadoPagoService, "_sdk", return_value=sdk):
            MercadoPagoService.cancelar_pagamentos_concorrentes(reserva_aprovada.id)

        db.session.refresh(reserva_concorrente)
        # Reserva cancelada localmente mesmo com falha no MP
        assert reserva_concorrente.status == ReservaStatus.CANCELED


# ---------------------------------------------------------------------------
# Fluxo completo Active Defense via webhook
# ---------------------------------------------------------------------------


class TestActiveDefenseWebhookFlow:
    """Testes de integração para o fluxo completo de defesa ativa via webhook."""

    def test_webhook_aprovado_com_overbooking_cancela_e_extorna(self, db):
        """Fluxo completo: webhook approved → PAID → overbooking → REFUNDED → CANCELLED.

        Reproduz o incidente de 18/04: pagamento aprovado mas slot já ocupado.
        A reserva deve:
          1. Virar PAID (empresa registra o dinheiro)
          2. Detectar overbooking
          3. Emitir extorno automático no MP
          4. Virar CANCELLED com overbooking_extornada=True
        """
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        # Reserva já PAID que ocupa o slot
        reserva_existente = _make_reserva(
            db,
            check_in=datetime(2026, 4, 18, 8, 0),
            check_out=datetime(2026, 4, 20, 8, 0),
            status=ReservaStatus.PAID,
        )
        _make_link(db, reserva_existente, resource, quantity=1)

        # Nova reserva PENDING que vai ser paga via webhook
        nova_reserva = _make_reserva(
            db,
            check_in=datetime(2026, 4, 18, 8, 0),
            check_out=datetime(2026, 4, 20, 8, 0),
            status=ReservaStatus.PENDING,
            total_amount=100.00,
        )
        _make_link(db, nova_reserva, resource, quantity=1)
        txn = _make_txn(
            db, nova_reserva, status=TransactionStatus.PENDING, amount=100.00
        )
        db.session.commit()

        mp_response = {
            "id": "mp-webhook-001",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": 100.00,
            "payment_type_id": "bank_transfer",
            "date_approved": "2026-04-18T10:00:00.000Z",
            "external_reference": txn.id,  # UUID da PaymentTransaction
        }

        with patch.object(
            MercadoPagoService, "_sdk", return_value=_mock_sdk_webhook(mp_response)
        ), patch("app.services.payment_service._notificar_admins"):
            MercadoPagoService.processar_webhook(
                {"type": "payment", "data": {"id": "mp-webhook-001"}}
            )

        db.session.refresh(nova_reserva)
        db.session.refresh(txn)

        # A reserva deve estar CANCELLED (não PAID) após o extorno
        assert nova_reserva.status == ReservaStatus.CANCELED
        assert nova_reserva.overbooking_extornada is True

        # A txn original deve permanecer APPROVED
        assert txn.status == TransactionStatus.APPROVED

        # Uma nova txn REFUNDED deve ter sido criada
        txns = (
            db.session.query(PaymentTransaction)
            .filter_by(reserva_group_id=nova_reserva.group_id)
            .all()
        )
        statuses = {t.status for t in txns}
        assert TransactionStatus.APPROVED in statuses
        assert TransactionStatus.REFUNDED in statuses

    def test_webhook_aprovado_limpo_preserva_concorrente_com_capacidade(self, db):
        """Fluxo limpo: webhook approved não cancela concorrente quando há estoque suficiente.

        stock=2, cada reserva usa quantity=1. PENDING tem blocks_resource=True, portanto:
        - A aprovada passa no check de overbooking (consumed=1 pelo concorrente, +1 aprovada ≤ 2)
        - Após aprovação: consumed_PAID=1, concorrente precisa de 1 → 1+1=2 ≤ 2 → não cancela

        O comportamento correto é manter a concorrente PENDING: há capacidade para ambas.
        A lógica antiga (cancelar tudo) estava errada — este teste valida a correção.
        """
        src = _make_source(db)
        resource = _make_resource(db, src, stock=2)  # dois slots disponíveis

        reserva_aprovada = _make_reserva(
            db,
            check_in=datetime(2026, 5, 1, 8, 0),
            check_out=datetime(2026, 5, 3, 8, 0),
            status=ReservaStatus.PENDING,
            total_amount=100.00,
        )
        _make_link(db, reserva_aprovada, resource, quantity=1)
        txn_aprovada = _make_txn(
            db, reserva_aprovada, status=TransactionStatus.PENDING, amount=100.00
        )

        # Concorrente usa 1 de 2 vagas — ainda cabe após a aprovação
        reserva_concorrente = _make_reserva(
            db,
            check_in=datetime(2026, 5, 2, 8, 0),
            check_out=datetime(2026, 5, 4, 8, 0),
            status=ReservaStatus.PENDING,
        )
        _make_link(db, reserva_concorrente, resource, quantity=1)
        _make_txn(
            db,
            reserva_concorrente,
            status=TransactionStatus.PENDING,
            mp_payment_id="mp-pix-concorrente-002",
        )
        db.session.commit()

        mp_response = {
            "id": "mp-webhook-002",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": 100.00,
            "payment_type_id": "bank_transfer",
            "date_approved": "2026-05-01T10:00:00.000Z",
            "external_reference": txn_aprovada.id,
        }

        with patch.object(
            MercadoPagoService, "_sdk", return_value=_mock_sdk_webhook(mp_response)
        ), patch("app.services.payment_service._notificar_admins"):
            MercadoPagoService.processar_webhook(
                {"type": "payment", "data": {"id": "mp-webhook-002"}}
            )

        db.session.refresh(reserva_aprovada)
        db.session.refresh(reserva_concorrente)

        assert reserva_aprovada.status == ReservaStatus.PAID
        # Concorrente deve permanecer PENDING — há capacidade disponível para ambas
        assert reserva_concorrente.status == ReservaStatus.PENDING

    def test_aviso1_enviado_antes_do_extorno(self, db):
        """Aviso #1 ('Overbooking: reserva paga — extorno em processamento') deve ser disparado."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        reserva_existente = _make_reserva(
            db,
            check_in=datetime(2026, 6, 1, 8, 0),
            check_out=datetime(2026, 6, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        _make_link(db, reserva_existente, resource, quantity=1)

        nova_reserva = _make_reserva(
            db,
            check_in=datetime(2026, 6, 1, 8, 0),
            check_out=datetime(2026, 6, 3, 8, 0),
            status=ReservaStatus.PENDING,
            total_amount=100.00,
        )
        _make_link(db, nova_reserva, resource, quantity=1)
        txn = _make_txn(
            db, nova_reserva, status=TransactionStatus.PENDING, amount=100.00
        )
        db.session.commit()

        mp_response = {
            "id": "mp-wh-aviso",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": 100.00,
            "payment_type_id": "bank_transfer",
            "date_approved": "2026-06-01T10:00:00.000Z",
            "external_reference": txn.id,
        }

        notificar_calls = []

        def fake_notificar(titulo, mensagem, log_tag):
            notificar_calls.append(titulo)

        with patch.object(
            MercadoPagoService, "_sdk", return_value=_mock_sdk_webhook(mp_response)
        ), patch(
            "app.services.payment_service._notificar_admins",
            side_effect=fake_notificar,
        ):
            MercadoPagoService.processar_webhook(
                {"type": "payment", "data": {"id": "mp-wh-aviso"}}
            )

        titulos = " | ".join(notificar_calls)
        assert "Overbooking" in titulos, (
            f"Aviso #1 não detectado. Avisos disparados: {notificar_calls}"
        )
        assert "Extorno concluído" in titulos, (
            f"Aviso #2 não detectado. Avisos disparados: {notificar_calls}"
        )

    def test_webhook_legado_com_overbooking(self, db):
        """Caminho legado (sem external_reference UUID) deve também acionar defesa ativa."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        # Reserva PAID que ocupa o slot
        reserva_existente = _make_reserva(
            db,
            check_in=datetime(2026, 7, 1, 8, 0),
            check_out=datetime(2026, 7, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        _make_link(db, reserva_existente, resource, quantity=1)

        # Nova reserva com payment_id legado (sem txn UUID)
        nova_reserva = _make_reserva(
            db,
            check_in=datetime(2026, 7, 1, 8, 0),
            check_out=datetime(2026, 7, 3, 8, 0),
            status=ReservaStatus.PENDING,
            total_amount=100.00,
        )
        nova_reserva.payment_id = "legacy-pay-001"
        _make_link(db, nova_reserva, resource, quantity=1)
        db.session.commit()

        mp_response = {
            "id": "legacy-pay-001",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": 100.00,
            "payment_type_id": "bank_transfer",
            "date_approved": "2026-07-01T10:00:00.000Z",
            "external_reference": "",  # ausente → caminho legado
        }

        with patch.object(
            MercadoPagoService, "_sdk", return_value=_mock_sdk_webhook(mp_response)
        ), patch("app.services.payment_service._notificar_admins"):
            MercadoPagoService.processar_webhook(
                {"type": "payment", "data": {"id": "legacy-pay-001"}}
            )

        db.session.refresh(nova_reserva)
        assert nova_reserva.status == ReservaStatus.CANCELED
        assert nova_reserva.overbooking_extornada is True


# ---------------------------------------------------------------------------
# Casos de borda
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Casos de borda e cenários atípicos do sistema de overbooking."""

    def test_mp_payment_id_nulo_nao_chama_api_refund(self, db):
        """Se mp_payment_id for nulo e txn também não tiver, não deve chamar MP Refund."""
        reserva = _make_reserva(
            db,
            check_in=datetime(2026, 4, 1, 8, 0),
            check_out=datetime(2026, 4, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        # txn sem mp_payment_id (pagamento não chegou a ser processado pelo MP)
        txn = _make_txn(db, reserva, status=TransactionStatus.APPROVED, mp_payment_id=None)
        db.session.commit()

        sdk_mock = _mock_sdk_refund()

        with patch.object(MercadoPagoService, "_sdk", return_value=sdk_mock), patch(
            "app.services.payment_service._notificar_admins"
        ):
            MercadoPagoService._emitir_reembolso_automatico(
                mp_payment_id="",  # vazio
                txn_id=txn.id,
                reserva_id=reserva.id,
                conflitos=[],
            )

        # Sem mp_payment_id real, a API de refund não deve ter sido chamada
        sdk_mock.payment().refunds.assert_not_called()

        # Mas a reserva ainda deve ser cancelada localmente
        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.CANCELED

    def test_reserva_ja_cancelada_nao_muda_novamente(self, db):
        """Reserva já CANCELLED não deve ser alterada por extorno duplicado."""
        reserva = _make_reserva(
            db,
            check_in=datetime(2026, 4, 1, 8, 0),
            check_out=datetime(2026, 4, 3, 8, 0),
            status=ReservaStatus.CANCELED,
        )
        txn = _make_txn(
            db, reserva, status=TransactionStatus.APPROVED, mp_payment_id="mp-dup-001"
        )
        db.session.commit()

        with patch.object(
            MercadoPagoService, "_sdk", return_value=_mock_sdk_refund()
        ), patch("app.services.payment_service._notificar_admins"):
            MercadoPagoService._emitir_reembolso_automatico(
                mp_payment_id="mp-dup-001",
                txn_id=txn.id,
                reserva_id=reserva.id,
                conflitos=[],
            )

        db.session.refresh(reserva)
        # Status não deve mudar — já estava CANCELLED
        assert reserva.status == ReservaStatus.CANCELED

    def test_overbooking_error_inclui_dados_dos_conflitos(self, db):
        """OverbookingError.conflitos deve incluir reserva_id, customer_name, check_in, check_out e status."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        reserva_paga = _make_reserva(
            db,
            check_in=datetime(2026, 4, 1, 8, 0),
            check_out=datetime(2026, 4, 3, 8, 0),
            status=ReservaStatus.PAID,
            customer_name="Ana Conflito",
        )
        _make_link(db, reserva_paga, resource, quantity=1)
        db.session.commit()

        with pytest.raises(OverbookingError) as exc_info:
            ReservaService._verificar_estoque(
                resource,
                datetime(2026, 4, 2, 8, 0),
                datetime(2026, 4, 4, 8, 0),
                requested_qty=1,
            )

        conflitos = exc_info.value.conflitos
        assert len(conflitos) == 1
        c = conflitos[0]
        assert c["reserva_id"] == reserva_paga.id
        assert c["customer_name"] == "Ana Conflito"
        assert "check_in" in c
        assert "check_out" in c
        assert "status" in c

    def test_multiplas_reservas_mesmo_recurso_detecta_todas(self, db):
        """Deve detectar conflito quando estoque=2 já está totalmente consumido por 2 reservas."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=2)

        for i in range(2):
            r = _make_reserva(
                db,
                check_in=datetime(2026, 5, 1, 8, 0),
                check_out=datetime(2026, 5, 3, 8, 0),
                status=ReservaStatus.PAID,
            )
            _make_link(db, r, resource, quantity=1)
        db.session.commit()

        # Estoque esgotado — terceira reserva deve falhar
        with pytest.raises(OverbookingError):
            ReservaService._verificar_estoque(
                resource,
                datetime(2026, 5, 2, 8, 0),
                datetime(2026, 5, 4, 8, 0),
                requested_qty=1,
            )

    def test_verificar_estoque_reserva_deletada_nao_conta(self, db):
        """Reserva soft-deleted (deleted_at != None) não deve contar como consumo."""
        src = _make_source(db)
        resource = _make_resource(db, src, stock=1)

        reserva_deletada = _make_reserva(
            db,
            check_in=datetime(2026, 5, 1, 8, 0),
            check_out=datetime(2026, 5, 3, 8, 0),
            status=ReservaStatus.PAID,
        )
        reserva_deletada.deleted_at = datetime.now(timezone.utc)  # soft-deleted
        _make_link(db, reserva_deletada, resource, quantity=1)
        db.session.commit()

        # Reserva deletada não deve bloquear o slot
        ReservaService._verificar_estoque(
            resource,
            datetime(2026, 5, 2, 8, 0),
            datetime(2026, 5, 4, 8, 0),
            requested_qty=1,
        )
