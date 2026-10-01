# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes do ReservaConfigService — taxa de serviço e desconto linear por fonte.

Cobre a herança do valor global, a remoção de override por campo vazio,
a validação de percentual e a auditoria obrigatória.
"""

from __future__ import annotations

import pytest

from app.core.models import AuditAction, AuditLog
from app.models.reservas import ReservaConfig, ReservaSource
from app.services.reserva_config_service import (
    CHAVE_DESCONTO_LINEAR,
    CHAVE_TAXA_SERVICO,
    ReservaConfigService,
)


def _criar_fonte(db, nome="Cachoeira") -> ReservaSource:
    fonte = ReservaSource(name=nome)
    db.session.add(fonte)
    db.session.commit()
    return fonte


def _config_global(db, chave, valor):
    db.session.add(ReservaConfig(source_id=None, config_key=chave, config_value=valor))
    db.session.commit()


class TestObterPricingConfig:

    def test_sem_override_herda_do_global(self, app, db):
        with app.app_context():
            fonte = _criar_fonte(db)
            _config_global(db, CHAVE_TAXA_SERVICO, 8.1)

            cfg = ReservaConfigService.obter_pricing_config(fonte.id)

            assert cfg[CHAVE_TAXA_SERVICO]["valor"] == 8.1
            assert cfg[CHAVE_TAXA_SERVICO]["proprio"] is False
            assert cfg[CHAVE_TAXA_SERVICO]["global"] == 8.1

    def test_override_da_fonte_prevalece(self, app, db):
        with app.app_context():
            fonte = _criar_fonte(db)
            _config_global(db, CHAVE_TAXA_SERVICO, 8.1)
            ReservaConfigService.salvar_pricing_config(
                source_id=fonte.id, service_fee_percent="12.5", ator_id=1)

            cfg = ReservaConfigService.obter_pricing_config(fonte.id)

            assert cfg[CHAVE_TAXA_SERVICO]["valor"] == 12.5
            assert cfg[CHAVE_TAXA_SERVICO]["proprio"] is True
            assert cfg[CHAVE_TAXA_SERVICO]["global"] == 8.1

    def test_sem_nenhuma_config_devolve_none(self, app, db):
        with app.app_context():
            fonte = _criar_fonte(db)
            cfg = ReservaConfigService.obter_pricing_config(fonte.id)
            assert cfg[CHAVE_DESCONTO_LINEAR]["valor"] is None


class TestSalvarPricingConfig:

    def test_happy_path_persiste(self, app, db):
        with app.app_context():
            fonte = _criar_fonte(db)

            ReservaConfigService.salvar_pricing_config(
                source_id=fonte.id,
                service_fee_percent="10",
                flat_discount_percent="5",
                ator_id=1,
            )

            cfg = ReservaConfigService.obter_pricing_config(fonte.id)
            assert cfg[CHAVE_TAXA_SERVICO]["valor"] == 10.0
            assert cfg[CHAVE_DESCONTO_LINEAR]["valor"] == 5.0

    def test_audit_log_criado(self, app, db):
        with app.app_context():
            fonte = _criar_fonte(db)
            ReservaConfigService.salvar_pricing_config(
                source_id=fonte.id, service_fee_percent="10", ator_id=1)

            count = (
                db.session.query(AuditLog)
                .filter_by(action=AuditAction.update, entity_id=fonte.id,
                           module="reserva_config")
                .count()
            )
            assert count == 1

    def test_campo_vazio_remove_override(self, app, db):
        """Limpar o campo devolve a fonte à herança do valor global."""
        with app.app_context():
            fonte = _criar_fonte(db)
            _config_global(db, CHAVE_TAXA_SERVICO, 8.1)
            ReservaConfigService.salvar_pricing_config(
                source_id=fonte.id, service_fee_percent="12.5", ator_id=1)

            ReservaConfigService.salvar_pricing_config(
                source_id=fonte.id, service_fee_percent="", ator_id=1)

            cfg = ReservaConfigService.obter_pricing_config(fonte.id)
            assert cfg[CHAVE_TAXA_SERVICO]["proprio"] is False
            assert cfg[CHAVE_TAXA_SERVICO]["valor"] == 8.1

    def test_aceita_virgula_decimal(self, app, db):
        """Operador brasileiro digita 8,5 — não deve virar erro."""
        with app.app_context():
            fonte = _criar_fonte(db)
            ReservaConfigService.salvar_pricing_config(
                source_id=fonte.id, service_fee_percent="8,5", ator_id=1)

            cfg = ReservaConfigService.obter_pricing_config(fonte.id)
            assert cfg[CHAVE_TAXA_SERVICO]["valor"] == 8.5

    def test_percentual_nao_numerico_falha(self, app, db):
        with app.app_context():
            fonte = _criar_fonte(db)
            with pytest.raises(ValueError, match="número válido"):
                ReservaConfigService.salvar_pricing_config(
                    source_id=fonte.id, service_fee_percent="abc", ator_id=1)

    def test_percentual_fora_do_intervalo_falha(self, app, db):
        with app.app_context():
            fonte = _criar_fonte(db)
            with pytest.raises(ValueError, match="entre 0 e 100"):
                ReservaConfigService.salvar_pricing_config(
                    source_id=fonte.id, flat_discount_percent="150", ator_id=1)

    def test_fonte_inexistente_falha(self, app, db):
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrada"):
                ReservaConfigService.salvar_pricing_config(
                    source_id=99999, service_fee_percent="10", ator_id=1)


class TestFormatoQueOPricingLe:
    """Regressão: a tela precisa gravar no formato que o cálculo já lê.

    `service_fee_percent` é chave de topo, mas `flat_discount_percent` mora
    ANINHADO dentro do JSON `pricing_rules`. Gravar como chave de topo faria a
    tela salvar com sucesso e o desconto nunca aparecer no preço.
    """

    def test_desconto_linear_gravado_dentro_de_pricing_rules(self, app, db):
        with app.app_context():
            fonte = _criar_fonte(db)
            ReservaConfigService.salvar_pricing_config(
                source_id=fonte.id, flat_discount_percent="5", ator_id=1)

            linha = (
                db.session.query(ReservaConfig)
                .filter_by(source_id=fonte.id, config_key="pricing_rules")
                .one()
            )
            assert linha.config_value["flat_discount_percent"] == 5.0

            # E NÃO como chave de topo
            assert (
                db.session.query(ReservaConfig)
                .filter_by(source_id=fonte.id, config_key="flat_discount_percent")
                .count() == 0
            )

    def test_valores_salvos_chegam_no_calculo(self, app, db):
        """Ponta a ponta: salvar na tela muda o total calculado."""
        with app.app_context():
            from datetime import date
            from app.models.reservas import (
                BillingType, ReservaCategory, ReservaItem,
            )
            from app.services.reserva_pricing_service import ReservaPricingService

            fonte = _criar_fonte(db)
            cat = ReservaCategory(name="Day Use", source_id=fonte.id)
            db.session.add(cat)
            db.session.flush()
            item = ReservaItem(
                name="Inteira", billing_type=BillingType.FIXED, price=100.0,
                requires_deposit=False, category_id=cat.id,
            )
            db.session.add(item)
            db.session.commit()

            ReservaConfigService.salvar_pricing_config(
                source_id=fonte.id,
                service_fee_percent="10",
                flat_discount_percent="20",
                ator_id=1,
            )

            r = ReservaPricingService.calculate(
                source_id=fonte.id,
                check_in_date=date(2027, 5, 10),
                check_out_date=date(2027, 5, 11),
                items=[{"item_id": item.id, "quantity": 1}],
            )

            # 100 − 20% = 80 ; taxa 10% sobre 80 = 8 ; total 88
            assert r["subtotal"] == 100.0
            assert r["discount_amount"] == 20.0
            assert r["service_fee"] == 8.0
            assert r["total"] == 88.0

    def test_preserva_early_bird_legado_no_json(self, app, db):
        """Salvar o desconto não pode apagar outras chaves de pricing_rules."""
        with app.app_context():
            fonte = _criar_fonte(db)
            db.session.add(ReservaConfig(
                source_id=fonte.id, config_key="pricing_rules",
                config_value={"early_bird_enabled": True, "early_bird_days": 30},
            ))
            db.session.commit()

            ReservaConfigService.salvar_pricing_config(
                source_id=fonte.id, flat_discount_percent="7", ator_id=1)

            regras = (
                db.session.query(ReservaConfig)
                .filter_by(source_id=fonte.id, config_key="pricing_rules")
                .one().config_value
            )
            assert regras["early_bird_enabled"] is True
            assert regras["early_bird_days"] == 30
            assert regras["flat_discount_percent"] == 7.0

    def test_limpar_desconto_remove_linha_vazia(self, app, db):
        """Sem outras chaves, o JSON vazio não fica de lixo no banco."""
        with app.app_context():
            fonte = _criar_fonte(db)
            ReservaConfigService.salvar_pricing_config(
                source_id=fonte.id, flat_discount_percent="7", ator_id=1)
            ReservaConfigService.salvar_pricing_config(
                source_id=fonte.id, flat_discount_percent="", ator_id=1)

            assert (
                db.session.query(ReservaConfig)
                .filter_by(source_id=fonte.id, config_key="pricing_rules")
                .count() == 0
            )
