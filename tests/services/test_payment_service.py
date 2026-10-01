# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes unitários para MercadoPagoService.

Cobertura:
  - _recalcular_saldo_reserva: lógica de transição de status (PENDING → PARTIALLY_PAID → PAID)
  - processar_pagamento: criação de PaymentTransaction, envio de external_reference, aprovação imediata
  - processar_webhook (caminho legado): Reserva.payment_id sem external_reference UUID
  - processar_webhook (caminho novo): PaymentTransaction via external_reference UUID

Todos os testes usam SQLite in-memory (TestingConfig) e mocam o SDK do Mercado Pago
para evitar chamadas reais à API externa.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from app.models.payment import PaymentTransaction, TransactionStatus
from app.models.reservas import Reserva, ReservaOrigin, ReservaStatus
from app.services.configuracao_service import CHAVE_MP_ACCESS_TOKEN, ConfiguracaoService
from app.services.payment_service import MercadoPagoService, _UUID_RE
from app.services.secret_service import SecretService


# ---------------------------------------------------------------------------
# Helpers de criação de fixtures
# ---------------------------------------------------------------------------


def _make_reserva(
    db,
    *,
    total_amount: float = 100.00,
    status: ReservaStatus = ReservaStatus.PENDING,
    payment_id: str | None = None,
) -> Reserva:
    """Cria uma Reserva mínima diretamente no banco para uso nos testes.

    Args:
        db: Fixture de banco de dados do pytest.
        total_amount: Valor total da reserva em reais.
        status: Status inicial da Reserva.
        payment_id: payment_id legado (Reserva.payment_id), se houver.

    Returns:
        Instância de Reserva persistida no banco.
    """
    reserva = Reserva(
        group_id=str(uuid.uuid4()),
        version=1,
        status=status,
        origin=ReservaOrigin.BALCAO,
        customer_name="Cliente Teste",
        total_amount=Decimal(str(total_amount)),
        check_in_date=date.today(),
        check_out_date=date.today() + timedelta(days=1),
    )
    if payment_id:
        reserva.payment_id = payment_id
    db.session.add(reserva)
    db.session.commit()
    return reserva


def _make_txn(
    db,
    reserva: Reserva,
    *,
    status: TransactionStatus = TransactionStatus.PENDING,
    amount: float = 50.00,
) -> PaymentTransaction:
    """Cria uma PaymentTransaction mínima para uso nos testes.

    Args:
        db: Fixture de banco de dados do pytest.
        reserva: Reserva à qual a transação pertence.
        status: Status inicial da transação.
        amount: Valor da transação em reais.

    Returns:
        Instância de PaymentTransaction persistida no banco.
    """
    txn = PaymentTransaction(
        id=str(uuid.uuid4()),
        reserva_group_id=reserva.group_id,
        reserva_id=reserva.id,
        status=status,
        amount=Decimal(str(amount)),
        payment_method="pix",
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(txn)
    db.session.commit()
    return txn


def _mock_sdk(mp_response: dict) -> MagicMock:
    """Cria mock do SDK do Mercado Pago com response configurado.

    Args:
        mp_response: Dict a ser retornado em get() e create() do payment SDK.

    Returns:
        MagicMock configurado para simular mercadopago.SDK.
    """
    sdk = MagicMock()
    sdk.payment().get.return_value = {"response": mp_response}
    sdk.payment().create.return_value = {"response": mp_response}
    return sdk


def _notificacao_webhook(payment_id: str) -> dict:
    """Monta payload padrão de notificação de webhook do Mercado Pago.

    Args:
        payment_id: ID do pagamento no Mercado Pago.

    Returns:
        Dict no formato { type, data } esperado pelo processador de webhook.
    """
    return {"type": "payment", "data": {"id": payment_id}}


class TestMercadoPagoAccessToken:
    """Testes da resolução retrocompatível do Access Token."""

    def test_access_token_prioriza_secret_service(self, app, db, monkeypatch):
        monkeypatch.setenv("SISTUR_SECRETS_KEY", Fernet.generate_key().decode("utf-8"))
        monkeypatch.setenv("MP_ACCESS_TOKEN", "ENV-token")
        with app.app_context():
            ConfiguracaoService.set(CHAVE_MP_ACCESS_TOKEN, "CONFIG-token", ator_id=1)
            SecretService.set(
                SecretService.MERCADOPAGO_ACCESS_TOKEN,
                "SECRET-token",
                provider="mercadopago",
                ator_id=1,
            )

            assert MercadoPagoService._access_token() == "SECRET-token"

    def test_access_token_usa_config_legada_e_env_como_fallback(self, app, db, monkeypatch):
        monkeypatch.delenv("SISTUR_SECRETS_KEY", raising=False)
        monkeypatch.setenv("MP_ACCESS_TOKEN", "ENV-token")
        with app.app_context():
            ConfiguracaoService.set(CHAVE_MP_ACCESS_TOKEN, "CONFIG-token", ator_id=1)
            assert MercadoPagoService._access_token() == "CONFIG-token"

            ConfiguracaoService.set(CHAVE_MP_ACCESS_TOKEN, None, ator_id=1)
            assert MercadoPagoService._access_token() == "ENV-token"


# ---------------------------------------------------------------------------
# _recalcular_saldo_reserva
# ---------------------------------------------------------------------------


class TestRecalcularSaldoReserva:
    """Testes para MercadoPagoService._recalcular_saldo_reserva."""

    def test_sem_transacoes_aprovadas_mantem_pending(self, db):
        """Sem transações APPROVED, reserva deve permanecer PENDING."""
        reserva = _make_reserva(db, total_amount=100.00)
        _make_txn(db, reserva, status=TransactionStatus.PENDING, amount=100.00)

        MercadoPagoService._recalcular_saldo_reserva(reserva.group_id)

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PENDING

    def test_soma_parcial_define_partially_paid(self, db):
        """Soma APPROVED < total_amount → status deve ser PARTIALLY_PAID."""
        reserva = _make_reserva(db, total_amount=100.00)
        _make_txn(db, reserva, status=TransactionStatus.APPROVED, amount=50.00)

        MercadoPagoService._recalcular_saldo_reserva(reserva.group_id)

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PARTIALLY_PAID

    def test_soma_total_define_paid(self, db):
        """Soma APPROVED >= total_amount → status deve ser PAID."""
        reserva = _make_reserva(db, total_amount=100.00)
        _make_txn(db, reserva, status=TransactionStatus.APPROVED, amount=100.00)

        MercadoPagoService._recalcular_saldo_reserva(reserva.group_id)

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PAID

    def test_tolerancia_centavos_considera_pago(self, db):
        """Soma APPROVED dentro da tolerância de 0.005 do total deve resultar em PAID.

        A margem de arredondamento é 0.005 (ajustada no commit c5283db de
        0.01 para 0.005 para reduzir falso-positivo em pagamentos parciais).
        """
        reserva = _make_reserva(db, total_amount=100.00)
        # 100.00 - 0.004 = 99.996, que é >= 100.00 - 0.005 = 99.995
        _make_txn(db, reserva, status=TransactionStatus.APPROVED, amount=99.996)

        MercadoPagoService._recalcular_saldo_reserva(reserva.group_id)

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PAID

    def test_multiplas_transacoes_parciais_somadas(self, db):
        """Múltiplas transações APPROVED com soma >= total → PAID."""
        reserva = _make_reserva(db, total_amount=100.00)
        _make_txn(db, reserva, status=TransactionStatus.APPROVED, amount=60.00)
        _make_txn(db, reserva, status=TransactionStatus.APPROVED, amount=40.00)

        MercadoPagoService._recalcular_saldo_reserva(reserva.group_id)

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PAID

    def test_transacoes_rejeitadas_nao_contam(self, db):
        """Transações REJECTED não entram na soma — reserva deve permanecer PENDING."""
        reserva = _make_reserva(db, total_amount=100.00)
        _make_txn(db, reserva, status=TransactionStatus.REJECTED, amount=100.00)

        MercadoPagoService._recalcular_saldo_reserva(reserva.group_id)

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PENDING

    def test_transacoes_canceladas_nao_contam(self, db):
        """Transações CANCELLED não entram na soma — reserva deve permanecer PENDING."""
        reserva = _make_reserva(db, total_amount=100.00)
        _make_txn(db, reserva, status=TransactionStatus.CANCELLED, amount=100.00)

        MercadoPagoService._recalcular_saldo_reserva(reserva.group_id)

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PENDING

    def test_group_id_inexistente_nao_levanta_excecao(self, db):
        """group_id sem Reserva ativa deve retornar silenciosamente sem exceção."""
        # Não deve levantar nenhuma exceção
        MercadoPagoService._recalcular_saldo_reserva(str(uuid.uuid4()))

    def test_reserva_partially_paid_evolui_para_paid(self, db):
        """Reserva em PARTIALLY_PAID deve evoluir para PAID quando soma atinge o total."""
        reserva = _make_reserva(
            db, total_amount=100.00, status=ReservaStatus.PARTIALLY_PAID
        )
        # Já há 50 aprovados (representados pelo status PARTIALLY_PAID)
        _make_txn(db, reserva, status=TransactionStatus.APPROVED, amount=50.00)
        _make_txn(db, reserva, status=TransactionStatus.APPROVED, amount=50.00)

        MercadoPagoService._recalcular_saldo_reserva(reserva.group_id)

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PAID


# ---------------------------------------------------------------------------
# processar_pagamento
# ---------------------------------------------------------------------------


class TestProcessarPagamento:
    """Testes para MercadoPagoService.processar_pagamento."""

    def test_reserva_nao_encontrada_levanta_value_error(self, db):
        """Reserva inexistente deve levantar ValueError."""
        with pytest.raises(ValueError, match="não encontrada"):
            MercadoPagoService.processar_pagamento(
                999999,
                {"payment_method_id": "pix", "payer": {}},
            )

    def test_pix_cria_transacao_pending_e_retorna_dados(self, db):
        """Pagamento PIX deve criar PaymentTransaction PENDING com QR Code."""
        mp_response = {
            "id": "mp-111",
            "status": "pending",
            "status_detail": "pending_waiting_transfer",
            "point_of_interaction": {
                "transaction_data": {
                    "qr_code": "00020101...",
                    "qr_code_base64": "base64img",
                    "ticket_url": "https://mercadopago.com/cobro/1",
                }
            },
        }
        reserva = _make_reserva(db, total_amount=100.00)

        with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk(mp_response)):
            result = MercadoPagoService.processar_pagamento(
                reserva.id,
                {"payment_method_id": "pix", "payer": {"email": "t@t.com"}},
            )

        txn = (
            db.session.query(PaymentTransaction)
            .filter_by(reserva_group_id=reserva.group_id)
            .first()
        )
        assert txn is not None
        assert txn.status == TransactionStatus.PENDING
        assert txn.qr_code == "00020101..."
        assert "_transaction_id" in result

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PENDING  # webhook ainda não chegou

    def test_cartao_aprovado_imediatamente_define_paid(self, db):
        """Cartão aprovado pelo MP na hora deve marcar transação APPROVED e Reserva PAID."""
        mp_response = {
            "id": "mp-222",
            "status": "approved",
            "status_detail": "accredited",
        }
        reserva = _make_reserva(db, total_amount=100.00)

        with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk(mp_response)):
            MercadoPagoService.processar_pagamento(
                reserva.id,
                {
                    "payment_method_id": "visa",
                    "token": "tok_abc123",
                    "installments": 1,
                    "payer": {"email": "t@t.com"},
                },
            )

        txn = (
            db.session.query(PaymentTransaction)
            .filter_by(reserva_group_id=reserva.group_id)
            .first()
        )
        assert txn.status == TransactionStatus.APPROVED
        assert txn.approved_at is not None

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PAID

    def test_partial_amount_substitui_total_amount(self, db):
        """partial_amount deve prevalecer sobre reserva.total_amount no valor cobrado."""
        mp_response = {
            "id": "mp-333",
            "status": "pending",
            "status_detail": "pending_waiting_transfer",
        }
        reserva = _make_reserva(db, total_amount=200.00)

        with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk(mp_response)):
            MercadoPagoService.processar_pagamento(
                reserva.id,
                {"payment_method_id": "pix", "payer": {"email": "t@t.com"}},
                partial_amount=75.00,
            )

        txn = (
            db.session.query(PaymentTransaction)
            .filter_by(reserva_group_id=reserva.group_id)
            .first()
        )
        assert float(txn.amount) == 75.00

    def test_external_reference_enviado_como_uuid_da_transacao(self, db):
        """O UUID da PaymentTransaction deve ser enviado como external_reference ao MP."""
        captured: dict = {}

        def fake_create(payload: dict) -> dict:
            captured.update(payload)
            return {"response": {"id": "mp-444", "status": "pending", "status_detail": "x"}}

        reserva = _make_reserva(db, total_amount=100.00)
        sdk = MagicMock()
        sdk.payment().create.side_effect = fake_create

        with patch.object(MercadoPagoService, "_sdk", return_value=sdk):
            MercadoPagoService.processar_pagamento(
                reserva.id,
                {"payment_method_id": "pix", "payer": {}},
            )

        ext_ref = captured.get("external_reference", "")
        assert _UUID_RE.match(ext_ref), "external_reference deve ser UUID válido"

    def test_transaction_id_incluido_no_response(self, db):
        """O campo _transaction_id deve estar presente no response retornado."""
        mp_response = {"id": "mp-555", "status": "pending", "status_detail": "x"}
        reserva = _make_reserva(db, total_amount=100.00)

        with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk(mp_response)):
            result = MercadoPagoService.processar_pagamento(
                reserva.id,
                {"payment_method_id": "pix", "payer": {}},
            )

        assert "_transaction_id" in result
        assert _UUID_RE.match(result["_transaction_id"])

    def test_cartao_sem_token_levanta_value_error(self, db):
        """Pagamento com cartão sem token deve levantar ValueError."""
        reserva = _make_reserva(db, total_amount=100.00)

        # O SDK nunca é chamado pois o token é verificado antes
        with patch.object(MercadoPagoService, "_sdk", return_value=MagicMock()):
            with pytest.raises(ValueError, match="Token do cartão"):
                MercadoPagoService.processar_pagamento(
                    reserva.id,
                    {
                        "payment_method_id": "visa",
                        "installments": 1,
                        "payer": {"email": "t@t.com"},
                        # token ausente
                    },
                )


# ---------------------------------------------------------------------------
# processar_webhook — caminho legado (sem external_reference UUID)
# ---------------------------------------------------------------------------


class TestProcessarWebhookLegado:
    """Testes para o caminho legado do webhook (Reserva.payment_id, sem UUID)."""

    def test_aprovado_marca_reserva_paid(self, db):
        """Webhook approved deve marcar a Reserva como PAID pelo caminho legado."""
        reserva = _make_reserva(db, total_amount=100.00, payment_id="legacy-001")

        mp_response = {
            "id": "legacy-001",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": 100.00,
            "payment_type_id": "bank_transfer",
            "date_approved": "2026-03-20T10:00:00.000Z",
            "external_reference": "",  # ausente → caminho legado
        }

        with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk(mp_response)):
            result = MercadoPagoService.processar_webhook(
                _notificacao_webhook("legacy-001")
            )

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PAID
        assert reserva.payment_data is not None
        assert reserva.payment_data.get("transaction_id") == "legacy-001"
        assert "msg" in result

    def test_cancelado_marca_reserva_canceled(self, db):
        """Webhook cancelled deve marcar a Reserva como CANCELED."""
        reserva = _make_reserva(db, total_amount=100.00, payment_id="legacy-002")

        mp_response = {
            "id": "legacy-002",
            "status": "cancelled",
            "status_detail": "by_collector",
            "external_reference": None,
        }

        with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk(mp_response)):
            MercadoPagoService.processar_webhook(_notificacao_webhook("legacy-002"))

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.CANCELED

    def test_reserva_nao_encontrada_retorna_msg_sem_excecao(self, db):
        """Webhook sem Reserva correspondente deve retornar msg sem lançar exceção."""
        mp_response = {
            "id": "inexistente-999",
            "status": "approved",
            "external_reference": "",
        }

        with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk(mp_response)):
            result = MercadoPagoService.processar_webhook(
                _notificacao_webhook("inexistente-999")
            )

        assert "não encontrada" in result["msg"]

    def test_tipo_diferente_de_payment_ignorado(self, db):
        """Notificação com type != payment deve ser ignorada sem chamar o MP."""
        with patch.object(MercadoPagoService, "_sdk", return_value=MagicMock()) as mock_sdk:
            result = MercadoPagoService.processar_webhook(
                {"type": "merchant_order", "data": {"id": "99"}}
            )

        assert result["msg"] == "Notificação ignorada."
        # _sdk não deve ter sido chamado — a verificação de tipo é anterior à API
        mock_sdk.payment.assert_not_called()

    def test_idempotencia_reserva_ja_paid_nao_muda(self, db):
        """Webhook duplicado para Reserva já PAID não deve alterar o status."""
        reserva = _make_reserva(
            db, total_amount=100.00, status=ReservaStatus.PAID, payment_id="legacy-003"
        )

        mp_response = {
            "id": "legacy-003",
            "status": "approved",
            "external_reference": "",
        }

        with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk(mp_response)):
            MercadoPagoService.processar_webhook(_notificacao_webhook("legacy-003"))

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PAID  # não mudou


# ---------------------------------------------------------------------------
# processar_webhook — caminho novo (external_reference = UUID)
# ---------------------------------------------------------------------------


class TestProcessarWebhookNovo:
    """Testes para o caminho novo do webhook (external_reference = UUID da transação)."""

    def test_aprovado_atualiza_transacao_e_reserva_para_paid(self, db):
        """Webhook approved via UUID deve aprovar a transação e propagar PAID para a Reserva."""
        reserva = _make_reserva(db, total_amount=100.00)
        txn = _make_txn(db, reserva, status=TransactionStatus.PENDING, amount=100.00)

        mp_response = {
            "id": "mp-novo-001",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": 100.00,
            "payment_type_id": "bank_transfer",
            "date_approved": "2026-03-20T10:00:00.000Z",
            "external_reference": txn.id,  # UUID da PaymentTransaction
        }

        with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk(mp_response)):
            result = MercadoPagoService.processar_webhook(_notificacao_webhook("mp-novo-001"))

        db.session.refresh(txn)
        db.session.refresh(reserva)

        assert txn.status == TransactionStatus.APPROVED
        assert txn.approved_at is not None
        assert txn.mp_payment_id == "mp-novo-001"
        assert reserva.status == ReservaStatus.PAID
        assert "aprovada" in result["msg"]

    def test_aprovacao_parcial_define_partially_paid(self, db):
        """Aprovação de valor parcial deve deixar a Reserva em PARTIALLY_PAID."""
        reserva = _make_reserva(db, total_amount=100.00)
        txn = _make_txn(db, reserva, status=TransactionStatus.PENDING, amount=50.00)

        mp_response = {
            "id": "mp-novo-002",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": 50.00,
            "date_approved": "2026-03-20T10:00:00.000Z",
            "external_reference": txn.id,
        }

        with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk(mp_response)):
            MercadoPagoService.processar_webhook(_notificacao_webhook("mp-novo-002"))

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PARTIALLY_PAID

    def test_segunda_aprovacao_parcial_define_paid(self, db):
        """Segunda aprovação que completa o total deve evoluir de PARTIALLY_PAID para PAID."""
        reserva = _make_reserva(
            db, total_amount=100.00, status=ReservaStatus.PARTIALLY_PAID
        )
        # Primeira transação já aprovada (representa o estado PARTIALLY_PAID)
        _make_txn(db, reserva, status=TransactionStatus.APPROVED, amount=50.00)
        # Segunda transação ainda pendente
        txn2 = _make_txn(db, reserva, status=TransactionStatus.PENDING, amount=50.00)

        mp_response = {
            "id": "mp-novo-003",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": 50.00,
            "date_approved": "2026-03-20T10:00:00.000Z",
            "external_reference": txn2.id,
        }

        with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk(mp_response)):
            MercadoPagoService.processar_webhook(_notificacao_webhook("mp-novo-003"))

        db.session.refresh(txn2)
        db.session.refresh(reserva)
        assert txn2.status == TransactionStatus.APPROVED
        assert reserva.status == ReservaStatus.PAID

    def test_rejeitado_atualiza_transacao_para_rejected(self, db):
        """Webhook rejected deve marcar PaymentTransaction como REJECTED, Reserva permanece PENDING."""
        reserva = _make_reserva(db, total_amount=100.00)
        txn = _make_txn(db, reserva, status=TransactionStatus.PENDING, amount=100.00)

        mp_response = {
            "id": "mp-novo-004",
            "status": "rejected",
            "status_detail": "cc_rejected_bad_filled_security_code",
            "external_reference": txn.id,
        }

        with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk(mp_response)):
            MercadoPagoService.processar_webhook(_notificacao_webhook("mp-novo-004"))

        db.session.refresh(txn)
        db.session.refresh(reserva)
        assert txn.status == TransactionStatus.REJECTED
        assert reserva.status == ReservaStatus.PENDING  # não houve pagamento aprovado

    def test_cancelado_atualiza_transacao_para_cancelled(self, db):
        """Webhook cancelled deve marcar PaymentTransaction como CANCELLED."""
        reserva = _make_reserva(db, total_amount=100.00)
        txn = _make_txn(db, reserva, status=TransactionStatus.PENDING, amount=100.00)

        mp_response = {
            "id": "mp-novo-005",
            "status": "cancelled",
            "status_detail": "expired",
            "external_reference": txn.id,
        }

        with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk(mp_response)):
            MercadoPagoService.processar_webhook(_notificacao_webhook("mp-novo-005"))

        db.session.refresh(txn)
        assert txn.status == TransactionStatus.CANCELLED

    def test_transacao_nao_encontrada_retorna_msg(self, db):
        """external_reference UUID sem PaymentTransaction correspondente retorna msg sem exceção."""
        fake_uuid = str(uuid.uuid4())

        mp_response = {
            "id": "mp-novo-404",
            "status": "approved",
            "external_reference": fake_uuid,
        }

        with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk(mp_response)):
            result = MercadoPagoService.processar_webhook(_notificacao_webhook("mp-novo-404"))

        assert "não encontrada" in result["msg"]

    def test_idempotencia_transacao_ja_aprovada(self, db):
        """Webhook duplicado para transação já APPROVED não deve alterar estado nem duplicar commit."""
        reserva = _make_reserva(db, total_amount=100.00, status=ReservaStatus.PAID)
        txn = _make_txn(db, reserva, status=TransactionStatus.APPROVED, amount=100.00)
        txn.approved_at = datetime.now(timezone.utc)
        db.session.commit()

        mp_response = {
            "id": "mp-novo-dup",
            "status": "approved",
            "status_detail": "accredited",
            "transaction_amount": 100.00,
            "external_reference": txn.id,
        }

        with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk(mp_response)):
            MercadoPagoService.processar_webhook(_notificacao_webhook("mp-novo-dup"))

        db.session.refresh(txn)
        db.session.refresh(reserva)
        # Estado não deve ter mudado
        assert txn.status == TransactionStatus.APPROVED
        assert reserva.status == ReservaStatus.PAID


# ---------------------------------------------------------------------------
# _UUID_RE — expressão regular de detecção de external_reference
# ---------------------------------------------------------------------------


class TestUuidRegex:
    """Testes para a expressão regular de detecção de UUID no external_reference."""

    def test_uuid_valido_reconhecido(self):
        """UUID v4 no formato padrão deve ser reconhecido pelo regex."""
        assert _UUID_RE.match("550e8400-e29b-41d4-a716-446655440000")

    def test_uuid_case_insensitive(self):
        """UUID com letras maiúsculas deve ser reconhecido (flag IGNORECASE)."""
        assert _UUID_RE.match("550E8400-E29B-41D4-A716-446655440000")

    def test_string_vazia_nao_reconhecida(self):
        """String vazia não deve ser reconhecida como UUID."""
        assert not _UUID_RE.match("")

    def test_payment_id_numerico_nao_reconhecido(self):
        """ID numérico do Mercado Pago (ex: '123456789') não deve ser reconhecido como UUID."""
        assert not _UUID_RE.match("123456789")

    def test_string_arbitraria_nao_reconhecida(self):
        """String arbitrária não deve ser reconhecida como UUID."""
        assert not _UUID_RE.match("legacy-payment-ref")


# ---------------------------------------------------------------------------
# gerar_link_pagamento_cartao
# ---------------------------------------------------------------------------


def _mock_sdk_preference(preference_response: dict) -> MagicMock:
    """Cria mock do SDK do Mercado Pago com preference configurada.

    Args:
        preference_response: Dict a ser retornado em create() do preference SDK.

    Returns:
        MagicMock configurado para simular mercadopago.SDK com preference.
    """
    sdk = MagicMock()
    sdk.preference().create.return_value = {"response": preference_response, "status": 201}
    return sdk


class TestGerarLinkPagamentoCartao:
    """Testes para MercadoPagoService.gerar_link_pagamento_cartao."""

    def test_happy_path_cria_txn_e_retorna_url(self, app, db):
        """Happy path: cria PaymentTransaction(PENDING) e retorna checkout_url."""
        from app.models.payment import PaymentTransaction, TransactionStatus

        with app.app_context():
            reserva = _make_reserva(db, total_amount=200.00)

            preference_response = {
                "id": "pref-abc-123",
                "init_point": "https://www.mercadopago.com.br/checkout/v1/redirect?pref_id=pref-abc-123",
                "sandbox_init_point": "https://sandbox.mercadopago.com.br/checkout/v1/redirect?pref_id=pref-abc-123",
                "date_created": "2026-05-18T10:00:00.000-04:00",
            }

            with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk_preference(preference_response)):
                resultado = MercadoPagoService.gerar_link_pagamento_cartao(
                    reserva.id, payer_email="teste@email.com"
                )

            assert resultado["checkout_url"] == preference_response["init_point"]
            assert resultado["preference_id"] == "pref-abc-123"
            assert resultado["amount"] == 200.00
            assert "transaction_id" in resultado
            assert "expires_at" in resultado

            txn = db.session.query(PaymentTransaction).filter_by(
                reserva_group_id=reserva.group_id,
                payment_method="credit_card",
            ).first()
            assert txn is not None
            assert txn.status == TransactionStatus.PENDING
            assert txn.ticket_url == preference_response["init_point"]
            assert txn.mp_response_snapshot["preference_id"] == "pref-abc-123"

    def test_token_sandbox_do_cofre_usa_sandbox_init_point(self, app, db, monkeypatch):
        """Token TEST- salvo no cofre deve selecionar sandbox_init_point."""
        monkeypatch.setenv("SISTUR_SECRETS_KEY", Fernet.generate_key().decode("utf-8"))
        monkeypatch.delenv("MP_ACCESS_TOKEN", raising=False)
        with app.app_context():
            SecretService.set(
                SecretService.MERCADOPAGO_ACCESS_TOKEN,
                "TEST-token-1234",
                provider="mercadopago",
                ator_id=1,
            )
            reserva = _make_reserva(db, total_amount=200.00)

            preference_response = {
                "id": "pref-sandbox-123",
                "init_point": "https://www.mercadopago.com.br/checkout/v1/redirect?pref_id=pref-sandbox-123",
                "sandbox_init_point": "https://sandbox.mercadopago.com.br/checkout/v1/redirect?pref_id=pref-sandbox-123",
                "date_created": "2026-05-18T10:00:00.000-04:00",
            }

            with patch.object(MercadoPagoService, "_sdk", return_value=_mock_sdk_preference(preference_response)):
                resultado = MercadoPagoService.gerar_link_pagamento_cartao(
                    reserva.id, payer_email="teste@email.com"
                )

            assert resultado["checkout_url"] == preference_response["sandbox_init_point"]
            txn = db.session.query(PaymentTransaction).filter_by(
                reserva_group_id=reserva.group_id,
                payment_method="credit_card",
            ).first()
            assert txn.mp_response_snapshot["is_sandbox"] is True

    def test_external_reference_eh_uuid_da_txn(self, app, db):
        """A preference deve ser criada com external_reference igual ao UUID da transação."""
        with app.app_context():
            reserva = _make_reserva(db, total_amount=150.00)

            preference_response = {
                "id": "pref-ext-ref",
                "init_point": "https://mp.test/checkout",
                "sandbox_init_point": "",
                "date_created": "2026-05-18T10:00:00.000-04:00",
            }

            sdk_mock = _mock_sdk_preference(preference_response)

            with patch.object(MercadoPagoService, "_sdk", return_value=sdk_mock):
                resultado = MercadoPagoService.gerar_link_pagamento_cartao(reserva.id)

            # Verifica que o external_reference enviado ao MP é o UUID da txn
            chamada = sdk_mock.preference().create.call_args
            payload_enviado = chamada[0][0]
            assert _UUID_RE.match(payload_enviado["external_reference"])
            assert payload_enviado["external_reference"] == resultado["transaction_id"]

    def test_pix_excluido_do_link_cartao(self, app, db):
        """A preference deve excluir bank_transfer (PIX) e boleto dos métodos permitidos."""
        with app.app_context():
            reserva = _make_reserva(db, total_amount=100.00)

            preference_response = {
                "id": "pref-excl",
                "init_point": "https://mp.test/checkout",
                "sandbox_init_point": "",
                "date_created": "2026-05-18T10:00:00.000-04:00",
            }

            sdk_mock = _mock_sdk_preference(preference_response)

            with patch.object(MercadoPagoService, "_sdk", return_value=sdk_mock):
                MercadoPagoService.gerar_link_pagamento_cartao(reserva.id)

            payload_enviado = sdk_mock.preference().create.call_args[0][0]
            tipos_excluidos = [
                t["id"] for t in payload_enviado["payment_methods"]["excluded_payment_types"]
            ]
            assert "bank_transfer" in tipos_excluidos
            assert "ticket" in tipos_excluidos

    def test_reserva_sem_total_levanta_valor_error(self, app, db):
        """Reserva sem total_amount deve levantar ValueError."""
        with app.app_context():
            reserva = _make_reserva(db, total_amount=0.00)

            with pytest.raises(ValueError, match="sem valor total"):
                MercadoPagoService.gerar_link_pagamento_cartao(reserva.id)

    def test_reserva_nao_encontrada_levanta_valor_error(self, app, db):
        """ID de reserva inexistente deve levantar ValueError."""
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrada"):
                MercadoPagoService.gerar_link_pagamento_cartao(999999)

    def test_mp_sem_preference_id_levanta_runtime_error(self, app, db):
        """Resposta do MP sem preference ID deve levantar RuntimeError e não persistir txn."""
        from app.models.payment import PaymentTransaction

        with app.app_context():
            reserva = _make_reserva(db, total_amount=100.00)

            sdk_mock = MagicMock()
            sdk_mock.preference().create.return_value = {"response": {}, "status": 500}

            with patch.object(MercadoPagoService, "_sdk", return_value=sdk_mock):
                with pytest.raises(RuntimeError, match="não retornou ID"):
                    MercadoPagoService.gerar_link_pagamento_cartao(reserva.id)

            # Nenhuma transação deve ter sido persistida
            count = db.session.query(PaymentTransaction).filter_by(
                reserva_group_id=reserva.group_id,
                payment_method="credit_card",
            ).count()
            assert count == 0


# ---------------------------------------------------------------------------
# notification_url — para qual ambiente o Mercado Pago avisa
# ---------------------------------------------------------------------------


class TestNotificationUrl:
    """O webhook precisa voltar para o ambiente que criou a cobrança.

    Regressão de uma falha real em 2026-08-18: sem ``notification_url`` no
    payload, o Mercado Pago usa o webhook cadastrado na conta, que é único e
    aponta para produção. Uma cobrança criada no staging foi paga de verdade, a
    notificação chegou em produção, e lá a transação não existia — registrada
    como ``WEBHOOK_ORPHAN_TRANSACTION`` e descartada. A reserva ficou PENDING
    até alguém reprocessar na mão, enquanto o cliente olhava um QR já pago.
    """

    def _capturar_payload(self, db, monkeypatch, url: str | None) -> dict:
        """Roda um pagamento PIX e devolve o payload enviado ao Mercado Pago."""
        if url is None:
            monkeypatch.delenv("MP_NOTIFICATION_URL", raising=False)
        else:
            monkeypatch.setenv("MP_NOTIFICATION_URL", url)

        mp_response = {
            "id": "mp-notif",
            "status": "pending",
            "status_detail": "pending_waiting_transfer",
            "point_of_interaction": {"transaction_data": {"qr_code": "0002"}},
        }
        reserva = _make_reserva(db, total_amount=50.00)
        sdk = _mock_sdk(mp_response)

        with patch.object(MercadoPagoService, "_sdk", return_value=sdk):
            MercadoPagoService.processar_pagamento(
                reserva.id,
                {"payment_method_id": "pix", "payer": {"email": "t@t.com"}},
            )

        return sdk.payment().create.call_args[0][0]

    def test_envia_notification_url_quando_configurada(self, db, monkeypatch):
        payload = self._capturar_payload(
            db, monkeypatch, "https://test-admin.exemplo/api/v1/payments/webhook"
        )
        assert payload["notification_url"] == "https://test-admin.exemplo/api/v1/payments/webhook"

    def test_sem_a_variavel_o_payload_nao_muda(self, db, monkeypatch):
        """Ausente, vale o webhook da conta — o comportamento de antes.

        Importa porque produção ainda não define a variável, e definir uma URL
        vazia lá redirecionaria as notificações para lugar nenhum.
        """
        payload = self._capturar_payload(db, monkeypatch, None)
        assert "notification_url" not in payload

    def test_variavel_em_branco_e_ignorada(self, db, monkeypatch):
        payload = self._capturar_payload(db, monkeypatch, "   ")
        assert "notification_url" not in payload

    def test_external_reference_continua_sendo_a_transacao(self, db, monkeypatch):
        """O webhook casa a notificação por este campo; a URL não o substitui."""
        payload = self._capturar_payload(
            db, monkeypatch, "https://test-admin.exemplo/api/v1/payments/webhook"
        )
        assert payload["external_reference"]
        txn = db.session.query(PaymentTransaction).filter_by(
            id=payload["external_reference"]
        ).first()
        assert txn is not None
