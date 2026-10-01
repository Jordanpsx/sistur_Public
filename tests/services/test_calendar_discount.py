# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes unitários para o sistema de descontos por data específica (CalendarDiscount).

Cobertura:
  - _find_calendar_discount: retorna None quando sem desconto para a data
  - _find_calendar_discount: retorna None quando CalendarDiscount existe mas sem tiers
  - _find_calendar_discount: aplica tier de antecedência quando qualifica
  - _find_calendar_discount: retorna None quando nenhum tier de antecedência qualifica
  - _find_calendar_discount: prioriza category-specific sobre fonte
  - _find_calendar_discount: ignora descontos inativos
  - _find_calendar_discount: seleciona o tier de maior antecedência aplicável
  - CalendarDiscount tem prioridade sobre AdvanceDiscountRule no calculate()
  - Criação via rota: persiste + AuditLog
  - Criação duplicada (mesma fonte+cat+data) é rejeitada
  - Remoção via rota: remove + AuditLog
  - JSON API retorna apenas descontos do mês solicitado
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.audit import AuditLog
from app.models.reservas import (
    AdvanceDiscountRule,
    CalendarDiscount,
    CalendarDiscountAdvanceTier,
    ReservaCategory,
    ReservaSource,
)
from app.services.reserva_pricing_service import ReservaPricingService


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_source(db) -> ReservaSource:
    """Cria ReservaSource mínima."""
    src = ReservaSource(name=f"fonte-{uuid.uuid4().hex[:6]}", display_name="Teste")
    db.session.add(src)
    db.session.flush()
    return src


def _make_cd(
    db,
    source: ReservaSource,
    *,
    discount_date: date,
    label: str | None = None,
    category_id: int | None = None,
    is_active: bool = True,
) -> CalendarDiscount:
    """Cria CalendarDiscount sem discount_percentage (não mais usado)."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    cd = CalendarDiscount(
        source_id=source.id,
        category_id=category_id,
        discount_date=discount_date,
        label=label,
        is_active=is_active,
        criado_em=now,
        atualizado_em=now,
    )
    db.session.add(cd)
    db.session.flush()
    return cd


def _add_advance_tier(
    db,
    cd: CalendarDiscount,
    *,
    min_days: int,
    pct: float,
    min_stay: int | None = None,
) -> CalendarDiscountAdvanceTier:
    """Adiciona CalendarDiscountAdvanceTier. min_stay=None = sem restrição de estadia."""
    tier = CalendarDiscountAdvanceTier(
        calendar_discount_id=cd.id,
        min_days_in_advance=min_days,
        min_stay_days=min_stay,
        discount_percentage=Decimal(str(pct)),
    )
    db.session.add(tier)
    db.session.flush()
    return tier


# ---------------------------------------------------------------------------
# Testes de serviço — _find_calendar_discount
# ---------------------------------------------------------------------------


class TestFindCalendarDiscount:
    """Testa a lógica de busca de desconto de calendário com tiers de antecedência."""

    def test_retorna_none_quando_sem_desconto_na_data(self, app, db):
        """Data sem CalendarDiscount cadastrado deve retornar None."""
        with app.app_context():
            src = _make_source(db)
            db.session.commit()

            result = ReservaPricingService._find_calendar_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=date.today() + timedelta(days=5),
            )
            assert result is None

    def test_retorna_none_quando_sem_tiers(self, app, db):
        """CalendarDiscount ativo sem tiers não deve produzir desconto."""
        with app.app_context():
            src = _make_source(db)
            target = date.today() + timedelta(days=10)
            _make_cd(db, src, discount_date=target, label="Data Especial")
            db.session.commit()

            result = ReservaPricingService._find_calendar_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=target,
            )
            assert result is None

    def test_aplica_tier_quando_antecedencia_qualifica(self, app, db):
        """Tier com min_days_in_advance <= days_in_advance deve ser retornado."""
        with app.app_context():
            src = _make_source(db)
            target = date.today() + timedelta(days=40)
            cd = _make_cd(db, src, discount_date=target, label="Black Friday")
            tier = _add_advance_tier(db, cd, min_days=30, pct=25.0)
            db.session.commit()

            result = ReservaPricingService._find_calendar_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=target,
            )

            assert result is not None
            assert result["percent"] == 25.0
            assert result["name"] == "Black Friday"
            assert result["calendar_discount_id"] == cd.id
            assert result["advance_tier_id"] == tier.id
            assert result["advance_tier_min_days"] == 30

    def test_retorna_none_quando_antecedencia_insuficiente(self, app, db):
        """Se nenhum tier de antecedência qualifica, retorna None."""
        with app.app_context():
            src = _make_source(db)
            target = date.today() + timedelta(days=10)
            cd = _make_cd(db, src, discount_date=target)
            _add_advance_tier(db, cd, min_days=30, pct=20.0)
            db.session.commit()

            result = ReservaPricingService._find_calendar_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=target,
            )
            assert result is None

    def test_seleciona_tier_de_maior_antecedencia_aplicavel(self, app, db):
        """Com múltiplos tiers, deve usar o de maior min_days_in_advance que qualifica."""
        with app.app_context():
            src = _make_source(db)
            target = date.today() + timedelta(days=50)
            cd = _make_cd(db, src, discount_date=target)
            _add_advance_tier(db, cd, min_days=15, pct=10.0)
            tier_maior = _add_advance_tier(db, cd, min_days=30, pct=25.0)
            _add_advance_tier(db, cd, min_days=60, pct=40.0)  # não qualifica (dias=50 < 60)
            db.session.commit()

            result = ReservaPricingService._find_calendar_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=target,
            )

            assert result["percent"] == 25.0
            assert result["advance_tier_id"] == tier_maior.id
            assert result["advance_tier_min_days"] == 30

    def test_ignora_desconto_inativo(self, app, db):
        """CalendarDiscount inativo não deve ser retornado."""
        with app.app_context():
            src = _make_source(db)
            target = date.today() + timedelta(days=40)
            cd = _make_cd(db, src, discount_date=target, is_active=False)
            _add_advance_tier(db, cd, min_days=10, pct=30.0)
            db.session.commit()

            result = ReservaPricingService._find_calendar_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=target,
            )
            assert result is None

    def test_category_specific_tem_prioridade(self, app, db):
        """Desconto específico de categoria prevalece sobre desconto de fonte."""
        with app.app_context():
            src = _make_source(db)
            cat = ReservaCategory(source_id=src.id, name="Camping")
            db.session.add(cat)
            db.session.flush()

            target = date.today() + timedelta(days=40)
            cd_fonte = _make_cd(db, src, discount_date=target, category_id=None)
            _add_advance_tier(db, cd_fonte, min_days=10, pct=10.0)

            cd_cat = _make_cd(db, src, discount_date=target, category_id=cat.id)
            _add_advance_tier(db, cd_cat, min_days=10, pct=25.0)
            db.session.commit()

            result = ReservaPricingService._find_calendar_discount(
                source_id=src.id,
                category_id=cat.id,
                check_in_date=target,
            )

            assert result["percent"] == 25.0
            assert result["calendar_discount_id"] == cd_cat.id


# ---------------------------------------------------------------------------
# Testes de serviço — dimensão de estadia em _find_calendar_discount
# ---------------------------------------------------------------------------


class TestFindCalendarDiscountStay:
    """Testa a dimensão min_stay_days dos tiers de CalendarDiscount."""

    def test_tier_com_stay_qualifica_quando_estadia_suficiente(self, app, db):
        """Tier com min_stay_days=3 deve qualificar para estadia de 5 noites."""
        with app.app_context():
            src = _make_source(db)
            target = date.today() + timedelta(days=40)
            cd = _make_cd(db, src, discount_date=target, label="Verão")
            tier = _add_advance_tier(db, cd, min_days=20, pct=30.0, min_stay=3)
            db.session.commit()

            result = ReservaPricingService._find_calendar_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=target,
                stay_days=5,
            )

            assert result is not None
            assert result["percent"] == 30.0
            assert result["advance_tier_id"] == tier.id
            assert result["advance_tier_min_stay"] == 3

    def test_tier_com_stay_nao_qualifica_quando_estadia_curta(self, app, db):
        """Tier com min_stay_days=3 não deve qualificar para estadia de 2 noites."""
        with app.app_context():
            src = _make_source(db)
            target = date.today() + timedelta(days=40)
            cd = _make_cd(db, src, discount_date=target)
            _add_advance_tier(db, cd, min_days=20, pct=30.0, min_stay=3)
            db.session.commit()

            result = ReservaPricingService._find_calendar_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=target,
                stay_days=2,
            )

            assert result is None

    def test_tier_generico_usado_quando_stay_especifico_nao_qualifica(self, app, db):
        """Tier sem restrição de estadia serve de fallback quando stay-tier não qualifica."""
        with app.app_context():
            src = _make_source(db)
            target = date.today() + timedelta(days=40)
            cd = _make_cd(db, src, discount_date=target)
            tier_base = _add_advance_tier(db, cd, min_days=20, pct=10.0, min_stay=None)
            _add_advance_tier(db, cd, min_days=20, pct=25.0, min_stay=3)
            db.session.commit()

            # Estadia = 2 noites: o stay-tier (3+) não qualifica, mas o base (None) sim.
            result = ReservaPricingService._find_calendar_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=target,
                stay_days=2,
            )

            assert result is not None
            assert result["percent"] == 10.0
            assert result["advance_tier_id"] == tier_base.id
            assert result["advance_tier_min_stay"] is None

    def test_tier_stay_especifico_vence_tier_generico(self, app, db):
        """Tier com min_stay_days definido prevalece sobre tier genérico (None) quando qualifica."""
        with app.app_context():
            src = _make_source(db)
            target = date.today() + timedelta(days=40)
            cd = _make_cd(db, src, discount_date=target)
            _add_advance_tier(db, cd, min_days=20, pct=10.0, min_stay=None)
            tier_stay = _add_advance_tier(db, cd, min_days=20, pct=25.0, min_stay=3)
            db.session.commit()

            # Estadia = 5 noites: tanto o base quanto o stay-tier qualificam;
            # o mais específico (min_stay=3) deve vencer.
            result = ReservaPricingService._find_calendar_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=target,
                stay_days=5,
            )

            assert result is not None
            assert result["percent"] == 25.0
            assert result["advance_tier_id"] == tier_stay.id

    def test_seleciona_melhor_tier_2d(self, app, db):
        """Com vários tiers 2D, usa o mais específico que qualifica nas duas dimensões."""
        with app.app_context():
            src = _make_source(db)
            target = date.today() + timedelta(days=50)
            cd = _make_cd(db, src, discount_date=target, label="Black Friday")
            _add_advance_tier(db, cd, min_days=30, pct=10.0, min_stay=None)  # base
            _add_advance_tier(db, cd, min_days=30, pct=15.0, min_stay=2)
            tier_melhor = _add_advance_tier(db, cd, min_days=30, pct=25.0, min_stay=4)
            _add_advance_tier(db, cd, min_days=30, pct=40.0, min_stay=7)  # não qualifica
            db.session.commit()

            # 50 dias de antecedência, 5 noites de estadia:
            # - base (None): qualifica → 10%
            # - min_stay=2:  qualifica (5≥2) → 15%
            # - min_stay=4:  qualifica (5≥4) → 25%  ← deve vencer
            # - min_stay=7:  não qualifica (5<7)
            result = ReservaPricingService._find_calendar_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=target,
                stay_days=5,
            )

            assert result is not None
            assert result["percent"] == 25.0
            assert result["advance_tier_id"] == tier_melhor.id
            assert result["name"] == "Black Friday"


# ---------------------------------------------------------------------------
# Teste de integração — CalendarDiscount tem prioridade sobre AdvanceDiscountRule
# ---------------------------------------------------------------------------


class TestCalendarDiscountPriority:
    """Verifica que CalendarDiscount substitui o Early Bird quando existe e qualifica."""

    def test_calendar_overrides_advance_discount(self, app, db):
        """CalendarDiscount com tier qualificado deve sobrepor AdvanceDiscountRule."""
        with app.app_context():
            src = _make_source(db)
            target = date.today() + timedelta(days=45)

            rule = AdvanceDiscountRule(
                source_id=src.id,
                category_id=None,
                min_days_in_advance=30,
                discount_percentage=Decimal("10.0"),
                is_active=True,
            )
            db.session.add(rule)

            cd = _make_cd(db, src, discount_date=target, label="Black Friday")
            _add_advance_tier(db, cd, min_days=30, pct=35.0)
            db.session.commit()

            advance = ReservaPricingService._find_best_advance_discount(
                source_id=src.id, category_id=None,
                check_in_date=target, stay_days=3,
            )
            calendar = ReservaPricingService._find_calendar_discount(
                source_id=src.id, category_id=None,
                check_in_date=target,
            )

            assert advance is not None, "Early Bird deveria qualificar"
            assert calendar is not None, "Calendar discount deveria qualificar"
            assert calendar["percent"] == 35.0
            assert calendar["name"] == "Black Friday"


# ---------------------------------------------------------------------------
# Testes de rota — CRUD de CalendarDiscount
# ---------------------------------------------------------------------------


class TestCalendarDiscountRoutes:
    """Testa as rotas HTTP de criação e remoção de descontos de calendário."""

    def _login(self, client, app, db):
        """Autentica super_admin."""
        from app.models.funcionario import Funcionario
        from app.models.role import Role

        with app.app_context():
            role = Role(nome="Admin", descricao="Teste", is_super_admin=True, ativo=True)
            db.session.add(role)
            db.session.flush()
            func = Funcionario(nome="Admin", cpf="11122233366", role_id=role.id, ativo=True)
            db.session.add(func)
            db.session.commit()
            func_id = func.id

        with client.session_transaction() as sess:
            sess["funcionario_id"] = func_id
            sess["_fresh"] = True

    def test_criar_calendar_discount_persiste_e_audit(self, app, db, client):
        """POST criar deve salvar CalendarDiscount e gerar AuditLog."""
        with app.app_context():
            src = _make_source(db)
            db.session.commit()
            src_id = src.id

        self._login(client, app, db)

        target = date.today() + timedelta(days=20)
        resp = client.post(
            "/reservas/admin/config/ajustes-preco/calendar/criar",
            data={
                "source_id": str(src_id),
                "category_id": "",
                "discount_date": target.isoformat(),
                "label": "Black Friday",
                "is_active": "1",
                "open_year": str(target.year),
                "open_month": str(target.month),
            },
            follow_redirects=False,
        )

        assert resp.status_code in (301, 302)

        with app.app_context():
            cd = db.session.query(CalendarDiscount).filter_by(
                source_id=src_id, discount_date=target
            ).first()
            assert cd is not None
            assert cd.label == "Black Friday"
            assert cd.discount_percentage is None  # não mais usado

            audit = db.session.query(AuditLog).filter_by(
                module="calendar_discount", entity_id=cd.id
            ).first()
            assert audit is not None

    def test_criar_duplicado_nao_persiste(self, app, db, client):
        """Segunda criação para mesma fonte+data deve ser rejeitada sem duplicar."""
        with app.app_context():
            src = _make_source(db)
            target = date.today() + timedelta(days=20)
            _make_cd(db, src, discount_date=target)
            db.session.commit()
            src_id = src.id

        self._login(client, app, db)

        resp = client.post(
            "/reservas/admin/config/ajustes-preco/calendar/criar",
            data={
                "source_id": str(src_id),
                "category_id": "",
                "discount_date": target.isoformat(),
                "open_year": str(target.year),
                "open_month": str(target.month),
            },
            follow_redirects=False,
        )

        assert resp.status_code in (301, 302)

        with app.app_context():
            count = db.session.query(CalendarDiscount).filter_by(
                source_id=src_id, discount_date=target
            ).count()
            assert count == 1

    def test_deletar_calendar_discount_remove_e_audit(self, app, db, client):
        """POST deletar deve remover CalendarDiscount e gerar AuditLog."""
        with app.app_context():
            src = _make_source(db)
            target = date.today() + timedelta(days=20)
            cd = _make_cd(db, src, discount_date=target)
            db.session.commit()
            cd_id = cd.id

        self._login(client, app, db)

        resp = client.post(
            f"/reservas/admin/config/ajustes-preco/calendar/{cd_id}/deletar",
            follow_redirects=False,
        )

        assert resp.status_code in (301, 302)

        with app.app_context():
            assert db.session.get(CalendarDiscount, cd_id) is None
            audit = db.session.query(AuditLog).filter_by(
                module="calendar_discount", entity_id=cd_id
            ).first()
            assert audit is not None

    def test_api_json_retorna_apenas_descontos_do_mes(self, app, db, client):
        """GET calendar/api deve retornar apenas descontos do mês+ano solicitado."""
        with app.app_context():
            src = _make_source(db)
            in_month  = date(2027, 3, 15)
            out_month = date(2027, 4, 5)
            _make_cd(db, src, discount_date=in_month)
            _make_cd(db, src, discount_date=out_month)
            db.session.commit()
            src_id = src.id

        self._login(client, app, db)

        resp = client.get(
            f"/reservas/admin/config/ajustes-preco/calendar/api"
            f"?source_id={src_id}&year=2027&month=3"
        )

        assert resp.status_code == 200
        # Envelope {discounts, eventos} desde a separação dos calendários:
        # a camada de datas especiais (Calendário Global) divide a mesma grade.
        data = resp.get_json()["discounts"]
        assert len(data) == 1
        assert data[0]["date"] == "2027-03-15"
        assert "tiers" in data[0]
        assert "pct" not in data[0]  # não existe mais pct base no CalendarDiscount

    def test_api_json_tier_inclui_min_stay_days(self, app, db, client):
        """Tier com min_stay_days deve ser retornado no campo tiers da API JSON."""
        with app.app_context():
            src = _make_source(db)
            target = date(2027, 6, 10)
            cd = _make_cd(db, src, discount_date=target)
            _add_advance_tier(db, cd, min_days=15, pct=10.0, min_stay=None)
            _add_advance_tier(db, cd, min_days=15, pct=25.0, min_stay=3)
            db.session.commit()
            src_id = src.id

        self._login(client, app, db)

        resp = client.get(
            f"/reservas/admin/config/ajustes-preco/calendar/api"
            f"?source_id={src_id}&year=2027&month=6"
        )

        assert resp.status_code == 200
        tiers = resp.get_json()["discounts"][0]["tiers"]
        assert len(tiers) == 2
        stay_tiers = [t for t in tiers if t.get("min_stay_days") == 3]
        assert len(stay_tiers) == 1
        assert stay_tiers[0]["pct"] == 25.0

    def test_replicar_calendar_discount_detecta_conflitos_json(self, app, db, client):
        """POST replicar deve retornar JSON 409 quando destinos já têm regra."""
        with app.app_context():
            src = _make_source(db)
            source_date = date(2027, 7, 4)
            conflict_date = date(2027, 7, 5)
            cd = _make_cd(db, src, discount_date=source_date)
            _add_advance_tier(db, cd, min_days=15, pct=10.0, min_stay=1)
            _make_cd(db, src, discount_date=conflict_date)
            db.session.commit()
            cd_id = cd.id

        self._login(client, app, db)

        resp = client.post(
            f"/reservas/admin/config/ajustes-preco/calendar/{cd_id}/replicar",
            json={"target_dates": [conflict_date.isoformat()]},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        )

        assert resp.status_code == 409
        data = resp.get_json()
        assert data["ok"] is False
        assert data["conflitos"] == [conflict_date.isoformat()]

    def test_replicar_cria_descontos_e_copia_tiers(self, app, db, client):
        """Replicar para datas livres cria um CalendarDiscount por data com os tiers da origem.

        Caminho de escrita — os dois testes antigos de replicação paravam nos
        retornos antecipados (409 de conflito e 401 sem sessão) e nunca
        chegavam a executar a cópia de fato.
        """
        with app.app_context():
            src = _make_source(db)
            cd = _make_cd(db, src, discount_date=date(2027, 7, 4), label="Origem")
            _add_advance_tier(db, cd, min_days=15, pct=10.0, min_stay=None)
            _add_advance_tier(db, cd, min_days=15, pct=25.0, min_stay=3)
            db.session.commit()
            cd_id, src_id = cd.id, src.id

        self._login(client, app, db)

        resp = client.post(
            f"/reservas/admin/config/ajustes-preco/calendar/{cd_id}/replicar",
            json={"target_dates": ["2027-07-10", "2027-07-11"]},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        )

        assert resp.status_code == 200
        assert resp.get_json() == {
            "ok": True, "criados": 2, "substituidos": 0, "pulados": 0,
        }

        with app.app_context():
            for alvo in (date(2027, 7, 10), date(2027, 7, 11)):
                novo = db.session.query(CalendarDiscount).filter_by(
                    source_id=src_id, discount_date=alvo,
                ).one()
                assert novo.label == "Origem"
                assert novo.is_active is True
                copiados = {
                    (t.min_days_in_advance, t.min_stay_days, float(t.discount_percentage))
                    for t in novo.advance_tiers
                }
                assert copiados == {(15, None, 10.0), (15, 3, 25.0)}

    def test_replicar_overwrite_substitui_tiers_do_destino(self, app, db, client):
        """on_conflict=overwrite troca os tiers do destino pelos da origem."""
        with app.app_context():
            src = _make_source(db)
            cd = _make_cd(db, src, discount_date=date(2027, 7, 4))
            _add_advance_tier(db, cd, min_days=15, pct=10.0, min_stay=None)

            alvo = _make_cd(db, src, discount_date=date(2027, 7, 20))
            _add_advance_tier(db, alvo, min_days=5, pct=99.0, min_stay=None)
            db.session.commit()
            cd_id, alvo_id = cd.id, alvo.id

        self._login(client, app, db)

        resp = client.post(
            f"/reservas/admin/config/ajustes-preco/calendar/{cd_id}/replicar",
            json={"target_dates": ["2027-07-20"], "on_conflict": "overwrite"},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        )

        assert resp.status_code == 200
        assert resp.get_json()["substituidos"] == 1

        with app.app_context():
            tiers = db.session.query(CalendarDiscountAdvanceTier).filter_by(
                calendar_discount_id=alvo_id,
            ).all()
            assert len(tiers) == 1
            assert tiers[0].min_days_in_advance == 15
            assert float(tiers[0].discount_percentage) == 10.0

    def test_replicar_skip_preserva_destino_e_cria_os_livres(self, app, db, client):
        """on_conflict=skip: data em conflito fica intacta, data livre é criada."""
        with app.app_context():
            src = _make_source(db)
            cd = _make_cd(db, src, discount_date=date(2027, 7, 4))
            _add_advance_tier(db, cd, min_days=15, pct=10.0, min_stay=None)

            alvo = _make_cd(db, src, discount_date=date(2027, 7, 20))
            _add_advance_tier(db, alvo, min_days=5, pct=99.0, min_stay=None)
            db.session.commit()
            cd_id, alvo_id, src_id = cd.id, alvo.id, src.id

        self._login(client, app, db)

        resp = client.post(
            f"/reservas/admin/config/ajustes-preco/calendar/{cd_id}/replicar",
            json={"target_dates": ["2027-07-20", "2027-07-21"], "on_conflict": "skip"},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        )

        assert resp.status_code == 200
        assert resp.get_json()["pulados"] == 1
        assert resp.get_json()["criados"] == 1

        with app.app_context():
            # Conflito preservado, com o tier original de 99%
            intacto = db.session.query(CalendarDiscountAdvanceTier).filter_by(
                calendar_discount_id=alvo_id,
            ).all()
            assert len(intacto) == 1
            assert float(intacto[0].discount_percentage) == 99.0

            # Data livre do mesmo lote foi criada com o tier da origem
            novo = db.session.query(CalendarDiscount).filter_by(
                source_id=src_id, discount_date=date(2027, 7, 21),
            ).one()
            assert len(novo.advance_tiers) == 1
            assert float(novo.advance_tiers[0].discount_percentage) == 10.0

    def test_replicar_gera_audit_log(self, app, db, client):
        """Replicação é mutação: precisa deixar AuditLog."""
        with app.app_context():
            src = _make_source(db)
            cd = _make_cd(db, src, discount_date=date(2027, 7, 4))
            _add_advance_tier(db, cd, min_days=15, pct=10.0, min_stay=None)
            db.session.commit()
            cd_id = cd.id

        self._login(client, app, db)

        client.post(
            f"/reservas/admin/config/ajustes-preco/calendar/{cd_id}/replicar",
            json={"target_dates": ["2027-07-10"]},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        )

        with app.app_context():
            audit = db.session.query(AuditLog).filter_by(
                module="calendar_discount", entity_id=cd_id,
            ).first()
            assert audit is not None
            assert audit.new_state["criados"] == 1
            assert audit.new_state["target_dates"] == ["2027-07-10"]

    def test_replicar_data_invalida_retorna_400(self, app, db, client):
        """Data fora do formato ISO deve ser rejeitada antes de qualquer escrita."""
        with app.app_context():
            src = _make_source(db)
            cd = _make_cd(db, src, discount_date=date(2027, 7, 4))
            db.session.commit()
            cd_id = cd.id

        self._login(client, app, db)

        resp = client.post(
            f"/reservas/admin/config/ajustes-preco/calendar/{cd_id}/replicar",
            json={"target_dates": ["04/07/2027"]},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        )

        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_replicar_calendar_discount_sem_sessao_retorna_json(self, app, db, client):
        """POST replicar via AJAX sem sessão deve retornar JSON em vez de HTML."""
        with app.app_context():
            src = _make_source(db)
            cd = _make_cd(db, src, discount_date=date(2027, 8, 4))
            db.session.commit()
            cd_id = cd.id

        resp = client.post(
            f"/reservas/admin/config/ajustes-preco/calendar/{cd_id}/replicar",
            json={"target_dates": ["2027-08-05"]},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        )

        assert resp.status_code == 401
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error"] == "Sessão expirada. Faça login novamente."
