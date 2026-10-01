# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Testes para o ReservaService.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.reservas import (
    BillingType,
    Reserva,
    ReservaCategory,
    ReservaGroup,
    ReservaItem,
    ReservaItemLink,
    ReservaResource,
    ReservaSource,
    ReservaStatus,
    ReservaStatusConfig,
    ResourceNotice,
)
from app.extensions import db as _db
from app.services.blacklist_service import BlacklistService, ClientBlacklistedError
from app.services.reserva_service import ReservaService, ResourceNoticeBlockedError
from app.services.resource_notice_service import ResourceNoticeService
from app.core.audit import AuditLog

@pytest.fixture
def reserva_item(db):
    """Cria um ReservaItem fixo para os testes.

    No modelo 3-tier atual, ReservaResource não fica mais ligado ao ReservaItem
    (catálogo de tarifas) — o recurso físico é escolhido no pedido e registrado
    em ReservaItemLink.resource_id. Essa fixture cria um recurso no mesmo source
    apenas para cenários que precisem referenciá-lo diretamente em outros testes.
    """
    source = ReservaSource(name="Cachoeira")
    cat = ReservaCategory(name="Day Use", source=source)
    ReservaResource(name="Acesso", stock_quantity=1, source=source)

    item = ReservaItem(
        name="Chalé 1",
        billing_type="PER_DAY",
        price=150.00,
        requires_deposit=False,
        category=cat,
    )
    db.session.add(cat)
    db.session.add(item)
    db.session.commit()
    return item

def test_criar_reserva_sucesso(db, reserva_item):
    """Testa a criação de uma reserva básica."""
    payload = [{
        "item_id": reserva_item.id,
        "quantity": 1,
        "price_override": "150.00"
    }]
    
    check_in = date.today()
    check_out = check_in + timedelta(days=1)
    
    reserva = ReservaService.criar(
        source_name="Cachoeira",
        category_name="Day Use",
        customer_name="João Silva",
        customer_document="12345678901",
        origin="WEB",
        check_in_date=check_in,
        check_out_date=check_out,
        items_payload=payload,
        metadata_info={"pet_name": "Rex"}
    )
    
    # 1. Assert DB Creation
    assert reserva.id is not None
    assert reserva.customer_name == "João Silva"
    assert reserva.metadata_info == {"pet_name": "Rex"}
    
    # 2. Assert Audit Log (Rule #6)
    logs = db.session.query(AuditLog).filter_by(action="create", module="reservas", entity_id=reserva.id).count()
    assert logs == 1


def test_listar_filtra_por_periodo_de_checkin(db, reserva_item):
    """listar() com check_in_de/check_in_ate restringe pelo período de check-in."""
    payload = [{"item_id": reserva_item.id, "quantity": 1, "price_override": "150.00"}]

    reserva_julho = ReservaService.criar(
        source_name="Cachoeira",
        category_name="Day Use",
        customer_name="Cliente Julho",
        customer_document="12345678901",
        origin="WEB",
        check_in_date=date(2026, 7, 10),
        check_out_date=date(2026, 7, 11),
        items_payload=payload,
    )
    ReservaService.criar(
        source_name="Cachoeira",
        category_name="Day Use",
        customer_name="Cliente Junho",
        customer_document="98765432100",
        origin="WEB",
        check_in_date=date(2026, 6, 10),
        check_out_date=date(2026, 6, 11),
        items_payload=payload,
    )

    reservas, total = ReservaService.listar(
        check_in_de=date(2026, 7, 1),
        check_in_ate=date(2026, 7, 31),
    )

    assert total == 1
    assert reservas[0].id == reserva_julho.id


def test_listar_filtra_por_item_nome(db, reserva_item):
    """listar() com item_nome só retorna reservas com aquele item/tarifa ativo."""
    outro_item = ReservaItem(
        name="Meia",
        billing_type="PER_DAY",
        price=75.00,
        requires_deposit=False,
        category_id=reserva_item.category_id,
    )
    db.session.add(outro_item)
    db.session.commit()

    check_in = date.today()
    check_out = check_in + timedelta(days=1)

    reserva_meia = ReservaService.criar(
        source_name="Cachoeira",
        category_name="Day Use",
        customer_name="Cliente Meia",
        customer_document="12345678901",
        origin="WEB",
        check_in_date=check_in,
        check_out_date=check_out,
        items_payload=[{"item_id": outro_item.id, "quantity": 1, "price_override": "75.00"}],
    )
    ReservaService.criar(
        source_name="Cachoeira",
        category_name="Day Use",
        customer_name="Cliente Inteira",
        customer_document="98765432100",
        origin="WEB",
        check_in_date=check_in,
        check_out_date=check_out,
        items_payload=[{"item_id": reserva_item.id, "quantity": 1, "price_override": "150.00"}],
    )

    reservas, total = ReservaService.listar(item_nome="Meia")

    assert total == 1
    assert reservas[0].id == reserva_meia.id


def test_criar_reserva_mesmo_dia_apos_cutoff_falha(db, reserva_item):
    """Categoria com same_day_cutoff_time deve recusar check-in hoje após o horário via WEB."""
    reserva_item.category.same_day_cutoff_time = time(0, 0)
    db.session.commit()

    payload = [{
        "item_id": reserva_item.id,
        "quantity": 1,
        "price_override": "150.00",
    }]

    with pytest.raises(ValueError, match="só são aceitas até"):
        ReservaService.criar(
            source_name="Cachoeira",
            category_name="Day Use",
            customer_name="João Silva",
            customer_document="12345678901",
            origin="WEB",
            check_in_date=date.today(),
            check_out_date=date.today() + timedelta(days=1),
            items_payload=payload,
        )


def test_criar_reserva_dia_seguinte_com_cutoff_sucesso(db, reserva_item):
    """Cutoff de mesmo dia não deve afetar reservas com check-in futuro."""
    reserva_item.category.same_day_cutoff_time = time(0, 0)
    db.session.commit()

    payload = [{
        "item_id": reserva_item.id,
        "quantity": 1,
        "price_override": "150.00",
    }]

    reserva = ReservaService.criar(
        source_name="Cachoeira",
        category_name="Day Use",
        customer_name="João Silva",
        customer_document="12345678901",
        origin="WEB",
        check_in_date=date.today() + timedelta(days=1),
        check_out_date=date.today() + timedelta(days=2),
        items_payload=payload,
    )
    assert reserva.id is not None


def test_criar_reserva_balcao_ignora_cutoff_mesmo_dia(db, reserva_item):
    """Vendas de balcão (BALCAO) não são bloqueadas pelo cutoff de mesmo dia."""
    reserva_item.category.same_day_cutoff_time = time(0, 0)
    db.session.commit()

    payload = [{
        "item_id": reserva_item.id,
        "quantity": 1,
        "price_override": "150.00",
    }]

    reserva = ReservaService.criar(
        source_name="Cachoeira",
        category_name="Day Use",
        customer_name="João Silva",
        customer_document="12345678901",
        origin="BALCAO",
        check_in_date=date.today(),
        check_out_date=date.today() + timedelta(days=1),
        items_payload=payload,
    )
    assert reserva.id is not None


def test_criar_reserva_bloqueada_por_aviso_source_falha(db, reserva_item):
    """Aviso com blocks_source=True deve impedir a criação de reserva no período."""
    source = reserva_item.category.source
    check_in = date.today()
    check_out = check_in + timedelta(days=1)

    ResourceNoticeService.criar(
        event_name="Manutenção geral",
        start_date=check_in,
        end_date=check_out,
        blocks_source=True,
        source_id=source.id,
    )

    payload = [{
        "item_id": reserva_item.id,
        "quantity": 1,
        "price_override": "150.00",
    }]

    with pytest.raises(ValueError, match="Reservas bloqueadas"):
        ReservaService.criar(
            source_name="Cachoeira",
            category_name="Day Use",
            customer_name="João Silva",
            customer_document="12345678901",
            origin="WEB",
            check_in_date=check_in,
            check_out_date=check_out,
            items_payload=payload,
        )


def test_criar_reserva_bloqueada_por_aviso_items_falha(db, reserva_item):
    """Aviso com blocks_items=True (todos os itens) deve impedir a reserva do item."""
    source = reserva_item.category.source
    check_in = date.today()
    check_out = check_in + timedelta(days=1)

    ResourceNoticeService.criar(
        event_name="Reforma do chalé",
        start_date=check_in,
        end_date=check_out,
        blocks_items=True,
        source_id=source.id,
    )

    payload = [{
        "item_id": reserva_item.id,
        "quantity": 1,
        "price_override": "150.00",
    }]

    with pytest.raises(ValueError, match="está bloqueado"):
        ReservaService.criar(
            source_name="Cachoeira",
            category_name="Day Use",
            customer_name="João Silva",
            customer_document="12345678901",
            origin="WEB",
            check_in_date=check_in,
            check_out_date=check_out,
            items_payload=payload,
        )


def test_criar_reserva_fora_periodo_aviso_sucesso(db, reserva_item):
    """Aviso ativo em outro período não deve afetar reserva fora dessa janela."""
    source = reserva_item.category.source
    check_in = date.today()
    check_out = check_in + timedelta(days=1)

    ResourceNoticeService.criar(
        event_name="Manutenção geral",
        start_date=check_in + timedelta(days=30),
        end_date=check_in + timedelta(days=31),
        blocks_source=True,
        source_id=source.id,
    )

    payload = [{
        "item_id": reserva_item.id,
        "quantity": 1,
        "price_override": "150.00",
    }]

    reserva = ReservaService.criar(
        source_name="Cachoeira",
        category_name="Day Use",
        customer_name="João Silva",
        customer_document="12345678901",
        origin="WEB",
        check_in_date=check_in,
        check_out_date=check_out,
        items_payload=payload,
    )
    assert reserva.id is not None


def test_criar_reserva_sem_item_falha(db):
    """Testa falha se nenhum item for passado."""
    check_in = date.today()
    with pytest.raises(ValueError, match="A reserva deve conter ao menos um item."):
        ReservaService.criar(
            source_name="Cachoeira",
            category_name="Day Use",
            customer_name="João Silva",
            customer_document="12345678901",
            origin="WEB",
            check_in_date=check_in,
            check_out_date=None,
            items_payload=[],
        )


def test_criar_reserva_cliente_lista_negra_falha(db, reserva_item):
    """Testa bloqueio definitivo de reserva para cliente banido."""
    source = reserva_item.category.source
    BlacklistService.criar(
        name="João Silva",
        cpf="123.456.789-01",
        block_by_cpf=True,
        source_ids=[source.id],
    )

    payload = [{
        "item_id": reserva_item.id,
        "quantity": 1,
        "price_override": "150.00",
    }]

    with pytest.raises(ClientBlacklistedError, match="Cliente sem autorização"):
        ReservaService.criar(
            source_name=source.name,
            category_name=reserva_item.category.name,
            customer_name="João Silva",
            customer_document="12345678901",
            origin="WEB",
            check_in_date=date.today(),
            check_out_date=date.today() + timedelta(days=1),
            items_payload=payload,
        )

    total_reservas = db.session.query(Reserva).count()
    assert total_reservas == 0


def test_blacklist_criar_registra_auditoria(db, reserva_item):
    """Testa criação de banimento com auditoria."""
    source = reserva_item.category.source

    entry = BlacklistService.criar(
        name="Maria Souza",
        cpf="111.222.333-44",
        phone="(11) 99999-0000",
        block_by_cpf=True,
        block_by_phone=True,
        source_ids=[source.id],
    )

    assert entry.id is not None
    assert entry.cpf == "11122233344"
    assert entry.phone == "11999990000"
    logs = db.session.query(AuditLog).filter_by(
        action="create",
        module="reservas_blacklist",
        entity_id=entry.id,
    ).count()
    assert logs == 1


def test_blacklist_criar_sem_criterio_falha(db, reserva_item):
    """Testa falha quando nenhum critério de bloqueio é selecionado."""
    source = reserva_item.category.source

    with pytest.raises(ValueError, match="Selecione ao menos um critério"):
        BlacklistService.criar(
            name="Maria Souza",
            cpf="11122233344",
            block_by_name=False,
            block_by_cpf=False,
            block_by_phone=False,
            block_by_email=False,
            source_ids=[source.id],
        )


def test_blacklist_deletar_nao_encontrado_falha(db):
    """Testa falha ao remover banimento inexistente."""
    with pytest.raises(ValueError, match="não encontrado"):
        BlacklistService.deletar(9999)

def test_detalhe_sucesso(db, reserva_item):
    """Testa buscar o detalhe de uma reserva."""
    payload = [{
        "item_id": reserva_item.id,
        "quantity": 1,
        "price_override": "150.00"
    }]
    
    check_in = date.today()
    check_out = check_in + timedelta(days=1)
    reserva = ReservaService.criar(
        source_name="Cachoeira",
        category_name="Day Use",
        customer_name="João Silva",
        customer_document="12345678901",
        origin="WEB",
        check_in_date=check_in,
        check_out_date=check_out,
        items_payload=payload,
    )
    
    result = ReservaService.detalhe(reserva.id)
    assert result.id == reserva.id
    assert result.customer_name == "João Silva"

def test_detalhe_nao_encontrado(db):
    """Testa buscar uma reserva inexistente."""
    with pytest.raises(ValueError, match="Reserva não encontrada."):
        ReservaService.detalhe(9999)


# ---------------------------------------------------------------------------
# Testes: deletar()
# ---------------------------------------------------------------------------

import uuid as _uuid


def _criar_reserva_simples(db):
    """Cria uma Reserva mínima via ORM para uso nos testes de deletar."""
    source = ReservaSource(name=f"Venue-{_uuid.uuid4().hex[:6]}")
    cat = ReservaCategory(name="Day Use", source=source)
    db.session.add_all([source, cat])
    db.session.flush()

    from app.models.reservas import ReservaOrigin
    reserva = Reserva(
        group_id=str(_uuid.uuid4()),
        version=1,
        source_id=source.id,
        category_id=cat.id,
        customer_name="João Silva",
        customer_document="12345678901",
        origin=ReservaOrigin.WEB,
        status=ReservaStatus.PENDING,
        check_in_date=date.today(),
        check_out_date=date.today() + timedelta(days=1),
    )
    db.session.add(reserva)
    db.session.commit()
    return reserva


def test_deletar_reserva_sucesso(db):
    """Soft-delete define deleted_at em todas as versões e registra AuditLog."""
    reserva = _criar_reserva_simples(db)
    group_id = reserva.group_id

    ReservaService.deletar(reserva.id, ator_id=None)

    versoes = db.session.query(Reserva).filter(Reserva.group_id == group_id).all()
    assert all(v.deleted_at is not None for v in versoes)

    logs = db.session.query(AuditLog).filter_by(
        action="delete", module="reservas", entity_id=reserva.id
    ).count()
    assert logs == 1


def test_deletar_reserva_nao_encontrada(db):
    """Deletar reserva inexistente deve levantar ValueError."""
    with pytest.raises(ValueError, match="Reserva não encontrada."):
        ReservaService.deletar(9999)


def test_deletar_reserva_ja_deletada(db):
    """Segunda chamada a deletar() para a mesma reserva deve levantar ValueError."""
    reserva = _criar_reserva_simples(db)

    ReservaService.deletar(reserva.id)

    with pytest.raises(ValueError, match="Reserva não encontrada."):
        ReservaService.deletar(reserva.id)




def test_catalogo_inclui_internal_slug(db):
    """Verifica que catalogo() retorna internal_slug no JSON de itens, incluindo None para itens sem slug."""
    from app.models.reservas import BillingType

    source = ReservaSource(name="CatalogoTeste", is_active=True)
    db.session.add(source)
    db.session.flush()

    cat = ReservaCategory(source_id=source.id, name="Cat")
    db.session.add(cat)
    db.session.flush()

    item_com_slug = ReservaItem(
        category_id=cat.id,
        name="Inteira",
        price=100.0,
        billing_type=BillingType.FIXED,
        internal_slug="dayuse_inteira",
    )
    item_sem_slug = ReservaItem(
        category_id=cat.id,
        name="Meia",
        price=50.0,
        billing_type=BillingType.FIXED,
        internal_slug=None,
    )
    db.session.add_all([item_com_slug, item_sem_slug])
    db.session.commit()

    resultado = ReservaService.catalogo(source_id=source.id)

    assert len(resultado) == 1
    items_out = resultado[0]["categories"][0]["items"]
    slugs = {i["name"]: i["internal_slug"] for i in items_out}

    assert slugs["Inteira"] == "dayuse_inteira"
    assert slugs["Meia"] is None
    # price deve ser float, nunca Decimal
    for item_out in items_out:
        assert isinstance(item_out["price"], float)


# ---------------------------------------------------------------------------
# Fixture — grupo de churrasqueiras para testes de atribuição de recursos
# ---------------------------------------------------------------------------


@pytest.fixture
def churrasqueiras(db):
    """Cria cenário com 3 churrasqueiras (A1, B2, C3) num grupo compartilhado.

    Estrutura:
        Source "Cachoeira"
          └── Category "Day Use"
                └── Item "Churrasqueira pequena" (FIXED R$100, group=grupo)
        Group "Churrasqueiras" (source=Cachoeira)
          ├── Resource "Churrasqueira A1" (stock=1)
          ├── Resource "Churrasqueira B2" (stock=1)
          └── Resource "Churrasqueira C3" (stock=1)
    """
    source = ReservaSource(name="Cachoeira")
    cat = ReservaCategory(name="Day Use", source=source)
    db.session.add(cat)
    db.session.flush()

    grupo = ReservaGroup(name="Churrasqueiras", sources=[source])
    db.session.add(grupo)
    db.session.flush()

    a1 = ReservaResource(
        name="Churrasqueira A1", stock_quantity=1,
        source=source, group_id=grupo.id,
    )
    b2 = ReservaResource(
        name="Churrasqueira B2", stock_quantity=1,
        source=source, group_id=grupo.id,
    )
    c3 = ReservaResource(
        name="Churrasqueira C3", stock_quantity=1,
        source=source, group_id=grupo.id,
    )
    db.session.add_all([a1, b2, c3])
    db.session.flush()

    item = ReservaItem(
        name="Churrasqueira pequena",
        billing_type=BillingType.FIXED,
        price=100.0,
        category=cat,
        group_id=grupo.id,
        requires_resource=True,
    )
    db.session.add(item)
    db.session.commit()

    return {
        "source": source,
        "cat": cat,
        "grupo": grupo,
        "a1": a1,
        "b2": b2,
        "c3": c3,
        "item": item,
    }


def _criar_reserva_churrasqueira(source_name, category_name, item_id,
                                  resource_id=None, customer="João Silva",
                                  check_in=None, check_out=None):
    """Helper que monta e chama ReservaService.criar() para 1 item de churrasqueira (FIXED R$100)."""
    today = date.today()
    payload = {
        "item_id": item_id,
        "quantity": 1,
        "price_override": "100.00",
    }
    if resource_id is not None:
        payload["resource_id"] = resource_id

    return ReservaService.criar(
        source_name=source_name,
        category_name=category_name,
        customer_name=customer,
        customer_document="12345678901",
        origin="BALCAO",
        check_in_date=check_in or today,
        check_out_date=check_out,
        items_payload=[payload],
    )


# ---------------------------------------------------------------------------
# Testes de regressão — atribuição de recurso físico (churrasqueira)
# ---------------------------------------------------------------------------


def test_resource_id_explicito_preservado(db, churrasqueiras):
    """Regressão: resource_id explícito no payload deve ser honrado integralmente.

    Cenário que gerou o bug original:
    - Cliente escolheu B2 no wizard
    - PHP não enviava churrasqueira_resource_id no campo sanitizado
    - Python recebia resource_id=None e auto-atribuía A1 (primeira alfabeticamente)

    Este teste garante que, quando resource_id=B2 é enviado, o link criado
    aponta para B2, não para qualquer outro recurso.
    """
    g = churrasqueiras

    reserva = _criar_reserva_churrasqueira(
        source_name="Cachoeira",
        category_name="Day Use",
        item_id=g["item"].id,
        resource_id=g["b2"].id,  # escolha explícita de B2
    )

    link = reserva.items[0]
    assert link.resource_id == g["b2"].id, (
        f"Esperado B2 (id={g['b2'].id}), mas link.resource_id={link.resource_id}. "
        "Provável causa: sanitizador PHP não está enviando churrasqueira_resource_id."
    )


def test_resource_id_explicito_nao_e_substituido_pelo_primeiro_alfabetico(db, churrasqueiras):
    """Auto-assignment NÃO deve rodar quando resource_id explícito é fornecido.

    Garante que o bloco 'if resource_id is None and item.group_id is not None'
    não é atingido quando o cliente já escolheu um recurso.
    """
    g = churrasqueiras

    # Escolhe C3 — o último alfabeticamente. Auto-assignment daria A1.
    reserva = _criar_reserva_churrasqueira(
        source_name="Cachoeira",
        category_name="Day Use",
        item_id=g["item"].id,
        resource_id=g["c3"].id,
    )

    assert reserva.items[0].resource_id == g["c3"].id


def test_auto_assignment_sem_resource_id_pega_primeiro_alfabetico(db, churrasqueiras):
    """Sem resource_id: auto-assignment deve selecionar A1 (primeiro alfabeticamente).

    Este comportamento é o esperado quando o cliente não especifica uma
    churrasqueira e o sistema escolhe automaticamente.
    """
    g = churrasqueiras

    reserva = _criar_reserva_churrasqueira(
        source_name="Cachoeira",
        category_name="Day Use",
        item_id=g["item"].id,
        resource_id=None,  # sem escolha explícita
    )

    assert reserva.items[0].resource_id == g["a1"].id, (
        "Auto-assignment deve selecionar A1 (primeiro alfabeticamente) quando não há escolha explícita."
    )


def test_auto_assignment_pula_recurso_ocupado(db, churrasqueiras):
    """Auto-assignment deve pular A1 (já reservada) e atribuir B2.

    Simula o cenário em que dois clientes chegam sem escolha explícita
    de churrasqueira no mesmo dia: o segundo deve receber B2, não A1.
    """
    g = churrasqueiras
    today = date.today()

    # Primeira reserva ocupa A1
    _criar_reserva_churrasqueira(
        source_name="Cachoeira",
        category_name="Day Use",
        item_id=g["item"].id,
        resource_id=g["a1"].id,
        customer="Cliente 1",
        check_in=today,
    )

    # Segunda reserva sem escolha explícita — A1 está ocupada, deve pegar B2
    reserva2 = _criar_reserva_churrasqueira(
        source_name="Cachoeira",
        category_name="Day Use",
        item_id=g["item"].id,
        resource_id=None,
        customer="Cliente 2",
        check_in=today,
    )

    assert reserva2.items[0].resource_id == g["b2"].id, (
        "Auto-assignment deveria ter pulado A1 (ocupada) e selecionado B2."
    )


def test_dois_itens_mesmo_grupo_recebem_recursos_distintos(db, churrasqueiras):
    """Dois itens no mesmo payload com mesmo group_id devem receber recursos diferentes.

    Garante que seen_resource_ids evita que o segundo item receba o mesmo
    recurso já atribuído ao primeiro item no mesmo request.
    """
    g = churrasqueiras

    # Segundo item no mesmo grupo (nome diferente para respeitar unique constraint)
    item2 = ReservaItem(
        name="Churrasqueira pequena 2",
        billing_type=BillingType.FIXED,
        price=100.0,
        category=g["cat"],
        group_id=g["grupo"].id,
        requires_resource=True,
    )
    db.session.add(item2)
    db.session.commit()

    today = date.today()
    reserva = ReservaService.criar(
        source_name="Cachoeira",
        category_name="Day Use",
        customer_name="Grupo Família",
        customer_document="12345678901",
        origin="BALCAO",
        check_in_date=today,
        check_out_date=None,
        items_payload=[
            {"item_id": g["item"].id, "quantity": 1, "price_override": "100.00"},
            {"item_id": item2.id, "quantity": 1, "price_override": "100.00"},
        ],
    )

    resource_ids = {link.resource_id for link in reserva.items}
    assert len(resource_ids) == 2, (
        "Dois itens do mesmo grupo devem receber recursos físicos distintos."
    )
    # Os dois primeiros alfabeticamente devem ser A1 e B2
    assert g["a1"].id in resource_ids
    assert g["b2"].id in resource_ids


def test_item_sem_grupo_nao_recebe_auto_assignment(db):
    """Item com group_id=None não deve acionar auto-assignment (resource_id fica None).

    Itens como 'Taxa de estacionamento' ou 'Lanche' não têm recurso físico
    e não devem receber um automaticamente.
    """
    source = ReservaSource(name="Parque")
    cat = ReservaCategory(name="Day Use", source=source)
    item = ReservaItem(
        name="Taxa de entrada",
        billing_type=BillingType.FIXED,
        price=50.0,
        category=cat,
        group_id=None,  # sem grupo — sem auto-assignment
    )
    db.session.add_all([cat, item])
    db.session.commit()

    reserva = ReservaService.criar(
        source_name="Parque",
        category_name="Day Use",
        customer_name="Visitante",
        customer_document="12345678901",
        origin="BALCAO",
        check_in_date=date.today(),
        check_out_date=None,
        items_payload=[{"item_id": item.id, "quantity": 1, "price_override": "50.00"}],
    )

    assert reserva.items[0].resource_id is None, (
        "Item sem group_id não deve ter recurso físico atribuído automaticamente."
    )


def test_categoria_dia_unico_normaliza_checkout(db):
    """Categoria single_day_only deve persistir a reserva sem check-out."""
    source = ReservaSource(name="Parque")
    cat = ReservaCategory(name="Day Use", source=source, single_day_only=True)
    item = ReservaItem(
        name="Entrada",
        billing_type=BillingType.FIXED,
        price=50.0,
        category=cat,
    )
    db.session.add_all([cat, item])
    db.session.commit()

    check_in = date.today()
    reserva = ReservaService.criar(
        source_name="Parque",
        category_name="Day Use",
        customer_name="Visitante Teste",
        customer_document="12345678901",
        origin="BALCAO",
        check_in_date=check_in,
        check_out_date=check_in + timedelta(days=1),
        items_payload=[{"item_id": item.id, "quantity": 1, "price_override": "50.00"}],
    )

    assert reserva.check_out_date is None


def test_link_resource_name_acessivel_apos_reserva(db, churrasqueiras):
    """link.resource.name deve ser acessível após a reserva (usado em detalhe.html).

    Garante que o relacionamento ORM ReservaItemLink → ReservaResource funciona
    e que o nome do recurso pode ser exibido na página de detalhes da reserva.
    """
    g = churrasqueiras

    reserva = _criar_reserva_churrasqueira(
        source_name="Cachoeira",
        category_name="Day Use",
        item_id=g["item"].id,
        resource_id=g["b2"].id,
    )

    db.session.refresh(reserva)
    link = reserva.items[0]

    assert link.resource_id == g["b2"].id
    assert link.resource is not None
    assert link.resource.name == "Churrasqueira B2"
    assert link.resource.deleted_at is None


def test_resource_atual_nome_construido_corretamente(db, churrasqueiras):
    """resource_atual_nome deve mapear item_id → resource.name para links ativos.

    Simula a lógica do route admin_editar_reserva_form que constrói o dict
    resource_atual_nome usado no accordion label de reserva_edit.html.
    """
    g = churrasqueiras

    reserva = _criar_reserva_churrasqueira(
        source_name="Cachoeira",
        category_name="Day Use",
        item_id=g["item"].id,
        resource_id=g["b2"].id,
    )

    db.session.refresh(reserva)

    # Reproduz exatamente a lógica do route
    resource_atual_nome: dict[int, str] = {}
    for link in reserva.items:
        if link.deleted_at is None and link.item_id is not None:
            if link.resource_id is not None and link.resource and not link.resource.deleted_at:
                resource_atual_nome[link.item_id] = link.resource.name

    assert resource_atual_nome == {g["item"].id: "Churrasqueira B2"}, (
        "resource_atual_nome deve mapear item_id → nome do recurso para exibição no accordion."
    )


# ---------------------------------------------------------------------------
# timeline_mensal
# ---------------------------------------------------------------------------

def _setup_timeline_source(db):
    """Cria fonte, categoria, recurso e item básicos para testes de timeline."""
    source = ReservaSource(name="Camping Teste")
    cat = ReservaCategory(name="Camping", source=source)
    resource = ReservaResource(name="Site A1", stock_quantity=1, source=source)
    item = ReservaItem(
        name="Site Camping",
        billing_type=BillingType.PER_DAY,
        price=80.00,
        requires_deposit=False,
        category=cat,
    )
    db.session.add_all([source, cat, resource, item])
    db.session.commit()
    return {"source": source, "category": cat, "resource": resource, "item": item}


def _criar_reserva_timeline(db, source, category, resource, item, ci, co,
                              status=ReservaStatus.PAID, customer="Cliente Teste"):
    """Cria uma Reserva com ReservaItemLink para uso nos testes de timeline."""
    import uuid as _uuid
    reserva = Reserva(
        group_id=str(_uuid.uuid4()),
        version=1,
        source_id=source.id,
        category_id=category.id,
        customer_name=customer,
        origin="BALCAO",
        status=status,
        check_in_date=datetime(ci.year, ci.month, ci.day),
        check_out_date=datetime(co.year, co.month, co.day) if co else None,
        expires_at=None,
    )
    db.session.add(reserva)
    db.session.flush()

    link = ReservaItemLink(
        reserva_id=reserva.id,
        item_id=item.id,
        resource_id=resource.id,
        quantity=1,
        locked_price=80.00,
    )
    db.session.add(link)
    db.session.commit()
    return reserva


class TestTimelineMensal:

    def test_fonte_inexistente_levanta_value_error(self, app, db):
        """source_id inválido deve levantar ValueError."""
        with app.app_context():
            with pytest.raises(ValueError, match="não encontrada"):
                ReservaService.timeline_mensal(source_id=9999, year=2026, month=4)

    def test_estrutura_basica_retornada(self, app, db):
        """Resultado deve conter source_id, source_name, year, month, num_days, resources."""
        with app.app_context():
            g = _setup_timeline_source(db)
            resultado = ReservaService.timeline_mensal(
                source_id=g["source"].id, year=2026, month=4
            )

            assert resultado["source_id"] == g["source"].id
            assert resultado["source_name"] == "Camping Teste"
            assert resultado["year"] == 2026
            assert resultado["month"] == 4
            assert resultado["num_days"] == 30
            assert isinstance(resultado["resources"], list)

    def test_recurso_sem_reservas_aparece_com_lista_vazia(self, app, db):
        """Recurso existente sem reservas no mês deve aparecer com reservations=[]."""
        with app.app_context():
            g = _setup_timeline_source(db)
            resultado = ReservaService.timeline_mensal(
                source_id=g["source"].id, year=2026, month=4
            )

            assert len(resultado["resources"]) == 1
            assert resultado["resources"][0]["reservations"] == []

    def test_reserva_single_day_marcada_como_check_in(self, app, db):
        """Reserva com check_in == check_out deve ter o dia marcado como CHECK_IN."""
        with app.app_context():
            g = _setup_timeline_source(db)
            _criar_reserva_timeline(
                db, g["source"], g["category"], g["resource"], g["item"],
                ci=date(2026, 4, 10), co=date(2026, 4, 10),
            )

            resultado = ReservaService.timeline_mensal(
                source_id=g["source"].id, year=2026, month=4
            )

            reservations = resultado["resources"][0]["reservations"]
            assert len(reservations) == 1
            assert reservations[0]["days"] == {10: "CHECK_IN"}

    def test_reserva_multi_dia_check_in_stay_check_out(self, app, db):
        """Reserva de 3 dias deve ter CHECK_IN no primeiro, STAY no meio, CHECK_OUT no último."""
        with app.app_context():
            g = _setup_timeline_source(db)
            _criar_reserva_timeline(
                db, g["source"], g["category"], g["resource"], g["item"],
                ci=date(2026, 4, 5), co=date(2026, 4, 7),
            )

            resultado = ReservaService.timeline_mensal(
                source_id=g["source"].id, year=2026, month=4
            )

            days = resultado["resources"][0]["reservations"][0]["days"]
            assert days[5] == "CHECK_IN"
            assert days[6] == "STAY"
            assert days[7] == "CHECK_OUT"

    def test_reserva_que_comeca_antes_do_mes(self, app, db):
        """Reserva que começa em março e termina em abril deve mostrar STAY/CHECK_OUT em abril."""
        with app.app_context():
            g = _setup_timeline_source(db)
            _criar_reserva_timeline(
                db, g["source"], g["category"], g["resource"], g["item"],
                ci=date(2026, 3, 29), co=date(2026, 4, 2),
            )

            resultado = ReservaService.timeline_mensal(
                source_id=g["source"].id, year=2026, month=4
            )

            days = resultado["resources"][0]["reservations"][0]["days"]
            # Dias 1 e 2 de abril visíveis; 3 e 4 de abril são dentro do mes mas > check_out
            assert days[1] == "STAY"
            assert days[2] == "CHECK_OUT"
            assert 3 not in days
            assert 5 not in days  # reserva não chega até abril 5

    def test_reserva_que_termina_apos_o_mes(self, app, db):
        """Reserva que começa em abril e termina em maio deve mostrar CHECK_IN/STAY até dia 30."""
        with app.app_context():
            g = _setup_timeline_source(db)
            _criar_reserva_timeline(
                db, g["source"], g["category"], g["resource"], g["item"],
                ci=date(2026, 4, 28), co=date(2026, 5, 3),
            )

            resultado = ReservaService.timeline_mensal(
                source_id=g["source"].id, year=2026, month=4
            )

            days = resultado["resources"][0]["reservations"][0]["days"]
            assert days[28] == "CHECK_IN"
            assert days[29] == "STAY"
            assert days[30] == "STAY"  # ainda em andamento — não chegou ao check_out
            assert 31 not in days  # abril tem 30 dias

    def test_reserva_cancelada_excluida_do_timeline(self, app, db):
        """Reservas com status CANCELED não devem aparecer no timeline."""
        with app.app_context():
            g = _setup_timeline_source(db)
            _criar_reserva_timeline(
                db, g["source"], g["category"], g["resource"], g["item"],
                ci=date(2026, 4, 5), co=date(2026, 4, 7),
                status=ReservaStatus.CANCELED,
            )

            resultado = ReservaService.timeline_mensal(
                source_id=g["source"].id, year=2026, month=4
            )

            assert resultado["resources"][0]["reservations"] == []

    def test_reserva_archived_excluida_do_timeline(self, app, db):
        """Reservas ARCHIVED_VERSION não devem aparecer no timeline."""
        with app.app_context():
            g = _setup_timeline_source(db)
            _criar_reserva_timeline(
                db, g["source"], g["category"], g["resource"], g["item"],
                ci=date(2026, 4, 5), co=date(2026, 4, 7),
                status=ReservaStatus.ARCHIVED_VERSION,
            )

            resultado = ReservaService.timeline_mensal(
                source_id=g["source"].id, year=2026, month=4
            )

            assert resultado["resources"][0]["reservations"] == []

    def test_reserva_fora_do_mes_nao_incluida(self, app, db):
        """Reserva em outro mês não deve aparecer no timeline do mês consultado."""
        with app.app_context():
            g = _setup_timeline_source(db)
            _criar_reserva_timeline(
                db, g["source"], g["category"], g["resource"], g["item"],
                ci=date(2026, 5, 10), co=date(2026, 5, 12),
            )

            resultado = ReservaService.timeline_mensal(
                source_id=g["source"].id, year=2026, month=4
            )

            assert resultado["resources"][0]["reservations"] == []

    def test_multiplas_reservas_no_mesmo_recurso(self, app, db):
        """Múltiplas reservas no mesmo recurso devem aparecer como entradas distintas."""
        with app.app_context():
            g = _setup_timeline_source(db)
            _criar_reserva_timeline(
                db, g["source"], g["category"], g["resource"], g["item"],
                ci=date(2026, 4, 1), co=date(2026, 4, 3), customer="Cliente A",
            )
            _criar_reserva_timeline(
                db, g["source"], g["category"], g["resource"], g["item"],
                ci=date(2026, 4, 10), co=date(2026, 4, 12), customer="Cliente B",
            )

            resultado = ReservaService.timeline_mensal(
                source_id=g["source"].id, year=2026, month=4
            )

            reservations = resultado["resources"][0]["reservations"]
            assert len(reservations) == 2
            nomes = {r["customer_name"] for r in reservations}
            assert nomes == {"Cliente A", "Cliente B"}

    def test_campos_obrigatorios_na_reserva(self, app, db):
        """Cada entrada de reserva deve conter os campos essenciais para o Gantt."""
        with app.app_context():
            g = _setup_timeline_source(db)
            _criar_reserva_timeline(
                db, g["source"], g["category"], g["resource"], g["item"],
                ci=date(2026, 4, 5), co=date(2026, 4, 7),
            )

            resultado = ReservaService.timeline_mensal(
                source_id=g["source"].id, year=2026, month=4
            )

            r = resultado["resources"][0]["reservations"][0]
            assert "reserva_id" in r
            assert "reserva_group_id" in r
            assert "customer_name" in r
            assert "status" in r
            assert "check_in_date" in r
            assert "check_out_date" in r
            assert "quantity" in r
            assert "days" in r
            assert isinstance(r["days"], dict)


class TestDescontoEfetivoPct:
    """Cobertura de regressão para o bug da reserva #3397: editar uma reserva
    (mesmo que só trocando o status) zerava qualquer desconto que não estivesse
    gravado em desconto_percentual_manual — caso de reservas cujo desconto veio
    embutido no price_override da criação (ex.: cupom do plugin WordPress).
    """

    def _reserva_base(self, item, **overrides):
        import uuid
        defaults = dict(
            group_id=str(uuid.uuid4()),
            version=1,
            source_id=item.category.source_id,
            category_id=item.category_id,
            customer_name="Cliente Teste",
            origin="WEB",
            status=ReservaStatus.PENDING,
            check_in_date=datetime.now(timezone.utc) + timedelta(days=5),
            check_out_date=datetime.now(timezone.utc) + timedelta(days=6),
            subtotal_amount=Decimal("100.00"),
            discount_amount=Decimal("0.00"),
            service_fee_amount=Decimal("0.00"),
            total_amount=Decimal("100.00"),
            desconto_percentual_manual=None,
        )
        defaults.update(overrides)
        reserva = Reserva(**defaults)
        _db.session.add(reserva)
        _db.session.commit()
        return reserva

    def test_usa_percentual_manual_quando_gravado(self, db, reserva_item):
        """Se desconto_percentual_manual está gravado, ele é a fonte da verdade."""
        reserva = self._reserva_base(
            reserva_item,
            subtotal_amount=Decimal("100.00"),
            discount_amount=Decimal("30.00"),
            total_amount=Decimal("70.00"),
            desconto_percentual_manual=Decimal("30.00"),
        )
        assert ReservaService.desconto_efetivo_pct(reserva) == 30.00

    def test_deriva_percentual_quando_manual_ausente(self, db, reserva_item):
        """Reserva sem desconto_percentual_manual mas com discount_amount > 0
        (ex.: origem wordpress_plugin) deve ter o percentual reconstruído a
        partir da razão discount_amount/subtotal_amount — é exatamente o
        caso da reserva #3397 antes do fix."""
        reserva = self._reserva_base(
            reserva_item,
            subtotal_amount=Decimal("305.00"),
            discount_amount=Decimal("152.50"),
            total_amount=Decimal("152.50"),
            desconto_percentual_manual=None,
        )
        assert ReservaService.desconto_efetivo_pct(reserva) == 50.00

    def test_zero_quando_nao_ha_desconto(self, db, reserva_item):
        reserva = self._reserva_base(
            reserva_item,
            subtotal_amount=Decimal("100.00"),
            discount_amount=Decimal("0.00"),
            total_amount=Decimal("100.00"),
            desconto_percentual_manual=None,
        )
        assert ReservaService.desconto_efetivo_pct(reserva) == 0.0

    def test_atualizar_preserva_desconto_derivado_em_edicao_de_status(self, db, reserva_item):
        """Regressão direta do bug: atualizar() com manual_discount_percent
        resolvido explicitamente (como a rota corrigida agora faz) preserva o
        desconto numa edição que só troca o status — sem essa resolução
        explícita, o desconto se perdia porque não havia nada para herdar."""
        reserva = self._reserva_base(
            reserva_item,
            subtotal_amount=Decimal("305.00"),
            discount_amount=Decimal("152.50"),
            total_amount=Decimal("152.50"),
            desconto_percentual_manual=None,
        )
        link = ReservaItemLink(
            reserva_id=reserva.id,
            item_id=reserva_item.id,
            quantity=1,
            locked_price=Decimal("152.50"),
        )
        _db.session.add(link)
        _db.session.commit()

        pct_efetivo = ReservaService.desconto_efetivo_pct(reserva)
        assert pct_efetivo == 50.00

        nova = ReservaService.atualizar(
            reserva.id,
            items_payload=[{
                "item_id": reserva_item.id,
                "quantity": 1,
                "price_override": float(reserva_item.price),
            }],
            status="PAID",
            manual_discount_percent=pct_efetivo,
            ator_id=None,
        )

        assert nova.discount_amount > 0
        assert nova.desconto_percentual_manual == Decimal("50.00")
        assert nova.total_amount < nova.subtotal_amount


# ---------------------------------------------------------------------------
# force_notice_override — override de aviso de bloqueio no balcão
# ---------------------------------------------------------------------------

class TestForceNoticeOverride:
    """Operador do balcão pode forçar a criação de uma reserva mesmo com um
    ResourceNotice bloqueando o período (ex.: manutenção toda quarta-feira),
    diferente de clientes via WEB/BOT, que nunca recebem esse override."""

    def _aviso_bloqueio(self, db, source_id, check_in, check_out=None,
                         blocks_source=True, blocks_items=False, blocked_item_ids=None):
        n = ResourceNotice(
            event_name="Manutenção de teste",
            start_date=check_in,
            end_date=(check_out or check_in),
            source_id=source_id,
            blocks_source=blocks_source,
            blocks_items=blocks_items,
            blocked_item_ids=blocked_item_ids,
        )
        db.session.add(n)
        db.session.commit()
        return n

    def test_sem_override_levanta_resource_notice_blocked_error(self, db, reserva_item):
        check_in = date.today() + timedelta(days=5)
        self._aviso_bloqueio(db, reserva_item.category.source_id, check_in)

        with pytest.raises(ResourceNoticeBlockedError) as exc_info:
            ReservaService.criar(
                source_name="Cachoeira", category_name="Day Use",
                customer_name="Cliente Teste", customer_document=None, origin="BALCAO",
                check_in_date=check_in, check_out_date=check_in + timedelta(days=1),
                items_payload=[{"item_id": reserva_item.id, "quantity": 1, "price_override": "150.00"}],
            )
        assert "Manutenção de teste" in exc_info.value.notice_text

    def test_com_override_cria_normalmente(self, db, reserva_item):
        check_in = date.today() + timedelta(days=5)
        self._aviso_bloqueio(db, reserva_item.category.source_id, check_in)

        reserva = ReservaService.criar(
            source_name="Cachoeira", category_name="Day Use",
            customer_name="Cliente Teste", customer_document=None, origin="BALCAO",
            check_in_date=check_in, check_out_date=check_in + timedelta(days=1),
            items_payload=[{"item_id": reserva_item.id, "quantity": 1, "price_override": "150.00"}],
            force_notice_override=True,
        )
        assert reserva.id is not None

    def test_override_registra_aviso_ignorado_no_metadata(self, db, reserva_item):
        """O aviso ignorado fica registrado para auditoria — não é um bypass silencioso."""
        check_in = date.today() + timedelta(days=5)
        self._aviso_bloqueio(db, reserva_item.category.source_id, check_in)

        reserva = ReservaService.criar(
            source_name="Cachoeira", category_name="Day Use",
            customer_name="Cliente Teste", customer_document=None, origin="BALCAO",
            check_in_date=check_in, check_out_date=check_in + timedelta(days=1),
            items_payload=[{"item_id": reserva_item.id, "quantity": 1, "price_override": "150.00"}],
            force_notice_override=True,
        )
        ignorados = reserva.metadata_info["avisos_bloqueio_ignorados"]
        assert len(ignorados) == 1
        assert ignorados[0]["tipo"] == "blocks_source"
        assert "Manutenção de teste" in ignorados[0]["texto"]

    def test_override_sem_aviso_ativo_nao_adiciona_metadata_vazio(self, db, reserva_item):
        """force_notice_override=True sem nenhum aviso bloqueando não deve
        poluir metadata_info com uma chave vazia."""
        check_in = date.today() + timedelta(days=5)

        reserva = ReservaService.criar(
            source_name="Cachoeira", category_name="Day Use",
            customer_name="Cliente Teste", customer_document=None, origin="BALCAO",
            check_in_date=check_in, check_out_date=check_in + timedelta(days=1),
            items_payload=[{"item_id": reserva_item.id, "quantity": 1, "price_override": "150.00"}],
            force_notice_override=True,
        )
        assert not (reserva.metadata_info or {}).get("avisos_bloqueio_ignorados")

    def test_bloqueio_de_item_especifico_tambem_aceita_override(self, db, reserva_item):
        check_in = date.today() + timedelta(days=5)
        self._aviso_bloqueio(
            db, reserva_item.category.source_id, check_in,
            blocks_source=False, blocks_items=True,
            blocked_item_ids=[reserva_item.id],
        )

        with pytest.raises(ResourceNoticeBlockedError):
            ReservaService.criar(
                source_name="Cachoeira", category_name="Day Use",
                customer_name="Cliente Teste", customer_document=None, origin="BALCAO",
                check_in_date=check_in, check_out_date=check_in + timedelta(days=1),
                items_payload=[{"item_id": reserva_item.id, "quantity": 1, "price_override": "150.00"}],
            )

        reserva = ReservaService.criar(
            source_name="Cachoeira", category_name="Day Use",
            customer_name="Cliente Teste", customer_document=None, origin="BALCAO",
            check_in_date=check_in, check_out_date=check_in + timedelta(days=1),
            items_payload=[{"item_id": reserva_item.id, "quantity": 1, "price_override": "150.00"}],
            force_notice_override=True,
        )
        assert reserva.metadata_info["avisos_bloqueio_ignorados"][0]["tipo"] == "blocks_items"

    def test_sem_force_flag_permanece_bloqueado_por_padrao(self, db, reserva_item):
        """Regressão: o comportamento padrão (sem passar force_notice_override)
        continua bloqueando — a nova flag é opt-in, não muda o caminho existente
        usado por WEB/BOT."""
        check_in = date.today() + timedelta(days=5)
        self._aviso_bloqueio(db, reserva_item.category.source_id, check_in)

        with pytest.raises(ResourceNoticeBlockedError):
            ReservaService.criar(
                source_name="Cachoeira", category_name="Day Use",
                customer_name="Cliente WEB", customer_document=None, origin="WEB",
                check_in_date=check_in, check_out_date=check_in + timedelta(days=1),
                items_payload=[{"item_id": reserva_item.id, "quantity": 1, "price_override": "150.00"}],
            )


class TestCancelarPendentesVencidas:
    """cancelar_pendentes_vencidas() — auto-cancelamento configurável por status."""

    def _reserva_pendente(self, db, reserva_item, *, dias_expirada=0, origin="WEB"):
        check_in = date.today() + timedelta(days=10)
        reserva = ReservaService.criar(
            source_name="Cachoeira", category_name="Day Use",
            customer_name="Cliente Teste", customer_document="12345678901", origin=origin,
            check_in_date=check_in, check_out_date=check_in + timedelta(days=1),
            items_payload=[{"item_id": reserva_item.id, "quantity": 1, "price_override": "150.00"}],
        )
        if dias_expirada:
            reserva.expires_at = datetime.now(timezone.utc) - timedelta(days=dias_expirada)
            db.session.commit()
        return reserva

    def test_sem_prazo_configurado_nao_cancela_nada(self, db, reserva_item):
        """Toda ReservaStatusConfig.auto_cancel_hours nasce None — comportamento
        padrão é não cancelar nada, mesmo com reservas bem vencidas."""
        self._reserva_pendente(db, reserva_item, dias_expirada=30)

        report = ReservaService.cancelar_pendentes_vencidas()

        assert report == {"avaliadas": 0, "canceladas": 0}

    def test_cancela_pendente_vencida_alem_do_prazo(self, db, reserva_item):
        """Com PENDING.auto_cancel_hours=24, uma reserva vencida há 2 dias é
        cancelada — vira nova versão com status CANCELED."""
        reserva = self._reserva_pendente(db, reserva_item, dias_expirada=2)
        cfg = db.session.query(ReservaStatusConfig).filter_by(internal_name="PENDING").one()
        cfg.auto_cancel_hours = 24
        db.session.commit()

        report = ReservaService.cancelar_pendentes_vencidas()

        assert report == {"avaliadas": 1, "canceladas": 1}
        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.ARCHIVED_VERSION
        atual = (
            db.session.query(Reserva)
            .filter_by(group_id=reserva.group_id, status=ReservaStatus.CANCELED)
            .one()
        )
        assert "Cancelamento automático" in atual.metadata_info["motivo_cancelamento"]

    def test_nao_cancela_dentro_do_prazo_configurado(self, db, reserva_item):
        """Vencida há 1h, com prazo de 24h — ainda não é candidata."""
        reserva = self._reserva_pendente(db, reserva_item)
        reserva.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.session.commit()
        cfg = db.session.query(ReservaStatusConfig).filter_by(internal_name="PENDING").one()
        cfg.auto_cancel_hours = 24
        db.session.commit()

        report = ReservaService.cancelar_pendentes_vencidas()

        assert report == {"avaliadas": 0, "canceladas": 0}
        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PENDING

    def test_nao_cancela_status_sem_prazo_configurado(self, db, reserva_item):
        """Configurar PARTIALLY_PAID não afeta PENDING — cada status é independente."""
        reserva = self._reserva_pendente(db, reserva_item, dias_expirada=5)
        cfg = db.session.query(ReservaStatusConfig).filter_by(internal_name="PARTIALLY_PAID").one()
        cfg.auto_cancel_hours = 1
        db.session.commit()

        report = ReservaService.cancelar_pendentes_vencidas()

        assert report == {"avaliadas": 0, "canceladas": 0}
        db.session.refresh(reserva)
        assert reserva.status == ReservaStatus.PENDING


class TestReconciliarPagamentosPendentes:
    """reconciliar_pagamentos_pendentes() — generalização do fallback de webhook."""

    def test_ignora_reserva_sem_payment_id(self, db, reserva_item):
        """Reserva que nunca gerou cobrança não tem o que reconciliar."""
        check_in = date.today() + timedelta(days=10)
        ReservaService.criar(
            source_name="Cachoeira", category_name="Day Use",
            customer_name="Cliente Teste", customer_document="12345678901", origin="WEB",
            check_in_date=check_in, check_out_date=check_in + timedelta(days=1),
            items_payload=[{"item_id": reserva_item.id, "quantity": 1, "price_override": "150.00"}],
        )

        report = ReservaService.reconciliar_pagamentos_pendentes(min_age_hours=0)

        assert report == {"verificadas": 0, "confirmadas": 0, "erros": 0}

    def test_inclui_origem_bot_por_padrao(self, db, reserva_item):
        """Sem a varredura própria do bot, reservas de origem BOT entram na
        rotina generalizada como qualquer outra."""
        check_in = date.today() + timedelta(days=10)
        reserva = ReservaService.criar(
            source_name="Cachoeira", category_name="Day Use",
            customer_name="Cliente Bot", customer_document="12345678901", origin="BOT",
            check_in_date=check_in, check_out_date=check_in + timedelta(days=1),
            items_payload=[{"item_id": reserva_item.id, "quantity": 1, "price_override": "150.00"}],
        )
        reserva.payment_id = "123456789"
        reserva.expires_at = datetime.now(timezone.utc) - timedelta(hours=5)
        db.session.commit()

        report = ReservaService.reconciliar_pagamentos_pendentes(min_age_hours=0)

        assert report["verificadas"] == 1

    def test_min_age_hours_filtra_reservas_recentes(self, db, reserva_item):
        """Uma reserva ainda dentro da janela de tolerância não é candidata —
        dá tempo do webhook chegar pelo caminho normal primeiro."""
        check_in = date.today() + timedelta(days=10)
        reserva = ReservaService.criar(
            source_name="Cachoeira", category_name="Day Use",
            customer_name="Cliente Teste", customer_document="12345678901", origin="WEB",
            check_in_date=check_in, check_out_date=check_in + timedelta(days=1),
            items_payload=[{"item_id": reserva_item.id, "quantity": 1, "price_override": "150.00"}],
        )
        reserva.payment_id = "123456789"
        reserva.expires_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        db.session.commit()

        report = ReservaService.reconciliar_pagamentos_pendentes(min_age_hours=2)

        assert report == {"verificadas": 0, "confirmadas": 0, "erros": 0}
