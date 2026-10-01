# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
RHService — Agregações e consultas de leitura para o módulo de Recursos Humanos.

Arquitetura em camadas: sem imports do Flask — puro Python, compatível com Cython.
Este serviço contém apenas operações de leitura (sem mutações), portanto não
chama AuditService.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models.funcionario import Funcionario
from app.models.ponto import TimeDay, TimeEntry
from app.services.base import BaseService


class RHService(BaseService):
    """
    Serviço de leitura e agregação para o dashboard de RH.

    Todos os métodos são estáticos — sem estado de instância — compatível com Cython.
    Sem mutações: não chama AuditService.
    """

    # ------------------------------------------------------------------
    # Resumo Mensal
    # ------------------------------------------------------------------

    @staticmethod
    def resumo_mensal(ano: int, mes: int) -> dict:
        """Agrega dados de ponto de todos os funcionários em um mês específico.

        Utiliza SQLAlchemy func.sum() para eficiência — uma query por métrica,
        sem carregar registros individuais na memória.

        Args:
            ano: Ano do período a agregar (ex.: 2026).
            mes: Mês do período a agregar (1–12).

        Returns:
            Dicionário com as métricas agregadas:
            - total_funcionarios (int): funcionários ativos no sistema.
            - total_minutos_trabalhados (int): soma de minutos trabalhados no mês.
            - total_saldo_minutos (int): soma dos saldos calculados no mês.
            - dias_com_revisao (int): dias marcados com needs_review=True.
        """
        inicio = date(ano, mes, 1)
        fim = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)

        total_funcionarios = (
            db.session.query(func.count(Funcionario.id))
            .filter_by(ativo=True)
            .scalar()
        ) or 0

        agg = (
            db.session.query(
                func.coalesce(func.sum(TimeDay.minutos_trabalhados), 0),
                func.coalesce(func.sum(TimeDay.saldo_calculado_minutos), 0),
                func.coalesce(
                    func.sum(db.case((TimeDay.needs_review == True, 1), else_=0)), 0
                ),
            )
            .filter(
                TimeDay.shift_date >= inicio,
                TimeDay.shift_date < fim,
            )
            .one()
        )

        return {
            "total_funcionarios": total_funcionarios,
            "total_minutos_trabalhados": int(agg[0]),
            "total_saldo_minutos": int(agg[1]),
            "dias_com_revisao": int(agg[2]),
        }

    # ------------------------------------------------------------------
    # Central de Alertas
    # ------------------------------------------------------------------

    @staticmethod
    def alertas_revisao(janela_dias: int = 7) -> list[dict]:
        """Retorna ocorrências com needs_review=True nos últimos N dias.

        Faz JOIN entre TimeDay e Funcionario para retornar o nome do colaborador
        junto com os dados do dia problemático.

        Args:
            janela_dias: Quantidade de dias para trás a considerar (padrão: 7).

        Returns:
            Lista de dicionários ordenados por shift_date DESC, cada um contendo:
            - funcionario_id (int)
            - nome (str): nome do funcionário
            - shift_date (date): data do dia com pendência
            - minutos_trabalhados (int)
        """
        corte = date.today() - timedelta(days=janela_dias)

        rows = (
            db.session.query(
                TimeDay.funcionario_id,
                Funcionario.nome,
                TimeDay.shift_date,
                TimeDay.minutos_trabalhados,
            )
            .join(Funcionario, Funcionario.id == TimeDay.funcionario_id)
            .filter(
                TimeDay.needs_review == True,
                TimeDay.shift_date >= corte,
                Funcionario.ativo == True,
            )
            .order_by(TimeDay.shift_date.desc())
            .all()
        )

        return [
            {
                "funcionario_id": r.funcionario_id,
                "nome": r.nome,
                "shift_date": r.shift_date,
                "minutos_trabalhados": r.minutos_trabalhados,
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Folgas do dia (2.4)
    # ------------------------------------------------------------------

    _DIAS_SEMANA = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")

    @staticmethod
    def funcionarios_de_folga(data: date) -> list[dict]:
        """Lista os colaboradores ativos que estão de folga em uma data.

        Combina três fontes, em ordem de prioridade do motivo exibido:
          1. **Ausência justificada aprovada** (atestado, férias, folga) na data;
          2. **Escala** com tipo FOLGA registrada para a data;
          3. **Jornada semanal** do CLT inativa naquele dia da semana.

        Quem tem escala tipo NORMAL na data está trabalhando e nunca aparece —
        a escala publicada sobrepõe a jornada. Diaristas só aparecem por ausência
        justificada ou folga de escala (não têm jornada recorrente).

        Args:
            data: Data a consultar.

        Returns:
            Lista de dicts ordenada por nome, cada um com:
            - funcionario (Funcionario)
            - motivo (str): rótulo legível ("Atestado", "Férias",
              "Folga justificada", "Folga (escala)", "Folga (jornada)").
            - fonte (str): "ausencia" | "escala" | "jornada".
        """
        from app.models.avisos import AusenciaJustificada, TipoAusencia
        from app.models.escala import EscalaEntrada

        dia_key = RHService._DIAS_SEMANA[data.weekday()]

        funcionarios = (
            db.session.query(Funcionario)
            .filter_by(ativo=True)
            .order_by(Funcionario.nome)
            .all()
        )

        escala_map = {
            e.funcionario_id: e.tipo
            for e in db.session.query(EscalaEntrada).filter(EscalaEntrada.data == data).all()
        }
        ausencia_map = {
            a.funcionario_id: a.tipo
            for a in db.session.query(AusenciaJustificada)
            .filter(
                AusenciaJustificada.data == data,
                AusenciaJustificada.aprovado == True,
            )
            .all()
        }

        _AUSENCIA_LABEL = {
            TipoAusencia.ATESTADO: "Atestado",
            TipoAusencia.FERIAS: "Férias",
            TipoAusencia.FOLGA: "Folga justificada",
        }

        resultado: list[dict] = []
        for f in funcionarios:
            escala_tipo = escala_map.get(f.id)
            if escala_tipo == "NORMAL":
                continue  # trabalhando — a escala sobrepõe a jornada

            if f.id in ausencia_map:
                tipo_aus = ausencia_map[f.id]
                resultado.append({
                    "funcionario": f,
                    "motivo": _AUSENCIA_LABEL.get(tipo_aus, "Ausência justificada"),
                    "fonte": "ausencia",
                })
            elif escala_tipo == "FOLGA":
                resultado.append({"funcionario": f, "motivo": "Folga (escala)", "fonte": "escala"})
            elif escala_tipo == "FALTA_JUSTIFICADA":
                resultado.append({"funcionario": f, "motivo": "Falta justificada (escala)", "fonte": "escala"})
            elif f.tipo_contrato == "CLT" and f.jornada_semanal:
                dia_cfg = f.jornada_semanal.get(dia_key) or {}
                if not dia_cfg.get("ativo", False):
                    resultado.append({"funcionario": f, "motivo": "Folga (jornada)", "fonte": "jornada"})
            # diarista sem escala/ausência → não está "de folga", apenas não escalado

        return resultado

    @staticmethod
    def folgas_no_periodo(data_inicio: date, data_fim: date) -> dict:
        """Versão em lote de `funcionarios_de_folga` para um intervalo de dias.

        Mesma regra de negócio, com número de queries **constante** (3) em vez
        de 3 por dia: quem consome um mês inteiro (relatórios do 2.6, composição
        de equipe do 2.1b) fazia ~90 queries onde bastam 3.

        Args:
            data_inicio: Primeiro dia do intervalo.
            data_fim: Último dia (inclusive).

        Returns:
            Dict {data: lista de dicts} no mesmo formato de
            `funcionarios_de_folga` — dias sem ninguém de folga vêm com [].
        """
        from app.models.avisos import AusenciaJustificada, TipoAusencia
        from app.models.escala import EscalaEntrada

        funcionarios = (
            db.session.query(Funcionario)
            .filter_by(ativo=True)
            .order_by(Funcionario.nome)
            .all()
        )

        # (funcionario_id, data) → tipo — uma query para todo o intervalo
        escala_map = {
            (e.funcionario_id, e.data): e.tipo
            for e in db.session.query(EscalaEntrada).filter(
                EscalaEntrada.data >= data_inicio,
                EscalaEntrada.data <= data_fim,
            ).all()
        }
        ausencia_map = {
            (a.funcionario_id, a.data): a.tipo
            for a in db.session.query(AusenciaJustificada).filter(
                AusenciaJustificada.data >= data_inicio,
                AusenciaJustificada.data <= data_fim,
                AusenciaJustificada.aprovado == True,  # noqa: E712
            ).all()
        }

        _AUSENCIA_LABEL = {
            TipoAusencia.ATESTADO: "Atestado",
            TipoAusencia.FERIAS: "Férias",
            TipoAusencia.FOLGA: "Folga justificada",
        }

        total_dias = (data_fim - data_inicio).days + 1
        por_dia: dict[date, list[dict]] = {}
        for i in range(total_dias):
            dia = data_inicio + timedelta(days=i)
            dia_key = RHService._DIAS_SEMANA[dia.weekday()]
            do_dia: list[dict] = []
            for f in funcionarios:
                escala_tipo = escala_map.get((f.id, dia))
                if escala_tipo == "NORMAL":
                    continue  # trabalhando — a escala sobrepõe a jornada

                if (f.id, dia) in ausencia_map:
                    tipo_aus = ausencia_map[(f.id, dia)]
                    do_dia.append({
                        "funcionario": f,
                        "motivo": _AUSENCIA_LABEL.get(tipo_aus, "Ausência justificada"),
                        "fonte": "ausencia",
                    })
                elif escala_tipo == "FOLGA":
                    do_dia.append({"funcionario": f, "motivo": "Folga (escala)", "fonte": "escala"})
                elif escala_tipo == "FALTA_JUSTIFICADA":
                    do_dia.append({"funcionario": f, "motivo": "Falta justificada (escala)", "fonte": "escala"})
                elif f.tipo_contrato == "CLT" and f.jornada_semanal:
                    dia_cfg = f.jornada_semanal.get(dia_key) or {}
                    if not dia_cfg.get("ativo", False):
                        do_dia.append({
                            "funcionario": f,
                            "motivo": "Folga (jornada)",
                            "fonte": "jornada",
                        })
            por_dia[dia] = do_dia

        return por_dia

    # ------------------------------------------------------------------
    # Integridade do Banco de Horas (2.9c)
    # ------------------------------------------------------------------

    @staticmethod
    def divergencias_banco_horas(apenas_ativos: bool = True) -> list[dict]:
        """Aponta funcionários cujo `banco_horas_acumulado` divergiu da soma real.

        `Funcionario.banco_horas_acumulado` é um somatório **materializado**:
        toda escrita (processar_dia, débito provisório de ausência, reversão,
        abatimento) o ajusta por *delta*. Uma falha no meio de uma transação —
        ou uma edição direta no banco — faz o cache divergir da verdade sem
        nenhum sinal. Este método é o detector; a correção já existe em
        `PontoService.recalcular_banco_global`.

        Invariante verificado:
            banco_horas_acumulado == Σ TimeDay.saldo_final_minutos
                                     − Σ TimeBankDeduction.minutos_abatidos

        Custo: 3 queries no total, independentemente do nº de funcionários.

        Args:
            apenas_ativos: Restringe a funcionários ativos. Padrão: True.

        Returns:
            Lista (só com quem divergiu) ordenada pela maior diferença
            absoluta, cada item com: funcionario (Funcionario), acumulado
            (int, valor em cache), esperado (int, soma real), diferenca
            (int, acumulado − esperado), soma_dias (int) e soma_abatimentos (int).
        """
        from app.models.ponto import TimeBankDeduction

        query = db.session.query(Funcionario)
        if apenas_ativos:
            query = query.filter(Funcionario.ativo == True)  # noqa: E712
        funcionarios = query.order_by(Funcionario.nome).all()
        if not funcionarios:
            return []

        ids = [f.id for f in funcionarios]

        soma_dias = dict(
            db.session.query(
                TimeDay.funcionario_id,
                func.coalesce(func.sum(TimeDay.saldo_final_minutos), 0),
            )
            .filter(TimeDay.funcionario_id.in_(ids))
            .group_by(TimeDay.funcionario_id)
            .all()
        )
        soma_abatimentos = dict(
            db.session.query(
                TimeBankDeduction.funcionario_id,
                func.coalesce(func.sum(TimeBankDeduction.minutos_abatidos), 0),
            )
            .filter(TimeBankDeduction.funcionario_id.in_(ids))
            .group_by(TimeBankDeduction.funcionario_id)
            .all()
        )

        divergencias = []
        for f in funcionarios:
            dias = int(soma_dias.get(f.id, 0) or 0)
            abatimentos = int(soma_abatimentos.get(f.id, 0) or 0)
            esperado = dias - abatimentos
            acumulado = int(f.banco_horas_acumulado or 0)
            if acumulado != esperado:
                divergencias.append({
                    "funcionario": f,
                    "acumulado": acumulado,
                    "esperado": esperado,
                    "diferenca": acumulado - esperado,
                    "soma_dias": dias,
                    "soma_abatimentos": abatimentos,
                })

        divergencias.sort(key=lambda d: abs(d["diferenca"]), reverse=True)
        return divergencias

    # ------------------------------------------------------------------
    # Listagem com Filtros
    # ------------------------------------------------------------------

    @staticmethod
    def listar_com_filtros(
        q: str = "",
        area_id: int | None = None,
        status: str = "ativo",
        page: int = 1,
        per_page: int = 25,
        escopo_area_id: int | None = None,
    ):
        """Lista funcionários com filtros combinados de busca, área e status.

        Move a lógica de query do blueprint para o serviço (Arquitetura em camadas —
        sem queries em blueprints).

        Args:
            q: Termo livre para filtrar por nome ou CPF (case-insensitive).
            area_id: ID da área para filtrar (escolha do usuário). None = todas
                as áreas visíveis ao ator.
            status: "ativo" | "inativo" | "todos". Padrão: "ativo".
            page: Página atual (1-indexed). Padrão: 1.
            per_page: Itens por página. Padrão: 25.
            escopo_area_id: Restrição de visibilidade do ator (D17) — quando
                informado, a listagem nunca extrapola essa área, mesmo que
                `area_id` peça outra. None = sem restrição (super admin ou
                `funcionarios.view_all`), mesmo padrão do audit viewer.

        Returns:
            Objeto Pagination com `.items`, `.total`, `.pages`, `.has_prev`, `.has_next`.
        """
        query = db.session.query(Funcionario).options(
            joinedload(Funcionario.area),
            joinedload(Funcionario.role),
        )

        if status == "ativo":
            query = query.filter(Funcionario.ativo == True)
        elif status == "inativo":
            query = query.filter(Funcionario.ativo == False)
        # "todos" → sem filtro de status

        if escopo_area_id is not None:
            query = query.filter(Funcionario.area_id == escopo_area_id)
        elif area_id:
            query = query.filter(Funcionario.area_id == area_id)

        if q:
            termo = f"%{q}%"
            query = query.filter(
                db.or_(
                    Funcionario.nome.ilike(termo),
                    Funcionario.cpf.ilike(termo),
                )
            )

        return query.order_by(Funcionario.nome).paginate(
            page=page, per_page=per_page, error_out=False
        )

    # ------------------------------------------------------------------
    # Folha de Ponto Individual (Tab 3 do Dashboard RH)
    # ------------------------------------------------------------------

    @staticmethod
    def folha_ponto_mes(funcionario_id: int, ano: int, mes: int) -> dict:
        """Monta a Folha de Ponto CLT de um funcionário para um mês específico.

        Carrega TimeDay e TimeEntry em queries separadas (evitando produto
        cartesiano) e monta a estrutura de 10 colunas usada na tabela CLT
        e no PDF da Folha de Ponto.

        Arquitetura em camadas: sem queries em blueprints — toda lógica aqui.

        Args:
            funcionario_id: PK do funcionário.
            ano: Ano do período (ex.: 2026).
            mes: Mês do período (1–12).

        Returns:
            Dicionário com:
            - funcionario (Funcionario): instância do funcionário.
            - linhas (list[dict]): uma linha por dia com TimeDay registrado,
              contendo as 10 colunas CLT + time_day_id + needs_review.
              Cada linha:
                shift_date, dia_semana, clock_in, lunch_start, lunch_end,
                clock_out, minutos_trabalhados, expected_minutes_snapshot,
                saldo_calculado_minutos, status, needs_review, time_day_id,
                folga_compensatoria (bool), folga_minutos (int),
                esperado_exibido_minutos (int), desvio_minutos (int).
              Os campos de batida são instâncias de TimeEntry ou None.
            - total_minutos_trabalhados (int): soma dos minutos trabalhados.
            - total_saldo_minutos (int): soma dos desvios **exibidos** — já
              com as folgas compensatórias cobradas.
            - total_expected_minutos (int): soma das jornadas **exibidas** —
              inclui a jornada dos dias trocados por folga.
            - dias_com_revisao (int): contagem de dias com needs_review=True.

        Folga compensatória: no banco, o dia fica zerado e a jornada sai por um
        `TimeBankDeduction` à parte — é o que evita cobrar a folga duas vezes.
        Mas a folha é o documento que o colaborador assina; se ela mostrasse o
        dia zerado, a folga pareceria não ter custado nada. Por isso a linha
        exibe a jornada do dia como coberta pela folga (`esperado_exibido`) e o
        desvio já descontado (`desvio = saldo do dia − folga`). Assim a conta
        fecha na própria folha: trabalhadas − esperadas = saldo do mês.

        Raises:
            ValueError: Se o funcionário não for encontrado.
        """
        from collections import defaultdict

        funcionario = db.session.get(Funcionario, funcionario_id)
        if not funcionario:
            raise ValueError(f"Funcionário {funcionario_id} não encontrado.")

        inicio = date(ano, mes, 1)
        fim = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)

        # Query 1: dias processados
        days = (
            db.session.query(TimeDay)
            .filter(
                TimeDay.funcionario_id == funcionario_id,
                TimeDay.shift_date >= inicio,
                TimeDay.shift_date < fim,
            )
            .order_by(TimeDay.shift_date)
            .all()
        )

        # Query 2: batidas — separada para evitar produto cartesiano
        entries_raw = (
            db.session.query(TimeEntry)
            .filter(
                TimeEntry.funcionario_id == funcionario_id,
                TimeEntry.shift_date >= inicio,
                TimeEntry.shift_date < fim,
            )
            .order_by(TimeEntry.shift_date, TimeEntry.punch_time)
            .all()
        )

        # Agrupa batidas por dia
        entries_by_day: dict = defaultdict(list)
        for e in entries_raw:
            entries_by_day[e.shift_date].append(e)

        DOW_ABBR = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]

        _STATUS_LABELS = {
            True:  "Revisar",
            False: "OK",
        }

        from app.services.ponto_service import PontoService

        folgas = PontoService.folgas_compensatorias_no_periodo(funcionario_id, inicio, fim)

        linhas = []
        for d in days:
            entries = entries_by_day.get(d.shift_date, [])
            folga_min = folgas.get(d.shift_date, 0)

            # Mapeia tipo → primeiro TimeEntry daquele tipo
            by_type: dict[str, "TimeEntry"] = {}  # type: ignore[name-defined]
            for e in entries:
                key = e.punch_type.value
                if key not in by_type:
                    by_type[key] = e

            linhas.append({
                "shift_date":              d.shift_date,
                "dia_semana":             DOW_ABBR[d.shift_date.weekday()],
                "clock_in":               by_type.get("clock_in"),
                "lunch_start":            by_type.get("lunch_start"),
                "lunch_end":              by_type.get("lunch_end"),
                "clock_out":              by_type.get("clock_out"),
                "minutos_trabalhados":    d.minutos_trabalhados,
                "expected_minutes_snapshot": d.expected_minutes_snapshot,
                "saldo_calculado_minutos": d.saldo_calculado_minutos,
                "status":                 _STATUS_LABELS[d.needs_review],
                "needs_review":           d.needs_review,
                "time_day_id":            d.id,
                "folga_compensatoria":    folga_min > 0,
                "folga_minutos":          folga_min,
                "esperado_exibido_minutos": d.expected_minutes_snapshot + folga_min,
                "desvio_minutos":         d.saldo_calculado_minutos - folga_min,
            })

        total_minutos_trabalhados = sum(d.minutos_trabalhados for d in days)
        total_saldo_minutos = sum(linha["desvio_minutos"] for linha in linhas)
        total_expected_minutos = sum(linha["esperado_exibido_minutos"] for linha in linhas)
        dias_com_revisao = sum(1 for d in days if d.needs_review)

        return {
            "funcionario":             funcionario,
            "linhas":                  linhas,
            "total_minutos_trabalhados": total_minutos_trabalhados,
            "total_saldo_minutos":     total_saldo_minutos,
            "total_expected_minutos":  total_expected_minutos,
            "dias_com_revisao":        dias_com_revisao,
        }

    # ------------------------------------------------------------------
    # Planilha Mensal de Batidas
    # ------------------------------------------------------------------

    @staticmethod
    def matriz_batidas_mes(ano: int, mes: int, page: int = 1, per_page: int = 30) -> dict:
        """Agrega contagem de batidas (TimeEntry) por funcionário × dia do mês.

        Retorna estrutura pronta para renderização da planilha mensal no dashboard RH.
        Usa GROUP BY em TimeEntry (não TimeDay) para obter a contagem bruta de batidas,
        independentemente do processamento do saldo.

        Args:
            ano: Ano do período (ex.: 2026).
            mes: Mês do período (1–12).
            page: Página atual (1-indexed). Padrão: 1.
            per_page: Funcionários por página. Padrão: 30.

        Returns:
            Dicionário com:
            - dias_no_mes (int): total de dias no mês.
            - fins_semana (set[int]): dias 1..N que são sábado ou domingo.
            - linhas (list[dict]): uma entrada por funcionário ativo na página, contendo:
                - funcionario_id (int)
                - nome (str)
                - batidas (dict[int, int]): {dia: contagem}; ausente = zero batidas.
            - pagination: objeto Pagination com .total, .pages, .has_prev, .has_next.
        """
        inicio = date(ano, mes, 1)
        dias_no_mes = calendar.monthrange(ano, mes)[1]
        fim = date(ano, mes, dias_no_mes) + timedelta(days=1)

        # Identifica fins de semana (5=sáb, 6=dom)
        fins_semana = {
            d for d in range(1, dias_no_mes + 1)
            if date(ano, mes, d).weekday() >= 5
        }

        # Funcionários ativos ordenados por nome — paginados
        paginacao = (
            db.session.query(Funcionario)
            .filter_by(ativo=True)
            .order_by(Funcionario.nome)
            .paginate(page=page, per_page=per_page, error_out=False)
        )
        funcionarios = paginacao.items
        fids = [f.id for f in funcionarios]

        # Contagem de batidas apenas para os funcionários desta página
        agg = (
            db.session.query(
                TimeEntry.funcionario_id,
                TimeEntry.shift_date,
                func.count(TimeEntry.id).label("punch_count"),
            )
            .filter(
                TimeEntry.shift_date >= inicio,
                TimeEntry.shift_date < fim,
                TimeEntry.funcionario_id.in_(fids),
            )
            .group_by(TimeEntry.funcionario_id, TimeEntry.shift_date)
            .all()
        ) if fids else []

        # Lookup: funcionario_id → {dia_do_mes: contagem}
        lookup: dict[int, dict[int, int]] = {}
        for row in agg:
            lookup.setdefault(row.funcionario_id, {})[row.shift_date.day] = row.punch_count

        linhas = [
            {
                "funcionario_id": f.id,
                "nome": f.nome,
                "batidas": lookup.get(f.id, {}),
            }
            for f in funcionarios
        ]

        return {
            "dias_no_mes": dias_no_mes,
            "fins_semana": fins_semana,
            "linhas": linhas,
            "pagination": paginacao,
        }
