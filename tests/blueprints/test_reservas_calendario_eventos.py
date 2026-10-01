# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes das rotas de data especial geridas pelo módulo de Reservas.

O foco é a POLÍTICA de fronteira entre os dois calendários: o módulo cria
eventos que afetam só preço e só mantém o que ele mesmo criou enquanto o
evento não for promovido a evento de folha pelo super admin.
"""

from __future__ import annotations

from datetime import date

from app.extensions import db as _db
from app.models.calendario import GlobalEvent, TipoEvento
from app.models.funcionario import Funcionario
from app.models.role import Role, RolePermission

BASE = "/reservas/admin/config/ajustes-preco/calendar/evento"


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


def _admin_modulo(client):
    """Loga um usuário com reservas.configurar (admin do módulo)."""
    role = _role("Admin Reservas", [("reservas", "view"), ("reservas", "configurar")])
    func = _funcionario("Admin Reservas", "52998224725", role)
    _login(client, func.id)
    return func


def _evento(db, **kwargs) -> GlobalEvent:
    defaults = {
        "titulo": "Alta Temporada Julho",
        "data_evento": date(2027, 7, 1),
        "data_fim": date(2027, 7, 31),
        "tipo": TipoEvento.HIGH_SEASON,
        "afeta_precificacao": True,
        "afeta_folha": False,
        "recorrente_anual": False,
        "origem": GlobalEvent.ORIGEM_RESERVAS,
    }
    defaults.update(kwargs)
    ev = GlobalEvent(**defaults)
    db.session.add(ev)
    db.session.commit()
    return ev


# ---------------------------------------------------------------------------
# Criação
# ---------------------------------------------------------------------------

class TestCriarEvento:

    def test_evento_nasce_so_de_preco_e_com_origem_reservas(self, app, client, db):
        """A rota força os flags — nunca lê afeta_folha do formulário."""
        with app.app_context():
            _admin_modulo(client)

            resp = client.post(f"{BASE}/criar", data={
                "titulo": "Alta Temporada Julho",
                "data_evento": "2027-07-01",
                "data_fim": "2027-07-31",
                "tipo": "HIGH_SEASON",
                # Tentativa deliberada de promover o evento pelo formulário:
                "afeta_folha": "1",
            })

            assert resp.status_code == 302
            ev = db.session.query(GlobalEvent).one()
            assert ev.titulo == "Alta Temporada Julho"
            assert ev.data_fim == date(2027, 7, 31)
            assert ev.afeta_precificacao is True
            assert ev.afeta_folha is False
            assert ev.origem == GlobalEvent.ORIGEM_RESERVAS

    def test_tipo_manutencao_e_rejeitado(self, app, client, db):
        """MAINTENANCE é operacional, não de preço — fica só no calendário global."""
        with app.app_context():
            _admin_modulo(client)

            client.post(f"{BASE}/criar", data={
                "titulo": "Parada técnica",
                "data_evento": "2027-07-01",
                "tipo": "MAINTENANCE",
            })

            assert db.session.query(GlobalEvent).count() == 0

    def test_data_invalida_nao_cria(self, app, client, db):
        with app.app_context():
            _admin_modulo(client)

            client.post(f"{BASE}/criar", data={
                "titulo": "Qualquer", "data_evento": "31/07/2027",
                "tipo": "HIGH_SEASON",
            })

            assert db.session.query(GlobalEvent).count() == 0


# ---------------------------------------------------------------------------
# Política de edição
# ---------------------------------------------------------------------------

class TestPoliticaDeEdicao:

    def test_edita_evento_do_proprio_modulo(self, app, client, db):
        with app.app_context():
            _admin_modulo(client)
            ev = _evento(db)

            client.post(f"{BASE}/{ev.id}/editar", data={
                "titulo": "Alta Temporada Julho (revisada)",
                "data_evento": "2027-07-05",
                "data_fim": "2027-07-25",
                "tipo": "HIGH_SEASON",
            })

            db.session.refresh(ev)
            assert ev.titulo == "Alta Temporada Julho (revisada)"
            assert ev.data_evento == date(2027, 7, 5)

    def test_nao_edita_evento_de_origem_global(self, app, client, db):
        with app.app_context():
            _admin_modulo(client)
            ev = _evento(db, titulo="Natal", origem=GlobalEvent.ORIGEM_GLOBAL)

            client.post(f"{BASE}/{ev.id}/editar", data={
                "titulo": "Sequestrado",
                "data_evento": "2027-07-05",
                "tipo": "HIGH_SEASON",
            })

            db.session.refresh(ev)
            assert ev.titulo == "Natal"

    def test_nao_edita_evento_promovido_a_folha(self, app, client, db):
        """Depois da promoção pelo super admin, o módulo perde a caneta."""
        with app.app_context():
            _admin_modulo(client)
            ev = _evento(db, titulo="Feriado Municipal", afeta_folha=True)

            client.post(f"{BASE}/{ev.id}/editar", data={
                "titulo": "Alterado",
                "data_evento": "2027-07-05",
                "tipo": "LOCAL_HOLIDAY",
            })

            db.session.refresh(ev)
            assert ev.titulo == "Feriado Municipal"

    def test_deleta_evento_do_proprio_modulo(self, app, client, db):
        with app.app_context():
            _admin_modulo(client)
            ev = _evento(db)

            client.post(f"{BASE}/{ev.id}/deletar")

            assert db.session.query(GlobalEvent).count() == 0

    def test_nao_deleta_evento_promovido_a_folha(self, app, client, db):
        with app.app_context():
            _admin_modulo(client)
            ev = _evento(db, afeta_folha=True)

            client.post(f"{BASE}/{ev.id}/deletar")

            assert db.session.get(GlobalEvent, ev.id) is not None


# ---------------------------------------------------------------------------
# Permissão
# ---------------------------------------------------------------------------

class TestPermissao:

    def test_reservas_edit_sozinho_nao_basta(self, app, client, db):
        """Quem só edita reservas de cliente não configura o módulo."""
        with app.app_context():
            role = _role("Operador", [("reservas", "view"), ("reservas", "edit")])
            func = _funcionario("Operador Balcão", "11144477735", role)
            _login(client, func.id)

            resp = client.post(f"{BASE}/criar", data={
                "titulo": "Alta Temporada", "data_evento": "2027-07-01",
                "tipo": "HIGH_SEASON",
            })

            assert resp.status_code == 403
            assert db.session.query(GlobalEvent).count() == 0

    def test_hub_exige_configurar(self, app, client, db):
        with app.app_context():
            role = _role("Operador", [("reservas", "view"), ("reservas", "edit")])
            func = _funcionario("Operador Balcão", "11144477735", role)
            _login(client, func.id)

            assert client.get("/reservas/admin/config/").status_code == 403

    def test_hub_acessivel_com_configurar(self, app, client, db):
        with app.app_context():
            _admin_modulo(client)
            assert client.get("/reservas/admin/config/").status_code == 200


# ---------------------------------------------------------------------------
# Feed do calendário
# ---------------------------------------------------------------------------

class TestFeedCalendario:

    def test_api_devolve_envelope_com_eventos(self, app, client, db):
        """O feed passou a ser envelope {discounts, eventos}."""
        with app.app_context():
            from app.models.reservas import ReservaSource

            _admin_modulo(client)
            fonte = ReservaSource(name="Cachoeira", is_active=True)
            db.session.add(fonte)
            db.session.commit()
            _evento(db)

            resp = client.get(
                "/reservas/admin/config/ajustes-preco/calendar/api"
                f"?source_id={fonte.id}&year=2027&month=7"
            )

            assert resp.status_code == 200
            data = resp.get_json()
            assert data["discounts"] == []
            assert len(data["eventos"]) == 1

            ev = data["eventos"][0]
            assert ev["titulo"] == "Alta Temporada Julho"
            assert ev["data_inicio"] == "2027-07-01"
            assert ev["data_fim"] == "2027-07-31"
            assert ev["editavel"] is True

    def test_evento_global_vem_como_nao_editavel(self, app, client, db):
        with app.app_context():
            from app.models.reservas import ReservaSource

            _admin_modulo(client)
            fonte = ReservaSource(name="Cachoeira", is_active=True)
            db.session.add(fonte)
            db.session.commit()
            _evento(db, origem=GlobalEvent.ORIGEM_GLOBAL)

            resp = client.get(
                "/reservas/admin/config/ajustes-preco/calendar/api"
                f"?source_id={fonte.id}&year=2027&month=7"
            )

            assert resp.get_json()["eventos"][0]["editavel"] is False
