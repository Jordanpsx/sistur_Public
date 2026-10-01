# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Testes do AnalyticsService — KPIs e agregações do dashboard de Reservas.

Cobre os métodos portáveis para SQLite (a suíte roda em memória):
ticket médio contratado, estatísticas de antecedência (média/mediana/p90),
série temporal e conversão por origem.
"""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.models import Area
from app.models.pdv import PdvMovimentacaoCaixa, PdvPaymentMethod, PdvSale, PdvShift, TipoMovimentacaoCaixa
from app.models.reservas import Reserva, ReservaOrigin, ReservaSource, ReservaStatus
from app.services.analytics_service import AnalyticsService

HOJE = date(2026, 6, 10)
INICIO = HOJE - timedelta(days=30)


def _reserva(
    db,
    check_in: date,
    total: float = 100.0,
    status=ReservaStatus.PAID,
    origin=ReservaOrigin.WEB,
    customer_name: str = "Cliente Teste",
    customer_document: str = "",
    criado_em: datetime | None = None,
    metadata_info: dict | None = None,
) -> Reserva:
    """Cria e persiste uma Reserva mínima para os testes de analytics.

    Args:
        db: Fixture de banco da suíte.
        check_in: Data de check-in da reserva.
        total: Valor contratado (total_amount).
        status: Status da reserva.
        origin: Origem (WEB/BALCAO).
        customer_name: Nome do cliente.
        customer_document: CPF para a deduplicação de conversão.
        criado_em: Timestamp de criação (para antecedência).
        metadata_info: JSON de metadados (flag walk-in etc.).

    Returns:
        Instância de Reserva persistida (flush, sem commit).
    """
    r = Reserva(
        group_id=str(uuid.uuid4()),
        version=1,
        status=status,
        origin=origin,
        customer_name=customer_name,
        customer_document=customer_document,
        total_amount=Decimal(str(total)),
        check_in_date=check_in,
        check_out_date=check_in + timedelta(days=1),
        metadata_info=metadata_info,
    )
    if criado_em is not None:
        r.criado_em = criado_em
    db.session.add(r)
    db.session.flush()
    return r


# ---------------------------------------------------------------------------
# resumo_periodo — ticket médio contratado
# ---------------------------------------------------------------------------

def test_ticket_medio_usa_receita_contratada(db):
    """Ticket médio = receita contratada ÷ reservas, mesmo sem pagamentos."""
    _reserva(db, HOJE - timedelta(days=2), total=200.0)
    _reserva(db, HOJE - timedelta(days=3), total=100.0)

    resumo = AnalyticsService.resumo_periodo(INICIO, HOJE)

    assert resumo["total_reservas"] == 2
    assert resumo["receita_contratada"] == 300.0
    # Sem transações no ledger, recebida = 0 — o ticket NÃO pode ser 0.
    assert resumo["receita_recebida"] == 0.0
    assert resumo["ticket_medio"] == pytest.approx(150.0)


def test_resumo_exclui_canceladas_por_padrao(db):
    """CANCELED e ARCHIVED_VERSION ficam fora do resumo padrão."""
    _reserva(db, HOJE - timedelta(days=2), total=100.0)
    _reserva(db, HOJE - timedelta(days=2), total=50.0, status=ReservaStatus.CANCELED)
    _reserva(db, HOJE - timedelta(days=2), total=70.0, status=ReservaStatus.ARCHIVED_VERSION)

    resumo = AnalyticsService.resumo_periodo(INICIO, HOJE)

    assert resumo["total_reservas"] == 1
    assert resumo["receita_contratada"] == 100.0


# ---------------------------------------------------------------------------
# antecedencia_stats
# ---------------------------------------------------------------------------

def test_antecedencia_stats_media_mediana_p90(db):
    """Estatísticas calculadas sobre os dias entre criação e check-in."""
    # Antecedências: 0, 10 e 20 dias → média 10, mediana 10.
    for dias in (0, 10, 20):
        check_in = HOJE - timedelta(days=1)
        _reserva(
            db, check_in,
            criado_em=datetime.combine(check_in - timedelta(days=dias), datetime.min.time()),
        )

    stats = AnalyticsService.antecedencia_stats(INICIO, HOJE)

    assert stats["n"] == 3
    assert stats["media"] == pytest.approx(10.0)
    assert stats["mediana"] == pytest.approx(10.0)
    # p90 com interpolação linear entre 10 e 20: 10 + 0.8*10 = 18
    assert stats["p90"] == pytest.approx(18.0)


def test_antecedencia_exclui_walkins_e_retroativas(db):
    """Walk-ins (nome ou flag) e reservas retroativas ficam fora da métrica."""
    check_in = HOJE - timedelta(days=1)
    criado_5d = datetime.combine(check_in - timedelta(days=5), datetime.min.time())

    _reserva(db, check_in, criado_em=criado_5d)  # válida: 5 dias
    _reserva(db, check_in, criado_em=criado_5d, customer_name="Balcão 3")  # walk-in por nome
    _reserva(db, check_in, criado_em=criado_5d, metadata_info={"is_balcao_mode": True})  # walk-in por flag
    _reserva(  # retroativa: criada depois do check-in
        db, check_in,
        criado_em=datetime.combine(check_in + timedelta(days=2), datetime.min.time()),
    )

    stats = AnalyticsService.antecedencia_stats(INICIO, HOJE)

    assert stats["n"] == 1
    assert stats["media"] == pytest.approx(5.0)


def test_antecedencia_sem_dados_retorna_none(db):
    """Período vazio retorna None em todas as estatísticas."""
    stats = AnalyticsService.antecedencia_stats(INICIO, HOJE)
    assert stats == {"media": None, "mediana": None, "p90": None, "n": 0}
    assert AnalyticsService.antecedencia_media(INICIO, HOJE) is None


# ---------------------------------------------------------------------------
# serie_temporal
# ---------------------------------------------------------------------------

def test_serie_temporal_agrega_por_dia(db):
    """Reservas do mesmo dia somam count e receita; dias vazios não aparecem."""
    d1 = HOJE - timedelta(days=5)
    d2 = HOJE - timedelta(days=2)
    _reserva(db, d1, total=100.0)
    _reserva(db, d1, total=50.0)
    _reserva(db, d2, total=70.0)

    serie = AnalyticsService.serie_temporal(INICIO, HOJE)

    assert len(serie) == 2
    assert serie[0] == {"dia": d1.isoformat(), "count": 2, "receita": 150.0}
    assert serie[1] == {"dia": d2.isoformat(), "count": 1, "receita": 70.0}


def test_serie_temporal_respeita_filtro_de_status(db):
    """Filtro explícito de status restringe a série."""
    d = HOJE - timedelta(days=3)
    _reserva(db, d, total=100.0, status=ReservaStatus.PAID)
    _reserva(db, d, total=40.0, status=ReservaStatus.PENDING)

    serie = AnalyticsService.serie_temporal(INICIO, HOJE, statuses=["PAID"])

    assert len(serie) == 1
    assert serie[0]["receita"] == 100.0


# ---------------------------------------------------------------------------
# conversao_por_origem
# ---------------------------------------------------------------------------

def test_conversao_por_origem_separa_canais(db):
    """Cada origem tem sua própria taxa; PENDING/CANCELED não convertem."""
    d = HOJE - timedelta(days=2)
    # WEB: 2 intenções, 1 convertida (50%)
    _reserva(db, d, origin=ReservaOrigin.WEB, status=ReservaStatus.PAID,
             customer_document="11111111111")
    _reserva(db, d, origin=ReservaOrigin.WEB, status=ReservaStatus.PENDING,
             customer_document="22222222222")
    # BALCAO: 1 intenção convertida (100%)
    _reserva(db, d, origin=ReservaOrigin.BALCAO, status=ReservaStatus.COMPLETED,
             customer_document="33333333333")

    resultado = AnalyticsService.conversao_por_origem(INICIO, HOJE)
    por_origem = {r["origem"]: r for r in resultado}

    assert por_origem["WEB"]["total_intents"] == 2
    assert por_origem["WEB"]["converted_intents"] == 1
    assert por_origem["WEB"]["taxa"] == pytest.approx(50.0)
    assert por_origem["BALCAO"]["taxa"] == pytest.approx(100.0)


def test_conversao_por_origem_deduplica_por_cpf_e_data(db):
    """Mesmo CPF + mesma data de check-in = uma única intenção."""
    d = HOJE - timedelta(days=2)
    # Cliente tentou (PENDING) e depois pagou (PAID) — 1 intenção convertida.
    _reserva(db, d, origin=ReservaOrigin.WEB, status=ReservaStatus.PENDING,
             customer_document="44444444444")
    _reserva(db, d, origin=ReservaOrigin.WEB, status=ReservaStatus.PAID,
             customer_document="44444444444")

    resultado = AnalyticsService.conversao_por_origem(INICIO, HOJE)
    web = next(r for r in resultado if r["origem"] == "WEB")

    assert web["total_intents"] == 1
    assert web["converted_intents"] == 1
    assert web["taxa"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# PDV — vendas de balcão e movimentações de caixa
# ---------------------------------------------------------------------------

def _source(db, name: str = "Cachoeira") -> ReservaSource:
    """Cria um venue mínimo para ancorar PdvShift."""
    s = ReservaSource(name=name)
    db.session.add(s)
    db.session.flush()
    return s


def _shift(db, source_id: int, operador_id: int = 1) -> PdvShift:
    """Cria um turno de caixa mínimo (não precisa estar OPEN para analytics)."""
    shift = PdvShift(
        source_id=source_id,
        operador_id=operador_id,
        opening_float_amount=Decimal("0"),
    )
    db.session.add(shift)
    db.session.flush()
    return shift


def _sale(
    db,
    shift_id: int,
    payment_method: PdvPaymentMethod,
    total: float,
    criado_em: datetime,
) -> PdvSale:
    """Cria uma venda de PDV mínima na data informada."""
    sale = PdvSale(
        shift_id=shift_id,
        client_sale_uuid=str(uuid.uuid4()),
        payment_method=payment_method,
        amount_total=Decimal(str(total)),
        criado_em=criado_em,
    )
    db.session.add(sale)
    db.session.flush()
    return sale


def _movimentacao(
    db,
    shift_id: int,
    tipo: TipoMovimentacaoCaixa,
    valor: float,
    data_hora: datetime,
    centro_de_custo_id: int | None = None,
    funcionario_id: int = 1,
) -> PdvMovimentacaoCaixa:
    """Cria uma movimentação de caixa mínima na data informada."""
    mov = PdvMovimentacaoCaixa(
        caixa_turno_id=shift_id,
        tipo_movimentacao=tipo,
        valor=Decimal(str(valor)),
        funcionario_id=funcionario_id,
        centro_de_custo_id=centro_de_custo_id,
        descricao="Movimentação de teste",
        data_hora=data_hora,
    )
    db.session.add(mov)
    db.session.flush()
    return mov


def test_pdv_resumo_periodo_soma_vendas(db):
    """total_vendas/receita_pdv/ticket_medio_pdv somam PdvSale.amount_total."""
    source = _source(db)
    shift = _shift(db, source.id)
    d = datetime.combine(HOJE - timedelta(days=2), datetime.min.time())
    _sale(db, shift.id, PdvPaymentMethod.CASH, 100.0, d)
    _sale(db, shift.id, PdvPaymentMethod.EXTERNAL_PIX, 50.0, d)

    resumo = AnalyticsService.pdv_resumo_periodo(INICIO, HOJE)

    assert resumo["total_vendas"] == 2
    assert resumo["receita_pdv"] == 150.0
    assert resumo["ticket_medio_pdv"] == pytest.approx(75.0)


def test_pdv_resumo_periodo_respeita_source_id(db):
    """Venda de outro venue não entra quando source_id filtra."""
    cachoeira = _source(db, "Cachoeira")
    outro = _source(db, "Outro Venue")
    shift_a = _shift(db, cachoeira.id)
    shift_b = _shift(db, outro.id)
    d = datetime.combine(HOJE - timedelta(days=2), datetime.min.time())
    _sale(db, shift_a.id, PdvPaymentMethod.CASH, 100.0, d)
    _sale(db, shift_b.id, PdvPaymentMethod.CASH, 999.0, d)

    resumo = AnalyticsService.pdv_resumo_periodo(INICIO, HOJE, source_id=cachoeira.id)

    assert resumo["total_vendas"] == 1
    assert resumo["receita_pdv"] == 100.0


def test_pdv_por_metodo_pagamento_agrupa(db):
    """Agrega count/receita por PdvPaymentMethod."""
    source = _source(db)
    shift = _shift(db, source.id)
    d = datetime.combine(HOJE - timedelta(days=2), datetime.min.time())
    _sale(db, shift.id, PdvPaymentMethod.CASH, 100.0, d)
    _sale(db, shift.id, PdvPaymentMethod.CASH, 50.0, d)
    _sale(db, shift.id, PdvPaymentMethod.EXTERNAL_PIX, 30.0, d)

    resultado = AnalyticsService.pdv_por_metodo_pagamento(INICIO, HOJE)
    por_metodo = {r["metodo"]: r for r in resultado}

    assert por_metodo["CASH"]["count"] == 2
    assert por_metodo["CASH"]["receita"] == 150.0
    assert por_metodo["EXTERNAL_PIX"]["receita"] == 30.0


def test_movimentacoes_caixa_resumo_calcula_saldo(db):
    """Sangria (3 tipos de saída) subtrai; suprimento soma; saldo = suprimento − sangria."""
    source = _source(db)
    shift = _shift(db, source.id)
    d = datetime.combine(HOJE - timedelta(days=2), datetime.min.time())
    _movimentacao(db, shift.id, TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE, 40.0, d)
    _movimentacao(db, shift.id, TipoMovimentacaoCaixa.SUPRIMENTO, 15.0, d)

    resumo = AnalyticsService.movimentacoes_caixa_resumo(INICIO, HOJE)

    assert resumo["total_sangria"] == 40.0
    assert resumo["total_suprimento"] == 15.0
    assert resumo["saldo_liquido"] == pytest.approx(-25.0)
    assert resumo["count_movimentacoes"] == 2


def test_movimentacoes_caixa_por_tipo_agrupa(db):
    """Agrega count/valor por TipoMovimentacaoCaixa."""
    source = _source(db)
    shift = _shift(db, source.id)
    d = datetime.combine(HOJE - timedelta(days=2), datetime.min.time())
    _movimentacao(db, shift.id, TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE, 40.0, d)
    _movimentacao(db, shift.id, TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE, 10.0, d)

    resultado = AnalyticsService.movimentacoes_caixa_por_tipo(INICIO, HOJE)

    assert len(resultado) == 1
    assert resultado[0]["tipo"] == "RECOLHIMENTO_COFRE"
    assert resultado[0]["count"] == 2
    assert resultado[0]["valor"] == 50.0


def test_movimentacoes_caixa_por_centro_custo_so_considera_pagamento_despesa(db):
    """Só PAGAMENTO_DESPESA aparece — RECOLHIMENTO_COFRE não tem centro de custo."""
    source = _source(db)
    shift = _shift(db, source.id)
    area = Area(name="Administrativo", slug="administrativo")
    db.session.add(area)
    db.session.flush()
    d = datetime.combine(HOJE - timedelta(days=2), datetime.min.time())
    _movimentacao(db, shift.id, TipoMovimentacaoCaixa.PAGAMENTO_DESPESA, 30.0, d, centro_de_custo_id=area.id)
    _movimentacao(db, shift.id, TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE, 20.0, d)

    resultado = AnalyticsService.movimentacoes_caixa_por_centro_custo(INICIO, HOJE)

    assert len(resultado) == 1
    assert resultado[0]["centro_custo"] == "Administrativo"
    assert resultado[0]["valor"] == 30.0
    assert resultado[0]["count"] == 1
