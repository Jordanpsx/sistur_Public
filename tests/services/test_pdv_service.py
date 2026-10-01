# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes para o PdvService (módulo PDV — Ponto de Venda de balcão).

Cobertura mínima por método de mutação (skills/test-service.md):
happy path, AuditLog criado, input inválido, entidade não encontrada,
mais os invariantes específicos do PDV: idempotência por client_sale_uuid,
status PAID imediato para CASH, troco, conflito de estoque (OverbookingError)
e trilha PdvOfflineSyncLog.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.core.models import AuditAction, AuditLog
from app.models.pdv import (
    PdvChangeSource,
    PdvOfflineSyncLog,
    PdvPaymentMethod,
    PdvSale,
    PdvShiftStatus,
    PdvSyncOutcome,
)
from app.models.funcionario import Funcionario
from app.models.role import Role, RolePermission
from app.models.reservas import (
    Reserva,
    ReservaCategory,
    ReservaGroup,
    ReservaItem,
    ReservaResource,
    ReservaSource,
    ReservaStatus,
)
from app.services.pdv_service import PdvService, QuebraDeCaixaError
from app.services.reserva_service import OverbookingError


@pytest.fixture
def catalogo_pdv(db):
    """Venue com item ilimitado (Lenha) e item de recurso finito (Day Use)."""
    source = ReservaSource(name="Cachoeira")
    cat_loja = ReservaCategory(name="Loja", source=source)
    cat_dayuse = ReservaCategory(name="Day Use", source=source)

    lenha = ReservaItem(
        name="Lenha (fardo)",
        billing_type="FIXED",
        price=30.00,
        requires_deposit=False,
        category=cat_loja,
    )

    grupo = ReservaGroup(name="Acessos", sources=[source])
    db.session.add(grupo)
    db.session.flush()
    recurso = ReservaResource(
        name="Capacidade Day Use", stock_quantity=1, source=source, group_id=grupo.id
    )
    dayuse = ReservaItem(
        name="Day Use Inteira",
        billing_type="FIXED",
        price=60.00,
        requires_deposit=False,
        category=cat_dayuse,
        group_id=grupo.id,
        requires_resource=True,
    )

    db.session.add_all([cat_loja, cat_dayuse, lenha, recurso, dayuse])
    db.session.commit()
    return {
        "source": source,
        "lenha": lenha,
        "dayuse": dayuse,
        "recurso": recurso,
    }


@pytest.fixture
def turno(db, catalogo_pdv):
    """Turno de caixa aberto no venue de teste."""
    return PdvService.abrir_turno(
        source_id=catalogo_pdv["source"].id,
        operador_id=1,
        opening_float_amount=100.0,
        ator_id=1,
    )


def _venda_kwargs(catalogo_pdv, turno, **overrides):
    """Payload base de venda CASH de 2 fardos de lenha."""
    base = dict(
        client_sale_uuid=str(uuid.uuid4()),
        shift_id=turno.id,
        source_name="Cachoeira",
        category_name="Loja",
        items=[{"item_id": catalogo_pdv["lenha"].id, "quantity": 2}],
        payment_method="CASH",
        customer_name="Maria Souza",
        amount_tendered=100.0,
        ator_id=1,
    )
    base.update(overrides)
    return base


def _gerente_pdv(db, *, cpf="52998224725", senha="senha-segura"):
    """Funcionário com permissão gerencial de PDV para testes de override."""
    from werkzeug.security import generate_password_hash

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
    return gerente


def _funcionario_sem_override(db, *, cpf="12345678909", senha="senha-segura"):
    """Funcionário ativo, com senha, mas sem pdv.manage_shift."""
    from werkzeug.security import generate_password_hash

    role = Role(nome=f"Operador sem override {cpf}", is_super_admin=False, ativo=True)
    db.session.add(role)
    db.session.flush()
    funcionario = Funcionario(
        nome="Operador sem override",
        cpf=cpf,
        ativo=True,
        role_id=role.id,
        senha_hash=generate_password_hash(senha),
    )
    db.session.add(funcionario)
    db.session.commit()
    return funcionario


class TestAbrirTurno:
    def test_happy_path(self, db, catalogo_pdv):
        shift = PdvService.abrir_turno(
            source_id=catalogo_pdv["source"].id,
            operador_id=1,
            opening_float_amount=50.0,
            opening_float_pix=25.0,
            ator_id=1,
        )
        assert shift.id is not None
        assert shift.status == PdvShiftStatus.OPEN
        assert float(shift.opening_float_amount) == 50.0
        assert float(shift.opening_float_pix) == 25.0

    def test_audit_log_created(self, db, catalogo_pdv):
        shift = PdvService.abrir_turno(
            source_id=catalogo_pdv["source"].id, operador_id=1, ator_id=1
        )
        count = db.session.query(AuditLog).filter_by(
            action=AuditAction.create, module="pdv", entity_id=shift.id
        ).count()
        assert count == 1

    def test_turno_duplicado(self, db, catalogo_pdv, turno):
        with pytest.raises(ValueError, match="turno aberto"):
            PdvService.abrir_turno(
                source_id=catalogo_pdv["source"].id, operador_id=1, ator_id=1
            )

    def test_venue_nao_encontrado(self, db):
        with pytest.raises(ValueError, match="não encontrado"):
            PdvService.abrir_turno(source_id=9999, operador_id=1, ator_id=1)

    def test_fundo_negativo(self, db, catalogo_pdv):
        with pytest.raises(ValueError, match="negativo"):
            PdvService.abrir_turno(
                source_id=catalogo_pdv["source"].id,
                operador_id=1,
                opening_float_amount=-10.0,
            )

        with pytest.raises(ValueError, match="PIX.*negativo"):
            PdvService.abrir_turno(
                source_id=catalogo_pdv["source"].id,
                operador_id=1,
                opening_float_pix=-10.0,
            )


class TestFecharTurno:
    def test_happy_path_com_conferencia(self, db, catalogo_pdv, turno):
        PdvService.registrar_venda(**_venda_kwargs(catalogo_pdv, turno))
        fechado = PdvService.fechar_turno(
            turno.id, closing_counted_amount=160.0, ator_id=1
        )
        assert fechado.status == PdvShiftStatus.CLOSED
        # esperado = fundo 100 + venda CASH 60 (2 × 30)
        assert float(fechado.closing_expected_amount) == 160.0
        assert float(fechado.closing_expected_pix) == 0.0
        assert fechado.closed_at is not None

    def test_fechamento_com_troco_pix(self, db, catalogo_pdv, turno):
        turno.opening_float_pix = 50.0
        db.session.commit()
        PdvService.registrar_venda(
            **_venda_kwargs(
                catalogo_pdv,
                turno,
                amount_tendered=100.0,
                change_source=PdvChangeSource.PIX,
            )
        )

        fechado = PdvService.fechar_turno(
            turno.id,
            closing_counted_amount=200.0,
            closing_counted_pix=10.0,
            ator_id=1,
        )

        assert fechado.status == PdvShiftStatus.CLOSED
        assert float(fechado.closing_expected_amount) == 200.0
        assert float(fechado.closing_expected_pix) == 10.0

    def test_external_pix_nao_altera_o_fundo_de_troco(self, db, catalogo_pdv, turno):
        """O fundo PIX é a reserva de troco — fixo, só decresce. Pix recebido
        direto do cliente não é 'troco digital': vai para fora do sistema
        (conta bancária/pix do estabelecimento), não altera essa reserva."""
        turno.opening_float_pix = 50.0
        db.session.commit()
        PdvService.registrar_venda(
            **_venda_kwargs(
                catalogo_pdv, turno, payment_method="EXTERNAL_PIX", amount_tendered=None
            )
        )

        fechado = PdvService.fechar_turno(
            turno.id,
            closing_counted_amount=100.0,  # gaveta física intocada — fundo original
            closing_counted_pix=50.0,      # fundo intocado — a venda pix não passou por aqui
            ator_id=1,
        )

        assert fechado.status == PdvShiftStatus.CLOSED
        assert float(fechado.closing_expected_amount) == 100.0
        assert float(fechado.closing_expected_pix) == 50.0

    def test_varias_vendas_external_pix_nao_alteram_o_fundo(self, db, catalogo_pdv, turno):
        turno.opening_float_pix = 50.0
        db.session.commit()
        PdvService.registrar_venda(**_venda_kwargs(
            catalogo_pdv, turno, payment_method="EXTERNAL_PIX", amount_tendered=None,
        ))
        PdvService.registrar_venda(**_venda_kwargs(
            catalogo_pdv, turno, payment_method="EXTERNAL_PIX", amount_tendered=None,
        ))

        fechado = PdvService.fechar_turno(
            turno.id, closing_counted_amount=100.0, closing_counted_pix=50.0, ator_id=1,
        )
        assert float(fechado.closing_expected_pix) == 50.0, \
            "duas vendas EXTERNAL_PIX somando 120 não devem mover o fundo nem um centavo"

    def test_external_pix_visivel_no_relatorio_por_metodo(self, db, catalogo_pdv, turno):
        """Não entra na conferência do fundo, mas continua rastreável — via
        relatório, não via 'contagem física' que não existe pra pix recebido."""
        PdvService.registrar_venda(**_venda_kwargs(
            catalogo_pdv, turno, payment_method="EXTERNAL_PIX", amount_tendered=None,
        ))
        relatorio = PdvService.relatorio_turno(turno.id)
        totals = {t["method"]: t for t in relatorio["totals_by_method"]}
        assert totals["EXTERNAL_PIX"]["count"] == 1
        assert totals["EXTERNAL_PIX"]["total"] == 60.0

    def test_mp_pix_nao_entra_em_nenhuma_conferencia(self, db, catalogo_pdv, turno):
        """MP_PIX liquida na conta Mercado Pago — não é gaveta nem conta pix do caixa."""
        PdvService.registrar_venda(**_venda_kwargs(
            catalogo_pdv, turno, payment_method="MP_PIX", amount_tendered=None,
        ))
        fechado = PdvService.fechar_turno(
            turno.id, closing_counted_amount=100.0, closing_counted_pix=0.0, ator_id=1,
        )
        assert float(fechado.closing_expected_amount) == 100.0
        assert float(fechado.closing_expected_pix) == 0.0

    def test_quebra_exige_motivo(self, db, turno):
        with pytest.raises(QuebraDeCaixaError) as exc:
            PdvService.fechar_turno(turno.id, closing_counted_amount=90.0, ator_id=1)

        assert exc.value.detalhe["expected_cash"] == 100.0
        assert exc.value.detalhe["counted_cash"] == 90.0

    def test_fechamento_rejeita_valores_negativos(self, db, turno):
        with pytest.raises(ValueError, match="gaveta.*negativo"):
            PdvService.fechar_turno(turno.id, closing_counted_amount=-1.0, ator_id=1)

        with pytest.raises(ValueError, match="PIX.*negativo"):
            PdvService.fechar_turno(
                turno.id,
                closing_counted_amount=100.0,
                closing_counted_pix=-1.0,
                ator_id=1,
            )

    def test_quebra_com_motivo_fica_pendente_e_aprova(self, db, turno):
        PdvService.fechar_turno(
            turno.id,
            closing_counted_amount=90.0,
            discrepancy_reason="Diferença conferida pelo operador",
            ator_id=1,
        )
        assert turno.status == PdvShiftStatus.PENDING_APPROVAL
        assert turno.closed_at is None

        _funcionario_sem_override(db, cpf="12345678909")
        aprovador = _gerente_pdv(db, cpf="52998224725")
        aprovado = PdvService.aprovar_fechamento(
            turno.id,
            aprovador_id=aprovador.id,
            ator_id=aprovador.id,
        )

        assert aprovado.status == PdvShiftStatus.CLOSED
        assert aprovado.approved_by_id == aprovador.id
        assert aprovado.closed_at is not None

    def test_operador_nao_aprova_propria_quebra(self, db, turno):
        PdvService.fechar_turno(
            turno.id,
            closing_counted_amount=90.0,
            discrepancy_reason="Diferença conferida pelo operador",
            ator_id=1,
        )
        with pytest.raises(ValueError, match="não pode aprovar"):
            PdvService.aprovar_fechamento(turno.id, aprovador_id=1, ator_id=1)

    def test_turno_pendente_bloqueia_novo_turno_mesmo_operador_venue(self, db, catalogo_pdv, turno):
        PdvService.fechar_turno(
            turno.id,
            closing_counted_amount=90.0,
            discrepancy_reason="Diferença conferida pelo operador",
            ator_id=1,
        )

        with pytest.raises(ValueError, match="aguardando aprovação"):
            PdvService.abrir_turno(
                source_id=catalogo_pdv["source"].id,
                operador_id=turno.operador_id,
                opening_float_amount=100.0,
                ator_id=1,
            )

    def test_ja_fechado(self, db, turno):
        PdvService.fechar_turno(turno.id, closing_counted_amount=100.0, ator_id=1)
        with pytest.raises(ValueError, match="fechado"):
            PdvService.fechar_turno(turno.id, closing_counted_amount=100.0, ator_id=1)

    def test_nao_encontrado(self, db):
        with pytest.raises(ValueError, match="não encontrado"):
            PdvService.fechar_turno(9999, closing_counted_amount=0.0)


class TestRegistrarVenda:
    def test_happy_path_cash_paid_imediato(self, db, catalogo_pdv, turno):
        resultado = PdvService.registrar_venda(**_venda_kwargs(catalogo_pdv, turno))
        assert resultado["duplicate"] is False
        assert resultado["pricing"]["total"] == 60.0

        sale = db.session.query(PdvSale).one()
        assert sale.payment_method == PdvPaymentMethod.CASH
        assert float(sale.amount_total) == 60.0
        assert float(sale.amount_change) == 40.0  # entregue 100 − total 60

        reserva = db.session.get(Reserva, resultado["reserva_id"])
        assert reserva is not None
        assert reserva.status == ReservaStatus.PAID
        assert reserva.metadata_info.get("origem_pdv") is True

    def test_audit_log_created(self, db, catalogo_pdv, turno):
        resultado = PdvService.registrar_venda(**_venda_kwargs(catalogo_pdv, turno))
        logs = db.session.query(AuditLog).filter_by(
            action=AuditAction.create,
            module="pdv",
            entity_id=resultado["sale"]["id"],
        ).all()
        # entity_id pode colidir com o log do turno (ids independentes);
        # filtra pelo snapshot que só a venda possui (client_sale_uuid).
        logs_venda = [
            log for log in logs
            if (log.new_state or {}).get("client_sale_uuid")
            == resultado["sale"]["client_sale_uuid"]
        ]
        assert len(logs_venda) == 1

    def test_idempotencia_reenvio(self, db, catalogo_pdv, turno):
        kwargs = _venda_kwargs(catalogo_pdv, turno)
        primeiro = PdvService.registrar_venda(**kwargs)
        segundo = PdvService.registrar_venda(**kwargs)
        assert segundo["duplicate"] is True
        assert segundo["reserva_id"] == primeiro["reserva_id"]
        assert db.session.query(PdvSale).count() == 1
        assert db.session.query(Reserva).count() == 1

    def test_mp_pix_permanece_pending(self, db, catalogo_pdv, turno):
        resultado = PdvService.registrar_venda(
            **_venda_kwargs(
                catalogo_pdv, turno, payment_method="MP_PIX", amount_tendered=None
            )
        )
        reserva = db.session.get(Reserva, resultado["reserva_id"])
        assert reserva.status == ReservaStatus.PENDING

    def test_external_pix_paid_imediato(self, db, catalogo_pdv, turno):
        """Pix recebido direto no balcão não é diferente de dinheiro/maquininha
        própria — o sistema só registra algo que já aconteceu (correção do
        bug relatado pelo usuário: 'Pix' no PDV gerava cobrança MP e esperava
        confirmação, como se o pagamento ainda não tivesse sido feito)."""
        resultado = PdvService.registrar_venda(
            **_venda_kwargs(
                catalogo_pdv, turno, payment_method="EXTERNAL_PIX", amount_tendered=None
            )
        )
        sale = db.session.query(PdvSale).one()
        assert sale.payment_method == PdvPaymentMethod.EXTERNAL_PIX

        reserva = db.session.get(Reserva, resultado["reserva_id"])
        assert reserva.status == ReservaStatus.PAID

    def test_external_card_terminal_paid_imediato(self, db, catalogo_pdv, turno):
        """Mesma regra para cartão na maquininha própria — sem espera, sem MP."""
        resultado = PdvService.registrar_venda(
            **_venda_kwargs(
                catalogo_pdv, turno,
                payment_method="EXTERNAL_CARD_TERMINAL", amount_tendered=None,
            )
        )
        reserva = db.session.get(Reserva, resultado["reserva_id"])
        assert reserva.status == ReservaStatus.PAID

    def test_walkin_sem_nome_gera_balcao_n(self, db, catalogo_pdv, turno):
        resultado = PdvService.registrar_venda(
            **_venda_kwargs(catalogo_pdv, turno, customer_name=None)
        )
        reserva = db.session.get(Reserva, resultado["reserva_id"])
        assert reserva.customer_name.startswith("Balcão ")

    def test_troco_insuficiente(self, db, catalogo_pdv, turno):
        with pytest.raises(ValueError, match="menor que o total"):
            PdvService.registrar_venda(
                **_venda_kwargs(catalogo_pdv, turno, amount_tendered=10.0)
            )

    def test_turno_fechado(self, db, catalogo_pdv, turno):
        PdvService.fechar_turno(turno.id, closing_counted_amount=100.0, ator_id=1)
        with pytest.raises(ValueError, match="fechado"):
            PdvService.registrar_venda(**_venda_kwargs(catalogo_pdv, turno))

    def test_sem_itens(self, db, catalogo_pdv, turno):
        with pytest.raises(ValueError, match="ao menos um item"):
            PdvService.registrar_venda(**_venda_kwargs(catalogo_pdv, turno, items=[]))

    def test_metodo_invalido(self, db, catalogo_pdv, turno):
        with pytest.raises(ValueError, match="inválido"):
            PdvService.registrar_venda(
                **_venda_kwargs(catalogo_pdv, turno, payment_method="CHEQUE")
            )

    def test_overbooking_recurso_finito(self, db, catalogo_pdv, turno):
        """Recurso com stock=1: segunda venda do mesmo dia deve conflitar."""
        kwargs = _venda_kwargs(
            catalogo_pdv,
            turno,
            category_name="Day Use",
            items=[{
                "item_id": catalogo_pdv["dayuse"].id,
                "quantity": 1,
                "resource_id": catalogo_pdv["recurso"].id,
            }],
            amount_tendered=100.0,
        )
        PdvService.registrar_venda(**kwargs)

        kwargs2 = dict(kwargs, client_sale_uuid=str(uuid.uuid4()))
        with pytest.raises(OverbookingError):
            PdvService.registrar_venda(**kwargs2)

    def test_autorizar_manager_override(self, db):
        gerente = _gerente_pdv(db)

        override = PdvService.autorizar_manager_override(
            cpf="529.982.247-25",
            senha="senha-segura",
            reason="Cliente autorizado pela gerência",
        )

        assert override == {
            "manager_id": gerente.id,
            "manager_name": "Gerente PDV",
            "reason": "Cliente autorizado pela gerência",
        }

    def test_autorizar_manager_override_rejeita_senha_invalida(self, db):
        _gerente_pdv(db)

        with pytest.raises(ValueError, match="Credenciais de gerente inválidas"):
            PdvService.autorizar_manager_override(
                cpf="52998224725",
                senha="errada",
                reason="Cliente autorizado pela gerência",
            )

    def test_force_overbooking_exige_override_gerencial(self, db, catalogo_pdv, turno):
        with pytest.raises(ValueError, match="Override gerencial obrigatório"):
            PdvService.registrar_venda(
                **_venda_kwargs(catalogo_pdv, turno, force_overbooking=True)
            )

    def test_force_overbooking_rejeita_funcionario_sem_permissao(self, db, catalogo_pdv, turno):
        operador = _funcionario_sem_override(db)

        with pytest.raises(ValueError, match="sem permissão gerencial"):
            PdvService.registrar_venda(
                **_venda_kwargs(
                    catalogo_pdv,
                    turno,
                    force_overbooking=True,
                    manager_override={
                        "manager_id": operador.id,
                        "reason": "Cliente autorizado pela gerência",
                    },
                )
            )

    def test_force_overbooking_com_override_audita(self, db, catalogo_pdv, turno):
        gerente = _gerente_pdv(db)
        resultado = PdvService.registrar_venda(
            **_venda_kwargs(
                catalogo_pdv,
                turno,
                force_overbooking=True,
                manager_override={
                    "manager_id": gerente.id,
                    "reason": "Cliente autorizado pela gerência",
                },
            )
        )

        sale = db.session.query(PdvSale).filter_by(id=resultado["sale"]["id"]).one()
        log = db.session.query(AuditLog).filter_by(
            action=AuditAction.update,
            module="pdv",
            entity_id=sale.id,
        ).one()
        assert log.new_state["action"] == "manager_override"
        assert log.new_state["manager_id"] == gerente.id
        assert log.new_state["reason"] == "Cliente autorizado pela gerência"
        assert log.new_state["force_overbooking"] is True

    def test_sync_log_offline_aceita(self, db, catalogo_pdv, turno):
        PdvService.registrar_venda(
            **_venda_kwargs(
                catalogo_pdv, turno, synced_offline=True, device_id="terminal-01"
            )
        )
        log = db.session.query(PdvOfflineSyncLog).one()
        assert log.outcome == PdvSyncOutcome.ACCEPTED
        assert log.device_id == "terminal-01"
        sale = db.session.query(PdvSale).one()
        assert sale.synced_offline is True

    def test_offline_override_note_registrada(self, db, catalogo_pdv, turno):
        """Nota offline vai para metadata, sync log e AuditLog dedicado."""
        resultado = PdvService.registrar_venda(
            **_venda_kwargs(
                catalogo_pdv,
                turno,
                synced_offline=True,
                device_id="terminal-01",
                offline_override_note={
                    "cpf": "529.982.247-25",
                    "reason": "Gerente confirmou vaga no balcão",
                },
            )
        )
        reserva = db.session.get(Reserva, resultado["reserva_id"])
        nota = reserva.metadata_info.get("pdv_offline_override_note")
        assert nota == {"cpf": "52998224725", "reason": "Gerente confirmou vaga no balcão"}

        log = db.session.query(PdvOfflineSyncLog).one()
        assert log.payload_snapshot["offline_override_note"]["cpf"] == "52998224725"

        auditoria = [
            a for a in db.session.query(AuditLog).filter_by(module="pdv").all()
            if (a.new_state or {}).get("action") == "offline_override_note"
        ]
        assert len(auditoria) == 1
        assert auditoria[0].new_state["manager_cpf"] == "52998224725"

    def test_offline_override_note_motivo_curto(self, db, catalogo_pdv, turno):
        with pytest.raises(ValueError, match="10 caracteres"):
            PdvService.registrar_venda(
                **_venda_kwargs(
                    catalogo_pdv,
                    turno,
                    offline_override_note={"cpf": "52998224725", "reason": "curto"},
                )
            )

    def test_offline_override_note_cpf_invalido(self, db, catalogo_pdv, turno):
        with pytest.raises(ValueError, match="CPF do responsável"):
            PdvService.registrar_venda(
                **_venda_kwargs(
                    catalogo_pdv,
                    turno,
                    offline_override_note={"cpf": "123", "reason": "Motivo suficientemente longo"},
                )
            )

    def test_replay_fila_offline_apos_reconexao(self, db, catalogo_pdv, turno):
        """Simula o drain da fila: mesmo payload reenviado N vezes → 1 venda.

        Reproduz o cenário 'resposta perdida na reconexão': o SW reenvia o
        job porque não viu o 200, e o backend precisa devolver a venda
        original sem duplicar Reserva/PdvSale nem estourar estoque.
        """
        kwargs = _venda_kwargs(
            catalogo_pdv,
            turno,
            synced_offline=True,
            device_id="terminal-01",
            category_name="Day Use",
            items=[{
                "item_id": catalogo_pdv["dayuse"].id,
                "quantity": 1,
                "resource_id": catalogo_pdv["recurso"].id,
            }],
        )
        primeiro = PdvService.registrar_venda(**kwargs)
        for _ in range(3):  # retries do drain
            replay = PdvService.registrar_venda(**kwargs)
            assert replay["duplicate"] is True
            assert replay["reserva_id"] == primeiro["reserva_id"]

        assert db.session.query(PdvSale).count() == 1
        assert db.session.query(Reserva).count() == 1
        # Trilha: 1 ACCEPTED + 3 REJECTED_DUPLICATE
        outcomes = [
            log.outcome for log in db.session.query(PdvOfflineSyncLog).all()
        ]
        assert outcomes.count(PdvSyncOutcome.ACCEPTED) == 1
        assert outcomes.count(PdvSyncOutcome.REJECTED_DUPLICATE) == 3

    def test_sync_estoque_esgotado_rejeita_e_audita(self, db, catalogo_pdv, turno):
        """Venda 🔴 vinda da fila com estoque já vendido online → REJECTED_STOCK.

        É o cenário de cache defasado do §4.2: a nota de override offline não
        seta force_overbooking, então o lock de estoque decide e rejeita.
        """
        base = dict(
            category_name="Day Use",
            items=[{
                "item_id": catalogo_pdv["dayuse"].id,
                "quantity": 1,
                "resource_id": catalogo_pdv["recurso"].id,
            }],
        )
        # Outro canal (web) consome a última vaga primeiro
        PdvService.registrar_venda(**_venda_kwargs(catalogo_pdv, turno, **base))

        # Agora chega a venda offline autorizada por nota gerencial
        with pytest.raises(OverbookingError):
            PdvService.registrar_venda(
                **_venda_kwargs(
                    catalogo_pdv,
                    turno,
                    **base,
                    synced_offline=True,
                    device_id="terminal-01",
                    offline_override_note={
                        "cpf": "52998224725",
                        "reason": "Gerente autorizou no balcão",
                    },
                )
            )
        rejeitado = db.session.query(PdvOfflineSyncLog).filter_by(
            outcome=PdvSyncOutcome.REJECTED_STOCK,
        ).one()
        assert rejeitado.device_id == "terminal-01"
        assert rejeitado.payload_snapshot["offline_override_note"]["reason"] == "Gerente autorizou no balcão"
        # Não criou venda nem reserva extra
        assert db.session.query(PdvSale).count() == 1

    def test_sync_log_duplicata(self, db, catalogo_pdv, turno):
        kwargs = _venda_kwargs(catalogo_pdv, turno)
        PdvService.registrar_venda(**kwargs)
        PdvService.registrar_venda(**dict(kwargs, device_id="terminal-01"))
        log = db.session.query(PdvOfflineSyncLog).one()
        assert log.outcome == PdvSyncOutcome.REJECTED_DUPLICATE

    def test_listar_sync_logs_resume_sem_payload_bruto(self, db, catalogo_pdv, turno):
        PdvService.registrar_venda(
            **_venda_kwargs(
                catalogo_pdv, turno, synced_offline=True, device_id="terminal-01"
            )
        )

        logs = PdvService.listar_sync_logs()

        assert len(logs) == 1
        assert logs[0]["outcome"] == "ACCEPTED"
        assert logs[0]["device_id"] == "terminal-01"
        assert "payload_snapshot" not in logs[0]
        assert logs[0]["payload_summary"] == {
            "shift_id": turno.id,
            "source_name": "Cachoeira",
            "payment_method": "CASH",
            "item_count": 1,
            "total_quantity": 2,
        }

    def test_listar_sync_logs_filtra_outcome(self, db, catalogo_pdv, turno):
        kwargs = _venda_kwargs(catalogo_pdv, turno)
        PdvService.registrar_venda(**dict(kwargs, synced_offline=True, device_id="terminal-01"))
        PdvService.registrar_venda(**dict(kwargs, device_id="terminal-01"))

        logs = PdvService.listar_sync_logs(outcome="REJECTED_DUPLICATE")

        assert len(logs) == 1
        assert logs[0]["outcome"] == "REJECTED_DUPLICATE"

    def test_listar_sync_logs_rejeita_outcome_invalido(self, db):
        with pytest.raises(ValueError, match="Outcome de sync inválido"):
            PdvService.listar_sync_logs(outcome="INVALIDO")


class TestAnexarComanda:
    def _reserva_hospede(self, catalogo_pdv, turno):
        """Cria a reserva original do hóspede via venda PDV."""
        resultado = PdvService.registrar_venda(**_venda_kwargs(catalogo_pdv, turno))
        return resultado["reserva_id"]

    def test_happy_path(self, db, catalogo_pdv, turno):
        reserva_id = self._reserva_hospede(catalogo_pdv, turno)
        resultado = PdvService.anexar_comanda(
            client_sale_uuid=str(uuid.uuid4()),
            shift_id=turno.id,
            reserva_id=reserva_id,
            items=[{"item_id": catalogo_pdv["lenha"].id, "quantity": 1}],
            ator_id=1,
        )
        assert resultado["duplicate"] is False
        # Nova versão criada (versionamento imutável)
        assert resultado["reserva_id"] != reserva_id
        sale = db.session.query(PdvSale).filter_by(
            client_sale_uuid=resultado["sale"]["client_sale_uuid"]
        ).one()
        assert sale.payment_method == PdvPaymentMethod.COMANDA
        # Valor anexado = total novo (90) − total anterior (60)
        assert float(sale.amount_total) == 30.0

    def test_idempotencia(self, db, catalogo_pdv, turno):
        reserva_id = self._reserva_hospede(catalogo_pdv, turno)
        chave = str(uuid.uuid4())
        PdvService.anexar_comanda(
            client_sale_uuid=chave,
            shift_id=turno.id,
            reserva_id=reserva_id,
            items=[{"item_id": catalogo_pdv["lenha"].id, "quantity": 1}],
        )
        segundo = PdvService.anexar_comanda(
            client_sale_uuid=chave,
            shift_id=turno.id,
            reserva_id=reserva_id,
            items=[{"item_id": catalogo_pdv["lenha"].id, "quantity": 1}],
        )
        assert segundo["duplicate"] is True

    def test_reserva_nao_encontrada(self, db, catalogo_pdv, turno):
        with pytest.raises(ValueError, match="não encontrada"):
            PdvService.anexar_comanda(
                client_sale_uuid=str(uuid.uuid4()),
                shift_id=turno.id,
                reserva_id=9999,
                items=[{"item_id": catalogo_pdv["lenha"].id, "quantity": 1}],
            )


class TestCarrinhosSuspensos:
    def test_suspender_listar_resolver(self, db, catalogo_pdv, turno):
        held = PdvService.suspender_carrinho(
            turno.id,
            cart_json={"items": [{"item_id": 1, "quantity": 2}]},
            label="Mesa 3",
            ator_id=1,
        )
        pendentes = PdvService.listar_carrinhos_suspensos(turno.id)
        assert [c.id for c in pendentes] == [held.id]

        PdvService.resolver_carrinho_suspenso(held.id, ator_id=1)
        assert PdvService.listar_carrinhos_suspensos(turno.id) == []

    def test_carrinho_vazio(self, db, turno):
        with pytest.raises(ValueError, match="vazio"):
            PdvService.suspender_carrinho(turno.id, cart_json={}, ator_id=1)

    def test_resolver_nao_encontrado(self, db):
        with pytest.raises(ValueError, match="não encontrado"):
            PdvService.resolver_carrinho_suspenso(9999)


class TestBuscarCliente:
    def test_por_nome_e_documento(self, db, catalogo_pdv, turno):
        PdvService.registrar_venda(
            **_venda_kwargs(catalogo_pdv, turno, customer_document="52998224725")
        )
        por_nome = PdvService.buscar_cliente("Maria")
        assert len(por_nome) == 1
        assert por_nome[0]["customer_name"] == "Maria Souza"

        por_cpf = PdvService.buscar_cliente("529.982.247-25")
        assert len(por_cpf) == 1

    def test_query_curta(self, db):
        with pytest.raises(ValueError, match="3 caracteres"):
            PdvService.buscar_cliente("ab")


class TestCatalogoPdv:
    def test_flags_offline(self, db, catalogo_pdv):
        payload = PdvService.catalogo_pdv(catalogo_pdv["source"].id)
        itens = {
            item["name"]: item
            for cat in payload["source"]["categories"]
            for item in cat["items"]
        }
        assert itens["Lenha (fardo)"]["pdv_offline_allowed"] is True   # 🟢 sem recurso
        assert itens["Day Use Inteira"]["pdv_offline_allowed"] is False  # 🔴 stock=1

    def test_venue_inexistente(self, db):
        with pytest.raises(ValueError, match="não encontrado"):
            PdvService.catalogo_pdv(9999)


class TestTurnoAtual:
    def test_retorna_turno_aberto(self, db, catalogo_pdv, turno):
        atual = PdvService.turno_atual(operador_id=1)
        assert atual is not None
        assert atual["shift_id"] == turno.id
        assert atual["source_name"] == "Cachoeira"
        assert atual["opening_float_amount"] == 100.0

    def test_sem_turno_aberto(self, db, catalogo_pdv, turno):
        PdvService.fechar_turno(turno.id, closing_counted_amount=100.0, ator_id=1)
        assert PdvService.turno_atual(operador_id=1) is None

    def test_operador_diferente(self, db, catalogo_pdv, turno):
        assert PdvService.turno_atual(operador_id=99) is None


class TestListarVenues:
    def test_lista_ativos(self, db, catalogo_pdv):
        venues = PdvService.listar_venues()
        assert venues == [{"id": catalogo_pdv["source"].id, "name": "Cachoeira"}]


class TestRelatorioTurno:
    def test_totais_por_metodo(self, db, catalogo_pdv, turno):
        PdvService.registrar_venda(**_venda_kwargs(catalogo_pdv, turno))
        PdvService.registrar_venda(
            **_venda_kwargs(
                catalogo_pdv,
                turno,
                payment_method="EXTERNAL_CARD_TERMINAL",
                amount_tendered=None,
            )
        )
        rel = PdvService.relatorio_turno(turno.id)
        assert rel["sales_count"] == 2
        assert rel["grand_total"] == 120.0
        metodos = {t["method"]: t["total"] for t in rel["totals_by_method"]}
        assert metodos == {"CASH": 60.0, "EXTERNAL_CARD_TERMINAL": 60.0}

    def test_nao_encontrado(self, db):
        with pytest.raises(ValueError, match="não encontrado"):
            PdvService.relatorio_turno(9999)



class TestCatalogoPrecoDoDia:
    """O catálogo do PDV precisa EXIBIR o mesmo preço que a venda vai COBRAR.

    ReservaService.catalogo() devolve sempre item.price (base). Sem a correção
    em catalogo_pdv, a tela mostrava o preço base enquanto registrar_venda()
    cobrava o preço do dia (dia útil / fim de semana / feriado) — divergência
    invisível em dia útil e que só aparecia no fim de semana ou feriado.
    """

    @staticmethod
    def _congelar_data(monkeypatch, quando):
        """Fixa a data que o catálogo usa como referência de precificação."""
        import app.services.pdv_service as pdv_mod

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return quando

        monkeypatch.setattr(pdv_mod, "datetime", _DT)

    @staticmethod
    def _preco_na_tela(source_id, item_id):
        payload = PdvService.catalogo_pdv(source_id=source_id, favorite_item_ids=[])
        for categoria in payload["source"]["categories"]:
            for item in categoria["items"]:
                if int(item["id"]) == item_id:
                    return item["price"]
        raise AssertionError(f"item {item_id} não encontrado no catálogo")

    @staticmethod
    def _item_tarifado(db, catalogo_pdv, **precos):
        item = ReservaItem(
            category_id=catalogo_pdv["lenha"].category_id,
            name="Inteira",
            billing_type="FIXED",
            **precos,
        )
        db.session.add(item)
        db.session.commit()
        return item

    def test_tela_usa_preco_de_fim_de_semana(self, db, catalogo_pdv, monkeypatch):
        item = self._item_tarifado(
            db, catalogo_pdv,
            price=30, price_weekday=30, price_weekend=35, price_holiday=40,
        )
        self._congelar_data(
            monkeypatch, datetime(2026, 8, 8, 15, 0, tzinfo=timezone.utc)  # sábado
        )
        assert self._preco_na_tela(catalogo_pdv["source"].id, item.id) == 35.0

    def test_tela_usa_preco_de_dia_util(self, db, catalogo_pdv, monkeypatch):
        item = self._item_tarifado(
            db, catalogo_pdv,
            price=30, price_weekday=22, price_weekend=35, price_holiday=40,
        )
        self._congelar_data(
            monkeypatch, datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)  # segunda
        )
        assert self._preco_na_tela(catalogo_pdv["source"].id, item.id) == 22.0

    def test_tela_e_venda_concordam_no_fim_de_semana(self, db, catalogo_pdv, monkeypatch):
        """O invariante que importa: exibido == cobrado."""
        from app.services.reserva_pricing_service import ReservaPricingService

        item = self._item_tarifado(
            db, catalogo_pdv,
            price=30, price_weekday=30, price_weekend=35, price_holiday=40,
        )
        sabado = datetime(2026, 8, 8, 15, 0, tzinfo=timezone.utc)
        self._congelar_data(monkeypatch, sabado)

        exibido = self._preco_na_tela(catalogo_pdv["source"].id, item.id)
        cobrado = ReservaPricingService.calculate(
            source_id=catalogo_pdv["source"].id,
            check_in_date=datetime(2026, 8, 8),
            check_out_date=datetime(2026, 8, 8),
            items=[{"item_id": item.id, "quantity": 1}],
        )["items_breakdown"][0]["unit_price"]

        assert float(exibido) == float(cobrado) == 35.0

    def test_item_sem_tarifa_por_dia_mantem_preco_base(self, db, catalogo_pdv):
        preco = self._preco_na_tela(
            catalogo_pdv["source"].id, catalogo_pdv["lenha"].id
        )
        assert preco == float(catalogo_pdv["lenha"].price)

    def test_payload_expoe_a_data_usada_na_precificacao(self, db, catalogo_pdv):
        payload = PdvService.catalogo_pdv(
            source_id=catalogo_pdv["source"].id, favorite_item_ids=[]
        )
        assert "pricing_date" in payload
