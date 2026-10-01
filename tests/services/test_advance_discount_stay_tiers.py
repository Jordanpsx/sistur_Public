# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes unitários para o sistema de desconto progressivo por dias de estadia
(AdvanceDiscountStayTier) integrado ao ReservaPricingService.

Cobertura:
  - _find_best_advance_discount: seleciona tier correto quando há vários tiers
  - _find_best_advance_discount: cai no desconto base quando nenhum tier qualifica
  - _find_best_advance_discount: sem tiers cadastrados usa desconto base
  - Criação de tier via rota: cria registro + dispara AuditLog
  - Remoção de tier via rota: remove registro + dispara AuditLog
  - Criação de tier duplicado (mesma regra + mesmo min_stay_days) é rejeitada
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.core.audit import AuditLog
from app.models.reservas import (
    AdvanceDiscountRule,
    AdvanceDiscountStayTier,
    ReservaCategory,
    ReservaSource,
)
from app.services.reserva_pricing_service import ReservaPricingService


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_source(db) -> ReservaSource:
    """Cria ReservaSource mínima para os testes."""
    src = ReservaSource(name=f"fonte-{uuid.uuid4().hex[:6]}", display_name="Teste")
    db.session.add(src)
    db.session.flush()
    return src


def _make_rule(db, source: ReservaSource, *, min_days: int = 30, pct: float = 10.0) -> AdvanceDiscountRule:
    """Cria AdvanceDiscountRule ativa sem tiers."""
    rule = AdvanceDiscountRule(
        source_id=source.id,
        category_id=None,
        min_days_in_advance=min_days,
        discount_percentage=Decimal(str(pct)),
        is_active=True,
    )
    db.session.add(rule)
    db.session.flush()
    return rule


def _add_tier(db, rule: AdvanceDiscountRule, *, min_stay: int, pct: float) -> AdvanceDiscountStayTier:
    """Adiciona um AdvanceDiscountStayTier a uma regra."""
    tier = AdvanceDiscountStayTier(
        rule_id=rule.id,
        min_stay_days=min_stay,
        discount_percentage=Decimal(str(pct)),
    )
    db.session.add(tier)
    db.session.flush()
    return tier


# ---------------------------------------------------------------------------
# Testes de serviço — _find_best_advance_discount com stay_days
# ---------------------------------------------------------------------------


class TestFindBestAdvanceDiscountStayTiers:
    """Testa a lógica de seleção de tier de estadia dentro de _find_best_advance_discount."""

    def test_sem_tiers_usa_desconto_base(self, app, db):
        """Regra sem tiers: retorna discount_percentage base da regra."""
        with app.app_context():
            src = _make_source(db)
            rule = _make_rule(db, src, min_days=30, pct=10.0)
            db.session.commit()

            check_in = date.today() + timedelta(days=45)
            result = ReservaPricingService._find_best_advance_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=check_in,
                stay_days=5,
            )

            assert result is not None
            assert result["percent"] == 10.0
            assert result["stay_tier_id"] is None
            assert result["stay_tier_min_days"] is None

    def test_tier_aplica_quando_estadia_qualifica(self, app, db):
        """Quando stay_days >= min_stay_days do tier, usa o percentual do tier."""
        with app.app_context():
            src = _make_source(db)
            rule = _make_rule(db, src, min_days=30, pct=10.0)
            tier = _add_tier(db, rule, min_stay=3, pct=15.0)
            db.session.commit()

            check_in = date.today() + timedelta(days=45)
            result = ReservaPricingService._find_best_advance_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=check_in,
                stay_days=5,
            )

            assert result is not None
            assert result["percent"] == 15.0
            assert result["stay_tier_id"] == tier.id
            assert result["stay_tier_min_days"] == 3

    def test_seleciona_tier_mais_alto_aplicavel(self, app, db):
        """Com múltiplos tiers, seleciona o de maior min_stay_days que ainda qualifica."""
        with app.app_context():
            src = _make_source(db)
            rule = _make_rule(db, src, min_days=30, pct=10.0)
            _add_tier(db, rule, min_stay=3, pct=12.0)
            tier7 = _add_tier(db, rule, min_stay=7, pct=18.0)
            _add_tier(db, rule, min_stay=14, pct=25.0)  # não qualifica (estadia=10)
            db.session.commit()

            check_in = date.today() + timedelta(days=45)
            result = ReservaPricingService._find_best_advance_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=check_in,
                stay_days=10,
            )

            assert result is not None
            assert result["percent"] == 18.0
            assert result["stay_tier_id"] == tier7.id

    def test_tier_nao_aplica_quando_estadia_curta(self, app, db):
        """Quando stay_days < todos os min_stay_days, cai no desconto base."""
        with app.app_context():
            src = _make_source(db)
            rule = _make_rule(db, src, min_days=30, pct=10.0)
            _add_tier(db, rule, min_stay=5, pct=20.0)
            db.session.commit()

            check_in = date.today() + timedelta(days=45)
            result = ReservaPricingService._find_best_advance_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=check_in,
                stay_days=2,  # menor que o mínimo do tier (5)
            )

            assert result is not None
            assert result["percent"] == 10.0
            assert result["stay_tier_id"] is None

    def test_stay_days_zero_usa_desconto_base(self, app, db):
        """stay_days=0 (check-in == check-out) nunca aplica tier."""
        with app.app_context():
            src = _make_source(db)
            rule = _make_rule(db, src, min_days=30, pct=10.0)
            _add_tier(db, rule, min_stay=1, pct=15.0)
            db.session.commit()

            check_in = date.today() + timedelta(days=45)
            result = ReservaPricingService._find_best_advance_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=check_in,
                stay_days=0,
            )

            assert result is not None
            assert result["percent"] == 10.0
            assert result["stay_tier_id"] is None

    def test_retorna_none_quando_regra_inativa(self, app, db):
        """Regra inativa não deve ser selecionada, mesmo com tier aplicável."""
        with app.app_context():
            src = _make_source(db)
            rule = _make_rule(db, src, min_days=30, pct=10.0)
            rule.is_active = False
            _add_tier(db, rule, min_stay=3, pct=20.0)
            db.session.commit()

            check_in = date.today() + timedelta(days=45)
            result = ReservaPricingService._find_best_advance_discount(
                source_id=src.id,
                category_id=None,
                check_in_date=check_in,
                stay_days=5,
            )

            assert result is None


# ---------------------------------------------------------------------------
# Testes de rota — CRUD de stay tiers
# ---------------------------------------------------------------------------


class TestStayTierRoutes:
    """Testa as rotas de criação e remoção de stay tiers."""

    def _login(self, client, app, db):
        """Autentica um funcionário super_admin (tem todas as permissões)."""
        from app.models.funcionario import Funcionario
        from app.models.role import Role

        with app.app_context():
            role = Role(nome="Admin Teste", descricao="Teste", is_super_admin=True, ativo=True)
            db.session.add(role)
            db.session.flush()

            func = Funcionario(
                nome="Admin",
                cpf="11122233344",
                role_id=role.id,
                ativo=True,
            )
            db.session.add(func)
            db.session.commit()
            func_id = func.id

        with client.session_transaction() as sess:
            sess["funcionario_id"] = func_id
            sess["_fresh"] = True

        return func_id

    def test_criar_tier_grava_banco_e_dispara_audit(self, app, db, client):
        """POST /tiers/criar deve persistir o tier e gerar AuditLog de criação."""
        with app.app_context():
            src = _make_source(db)
            rule = _make_rule(db, src)
            db.session.commit()
            rule_id = rule.id

        self._login(client, app, db)

        resp = client.post(
            f"/reservas/admin/config/ajustes-preco/{rule_id}/tiers/criar",
            data={"min_stay_days": "4", "discount_percentage": "20.00"},
            follow_redirects=False,
        )

        assert resp.status_code in (301, 302)

        with app.app_context():
            tier = db.session.query(AdvanceDiscountStayTier).filter_by(rule_id=rule_id).first()
            assert tier is not None
            assert tier.min_stay_days == 4
            assert float(tier.discount_percentage) == 20.0

            audit = db.session.query(AuditLog).filter_by(
                module="desconto_antecipado_stay_tier",
                entity_id=tier.id,
            ).first()
            assert audit is not None

    def test_criar_tier_duplicado_retorna_erro(self, app, db, client):
        """Criar tier com mesmo min_stay_days para a mesma regra deve redirecionar sem criar duplicata."""
        with app.app_context():
            src = _make_source(db)
            rule = _make_rule(db, src)
            _add_tier(db, rule, min_stay=4, pct=20.0)
            db.session.commit()
            rule_id = rule.id

        self._login(client, app, db)

        resp = client.post(
            f"/reservas/admin/config/ajustes-preco/{rule_id}/tiers/criar",
            data={"min_stay_days": "4", "discount_percentage": "25.00"},
            follow_redirects=False,
        )

        # Deve redirecionar (não 200/400 inline) sem ter criado o tier duplicado
        assert resp.status_code in (301, 302)

        with app.app_context():
            count = db.session.query(AdvanceDiscountStayTier).filter_by(
                rule_id=rule_id, min_stay_days=4
            ).count()
            assert count == 1  # não criou duplicata

    def test_deletar_tier_remove_do_banco_e_dispara_audit(self, app, db, client):
        """POST /tiers/<id>/deletar deve remover o tier e gerar AuditLog de deleção."""
        with app.app_context():
            src = _make_source(db)
            rule = _make_rule(db, src)
            tier = _add_tier(db, rule, min_stay=3, pct=15.0)
            db.session.commit()
            rule_id = rule.id
            tier_id = tier.id

        self._login(client, app, db)

        resp = client.post(
            f"/reservas/admin/config/ajustes-preco/{rule_id}/tiers/{tier_id}/deletar",
            follow_redirects=False,
        )

        assert resp.status_code in (301, 302)

        with app.app_context():
            assert db.session.get(AdvanceDiscountStayTier, tier_id) is None

            audit = db.session.query(AuditLog).filter_by(
                module="desconto_antecipado_stay_tier",
                entity_id=tier_id,
            ).first()
            assert audit is not None

    def test_deletar_tier_de_regra_errada_retorna_erro(self, app, db, client):
        """Tentativa de deletar tier com rule_id incorreto não deve remover o tier."""
        with app.app_context():
            src = _make_source(db)
            rule1 = _make_rule(db, src)
            rule2 = _make_rule(db, src, min_days=60)
            tier = _add_tier(db, rule1, min_stay=3, pct=15.0)
            db.session.commit()
            tier_id = tier.id
            rule2_id = rule2.id

        self._login(client, app, db)

        # Tenta deletar o tier da rule1 usando a URL da rule2
        resp = client.post(
            f"/reservas/admin/config/ajustes-preco/{rule2_id}/tiers/{tier_id}/deletar",
            follow_redirects=False,
        )

        # Deve redirecionar sem remover o tier
        assert resp.status_code in (301, 302)

        with app.app_context():
            # Tier original deve continuar existindo
            assert db.session.get(AdvanceDiscountStayTier, tier_id) is not None
