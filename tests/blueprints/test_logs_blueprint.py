# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Testes para o endpoint POST /api/logs/payment-error."""

import pytest
from app.models.integration_log import IntegrationLog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

URL = "/api/logs/payment-error"

PAYLOAD_MINIMO = {
    "error_source": "MP_BRICKS",
    "error_message": "payment_id não encontrado.",
}

PAYLOAD_COMPLETO = {
    "error_source": "WP_INTERNAL",
    "error_message": "Erro ao conectar com servidor de pagamentos.",
    "reservation_id": 42,
    "error_code": "E301",
    "raw_data": {
        "type": "network_error",
        "status": 502,
        "detail": "Bad Gateway",
    },
}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_log_payload_minimo_retorna_201(client, db):
    """Payload com apenas os campos obrigatórios deve persistir e retornar 201."""
    resp = client.post(URL, json=PAYLOAD_MINIMO)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["logged"] is True
    assert isinstance(data["id"], int)


def test_log_payload_completo_persiste_todos_os_campos(client, db):
    """Payload completo deve persistir todos os campos no banco de dados."""
    resp = client.post(URL, json=PAYLOAD_COMPLETO)
    assert resp.status_code == 201

    log = db.session.get(IntegrationLog, resp.get_json()["id"])
    assert log is not None
    assert log.error_source == "WP_INTERNAL"
    assert log.error_message == "Erro ao conectar com servidor de pagamentos."
    assert log.reserva_id == 42
    assert log.error_code == "E301"
    assert log.raw_payload == PAYLOAD_COMPLETO["raw_data"]


def test_log_sem_reserva_id_aceito(client, db):
    """reservation_id é opcional — deve ser aceito quando ausente."""
    payload = {"error_source": "SISTUR_SYNC", "error_message": "Sync falhou."}
    resp = client.post(URL, json=payload)
    assert resp.status_code == 201
    log = db.session.get(IntegrationLog, resp.get_json()["id"])
    assert log.reserva_id is None


def test_log_error_source_normalizado_para_uppercase(client, db):
    """error_source em lowercase deve ser aceito (normalizado internamente)."""
    resp = client.post(URL, json={"error_source": "mp_bricks", "error_message": "err"})
    assert resp.status_code == 201
    log = db.session.get(IntegrationLog, resp.get_json()["id"])
    assert log.error_source == "MP_BRICKS"


def test_log_raw_data_nao_dict_ignorado(client, db):
    """raw_data que não seja dict deve ser ignorado sem causar erro."""
    payload = {**PAYLOAD_MINIMO, "raw_data": "string inválida"}
    resp = client.post(URL, json=payload)
    assert resp.status_code == 201
    log = db.session.get(IntegrationLog, resp.get_json()["id"])
    assert log.raw_payload is None


def test_log_reservation_id_invalido_ignorado(client, db):
    """reservation_id não conversível para int deve ser ignorado sem causar erro."""
    payload = {**PAYLOAD_MINIMO, "reservation_id": "abc"}
    resp = client.post(URL, json=payload)
    assert resp.status_code == 201
    log = db.session.get(IntegrationLog, resp.get_json()["id"])
    assert log.reserva_id is None


def test_log_error_message_truncada_a_2000_chars(client, db):
    """error_message com mais de 2000 chars deve ser truncada silenciosamente."""
    payload = {**PAYLOAD_MINIMO, "error_message": "x" * 3000}
    resp = client.post(URL, json=payload)
    assert resp.status_code == 201
    log = db.session.get(IntegrationLog, resp.get_json()["id"])
    assert len(log.error_message) == 2000


# ---------------------------------------------------------------------------
# Validações de entrada (400)
# ---------------------------------------------------------------------------

def test_sem_body_retorna_400(client, db):
    """Request sem body JSON deve retornar 400."""
    resp = client.post(URL, content_type="application/json")
    assert resp.status_code == 400
    assert "erro" in resp.get_json()


def test_error_source_invalido_retorna_400(client, db):
    """error_source fora do conjunto permitido deve retornar 400."""
    payload = {**PAYLOAD_MINIMO, "error_source": "DESCONHECIDO"}
    resp = client.post(URL, json=payload)
    assert resp.status_code == 400
    assert "error_source" in resp.get_json()["erro"]


def test_error_message_vazio_retorna_400(client, db):
    """error_message vazio deve retornar 400."""
    payload = {"error_source": "MP_BRICKS", "error_message": ""}
    resp = client.post(URL, json=payload)
    assert resp.status_code == 400
    assert "error_message" in resp.get_json()["erro"]


def test_error_message_ausente_retorna_400(client, db):
    """Ausência de error_message deve retornar 400."""
    resp = client.post(URL, json={"error_source": "MP_BRICKS"})
    assert resp.status_code == 400
