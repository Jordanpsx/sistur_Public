# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Testes de permissões granulares em Reservas."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.reservas import Reserva, ReservaOrigin, ReservaSource, ReservaStatus
from app.models.role import Role, RolePermission


def _role(nome: str, perms: list[tuple[str, str]]) -> Role:
    role = Role(nome=nome, ativo=True)
    _db.session.add(role)
    _db.session.flush()
    for modulo, acao in perms:
        _db.session.add(RolePermission(role_id=role.id, modulo=modulo, acao=acao))
    _db.session.flush()
    return role


def _funcionario(nome: str, cpf: str, role: Role) -> Funcionario:
    funcionario = Funcionario(nome=nome, cpf=cpf, role_id=role.id, ativo=True)
    _db.session.add(funcionario)
    _db.session.commit()
    return funcionario


def _login(client, funcionario_id: int) -> None:
    with client.session_transaction() as sess:
        sess["funcionario_id"] = funcionario_id


def _reserva(
    source_name: str = "Cachoeira",
    group_id: str = "grupo-permissoes-1",
    customer_name: str = "Cliente Sigiloso",
) -> Reserva:
    source = ReservaSource(name=source_name, display_name=source_name, is_active=True)
    _db.session.add(source)
    _db.session.flush()
    reserva = Reserva(
        group_id=group_id,
        version=1,
        source_id=source.id,
        customer_name=customer_name,
        customer_document="12345678901",
        origin=ReservaOrigin.WEB,
        status=ReservaStatus.PENDING,
        check_in_date=datetime(2026, 7, 1, 9, 0),
        metadata_info={
            "telefone": "11999999999",
            "email": "cliente@example.com",
        },
        subtotal_amount=Decimal("100.00"),
        discount_amount=Decimal("10.00"),
        service_fee_amount=Decimal("5.00"),
        total_amount=Decimal("95.00"),
        payment_data={
            "transaction_id": "txn_123",
            "amount_paid": 50,
        },
        pix_data={
            "qr_code": "pix-copia-e-cola",
        },
    )
    _db.session.add(reserva)
    _db.session.commit()
    return reserva


def _scoped_perm(role: Role, modulo: str, acao: str, source_id: int) -> None:
    _db.session.add(
        RolePermission(
            role_id=role.id,
            modulo=modulo,
            acao=acao,
            source_id=source_id,
        )
    )
    _db.session.commit()


def test_api_detalhe_redige_campos_sensiveis_para_reservas_view_basico(app, client, db):
    with app.app_context():
        role = _role("reservas_basico", [("reservas", "view")])
        funcionario = _funcionario("Operador Básico", "52998224725", role)
        reserva = _reserva()
        funcionario_id = funcionario.id
        reserva_id = reserva.id

    _login(client, funcionario_id)
    response = client.get(f"/reservas/api/{reserva_id}")

    assert response.status_code == 200
    data = response.get_json()["reserva"]
    assert data["customer_name"] == "Cliente Sigiloso"
    assert "customer_document" not in data
    assert "total_amount" not in data
    assert "payment_data" not in data
    assert "pix_data" not in data
    assert data["metadata_info"] == {}


def test_api_detalhe_exibe_campos_sensiveis_com_permissoes_granulares(app, client, db):
    with app.app_context():
        role = _role(
            "reservas_completo",
            [
                ("reservas", "view"),
                ("reservas", "view_contact"),
                ("reservas", "view_financial"),
                ("reservas", "view_payments"),
            ],
        )
        funcionario = _funcionario("Operador Completo", "11144477735", role)
        reserva = _reserva()
        funcionario_id = funcionario.id
        reserva_id = reserva.id

    _login(client, funcionario_id)
    response = client.get(f"/reservas/api/{reserva_id}")

    assert response.status_code == 200
    data = response.get_json()["reserva"]
    assert data["customer_document"] == "12345678901"
    assert data["metadata_info"]["telefone"] == "11999999999"
    assert data["total_amount"] == "95.00"
    assert data["payment_data"]["transaction_id"] == "txn_123"
    assert data["pix_data"]["qr_code"] == "pix-copia-e-cola"


def test_tela_detalhe_oculta_contato_financeiro_e_pagamento_sem_permissoes(app, client, db):
    with app.app_context():
        role = _role("reservas_basico", [("reservas", "view")])
        funcionario = _funcionario("Operador Básico", "39053344705", role)
        reserva = _reserva()
        funcionario_id = funcionario.id
        reserva_id = reserva.id

    _login(client, funcionario_id)
    response = client.get(f"/reservas/{reserva_id}")
    body = response.data.decode()

    assert response.status_code == 200
    assert "Cliente Sigiloso" in body
    assert "12345678901" not in body
    assert "11999999999" not in body
    assert "Valor Total" not in body
    assert "txn_123" not in body
    assert "pix-copia-e-cola" not in body


def test_dashboard_lista_oculta_contato_e_total_sem_permissoes_granulares(app, client, db):
    with app.app_context():
        role = _role("reservas_basico", [("reservas", "view")])
        funcionario = _funcionario("Operador Básico", "64110585900", role)
        _reserva()
        funcionario_id = funcionario.id

    _login(client, funcionario_id)
    response = client.get("/reservas/?view=lista")
    body = response.data.decode()

    assert response.status_code == 200
    assert "Cliente Sigiloso" in body
    assert "11999999999" not in body
    assert "R$ 95.00" not in body


def test_permissoes_escopadas_por_venue_limitam_visibilidade(app, client, db):
    with app.app_context():
        role = _role("reservas_cachoeira", [])
        reserva_liberada = _reserva("Cachoeira", "grupo-cachoeira")
        reserva_bloqueada = _reserva("Vinhedo", "grupo-vinhedo")
        _scoped_perm(role, "reservas", "view", reserva_liberada.source_id)
        _scoped_perm(role, "reservas", "view_contact", reserva_liberada.source_id)
        funcionario = _funcionario("Operador Venue", "07699220030", role)
        funcionario_id = funcionario.id
        liberada_id = reserva_liberada.id
        bloqueada_id = reserva_bloqueada.id

    _login(client, funcionario_id)

    response_liberada = client.get(f"/reservas/api/{liberada_id}")
    assert response_liberada.status_code == 200
    data = response_liberada.get_json()["reserva"]
    assert data["customer_document"] == "12345678901"
    assert data["metadata_info"]["telefone"] == "11999999999"

    response_bloqueada = client.get(f"/reservas/api/{bloqueada_id}")
    assert response_bloqueada.status_code == 403


def test_listagens_filtram_reservas_por_venue_escopada(app, client, db):
    with app.app_context():
        role = _role("reservas_cachoeira_lista", [])
        reserva_liberada = _reserva(
            "Cachoeira",
            "grupo-cachoeira",
            customer_name="Cliente Cachoeira",
        )
        _reserva(
            "Vinhedo",
            "grupo-vinhedo",
            customer_name="Cliente Vinhedo",
        )
        _scoped_perm(role, "reservas", "view", reserva_liberada.source_id)
        funcionario = _funcionario("Operador Venue Lista", "37191730640", role)
        funcionario_id = funcionario.id

    _login(client, funcionario_id)

    response_api = client.get("/reservas/api/")
    assert response_api.status_code == 200
    data = response_api.get_json()
    assert data["total"] == 1
    assert [r["customer_name"] for r in data["reservas"]] == ["Cliente Cachoeira"]

    response_html = client.get("/reservas/?view=lista")
    body = response_html.data.decode()
    assert response_html.status_code == 200
    assert "Cliente Cachoeira" in body
    assert "Cliente Vinhedo" not in body


def test_permissao_granular_escopada_redige_outra_venue(app, client, db):
    with app.app_context():
        role = _role("reservas_misto", [("reservas", "view")])
        reserva_com_contato = _reserva("Cachoeira", "grupo-cachoeira")
        reserva_sem_contato = _reserva("Vinhedo", "grupo-vinhedo")
        _scoped_perm(role, "reservas", "view_contact", reserva_com_contato.source_id)
        funcionario = _funcionario("Operador Misto", "64110585900", role)
        funcionario_id = funcionario.id
        com_contato_id = reserva_com_contato.id
        sem_contato_id = reserva_sem_contato.id

    _login(client, funcionario_id)

    response_com_contato = client.get(f"/reservas/api/{com_contato_id}")
    assert response_com_contato.status_code == 200
    assert "customer_document" in response_com_contato.get_json()["reserva"]

    response_sem_contato = client.get(f"/reservas/api/{sem_contato_id}")
    assert response_sem_contato.status_code == 200
    assert "customer_document" not in response_sem_contato.get_json()["reserva"]
