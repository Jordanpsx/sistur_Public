# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes de integração para as rotas administrativas de Ponto/QR do blueprint RH:

    GET  /rh/funcionarios/<id>/banco-horas
    GET  /rh/folha-ponto/<id>/pdf
    GET  /rh/funcionarios/<id>/qrcode.png
    POST /rh/funcionarios/<id>/regenerar-qrcode

Fecham a lacuna do item 2.8 P3: rotas que tocam dinheiro (extrato de banco de
horas, PDF financeiro) e credenciais de ponto (QR code), sem cobertura de
rota até então.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from app.core.audit import AuditAction
from app.core.models import AuditLog
from app.extensions import db as _db
from app.models.funcionario import Funcionario
from app.models.role import Role, RolePermission
from app.services.ponto_service import PontoService


# ---------------------------------------------------------------------------
# Helpers de fixtures
# ---------------------------------------------------------------------------

def _criar_role(nome: str, permissoes: list[tuple[str, str]], is_super_admin: bool = False) -> Role:
    """Cria um Role com uma lista de (modulo, acao) como RolePermission."""
    role = Role(nome=nome, descricao=nome, is_super_admin=is_super_admin, ativo=True)
    _db.session.add(role)
    _db.session.flush()
    for modulo, acao in permissoes:
        _db.session.add(RolePermission(role_id=role.id, modulo=modulo, acao=acao))
    _db.session.flush()
    return role


def _criar_funcionario(nome: str, cpf: str, role: Role | None = None) -> Funcionario:
    f = Funcionario(nome=nome, cpf=cpf, ativo=True, minutos_esperados_dia=480, minutos_almoco=60)
    if role:
        f.role_id = role.id
    _db.session.add(f)
    _db.session.commit()
    return f


def _login(client, funcionario_id: int):
    with client.session_transaction() as sess:
        sess["funcionario_id"] = funcionario_id


def _registrar_ponto_completo(funcionario_id: int):
    """Registra um par entrada/saída no dia corrente (UTC), gerando um TimeDay.

    Usa 13h/21h UTC (10h/18h em America/Sao_Paulo) para garantir que ambas as
    batidas caiam no mesmo `shift_date` local, independentemente de quando o
    teste rodar — evita virar o dia por estar perto da meia-noite UTC.
    """
    hoje = date.today()
    entrada = datetime.combine(hoje, time(13, 0), tzinfo=timezone.utc)
    saida = datetime.combine(hoje, time(21, 0), tzinfo=timezone.utc)
    PontoService.registrar_batida(
        funcionario_id=funcionario_id, punch_time=entrada, source="admin", ator_id=funcionario_id,
    )
    PontoService.registrar_batida(
        funcionario_id=funcionario_id, punch_time=saida, source="admin", ator_id=funcionario_id,
    )


# ---------------------------------------------------------------------------
# GET /rh/funcionarios/<id>/banco-horas
# ---------------------------------------------------------------------------

class TestBancoHorasFuncionario:

    def test_sem_sessao_redireciona_para_login(self, client, db):
        alvo = _criar_funcionario("Alvo BH", "52998224725")
        resp = client.get(f"/rh/funcionarios/{alvo.id}/banco-horas")
        assert resp.status_code == 302
        assert "/portal/login" in resp.headers["Location"]

    def test_sem_permissao_retorna_403(self, app, client, db):
        with app.app_context():
            role = _criar_role("sem_view", [("dashboard", "view")])
            ator = _criar_funcionario("Sem Permissão", "07699220030", role)
            alvo = _criar_funcionario("Alvo BH", "11144477735")
            ator_id, alvo_id = ator.id, alvo.id
        _login(client, ator_id)
        resp = client.get(f"/rh/funcionarios/{alvo_id}/banco-horas")
        assert resp.status_code == 403

    def test_com_permissao_retorna_200_com_dados_do_mes(self, app, client, db):
        with app.app_context():
            role = _criar_role("rh_view", [("funcionarios", "view")])
            ator = _criar_funcionario("RH Visor", "64110585900", role)
            alvo = _criar_funcionario("Alvo BH", "39053344705")
            ator_id, alvo_id = ator.id, alvo.id
            _registrar_ponto_completo(alvo_id)

        _login(client, ator_id)
        resp = client.get(f"/rh/funcionarios/{alvo_id}/banco-horas")

        assert resp.status_code == 200
        corpo = resp.data.decode()
        assert "Alvo BH" in corpo

    def test_funcionario_inexistente_redireciona_com_flash(self, app, client, db):
        with app.app_context():
            role = _criar_role("rh_view2", [("funcionarios", "view")])
            ator = _criar_funcionario("RH Visor 2", "16899854372", role)
            ator_id = ator.id

        _login(client, ator_id)
        resp = client.get("/rh/funcionarios/999999/banco-horas")

        assert resp.status_code == 302
        assert "/rh/" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# GET /rh/folha-ponto/<id>/pdf
# ---------------------------------------------------------------------------

class TestFolhaPontoPdf:

    def test_sem_sessao_redireciona_para_login(self, client, db):
        alvo = _criar_funcionario("Alvo PDF", "52998224725")
        resp = client.get(f"/rh/folha-ponto/{alvo.id}/pdf")
        assert resp.status_code == 302
        assert "/portal/login" in resp.headers["Location"]

    def test_sem_permissao_retorna_403(self, app, client, db):
        with app.app_context():
            role = _criar_role("sem_imprimir", [("folha_ponto", "view")])
            ator = _criar_funcionario("Sem Imprimir", "07699220030", role)
            alvo = _criar_funcionario("Alvo PDF", "11144477735")
            ator_id, alvo_id = ator.id, alvo.id
        _login(client, ator_id)
        resp = client.get(f"/rh/folha-ponto/{alvo_id}/pdf")
        assert resp.status_code == 403

    def test_com_permissao_retorna_pdf(self, app, client, db):
        with app.app_context():
            role = _criar_role("rh_imprimir", [("folha_ponto", "imprimir")])
            ator = _criar_funcionario("RH Imprimir", "64110585900", role)
            alvo = _criar_funcionario("Alvo PDF", "39053344705")
            ator_id, alvo_id = ator.id, alvo.id
            _registrar_ponto_completo(alvo_id)

        _login(client, ator_id)
        resp = client.get(f"/rh/folha-ponto/{alvo_id}/pdf")

        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "application/pdf"
        assert resp.data[:4] == b"%PDF"

    def test_funcionario_inexistente_redireciona_com_flash(self, app, client, db):
        with app.app_context():
            role = _criar_role("rh_imprimir2", [("folha_ponto", "imprimir")])
            ator = _criar_funcionario("RH Imprimir 2", "16899854372", role)
            ator_id = ator.id

        _login(client, ator_id)
        resp = client.get("/rh/folha-ponto/999999/pdf")

        assert resp.status_code == 302
        assert "/rh/" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# GET /rh/funcionarios/<id>/qrcode.png
# ---------------------------------------------------------------------------

class TestQrcodePng:

    def test_sem_sessao_redireciona_para_login(self, client, db):
        alvo = _criar_funcionario("Alvo QR", "52998224725")
        resp = client.get(f"/rh/funcionarios/{alvo.id}/qrcode.png")
        assert resp.status_code == 302
        assert "/portal/login" in resp.headers["Location"]

    def test_visualizacao_do_proprio_qr_nao_exige_permissao(self, app, client, db):
        with app.app_context():
            role = _criar_role("basico", [("dashboard", "view")])
            f = _criar_funcionario("Autoserve", "07699220030", role)
            fid = f.id

        _login(client, fid)
        resp = client.get(f"/rh/funcionarios/{fid}/qrcode.png")

        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "image/png"

    def test_visualizar_qr_de_outro_sem_permissao_retorna_403(self, app, client, db):
        with app.app_context():
            role = _criar_role("basico2", [("dashboard", "view")])
            ator = _criar_funcionario("Sem Permissão QR", "64110585900", role)
            alvo = _criar_funcionario("Outro Colega", "39053344705")
            ator_id, alvo_id = ator.id, alvo.id

        _login(client, ator_id)
        resp = client.get(f"/rh/funcionarios/{alvo_id}/qrcode.png")
        assert resp.status_code == 403

    def test_visualizar_qr_de_outro_com_permissao_retorna_200(self, app, client, db):
        with app.app_context():
            role = _criar_role("qr_view", [("qrcode", "view")])
            ator = _criar_funcionario("Com Permissão QR", "16899854372", role)
            alvo = _criar_funcionario("Outro Colega 2", "45372918460")
            ator_id, alvo_id = ator.id, alvo.id

        _login(client, ator_id)
        resp = client.get(f"/rh/funcionarios/{alvo_id}/qrcode.png")

        assert resp.status_code == 200
        assert resp.headers["Content-Type"] == "image/png"

    def test_funcionario_inexistente_retorna_404(self, app, client, db):
        with app.app_context():
            role = _criar_role("qr_view2", [("qrcode", "view")])
            ator = _criar_funcionario("Com Permissão QR 2", "98765432100", role)
            ator_id = ator.id

        _login(client, ator_id)
        resp = client.get("/rh/funcionarios/999999/qrcode.png")
        assert resp.status_code == 404

    def test_gera_token_automaticamente_se_ausente(self, app, client, db):
        """Funcionário sem token_qr (cadastrado antes da feature) recebe um novo token ao acessar."""
        with app.app_context():
            f = _criar_funcionario("Sem Token", "36572819400")
            assert f.token_qr is None
            fid = f.id

        _login(client, fid)
        resp = client.get(f"/rh/funcionarios/{fid}/qrcode.png")

        assert resp.status_code == 200
        with app.app_context():
            atualizado = _db.session.get(Funcionario, fid)
            assert atualizado.token_qr is not None


# ---------------------------------------------------------------------------
# POST /rh/funcionarios/<id>/regenerar-qrcode
# ---------------------------------------------------------------------------

class TestRegenerarQrcode:

    def test_sem_sessao_redireciona_para_login(self, client, db):
        alvo = _criar_funcionario("Alvo Regen", "52998224725")
        resp = client.post(f"/rh/funcionarios/{alvo.id}/regenerar-qrcode")
        assert resp.status_code == 302
        assert "/portal/login" in resp.headers["Location"]

    def test_sem_permissao_retorna_403(self, app, client, db):
        with app.app_context():
            role = _criar_role("sem_regen", [("qrcode", "view")])
            ator = _criar_funcionario("Sem Regenerar", "07699220030", role)
            alvo = _criar_funcionario("Alvo Regen", "11144477735")
            ator_id, alvo_id = ator.id, alvo.id
        _login(client, ator_id)
        resp = client.post(f"/rh/funcionarios/{alvo_id}/regenerar-qrcode")
        assert resp.status_code == 403

    def test_com_permissao_regenera_token_e_audita(self, app, client, db):
        with app.app_context():
            role = _criar_role("qr_regen", [("qrcode", "regenerar")])
            ator = _criar_funcionario("Pode Regenerar", "64110585900", role)
            alvo = _criar_funcionario("Alvo Regen 2", "39053344705")
            ator_id, alvo_id = ator.id, alvo.id
            token_anterior = alvo.token_qr

        resp = self._post_regenerar(client, ator_id, alvo_id)

        assert resp.status_code == 302
        assert f"/rh/funcionarios/{alvo_id}/editar" in resp.headers["Location"]

        with app.app_context():
            atualizado = _db.session.get(Funcionario, alvo_id)
            assert atualizado.token_qr is not None
            assert atualizado.token_qr != token_anterior

            count = (
                _db.session.query(AuditLog)
                .filter_by(module="funcionarios", entity_id=alvo_id, action=AuditAction.update)
                .count()
            )
            assert count == 1

    @staticmethod
    def _post_regenerar(client, ator_id: int, alvo_id: int):
        _login(client, ator_id)
        return client.post(f"/rh/funcionarios/{alvo_id}/regenerar-qrcode")
