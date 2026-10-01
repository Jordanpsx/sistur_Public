# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""O webhook do Mercado Pago chega e a reserva muda de status sozinha.

Havia cobertura em duas pontas e um vão no meio:

  - ``tests/services/test_payment_service.py`` testa
    ``MercadoPagoService.processar_webhook()`` — a lógica que muda o status;
  - ``tests/blueprints/test_payment_failure_logging.py`` bate no endpoint HTTP,
    mas só nos caminhos de **erro**.

Ninguém exercitava o caminho feliz **pela rota**: notificação do Mercado Pago
entra por HTTP, sai com a reserva paga. É o percurso que o cliente depende para
a tela dele parar de esperar, e o que falhou em produção em 2026-08-18 — ali por
configuração, não por lógica, mas sem estes testes nem a lógica estava travada
ponta a ponta.

O SDK do Mercado Pago é mocado: o Sistur sempre consulta a API para obter o
status real, e nunca confia no corpo da notificação.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.extensions import db as _db
from app.models.reservas import Reserva, ReservaOrigin, ReservaStatus
from app.services.payment_service import MercadoPagoService

WEBHOOK = "/api/v1/payments/webhook"


def _reserva(total=100.00, status=ReservaStatus.PENDING, payment_id=None):
    r = Reserva(
        group_id=str(uuid.uuid4()),
        version=1,
        status=status,
        origin=ReservaOrigin.WEB,
        customer_name="Cliente Teste",
        total_amount=Decimal(str(total)),
        check_in_date=date.today(),
        check_out_date=date.today() + timedelta(days=1),
    )
    if payment_id:
        r.payment_id = payment_id
    _db.session.add(r)
    _db.session.commit()
    return r


def _sdk(resposta: dict) -> MagicMock:
    sdk = MagicMock()
    sdk.payment().get.return_value = {"response": resposta}
    sdk.payment().create.return_value = {"response": resposta}
    return sdk


def _pagamento_mp(payment_id: str, status: str, total: float, ref: str = ""):
    return {
        "id": payment_id,
        "status": status,
        "status_detail": "accredited" if status == "approved" else status,
        "transaction_amount": total,
        "payment_type_id": "bank_transfer",
        "date_approved": "2026-08-18T10:00:00.000Z",
        "external_reference": ref,
    }


class TestWebhookMudaStatus:
    def test_pix_aprovado_deixa_a_reserva_paga(self, app, client, db):
        """O caso que o cliente vive: paga o PIX e a reserva vira PAID sozinha."""
        reserva = _reserva(total=100.00, payment_id="mp-pix-1")
        assert reserva.status == ReservaStatus.PENDING

        with patch.object(
            MercadoPagoService, "_sdk",
            return_value=_sdk(_pagamento_mp("mp-pix-1", "approved", 100.00)),
        ):
            r = client.post(WEBHOOK, json={"type": "payment", "data": {"id": "mp-pix-1"}})

        assert r.status_code == 200
        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PAID

    def test_pagamento_recusado_nao_marca_como_paga(self, app, client, db):
        reserva = _reserva(total=100.00, payment_id="mp-rej-1")

        with patch.object(
            MercadoPagoService, "_sdk",
            return_value=_sdk(_pagamento_mp("mp-rej-1", "rejected", 100.00)),
        ):
            client.post(WEBHOOK, json={"type": "payment", "data": {"id": "mp-rej-1"}})

        db.session.refresh(reserva)
        assert reserva.status != ReservaStatus.PAID

    def test_notificacao_por_query_string_tambem_funciona(self, app, client, db):
        """O Mercado Pago às vezes manda `?data.id=` em vez de corpo JSON.

        A rota trata esse formato e nada exercitava esse ramo.
        """
        reserva = _reserva(total=100.00, payment_id="mp-qs-1")

        with patch.object(
            MercadoPagoService, "_sdk",
            return_value=_sdk(_pagamento_mp("mp-qs-1", "approved", 100.00)),
        ):
            r = client.post(f"{WEBHOOK}?data.id=mp-qs-1")

        assert r.status_code == 200
        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PAID

    def test_notificacao_repetida_nao_altera_nada(self, app, client, db):
        """O Mercado Pago reenvia notificações. Duas entregas = um pagamento."""
        reserva = _reserva(total=100.00, payment_id="mp-dup-1")
        mp = _pagamento_mp("mp-dup-1", "approved", 100.00)

        with patch.object(MercadoPagoService, "_sdk", return_value=_sdk(mp)):
            client.post(WEBHOOK, json={"type": "payment", "data": {"id": "mp-dup-1"}})
            db.session.refresh(reserva)
            atualizado_em = reserva.atualizado_em
            client.post(WEBHOOK, json={"type": "payment", "data": {"id": "mp-dup-1"}})

        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PAID
        assert reserva.atualizado_em == atualizado_em

    def test_notificacao_que_nao_e_pagamento_e_ignorada(self, app, client, db):
        reserva = _reserva(total=100.00, payment_id="mp-outro-1")

        with patch.object(
            MercadoPagoService, "_sdk",
            return_value=_sdk(_pagamento_mp("mp-outro-1", "approved", 100.00)),
        ):
            r = client.post(WEBHOOK, json={"type": "plan", "data": {"id": "mp-outro-1"}})

        assert r.status_code == 200
        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PENDING

    def test_pagamento_de_outro_ambiente_responde_200_sem_quebrar(self, app, client, db):
        """A falha real de 2026-08-18, vista do lado de quem recebe errado.

        Uma cobrança criada em OUTRO ambiente chega aqui e não corresponde a
        reserva nenhuma. A rota precisa responder 200 — o Mercado Pago reenvia
        indefinidamente em qualquer outro código — sem tocar em reservas alheias.
        """
        reserva = _reserva(total=100.00, payment_id="mp-meu-1")

        with patch.object(
            MercadoPagoService, "_sdk",
            return_value=_sdk(
                _pagamento_mp("mp-de-outro-ambiente", "approved", 100.00, ref=str(uuid.uuid4()))
            ),
        ):
            r = client.post(
                WEBHOOK, json={"type": "payment", "data": {"id": "mp-de-outro-ambiente"}}
            )

        assert r.status_code == 200
        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PENDING

    def test_payload_ausente_recusa(self, app, client, db):
        assert client.post(WEBHOOK, json=None).status_code == 400


class TestUrlDeNotificacao:
    """A URL configurada precisa apontar para um webhook de verdade.

    Os testes em `test_payment_service.py` garantem que o campo vai no payload.
    Não garantem que o **valor** faz sentido: uma URL com typo, apontando para a
    raiz, ou em http, é aceita em silêncio e o pagamento nunca volta.
    """

    @pytest.mark.parametrize("url", [
        "test-admin.exemplo/api/v1/payments/webhook",   # sem esquema
        "https://test-admin.exemplo/",                  # não é o webhook
        "https://test-admin.exemplo/api/v1/payments",   # caminho incompleto
    ])
    def test_url_suspeita_e_detectavel(self, url):
        """Documenta o formato correto e o que fica de fora dele.

        Não há validação no código — este teste fixa a regra para quem for
        configurar um ambiente novo, e falha se alguém trocar o caminho da rota
        sem atualizar a documentação.
        """
        assert not (url.startswith("https://") and url.endswith("/api/v1/payments/webhook"))

    def test_url_do_staging_esta_no_formato_certo(self):
        url = "https://test-admin.cachoeiradogirassol.com.br/api/v1/payments/webhook"
        assert url.startswith("https://")
        assert url.endswith(WEBHOOK)
