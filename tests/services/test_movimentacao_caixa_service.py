# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes para o MovimentacaoCaixaService (sangria/suprimento do PDV).

Cobertura mínima por método de mutação (skills/test-service.md):
happy path, AuditLog criado, input inválido, entidade não encontrada,
mais os invariantes específicos: separação sangria/suprimento por tipo,
centro_de_custo_id obrigatório em PAGAMENTO_DESPESA, conta_destino_id
obrigatório em TRANSFERENCIA_INTERNA, exigência de autorizado_por_id acima
do limite (via autorizado_por_id direto OU manager_override), turno precisa
estar OPEN para registrar movimentação, e a integração de
PdvService._esperados_fechamento/fechar_turno com sangria/suprimento.
"""

from decimal import Decimal

import pytest

from app.core.models import Area, AuditAction, AuditLog
from app.models.funcionario import Funcionario
from app.models.pdv import PdvMovimentacaoCaixa, TipoMovimentacaoCaixa
from app.models.reservas import (
    ReservaCategory,
    ReservaItem,
    ReservaSource,
)
from app.services.movimentacao_caixa_service import (
    LIMITE_EXIGE_AUTORIZACAO,
    MovimentacaoCaixaService,
)
from app.services.pdv_service import PdvService


@pytest.fixture
def catalogo_pdv(db):
    """Venue mínimo com um item FIXED em dinheiro (Lenha)."""
    source = ReservaSource(name="Cachoeira")
    cat_loja = ReservaCategory(name="Loja", source=source)
    lenha = ReservaItem(
        name="Lenha (fardo)",
        billing_type="FIXED",
        price=30.00,
        requires_deposit=False,
        category=cat_loja,
    )
    db.session.add_all([source, cat_loja, lenha])
    db.session.commit()
    return {"source": source, "lenha": lenha}


@pytest.fixture
def turno(db, catalogo_pdv):
    """Turno de caixa aberto, fundo de troco R$ 100."""
    return PdvService.abrir_turno(
        source_id=catalogo_pdv["source"].id,
        operador_id=1,
        opening_float_amount=100.0,
        ator_id=1,
    )


@pytest.fixture
def operador(db):
    """Funcionário que executa a movimentação de caixa."""
    f = Funcionario(nome="Operador Caixa", cpf="52998224725", ativo=True)
    db.session.add(f)
    db.session.commit()
    return f


@pytest.fixture
def gerente(db):
    """Funcionário que autoriza movimentações acima do limite."""
    f = Funcionario(nome="Gerente Financeiro", cpf="11144477735", ativo=True)
    db.session.add(f)
    db.session.commit()
    return f


@pytest.fixture
def area_administrativo(db):
    """Centro de custo para despesas administrativas."""
    area = Area(name="Administrativo", slug="administrativo")
    db.session.add(area)
    db.session.commit()
    return area


def _gerente_pdv(db, *, cpf="12345678909", senha="senha-segura"):
    """Funcionário com permissão pdv.manage_shift e senha — para manager_override."""
    from werkzeug.security import generate_password_hash

    from app.models.role import Role, RolePermission

    role = Role(nome=f"Gerente PDV {cpf}", is_super_admin=False, ativo=True)
    db.session.add(role)
    db.session.flush()
    db.session.add(RolePermission(role_id=role.id, modulo="pdv", acao="manage_shift"))
    gerente = Funcionario(
        nome="Gerente PDV",
        cpf=cpf,
        ativo=True,
        role_id=role.id,
        senha_hash=generate_password_hash(senha),
    )
    db.session.add(gerente)
    db.session.commit()
    return gerente, senha


def _venda_cash(catalogo_pdv, turno, valor=30.0, quantity=1):
    """Registra uma venda CASH para alimentar as vendas do turno."""
    import uuid

    PdvService.registrar_venda(
        client_sale_uuid=str(uuid.uuid4()),
        shift_id=turno.id,
        source_name="Cachoeira",
        category_name="Loja",
        items=[{"item_id": catalogo_pdv["lenha"].id, "quantity": quantity}],
        payment_method="CASH",
        customer_name="Cliente Teste",
        amount_tendered=valor,
        ator_id=1,
    )


class TestRegistrarSangria:
    def test_happy_path_recolhimento_cofre(self, db, turno, operador):
        mov = MovimentacaoCaixaService.registrar_sangria(
            caixa_turno_id=turno.id,
            tipo_movimentacao=TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
            valor=Decimal("50.00"),
            funcionario_id=operador.id,
            descricao="Recolhimento para o cofre da administração",
            ator_id=operador.id,
        )
        assert mov.id is not None
        assert mov.tipo_movimentacao == TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE
        assert mov.valor == Decimal("50.00")
        assert mov.centro_de_custo_id is None
        assert mov.conta_destino_id is None

    def test_happy_path_pagamento_despesa(self, db, turno, operador, area_administrativo):
        mov = MovimentacaoCaixaService.registrar_sangria(
            caixa_turno_id=turno.id,
            tipo_movimentacao=TipoMovimentacaoCaixa.PAGAMENTO_DESPESA,
            valor=Decimal("40.00"),
            funcionario_id=operador.id,
            descricao="Pagamento de frete de fornecedor",
            centro_de_custo_id=area_administrativo.id,
            ator_id=operador.id,
        )
        assert mov.centro_de_custo_id == area_administrativo.id

    def test_happy_path_transferencia_interna(self, db, turno, operador):
        outro_turno = PdvService.abrir_turno(
            source_id=turno.source_id, operador_id=2, ator_id=2
        )
        mov = MovimentacaoCaixaService.registrar_sangria(
            caixa_turno_id=turno.id,
            tipo_movimentacao=TipoMovimentacaoCaixa.TRANSFERENCIA_INTERNA,
            valor=Decimal("20.00"),
            funcionario_id=operador.id,
            descricao="Transferência para caixa da Portaria",
            conta_destino_id=outro_turno.id,
            ator_id=operador.id,
        )
        assert mov.conta_destino_id == outro_turno.id

    def test_audit_log_created(self, db, turno, operador):
        mov = MovimentacaoCaixaService.registrar_sangria(
            caixa_turno_id=turno.id,
            tipo_movimentacao=TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
            valor=Decimal("50.00"),
            funcionario_id=operador.id,
            descricao="Recolhimento",
            ator_id=operador.id,
        )
        count = db.session.query(AuditLog).filter_by(
            action=AuditAction.create,
            module="pdv_movimentacao_caixa",
            entity_id=mov.id,
        ).count()
        assert count == 1

    def test_suprimento_rejeitado(self, db, turno, operador):
        with pytest.raises(ValueError, match="registrar_suprimento"):
            MovimentacaoCaixaService.registrar_sangria(
                caixa_turno_id=turno.id,
                tipo_movimentacao=TipoMovimentacaoCaixa.SUPRIMENTO,
                valor=Decimal("50.00"),
                funcionario_id=operador.id,
                descricao="Não deveria funcionar",
                ator_id=operador.id,
            )

    def test_pagamento_despesa_exige_centro_de_custo(self, db, turno, operador):
        with pytest.raises(ValueError, match="centro_de_custo_id"):
            MovimentacaoCaixaService.registrar_sangria(
                caixa_turno_id=turno.id,
                tipo_movimentacao=TipoMovimentacaoCaixa.PAGAMENTO_DESPESA,
                valor=Decimal("40.00"),
                funcionario_id=operador.id,
                descricao="Sem centro de custo",
                ator_id=operador.id,
            )

    def test_transferencia_interna_exige_conta_destino(self, db, turno, operador):
        with pytest.raises(ValueError, match="conta_destino_id"):
            MovimentacaoCaixaService.registrar_sangria(
                caixa_turno_id=turno.id,
                tipo_movimentacao=TipoMovimentacaoCaixa.TRANSFERENCIA_INTERNA,
                valor=Decimal("20.00"),
                funcionario_id=operador.id,
                descricao="Sem conta destino",
                ator_id=operador.id,
            )

    def test_valor_invalido(self, db, turno, operador):
        with pytest.raises(ValueError, match="positivo"):
            MovimentacaoCaixaService.registrar_sangria(
                caixa_turno_id=turno.id,
                tipo_movimentacao=TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
                valor=Decimal("0.00"),
                funcionario_id=operador.id,
                descricao="Valor zero",
                ator_id=operador.id,
            )

    def test_turno_fechado(self, db, catalogo_pdv, turno, operador):
        PdvService.fechar_turno(
            shift_id=turno.id,
            closing_counted_amount=100.0,
            closing_counted_pix=0.0,
            ator_id=1,
        )
        with pytest.raises(ValueError, match="fechado"):
            MovimentacaoCaixaService.registrar_sangria(
                caixa_turno_id=turno.id,
                tipo_movimentacao=TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
                valor=Decimal("50.00"),
                funcionario_id=operador.id,
                descricao="Turno já fechado",
                ator_id=operador.id,
            )

    def test_turno_nao_encontrado(self, db, operador):
        with pytest.raises(ValueError, match="não encontrado"):
            MovimentacaoCaixaService.registrar_sangria(
                caixa_turno_id=9999,
                tipo_movimentacao=TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
                valor=Decimal("50.00"),
                funcionario_id=operador.id,
                descricao="Turno inexistente",
                ator_id=operador.id,
            )

    def test_funcionario_nao_encontrado(self, db, turno):
        with pytest.raises(ValueError, match="não encontrado"):
            MovimentacaoCaixaService.registrar_sangria(
                caixa_turno_id=turno.id,
                tipo_movimentacao=TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
                valor=Decimal("50.00"),
                funcionario_id=9999,
                descricao="Funcionário inexistente",
            )

    def test_valor_alto_exige_autorizacao(self, db, turno, operador):
        with pytest.raises(ValueError, match="autorizado_por_id"):
            MovimentacaoCaixaService.registrar_sangria(
                caixa_turno_id=turno.id,
                tipo_movimentacao=TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
                valor=LIMITE_EXIGE_AUTORIZACAO,
                funcionario_id=operador.id,
                descricao="Valor alto sem autorização",
                ator_id=operador.id,
            )

    def test_valor_alto_com_autorizacao_funciona(self, db, turno, operador, gerente):
        mov = MovimentacaoCaixaService.registrar_sangria(
            caixa_turno_id=turno.id,
            tipo_movimentacao=TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
            valor=LIMITE_EXIGE_AUTORIZACAO,
            funcionario_id=operador.id,
            descricao="Valor alto com autorização",
            autorizado_por_id=gerente.id,
            ator_id=operador.id,
        )
        assert mov.autorizado_por_id == gerente.id

    def test_valor_alto_com_manager_override_funciona(self, db, turno, operador):
        gerente, senha = _gerente_pdv(db, cpf="12345678909")
        override = PdvService.autorizar_manager_override(
            cpf=gerente.cpf, senha=senha, reason="Confirmado presencialmente",
        )
        mov = MovimentacaoCaixaService.registrar_sangria(
            caixa_turno_id=turno.id,
            tipo_movimentacao=TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
            valor=LIMITE_EXIGE_AUTORIZACAO,
            funcionario_id=operador.id,
            descricao="Valor alto via override do gerente",
            manager_override=override,
            ator_id=operador.id,
        )
        assert mov.autorizado_por_id == gerente.id

    def test_manager_override_sem_permissao_gerencial_falha(self, db, turno, operador):
        from app.models.role import Role

        role = Role(nome="Sem permissão PDV", is_super_admin=False, ativo=True)
        db.session.add(role)
        db.session.flush()
        sem_permissao = Funcionario(
            nome="Sem Permissão", cpf="12345678909", ativo=True, role_id=role.id,
        )
        db.session.add(sem_permissao)
        db.session.commit()
        with pytest.raises(ValueError, match="permissão gerencial"):
            MovimentacaoCaixaService.registrar_sangria(
                caixa_turno_id=turno.id,
                tipo_movimentacao=TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
                valor=LIMITE_EXIGE_AUTORIZACAO,
                funcionario_id=operador.id,
                descricao="Override inválido",
                manager_override={"manager_id": sem_permissao.id, "reason": "Motivo qualquer aqui"},
                ator_id=operador.id,
            )


class TestRegistrarSuprimento:
    def test_happy_path(self, db, turno, operador):
        mov = MovimentacaoCaixaService.registrar_suprimento(
            caixa_turno_id=turno.id,
            valor=Decimal("30.00"),
            funcionario_id=operador.id,
            descricao="Reforço de troco",
            ator_id=operador.id,
        )
        assert mov.tipo_movimentacao == TipoMovimentacaoCaixa.SUPRIMENTO
        assert mov.valor == Decimal("30.00")

    def test_audit_log_created(self, db, turno, operador):
        mov = MovimentacaoCaixaService.registrar_suprimento(
            caixa_turno_id=turno.id,
            valor=Decimal("30.00"),
            funcionario_id=operador.id,
            descricao="Reforço de troco",
            ator_id=operador.id,
        )
        count = db.session.query(AuditLog).filter_by(
            action=AuditAction.create,
            module="pdv_movimentacao_caixa",
            entity_id=mov.id,
        ).count()
        assert count == 1

    def test_turno_fechado(self, db, turno, operador):
        PdvService.fechar_turno(
            shift_id=turno.id,
            closing_counted_amount=100.0,
            closing_counted_pix=0.0,
            ator_id=1,
        )
        with pytest.raises(ValueError, match="fechado"):
            MovimentacaoCaixaService.registrar_suprimento(
                caixa_turno_id=turno.id,
                valor=Decimal("30.00"),
                funcionario_id=operador.id,
                descricao="Turno já fechado",
                ator_id=operador.id,
            )


class TestCalcularSaldoEsperadoGaveta:
    def test_apenas_fundo_sem_movimentacoes(self, db, turno):
        saldo = MovimentacaoCaixaService.calcular_saldo_esperado_gaveta(turno.id)
        assert saldo == Decimal("100.00")

    def test_fundo_mais_vendas_cash(self, db, catalogo_pdv, turno):
        _venda_cash(catalogo_pdv, turno, valor=30.0, quantity=1)
        saldo = MovimentacaoCaixaService.calcular_saldo_esperado_gaveta(turno.id)
        assert saldo == Decimal("130.00")

    def test_sangria_subtrai_do_saldo(self, db, turno, operador):
        MovimentacaoCaixaService.registrar_sangria(
            caixa_turno_id=turno.id,
            tipo_movimentacao=TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
            valor=Decimal("40.00"),
            funcionario_id=operador.id,
            descricao="Recolhimento",
            ator_id=operador.id,
        )
        saldo = MovimentacaoCaixaService.calcular_saldo_esperado_gaveta(turno.id)
        assert saldo == Decimal("60.00")

    def test_suprimento_soma_ao_saldo(self, db, turno, operador):
        MovimentacaoCaixaService.registrar_suprimento(
            caixa_turno_id=turno.id,
            valor=Decimal("25.00"),
            funcionario_id=operador.id,
            descricao="Reforço",
            ator_id=operador.id,
        )
        saldo = MovimentacaoCaixaService.calcular_saldo_esperado_gaveta(turno.id)
        assert saldo == Decimal("125.00")

    def test_combinacao_completa(self, db, catalogo_pdv, turno, operador, area_administrativo):
        _venda_cash(catalogo_pdv, turno, valor=30.0, quantity=1)  # +30 -> 130
        MovimentacaoCaixaService.registrar_sangria(
            caixa_turno_id=turno.id,
            tipo_movimentacao=TipoMovimentacaoCaixa.PAGAMENTO_DESPESA,
            valor=Decimal("50.00"),
            funcionario_id=operador.id,
            descricao="Despesa administrativa",
            centro_de_custo_id=area_administrativo.id,
            ator_id=operador.id,
        )  # -50 -> 80
        MovimentacaoCaixaService.registrar_suprimento(
            caixa_turno_id=turno.id,
            valor=Decimal("15.00"),
            funcionario_id=operador.id,
            descricao="Reforço",
            ator_id=operador.id,
        )  # +15 -> 95
        saldo = MovimentacaoCaixaService.calcular_saldo_esperado_gaveta(turno.id)
        assert saldo == Decimal("95.00")

    def test_funciona_com_turno_fechado(self, db, turno, operador):
        MovimentacaoCaixaService.registrar_sangria(
            caixa_turno_id=turno.id,
            tipo_movimentacao=TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
            valor=Decimal("20.00"),
            funcionario_id=operador.id,
            descricao="Recolhimento antes de fechar",
            ator_id=operador.id,
        )
        # Fechamento já contabiliza a sangria no esperado (80, não 100) —
        # é exatamente a integração que faz fechar_turno() não acusar quebra
        # de caixa por uma sangria legítima.
        PdvService.fechar_turno(
            shift_id=turno.id,
            closing_counted_amount=80.0,
            closing_counted_pix=0.0,
            ator_id=1,
        )
        saldo = MovimentacaoCaixaService.calcular_saldo_esperado_gaveta(turno.id)
        assert saldo == Decimal("80.00")

    def test_turno_nao_encontrado(self, db):
        with pytest.raises(ValueError, match="não encontrado"):
            MovimentacaoCaixaService.calcular_saldo_esperado_gaveta(9999)


class TestFecharTurnoComMovimentacoes:
    """PdvService.fechar_turno() precisa refletir sangria/suprimento no
    esperado — integração pedida explicitamente (não é só um cálculo
    paralelo que a UI consulta à parte)."""

    def test_sangria_sem_contar_ela_acusa_quebra(self, db, turno, operador):
        MovimentacaoCaixaService.registrar_sangria(
            caixa_turno_id=turno.id,
            tipo_movimentacao=TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
            valor=Decimal("20.00"),
            funcionario_id=operador.id,
            descricao="Recolhimento",
            ator_id=operador.id,
        )
        with pytest.raises(Exception) as exc_info:
            PdvService.fechar_turno(
                shift_id=turno.id,
                closing_counted_amount=100.0,  # ignora a sangria — devia ser 80
                closing_counted_pix=0.0,
                ator_id=1,
            )
        assert "Quebra de caixa" in str(exc_info.value)

    def test_suprimento_soma_no_esperado_do_fechamento(self, db, turno, operador):
        MovimentacaoCaixaService.registrar_suprimento(
            caixa_turno_id=turno.id,
            valor=Decimal("30.00"),
            funcionario_id=operador.id,
            descricao="Reforço",
            ator_id=operador.id,
        )
        fechado = PdvService.fechar_turno(
            shift_id=turno.id,
            closing_counted_amount=130.0,  # 100 fundo + 30 suprimento
            closing_counted_pix=0.0,
            ator_id=1,
        )
        assert float(fechado.closing_expected_amount) == 130.0

    def test_venda_e_sangria_combinadas_fecham_certo(self, db, catalogo_pdv, turno, operador, area_administrativo):
        _venda_cash(catalogo_pdv, turno, valor=30.0, quantity=1)  # +30
        MovimentacaoCaixaService.registrar_sangria(
            caixa_turno_id=turno.id,
            tipo_movimentacao=TipoMovimentacaoCaixa.PAGAMENTO_DESPESA,
            valor=Decimal("50.00"),
            funcionario_id=operador.id,
            descricao="Despesa administrativa",
            centro_de_custo_id=area_administrativo.id,
            ator_id=operador.id,
        )  # -50
        fechado = PdvService.fechar_turno(
            shift_id=turno.id,
            closing_counted_amount=80.0,  # 100 + 30 - 50
            closing_counted_pix=0.0,
            ator_id=1,
        )
        assert float(fechado.closing_expected_amount) == 80.0


class TestListarPorTurno:
    def test_lista_ordenado_mais_recente_primeiro(self, db, turno, operador):
        primeira = MovimentacaoCaixaService.registrar_sangria(
            caixa_turno_id=turno.id,
            tipo_movimentacao=TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
            valor=Decimal("10.00"),
            funcionario_id=operador.id,
            descricao="Primeira",
            ator_id=operador.id,
        )
        segunda = MovimentacaoCaixaService.registrar_suprimento(
            caixa_turno_id=turno.id,
            valor=Decimal("5.00"),
            funcionario_id=operador.id,
            descricao="Segunda",
            ator_id=operador.id,
        )
        resultado = MovimentacaoCaixaService.listar_por_turno(turno.id)
        assert [m.id for m in resultado] == [segunda.id, primeira.id]

    def test_turno_sem_movimentacoes_retorna_vazio(self, db, turno):
        assert MovimentacaoCaixaService.listar_por_turno(turno.id) == []
