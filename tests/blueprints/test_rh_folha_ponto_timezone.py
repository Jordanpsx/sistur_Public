# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Regressões de fuso horário para a Folha de Ponto do RH."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.ponto import TimeEntry
from app.models.role import Role
from app.services.ponto_service import PontoService


LOCAL_TZ = ZoneInfo("America/Sao_Paulo")


def _as_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(LOCAL_TZ)


def _criar_admin_e_funcionario():
    role = Role(nome="super_admin", descricao="Acesso total", is_super_admin=True, ativo=True)
    _db.session.add(role)
    _db.session.flush()

    admin = Funcionario(nome="Admin RH", cpf="52998224725", ativo=True, role_id=role.id)
    funcionario = Funcionario(nome="Funcionario Ponto", cpf="07699220030", ativo=True)
    _db.session.add_all([admin, funcionario])
    _db.session.commit()
    return admin, funcionario


def _login(client, funcionario_id: int):
    with client.session_transaction() as sess:
        sess["funcionario_id"] = funcionario_id


def test_adicionar_batida_interpreta_datetime_local_no_fuso_do_sistema(app, client, db):
    with app.app_context():
        admin, funcionario = _criar_admin_e_funcionario()
        admin_id = admin.id
        funcionario_id = funcionario.id

    _login(client, admin_id)
    response = client.post(
        f"/rh/folha-ponto/{funcionario_id}/batida/adicionar",
        data={
            "novo_horario": "2026-01-15T08:00",
            "motivo": "Correção manual",
            "mes": "1",
            "ano": "2026",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    with app.app_context():
        entry = _db.session.query(TimeEntry).filter_by(funcionario_id=funcionario_id).one()
        assert _as_local(entry.punch_time).strftime("%Y-%m-%dT%H:%M") == "2026-01-15T08:00"
        assert entry.shift_date.isoformat() == "2026-01-15"


def test_editar_batida_preserva_horario_digitado_como_horario_local(app, client, db):
    with app.app_context():
        admin, funcionario = _criar_admin_e_funcionario()
        entry = PontoService.registrar_batida(
            funcionario_id=funcionario.id,
            punch_time=datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc),
            source="admin",
            ator_id=admin.id,
        )
        admin_id = admin.id
        funcionario_id = funcionario.id
        entry_id = entry.id

    _login(client, admin_id)
    response = client.post(
        f"/rh/folha-ponto/{funcionario_id}/batida/{entry_id}/editar",
        data={
            "novo_horario": "2026-01-15T09:30",
            "motivo": "Ajuste de horário",
            "mes": "1",
            "ano": "2026",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    with app.app_context():
        updated = _db.session.get(TimeEntry, entry_id)
        assert _as_local(updated.punch_time).strftime("%Y-%m-%dT%H:%M") == "2026-01-15T09:30"
        assert updated.shift_date.isoformat() == "2026-01-15"


def test_batida_manual_perto_da_meia_noite_mantem_shift_date_local(app, client, db):
    with app.app_context():
        admin, funcionario = _criar_admin_e_funcionario()
        admin_id = admin.id
        funcionario_id = funcionario.id

    _login(client, admin_id)
    response = client.post(
        f"/rh/folha-ponto/{funcionario_id}/batida/adicionar",
        data={
            "novo_horario": "2026-01-15T23:30",
            "motivo": "Plantão",
            "mes": "1",
            "ano": "2026",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    with app.app_context():
        entry = _db.session.query(TimeEntry).filter_by(funcionario_id=funcionario_id).one()
        assert _as_local(entry.punch_time).strftime("%Y-%m-%dT%H:%M") == "2026-01-15T23:30"
        assert entry.punch_time.replace(tzinfo=timezone.utc).date().isoformat() == "2026-01-16"
        assert entry.shift_date.isoformat() == "2026-01-15"
