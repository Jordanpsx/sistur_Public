# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Cobertura de logging das falhas do fluxo de pagamento.

Cada teste aqui representa um tipo de falha que o cliente percebe como
"não consegui pagar" e que precisa aparecer em /admin/logs. Antes desta
suíte, todos esses caminhos falhavam em silêncio.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.integration_log import IntegrationLog
from app.services.integration_log_service import IntegrationLogService


INGEST_URL = "/api/internal/reservas/ingest"
WEBHOOK_URL = "/api/v1/payments/webhook"
CONFIG_URL = "/api/v1/payments/config"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth(app) -> dict:
    """Headers com o Bearer token interno usado pelo WordPress."""
    return {
        "Authorization": f"Bearer {app.config['QR_SERVICE_TOKEN']}",
        "Content-Type": "application/json",
    }


def _logs(db, error_code: str | None = None) -> list[IntegrationLog]:
    """Retorna os logs gravados, opcionalmente filtrados por error_code."""
    q = db.session.query(IntegrationLog)
    if error_code:
        q = q.filter(IntegrationLog.error_code == error_code)
    return q.all()


def _payload_ingest(**overrides) -> dict:
    """Payload mínimo válido de criação de reserva, com overrides opcionais."""
    base = {
        "source_name": "Cachoeira",
        "category_name": "Day Use",
        "customer_name": "Maria Teste",
        "check_in_date": "2030-01-10",
        "items": [{"item_id": 1, "quantity": 2, "price_override": 100.0}],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# IntegrationLogService — contrato de resiliência
# ---------------------------------------------------------------------------

class TestIntegrationLogService:

    def test_registrar_persiste_e_classifica(self, app, db):
        log_id = IntegrationLogService.registrar(
            error_source="SISTUR_SYNC",
            error_code="WEBHOOK_UNEXPECTED_ERROR",
            error_message="boom",
            reserva_id="42",
        )
        log = db.session.get(IntegrationLog, log_id)
        assert log.reserva_id == 42
        assert log.error_category == "system"

    def test_source_desconhecido_nao_descarta_o_log(self, app, db):
        """Origem fora do conjunto vira SISTUR_SYNC — perder o log é pior."""
        log_id = IntegrationLogService.registrar(
            error_source="ORIGEM_NOVA",
            error_code="X",
            error_message="mensagem original",
        )
        log = db.session.get(IntegrationLog, log_id)
        assert log.error_source == "SISTUR_SYNC"
        assert "ORIGEM_NOVA" in log.error_message
        assert "mensagem original" in log.error_message

    def test_campos_longos_sao_truncados(self, app, db):
        log_id = IntegrationLogService.registrar(
            error_source="WP_INTERNAL",
            error_code="C" * 300,
            error_message="m" * 5000,
            customer_name="n" * 500,
        )
        log = db.session.get(IntegrationLog, log_id)
        assert len(log.error_message) == 2000
        assert len(log.error_code) == 100
        assert len(log.customer_name) == 255

    def test_nunca_propaga_excecao(self, app, db, monkeypatch):
        """Falha ao persistir não pode mascarar o erro original do chamador."""
        from app.extensions import db as _db

        def _explode():
            raise RuntimeError("db indisponível")

        monkeypatch.setattr(_db.session, "commit", _explode)
        assert IntegrationLogService.registrar(
            error_source="WP_INTERNAL", error_code="X", error_message="y"
        ) is None

    def test_raw_payload_invalido_ignorado(self, app, db):
        log_id = IntegrationLogService.registrar(
            error_source="WP_INTERNAL", error_code="X",
            error_message="y", raw_payload="não é dict",
        )
        assert db.session.get(IntegrationLog, log_id).raw_payload is None


# ---------------------------------------------------------------------------
# Classificação semântica
# ---------------------------------------------------------------------------

class TestClassify:

    @pytest.mark.parametrize("code", [
        "INGEST_VALIDATION_ERROR",
        "NONCE_INVALID",
        "INGEST_CLIENT_BANNED",
        "PAYMENT_ON_EXPIRED_RESERVA",
        "PIX_INVALID_INPUT",
    ])
    def test_contexto_do_cliente_e_invalid_data(self, code):
        assert IntegrationLog.classify("SISTUR_SYNC", code) == "invalid_data"

    @pytest.mark.parametrize("code", [
        "WEBHOOK_ORPHAN_TRANSACTION",
        "INGEST_UNEXPECTED_ERROR",
        "WEBHOOK_CONFIG_ERROR",
    ])
    def test_falhas_de_infra_sao_system(self, code):
        assert IntegrationLog.classify("SISTUR_SYNC", code) == "system"

    def test_recusa_de_cartao_continua_client_bank(self):
        assert IntegrationLog.classify("SISTUR_QR", "cc_rejected_call_for_authorize") == "client_bank"

    def test_falha_de_carregamento_do_brick_e_system(self):
        """Brick que não carrega é problema nosso, não recusa do banco."""
        assert IntegrationLog.classify("MP_BRICKS", "BRICK_CREATE_FAILED") == "system"
        assert IntegrationLog.classify("MP_BRICKS", "MP_PUBLIC_KEY_MISSING") == "system"


# ---------------------------------------------------------------------------
# Criação de reserva (ingest) — falhas que impedem o cliente de chegar ao pagamento
# ---------------------------------------------------------------------------

class TestIngestLogging:

    def test_campo_obrigatorio_ausente_gera_log(self, app, client, db):
        payload = _payload_ingest()
        payload.pop("category_name")
        resp = client.post(INGEST_URL, json=payload, headers=_auth(app))
        assert resp.status_code == 400

        logs = _logs(db, "INGEST_VALIDATION_ERROR")
        assert len(logs) == 1
        assert logs[0].error_source == "SISTUR_SYNC"
        assert logs[0].customer_name == "Maria Teste"
        assert logs[0].error_category == "invalid_data"

    def test_payload_sem_itens_gera_log(self, app, client, db):
        """Slug de catálogo não resolvido no WP chega aqui como items vazio."""
        payload = _payload_ingest(items=[])
        resp = client.post(INGEST_URL, json=payload, headers=_auth(app))
        assert resp.status_code == 400

        logs = _logs(db, "INGEST_EMPTY_ITEMS")
        assert len(logs) == 1

    def test_data_invalida_gera_log(self, app, client, db):
        payload = _payload_ingest(check_in_date="10/01/2030")
        resp = client.post(INGEST_URL, json=payload, headers=_auth(app))
        assert resp.status_code == 400
        assert len(_logs(db, "INGEST_VALIDATION_ERROR")) == 1

    def test_recusa_de_regra_de_negocio_gera_log(self, app, client, db):
        """Divergência de preço, item inexistente e estoque esgotado caem aqui."""
        resp = client.post(INGEST_URL, json=_payload_ingest(), headers=_auth(app))
        assert resp.status_code == 400

        logs = _logs(db, "INGEST_REJECTED")
        assert len(logs) == 1
        assert logs[0].error_source == "SISTUR_SYNC"
        assert logs[0].raw_payload["items"]

    def test_erro_inesperado_gera_log_e_json_500(self, app, client, db, monkeypatch):
        """Sem o handler, o WordPress receberia HTML de erro e nada seria logado."""
        from app.services.reserva_service import ReservaService

        def _explode(*_a, **_kw):
            raise TypeError("bug interno")

        monkeypatch.setattr(ReservaService, "criar", _explode)

        resp = client.post(INGEST_URL, json=_payload_ingest(), headers=_auth(app))
        assert resp.status_code == 500
        assert resp.get_json()["erro"]

        logs = _logs(db, "INGEST_UNEXPECTED_ERROR")
        assert len(logs) == 1
        assert "bug interno" in logs[0].error_message

    def test_cliente_bloqueado_gera_log(self, app, client, db, monkeypatch):
        from app.services.blacklist_service import ClientBlacklistedError
        from app.services.reserva_service import ReservaService

        def _banido(*_a, **_kw):
            raise ClientBlacklistedError("cliente bloqueado")

        monkeypatch.setattr(ReservaService, "criar", _banido)

        resp = client.post(INGEST_URL, json=_payload_ingest(), headers=_auth(app))
        assert resp.status_code == 403

        logs = _logs(db, "INGEST_CLIENT_BANNED")
        assert len(logs) == 1
        assert logs[0].error_category == "invalid_data"


# ---------------------------------------------------------------------------
# Webhook do Mercado Pago — cliente pagou e a reserva não confirma
# ---------------------------------------------------------------------------

class TestWebhookLogging:

    def test_erro_inesperado_no_webhook_gera_log(self, app, client, db, monkeypatch):
        from app.services.payment_service import MercadoPagoService

        def _explode(_notification):
            raise RuntimeError("MP fora do ar")

        monkeypatch.setattr(MercadoPagoService, "processar_webhook", _explode)

        resp = client.post(WEBHOOK_URL, json={"type": "payment", "data": {"id": "999"}})
        assert resp.status_code == 500

        logs = _logs(db, "WEBHOOK_CONFIG_ERROR")
        assert len(logs) == 1
        assert logs[0].raw_payload["mp_payment_id"] == "999"

    def test_excecao_generica_no_webhook_gera_log(self, app, client, db, monkeypatch):
        from app.services.payment_service import MercadoPagoService

        def _explode(_notification):
            raise KeyError("campo inesperado")

        monkeypatch.setattr(MercadoPagoService, "processar_webhook", _explode)

        resp = client.post(WEBHOOK_URL, json={"type": "payment", "data": {"id": "777"}})
        assert resp.status_code == 500

        logs = _logs(db, "WEBHOOK_UNEXPECTED_ERROR")
        assert len(logs) == 1
        assert "777" in logs[0].error_message

    def test_pagamento_aprovado_sem_transacao_gera_log(self, app, db):
        """Dinheiro recebido no MP sem transação no Sistur — conciliação manual."""
        from app.models.payment import PaymentTransaction, TransactionStatus
        from app.services.payment_service import MercadoPagoService

        resultado = MercadoPagoService._processar_webhook_novo(
            payment_id="123456",
            mp_payment={"status": "approved", "transaction_amount": 250.0,
                        "payer": {"email": "cliente@ex.com"}},
            mp_status="approved",
            transaction_id="00000000-0000-4000-8000-000000000000",
            db=db,
            PaymentTransaction=PaymentTransaction,
            TransactionStatus=TransactionStatus,
        )
        assert "não encontrada" in resultado["msg"]

        logs = _logs(db, "WEBHOOK_ORPHAN_TRANSACTION")
        assert len(logs) == 1
        assert logs[0].raw_payload["transaction_amount"] == 250.0

    def test_transacao_ausente_sem_aprovacao_nao_gera_ruido(self, app, db):
        """Notificação de pagamento pendente órfão não é problema — não loga."""
        from app.models.payment import PaymentTransaction, TransactionStatus
        from app.services.payment_service import MercadoPagoService

        MercadoPagoService._processar_webhook_novo(
            payment_id="123456",
            mp_payment={"status": "pending"},
            mp_status="pending",
            transaction_id="00000000-0000-4000-8000-000000000000",
            db=db,
            PaymentTransaction=PaymentTransaction,
            TransactionStatus=TransactionStatus,
        )
        assert _logs(db, "WEBHOOK_ORPHAN_TRANSACTION") == []

    def test_pagamento_aprovado_sem_reserva_legado_gera_log(self, app, db):
        from app.models.reservas import Reserva, ReservaStatus
        from app.services.payment_service import MercadoPagoService

        MercadoPagoService._processar_webhook_legado(
            payment_id="654321",
            mp_payment={"status": "approved", "transaction_amount": 90.0},
            mp_status="approved",
            db=db,
            Reserva=Reserva,
            ReservaStatus=ReservaStatus,
        )
        assert len(_logs(db, "WEBHOOK_ORPHAN_PAYMENT")) == 1


# ---------------------------------------------------------------------------
# Configuração de pagamento ausente — checkout morto para todos os clientes
# ---------------------------------------------------------------------------

class TestConfigLogging:

    def test_public_key_ausente_gera_log(self, app, client, db, monkeypatch):
        import app.blueprints.pagamentos.routes as rotas

        monkeypatch.setattr(rotas, "_ultimo_log_public_key", 0.0, raising=False)

        resp = client.get(CONFIG_URL)
        assert resp.status_code == 200
        assert resp.get_json()["enabled"] is False

        logs = _logs(db, "MP_PUBLIC_KEY_MISSING")
        assert len(logs) == 1
        assert logs[0].error_category == "system"

    def test_log_de_public_key_e_throttled(self, app, client, db, monkeypatch):
        """O WP consulta /config a cada 5 min — sem throttle o painel inunda."""
        import app.blueprints.pagamentos.routes as rotas

        monkeypatch.setattr(rotas, "_ultimo_log_public_key", 0.0, raising=False)

        for _ in range(5):
            client.get(CONFIG_URL)

        assert len(_logs(db, "MP_PUBLIC_KEY_MISSING")) == 1


# ---------------------------------------------------------------------------
# Pagamento sobre reserva com soft lock vencido
# ---------------------------------------------------------------------------

class TestSoftLockExpirado:

    def test_pagamento_em_reserva_expirada_gera_log(self, app, db, monkeypatch):
        from app.models.reservas import Reserva, ReservaOrigin, ReservaStatus
        from app.services.payment_service import MercadoPagoService

        reserva = Reserva(
            group_id="grupo-expirado",
            version=1,
            customer_name="João Atrasado",
            origin=ReservaOrigin.WEB,
            status=ReservaStatus.PENDING,
            check_in_date=datetime(2030, 1, 10),
            check_out_date=datetime(2030, 1, 11),
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=30),
            total_amount=150.0,
        )
        db.session.add(reserva)
        db.session.commit()

        # Interrompe antes da chamada ao MP — o log já deve ter sido gravado.
        monkeypatch.setattr(
            MercadoPagoService, "_sdk",
            classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("sem token"))),
        )

        with pytest.raises(RuntimeError):
            MercadoPagoService.processar_pagamento(
                reserva.id,
                {"payment_method_id": "pix", "payer": {"email": "a@b.com"}},
            )

        logs = _logs(db, "PAYMENT_ON_EXPIRED_RESERVA")
        assert len(logs) == 1
        assert logs[0].reserva_id == reserva.id
        assert logs[0].customer_name == "João Atrasado"

    def test_reserva_dentro_do_prazo_nao_gera_log(self, app, db, monkeypatch):
        from app.models.reservas import Reserva, ReservaOrigin, ReservaStatus
        from app.services.payment_service import MercadoPagoService

        reserva = Reserva(
            group_id="grupo-valido",
            version=1,
            customer_name="Ana Pontual",
            origin=ReservaOrigin.WEB,
            status=ReservaStatus.PENDING,
            check_in_date=datetime(2030, 1, 10),
            check_out_date=datetime(2030, 1, 11),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            total_amount=150.0,
        )
        db.session.add(reserva)
        db.session.commit()

        monkeypatch.setattr(
            MercadoPagoService, "_sdk",
            classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("sem token"))),
        )

        with pytest.raises(RuntimeError):
            MercadoPagoService.processar_pagamento(
                reserva.id,
                {"payment_method_id": "pix", "payer": {"email": "a@b.com"}},
            )

        assert _logs(db, "PAYMENT_ON_EXPIRED_RESERVA") == []
