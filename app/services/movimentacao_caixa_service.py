# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
MovimentacaoCaixaService — sangria, suprimento e demais movimentações
manuais de caixa do PDV, com separação estrita entre fluxo físico e P&L.

Regra central: retirar dinheiro físico de um PDV para pagar uma despesa
administrativa reduz o caixa físico daquele PDV, mas a despesa pertence ao
centro de custo que a originou (Administrativo, Manutenção…) — nunca à
margem do PDV de onde o dinheiro saiu. `PAGAMENTO_DESPESA` é o tipo que
carimba essa separação: o `caixa_turno_id` diz de onde saiu o físico, o
`centro_de_custo_id` diz a quem pertence o custo — nunca o mesmo campo.

Regras de arquitetura (mesmas do resto do PDV — ver pdv_service.py):
- NUNCA duplica o cálculo de vendas em dinheiro do turno:
  calcular_saldo_esperado_gaveta() reaproveita
  PdvService._esperados_fechamento() como base, para as duas contas nunca
  divergirem por acidente (uma fórmula usada nos dois lugares não tem como
  discordar de si mesma).
- Zero imports de Flask (Cython-ready).
- Todo valor monetário chega como Decimal|float|str e é normalizado via
  Decimal(str(...)) antes de qualquer aritmética — nunca Decimal(float)
  direto, que herda o erro de ponto flutuante do binário. Isso importa
  especialmente aqui: com annotation_typing=False no Cython, a anotação de
  tipo não é garantia de runtime — quem chama pode mandar um float mesmo
  com o hint dizendo Decimal.
- Sem imports dinâmicos, sem uso do módulo inspect.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.core.audit import AuditService
from app.extensions import db
from app.models.funcionario import Funcionario
from app.models.pdv import PdvMovimentacaoCaixa, PdvShift, TipoMovimentacaoCaixa
from app.services.base import BaseService
from app.services.pdv_service import PdvService

_MODULE = "pdv_movimentacao_caixa"

_MOVIMENTACAO_SNAPSHOT_FIELDS = [
    "id", "caixa_turno_id", "tipo_movimentacao", "valor", "conta_destino_id",
    "centro_de_custo_id", "funcionario_id", "autorizado_por_id", "descricao",
    "data_hora",
]

# Tipos que retiram dinheiro físico da gaveta (sangria, em sentido amplo).
_TIPOS_SAIDA = frozenset({
    TipoMovimentacaoCaixa.TRANSFERENCIA_INTERNA,
    TipoMovimentacaoCaixa.RECOLHIMENTO_COFRE,
    TipoMovimentacaoCaixa.PAGAMENTO_DESPESA,
})

# Acima deste valor, autorizado_por_id passa de recomendado para obrigatório.
LIMITE_EXIGE_AUTORIZACAO = Decimal("500.00")


def _to_decimal(valor: Decimal | float | str) -> Decimal:
    """Normaliza qualquer entrada numérica para Decimal com segurança.

    Nunca Decimal(float) direto — um float como 10.1 não tem representação
    binária exata e Decimal(10.1) carrega esse erro (vira algo como
    10.099999999999999645...). Passar por str primeiro corrige isso.

    Args:
        valor: Quantia em Decimal, float ou str.

    Returns:
        Decimal normalizado, seguro para aritmética monetária.
    """
    return Decimal(str(valor))


class MovimentacaoCaixaService(BaseService):
    """Sangria, suprimento e demais movimentações manuais de caixa do PDV."""

    @classmethod
    def registrar_sangria(
        cls,
        caixa_turno_id: int,
        tipo_movimentacao: TipoMovimentacaoCaixa | str,
        valor: Decimal | float | str,
        funcionario_id: int,
        descricao: str,
        conta_destino_id: int | None = None,
        centro_de_custo_id: int | None = None,
        autorizado_por_id: int | None = None,
        manager_override: dict | None = None,
        *,
        ator_id: int | None = None,
    ) -> PdvMovimentacaoCaixa:
        """Registra uma retirada de dinheiro físico da gaveta (sangria).

        Cobre TRANSFERENCIA_INTERNA, RECOLHIMENTO_COFRE e PAGAMENTO_DESPESA —
        os três tipos que tiram dinheiro da gaveta. Para repor caixa, use
        registrar_suprimento().

        Args:
            caixa_turno_id: PK do PdvShift de onde o dinheiro sai. Precisa
                estar OPEN — não é possível sangrar um turno fechado.
            tipo_movimentacao: TRANSFERENCIA_INTERNA, RECOLHIMENTO_COFRE ou
                PAGAMENTO_DESPESA.
            valor: Quantia retirada, sempre positiva (magnitude — o sinal é
                aplicado no cálculo do saldo esperado, não aqui).
            funcionario_id: PK do operador que executa a retirada.
            descricao: Motivo da retirada — obrigatório, porque é a única
                explicação textual de para onde foi o dinheiro físico.
            conta_destino_id: Obrigatório quando tipo_movimentacao é
                TRANSFERENCIA_INTERNA — PK de outro PdvShift OU identificador
                de uma conta virtual externa (ver docstring do model: essa
                referência é solta de propósito, sem FK rígida no banco).
            centro_de_custo_id: Obrigatório quando tipo_movimentacao é
                PAGAMENTO_DESPESA — PK de Area (sistur_areas) para onde a
                despesa é alocada. É isto que mantém a despesa fora da
                margem do PDV de origem.
            autorizado_por_id: PK do gestor que autorizou. Recomendado
                sempre; passa a ser **obrigatório** quando
                valor >= LIMITE_EXIGE_AUTORIZACAO (R$ 500,00). Ignorado se
                manager_override também for informado.
            manager_override: Resultado de
                PdvService.autorizar_manager_override() (dict com
                manager_id/reason) — validado via
                PdvService._validar_manager_override() e, se válido,
                substitui autorizado_por_id. É o caminho usado pela SPA:
                o operador entra CPF+senha do gestor na hora, sem precisar
                do cadastro prévio de um autorizado_por_id.
            ator_id: ID do funcionário executando a ação, para auditoria
                (pode ser igual a funcionario_id — auditoria registra quem
                mexeu no sistema, não necessariamente quem autorizou).

        Returns:
            Instância de PdvMovimentacaoCaixa persistida.

        Raises:
            ValueError: turno não encontrado/fechado, tipo inválido para
                sangria, valor <= 0, centro_de_custo_id ausente em
                PAGAMENTO_DESPESA, conta_destino_id ausente em
                TRANSFERENCIA_INTERNA, autorizado_por_id ausente acima do
                limite, descricao vazia, ou funcionario_id inexistente.
        """
        if isinstance(tipo_movimentacao, str):
            try:
                tipo_movimentacao = TipoMovimentacaoCaixa(tipo_movimentacao)
            except ValueError:
                validos = ", ".join(t.value for t in TipoMovimentacaoCaixa)
                raise ValueError(
                    f"tipo_movimentacao inválido: '{tipo_movimentacao}'. Válidos: {validos}."
                )

        if tipo_movimentacao not in _TIPOS_SAIDA:
            raise ValueError(
                f"registrar_sangria não aceita {tipo_movimentacao.value} — "
                "use registrar_suprimento() para entrada de dinheiro na gaveta."
            )

        if tipo_movimentacao == TipoMovimentacaoCaixa.PAGAMENTO_DESPESA and not centro_de_custo_id:
            raise ValueError(
                "centro_de_custo_id é obrigatório para PAGAMENTO_DESPESA — "
                "sem ele a despesa fica presa ao PDV de origem em vez do "
                "centro de custo correto."
            )
        if tipo_movimentacao == TipoMovimentacaoCaixa.TRANSFERENCIA_INTERNA and not conta_destino_id:
            raise ValueError(
                "conta_destino_id é obrigatório para TRANSFERENCIA_INTERNA."
            )

        return cls._registrar(
            caixa_turno_id=caixa_turno_id,
            tipo_movimentacao=tipo_movimentacao,
            valor=valor,
            funcionario_id=funcionario_id,
            descricao=descricao,
            conta_destino_id=conta_destino_id,
            centro_de_custo_id=centro_de_custo_id,
            autorizado_por_id=autorizado_por_id,
            manager_override=manager_override,
            ator_id=ator_id,
        )

    @classmethod
    def registrar_suprimento(
        cls,
        caixa_turno_id: int,
        valor: Decimal | float | str,
        funcionario_id: int,
        descricao: str,
        autorizado_por_id: int | None = None,
        manager_override: dict | None = None,
        *,
        ator_id: int | None = None,
    ) -> PdvMovimentacaoCaixa:
        """Registra um reforço de dinheiro físico na gaveta (suprimento).

        Contrapartida de registrar_sangria() — soma no cálculo do saldo
        esperado em vez de subtrair. Não usa conta_destino_id nem
        centro_de_custo_id: dinheiro entrando na gaveta não gera despesa
        nem aponta para outro centro de custo, só recompõe o físico.

        Args:
            caixa_turno_id: PK do PdvShift que recebe o reforço. Precisa
                estar OPEN.
            valor: Quantia depositada, sempre positiva.
            funcionario_id: PK do operador que executa o depósito.
            descricao: Motivo do reforço.
            autorizado_por_id: PK do gestor que autorizou (obrigatório
                acima de LIMITE_EXIGE_AUTORIZACAO, igual à sangria).
            manager_override: Ver registrar_sangria() — mesmo mecanismo.
            ator_id: ID do funcionário executando a ação, para auditoria.

        Returns:
            Instância de PdvMovimentacaoCaixa persistida.

        Raises:
            ValueError: turno não encontrado/fechado, valor <= 0,
                autorizado_por_id ausente acima do limite, descricao vazia,
                ou funcionario_id inexistente.
        """
        return cls._registrar(
            caixa_turno_id=caixa_turno_id,
            tipo_movimentacao=TipoMovimentacaoCaixa.SUPRIMENTO,
            valor=valor,
            funcionario_id=funcionario_id,
            descricao=descricao,
            conta_destino_id=None,
            centro_de_custo_id=None,
            autorizado_por_id=autorizado_por_id,
            manager_override=manager_override,
            ator_id=ator_id,
        )

    @classmethod
    def _registrar(
        cls,
        *,
        caixa_turno_id: int,
        tipo_movimentacao: TipoMovimentacaoCaixa,
        valor: Decimal | float | str,
        funcionario_id: int,
        descricao: str,
        conta_destino_id: int | None,
        centro_de_custo_id: int | None,
        autorizado_por_id: int | None,
        manager_override: dict | None = None,
        ator_id: int | None,
    ) -> PdvMovimentacaoCaixa:
        """Validações e persistência compartilhadas por sangria e suprimento."""
        shift = PdvService._shift_aberto(caixa_turno_id)

        valor_decimal = _to_decimal(valor)
        if valor_decimal <= 0:
            raise ValueError("valor deve ser positivo.")

        BaseService._require(descricao, "descricao")

        funcionario = db.session.get(Funcionario, funcionario_id)
        if not funcionario:
            raise ValueError(f"Funcionário {funcionario_id} não encontrado.")

        override = PdvService._validar_manager_override(manager_override, required=False)
        if override:
            autorizado_por_id = override["manager_id"]

        if valor_decimal >= LIMITE_EXIGE_AUTORIZACAO and not autorizado_por_id:
            raise ValueError(
                f"Movimentações de R$ {valor_decimal:.2f} ou mais exigem "
                f"autorizado_por_id (limite: R$ {LIMITE_EXIGE_AUTORIZACAO:.2f})."
            )

        movimentacao = PdvMovimentacaoCaixa(
            caixa_turno_id=shift.id,
            tipo_movimentacao=tipo_movimentacao,
            valor=valor_decimal,
            conta_destino_id=conta_destino_id,
            centro_de_custo_id=centro_de_custo_id,
            funcionario_id=funcionario_id,
            autorizado_por_id=autorizado_por_id,
            descricao=descricao.strip(),
            data_hora=datetime.now(timezone.utc),
        )
        db.session.add(movimentacao)
        db.session.flush()

        AuditService.log_create(
            module=_MODULE,
            entity_id=movimentacao.id,
            new_state=BaseService._snapshot(movimentacao, _MOVIMENTACAO_SNAPSHOT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()
        return movimentacao

    @classmethod
    def calcular_saldo_esperado_gaveta(cls, caixa_turno_id: int) -> Decimal:
        """Calcula o dinheiro físico esperado na gaveta neste momento.

        Delega inteiramente a PdvService._esperados_fechamento(), que já
        soma fundo inicial + vendas em dinheiro e aplica sangrias/suprimentos
        na parcela física — é a mesma conta usada no fechamento de turno
        (`PdvService.fechar_turno`), então as duas nunca podem divergir por
        acidente. Só o lado físico (gaveta) importa aqui; ignora o fundo PIX
        de troco, que não é afetado por sangria/suprimento neste desenho.

        Diferente de _registrar(), não exige que o turno esteja OPEN: este
        método também precisa funcionar para conferência histórica de um
        turno já fechado.

        Args:
            caixa_turno_id: PK do PdvShift a conferir.

        Returns:
            Decimal com o saldo físico esperado na gaveta.

        Raises:
            ValueError: Se o turno não for encontrado.
        """
        shift = db.session.query(PdvShift).filter(
            PdvShift.id == caixa_turno_id,
            PdvShift.deleted_at.is_(None),
        ).first()
        if not shift:
            raise ValueError("Turno não encontrado.")

        esperado_cash, _esperado_pix = PdvService._esperados_fechamento(shift)
        return esperado_cash

    @classmethod
    def listar_por_turno(cls, caixa_turno_id: int) -> list[PdvMovimentacaoCaixa]:
        """Lista as movimentações de caixa de um turno, mais recente primeiro.

        Leitura pura — sem AuditService, sem exigência de turno OPEN (útil
        também para conferência de turno já fechado).

        Args:
            caixa_turno_id: PK do PdvShift a listar.

        Returns:
            Lista de PdvMovimentacaoCaixa, ordenada por data_hora decrescente.
        """
        return db.session.query(PdvMovimentacaoCaixa).filter(
            PdvMovimentacaoCaixa.caixa_turno_id == caixa_turno_id,
            PdvMovimentacaoCaixa.deleted_at.is_(None),
        ).order_by(PdvMovimentacaoCaixa.data_hora.desc()).all()
