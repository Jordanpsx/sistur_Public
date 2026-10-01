# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes de integração para as rotas do Blueprint Ponto.

Testa autenticação e comportamento HTTP das rotas /ponto/.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.models.funcionario import Funcionario
from app.models.ponto import TimeEntry
from app.services.ponto_service import PontoService


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _criar_funcionario(db) -> Funcionario:
    """Cria e persiste um funcionário de teste."""
    f = Funcionario(
        nome="Ponto Teste",
        cpf="52998224725",
        minutos_esperados_dia=480,
        minutos_almoco=60,
        ativo=True,
    )
    db.session.add(f)
    db.session.commit()
    return f


def _login(client, funcionario_id: int):
    """Injeta uma sessão autenticada no cliente de teste."""
    with client.session_transaction() as sess:
        sess["funcionario_id"] = funcionario_id


# ---------------------------------------------------------------------------
# GET /ponto/ — histórico
# ---------------------------------------------------------------------------

class TestHistoricoRoute:

    def test_sem_sessao_redireciona_para_login(self, client, db):
        """GET /ponto/ sem sessão → redirect para /portal/login."""
        response = client.get("/ponto/")
        assert response.status_code == 302
        assert "/portal/login" in response.headers["Location"]

    def test_com_sessao_retorna_200(self, app, client, db):
        """GET /ponto/ com sessão ativa → HTTP 200."""
        with app.app_context():
            f = _criar_funcionario(db)
            fid = f.id
        _login(client, fid)
        response = client.get("/ponto/")
        assert response.status_code == 200
        assert b"Ponto Eletr" in response.data

    def test_historico_exibe_mes_corrente(self, app, client, db):
        """GET /ponto/ deve conter o ano corrente na resposta."""
        with app.app_context():
            f = _criar_funcionario(db)
            fid = f.id
        _login(client, fid)
        response = client.get("/ponto/")
        ano = str(datetime.now(timezone.utc).year).encode()
        assert ano in response.data


# ---------------------------------------------------------------------------
# POST /ponto/registrar — bater ponto
# ---------------------------------------------------------------------------

class TestRegistrarRoute:

    def test_sem_sessao_redireciona(self, client, db):
        """POST /ponto/registrar sem sessão → redirect."""
        response = client.post("/ponto/registrar", data={})
        assert response.status_code == 302
        assert "/portal/login" in response.headers["Location"]

    def test_registrar_cria_time_entry(self, app, client, db):
        """POST /ponto/registrar com sessão → cria TimeEntry e redireciona."""
        with app.app_context():
            f = _criar_funcionario(db)
            fid = f.id
        _login(client, fid)
        response = client.post(
            "/ponto/registrar",
            data={"punch_time": "2026-01-15T08:00:00"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/ponto/" in response.headers["Location"]

        with app.app_context():
            count = (
                db.session.query(TimeEntry)
                .filter_by(funcionario_id=fid)
                .count()
            )
            assert count == 1

    def test_duplicata_exibe_erro(self, app, client, db):
        """Segunda batida em menos de 5s deve exibir mensagem de erro no redirect."""
        with app.app_context():
            f = _criar_funcionario(db)
            fid = f.id
            PontoService.registrar_batida(
                funcionario_id=fid,
                punch_time=datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc),
                ator_id=fid,
            )
        _login(client, fid)

        # Segunda batida logo em seguida (2s depois)
        response = client.post(
            "/ponto/registrar",
            data={"punch_time": "2026-01-15T08:00:02"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "duplicada" in response.data.decode("utf-8").lower()
