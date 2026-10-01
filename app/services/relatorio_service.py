# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
RelatorioService — Central de Relatórios do RH (2.6, Caminho A).

Monta o contexto de cada relatório reaproveitando os services existentes
(RHService, EscalaService, OrganizacaoService, CustoOperacionalService) — sem
recalcular regra de negócio, sem migration. Somente leitura: nenhum método
muta dados. Cada builder devolve um dict pronto para o template de PDF/preview.

Arquitetura em camadas: sem imports do Flask — puro Python, compatível com Cython.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.extensions import db
from app.models.funcionario import Funcionario
from app.services.base import BaseService

# Teto de dias por consulta de período — mesmo limite do 2.1b, evita relatório
# gigante travando WeasyPrint ou virando exportação disfarçada.
_MAX_DIAS_PERIODO = 62


def _validar_periodo(data_inicio: date, data_fim: date) -> None:
    if data_inicio is None or data_fim is None:
        raise ValueError("Informe data de início e fim do relatório.")
    if data_fim < data_inicio:
        raise ValueError("A data final deve ser igual ou posterior à inicial.")
    if (data_fim - data_inicio).days + 1 > _MAX_DIAS_PERIODO:
        raise ValueError(f"Período máximo por relatório: {_MAX_DIAS_PERIODO} dias.")


class RelatorioService(BaseService):
    """Monta os dados dos relatórios estruturados de RH (somente leitura)."""

    @staticmethod
    def funcionarios_ativos_dia(data: date, area_id: int | None = None) -> dict:
        """Roster do dia: quem está ativo, de folga ou escalado, com status.

        Cruza `RHService.funcionarios_de_folga` (quem não trabalha) com
        `EscalaService.presenca_dia` (quem está escalado e seu status de
        ponto) sobre a base de ativos — o retrato completo do dia.

        Args:
            data: Data do relatório.
            area_id: Filtra por área. None = todas.

        Returns:
            Dict com `data`, `area_id`, `total_ativos` e `linhas` — uma por
            funcionário ativo, com `situacao` ("Folga"/"Escalado"/"Sem
            escala"), `motivo`/`status` conforme a fonte, e `labels`.
        """
        from app.services.escala_service import EscalaService
        from app.services.rh_service import RHService

        query = db.session.query(Funcionario).filter(Funcionario.ativo == True)  # noqa: E712
        if area_id:
            query = query.filter(Funcionario.area_id == area_id)
        ativos = query.order_by(Funcionario.nome).all()

        folgas_por_fid = {
            item["funcionario"].id: item["motivo"]
            for item in RHService.funcionarios_de_folga(data)
        }
        escala_por_fid = {
            item["funcionario"].id: item
            for item in EscalaService.presenca_dia(data, area_id=area_id)
        }

        linhas = []
        for f in ativos:
            if f.id in folgas_por_fid:
                linhas.append({
                    "funcionario": f,
                    "situacao": "Folga",
                    "detalhe": folgas_por_fid[f.id],
                })
            elif f.id in escala_por_fid:
                item = escala_por_fid[f.id]
                linhas.append({
                    "funcionario": f,
                    "situacao": "Escalado",
                    "detalhe": item["status"],
                    "labels": item["labels"],
                })
            else:
                linhas.append({"funcionario": f, "situacao": "Sem escala", "detalhe": None})

        return {
            "data": data,
            "area_id": area_id,
            "total_ativos": len(ativos),
            "linhas": linhas,
        }

    @staticmethod
    def folgas_periodo(data_inicio: date, data_fim: date, area_id: int | None = None) -> dict:
        """Folgas dia a dia num período (agregação de `funcionarios_de_folga`).

        Args:
            data_inicio: Primeiro dia do período.
            data_fim: Último dia (inclusive). Máximo de 62 dias.
            area_id: Filtra por área. None = todas.

        Returns:
            Dict com `data_inicio`, `data_fim`, `area_id` e `dias` — lista
            ordenada por data, cada uma com `data` e `folgas` (mesmo formato
            de `RHService.funcionarios_de_folga`).

        Raises:
            ValueError: Se período inválido ou acima do limite de dias.
        """
        from app.services.rh_service import RHService

        _validar_periodo(data_inicio, data_fim)

        # Lote: 3 queries para todo o período, em vez de 3 por dia.
        por_dia = RHService.folgas_no_periodo(data_inicio, data_fim)

        total_dias = (data_fim - data_inicio).days + 1
        dias = []
        for i in range(total_dias):
            dia = data_inicio + timedelta(days=i)
            itens = por_dia.get(dia, [])
            if area_id:
                itens = [it for it in itens if it["funcionario"].area_id == area_id]
            dias.append({"data": dia, "folgas": itens})

        return {
            "data_inicio": data_inicio,
            "data_fim": data_fim,
            "area_id": area_id,
            "dias": dias,
            "total_folgas": sum(len(d["folgas"]) for d in dias),
        }

    @staticmethod
    def escala_periodo(
        data_inicio: date,
        data_fim: date,
        area_id: int | None = None,
        equipe_id: int | None = None,
    ) -> dict:
        """Escala publicada num período, agrupada por dia.

        Args:
            data_inicio: Primeiro dia do período.
            data_fim: Último dia (inclusive). Máximo de 62 dias.
            area_id: Filtra por área. None = todas.
            equipe_id: Filtra por membros de uma equipe. None = todas.

        Returns:
            Dict com `data_inicio`, `data_fim`, `dias` — lista ordenada por
            data, cada uma com `data` e `entradas` (funcionario, tipo, labels).

        Raises:
            ValueError: Se período inválido, acima do limite de dias, ou
                equipe não encontrada.
        """
        from app.models.escala import EscalaEntrada
        from app.models.organizacao import Equipe, EquipeMembro

        _validar_periodo(data_inicio, data_fim)

        if equipe_id is not None and db.session.get(Equipe, equipe_id) is None:
            raise ValueError("Equipe não encontrada.")

        query = (
            db.session.query(EscalaEntrada, Funcionario)
            .join(Funcionario, EscalaEntrada.funcionario_id == Funcionario.id)
            .filter(
                EscalaEntrada.data >= data_inicio,
                EscalaEntrada.data <= data_fim,
                Funcionario.ativo == True,  # noqa: E712
            )
        )
        if area_id:
            query = query.filter(Funcionario.area_id == area_id)
        if equipe_id:
            membro_ids = (
                db.session.query(EquipeMembro.funcionario_id)
                .filter_by(equipe_id=equipe_id)
                .subquery()
            )
            query = query.filter(Funcionario.id.in_(membro_ids))

        por_dia: dict[date, list[dict]] = {}
        for entrada, funcionario in query.order_by(EscalaEntrada.data, Funcionario.nome).all():
            por_dia.setdefault(entrada.data, []).append({
                "funcionario": funcionario,
                "tipo": entrada.tipo,
                "labels": entrada.labels or [],
            })

        total_dias = (data_fim - data_inicio).days + 1
        dias = [
            {"data": data_inicio + timedelta(days=i), "entradas": por_dia.get(data_inicio + timedelta(days=i), [])}
            for i in range(total_dias)
        ]

        return {
            "data_inicio": data_inicio,
            "data_fim": data_fim,
            "area_id": area_id,
            "equipe_id": equipe_id,
            "dias": dias,
        }

    @staticmethod
    def relatorio_equipe(equipe_id: int, data_inicio: date, data_fim: date) -> dict:
        """Relatório de equipe: agenda, composição e custo.

        Compila num único documento o que um coordenador precisa prestar
        contas do período: quando a equipe operou, quem participou e quem
        esteve indisponível, e quanto custaram os diaristas do time.

        Args:
            equipe_id: PK da equipe.
            data_inicio: Primeiro dia do período.
            data_fim: Último dia (inclusive). Máximo de 62 dias.

        Returns:
            Dict com `equipe`, `data_inicio`, `data_fim`, `agendamentos`
            (períodos ativos no intervalo), `composicao` (membros ×
            disponibilidade, de `OrganizacaoService.composicao_no_periodo`),
            `custo_diaristas` (dict funcionario→subtotal + total_geral).

        Raises:
            ValueError: Se equipe não encontrada ou período inválido.
        """
        from app.models.escala import EscalaEntrada
        from app.services.organizacao_service import OrganizacaoService

        _validar_periodo(data_inicio, data_fim)

        composicao = OrganizacaoService.composicao_no_periodo(equipe_id, data_inicio, data_fim)
        equipe = composicao["equipe"]

        agendamentos = OrganizacaoService.listar_agendamentos(
            data_inicio, data_fim, equipe_id=equipe_id
        )

        # Custo de diaristas: dias trabalhados (escala NORMAL) no período × valor_diaria,
        # restrito aos membros atuais da equipe (não ao filtro de área do 2.5).
        diaristas = [
            m["funcionario"] for m in composicao["membros"]
            if m["funcionario"].tipo_contrato == "DIARISTA"
        ]
        # Uma agregação para todos os diaristas, em vez de um COUNT por membro.
        dias_por_funcionario: dict[int, int] = {}
        if diaristas:
            dias_por_funcionario = dict(
                db.session.query(
                    EscalaEntrada.funcionario_id,
                    db.func.count(EscalaEntrada.id),
                )
                .filter(
                    EscalaEntrada.funcionario_id.in_([f.id for f in diaristas]),
                    EscalaEntrada.data >= data_inicio,
                    EscalaEntrada.data <= data_fim,
                    EscalaEntrada.tipo == "NORMAL",
                )
                .group_by(EscalaEntrada.funcionario_id)
                .all()
            )

        custo_linhas = []
        custo_total = Decimal("0")
        custo_sem_valor = []
        for f in diaristas:
            dias_trabalhados = dias_por_funcionario.get(f.id, 0)
            if dias_trabalhados == 0:
                continue
            if not f.valor_diaria:
                custo_sem_valor.append({"funcionario": f, "dias_trabalhados": dias_trabalhados})
                continue
            subtotal = f.valor_diaria * dias_trabalhados
            custo_linhas.append({
                "funcionario": f,
                "dias_trabalhados": dias_trabalhados,
                "valor_diaria": f.valor_diaria,
                "subtotal": subtotal,
            })
            custo_total += subtotal

        return {
            "equipe": equipe,
            "data_inicio": data_inicio,
            "data_fim": data_fim,
            "agendamentos": agendamentos,
            "composicao": composicao,
            "custo_diaristas": {
                "linhas": custo_linhas,
                "sem_valor": custo_sem_valor,
                "total_geral": custo_total,
            },
        }
