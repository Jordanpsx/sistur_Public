# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes para SecurityDepositService.

Cobre: criar, pagar, devolver, deletar, is_asset_under_deposit, pendente_total.
Cada mutação verifica a criação de exatamente um AuditLog (Regra #6).
"""

from datetime import timedelta, timezone
from datetime import datetime

import pytest

from app.core.audit import AuditLog
from app.models.reservas import (
    AssetStatus,
    BillingType,
    PhysicalAsset,
    Reserva,
    ReservaCategory,
    ReservaItem,
    ReservaItemLink,
    ReservaOrigin,
    ReservaResource,
    ReservaSource,
    ReservaStatus,
    SecurityDeposit,
)
from app.services.reserva_service import SecurityDepositService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def reserva_simples(db):
    """Cria uma Reserva mínima em status PENDING para uso nos testes."""
    source = ReservaSource(name="TestSource")
    cat = ReservaCategory(name="TestCat", source=source)
    db.session.add_all([source, cat])
    db.session.flush()

    import uuid
    from datetime import date

    reserva = Reserva(
        group_id=str(uuid.uuid4()),
        version=1,
        source_id=source.id,
        category_id=cat.id,
        customer_name="Cliente Teste",
        customer_document="12345678901",
        origin=ReservaOrigin.BALCAO,
        status=ReservaStatus.PENDING,
        check_in_date=datetime.now(timezone.utc),
        check_out_date=datetime.now(timezone.utc) + timedelta(days=1),
        total_amount=200.00,
    )
    db.session.add(reserva)
    db.session.commit()
    return reserva


@pytest.fixture
def ativo_fisico(db, reserva_simples):
    """Cria um PhysicalAsset vinculado ao source da reserva."""
    asset = PhysicalAsset(
        source_id=reserva_simples.source_id,
        name="Churrasqueira A1",
        asset_type="Churrasqueira",
        current_status=AssetStatus.AVAILABLE,
    )
    db.session.add(asset)
    db.session.commit()
    return asset


# ---------------------------------------------------------------------------
# criar()
# ---------------------------------------------------------------------------


def test_criar_caucao_sucesso(app, db, reserva_simples):
    """Cria uma caução com valores válidos e verifica AuditLog."""
    with app.app_context():
        deposit = SecurityDepositService.criar(
            reserva_id=reserva_simples.id,
            amount=150.00,
            description="Caução padrão",
            ator_id=None,
        )

        assert deposit.id is not None
        assert float(deposit.amount) == 150.00
        assert deposit.is_paid is False
        assert deposit.is_refunded is False
        assert deposit.reserva_group_id == reserva_simples.group_id

        logs = (
            db.session.query(AuditLog)
            .filter_by(action="create", module="reservas", entity_id=deposit.id)
            .count()
        )
        assert logs == 1, "Deve existir exatamente 1 AuditLog de create."


def test_criar_caucao_valor_invalido(app, db, reserva_simples):
    """Rejeita criação de caução com valor zero ou negativo."""
    with app.app_context():
        with pytest.raises(ValueError, match="positivo"):
            SecurityDepositService.criar(
                reserva_id=reserva_simples.id,
                amount=0,
            )

        with pytest.raises(ValueError, match="positivo"):
            SecurityDepositService.criar(
                reserva_id=reserva_simples.id,
                amount=-50.00,
            )


def test_criar_caucao_reserva_nao_encontrada(app, db):
    """Rejeita criação de caução para reserva inexistente."""
    with app.app_context():
        with pytest.raises(ValueError, match="não encontrada"):
            SecurityDepositService.criar(
                reserva_id=99999,
                amount=100.00,
            )


def test_criar_caucao_com_ativo_vinculado(app, db, reserva_simples, ativo_fisico):
    """Cria caução vinculada a PhysicalAsset válido."""
    with app.app_context():
        deposit = SecurityDepositService.criar(
            reserva_id=reserva_simples.id,
            amount=200.00,
            linked_asset_id=ativo_fisico.id,
        )

        assert deposit.linked_asset_id == ativo_fisico.id


def test_criar_caucao_ativo_invalido(app, db, reserva_simples):
    """Rejeita criação de caução com linked_asset_id inexistente."""
    with app.app_context():
        with pytest.raises(ValueError, match="não encontrado"):
            SecurityDepositService.criar(
                reserva_id=reserva_simples.id,
                amount=100.00,
                linked_asset_id=99999,
            )


# ---------------------------------------------------------------------------
# pagar()
# ---------------------------------------------------------------------------


def test_pagar_caucao_sucesso(app, db, reserva_simples, ativo_fisico):
    """Marca caução como paga e atualiza status do ativo para UNDER_DEPOSIT."""
    with app.app_context():
        deposit = SecurityDepositService.criar(
            reserva_id=reserva_simples.id,
            amount=100.00,
            linked_asset_id=ativo_fisico.id,
        )

        deposit = SecurityDepositService.pagar(deposit_id=deposit.id, ator_id=None)

        assert deposit.is_paid is True

        # Re-query dentro do app_context — o fixture commitou em sessão
        # diferente, então `ativo_fisico` está detached e session.refresh falha.
        asset = db.session.get(PhysicalAsset, ativo_fisico.id)
        assert asset.current_status == AssetStatus.UNDER_DEPOSIT

        logs = (
            db.session.query(AuditLog)
            .filter_by(action="update", module="reservas", entity_id=deposit.id)
            .count()
        )
        assert logs == 1, "Deve existir exatamente 1 AuditLog de update."


def test_pagar_caucao_ja_paga(app, db, reserva_simples):
    """Rejeita tentativa de pagar caução já paga."""
    with app.app_context():
        deposit = SecurityDepositService.criar(
            reserva_id=reserva_simples.id,
            amount=100.00,
        )
        SecurityDepositService.pagar(deposit_id=deposit.id)

        with pytest.raises(ValueError, match="já foi registrada como paga"):
            SecurityDepositService.pagar(deposit_id=deposit.id)


# ---------------------------------------------------------------------------
# devolver()
# ---------------------------------------------------------------------------


def test_devolver_caucao_sucesso(app, db, reserva_simples, ativo_fisico):
    """Marca caução como devolvida e retorna ativo para AVAILABLE."""
    with app.app_context():
        deposit = SecurityDepositService.criar(
            reserva_id=reserva_simples.id,
            amount=100.00,
            linked_asset_id=ativo_fisico.id,
        )
        SecurityDepositService.pagar(deposit_id=deposit.id)
        deposit = SecurityDepositService.devolver(deposit_id=deposit.id, ator_id=None)

        assert deposit.is_refunded is True

        asset = db.session.get(PhysicalAsset, ativo_fisico.id)
        assert asset.current_status == AssetStatus.AVAILABLE


def test_devolver_caucao_nao_paga(app, db, reserva_simples):
    """Rejeita devolução de caução que ainda não foi paga."""
    with app.app_context():
        deposit = SecurityDepositService.criar(
            reserva_id=reserva_simples.id,
            amount=100.00,
        )

        with pytest.raises(ValueError, match="ainda não foi paga"):
            SecurityDepositService.devolver(deposit_id=deposit.id)


def test_devolver_caucao_ja_devolvida(app, db, reserva_simples):
    """Rejeita devolução duplicada da mesma caução."""
    with app.app_context():
        deposit = SecurityDepositService.criar(
            reserva_id=reserva_simples.id,
            amount=100.00,
        )
        SecurityDepositService.pagar(deposit_id=deposit.id)
        SecurityDepositService.devolver(deposit_id=deposit.id)

        with pytest.raises(ValueError, match="já foi devolvida"):
            SecurityDepositService.devolver(deposit_id=deposit.id)


# ---------------------------------------------------------------------------
# deletar()
# ---------------------------------------------------------------------------


def test_deletar_caucao_nao_paga(app, db, reserva_simples):
    """Deleta logicamente caução não-paga e verifica AuditLog."""
    with app.app_context():
        deposit = SecurityDepositService.criar(
            reserva_id=reserva_simples.id,
            amount=100.00,
        )
        deposit_id = deposit.id

        SecurityDepositService.deletar(deposit_id=deposit_id, ator_id=None)

        # Deve ter desaparecido das queries ativas
        ativo = (
            db.session.query(SecurityDeposit)
            .filter(
                SecurityDeposit.id == deposit_id,
                SecurityDeposit.deleted_at.is_(None),
            )
            .first()
        )
        assert ativo is None

        logs = (
            db.session.query(AuditLog)
            .filter_by(action="delete", module="reservas", entity_id=deposit_id)
            .count()
        )
        assert logs == 1, "Deve existir exatamente 1 AuditLog de delete."


def test_deletar_caucao_paga_rejeita(app, db, reserva_simples):
    """Impede deleção de caução já paga — deve usar devolver() em vez disso."""
    with app.app_context():
        deposit = SecurityDepositService.criar(
            reserva_id=reserva_simples.id,
            amount=100.00,
        )
        SecurityDepositService.pagar(deposit_id=deposit.id)

        with pytest.raises(ValueError, match="já foi paga"):
            SecurityDepositService.deletar(deposit_id=deposit.id)


# ---------------------------------------------------------------------------
# is_asset_under_deposit()
# ---------------------------------------------------------------------------


def test_is_asset_under_deposit_verdadeiro(app, db, reserva_simples, ativo_fisico):
    """Retorna True quando ativo tem caução paga e não devolvida."""
    with app.app_context():
        deposit = SecurityDepositService.criar(
            reserva_id=reserva_simples.id,
            amount=100.00,
            linked_asset_id=ativo_fisico.id,
        )
        SecurityDepositService.pagar(deposit_id=deposit.id)

        assert SecurityDepositService.is_asset_under_deposit(ativo_fisico.id) is True


def test_is_asset_under_deposit_falso_sem_caucao(app, db, ativo_fisico):
    """Retorna False quando ativo não possui caução ativa."""
    with app.app_context():
        assert SecurityDepositService.is_asset_under_deposit(ativo_fisico.id) is False


def test_is_asset_under_deposit_falso_apos_devolucao(
    app, db, reserva_simples, ativo_fisico
):
    """Retorna False após a caução ser devolvida."""
    with app.app_context():
        deposit = SecurityDepositService.criar(
            reserva_id=reserva_simples.id,
            amount=100.00,
            linked_asset_id=ativo_fisico.id,
        )
        SecurityDepositService.pagar(deposit_id=deposit.id)
        SecurityDepositService.devolver(deposit_id=deposit.id)

        assert SecurityDepositService.is_asset_under_deposit(ativo_fisico.id) is False


# ---------------------------------------------------------------------------
# pendente_total()
# ---------------------------------------------------------------------------


def test_pendente_total_soma_caucoes_nao_pagas(app, db, reserva_simples):
    """Calcula corretamente a soma de cauções não pagas do grupo."""
    with app.app_context():
        SecurityDepositService.criar(
            reserva_id=reserva_simples.id,
            amount=100.00,
        )
        SecurityDepositService.criar(
            reserva_id=reserva_simples.id,
            amount=50.00,
        )

        total = SecurityDepositService.pendente_total(reserva_simples.group_id)
        assert total == 150.00


def test_pendente_total_exclui_caucoes_pagas(app, db, reserva_simples):
    """pendente_total não conta cauções já pagas."""
    with app.app_context():
        dep_pago = SecurityDepositService.criar(
            reserva_id=reserva_simples.id,
            amount=100.00,
        )
        SecurityDepositService.pagar(deposit_id=dep_pago.id)

        SecurityDepositService.criar(
            reserva_id=reserva_simples.id,
            amount=75.00,
        )

        total = SecurityDepositService.pendente_total(reserva_simples.group_id)
        assert total == 75.00


def test_pendente_total_zero_sem_caucoes(app, db, reserva_simples):
    """pendente_total retorna 0.0 quando não há cauções pendentes."""
    with app.app_context():
        total = SecurityDepositService.pendente_total(reserva_simples.group_id)
        assert total == 0.0
