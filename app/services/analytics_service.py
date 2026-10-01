# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Serviço de análise de dados para o módulo de Reservas.

Fornece consultas agregadas (agrupamento por origem, categoria e itens)
sem nenhuma dependência do Flask — apenas SQLAlchemy e os modelos de domínio.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import Date, String, case, cast, func

from app.core.models import Area
from app.extensions import db
from app.models.payment import PaymentTransaction, TransactionStatus
from app.models.pdv import (
    PdvMovimentacaoCaixa,
    PdvPaymentMethod,
    PdvSale,
    PdvShift,
    TipoMovimentacaoCaixa,
)
from app.models.reservas import (
    Reserva,
    ReservaCategory,
    ReservaItem,
    ReservaItemLink,
    ReservaOrigin,
    ReservaStatus,
)

# Tipos de movimentação que retiram dinheiro físico da gaveta — mesma
# definição de MovimentacaoCaixaService._TIPOS_SAIDA, duplicada aqui de
# propósito: analytics é leitura pura, não deveria depender do módulo de
# escrita do PDV só para reaproveitar uma constante.
_TIPOS_SAIDA_CAIXA = frozenset({
    TipoMovimentacaoCaixa.TRANSFERENCIA_INTERNA,
    TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
    TipoMovimentacaoCaixa.PAGAMENTO_DESPESA,
})

# Status sempre excluídos (ARCHIVED_VERSION é interno de versionamento;
# CANCELED é excluído por padrão mas pode ser incluído via filtro de chips).
_ARCHIVED = ReservaStatus.ARCHIVED_VERSION
_DEFAULT_EXCLUIDOS = [ReservaStatus.ARCHIVED_VERSION, ReservaStatus.CANCELED]

# Status que representam uma intenção NÃO convertida (para a taxa de conversão).
# Qualquer status que não esteja nessa lista é considerado "convertido".
_STATUS_NAO_CONVERTIDOS = ["PENDING", "CANCELED", "ARCHIVED_VERSION"]


def _status_clause(statuses: list[str] | None):
    """Retorna a cláusula SQLAlchemy de filtro de status correta.

    Args:
        statuses: Lista de internal_names selecionados pelo usuário.
                  None = comportamento padrão (exclui CANCELED e ARCHIVED_VERSION).

    Returns:
        Expressão SQLAlchemy para uso em .filter().
    """
    if statuses:
        # Filtro explícito — ARCHIVED_VERSION nunca deve aparecer (é interno)
        clean = [s for s in statuses if s != _ARCHIVED.value]
        return Reserva.status.in_(clean) if clean else Reserva.status.in_(["__none__"])
    return Reserva.status.notin_(_DEFAULT_EXCLUIDOS)


def _apply_source(query, source_id: int | None):
    """Aplica filtro opcional por venue/source a uma query de Reserva.

    Centraliza a lógica para garantir que TODAS as métricas respeitem
    o filtro de venue quando o operador selecionar "Local / Venue" no dashboard.

    Args:
        query: Query SQLAlchemy já construída.
        source_id: ID do venue ou None para não filtrar.

    Returns:
        Query filtrada (ou inalterada se source_id is None).
    """
    if source_id is not None:
        return query.filter(Reserva.source_id == source_id)
    return query


def _calc_growth(atual: float, anterior: float) -> float | None:
    """Calcula o percentual de crescimento entre dois valores.

    Args:
        atual: Valor do período atual.
        anterior: Valor do período anterior (base de comparação).

    Returns:
        Percentual arredondado a 1 casa, ou None quando a base é zero ou negativa.
    """
    if anterior <= 0:
        return None
    return round((atual - anterior) / anterior * 100, 1)


class AnalyticsService:
    """Serviço de análise de dados de reservas.

    Todos os métodos são estáticos e não importam nenhum módulo do Flask.

    Filtros comuns aplicados a todas as consultas:
      - deleted_at IS NULL
      - status conforme _status_clause() — por padrão exclui CANCELED e ARCHIVED_VERSION
      - check_in_date no intervalo solicitado (func.date(), inclusive —
        portável entre MySQL e SQLite, ao contrário de CAST(... AS DATE))
    """

    # ------------------------------------------------------------------
    # Métodos principais
    # ------------------------------------------------------------------

    @staticmethod
    def resumo_periodo(
        data_inicio: date,
        data_fim: date,
        statuses: list[str] | None = None,
        source_id: int | None = None,
    ) -> dict:
        """Calcula KPIs do período com duas visões de receita: contratada e recebida.

        Usa o ledger `PaymentTransaction` como fonte de verdade para dinheiro
        efetivamente recebido, em vez de `Reserva.total_amount` (que representa
        intenção contratual e pode incluir PENDING/PARTIALLY_PAID).

        - `receita_contratada`: SUM(Reserva.total_amount) — valor agendado.
        - `receita_recebida`:   SUM(PaymentTransaction.amount WHERE status=APPROVED)
                                − SUM(PaymentTransaction.amount WHERE status=REFUNDED).
                                É o caixa líquido das reservas cuja check-in cai no período.
        - `receita_reembolsada`: SUM(PaymentTransaction.amount WHERE status=REFUNDED).
        - `ticket_medio`:        receita_contratada / total_reservas (ticket de venda).
                                 Usa a contratada porque o denominador são TODAS as
                                 reservas do período — dividir o caixa recebido por
                                 reservas ainda não pagas produzia um número híbrido
                                 sem significado (nem ticket de venda, nem de caixa).

        Args:
            data_inicio: Data inicial do filtro (inclusive).
            data_fim: Data final do filtro (inclusive).
            statuses: Lista de internal_names para filtrar. None = padrão.
            source_id: ID do venue para filtrar. None = todos.

        Returns:
            Dicionário com valores atuais, anteriores e taxas de crescimento.
            Mantém as chaves legadas (`receita_total`, `ticket_medio`) apontando
            para a receita recebida para compatibilidade com o template.
        """
        delta_dias = (data_fim - data_inicio).days + 1
        prev_fim = data_inicio - timedelta(days=1)
        prev_inicio = prev_fim - timedelta(days=delta_dias - 1)

        def _query(d_ini: date, d_fim: date) -> dict:
            # Separa em duas queries para evitar multiplicação de linhas:
            # um LEFT JOIN com PaymentTransaction repetiria Reserva.total_amount
            # N vezes quando há N transações por reserva, inflando a receita contratada.
            base_filters = [
                Reserva.deleted_at.is_(None),
                _status_clause(statuses),
                func.date(Reserva.check_in_date) >= d_ini,
                func.date(Reserva.check_in_date) <= d_fim,
            ]

            # Query 1: contagens e receita contratada direto da tabela de reservas.
            q_res = db.session.query(
                func.count(Reserva.id).label("total_reservas"),
                func.coalesce(func.sum(Reserva.total_amount), 0).label("receita_contratada"),
            ).filter(*base_filters)
            q_res = _apply_source(q_res, source_id)
            row_res = q_res.one()

            # Query 2: soma do ledger de pagamentos para as reservas do período.
            # Filtra PaymentTransaction pelos group_ids das reservas já filtradas,
            # garantindo que source_id e status_clause sejam respeitados.
            approved_amt = case(
                (PaymentTransaction.status == TransactionStatus.APPROVED,
                 PaymentTransaction.amount),
                else_=0,
            )
            refunded_amt = case(
                (PaymentTransaction.status == TransactionStatus.REFUNDED,
                 PaymentTransaction.amount),
                else_=0,
            )
            group_ids_sq = db.session.query(Reserva.group_id).filter(*base_filters)
            if source_id is not None:
                group_ids_sq = group_ids_sq.filter(Reserva.source_id == source_id)

            row_txns = db.session.query(
                func.coalesce(func.sum(approved_amt), 0).label("soma_aprovada"),
                func.coalesce(func.sum(refunded_amt), 0).label("soma_reembolsada"),
            ).filter(PaymentTransaction.reserva_group_id.in_(group_ids_sq)).one()

            return {
                "total_reservas":     row_res.total_reservas,
                "receita_contratada": row_res.receita_contratada,
                "soma_aprovada":      row_txns.soma_aprovada,
                "soma_reembolsada":   row_txns.soma_reembolsada,
            }

        atual = _query(data_inicio, data_fim)
        anterior = _query(prev_inicio, prev_fim)

        def _extract(row: dict):
            contratada  = float(row["receita_contratada"] or 0)
            aprovada    = float(row["soma_aprovada"] or 0)
            reembolsada = float(row["soma_reembolsada"] or 0)
            recebida    = aprovada - reembolsada
            count       = int(row["total_reservas"] or 0)
            ticket      = (contratada / count) if count else 0.0
            return {
                "total_reservas":      count,
                "receita_contratada":  contratada,
                "receita_recebida":    recebida,
                "receita_reembolsada": reembolsada,
                "ticket_medio":        ticket,
            }

        a = _extract(atual)
        p = _extract(anterior)

        return {
            # atual — manter `receita_total` apontando para a líquida recebida
            # (compatibilidade com o template e com quem consome o JSON).
            "total_reservas":        a["total_reservas"],
            "receita_total":         a["receita_recebida"],
            "receita_contratada":    a["receita_contratada"],
            "receita_recebida":      a["receita_recebida"],
            "receita_reembolsada":   a["receita_reembolsada"],
            "ticket_medio":          a["ticket_medio"],
            # período anterior
            "prev_total_reservas":      p["total_reservas"],
            "prev_receita_total":       p["receita_recebida"],
            "prev_receita_contratada":  p["receita_contratada"],
            "prev_receita_reembolsada": p["receita_reembolsada"],
            "prev_ticket_medio":        p["ticket_medio"],
            # crescimento
            "growth_reservas":      _calc_growth(a["total_reservas"], p["total_reservas"]),
            "growth_receita":       _calc_growth(a["receita_recebida"], p["receita_recebida"]),
            "growth_contratada":    _calc_growth(a["receita_contratada"], p["receita_contratada"]),
            "growth_reembolsada":   _calc_growth(a["receita_reembolsada"], p["receita_reembolsada"]),
            "growth_ticket":        _calc_growth(a["ticket_medio"], p["ticket_medio"]),
        }

    @staticmethod
    def por_origem(
        data_inicio: date,
        data_fim: date,
        statuses: list[str] | None = None,
        source_id: int | None = None,
    ) -> list[dict]:
        """Agrega reservas por origem (WEB / BALCAO) no período informado.

        Args:
            data_inicio: Data inicial do filtro (inclusive).
            data_fim: Data final do filtro (inclusive).
            statuses: Lista de internal_names para filtrar. None = padrão.
            source_id: ID do venue para filtrar. None = todos.

        Returns:
            Lista de dicionários com chaves:
              - origem (str): Valor do enum ReservaOrigin (ex: "WEB").
              - count (int): Quantidade de reservas.
              - receita (float): Soma de total_amount.
        """
        query = (
            db.session.query(
                Reserva.origin,
                func.count(Reserva.id).label("count"),
                func.coalesce(func.sum(Reserva.total_amount), 0).label("receita"),
            )
            .filter(
                Reserva.deleted_at.is_(None),
                _status_clause(statuses),
                func.date(Reserva.check_in_date) >= data_inicio,
                func.date(Reserva.check_in_date) <= data_fim,
            )
            .group_by(Reserva.origin)
        )
        rows = _apply_source(query, source_id).all()

        return [
            {
                "origem":  row.origin.value if isinstance(row.origin, ReservaOrigin) else str(row.origin),
                "count":   int(row.count),
                "receita": float(row.receita),
            }
            for row in rows
        ]

    @staticmethod
    def por_categoria(
        data_inicio: date,
        data_fim: date,
        source_id: int | None = None,
        statuses: list[str] | None = None,
    ) -> list[dict]:
        """Agrega reservas por categoria no período informado.

        Args:
            data_inicio: Data inicial do filtro (inclusive).
            data_fim: Data final do filtro (inclusive).
            source_id: ID do venue para filtrar. None = todos.
            statuses: Lista de internal_names para filtrar. None = padrão.

        Returns:
            Lista de dicionários ordenada por receita decrescente, com chaves:
              - categoria (str): Nome da categoria.
              - count (int): Quantidade de reservas.
              - receita (float): Soma de total_amount.
        """
        # LEFT OUTER JOIN para que reservas com category_id NULL não sumam
        # silenciosamente do relatório — aparecem como "Sem categoria".
        categoria_label = func.coalesce(ReservaCategory.name, "Sem categoria").label("categoria")
        query = (
            db.session.query(
                categoria_label,
                func.count(Reserva.id).label("count"),
                func.coalesce(func.sum(Reserva.total_amount), 0).label("receita"),
            )
            .outerjoin(ReservaCategory, Reserva.category_id == ReservaCategory.id)
            .filter(
                Reserva.deleted_at.is_(None),
                _status_clause(statuses),
                func.date(Reserva.check_in_date) >= data_inicio,
                func.date(Reserva.check_in_date) <= data_fim,
            )
            .group_by(categoria_label)
            .order_by(func.sum(Reserva.total_amount).desc())
        )

        query = _apply_source(query, source_id)

        return [
            {
                "categoria": row.categoria,
                "count":     int(row.count),
                "receita":   float(row.receita),
            }
            for row in query.all()
        ]

    @staticmethod
    def itens_mais_vendidos(
        data_inicio: date,
        data_fim: date,
        limit: int = 10,
        statuses: list[str] | None = None,
        source_id: int | None = None,
    ) -> list[dict]:
        """Lista os itens/tarifas mais vendidos por receita no período informado.

        Receita por linha = COALESCE(item_total, locked_price):
          - item_total: snapshot calculado pelo servidor na criação (correto para todos
            os billing_types, incluindo DAILY_PRORATED_HOURLY).
          - locked_price: fallback para registros legados sem item_total — nesses casos
            o valor já representa o total da linha (price_override enviado pelo frontend,
            equivalente a unit_price × quantity × dias/horas).

        Args:
            data_inicio: Data inicial do filtro (inclusive).
            data_fim: Data final do filtro (inclusive).
            limit: Número máximo de itens retornados. Default: 10.
            statuses: Lista de internal_names para filtrar. None = padrão.

        Returns:
            Lista de dicionários ordenada por receita decrescente, com chaves:
              - item (str): Nome do item/tarifa.
              - categoria (str): Nome da categoria do item.
              - quantidade (int): Soma das quantidades vendidas.
              - receita (float): Soma de item_total (ou locked_price para registros legados).
        """
        receita_efetiva = func.coalesce(
            ReservaItemLink.item_total, ReservaItemLink.locked_price
        )
        query = (
            db.session.query(
                ReservaItem.name.label("item"),
                ReservaCategory.name.label("categoria"),
                func.sum(ReservaItemLink.quantity).label("quantidade"),
                func.coalesce(func.sum(receita_efetiva), 0).label("receita"),
            )
            .join(ReservaItem, ReservaItemLink.item_id == ReservaItem.id)
            .join(Reserva, ReservaItemLink.reserva_id == Reserva.id)
            .join(ReservaCategory, ReservaItem.category_id == ReservaCategory.id)
            .filter(
                ReservaItemLink.deleted_at.is_(None),
                Reserva.deleted_at.is_(None),
                _status_clause(statuses),
                func.date(Reserva.check_in_date) >= data_inicio,
                func.date(Reserva.check_in_date) <= data_fim,
            )
            .group_by(ReservaItem.id, ReservaItem.name, ReservaCategory.name)
            .order_by(func.sum(receita_efetiva).desc())
            .limit(limit)
        )
        rows = _apply_source(query, source_id).all()

        return [
            {
                "item":       row.item,
                "categoria":  row.categoria,
                "quantidade": int(row.quantidade) if row.quantidade else 0,
                "receita":    float(row.receita),
            }
            for row in rows
        ]

    @staticmethod
    def itens_mais_vendidos_por_origem(
        data_inicio: date,
        data_fim: date,
        statuses: list[str] | None = None,
        source_id: int | None = None,
    ) -> list[dict]:
        """Lista todos os itens/tarifas com receita e quantidade detalhadas por origem.

        Agrega em duas dimensões (item × origem) e faz o pivot em Python,
        retornando uma lista ordenada por receita total decrescente. Sem limit —
        retorna todos os itens do período para permitir scroll completo no frontend.

        Args:
            data_inicio: Data inicial do filtro (inclusive).
            data_fim: Data final do filtro (inclusive).
            statuses: Lista de internal_names para filtrar. None = padrão.
            source_id: ID do venue ou None para todos os venues.

        Returns:
            Lista de dicionários ordenada por receita_total decrescente, com chaves:
              - item (str): Nome do item/tarifa.
              - categoria (str): Nome da categoria.
              - label (str): "item (categoria)" — pronto para uso no gráfico.
              - receita_total (float): Soma de receita de todas as origens.
              - quantidade_total (int): Soma de quantidade de todas as origens.
              - por_origem (dict): Mapeamento origem → {receita (float), quantidade (int)}.
        """
        receita_efetiva = func.coalesce(
            ReservaItemLink.item_total, ReservaItemLink.locked_price
        )
        rows = _apply_source(
            db.session.query(
                ReservaItem.id.label("item_id"),
                ReservaItem.name.label("item"),
                ReservaCategory.name.label("categoria"),
                Reserva.origin.label("origin"),
                func.sum(ReservaItemLink.quantity).label("quantidade"),
                func.coalesce(func.sum(receita_efetiva), 0).label("receita"),
            )
            .join(ReservaItem, ReservaItemLink.item_id == ReservaItem.id)
            .join(Reserva, ReservaItemLink.reserva_id == Reserva.id)
            .join(ReservaCategory, ReservaItem.category_id == ReservaCategory.id)
            .filter(
                ReservaItemLink.deleted_at.is_(None),
                Reserva.deleted_at.is_(None),
                _status_clause(statuses),
                func.date(Reserva.check_in_date) >= data_inicio,
                func.date(Reserva.check_in_date) <= data_fim,
            )
            .group_by(
                ReservaItem.id,
                ReservaItem.name,
                ReservaCategory.name,
                Reserva.origin,
            )
            .order_by(ReservaItem.id),
            source_id,
        ).all()

        items: dict[int, dict] = {}
        for row in rows:
            if row.item_id not in items:
                items[row.item_id] = {
                    "item": row.item,
                    "categoria": row.categoria,
                    "label": f"{row.item} ({row.categoria})",
                    "receita_total": 0.0,
                    "quantidade_total": 0,
                    "por_origem": {},
                }
            origem_key = (
                row.origin.value
                if isinstance(row.origin, ReservaOrigin)
                else str(row.origin)
            )
            qty = int(row.quantidade) if row.quantidade else 0
            rec = float(row.receita)
            items[row.item_id]["por_origem"][origem_key] = {
                "receita": rec,
                "quantidade": qty,
            }
            items[row.item_id]["receita_total"] += rec
            items[row.item_id]["quantidade_total"] += qty

        return sorted(items.values(), key=lambda x: x["receita_total"], reverse=True)

    # ------------------------------------------------------------------
    # Novos KPIs
    # ------------------------------------------------------------------

    @staticmethod
    def antecedencia_stats(
        data_inicio: date,
        data_fim: date,
        statuses: list[str] | None = None,
        source_id: int | None = None,
    ) -> dict:
        """Calcula estatísticas de antecedência de reserva em dias.

        Antecedência = número de dias entre a criação da reserva (criado_em)
        e a data de check-in. A média sozinha é distorcida por caudas (uma
        reserva feita com 180 dias puxa o valor para cima), por isso o
        retorno inclui mediana e p90.

        Exclui reservas de balcão walk-in (toggle "Reserva de Balcão" ativado),
        pois esses clientes chegam sem reserva prévia e distorcem a métrica.

        Identificação de walk-ins (dois critérios complementares):
          1. metadata_info.is_balcao_mode == true  — reservas criadas após a
             implementação do flag (dados novos).
          2. customer_name LIKE 'Balcão %'          — proxy para dados históricos,
             já que walk-ins recebem nome sequencial gerado automaticamente
             ("Balcão 1", "Balcão 2" etc.).

        A filtragem de walk-ins e o cálculo dos percentis são feitos em Python
        (não em SQL) para manter o método portável entre MySQL (produção) e
        SQLite (suíte de testes) — DATEDIFF e JSON_UNQUOTE não existem no SQLite.

        Args:
            data_inicio: Data inicial do filtro (check_in_date, inclusive).
            data_fim: Data final do filtro (check_in_date, inclusive).
            statuses: Lista de internal_names para filtrar. None = padrão.
            source_id: ID do venue para filtrar. None = todos.

        Returns:
            Dicionário com:
              - media (float|None): Média em dias, 1 casa decimal.
              - mediana (float|None): Mediana em dias, 1 casa decimal.
              - p90 (float|None): Percentil 90 em dias, 1 casa decimal.
              - n (int): Quantidade de reservas consideradas.
            Valores None quando não há dados no período.
        """
        query = (
            db.session.query(
                Reserva.check_in_date,
                Reserva.criado_em,
                Reserva.customer_name,
                Reserva.metadata_info,
            )
            .filter(
                Reserva.deleted_at.is_(None),
                _status_clause(statuses),
                func.date(Reserva.check_in_date) >= data_inicio,
                func.date(Reserva.check_in_date) <= data_fim,
                Reserva.criado_em.isnot(None),
            )
        )
        rows = _apply_source(query, source_id).all()

        import json as _json

        dias: list[float] = []
        for r in rows:
            # Exclui walk-ins por dois critérios (OR) — robusto a renomeação
            # manual do cliente após a criação.
            if r.customer_name and r.customer_name.startswith("Balcão "):
                continue
            meta = r.metadata_info
            if isinstance(meta, str):
                try:
                    meta = _json.loads(meta)
                except (ValueError, TypeError):
                    meta = None
            if isinstance(meta, dict) and meta.get("is_balcao_mode") is True:
                continue

            check_in = r.check_in_date.date() if hasattr(r.check_in_date, "date") else r.check_in_date
            criado = r.criado_em.date() if hasattr(r.criado_em, "date") else r.criado_em
            delta = (check_in - criado).days
            # Exclui criado_em > check_in_date (reservas retroativas) — delta
            # negativo distorce a média para baixo ou até valores negativos.
            if delta < 0:
                continue
            dias.append(float(delta))

        if not dias:
            return {"media": None, "mediana": None, "p90": None, "n": 0}

        dias.sort()
        n = len(dias)

        def _percentile(p: float) -> float:
            # Interpolação linear entre os vizinhos (método padrão do numpy).
            idx = (n - 1) * p
            lo, hi = int(idx), min(int(idx) + 1, n - 1)
            frac = idx - lo
            return dias[lo] * (1 - frac) + dias[hi] * frac

        return {
            "media":   round(sum(dias) / n, 1),
            "mediana": round(_percentile(0.5), 1),
            "p90":     round(_percentile(0.9), 1),
            "n":       n,
        }

    @staticmethod
    def antecedencia_media(
        data_inicio: date,
        data_fim: date,
        statuses: list[str] | None = None,
        source_id: int | None = None,
    ) -> float | None:
        """Calcula a antecedência média de reserva em dias.

        Mantido por compatibilidade com o endpoint JSON — delega para
        antecedencia_stats() e retorna apenas a média.

        Args:
            data_inicio: Data inicial do filtro (check_in_date, inclusive).
            data_fim: Data final do filtro (check_in_date, inclusive).
            statuses: Lista de internal_names para filtrar. None = padrão.
            source_id: ID do venue para filtrar. None = todos.

        Returns:
            Média de dias arredondada a 1 casa decimal, ou None se sem dados.
        """
        return AnalyticsService.antecedencia_stats(
            data_inicio, data_fim, statuses, source_id
        )["media"]

    @staticmethod
    def serie_temporal(
        data_inicio: date,
        data_fim: date,
        statuses: list[str] | None = None,
        source_id: int | None = None,
    ) -> list[dict]:
        """Agrega reservas e receita contratada por dia de check-in.

        Base do gráfico de evolução temporal do dashboard. Dias sem reservas
        não aparecem no retorno — o frontend preenche os buracos com zero
        para a média móvel ficar correta.

        Args:
            data_inicio: Data inicial do filtro (check_in_date, inclusive).
            data_fim: Data final do filtro (check_in_date, inclusive).
            statuses: Lista de internal_names para filtrar. None = padrão.
            source_id: ID do venue para filtrar. None = todos.

        Returns:
            Lista ordenada por dia ascendente, com chaves:
              - dia (str): Data ISO "YYYY-MM-DD".
              - count (int): Quantidade de reservas com check-in nesse dia.
              - receita (float): Soma de total_amount (contratada).
        """
        checkin_day = func.date(Reserva.check_in_date)
        query = (
            db.session.query(
                checkin_day.label("dia"),
                func.count(Reserva.id).label("count"),
                func.coalesce(func.sum(Reserva.total_amount), 0).label("receita"),
            )
            .filter(
                Reserva.deleted_at.is_(None),
                _status_clause(statuses),
                checkin_day >= data_inicio,
                checkin_day <= data_fim,
            )
            .group_by(checkin_day)
            .order_by(checkin_day)
        )
        rows = _apply_source(query, source_id).all()

        return [
            {
                "dia":     row.dia.isoformat() if hasattr(row.dia, "isoformat") else str(row.dia),
                "count":   int(row.count),
                "receita": float(row.receita),
            }
            for row in rows
        ]

    @staticmethod
    def conversao_por_origem(
        data_inicio: date,
        data_fim: date,
        source_id: int | None = None,
    ) -> list[dict]:
        """Calcula a taxa de conversão inteligente separada por origem.

        Mesma lógica de taxa_conversao_inteligente() (deduplicação por
        CPF + data de check-in, denominador com todos os status exceto
        ARCHIVED_VERSION), mas com a origem como dimensão extra — mede
        diretamente a eficácia de cada canal (WEB / BALCÃO / BOT).

        Args:
            data_inicio: Data inicial do filtro (check_in_date, inclusive).
            data_fim: Data final do filtro (check_in_date, inclusive).
            source_id: ID do venue para filtrar. None = todos.

        Returns:
            Lista ordenada por total de intenções decrescente, com chaves:
              - origem (str): Valor do enum ReservaOrigin (ex: "WEB").
              - total_intents (int): Intenções únicas (CPF+data) da origem.
              - converted_intents (int): Intenções convertidas.
              - taxa (float): Percentual de conversão (0–100).
        """
        from sqlalchemy import literal

        # literal().concat() compila para CONCAT() no MySQL e || no SQLite —
        # ao contrário de func.concat(), que não existe em SQLite < 3.44.
        doc_key = func.coalesce(
            func.nullif(Reserva.customer_document, ""),
            literal("_id_").concat(cast(Reserva.id, String)),
        )
        checkin_day = func.date(Reserva.check_in_date)
        converted_case = case(
            (Reserva.status.notin_(_STATUS_NAO_CONVERTIDOS), 1),
            else_=0,
        )

        subq_base = (
            db.session.query(
                Reserva.origin.label("origin"),
                doc_key.label("doc_key"),
                checkin_day.label("checkin_day"),
                func.max(converted_case).label("is_converted"),
            )
            .filter(
                Reserva.deleted_at.is_(None),
                Reserva.status != _ARCHIVED,
                checkin_day >= data_inicio,
                checkin_day <= data_fim,
            )
            .group_by(Reserva.origin, doc_key, checkin_day)
        )
        subq = _apply_source(subq_base, source_id).subquery()

        rows = (
            db.session.query(
                subq.c.origin,
                func.count().label("total_intents"),
                func.coalesce(func.sum(subq.c.is_converted), 0).label("converted_intents"),
            )
            .group_by(subq.c.origin)
            .all()
        )

        resultado = []
        for row in rows:
            origem = (
                row.origin.value
                if isinstance(row.origin, ReservaOrigin)
                else str(row.origin)
            )
            total = int(row.total_intents)
            converted = int(row.converted_intents)
            resultado.append({
                "origem":            origem,
                "total_intents":     total,
                "converted_intents": converted,
                "taxa":              round(converted / total * 100, 1) if total else 0.0,
            })

        return sorted(resultado, key=lambda d: d["total_intents"], reverse=True)

    @staticmethod
    def taxa_conversao_inteligente(
        data_inicio: date,
        data_fim: date,
        source_id: int | None = None,
    ) -> dict:
        """Calcula a taxa de conversão com deduplicação por CPF + data de check-in.

        Lógica:
          1. Agrupa reservas por (chave_cliente, DATE(check_in_date)).
             chave_cliente = COALESCE(NULLIF(customer_document,''), CONCAT('_id_', id))
             — garante que bookings anônimos (sem CPF) cada um conta como intenção própria.
          2. Cada grupo representa uma "intenção" de reserva.
          3. Uma intenção é considerada CONVERTIDA se pelo menos uma das reservas do
             grupo tem um status que NÃO seja PENDING, CANCELED nem ARCHIVED_VERSION.
             Isso inclui PAID, COMPLETED, PARTIALLY_PAID e qualquer status customizado
             de operador (DEPOSIT, EM_USO etc.).
          4. Taxa de conversão = intenções convertidas / total de intenções × 100.

        O denominador SEMPRE inclui todos os status não-arquivados (PENDING, CANCELED,
        PAID, etc.), independente de qualquer filtro de chips ativo no dashboard.
        Isso garante que a taxa seja semanticamente correta: filtrar por PAID no chip
        não deve artificialmente elevar a conversão para 100%.

        Args:
            data_inicio: Data inicial do filtro (check_in_date, inclusive).
            data_fim: Data final do filtro (check_in_date, inclusive).

        Returns:
            Dicionário com:
              - total_intents (int): Total de intenções únicas (CPF+data).
              - converted_intents (int): Intenções com ao menos uma reserva convertida.
              - taxa (float): Percentual de conversão (0–100).
        """
        # Expressão para a chave de deduplicação por cliente
        doc_key = func.coalesce(
            func.nullif(Reserva.customer_document, ""),
            func.concat("_id_", cast(Reserva.id, String)),
        )

        checkin_day = func.date(Reserva.check_in_date)

        # Status não-convertidos (PENDING, CANCELED, ARCHIVED_VERSION em string)
        converted_case = case(
            (Reserva.status.notin_(_STATUS_NAO_CONVERTIDOS), 1),
            else_=0,
        )

        # Denominador fixo: todos os status exceto ARCHIVED_VERSION (artefato interno).
        # Inclui deliberadamente PENDING e CANCELED para que representem intenções
        # não convertidas — essencial para uma taxa de conversão com significado real.
        status_filter = Reserva.status != _ARCHIVED

        subq_base = (
            db.session.query(
                doc_key.label("doc_key"),
                checkin_day.label("checkin_day"),
                func.max(converted_case).label("is_converted"),
            )
            .filter(
                Reserva.deleted_at.is_(None),
                status_filter,
                checkin_day >= data_inicio,
                checkin_day <= data_fim,
            )
            .group_by(doc_key, checkin_day)
        )
        subq = _apply_source(subq_base, source_id).subquery()

        row = (
            db.session.query(
                func.count().label("total_intents"),
                func.coalesce(func.sum(subq.c.is_converted), 0).label("converted_intents"),
            )
            .select_from(subq)
            .one()
        )

        total     = int(row.total_intents)
        converted = int(row.converted_intents)
        taxa      = round(converted / total * 100, 1) if total > 0 else 0.0

        return {
            "total_intents":     total,
            "converted_intents": converted,
            "taxa":              taxa,
        }

    # ------------------------------------------------------------------
    # Exportação — Fluxo de caixa para CSV
    # ------------------------------------------------------------------

    @staticmethod
    def fluxo_caixa(
        data_inicio: date,
        data_fim: date,
        source_id: int | None = None,
    ) -> list[dict]:
        """Lista transações financeiras do período para conciliação bancária em CSV.

        Retorna UMA LINHA POR TRANSAÇÃO (não agregado diário), permitindo casar
        cada movimento com a respectiva entrada no extrato do Mercado Pago ou
        do banco — nome, timestamp completo, valor e IDs de referência.

        A data-âncora de cada linha é:
          - Entradas (APPROVED): PaymentTransaction.approved_at
          - Saídas  (REFUNDED):  PaymentTransaction.refunded_at
        REFUND_FAILED é deliberadamente omitido — o dinheiro não saiu.

        Args:
            data_inicio: Data inicial (inclusive, aplicada à data-âncora da txn).
            data_fim:    Data final (inclusive).
            source_id:   Filtra por venue via JOIN com Reserva.source_id. None = todos.

        Returns:
            Lista de dicionários ordenada por data/hora ascendente, com chaves:
              - data_hora (str): "YYYY-MM-DD HH:MM:SS" (UTC) — precisão total.
              - tipo (str):      "Entrada" ou "Saída".
              - valor (float):   Valor movimentado (sempre positivo).
              - cliente (str):   Nome do cliente da reserva (ou "" se sem vínculo).
              - documento (str): CPF/CNPJ do cliente (somente dígitos, "" se ausente).
              - metodo (str):    Método de pagamento (pix, credit_card, etc.).
              - mp_payment_id (str): ID do pagamento no MP — chave de conciliação.
              - mp_refund_id (str):  ID do extorno (preenchido só em saídas).
              - reserva_id (int|None): FK da reserva, None se desvinculada.
              - venue (str):     Nome do venue/source da reserva.
              - status_detail (str): mp_status_detail do MP (quando disponível).
        """
        from app.models.reservas import ReservaSource as _RS

        def _build_query(is_entrada: bool):
            """Monta a query para entradas (APPROVED) ou saídas (REFUNDED)."""
            if is_entrada:
                status_filter = PaymentTransaction.status == TransactionStatus.APPROVED
                timestamp_col = PaymentTransaction.approved_at
            else:
                status_filter = PaymentTransaction.status == TransactionStatus.REFUNDED
                timestamp_col = PaymentTransaction.refunded_at

            q = (
                db.session.query(
                    PaymentTransaction.id.label("txn_id"),
                    timestamp_col.label("ts"),
                    PaymentTransaction.amount.label("valor"),
                    PaymentTransaction.payment_method.label("metodo"),
                    PaymentTransaction.mp_payment_id.label("mp_payment_id"),
                    PaymentTransaction.refund_id.label("mp_refund_id"),
                    PaymentTransaction.mp_status_detail.label("status_detail"),
                    PaymentTransaction.reserva_id.label("reserva_id"),
                    Reserva.customer_name.label("cliente"),
                    Reserva.customer_document.label("documento"),
                    _RS.name.label("venue"),
                )
                # LEFT JOIN: se a Reserva foi soft-deleted ou reserva_id é NULL,
                # mantemos a linha da txn mesmo sem nome — o dinheiro existiu.
                .outerjoin(Reserva, Reserva.id == PaymentTransaction.reserva_id)
                .outerjoin(_RS, _RS.id == Reserva.source_id)
                .filter(
                    status_filter,
                    timestamp_col.isnot(None),
                    func.date(timestamp_col) >= data_inicio,
                    func.date(timestamp_col) <= data_fim,
                )
            )
            if source_id is not None:
                q = q.filter(Reserva.source_id == source_id)
            return q

        resultado: list[dict] = []

        def _ingest(rows, tipo: str):
            for r in rows:
                ts = r.ts
                # MySQL DATETIME com TZ=UTC — formata para leitura em planilha.
                if ts is None:
                    continue
                data_hora = ts.strftime("%Y-%m-%d %H:%M:%S")
                resultado.append({
                    "data_hora":      data_hora,
                    "tipo":           tipo,
                    "valor":          float(r.valor or 0),
                    "cliente":        r.cliente or "",
                    "documento":      r.documento or "",
                    "metodo":         r.metodo or "",
                    "mp_payment_id":  r.mp_payment_id or "",
                    "mp_refund_id":   r.mp_refund_id or "",
                    "reserva_id":     r.reserva_id,
                    "venue":          r.venue or "",
                    "status_detail":  r.status_detail or "",
                })

        _ingest(_build_query(is_entrada=True).all(),  "Entrada")
        _ingest(_build_query(is_entrada=False).all(), "Saída")

        # Ordena por timestamp real para facilitar a conciliação linha-a-linha.
        resultado.sort(key=lambda d: d["data_hora"])
        return resultado

    # ------------------------------------------------------------------
    # PDV — vendas de balcão e movimentações de caixa (sangria/suprimento)
    # ------------------------------------------------------------------
    #
    # Estas métricas leem PdvSale/PdvMovimentacaoCaixa diretamente — não o
    # ledger PaymentTransaction. Vendas em dinheiro/cartão/pix recebidas no
    # balcão (CASH/EXTERNAL_*) nunca passam pelo Mercado Pago, então
    # `resumo_periodo` (que usa PaymentTransaction) não as enxerga; e
    # movimentação de caixa não é venda nem pagamento — não existe em
    # nenhuma das duas tabelas usadas no resto deste arquivo.
    #
    # Deliberadamente SEM filtro de `statuses`: os chips de status do
    # dashboard filtram o campo `Reserva.status`, um conceito que não se
    # aplica a PdvSale (é "como foi pago", não um estado de reserva) nem a
    # PdvMovimentacaoCaixa (não gera Reserva nenhuma). Aplicar o filtro
    # aqui misturaria dois modelos de dado diferentes sob o mesmo controle
    # de UI — mesmo raciocínio já usado em `taxa_conversao_inteligente` e
    # em `fluxo_caixa` (CSV), que também ignoram o filtro de status.

    @staticmethod
    def pdv_resumo_periodo(
        data_inicio: date,
        data_fim: date,
        source_id: int | None = None,
    ) -> dict:
        """Calcula KPIs de vendas do PDV (balcão) no período, com crescimento.

        Ancora pela data da venda (`PdvSale.criado_em`), não pelo check-in
        da reserva — o dinheiro do PDV muda de mão na hora da venda,
        diferente da receita "contratada" de `resumo_periodo`.

        Args:
            data_inicio: Data inicial do filtro (inclusive).
            data_fim: Data final do filtro (inclusive).
            source_id: ID do venue para filtrar (via PdvShift.source_id). None = todos.

        Returns:
            Dicionário com total_vendas, receita_pdv, ticket_medio_pdv,
            valores do período anterior e growth_* — mesmo formato de
            `resumo_periodo` para reaproveitar o template dos cards de KPI.
        """
        delta_dias = (data_fim - data_inicio).days + 1
        prev_fim = data_inicio - timedelta(days=1)
        prev_inicio = prev_fim - timedelta(days=delta_dias - 1)

        def _query(d_ini: date, d_fim: date) -> dict:
            q = (
                db.session.query(
                    func.count(PdvSale.id).label("total_vendas"),
                    func.coalesce(func.sum(PdvSale.amount_total), 0).label("receita_pdv"),
                )
                .join(PdvShift, PdvShift.id == PdvSale.shift_id)
                .filter(
                    PdvSale.deleted_at.is_(None),
                    func.date(PdvSale.criado_em) >= d_ini,
                    func.date(PdvSale.criado_em) <= d_fim,
                )
            )
            if source_id is not None:
                q = q.filter(PdvShift.source_id == source_id)
            row = q.one()
            count = int(row.total_vendas or 0)
            receita = float(row.receita_pdv or 0)
            return {
                "total_vendas": count,
                "receita_pdv": receita,
                "ticket_medio_pdv": (receita / count) if count else 0.0,
            }

        a = _query(data_inicio, data_fim)
        p = _query(prev_inicio, prev_fim)

        return {
            "total_vendas": a["total_vendas"],
            "receita_pdv": a["receita_pdv"],
            "ticket_medio_pdv": a["ticket_medio_pdv"],
            "prev_total_vendas": p["total_vendas"],
            "prev_receita_pdv": p["receita_pdv"],
            "prev_ticket_medio_pdv": p["ticket_medio_pdv"],
            "growth_vendas_pdv": _calc_growth(a["total_vendas"], p["total_vendas"]),
            "growth_receita_pdv": _calc_growth(a["receita_pdv"], p["receita_pdv"]),
            "growth_ticket_pdv": _calc_growth(a["ticket_medio_pdv"], p["ticket_medio_pdv"]),
        }

    @staticmethod
    def pdv_por_metodo_pagamento(
        data_inicio: date,
        data_fim: date,
        source_id: int | None = None,
    ) -> list[dict]:
        """Agrega vendas do PDV por método de pagamento no período.

        Args:
            data_inicio: Data inicial do filtro (inclusive).
            data_fim: Data final do filtro (inclusive).
            source_id: ID do venue para filtrar. None = todos.

        Returns:
            Lista de dicionários com chaves:
              - metodo (str): Valor do enum PdvPaymentMethod (ex: "CASH").
              - count (int): Quantidade de vendas.
              - receita (float): Soma de amount_total.
        """
        query = (
            db.session.query(
                PdvSale.payment_method,
                func.count(PdvSale.id).label("count"),
                func.coalesce(func.sum(PdvSale.amount_total), 0).label("receita"),
            )
            .join(PdvShift, PdvShift.id == PdvSale.shift_id)
            .filter(
                PdvSale.deleted_at.is_(None),
                func.date(PdvSale.criado_em) >= data_inicio,
                func.date(PdvSale.criado_em) <= data_fim,
            )
            .group_by(PdvSale.payment_method)
            .order_by(func.sum(PdvSale.amount_total).desc())
        )
        if source_id is not None:
            query = query.filter(PdvShift.source_id == source_id)

        return [
            {
                "metodo": row.payment_method.value if isinstance(row.payment_method, PdvPaymentMethod) else str(row.payment_method),
                "count": int(row.count),
                "receita": float(row.receita),
            }
            for row in query.all()
        ]

    @staticmethod
    def movimentacoes_caixa_resumo(
        data_inicio: date,
        data_fim: date,
        source_id: int | None = None,
    ) -> dict:
        """Calcula totais de sangria/suprimento do PDV no período.

        Sangria agrega os três tipos de saída física (TRANSFERENCIA_INTERNA,
        RECOLHIMENTO_COFRE, PAGAMENTO_DESPESA); suprimento é a entrada.
        `saldo_liquido` é suprimento − sangria — negativo é o caso comum
        (mais dinheiro sai da gaveta para o cofre/despesas do que entra).

        Args:
            data_inicio: Data inicial do filtro (inclusive).
            data_fim: Data final do filtro (inclusive).
            source_id: ID do venue para filtrar (via PdvShift.source_id). None = todos.

        Returns:
            Dicionário com total_sangria, total_suprimento, saldo_liquido,
            count_movimentacoes.
        """
        query = (
            db.session.query(
                PdvMovimentacaoCaixa.tipo_movimentacao,
                func.coalesce(func.sum(PdvMovimentacaoCaixa.valor), 0).label("valor"),
                func.count(PdvMovimentacaoCaixa.id).label("count"),
            )
            .join(PdvShift, PdvShift.id == PdvMovimentacaoCaixa.caixa_turno_id)
            .filter(
                PdvMovimentacaoCaixa.deleted_at.is_(None),
                func.date(PdvMovimentacaoCaixa.data_hora) >= data_inicio,
                func.date(PdvMovimentacaoCaixa.data_hora) <= data_fim,
            )
            .group_by(PdvMovimentacaoCaixa.tipo_movimentacao)
        )
        if source_id is not None:
            query = query.filter(PdvShift.source_id == source_id)

        total_sangria = 0.0
        total_suprimento = 0.0
        count_total = 0
        for row in query.all():
            valor = float(row.valor or 0)
            count_total += int(row.count or 0)
            if row.tipo_movimentacao in _TIPOS_SAIDA_CAIXA:
                total_sangria += valor
            else:
                total_suprimento += valor

        return {
            "total_sangria": total_sangria,
            "total_suprimento": total_suprimento,
            "saldo_liquido": total_suprimento - total_sangria,
            "count_movimentacoes": count_total,
        }

    @staticmethod
    def movimentacoes_caixa_por_tipo(
        data_inicio: date,
        data_fim: date,
        source_id: int | None = None,
    ) -> list[dict]:
        """Agrega movimentações de caixa por tipo no período.

        Args:
            data_inicio: Data inicial do filtro (inclusive).
            data_fim: Data final do filtro (inclusive).
            source_id: ID do venue para filtrar. None = todos.

        Returns:
            Lista de dicionários com chaves:
              - tipo (str): Valor do enum TipoMovimentacaoCaixa.
              - count (int): Quantidade de movimentações.
              - valor (float): Soma de valor.
        """
        query = (
            db.session.query(
                PdvMovimentacaoCaixa.tipo_movimentacao,
                func.count(PdvMovimentacaoCaixa.id).label("count"),
                func.coalesce(func.sum(PdvMovimentacaoCaixa.valor), 0).label("valor"),
            )
            .join(PdvShift, PdvShift.id == PdvMovimentacaoCaixa.caixa_turno_id)
            .filter(
                PdvMovimentacaoCaixa.deleted_at.is_(None),
                func.date(PdvMovimentacaoCaixa.data_hora) >= data_inicio,
                func.date(PdvMovimentacaoCaixa.data_hora) <= data_fim,
            )
            .group_by(PdvMovimentacaoCaixa.tipo_movimentacao)
            .order_by(func.sum(PdvMovimentacaoCaixa.valor).desc())
        )
        if source_id is not None:
            query = query.filter(PdvShift.source_id == source_id)

        return [
            {
                "tipo": row.tipo_movimentacao.value if isinstance(row.tipo_movimentacao, TipoMovimentacaoCaixa) else str(row.tipo_movimentacao),
                "count": int(row.count),
                "valor": float(row.valor),
            }
            for row in query.all()
        ]

    @staticmethod
    def movimentacoes_caixa_por_centro_custo(
        data_inicio: date,
        data_fim: date,
        source_id: int | None = None,
    ) -> list[dict]:
        """Agrega despesas de caixa (PAGAMENTO_DESPESA) por centro de custo.

        Só considera PAGAMENTO_DESPESA — é o único tipo que carrega
        `centro_de_custo_id`; os demais tipos (RECOLHIMENTO_COFRE,
        TRANSFERENCIA_INTERNA, SUPRIMENTO) não têm centro de custo por
        desenho (ver docstring de TipoMovimentacaoCaixa em app/models/pdv.py)
        e ficariam todos empilhados sob "Sem centro de custo", o que não
        ajudaria ninguém a decidir nada.

        Args:
            data_inicio: Data inicial do filtro (inclusive).
            data_fim: Data final do filtro (inclusive).
            source_id: ID do venue para filtrar. None = todos.

        Returns:
            Lista ordenada por valor decrescente, com chaves:
              - centro_custo (str): Nome da Area.
              - count (int): Quantidade de lançamentos.
              - valor (float): Soma de valor.
        """
        query = (
            db.session.query(
                Area.name.label("centro_custo"),
                func.count(PdvMovimentacaoCaixa.id).label("count"),
                func.coalesce(func.sum(PdvMovimentacaoCaixa.valor), 0).label("valor"),
            )
            .join(PdvShift, PdvShift.id == PdvMovimentacaoCaixa.caixa_turno_id)
            .join(Area, Area.id == PdvMovimentacaoCaixa.centro_de_custo_id)
            .filter(
                PdvMovimentacaoCaixa.deleted_at.is_(None),
                PdvMovimentacaoCaixa.tipo_movimentacao == TipoMovimentacaoCaixa.PAGAMENTO_DESPESA,
                func.date(PdvMovimentacaoCaixa.data_hora) >= data_inicio,
                func.date(PdvMovimentacaoCaixa.data_hora) <= data_fim,
            )
            .group_by(Area.name)
            .order_by(func.sum(PdvMovimentacaoCaixa.valor).desc())
        )
        if source_id is not None:
            query = query.filter(PdvShift.source_id == source_id)

        return [
            {
                "centro_custo": row.centro_custo,
                "count": int(row.count),
                "valor": float(row.valor),
            }
            for row in query.all()
        ]
