# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
OrganizacaoService — CRUD de Funcao, Equipe e composição de membros.

Regras de integridade:
- Exclusão de Funcao proibida enquanto houver membros vinculados (para evitar
  órfãos silenciosos em EscalaTurno futuro). Desativar é a alternativa.
- Exclusão de Area nunca deve deixar Funcao/Equipe com area_id inválido —
  as FKs usam ON DELETE SET NULL, mas o service adverte o caller.
- EquipeMembro.funcao_id é nullable: membro pode entrar na equipe sem função
  específica atribuída.

Sem imports Flask — compatível com Cython.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone

from app.core.audit import AuditAction, AuditService
from app.core.models import Area
from app.extensions import db
from app.models.organizacao import Equipe, EquipeAgendamento, EquipeMembro, Funcao
from app.services.base import BaseService

_AUDIT_AREA   = ["id", "name", "slug", "description", "color_index", "is_active"]
_AUDIT_FUNCAO = ["id", "nome", "area_id", "is_active"]
_AUDIT_EQUIPE = ["id", "nome", "area_id", "is_active"]
_AUDIT_MEMBRO = ["id", "equipe_id", "funcionario_id", "funcao_id"]
_AUDIT_AGENDAMENTO = ["id", "equipe_id", "data_inicio", "data_fim", "observacao", "deleted_at"]

# Limite de dias por consulta de composição — evita varrer períodos enormes.
_MAX_DIAS_COMPOSICAO = 62


def _slugify(value: str) -> str:
    """Converte nome em slug ASCII (minúsculo, sem acentos, hífens no lugar de espaços)."""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = value.lower().strip()
    value = re.sub(r"[^\w\s-]", "", value)
    value = re.sub(r"[\s_]+", "-", value)
    return re.sub(r"-+", "-", value).strip("-")


class OrganizacaoService(BaseService):
    """CRUD de Funcao, Equipe e membros. Sem imports Flask."""

    # ------------------------------------------------------------------
    # Funcao
    # ------------------------------------------------------------------

    @staticmethod
    def criar_funcao(nome: str, area_id: int | None = None, ator_id: int | None = None) -> Funcao:
        """Cria uma nova Funcao operacional.

        Args:
            nome:    Nome da função (ex.: "Recepção", "Salva-vidas").
            area_id: FK opcional para sistur_areas.
            ator_id: ID do ator que executa a ação.

        Returns:
            Instância persistida de Funcao.

        Raises:
            ValueError: Se nome vazio ou já existir para a mesma área.
        """
        nome = nome.strip() if nome else ""
        BaseService._require(nome, "nome")

        existente = db.session.query(Funcao).filter(
            db.func.lower(Funcao.nome) == nome.lower(),
            Funcao.area_id == area_id,
        ).first()
        if existente:
            raise ValueError(f"Já existe uma função '{nome}' nessa área.")

        funcao = Funcao(nome=nome, area_id=area_id, is_active=True)
        db.session.add(funcao)
        db.session.flush()

        AuditService.log_create(
            "funcoes",
            entity_id=funcao.id,
            new_state=BaseService._snapshot(funcao, _AUDIT_FUNCAO),
            actor_id=ator_id,
        )
        db.session.commit()
        return funcao

    @staticmethod
    def atualizar_funcao(funcao_id: int, nome: str, area_id: int | None = None, ator_id: int | None = None) -> Funcao:
        """Atualiza nome e/ou área de uma Funcao.

        Args:
            funcao_id: PK da função.
            nome:      Novo nome.
            area_id:   Nova área (None = sem área).
            ator_id:   ID do ator.

        Returns:
            Instância atualizada.

        Raises:
            ValueError: Se não encontrada ou nome duplicado na área.
        """
        funcao = db.session.get(Funcao, funcao_id)
        if not funcao:
            raise ValueError(f"Função {funcao_id} não encontrada.")

        nome = nome.strip() if nome else ""
        BaseService._require(nome, "nome")

        duplicada = db.session.query(Funcao).filter(
            db.func.lower(Funcao.nome) == nome.lower(),
            Funcao.area_id == area_id,
            Funcao.id != funcao_id,
        ).first()
        if duplicada:
            raise ValueError(f"Já existe uma função '{nome}' nessa área.")

        snap_antes = BaseService._snapshot(funcao, _AUDIT_FUNCAO)
        funcao.nome = nome
        funcao.area_id = area_id
        db.session.flush()

        AuditService.log_update(
            "funcoes",
            entity_id=funcao.id,
            previous_state=snap_antes,
            new_state=BaseService._snapshot(funcao, _AUDIT_FUNCAO),
            actor_id=ator_id,
        )
        db.session.commit()
        return funcao

    @staticmethod
    def desativar_funcao(funcao_id: int, ator_id: int | None = None) -> Funcao:
        """Desativa uma Funcao (is_active=False).

        Não deleta — funções desativadas podem referenciar histórico de escala.

        Args:
            funcao_id: PK da função.
            ator_id:   ID do ator.

        Returns:
            Instância atualizada.

        Raises:
            ValueError: Se não encontrada ou já inativa.
        """
        funcao = db.session.get(Funcao, funcao_id)
        if not funcao:
            raise ValueError(f"Função {funcao_id} não encontrada.")
        if not funcao.is_active:
            raise ValueError("Função já está inativa.")

        snap_antes = BaseService._snapshot(funcao, _AUDIT_FUNCAO)
        funcao.is_active = False
        db.session.flush()

        AuditService.log_update(
            "funcoes",
            entity_id=funcao.id,
            previous_state=snap_antes,
            new_state=BaseService._snapshot(funcao, _AUDIT_FUNCAO),
            actor_id=ator_id,
        )
        db.session.commit()
        return funcao

    @staticmethod
    def listar_funcoes(area_id: int | None = None, apenas_ativas: bool = True) -> list[Funcao]:
        """Lista funções com filtro opcional por área e status.

        Args:
            area_id:       Filtra por área quando fornecido.
            apenas_ativas: Se True, retorna apenas is_active=True.

        Returns:
            Lista ordenada por nome.
        """
        q = db.session.query(Funcao)
        if apenas_ativas:
            q = q.filter(Funcao.is_active == True)
        if area_id is not None:
            q = q.filter(Funcao.area_id == area_id)
        return q.order_by(Funcao.nome).all()

    # ------------------------------------------------------------------
    # Equipe
    # ------------------------------------------------------------------

    @staticmethod
    def criar_equipe(nome: str, area_id: int | None = None, ator_id: int | None = None) -> Equipe:
        """Cria uma nova Equipe.

        Args:
            nome:    Nome da equipe.
            area_id: FK opcional para sistur_areas.
            ator_id: ID do ator.

        Returns:
            Instância persistida de Equipe.

        Raises:
            ValueError: Se nome vazio ou duplicado para a mesma área.
        """
        nome = nome.strip() if nome else ""
        BaseService._require(nome, "nome")

        existente = db.session.query(Equipe).filter(
            db.func.lower(Equipe.nome) == nome.lower(),
            Equipe.area_id == area_id,
        ).first()
        if existente:
            raise ValueError(f"Já existe uma equipe '{nome}' nessa área.")

        equipe = Equipe(nome=nome, area_id=area_id, is_active=True)
        db.session.add(equipe)
        db.session.flush()

        AuditService.log_create(
            "equipes",
            entity_id=equipe.id,
            new_state=BaseService._snapshot(equipe, _AUDIT_EQUIPE),
            actor_id=ator_id,
        )
        db.session.commit()
        return equipe

    @staticmethod
    def atualizar_equipe(equipe_id: int, nome: str, area_id: int | None = None, ator_id: int | None = None) -> Equipe:
        """Atualiza nome e/ou área de uma Equipe.

        Args:
            equipe_id: PK da equipe.
            nome:      Novo nome.
            area_id:   Nova área.
            ator_id:   ID do ator.

        Returns:
            Instância atualizada.

        Raises:
            ValueError: Se não encontrada ou nome duplicado na área.
        """
        equipe = db.session.get(Equipe, equipe_id)
        if not equipe:
            raise ValueError(f"Equipe {equipe_id} não encontrada.")

        nome = nome.strip() if nome else ""
        BaseService._require(nome, "nome")

        duplicada = db.session.query(Equipe).filter(
            db.func.lower(Equipe.nome) == nome.lower(),
            Equipe.area_id == area_id,
            Equipe.id != equipe_id,
        ).first()
        if duplicada:
            raise ValueError(f"Já existe uma equipe '{nome}' nessa área.")

        snap_antes = BaseService._snapshot(equipe, _AUDIT_EQUIPE)
        equipe.nome = nome
        equipe.area_id = area_id
        db.session.flush()

        AuditService.log_update(
            "equipes",
            entity_id=equipe.id,
            previous_state=snap_antes,
            new_state=BaseService._snapshot(equipe, _AUDIT_EQUIPE),
            actor_id=ator_id,
        )
        db.session.commit()
        return equipe

    @staticmethod
    def desativar_equipe(equipe_id: int, ator_id: int | None = None) -> Equipe:
        """Desativa uma Equipe (is_active=False).

        Args:
            equipe_id: PK da equipe.
            ator_id:   ID do ator.

        Returns:
            Instância atualizada.

        Raises:
            ValueError: Se não encontrada ou já inativa.
        """
        equipe = db.session.get(Equipe, equipe_id)
        if not equipe:
            raise ValueError(f"Equipe {equipe_id} não encontrada.")
        if not equipe.is_active:
            raise ValueError("Equipe já está inativa.")

        snap_antes = BaseService._snapshot(equipe, _AUDIT_EQUIPE)
        equipe.is_active = False
        db.session.flush()

        AuditService.log_update(
            "equipes",
            entity_id=equipe.id,
            previous_state=snap_antes,
            new_state=BaseService._snapshot(equipe, _AUDIT_EQUIPE),
            actor_id=ator_id,
        )
        db.session.commit()
        return equipe

    @staticmethod
    def listar_equipes(area_id: int | None = None, apenas_ativas: bool = True) -> list[Equipe]:
        """Lista equipes com filtro opcional por área e status.

        Args:
            area_id:       Filtra por área quando fornecido.
            apenas_ativas: Se True, retorna apenas is_active=True.

        Returns:
            Lista ordenada por nome.
        """
        from sqlalchemy.orm import joinedload
        q = db.session.query(Equipe).options(joinedload(Equipe.membros))
        if apenas_ativas:
            q = q.filter(Equipe.is_active == True)
        if area_id is not None:
            q = q.filter(Equipe.area_id == area_id)
        return q.order_by(Equipe.nome).all()

    # ------------------------------------------------------------------
    # Membros de Equipe
    # ------------------------------------------------------------------

    @staticmethod
    def adicionar_membro(
        equipe_id: int,
        funcionario_id: int,
        funcao_id: int | None = None,
        ator_id: int | None = None,
    ) -> EquipeMembro:
        """Adiciona um Funcionario a uma Equipe com função opcional.

        Args:
            equipe_id:       PK da equipe.
            funcionario_id:  PK do funcionário.
            funcao_id:       PK da função (opcional).
            ator_id:         ID do ator.

        Returns:
            Instância de EquipeMembro persistida.

        Raises:
            ValueError: Se equipe não encontrada, funcionário já membro ou
                        função não pertence à mesma área da equipe.
        """
        equipe = db.session.get(Equipe, equipe_id)
        if not equipe:
            raise ValueError(f"Equipe {equipe_id} não encontrada.")

        existente = db.session.query(EquipeMembro).filter_by(
            equipe_id=equipe_id, funcionario_id=funcionario_id
        ).first()
        if existente:
            raise ValueError("Funcionário já é membro desta equipe.")

        membro = EquipeMembro(
            equipe_id=equipe_id,
            funcionario_id=funcionario_id,
            funcao_id=funcao_id,
        )
        db.session.add(membro)
        db.session.flush()

        AuditService.log_create(
            "equipe_membros",
            entity_id=membro.id,
            new_state=BaseService._snapshot(membro, _AUDIT_MEMBRO),
            actor_id=ator_id,
        )
        db.session.commit()
        return membro

    @staticmethod
    def remover_membro(
        equipe_id: int,
        funcionario_id: int,
        ator_id: int | None = None,
    ) -> None:
        """Remove um Funcionario de uma Equipe.

        Args:
            equipe_id:      PK da equipe.
            funcionario_id: PK do funcionário.
            ator_id:        ID do ator.

        Raises:
            ValueError: Se o vínculo não existir.
        """
        membro = db.session.query(EquipeMembro).filter_by(
            equipe_id=equipe_id, funcionario_id=funcionario_id
        ).first()
        if not membro:
            raise ValueError("Funcionário não é membro desta equipe.")

        snap = BaseService._snapshot(membro, _AUDIT_MEMBRO)
        membro_id = membro.id
        db.session.delete(membro)
        db.session.flush()

        AuditService.log(
            AuditAction.delete,
            "equipe_membros",
            entity_id=membro_id,
            previous_state=snap,
            new_state={},
            actor_id=ator_id,
        )
        db.session.commit()

    @staticmethod
    def atualizar_funcao_membro(
        equipe_id: int,
        funcionario_id: int,
        funcao_id: int | None,
        ator_id: int | None = None,
    ) -> EquipeMembro:
        """Altera a função de um membro dentro da equipe.

        Args:
            equipe_id:      PK da equipe.
            funcionario_id: PK do funcionário.
            funcao_id:      Nova função (None para remover).
            ator_id:        ID do ator.

        Returns:
            Instância atualizada.

        Raises:
            ValueError: Se o vínculo não existir.
        """
        membro = db.session.query(EquipeMembro).filter_by(
            equipe_id=equipe_id, funcionario_id=funcionario_id
        ).first()
        if not membro:
            raise ValueError("Funcionário não é membro desta equipe.")

        snap_antes = BaseService._snapshot(membro, _AUDIT_MEMBRO)
        membro.funcao_id = funcao_id
        db.session.flush()

        AuditService.log_update(
            "equipe_membros",
            entity_id=membro.id,
            previous_state=snap_antes,
            new_state=BaseService._snapshot(membro, _AUDIT_MEMBRO),
            actor_id=ator_id,
        )
        db.session.commit()
        return membro

    # ------------------------------------------------------------------
    # Area
    # ------------------------------------------------------------------

    @staticmethod
    def _validar_cor(cor) -> int | None:
        """Normaliza o índice de cor da paleta categórica (0-7) ou None."""
        if cor is None or cor == "":
            return None
        try:
            cor = int(cor)
        except (TypeError, ValueError):
            raise ValueError("Cor inválida: use um índice de 0 a 7.")
        if not 0 <= cor <= 7:
            raise ValueError("Cor inválida: use um índice de 0 a 7.")
        return cor

    @staticmethod
    def criar_area(
        nome: str,
        descricao: str | None = None,
        cor: int | None = None,
        ator_id: int | None = None,
    ) -> Area:
        """Cria um novo departamento operacional.

        O slug é derivado automaticamente do nome (ASCII, minúsculo, hifenizado).
        Se o slug gerado colidir, acrescenta sufixo numérico.

        Args:
            nome:     Nome da área (ex.: "Recepção", "Bar").
            descricao: Descrição opcional.
            cor:      Índice da paleta categórica (0-7). None = derivada do id.
            ator_id:  ID do ator (auditoria).

        Returns:
            Instância persistida de Area.

        Raises:
            ValueError: Se nome vazio, cor fora de 0-7 ou já existir área com mesmo nome.
        """
        nome = (nome or "").strip()
        BaseService._require(nome, "nome")
        cor = OrganizacaoService._validar_cor(cor)

        existente = db.session.query(Area).filter(
            db.func.lower(Area.name) == nome.lower()
        ).first()
        if existente:
            raise ValueError(f"Já existe uma área com o nome '{nome}'.")

        slug_base = _slugify(nome)
        slug = slug_base
        contador = 1
        while db.session.query(Area).filter_by(slug=slug).first():
            slug = f"{slug_base}-{contador}"
            contador += 1

        area = Area(
            name=nome, slug=slug, description=descricao or None,
            color_index=cor, is_active=True,
        )
        db.session.add(area)
        db.session.flush()

        AuditService.log_create(
            "areas",
            entity_id=area.id,
            new_state=BaseService._snapshot(area, _AUDIT_AREA),
            actor_id=ator_id,
        )
        db.session.commit()
        return area

    @staticmethod
    def atualizar_area(
        area_id: int,
        nome: str,
        descricao: str | None = None,
        cor: int | None = None,
        ator_id: int | None = None,
    ) -> Area:
        """Atualiza nome, descrição e cor de uma área.

        O slug NÃO é alterado ao mudar o nome — serve como identificador estável
        para referências externas (integrações, configurações, n8n, etc.).

        Args:
            area_id:  PK da área.
            nome:     Novo nome.
            descricao: Nova descrição (None limpa o campo).
            cor:      Índice da paleta categórica (0-7). None volta à cor
                      derivada do id.
            ator_id:  ID do ator (auditoria).

        Returns:
            Instância atualizada de Area.

        Raises:
            ValueError: Se área não encontrada, nome vazio, cor fora de 0-7 ou
                conflito de nome.
        """
        nome = (nome or "").strip()
        BaseService._require(nome, "nome")
        cor = OrganizacaoService._validar_cor(cor)

        area = db.session.get(Area, area_id)
        if not area:
            raise ValueError("Área não encontrada.")

        conflito = db.session.query(Area).filter(
            db.func.lower(Area.name) == nome.lower(),
            Area.id != area_id,
        ).first()
        if conflito:
            raise ValueError(f"Já existe uma área com o nome '{nome}'.")

        snap = BaseService._snapshot(area, _AUDIT_AREA)

        area.name = nome
        area.description = descricao or None
        area.color_index = cor
        db.session.flush()

        AuditService.log_update(
            "areas",
            entity_id=area.id,
            previous_state=snap,
            new_state=BaseService._snapshot(area, _AUDIT_AREA),
            actor_id=ator_id,
        )
        db.session.commit()
        return area

    @staticmethod
    def desativar_area(area_id: int, ator_id: int | None = None) -> Area:
        """Desativa uma área (is_active=False).

        Args:
            area_id: PK da área.
            ator_id: ID do ator (auditoria).

        Returns:
            Instância atualizada de Area.

        Raises:
            ValueError: Se área não encontrada.
        """
        area = db.session.get(Area, area_id)
        if not area:
            raise ValueError("Área não encontrada.")

        snap = BaseService._snapshot(area, _AUDIT_AREA)
        area.is_active = False
        db.session.flush()

        AuditService.log_update(
            "areas",
            entity_id=area.id,
            previous_state=snap,
            new_state=BaseService._snapshot(area, _AUDIT_AREA),
            actor_id=ator_id,
        )
        db.session.commit()
        return area

    @staticmethod
    def listar_areas(apenas_ativas: bool = True) -> list[Area]:
        """Lista áreas ordenadas por nome.

        Args:
            apenas_ativas: Se True (padrão), retorna só as ativas.

        Returns:
            Lista de Area.
        """
        q = db.session.query(Area)
        if apenas_ativas:
            q = q.filter_by(is_active=True)
        return q.order_by(Area.name).all()

    @staticmethod
    def equipes_do_dia(data, folgas: list[dict] | None = None) -> list[dict]:
        """Composição das equipes ativas em uma data, com disponibilidade por membro.

        Cruza os membros de cada equipe ativa com as folgas do dia
        (`RHService.funcionarios_de_folga`): membro de folga/ausência aparece
        marcado com o motivo, em vez de sumir da lista.

        Somente leitura — sem auditoria e sem mutação.

        Args:
            data:   Data a consultar.
            folgas: Resultado pré-calculado de ``funcionarios_de_folga(data)``,
                    para evitar recomputar quando o chamador já o possui.
                    Se None, é calculado internamente.

        Returns:
            Lista ordenada por nome da equipe, cada item com:
            - equipe (Equipe)
            - membros (list[dict]): funcionario, funcao (Funcao | None),
              folga_motivo (str | None — preenchido se o membro está de folga).
            - disponiveis (int): membros sem folga na data.
            - total (int): total de membros.
        """
        from sqlalchemy.orm import joinedload

        if folgas is None:
            from app.services.rh_service import RHService
            folgas = RHService.funcionarios_de_folga(data)

        motivo_por_funcionario = {item["funcionario"].id: item["motivo"] for item in folgas}

        equipes = (
            db.session.query(Equipe)
            .options(
                joinedload(Equipe.membros).joinedload(EquipeMembro.funcionario),
                joinedload(Equipe.membros).joinedload(EquipeMembro.funcao),
            )
            .filter(Equipe.is_active == True)  # noqa: E712
            .order_by(Equipe.nome)
            .all()
        )

        resultado = []
        for eq in equipes:
            membros = []
            disponiveis = 0
            for m in sorted(eq.membros, key=lambda x: x.funcionario.nome):
                if not m.funcionario.ativo:
                    continue
                motivo = motivo_por_funcionario.get(m.funcionario_id)
                if motivo is None:
                    disponiveis += 1
                membros.append({
                    "funcionario": m.funcionario,
                    "funcao": m.funcao,
                    "folga_motivo": motivo,
                })
            resultado.append({
                "equipe": eq,
                "membros": membros,
                "disponiveis": disponiveis,
                "total": len(membros),
            })
        return resultado

    @staticmethod
    def alertas_disponibilidade(data, equipe_id: int | None = None) -> dict[int, list[str]]:
        """Alertas de disponibilidade por funcionário para a montagem de equipe.

        Consolida três verificações que evitam descobrir o problema só no dia:
          1. **Folga/ausência na data** — via ``RHService.funcionarios_de_folga``;
          2. **Já alocado em outra equipe ativa** (exclui a própria equipe em edição);
          3. **Diarista sem ``valor_diaria``** — quebra o custo operacional (2.5).

        Somente leitura — sem auditoria e sem mutação.

        Args:
            data:      Data de referência (normalmente hoje).
            equipe_id: Equipe em edição, para não acusar "já alocado" nela mesma.

        Returns:
            Dict ``{funcionario_id: [avisos]}`` — só funcionários com ao menos
            um aviso aparecem.
        """
        from app.models.funcionario import Funcionario
        from app.services.rh_service import RHService

        alertas: dict[int, list[str]] = {}

        def _add(fid: int, aviso: str) -> None:
            alertas.setdefault(fid, []).append(aviso)

        for item in RHService.funcionarios_de_folga(data):
            _add(item["funcionario"].id, f"{item['motivo']} hoje")

        outras = (
            db.session.query(EquipeMembro, Equipe.nome)
            .join(Equipe, EquipeMembro.equipe_id == Equipe.id)
            .filter(Equipe.is_active == True)  # noqa: E712
        )
        if equipe_id is not None:
            outras = outras.filter(Equipe.id != equipe_id)
        for membro, equipe_nome in outras.all():
            _add(membro.funcionario_id, f"Já em: {equipe_nome}")

        diaristas_sem_diaria = (
            db.session.query(Funcionario.id)
            .filter(
                Funcionario.ativo == True,  # noqa: E712
                Funcionario.tipo_contrato == "DIARISTA",
                Funcionario.valor_diaria.is_(None),
            )
            .all()
        )
        for (fid,) in diaristas_sem_diaria:
            _add(fid, "Diarista sem diária cadastrada")

        return alertas

    # ------------------------------------------------------------------
    # Agendamentos de Equipe (2.1b — equipes dinâmicas)
    # ------------------------------------------------------------------

    @staticmethod
    def _notificar_membros(equipe: Equipe, titulo: str, mensagem: str, ator_id: int | None) -> None:
        """Envia um Aviso a cada membro ativo da equipe (melhor esforço).

        Falha de validação do aviso (ex.: destinatário inativo) nunca impede
        a operação de agendamento.
        """
        from app.services.aviso_service import AvisoService

        for m in equipe.membros:
            try:
                AvisoService.criar(
                    destinatario_id=m.funcionario_id,
                    titulo=titulo,
                    mensagem=mensagem,
                    tipo="SISTEMA",
                    remetente_id=ator_id,
                    ator_id=ator_id,
                )
            except ValueError:
                continue

    @staticmethod
    def _validar_periodo(data_inicio, data_fim) -> None:
        if data_inicio is None or data_fim is None:
            raise ValueError("Informe data de início e fim do agendamento.")
        if data_inicio > data_fim:
            raise ValueError("A data de início deve ser anterior ou igual à data de fim.")

    @staticmethod
    def _conflito_agendamento(equipe_id: int, data_inicio, data_fim, excluir_id: int | None = None):
        """Retorna o agendamento ativo da equipe que intersecta o período, se houver."""
        q = db.session.query(EquipeAgendamento).filter(
            EquipeAgendamento.equipe_id == equipe_id,
            EquipeAgendamento.deleted_at.is_(None),
            EquipeAgendamento.data_inicio <= data_fim,
            EquipeAgendamento.data_fim >= data_inicio,
        )
        if excluir_id is not None:
            q = q.filter(EquipeAgendamento.id != excluir_id)
        return q.first()

    @staticmethod
    def agendar_equipe(
        equipe_id: int,
        data_inicio,
        data_fim,
        observacao: str | None = None,
        ator_id: int | None = None,
        notificar: bool = True,
    ) -> EquipeAgendamento:
        """Agenda uma equipe para operar num período (dia, semana, mês ou custom).

        Períodos da mesma equipe não podem se sobrepor (validação de intervalo
        no service — constraint de banco não expressa interseção).

        Args:
            equipe_id:   PK da equipe (deve estar ativa).
            data_inicio: Primeiro dia do período.
            data_fim:    Último dia do período (inclusive).
            observacao:  Observação livre (opcional).
            ator_id:     ID do ator (auditoria).
            notificar:   Se True (padrão), avisa os membros via AvisoService.

        Returns:
            EquipeAgendamento persistido.

        Raises:
            ValueError: Se equipe não encontrada/inativa, período inválido ou
                sobreposição com agendamento existente.
        """
        OrganizacaoService._validar_periodo(data_inicio, data_fim)

        equipe = db.session.get(Equipe, equipe_id)
        if not equipe:
            raise ValueError("Equipe não encontrada.")
        if not equipe.is_active:
            raise ValueError("Equipe inativa não pode ser agendada.")

        conflito = OrganizacaoService._conflito_agendamento(equipe_id, data_inicio, data_fim)
        if conflito:
            raise ValueError(
                f"A equipe já tem agendamento de "
                f"{conflito.data_inicio.strftime('%d/%m/%Y')} a "
                f"{conflito.data_fim.strftime('%d/%m/%Y')} que sobrepõe este período."
            )

        ag = EquipeAgendamento(
            equipe_id=equipe_id,
            data_inicio=data_inicio,
            data_fim=data_fim,
            observacao=(observacao or "").strip() or None,
            criado_por_id=ator_id,
        )
        db.session.add(ag)
        db.session.flush()

        AuditService.log_create(
            "equipe_agendamentos",
            entity_id=ag.id,
            new_state=BaseService._snapshot(ag, _AUDIT_AGENDAMENTO),
            actor_id=ator_id,
        )
        if notificar:
            OrganizacaoService._notificar_membros(
                equipe,
                "Sua equipe foi agendada",
                f"A equipe '{equipe.nome}' vai operar de "
                f"{data_inicio.strftime('%d/%m/%Y')} a {data_fim.strftime('%d/%m/%Y')}.",
                ator_id,
            )
        db.session.commit()
        return ag

    @staticmethod
    def atualizar_agendamento(
        agendamento_id: int,
        data_inicio,
        data_fim,
        observacao: str | None = None,
        ator_id: int | None = None,
        notificar: bool = True,
    ) -> EquipeAgendamento:
        """Atualiza o período/observação de um agendamento ativo.

        Args:
            agendamento_id: PK do agendamento (não cancelado).
            data_inicio:    Novo primeiro dia.
            data_fim:       Novo último dia (inclusive).
            observacao:     Nova observação (None limpa).
            ator_id:        ID do ator (auditoria).
            notificar:      Se True (padrão), avisa os membros da mudança.

        Returns:
            EquipeAgendamento atualizado.

        Raises:
            ValueError: Se não encontrado, período inválido ou sobreposição.
        """
        OrganizacaoService._validar_periodo(data_inicio, data_fim)

        ag = db.session.get(EquipeAgendamento, agendamento_id)
        if not ag or ag.deleted_at is not None:
            raise ValueError("Agendamento não encontrado.")

        conflito = OrganizacaoService._conflito_agendamento(
            ag.equipe_id, data_inicio, data_fim, excluir_id=ag.id
        )
        if conflito:
            raise ValueError(
                f"A equipe já tem agendamento de "
                f"{conflito.data_inicio.strftime('%d/%m/%Y')} a "
                f"{conflito.data_fim.strftime('%d/%m/%Y')} que sobrepõe este período."
            )

        mudou_periodo = ag.data_inicio != data_inicio or ag.data_fim != data_fim
        snap = BaseService._snapshot(ag, _AUDIT_AGENDAMENTO)
        ag.data_inicio = data_inicio
        ag.data_fim = data_fim
        ag.observacao = (observacao or "").strip() or None
        db.session.flush()

        AuditService.log_update(
            "equipe_agendamentos",
            entity_id=ag.id,
            previous_state=snap,
            new_state=BaseService._snapshot(ag, _AUDIT_AGENDAMENTO),
            actor_id=ator_id,
        )
        if notificar and mudou_periodo:
            OrganizacaoService._notificar_membros(
                ag.equipe,
                "Agendamento da sua equipe mudou",
                f"A equipe '{ag.equipe.nome}' agora opera de "
                f"{data_inicio.strftime('%d/%m/%Y')} a {data_fim.strftime('%d/%m/%Y')}.",
                ator_id,
            )
        db.session.commit()
        return ag

    @staticmethod
    def cancelar_agendamento(agendamento_id: int, ator_id: int | None = None) -> EquipeAgendamento:
        """Cancela (soft-delete) um agendamento ativo e avisa os membros.

        Args:
            agendamento_id: PK do agendamento.
            ator_id:        ID do ator (auditoria).

        Returns:
            EquipeAgendamento cancelado (deleted_at preenchido).

        Raises:
            ValueError: Se não encontrado ou já cancelado.
        """
        ag = db.session.get(EquipeAgendamento, agendamento_id)
        if not ag or ag.deleted_at is not None:
            raise ValueError("Agendamento não encontrado.")

        snap = BaseService._snapshot(ag, _AUDIT_AGENDAMENTO)
        ag.deleted_at = datetime.now(timezone.utc)
        db.session.flush()

        AuditService.log_update(
            "equipe_agendamentos",
            entity_id=ag.id,
            previous_state=snap,
            new_state=BaseService._snapshot(ag, _AUDIT_AGENDAMENTO),
            actor_id=ator_id,
        )
        OrganizacaoService._notificar_membros(
            ag.equipe,
            "Agendamento da sua equipe cancelado",
            f"O período de {ag.data_inicio.strftime('%d/%m/%Y')} a "
            f"{ag.data_fim.strftime('%d/%m/%Y')} da equipe '{ag.equipe.nome}' foi cancelado.",
            ator_id,
        )
        db.session.commit()
        return ag

    @staticmethod
    def listar_agendamentos(inicio, fim, equipe_id: int | None = None) -> list[EquipeAgendamento]:
        """Agendamentos ativos que intersectam o intervalo [inicio, fim].

        Args:
            inicio:    Primeiro dia do intervalo consultado.
            fim:       Último dia do intervalo (inclusive).
            equipe_id: Filtra por equipe quando fornecido.

        Returns:
            Lista ordenada por data_inicio e nome da equipe, com equipe/área
            pré-carregadas (joinedload) para a timeline.
        """
        from sqlalchemy.orm import joinedload

        q = (
            db.session.query(EquipeAgendamento)
            .options(joinedload(EquipeAgendamento.equipe).joinedload(Equipe.area))
            .join(Equipe, EquipeAgendamento.equipe_id == Equipe.id)
            .filter(
                EquipeAgendamento.deleted_at.is_(None),
                EquipeAgendamento.data_inicio <= fim,
                EquipeAgendamento.data_fim >= inicio,
            )
        )
        if equipe_id is not None:
            q = q.filter(EquipeAgendamento.equipe_id == equipe_id)
        return q.order_by(EquipeAgendamento.data_inicio, Equipe.nome).all()

    @staticmethod
    def composicao_no_periodo(equipe_id: int, data_inicio, data_fim) -> dict:
        """Composição da equipe num período com disponibilidade por membro×dia.

        Cruza cada membro ativo com `RHService.funcionarios_de_folga` em cada
        dia do intervalo: membro de folga/ausência aparece "na equipe, mas
        indisponível" nos dias afetados (leave awareness do 2.1b).

        Args:
            equipe_id:   PK da equipe.
            data_inicio: Primeiro dia do período.
            data_fim:    Último dia (inclusive). Máximo de 62 dias por consulta.

        Returns:
            Dict com:
            - equipe (Equipe)
            - dias (list[date]): todos os dias do período.
            - membros (list[dict]): funcionario, funcao,
              indisponibilidades (dict[date, str] — motivo por dia afetado).

        Raises:
            ValueError: Se equipe não encontrada, período inválido ou acima
                do limite de dias.
        """
        from app.services.rh_service import RHService

        OrganizacaoService._validar_periodo(data_inicio, data_fim)
        total_dias = (data_fim - data_inicio).days + 1
        if total_dias > _MAX_DIAS_COMPOSICAO:
            raise ValueError(f"Período máximo por consulta: {_MAX_DIAS_COMPOSICAO} dias.")

        equipe = db.session.get(Equipe, equipe_id)
        if not equipe:
            raise ValueError("Equipe não encontrada.")

        dias = [data_inicio + timedelta(days=i) for i in range(total_dias)]
        membros_ativos = [m for m in equipe.membros if m.funcionario.ativo]
        ids_membros = {m.funcionario_id for m in membros_ativos}

        # Motivo de indisponibilidade por (funcionario_id, dia) — lote: 3 queries
        # para todo o período, em vez de 3 por dia.
        motivo_por_dia: dict[tuple[int, object], str] = {}
        for dia, itens in RHService.folgas_no_periodo(data_inicio, data_fim).items():
            for item in itens:
                fid = item["funcionario"].id
                if fid in ids_membros:
                    motivo_por_dia[(fid, dia)] = item["motivo"]

        membros = []
        for m in sorted(membros_ativos, key=lambda x: x.funcionario.nome):
            indisponibilidades = {
                dia: motivo_por_dia[(m.funcionario_id, dia)]
                for dia in dias
                if (m.funcionario_id, dia) in motivo_por_dia
            }
            membros.append({
                "funcionario": m.funcionario,
                "funcao": m.funcao,
                "indisponibilidades": indisponibilidades,
            })

        return {"equipe": equipe, "dias": dias, "membros": membros}
