# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
RoleService — toda a lógica de negócio do sistema de roles (RBAC).

Auditoria obrigatória: toda mutação chama AuditService.
Arquitetura em camadas: sem imports do Flask — puro Python, compatível com Cython.

Módulo de referência no legado:
    legacy/includes/class-sistur-permissions.php :: SISTUR_Permissions
"""

from __future__ import annotations

from typing import Any

from app.core.audit import AuditService
from app.core.models import AuditAction
from app.extensions import db
from app.models.funcionario import Funcionario
from app.models.role import Role, RolePermission
from app.services.base import BaseService


# Campos capturados nos snapshots de auditoria para Role
_ROLE_AUDIT_FIELDS = ["id", "nome", "descricao", "is_super_admin", "ativo"]


class RoleService(BaseService):
    """
    CRUD e regras de negócio para o sistema de roles do Portal do Colaborador.

    Todos os métodos são estáticos — sem estado de instância — para compatibilidade
    com Cython e importação sem efeitos colaterais.
    """

    # ------------------------------------------------------------------
    # Queries (somente leitura)
    # ------------------------------------------------------------------

    @staticmethod
    def buscar_por_id(role_id: int) -> Role | None:
        """Retorna o Role pelo PK, ou None se não encontrado."""
        return db.session.get(Role, role_id)

    @staticmethod
    def buscar_por_nome(nome: str) -> Role | None:
        """Retorna o Role pelo slug de nome, ou None se não encontrado."""
        return db.session.query(Role).filter_by(nome=nome).first()

    @staticmethod
    def listar(apenas_ativos: bool = True) -> list[Role]:
        """
        Retorna todos os roles ordenados por nome.

        Args:
            apenas_ativos: Se True, filtra somente roles com ativo=True.

        Returns:
            Lista de Role ordenada pelo campo nome.
        """
        q = db.session.query(Role)
        if apenas_ativos:
            q = q.filter_by(ativo=True)
        return q.order_by(Role.nome).all()

    # ------------------------------------------------------------------
    # Mutações — Role
    # ------------------------------------------------------------------

    @staticmethod
    def criar(
        nome: str,
        descricao: str | None = None,
        *,
        is_super_admin: bool = False,
        ator_id: int | None = None,
    ) -> Role:
        """
        Cria um novo Role e registra o evento no log de auditoria.

        Args:
            nome:          Slug único do papel (e.g. "gerente_rh", "estoquista").
                           Deve ser em snake_case, sem espaços.
            descricao:     Descrição legível para exibição na interface.
            is_super_admin: Se True, portadores deste role têm acesso irrestrito
                            ao portal sem necessidade de permissões explícitas.
                            Use com cautela — equivale ao Administrador do legado.
            ator_id:       ID do funcionário que está criando o role.

        Returns:
            Instância de Role persistida no banco de dados.

        Raises:
            ValueError: Se o nome for vazio ou já estiver em uso.
        """
        BaseService._require(nome, "nome")
        nome = nome.strip().lower()

        existing = db.session.query(Role).filter_by(nome=nome).first()
        if existing:
            raise ValueError(f"Role '{nome}' já existe.")

        role = Role(
            nome=nome,
            descricao=descricao,
            is_super_admin=is_super_admin,
            ativo=True,
        )
        db.session.add(role)
        db.session.flush()

        # Auditoria obrigatória
        AuditService.log_create(
            "roles",
            entity_id=role.id,
            new_state=BaseService._snapshot(role, _ROLE_AUDIT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()
        return role

    @staticmethod
    def atualizar(
        role_id: int,
        dados: dict[str, Any],
        ator_id: int | None = None,
    ) -> Role:
        """
        Atualiza campos editáveis de um Role.

        Args:
            role_id: PK do role a ser atualizado.
            dados:   Dicionário com os campos a alterar. Apenas 'descricao'
                     e 'is_super_admin' são permitidos (nome é imutável após criação).
            ator_id: ID do ator que está realizando a alteração.

        Returns:
            Instância de Role atualizada.

        Raises:
            ValueError: Se o role não for encontrado.
        """
        role = db.session.get(Role, role_id)
        if not role:
            raise ValueError(f"Role {role_id} não encontrado.")

        snapshot_antes = BaseService._snapshot(role, _ROLE_AUDIT_FIELDS)

        _CAMPOS_PERMITIDOS = {"descricao", "is_super_admin"}
        for campo, valor in dados.items():
            if campo in _CAMPOS_PERMITIDOS:
                setattr(role, campo, valor)

        db.session.flush()

        # Auditoria obrigatória
        AuditService.log_update(
            "roles",
            entity_id=role.id,
            previous_state=snapshot_antes,
            new_state=BaseService._snapshot(role, _ROLE_AUDIT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()
        return role

    @staticmethod
    def desativar(role_id: int, ator_id: int | None = None) -> Role:
        """
        Desativa um Role (soft-delete: ativo=False).

        Funcionários com este role perderão o acesso ao portal na próxima
        verificação de permissão.

        Args:
            role_id: PK do role a ser desativado.
            ator_id: ID do ator que está realizando a ação.

        Returns:
            Instância de Role desativada.

        Raises:
            ValueError: Se o role não for encontrado ou já estiver inativo.
            ValueError: Se for um role de super_admin (proteção contra lockout).
        """
        role = db.session.get(Role, role_id)
        if not role:
            raise ValueError(f"Role {role_id} não encontrado.")
        if not role.ativo:
            raise ValueError(f"Role '{role.nome}' já está inativo.")
        if role.is_super_admin:
            raise ValueError(
                "Não é permitido desativar um role de super_admin "
                "para evitar bloqueio total do sistema."
            )

        snapshot_antes = BaseService._snapshot(role, _ROLE_AUDIT_FIELDS)
        role.ativo = False
        db.session.flush()

        # Auditoria obrigatória
        AuditService.log_update(
            "roles",
            entity_id=role.id,
            previous_state=snapshot_antes,
            new_state=BaseService._snapshot(role, _ROLE_AUDIT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()
        return role

    # ------------------------------------------------------------------
    # Mutações — Permissões do Role
    # ------------------------------------------------------------------

    @staticmethod
    def adicionar_permissao(
        role_id: int,
        modulo: str,
        acao: str,
        source_id: int | None = None,
        ator_id: int | None = None,
    ) -> RolePermission:
        """
        Adiciona uma permissão (modulo + acao) ao role indicado.

        Se a permissão já existir, retorna a existente sem criar duplicata.

        Args:
            role_id: PK do role que receberá a permissão.
            modulo:  Módulo do sistema (e.g. "funcionarios", "dashboard").
            acao:    Ação dentro do módulo (e.g. "view", "create", "edit").
            source_id: Venue/fonte de reserva quando a permissão for escopada.
            ator_id: ID do ator que está realizando a alteração.

        Returns:
            Instância de RolePermission criada ou já existente.

        Raises:
            ValueError: Se o role não for encontrado.
        """
        role = db.session.get(Role, role_id)
        if not role:
            raise ValueError(f"Role {role_id} não encontrado.")

        existing = (
            db.session.query(RolePermission)
            .filter_by(role_id=role_id, modulo=modulo, acao=acao, source_id=source_id)
            .first()
        )
        if existing:
            return existing

        permissao = RolePermission(
            role_id=role_id,
            modulo=modulo,
            acao=acao,
            source_id=source_id,
        )
        db.session.add(permissao)
        db.session.flush()

        # Auditoria obrigatória
        AuditService.log_update(
            "roles",
            entity_id=role_id,
            previous_state=BaseService._snapshot(role, _ROLE_AUDIT_FIELDS),
            new_state={
                **BaseService._snapshot(role, _ROLE_AUDIT_FIELDS),
                "permissao_adicionada": f"{modulo}.{acao}",
                "source_id": source_id,
            },
            actor_id=ator_id,
        )
        db.session.commit()
        return permissao

    @staticmethod
    def remover_permissao(
        role_id: int,
        modulo: str,
        acao: str,
        source_id: int | None = None,
        ator_id: int | None = None,
    ) -> None:
        """
        Remove uma permissão (modulo + acao) do role indicado.

        Se a permissão não existir, encerra sem erro (idempotente).

        Args:
            role_id: PK do role que perderá a permissão.
            modulo:  Módulo do sistema.
            acao:    Ação dentro do módulo.
            source_id: Venue/fonte de reserva quando a permissão for escopada.
            ator_id: ID do ator que está realizando a alteração.

        Raises:
            ValueError: Se o role não for encontrado.
        """
        role = db.session.get(Role, role_id)
        if not role:
            raise ValueError(f"Role {role_id} não encontrado.")

        permissao = (
            db.session.query(RolePermission)
            .filter_by(role_id=role_id, modulo=modulo, acao=acao, source_id=source_id)
            .first()
        )
        if not permissao:
            return

        db.session.delete(permissao)
        db.session.flush()

        # Auditoria obrigatória
        AuditService.log_update(
            "roles",
            entity_id=role_id,
            previous_state={
                **BaseService._snapshot(role, _ROLE_AUDIT_FIELDS),
                "permissao_removida": f"{modulo}.{acao}",
                "source_id": source_id,
            },
            new_state=BaseService._snapshot(role, _ROLE_AUDIT_FIELDS),
            actor_id=ator_id,
        )
        db.session.commit()

    # ------------------------------------------------------------------
    # Mutações — Atribuição de Role ao Funcionario
    # ------------------------------------------------------------------

    @staticmethod
    def atribuir_ao_funcionario(
        funcionario_id: int,
        role_id: int,
        ator_id: int | None = None,
    ) -> Funcionario:
        """
        Atribui um Role a um Funcionario, substituindo o anterior se houver.

        Gera um AuditLog com action=permission_change contendo o estado
        anterior e o novo role do funcionário.

        Args:
            funcionario_id: PK do funcionário que receberá o role.
            role_id:        PK do role a ser atribuído.
            ator_id:        ID do funcionário que está realizando a ação.

        Returns:
            Instância de Funcionario atualizada com o novo role_id.

        Raises:
            ValueError: Se o funcionário ou o role não forem encontrados.
            ValueError: Se o role estiver inativo.
        """
        funcionario = db.session.get(Funcionario, funcionario_id)
        if not funcionario:
            raise ValueError(f"Funcionário {funcionario_id} não encontrado.")

        role = db.session.get(Role, role_id)
        if not role:
            raise ValueError(f"Role {role_id} não encontrado.")
        if not role.ativo:
            raise ValueError(f"Role '{role.nome}' está inativo e não pode ser atribuído.")

        # Captura role anterior para o snapshot de auditoria
        role_anterior = db.session.get(Role, funcionario.role_id) if funcionario.role_id else None
        snapshot_antes = {
            "funcionario_id": funcionario.id,
            "role_id": funcionario.role_id,
            "role_nome": role_anterior.nome if role_anterior else None,
        }

        funcionario.role_id = role_id
        db.session.flush()

        snapshot_depois = {
            "funcionario_id": funcionario.id,
            "role_id": role_id,
            "role_nome": role.nome,
        }

        # Auditoria obrigatória — action=permission_change para atribuição de role
        AuditService.log(
            AuditAction.permission_change,
            "roles",
            entity_id=funcionario.id,
            previous_state=snapshot_antes,
            new_state=snapshot_depois,
            actor_id=ator_id,
        )
        db.session.commit()
        return funcionario

    @staticmethod
    def remover_do_funcionario(
        funcionario_id: int,
        ator_id: int | None = None,
    ) -> Funcionario:
        """
        Remove o Role de um Funcionario, deixando-o sem permissões de acesso.

        Após esta operação, o funcionário não conseguirá visualizar módulos
        do portal até que um novo role seja atribuído.

        Args:
            funcionario_id: PK do funcionário que perderá o role.
            ator_id:        ID do funcionário que está realizando a ação.

        Returns:
            Instância de Funcionario com role_id=None.

        Raises:
            ValueError: Se o funcionário não for encontrado.
        """
        funcionario = db.session.get(Funcionario, funcionario_id)
        if not funcionario:
            raise ValueError(f"Funcionário {funcionario_id} não encontrado.")

        role_anterior = db.session.get(Role, funcionario.role_id) if funcionario.role_id else None
        snapshot_antes = {
            "funcionario_id": funcionario.id,
            "role_id": funcionario.role_id,
            "role_nome": role_anterior.nome if role_anterior else None,
        }

        funcionario.role_id = None
        db.session.flush()

        # Auditoria obrigatória
        AuditService.log(
            AuditAction.permission_change,
            "roles",
            entity_id=funcionario.id,
            previous_state=snapshot_antes,
            new_state={"funcionario_id": funcionario.id, "role_id": None, "role_nome": None},
            actor_id=ator_id,
        )
        db.session.commit()
        return funcionario
