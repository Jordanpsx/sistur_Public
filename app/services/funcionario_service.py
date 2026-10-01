# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
FuncionarioService — all business logic for the Funcionario domain.

Auditoria obrigatória: every mutation calls AuditService.
Arquitetura em camadas: no Flask imports — pure Python, Cython-compatible.
"""

from __future__ import annotations

from typing import Any

from app.core.audit import AuditService
from app.extensions import db
from app.models.funcionario import Funcionario, validar_cpf
from app.services.base import BaseService
from app.services.qr_service import QRService


# Fields captured in audit snapshots for this entity
_AUDIT_FIELDS = [
    "id", "nome", "cpf", "cargo", "matricula",
    "area_id", "ativo", "minutos_esperados_dia", "minutos_almoco",
    "tipo_contrato", "valor_diaria",
]


class FuncionarioService(BaseService):
    """
    CRUD + business rules for Funcionario.

    All methods are static — no instance state — so the class compiles
    cleanly via Cython and is importable with no side effects.
    """

    # ------------------------------------------------------------------
    # Queries (read-only)
    # ------------------------------------------------------------------

    @staticmethod
    def buscar_por_id(funcionario_id: int) -> Funcionario | None:
        """Return active Funcionario by PK, or None."""
        return db.session.get(Funcionario, funcionario_id)

    @staticmethod
    def buscar_por_cpf(cpf: str) -> Funcionario | None:
        """Return active Funcionario by CPF (raw 11 digits), or None."""
        from app.models.funcionario import validar_cpf as _validar
        try:
            cpf_limpo = _validar(cpf)
        except ValueError:
            return None
        return (
            db.session.query(Funcionario)
            .filter_by(cpf=cpf_limpo, ativo=True)
            .first()
        )

    @staticmethod
    def listar_ativos() -> list[Funcionario]:
        """Return all active employees ordered by name."""
        return (
            db.session.query(Funcionario)
            .filter_by(ativo=True)
            .order_by(Funcionario.nome)
            .all()
        )

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    @staticmethod
    def criar(
        nome: str,
        cpf: str,
        *,
        cargo: str | None = None,
        matricula: str | None = None,
        area_id: int | None = None,
        minutos_esperados_dia: int = 480,
        minutos_almoco: int = 60,
        tipo_contrato: str = "CLT",
        valor_diaria: float | None = None,
        ator_id: int | None = None,
    ) -> Funcionario:
        """
        Create a new Funcionario.

        Args:
            nome:                   Full name.
            cpf:                    CPF in any format — validated internally.
            cargo:                  Job title.
            matricula:              Employee registration number.
            area_id:                FK to sistur_areas.
            minutos_esperados_dia:  Daily expected work minutes (default 480 = 8h).
            minutos_almoco:         Lunch break minutes (default 60 = 1h).
            ator_id:                ID of the User/Funcionario performing the action.

        Returns:
            Persisted Funcionario instance.

        Raises:
            ValueError: If CPF is invalid or already registered.
        """
        BaseService._require(nome, "nome")
        cpf_limpo = validar_cpf(cpf)

        # Uniqueness check
        existing = (
            db.session.query(Funcionario).filter_by(cpf=cpf_limpo).first()
        )
        if existing:
            raise ValueError(f"CPF {cpf_limpo} já está cadastrado.")

        if tipo_contrato not in ("CLT", "DIARISTA"):
            raise ValueError(f"tipo_contrato inválido: '{tipo_contrato}'. Use 'CLT' ou 'DIARISTA'.")

        funcionario = Funcionario(
            nome=nome.strip(),
            cpf=cpf_limpo,
            cargo=cargo,
            matricula=matricula,
            area_id=area_id,
            minutos_esperados_dia=minutos_esperados_dia,
            minutos_almoco=minutos_almoco,
            tipo_contrato=tipo_contrato,
            valor_diaria=valor_diaria,
            ativo=True,
            token_qr=QRService.gerar_token(),
        )
        db.session.add(funcionario)
        db.session.flush()  # get PK before audit commit

        # Auditoria obrigatória
        AuditService.log_create(
            "funcionarios",
            entity_id=funcionario.id,
            new_state=BaseService._snapshot(funcionario, _AUDIT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()
        return funcionario

    @staticmethod
    def atualizar(
        funcionario_id: int,
        dados: dict[str, Any],
        ator_id: int | None = None,
    ) -> Funcionario:
        """
        Update writable fields on a Funcionario.

        Args:
            funcionario_id: PK of the employee to update.
            dados:          Dict of field → new value.  Only whitelisted
                            fields are applied; others are silently ignored.
            ator_id:        ID of the actor performing the update.

        Returns:
            Updated Funcionario instance.

        Raises:
            ValueError: If employee not found or CPF conflict.
        """
        funcionario = db.session.get(Funcionario, funcionario_id)
        if not funcionario:
            raise ValueError(f"Funcionário {funcionario_id} não encontrado.")

        snapshot_antes = BaseService._snapshot(funcionario, _AUDIT_FIELDS)

        _CAMPOS_PERMITIDOS = {
            "nome", "cargo", "matricula", "area_id",
            "minutos_esperados_dia", "minutos_almoco", "jornada_semanal",
            "email", "telefone", "data_admissao", "bio",
            "ctps", "ctps_uf", "cbo",
            "horario_entrada_padrao",  # campo para monitoramento de atrasos (AvisoService)
            "tipo_contrato", "valor_diaria",
        }

        if "tipo_contrato" in dados and dados["tipo_contrato"] not in ("CLT", "DIARISTA"):
            raise ValueError(f"tipo_contrato inválido: '{dados['tipo_contrato']}'. Use 'CLT' ou 'DIARISTA'.")

        for campo, valor in dados.items():
            if campo not in _CAMPOS_PERMITIDOS:
                continue
            if campo == "nome" and valor is not None:
                valor = valor.strip()
            setattr(funcionario, campo, valor)

        db.session.flush()

        # Auditoria obrigatória
        AuditService.log_update(
            "funcionarios",
            entity_id=funcionario.id,
            previous_state=snapshot_antes,
            new_state=BaseService._snapshot(funcionario, _AUDIT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()
        return funcionario

    @staticmethod
    def definir_senha(
        funcionario_id: int,
        senha: str,
        ator_id: int | None = None,
    ) -> Funcionario:
        """
        Define ou redefine a senha de acesso do funcionário ao portal.

        A senha é armazenada como hash seguro (werkzeug pbkdf2:sha256).
        O log de auditoria registra a ação mas nunca o valor da senha.

        Args:
            funcionario_id: PK do funcionário.
            senha:          Senha em texto puro — será hasheada antes de persistir.
            ator_id:        ID do ator que realizou a ação.

        Returns:
            Instância atualizada do Funcionario.

        Raises:
            ValueError: Se o funcionário não for encontrado ou a senha for vazia.
        """
        from werkzeug.security import generate_password_hash

        if not senha or not senha.strip():
            raise ValueError("A senha não pode ser vazia.")

        funcionario = db.session.get(Funcionario, funcionario_id)
        if not funcionario:
            raise ValueError(f"Funcionário {funcionario_id} não encontrado.")

        funcionario.senha_hash = generate_password_hash(senha)
        db.session.flush()

        # Audita sem expor a senha — previous/new state são ofuscados
        AuditService.log_update(
            "funcionarios",
            entity_id=funcionario.id,
            previous_state={"senha_hash": "[protegido]"},
            new_state={"senha_hash": "[redefinida]"},
            actor_id=ator_id,
        )
        db.session.commit()
        return funcionario

    @staticmethod
    def desativar(
        funcionario_id: int,
        ator_id: int | None = None,
    ) -> Funcionario:
        """
        Soft-delete: set ativo=False.

        Args:
            funcionario_id: PK of the employee to deactivate.
            ator_id:        ID of the actor.

        Returns:
            Deactivated Funcionario instance.
        """
        funcionario = db.session.get(Funcionario, funcionario_id)
        if not funcionario:
            raise ValueError(f"Funcionário {funcionario_id} não encontrado.")
        if not funcionario.ativo:
            raise ValueError(f"Funcionário {funcionario_id} já está inativo.")

        snapshot_antes = BaseService._snapshot(funcionario, _AUDIT_FIELDS)
        funcionario.ativo = False
        db.session.flush()

        # Auditoria obrigatória
        AuditService.log_update(
            "funcionarios",
            entity_id=funcionario.id,
            previous_state=snapshot_antes,
            new_state=BaseService._snapshot(funcionario, _AUDIT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()
        return funcionario

    @staticmethod
    def reativar(
        funcionario_id: int,
        ator_id: int | None = None,
    ) -> Funcionario:
        """Reverte o soft-delete: seta ativo=True.

        Args:
            funcionario_id: PK do funcionário a reativar.
            ator_id:        ID do ator que executou a ação.

        Returns:
            Instância do Funcionario reativado.

        Raises:
            ValueError: Se o funcionário não for encontrado ou já estiver ativo.
        """
        funcionario = db.session.get(Funcionario, funcionario_id)
        if not funcionario:
            raise ValueError(f"Funcionário {funcionario_id} não encontrado.")
        if funcionario.ativo:
            raise ValueError(f"Funcionário {funcionario_id} já está ativo.")

        snapshot_antes = BaseService._snapshot(funcionario, _AUDIT_FIELDS)
        funcionario.ativo = True
        db.session.flush()

        AuditService.log_update(
            "funcionarios",
            entity_id=funcionario.id,
            previous_state=snapshot_antes,
            new_state=BaseService._snapshot(funcionario, _AUDIT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()
        return funcionario

    @staticmethod
    def regenerar_token_qr(
        funcionario_id: int,
        ator_id: int | None = None,
    ) -> Funcionario:
        """Invalida o QR code atual e gera um novo token UUID para o funcionário.

        Deve ser usado quando o QR code físico é perdido ou comprometido.
        O token antigo deixa de ser válido imediatamente após o commit.

        Args:
            funcionario_id: PK do funcionário.
            ator_id:        ID do ator que solicitou a regeneração.

        Returns:
            Instância atualizada do Funcionario com o novo token_qr.

        Raises:
            ValueError: Se o funcionário não for encontrado.
        """
        funcionario = db.session.get(Funcionario, funcionario_id)
        if not funcionario:
            raise ValueError(f"Funcionário {funcionario_id} não encontrado.")

        token_anterior = funcionario.token_qr or "(nenhum)"
        funcionario.token_qr = QRService.gerar_token()
        db.session.flush()

        # Auditoria obrigatória
        AuditService.log_update(
            "funcionarios",
            entity_id=funcionario.id,
            previous_state={"token_qr": token_anterior},
            new_state={"token_qr": funcionario.token_qr},
            actor_id=ator_id,
        )
        db.session.commit()
        return funcionario

    # ------------------------------------------------------------------
    # Preferências de interface
    # ------------------------------------------------------------------

    _TEMAS_VALIDOS = ("light", "dark")

    @staticmethod
    def definir_tema(funcionario_id: int, tema: str | None) -> Funcionario:
        """Define a preferência de tema (claro/escuro) do colaborador.

        Preferência pessoal e cosmética, armazenada por conta — isola usuários
        em terminais compartilhados e segue o colaborador entre dispositivos.

        Nota de auditoria: este método **não** gera AuditLog de propósito. A
        regra de auditoria obrigatória visa mutações de negócio/compliance; uma
        troca de tema é cosmética, de alta frequência e sem valor de trilha —
        auditá-la apenas poluiria o histórico.

        Args:
            funcionario_id: PK do funcionário.
            tema:           'light', 'dark' ou None (seguir padrão do sistema).

        Returns:
            Instância atualizada do Funcionario.

        Raises:
            ValueError: Se o funcionário não for encontrado ou o tema for inválido.
        """
        if tema is not None and tema not in FuncionarioService._TEMAS_VALIDOS:
            raise ValueError(
                f"tema inválido: '{tema}'. Use 'light', 'dark' ou None."
            )

        funcionario = db.session.get(Funcionario, funcionario_id)
        if not funcionario:
            raise ValueError(f"Funcionário {funcionario_id} não encontrado.")

        funcionario.tema_preferido = tema
        db.session.commit()
        return funcionario
