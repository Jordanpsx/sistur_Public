# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes de integração para o Blueprint api_internal.

Testa o endpoint POST /api/internal/ponto/qr-scan — autenticação por token Bearer,
validação de payload Fernet, anti-revogação e registro de batida via QR scanner.
"""

from __future__ import annotations

from app.core.models import AuditLog
from app.models.funcionario import Funcionario
from app.models.ponto import TimeEntry
from app.services.qr_service import QRService


# ---------------------------------------------------------------------------
# Constantes de teste
# ---------------------------------------------------------------------------

ENDPOINT = "/api/internal/ponto/qr-scan"
TEST_TOKEN = ""  # Será lido do config da app (TestingConfig)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _criar_funcionario(db, token_qr: str = "test-token-qr-abc") -> Funcionario:
    """Cria e persiste um funcionário de teste com token_qr."""
    f = Funcionario(
        nome="QR Teste",
        cpf="52998224725",
        minutos_esperados_dia=480,
        minutos_almoco=60,
        ativo=True,
        token_qr=token_qr,
    )
    db.session.add(f)
    db.session.commit()
    return f


def _gerar_payload_fernet(app, funcionario: Funcionario) -> str:
    """Gera um payload Fernet válido para o funcionário."""
    return QRService.criptografar_payload(
        funcionario_id=funcionario.id,
        nome=funcionario.nome,
        token_qr=funcionario.token_qr,
        qr_secret_key=app.config["QR_SECRET_KEY"],
    )


def _auth_headers(app) -> dict:
    """Retorna headers com Bearer token válido."""
    token = app.config["QR_SERVICE_TOKEN"]
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------------

class TestAutenticacao:

    def test_sem_header_retorna_401(self, client, db):
        """Requisição sem Authorization header → 401."""
        resp = client.post(ENDPOINT, json={"qr_payload": "abc"})
        assert resp.status_code == 401

    def test_token_invalido_retorna_401(self, client, db):
        """Token Bearer incorreto → 401."""
        headers = {"Authorization": "Bearer token-errado-123"}
        resp = client.post(ENDPOINT, json={"qr_payload": "abc"}, headers=headers)
        assert resp.status_code == 401

    def test_header_sem_bearer_prefix_retorna_401(self, client, db):
        """Header Authorization sem prefixo 'Bearer ' → 401."""
        headers = {"Authorization": "Basic abc123"}
        resp = client.post(ENDPOINT, json={"qr_payload": "abc"}, headers=headers)
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Validação de payload
# ---------------------------------------------------------------------------

class TestValidacaoPayload:

    def test_body_vazio_retorna_400(self, app, client, db):
        """Requisição sem corpo JSON → 400."""
        resp = client.post(ENDPOINT, headers=_auth_headers(app))
        assert resp.status_code == 400

    def test_sem_qr_payload_retorna_400(self, app, client, db):
        """JSON sem campo 'qr_payload' → 400."""
        resp = client.post(ENDPOINT, json={"outro": "campo"}, headers=_auth_headers(app))
        assert resp.status_code == 400
        assert "qr_payload" in resp.json["erro"]

    def test_payload_fernet_invalido_retorna_422(self, app, client, db):
        """Payload Fernet adulterado → 422."""
        resp = client.post(
            ENDPOINT,
            json={"qr_payload": "token-invalido-nao-fernet"},
            headers=_auth_headers(app),
        )
        assert resp.status_code == 422
        assert "inválido" in resp.json["erro"]


# ---------------------------------------------------------------------------
# Validação de funcionário
# ---------------------------------------------------------------------------

class TestValidacaoFuncionario:

    def test_funcionario_inexistente_retorna_422(self, app, client, db):
        """Payload Fernet válido mas funcionário não existe → 422."""
        payload = QRService.criptografar_payload(
            funcionario_id=9999,
            nome="Fantasma",
            token_qr="token-inexistente",
            qr_secret_key=app.config["QR_SECRET_KEY"],
        )
        resp = client.post(
            ENDPOINT,
            json={"qr_payload": payload},
            headers=_auth_headers(app),
        )
        assert resp.status_code == 422
        assert "não encontrado" in resp.json["erro"]

    def test_token_revogado_retorna_422(self, app, client, db):
        """QR code com token_qr antigo (revogado) → 422."""
        with app.app_context():
            f = _criar_funcionario(db, token_qr="token-atual")
            # Gera payload com token diferente do armazenado
            payload = QRService.criptografar_payload(
                funcionario_id=f.id,
                nome=f.nome,
                token_qr="token-antigo-revogado",
                qr_secret_key=app.config["QR_SECRET_KEY"],
            )

        resp = client.post(
            ENDPOINT,
            json={"qr_payload": payload},
            headers=_auth_headers(app),
        )
        assert resp.status_code == 422
        assert "revogado" in resp.json["erro"].lower()


# ---------------------------------------------------------------------------
# Fluxo de sucesso
# ---------------------------------------------------------------------------

class TestFluxoSucesso:

    def test_batida_registrada_com_sucesso(self, app, client, db):
        """QR scan válido → 201, TimeEntry criada com source='QR'."""
        with app.app_context():
            f = _criar_funcionario(db)
            payload = _gerar_payload_fernet(app, f)
            fid = f.id

        resp = client.post(
            ENDPOINT,
            json={"qr_payload": payload},
            headers=_auth_headers(app),
        )
        assert resp.status_code == 201

        data = resp.json
        assert data["sucesso"] is True
        assert data["nome"] == "QR Teste"
        assert "hora" in data
        assert "tipo_batida" in data
        assert "impressao" in data

        # Verifica TimeEntry no banco
        with app.app_context():
            entry = db.session.query(TimeEntry).filter_by(funcionario_id=fid).first()
            assert entry is not None
            assert entry.source.value == "QR"

    def test_audit_log_criado(self, app, client, db):
        """QR scan bem-sucedido deve criar exatamente um AuditLog."""
        with app.app_context():
            f = _criar_funcionario(db)
            payload = _gerar_payload_fernet(app, f)
            fid = f.id

        resp = client.post(
            ENDPOINT,
            json={"qr_payload": payload},
            headers=_auth_headers(app),
        )
        assert resp.status_code == 201

        with app.app_context():
            count = db.session.query(AuditLog).filter_by(
                module="ponto", entity_id=fid
            ).count()
            assert count == 1

    def test_resposta_contem_dados_impressao(self, app, client, db):
        """Resposta 201 inclui bloco 'impressao' com nome, hora, tipo, data_completa."""
        with app.app_context():
            f = _criar_funcionario(db)
            payload = _gerar_payload_fernet(app, f)

        resp = client.post(
            ENDPOINT,
            json={"qr_payload": payload},
            headers=_auth_headers(app),
        )
        assert resp.status_code == 201

        impressao = resp.json["impressao"]
        assert impressao["nome"] == "QR Teste"
        assert "hora" in impressao
        assert "tipo" in impressao
        assert "data_completa" in impressao
