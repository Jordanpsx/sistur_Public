# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Módulo de Reservas — foundational models for generic booking system.

ARQUITETURA 3-TIER — Inventory Model:
  1. ReservaSource (venue/location) → owns Resources, Categories, Items, Config
  2. ReservaResource (physical inventory) → stock pool que múltiplos items podem compartilhar
     Exemplo: 'Capacidade Day Use' (50 spots) used by both 'Inteira' e 'Meia' tariffs
  3. ReservaItem (tariff/pricing) → pricepoint que decrémenta um Resource
     Cada item aponta a um Resource + Category

Padrões especiais:
  1. Soft Delete: `deleted_at` timestamp (NOT `ativo` boolean) — ativo = deleted_at IS NULL
  2. Versioning: `Reserva` nunca é atualizado in-place. Edições criam nova versão (row).
     Todas as versões compartilham o mesmo `group_id` UUID. Chave única: (group_id, version).
     Versão anterior é marcada `status=ARCHIVED_VERSION` quando nova versão é criada.
  3. Dynamic Config: `ReservaConfig` é key-value store JSON para settings globais ou por-source.

Permissões RBAC dinâmicas: inserir novo `ReservaSource` automaticamente cria permissões
para roles super_admin via SQLAlchemy event listener.

Headless API backend — múltiplos frontends (WordPress legado, portal, POS) consomem as mesmas tabelas.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import and_, event, select
from sqlalchemy.types import TypeDecorator, String as SAString

from app.extensions import db


# ---------------------------------------------------------------------------
# ReservaGroup — Hierarchical grouping for admin visualization
# ---------------------------------------------------------------------------


reserva_group_sources = db.Table(
    "sistur_reserva_group_sources",
    db.Column("group_id", db.Integer, db.ForeignKey("sistur_reserva_groups.id", ondelete="CASCADE"), primary_key=True),
    db.Column("source_id", db.Integer, db.ForeignKey("sistur_reserva_sources.id", ondelete="CASCADE"), primary_key=True),
)


class ReservaGroup(db.Model):
    """Grupo hierárquico para organização visual de itens no painel admin.

    Permite criar grupos como "Entradas" ou "Churrasqueiras" e sub-grupos
    como "Churrasqueiras > Pequenas > Cobertas". Cada ReservaItem ou
    ReservaResource pode referenciar um grupo para exibição contextual
    nas telas de gestão e white-label.

    A hierarquia suporta profundidade arbitrária via parent_id (N níveis).
    O atributo display_path percorre toda a cadeia de ancestrais.

    sources: locais (ReservaSource) aos quais o grupo pertence — relação N:N.
             Só populada em grupos raiz (parent_id IS NULL); um grupo raiz
             precisa de ao menos um local. Sub-grupos nunca guardam locais
             próprios — sempre herdam dinamicamente do ancestral raiz via
             effective_sources, para nunca divergir do pai.
    parent_id: NULL = grupo raiz. Integer = sub-grupo de outro grupo (qualquer profundidade).
    deleted_at: soft-delete — ativo = deleted_at IS NULL.
    """

    __tablename__ = "sistur_reserva_groups"

    id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    deleted_at = db.Column(db.DateTime, nullable=True, default=None)
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    sources = db.relationship("ReservaSource", secondary=reserva_group_sources, lazy="select")
    parent = db.relationship(
        "ReservaGroup",
        remote_side="ReservaGroup.id",
        foreign_keys=[parent_id],
        lazy="select",
    )
    children = db.relationship(
        "ReservaGroup",
        foreign_keys=[parent_id],
        lazy="select",
        overlaps="parent",
    )
    items = db.relationship("ReservaItem", back_populates="group", lazy="select")

    @property
    def display_path(self) -> str:
        """Retorna o caminho de exibição hierárquico completo do grupo.

        Percorre toda a cadeia de ancestrais (N níveis) para construir o breadcrumb.
        Protege contra referências circulares via conjunto visited.

        Returns:
            String no formato 'Raiz > Intermediário > Folha' para qualquer profundidade.
        """
        parts = [self.name]
        current = self
        visited: set[int] = set()
        while current.parent and current.parent.deleted_at is None:
            if current.parent.id in visited:
                break  # guarda contra referência circular
            visited.add(current.parent.id)
            parts.insert(0, current.parent.name)
            current = current.parent
        return " > ".join(parts)

    @property
    def effective_sources(self) -> list:
        """Retorna os locais efetivos do grupo, herdando do ancestral raiz.

        Grupos raiz (parent_id IS NULL) retornam seus próprios `sources`.
        Sub-grupos sempre herdam do ancestral raiz — nunca guardam locais
        próprios, para nunca divergir do grupo pai. Protege contra
        referências circulares via conjunto visited (mesmo padrão de
        display_path).

        Returns:
            Lista de ReservaSource do ancestral raiz da árvore.
        """
        current = self
        visited: set[int] = set()
        while current.parent and current.parent.deleted_at is None:
            if current.parent.id in visited:
                break  # guarda contra referência circular
            visited.add(current.parent.id)
            current = current.parent
        return current.sources

    def to_dict_recursive(self) -> dict:
        """Serializa o grupo e todos os seus descendentes ativos em estrutura JSON aninhada.

        Percorre a árvore de filhos recursivamente. Útil para endpoints de API que
        precisam retornar a hierarquia completa de grupos em uma única chamada.

        Returns:
            Dict com id, name, description, sort_order, source_ids e lista children recursiva.
        """
        return {
            "id": self.id,
            "name": self.name,
            "display_path": self.display_path,
            "description": self.description,
            "sort_order": self.sort_order,
            "source_ids": [s.id for s in self.effective_sources],
            "children": [
                child.to_dict_recursive()
                for child in self.children
                if child.deleted_at is None
            ],
        }

    def __repr__(self) -> str:
        return f"<ReservaGroup id={self.id} name={self.name!r} parent_id={self.parent_id}>"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BillingType(str, enum.Enum):
    """Tipos de cobrança para itens da reserva.

    Valores:
        FIXED:                  Preço fixo — sem multiplicador de dias ou horas.
                                Tiered pricing (feriado/fim-de-semana/dia-útil) aplicado
                                com base na data de check-in.
        PER_DAY:                Diária — preço × quantidade × número de dias.
                                Cada diária é tarifada individualmente (tiered pricing).
        PER_HOUR:               Por hora — preço × quantidade × horas totais (precisão
                                de datetime quando disponível; dias inteiros como fallback).
        DAILY_PRORATED_HOURLY:  Diária com pró-rata horário — modalidade híbrida para
                                camping e estadas com hora de entrada/saída variáveis.
                                - 1ª diária: cobrada integralmente (mínimo garantido).
                                - Horas adicionais: (tarifa_diária / 24) por hora excedente.
                                - Fórmula: se horas > 24:
                                    total = tarifa + ((horas - 24) × (tarifa / 24))
                                - tarifa = _price_for_day(check_in) → respeita tiered pricing.
                                - Requer datetime com hora para precisão; cai para diária
                                  inteira quando apenas date é fornecido.
    """
    FIXED = "FIXED"
    PER_DAY = "PER_DAY"
    PER_HOUR = "PER_HOUR"
    DAILY_PRORATED_HOURLY = "DAILY_PRORATED_HOURLY"


class ReservaOrigin(str, enum.Enum):
    """Origem do pedido de reserva."""
    WEB    = "WEB"
    BALCAO = "BALCAO"
    BOT    = "BOT"


class ReservaStatus(str, enum.Enum):
    """Estados da reserva ao longo do seu ciclo de vida."""

    PENDING = "PENDING"
    PARTIALLY_PAID = "PARTIALLY_PAID"   # pagamento parcial — saldo devedor > 0
    PAID = "PAID"
    CANCELED = "CANCELED"
    COMPLETED = "COMPLETED"
    ARCHIVED_VERSION = "ARCHIVED_VERSION"


class _StatusStr(str):
    """Subclasse de str com propriedade .value para compatibilidade com templates Jinja2.

    Usada por ReservaStatusType para status customizados (não presentes em
    ReservaStatus enum). Permite que templates continuem usando
    ``{{ reserva.status.value }}`` mesmo para status criados dinamicamente
    via ReservaStatusConfig, sem qualquer alteração nos templates existentes.
    """

    @property
    def value(self) -> str:
        """Retorna o próprio valor da string — espelha a API de enum."""
        return str(self)


class ReservaStatusType(TypeDecorator):
    """TypeDecorator que persiste ReservaStatus como VARCHAR(50).

    Armazena o valor como string no banco de dados, retornando o membro do enum
    ReservaStatus para os status do sistema (PENDING, PAID etc.) e um _StatusStr
    para status customizados criados via ReservaStatusConfig.

    Vantagem sobre db.Enum(ReservaStatus): aceita qualquer string, permitindo que
    novos status criados em tempo de execução via painel admin sejam persistidos
    sem alterar o enum Python ou gerar novas migrações de DB.

    Compatibilidade retroativa: todo código que usa ``reserva.status.value``
    ou ``reserva.status == ReservaStatus.X`` continua funcionando sem mudanças.
    """

    impl = SAString(50)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        """Converte para string ao persistir no banco."""
        if value is None:
            return None
        if isinstance(value, ReservaStatus):
            return value.value
        return str(value)

    def process_result_value(self, value, dialect):
        """Converte de string ao ler do banco.

        Returns:
            Membro de ReservaStatus para valores do sistema;
            _StatusStr para status customizados desconhecidos.
        """
        if value is None:
            return None
        try:
            return ReservaStatus(value)
        except ValueError:
            return _StatusStr(value)


class AssetStatus(str, enum.Enum):
    """Estado operacional de um ativo físico rastreável.

    Valores:
        AVAILABLE:     Disponível para uso ou caução.
        IN_USE:        Em uso por uma reserva ativa.
        UNDER_DEPOSIT: Com caução ativa e paga — aguarda devolução ao cliente.
        MAINTENANCE:   Fora de serviço para manutenção.
        LOST:          Extraviado ou irrecuperável.
    """

    AVAILABLE = "AVAILABLE"
    IN_USE = "IN_USE"
    UNDER_DEPOSIT = "UNDER_DEPOSIT"
    MAINTENANCE = "MAINTENANCE"
    LOST = "LOST"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ReservaSource(db.Model):
    """
    Fonte de reserva — location ou venue onde reservas são feitas.

    Exemplo: "Cachoeira" (Parque Cachoeira), "Vinhedo" (Parque Vinhedo).
    Inserir novo source cria automaticamente permissões RBAC via event listener.
    """

    __tablename__ = "sistur_reserva_sources"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    display_name = db.Column(db.String(100), nullable=False, server_default="")
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True, default=None)
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Campos de integração com Chatwoot — permitem roteamento automático de
    # mensagens para o contexto de bot correto sem lógica hardcoded no n8n.
    # inbox_id é o identificador numérico do inbox no Chatwoot.
    # inbox_name é o nome legível como fallback (inboxes podem ser recriados).
    inbox_id = db.Column(
        db.Integer,
        unique=True,
        nullable=True,
        index=True,
        comment="ID do inbox Chatwoot vinculado a este venue (para routing automático)",
    )
    inbox_name = db.Column(
        db.String(150),
        nullable=True,
        comment="Nome do inbox Chatwoot (fallback quando inbox_id não bate)",
    )

    # Campos de identidade visual para o relatório de comprovante de pagamento.
    # report_name sobrescreve display_name no cabeçalho do comprovante enviado ao cliente.
    # report_logo é URL pública da logo do venue exibida no comprovante.
    report_name = db.Column(
        db.String(150),
        nullable=True,
        comment="Nome do venue exibido no cabeçalho do comprovante de pagamento",
    )
    report_logo = db.Column(
        db.String(500),
        nullable=True,
        comment="URL pública da logo do venue para o comprovante de pagamento",
    )

    # Relationships
    categories = db.relationship(
        "ReservaCategory",
        back_populates="source",
        cascade="all, delete-orphan",
        lazy="select",
    )
    resources = db.relationship(
        "ReservaResource",
        back_populates="source",
        cascade="all, delete-orphan",
        lazy="select",
    )
    config = db.relationship(
        "ReservaConfig",
        back_populates="source",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def __repr__(self) -> str:
        return f"<ReservaSource id={self.id} name={self.name!r} active={self.is_active}>"


blacklist_entry_sources = db.Table(
    "sistur_blacklist_entry_sources",
    db.Column(
        "blacklist_entry_id",
        db.Integer,
        db.ForeignKey("sistur_blacklist_entries.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "source_id",
        db.Integer,
        db.ForeignKey("sistur_reserva_sources.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class BlacklistEntry(db.Model):
    """Banimento aplicado a um ou mais locais de reserva.

    Cada entrada define quais atributos do cliente devem bloquear a criação
    de reservas. O bloqueio é scoped por ReservaSource para permitir que uma
    restrição seja aplicada somente aos locais necessários.
    """

    __tablename__ = "sistur_blacklist_entries"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    cpf = db.Column(db.String(20), nullable=False)
    phone = db.Column(db.String(30), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    block_by_name = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    block_by_cpf = db.Column(db.Boolean, nullable=False, default=True, server_default="1")
    block_by_phone = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    block_by_email = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    deleted_at = db.Column(db.DateTime, nullable=True, default=None)
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    atualizado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.CheckConstraint(
            "block_by_name OR block_by_cpf OR block_by_phone OR block_by_email",
            name="ck_blacklist_entry_has_rule",
        ),
    )

    sources = db.relationship(
        "ReservaSource",
        secondary=blacklist_entry_sources,
        lazy="select",
    )

    def __repr__(self) -> str:
        return f"<BlacklistEntry id={self.id} name={self.name!r}>"


class ReservaCategory(db.Model):
    """
    Categoria de reserva dentro de uma fonte (ex: "Day Use", "Camping").

    Unique constraint: (source_id, name) — não pode haver categoria duplicada na mesma fonte.
    """

    __tablename__ = "sistur_reserva_categories"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(100), nullable=False)
    # Referência estável para URLs e integrações. Mesma razão do
    # ReservaItem.internal_slug: renomear a categoria não pode quebrar o link
    # que o cliente salvou nem a rota do formulário.
    internal_slug = db.Column(db.String(80), nullable=True, index=True)
    # Texto curto exibido no seletor de experiência do formulário público.
    descricao_curta = db.Column(db.Text, nullable=True)
    # Foto da experiência, mostrada no seletor e na home. Vazia = o card
    # continua sendo só texto, que é como ele nasceu; ninguém fica com um
    # buraco cinza porque o operador ainda não escolheu a imagem.
    imagem_url = db.Column(db.String(500), nullable=True)
    # Tema visual do formulário desta experiência. NULL = o padrão claro.
    # "noturno" veste o funil de céu estrelado, que faz sentido no camping e
    # não faria numa visita a vinícola — por isso é escolha do operador por
    # experiência, e não uma regra deduzida de `single_day_only`.
    tema = db.Column(db.String(20), nullable=True, default=None)
    single_day_only = db.Column(db.Boolean, nullable=False, default=False)
    # Horário limite (fuso do sistema) para aceitar reserva com check-in no mesmo dia,
    # via WEB/BOT. NULL = sem limite (comportamento padrão). Balcão nunca é bloqueado —
    # o operador avalia a situação real do local no momento da venda.
    same_day_cutoff_time = db.Column(db.Time, nullable=True, default=None)
    # Janela de horário da estadia. Só faz sentido onde a hora entra no preço —
    # hoje, itens DAILY_PRORATED_HOURLY (camping), onde 08:00→17:00 do dia
    # seguinte custa mais que 24 horas cheias. NULL nas quatro = a categoria não
    # tem dimensão de horário, que é o caso do Day use.
    #
    # Vive aqui, e não no código do site, porque é regra de operação: o camping
    # abre às 8h porque a portaria abre às 8h. Fixar isso no front repetiria o
    # erro dos preços digitados à mão no WordPress, que divergiram do que o
    # sistema cobrava.
    check_in_from = db.Column(db.Time, nullable=True, default=None)
    check_in_until = db.Column(db.Time, nullable=True, default=None)
    check_out_until = db.Column(db.Time, nullable=True, default=None)
    # Permanência mínima em horas. No camping são 24 — meia diária não existe.
    min_stay_hours = db.Column(db.Integer, nullable=True, default=None)
    deleted_at = db.Column(db.DateTime, nullable=True, default=None)
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint(
            "source_id", "name", name="uq_reserva_category_source_name"
        ),
    )

    # Relationships
    source = db.relationship("ReservaSource", back_populates="categories")
    items = db.relationship(
        "ReservaItem",
        back_populates="category",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def __repr__(self) -> str:
        return f"<ReservaCategory id={self.id} source_id={self.source_id} name={self.name!r}>"


class ReservaResource(db.Model):
    """
    Recurso físico (ativo específico) — unidade física reservável pelo atendente no momento do booking.

    Exemplos:
      - 'Churrasqueira A1' (stock=1) — ativo único, escolhido pelo atendente ao criar a reserva
      - 'Capacidade Day Use' (stock=50) — pool compartilhado, decrémenta por quantidade

    Arquitetura de Seleção Dinâmica:
      O resource_id NÃO fica mais no ReservaItem (catálogo). Ele vai para ReservaItemLink
      (a linha de pedido), onde o atendente escolhe o ativo físico em tempo de reserva.
      Assim: 1 tarifa genérica ('Day Use Inteira') funciona com qualquer ativo físico.

    group_id: Grupo visual para accordion no admin. NULL = sem grupo / nível raiz.
    stock_quantity: NULL = estoque infinito. 1 = ativo único (ex: churrasqueira).
    image_url: URL da imagem para galeria no painel do atendente.
    description: Descrição opcional exibida na galeria.
    deleted_at: soft-delete — ativo = deleted_at IS NULL.
    """

    __tablename__ = "sistur_reserva_resources"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    image_url = db.Column(db.String(512), nullable=True)
    stock_quantity = db.Column(db.Integer, nullable=True)
    deleted_at = db.Column(db.DateTime, nullable=True, default=None)
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint("source_id", "name", name="uq_reserva_resource_source_name"),
    )

    # Relationships
    source = db.relationship("ReservaSource", back_populates="resources")
    group = db.relationship("ReservaGroup", foreign_keys=[group_id], lazy="select")
    links = db.relationship("ReservaItemLink", back_populates="resource", lazy="select")
    images = db.relationship(
        "ResourceImage",
        back_populates="resource",
        order_by="ResourceImage.sort_order",
        lazy="select",
        primaryjoin="and_(ResourceImage.resource_id == ReservaResource.id, ResourceImage.deleted_at.is_(None))",
    )

    def __repr__(self) -> str:
        return (
            f"<ReservaResource id={self.id} source_id={self.source_id} "
            f"name={self.name!r} stock={self.stock_quantity} group_id={self.group_id}>"
        )


class ResourceImage(db.Model):
    """Imagem da galeria de um ReservaResource.

    Permite múltiplas fotos por recurso físico (churrasqueira, sala, etc.),
    ordenadas por sort_order para exibição em carousel no frontend.

    sort_order: menor = exibida primeiro. Padrão 0.
    deleted_at: soft-delete — imagens ativas têm deleted_at IS NULL.
    """

    __tablename__ = "sistur_resource_images"

    id = db.Column(db.Integer, primary_key=True)
    resource_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_resources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    image_url = db.Column(db.String(512), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    deleted_at = db.Column(db.DateTime, nullable=True, default=None)
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    resource = db.relationship(
        "ReservaResource",
        back_populates="images",
        foreign_keys=[resource_id],
    )

    def __repr__(self) -> str:
        return f"<ResourceImage id={self.id} resource_id={self.resource_id} order={self.sort_order}>"


class ReservaItem(db.Model):
    """
    Item/Tariff — pricepoint genérico do catálogo de reservas.

    Representa UMA TARIFA de preço, sem vínculo com ativo físico específico.
    O ativo físico (ReservaResource) é escolhido pelo atendente ao criar a reserva
    e fica gravado no ReservaItemLink, não aqui.

    Exemplo: 'Day Use Inteira' (R$ 150/pessoa, FIXED) pode ser reservada em
    qualquer churrasqueira — A1, A2, B1 — sem criar 3 itens de catálogo.

    Campos de preço:
        price:         Preço-base obrigatório. Usado como fallback quando o campo
                       específico de dia (abaixo) for NULL, e como preço único para
                       itens FIXED e PER_HOUR.
        price_weekday: Preço por dia em dias de semana (Seg–Sex). Só afeta PER_DAY.
                       Se NULL, usa `price`.
        price_weekend: Preço por dia nos fins de semana (Sáb e Dom). Só afeta PER_DAY.
                       Se NULL, usa `price`.
        price_holiday: Preço por dia em feriados (cadastrados em GlobalEvent via
                       CalendarService). Tem prioridade sobre price_weekend. Se NULL,
                       cai para price_weekend ou price_weekday conforme o dia.

    billing_type: Como cobrar (FIXED=preço fixo; PER_DAY=diária; PER_HOUR=por hora;
                  DAILY_PRORATED_HOURLY=diária + pró-rata horário para estadas variáveis).
    requires_deposit: Se requer depósito/caução para reservação.
    requires_resource: Se True, a reserva deste item precisa vincular um
                       ReservaResource físico compatível pelo mesmo group_id.
    is_entry_ticket: Se True, cada unidade vendida deste item representa uma pessoa entrando
                     no local. Usado para calcular contadores de "pessoas esperadas" e
                     "novas entradas" na visão semanal do dashboard sem lógica hardcoded.
    visible_to_web: Se True, o item aparece no formulário de reserva do site.
        Eixo separado de `visible_to_bot` — o estacionamento interno sai do site
        e a Tirolesa sai do bot, e nenhum dos dois implica o outro.
    visible_to_bot: Se True, o item pode ser citado pelo bot de WhatsApp e aparece na
                    ferramenta ConsultarItensReserva. False oculta o item do bot sem
                    removê-lo do catálogo admin (ex: itens internos, estacionamento).
    bot_description: Descrição curta opcional exibida pelo bot ao listar itens ao cliente
                     (max 300 chars). Evita que o LLM invente descrições genéricas.
    emoji_label: Emoji associado ao item para o relatório simplificado (ex: "🍖", "💧").
                 Nullable — itens sem emoji usam o nome como fallback no relatório WhatsApp.
    group_id: Grupo de visualização admin (opcional). Exibe como '[Grupo] - Nome' no painel.
    deleted_at: soft-delete timestamp — ativo = deleted_at IS NULL.
    """

    __tablename__ = "sistur_reserva_items"

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_categories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name = db.Column(db.String(150), nullable=False)
    internal_slug = db.Column(db.String(100), unique=True, nullable=True, index=True)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    price_weekday = db.Column(db.Numeric(10, 2), nullable=True)   # Mon–Fri override
    price_weekend = db.Column(db.Numeric(10, 2), nullable=True)   # Sat–Sun override
    price_holiday = db.Column(db.Numeric(10, 2), nullable=True)   # Holiday override (highest priority)
    billing_type = db.Column(db.Enum(BillingType, native_enum=False), nullable=False)
    requires_deposit = db.Column(db.Boolean, default=False, nullable=False)
    requires_resource = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    is_entry_ticket = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    visible_to_bot = db.Column(db.Boolean, nullable=False, default=True, server_default="1")
    # Visibilidade no site público, independente do bot. São eixos diferentes:
    # Tirolesa e Arvorismo estão ocultas para o bot e continuam à venda no site,
    # enquanto o estacionamento interno é o inverso. Reaproveitar
    # `visible_to_bot` juntaria duas decisões que o operador toma separadamente.
    visible_to_web = db.Column(db.Boolean, nullable=False, default=True, server_default="1")
    bot_description = db.Column(db.String(300), nullable=True)
    emoji_label = db.Column(db.String(10), nullable=True)
    deleted_at = db.Column(db.DateTime, nullable=True, default=None)
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint("category_id", "name", name="uq_reserva_item_category_name"),
    )

    # Relationships
    category = db.relationship("ReservaCategory", back_populates="items")
    group = db.relationship("ReservaGroup", back_populates="items", lazy="select")

    @property
    def display_name(self) -> str:
        """Retorna o nome de exibição com prefixo de grupo para o painel admin.

        Returns:
            String no formato '[Grupo] - Nome' ou '[Grupo > Sub] - Nome' se o item
            pertencer a um grupo ativo; caso contrário, retorna apenas o nome.
        """
        if self.group and self.group.deleted_at is None:
            return f"[{self.group.display_path}] - {self.name}"
        return self.name

    def __repr__(self) -> str:
        return (
            f"<ReservaItem id={self.id} name={self.name!r} category_id={self.category_id} "
            f"billing={self.billing_type.value}>"
        )


# ---------------------------------------------------------------------------
# ReservaTemplate — Dynamic Form Builder Templates
# ---------------------------------------------------------------------------

# Tabelas de associação M2M (Table objects sem model próprio).
# Definidas antes de ReservaTemplate para que os relacionamentos possam
# referenciar as tabelas via string 'sistur_template_*'.

template_groups = db.Table(
    "sistur_template_groups",
    db.Column(
        "template_id",
        db.Integer,
        db.ForeignKey("sistur_reserva_templates.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "group_id",
        db.Integer,
        db.ForeignKey("sistur_reserva_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

template_items = db.Table(
    "sistur_template_items",
    db.Column(
        "template_id",
        db.Integer,
        db.ForeignKey("sistur_reserva_templates.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "item_id",
        db.Integer,
        db.ForeignKey("sistur_reserva_items.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class ReservaTemplate(db.Model):
    """Template de reserva — definição reutilizável do formulário dinâmico.

    O template controla quais grupos de recursos (ex: 'Churrasqueiras') e
    quais itens/tarifas (ex: 'Inteira', 'Meia') ficam disponíveis quando o
    operador abre o Construtor de Reservas para aquele tipo de booking.

    `timeout_hours` determina por quanto tempo uma reserva PENDING gerada
    por este template permanece ativa antes de expirar automaticamente.
    NULL = sem expiração automática (reserva fica PENDING indefinidamente).

    Quando nenhum template é fornecido na criação da reserva, o serviço
    aplica um soft-lock padrão de 15 minutos (comportamento legado).

    Attributes:
        slug: Identificador único usado em URLs e código (ex: 'day-use').
        name: Nome legível exibido no seletor de templates (ex: 'Day Use').
        description: Texto explicativo exibido ao operador no formulário.
        timeout_hours: Horas até expiração automática de PENDING.
            NULL = sem timeout (reserva não expira automaticamente).
        is_active: Se False, o template não aparece no seletor para novos
            bookings, mas reservas existentes com este template não são afetadas.
        groups: Grupos de recursos disponíveis sob este template (M2M via
            sistur_template_groups). Define quais ativos físicos o operador
            pode selecionar no wizard de reserva.
        items: Itens/tarifas vendáveis sob este template (M2M via
            sistur_template_items). Define o catálogo de preços disponível.
        deleted_at: Soft-delete — ativo = deleted_at IS NULL.
    """

    __tablename__ = "sistur_reserva_templates"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(100), unique=True, nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    timeout_hours = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    deleted_at = db.Column(db.DateTime, nullable=True, default=None)
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    atualizado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # M2M: quais grupos de recursos este template expõe ao operador
    groups = db.relationship(
        "ReservaGroup",
        secondary="sistur_template_groups",
        lazy="select",
    )
    # M2M: quais itens/tarifas este template expõe ao operador
    items = db.relationship(
        "ReservaItem",
        secondary="sistur_template_items",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<ReservaTemplate id={self.id} slug={self.slug!r} "
            f"timeout_hours={self.timeout_hours} active={self.is_active}>"
        )


class Reserva(db.Model):
    """
    Reserva — pedido de booking em uma fonte específica.

    Versioning: Edições NÃO modificam esta linha. Em vez disso:
      1. Marca a linha anterior com status=ARCHIVED_VERSION
      2. Insere nova linha com mesmo group_id + version incrementado
      Todas as versões compartilham group_id (UUID String). Chave única: (group_id, version).

    Soft Delete: deleted_at IS NULL = registro ativo.
    """

    __tablename__ = "sistur_reservas"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.String(36), nullable=False, index=True)
    version = db.Column(db.Integer, nullable=False, default=1)
    source_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_sources.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    category_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    template_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_templates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    customer_name = db.Column(db.String(255), nullable=False)
    customer_document = db.Column(db.String(20), nullable=True)
    origin = db.Column(db.Enum(ReservaOrigin, native_enum=False), nullable=False)
    status = db.Column(ReservaStatusType(), default=ReservaStatus.PENDING, nullable=False, index=True)
    check_in_date = db.Column(db.DateTime, nullable=False, index=True)
    check_out_date = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    metadata_info = db.Column(db.JSON, nullable=True)
    # Pricing snapshots (server-calculated, for audit trail)
    subtotal_amount = db.Column(db.Numeric(10, 2), nullable=True)
    discount_amount = db.Column(db.Numeric(10, 2), nullable=True)
    service_fee_amount = db.Column(db.Numeric(10, 2), nullable=True)
    total_amount = db.Column(db.Numeric(10, 2), nullable=True)
    payment_id = db.Column(db.String(50), nullable=True, index=True)  # MP payment ID para estornos e rastreio
    payment_data = db.Column(db.JSON, nullable=True)  # Registro imutável do pagamento: transaction_id, amount_paid, payment_type, approval_date
    pix_data = db.Column(db.JSON, nullable=True)  # Dados do QR Code PIX: qr_code, qr_code_base64, ticket_url, generated_at (sobrescrito a cada geração)
    # Operador: quem criou/editou esta versão específica (FK para Funcionario)
    criado_por_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Visibilidade: primeira vez que esta versão foi aberta por um operador
    visualizada_em = db.Column(db.DateTime, nullable=True, default=None)
    # Anotações internas do operador (copiadas entre versões)
    notas_operador = db.Column(db.Text, nullable=True)

    @property
    def observacao_cliente(self) -> str | None:
        """Observação que o cliente escreveu no formulário do site.

        Mora em ``metadata_info`` por não ter coluna própria — o campo nasceu no
        plugin do WordPress e o Sistur manteve a chave para os relatórios
        existentes continuarem lendo.

        Returns:
            O texto, ou None quando não houver.
        """
        texto = (self.metadata_info or {}).get("observacoes")
        return texto.strip() if isinstance(texto, str) and texto.strip() else None

    @property
    def tem_anotacao(self) -> bool:
        """Se há qualquer anotação — do cliente ou do operador.

        Existe para o marcador das listagens: quem olha a fila precisa saber que
        há algo escrito, e não de quem partiu. Antes o ícone só considerava
        ``notas_operador``, então uma observação do cliente passava sem sinal.

        Returns:
            True se houver nota do operador ou observação do cliente.
        """
        return bool((self.notas_operador or "").strip()) or self.observacao_cliente is not None
    # Nota visível no relatório diário (copiada entre versões)
    nota_relatorio = db.Column(db.Text, nullable=True)
    agreed_notices_json = db.Column(db.JSON, nullable=True, default=None)
    # Desconto manual em % aplicado pelo operador de balcão (ex: 10.00 = 10%).
    # Salvo como percentual para que seja reaplicado proporcionalmente ao subtotal
    # em toda nova versão (edições, mudança de status, adição de itens).
    # NULL = sem desconto manual. Substituiu o antigo _manual_discount_amount em metadata_info.
    desconto_percentual_manual = db.Column(db.Numeric(5, 2), nullable=True, default=None)
    # Sinaliza que esta reserva (PENDING) entraria em conflito com reservas já pagas
    # se confirmada. Não bloqueia a criação — serve de alerta visual para o operador.
    # Migration: ALTER TABLE sistur_reservas ADD COLUMN overbooking_risco TINYINT(1) NOT NULL DEFAULT 0;
    overbooking_risco = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    # Setado pelo Active Defense quando o pagamento foi reembolsado automaticamente por overbooking.
    # Migration: ALTER TABLE sistur_reservas ADD COLUMN overbooking_extornada TINYINT(1) NOT NULL DEFAULT 0;
    overbooking_extornada = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    deleted_at = db.Column(db.DateTime, nullable=True, default=None)
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    atualizado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint("group_id", "version", name="uq_reserva_group_version"),
    )

    # Relationships
    source = db.relationship("ReservaSource", lazy="select")
    category = db.relationship("ReservaCategory", lazy="select")
    template = db.relationship("ReservaTemplate", lazy="select")
    criado_por = db.relationship("Funcionario", foreign_keys=[criado_por_id], lazy="select")
    items = db.relationship(
        "ReservaItemLink",
        back_populates="reserva",
        cascade="all, delete-orphan",
        lazy="select",
    )
    deposits = db.relationship(
        "SecurityDeposit",
        back_populates="reserva",
        lazy="select",
        primaryjoin=(
            "and_(SecurityDeposit.reserva_id == Reserva.id,"
            " SecurityDeposit.deleted_at.is_(None))"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Reserva id={self.id} group={self.group_id!r} v={self.version} "
            f"status={self.status.value} customer={self.customer_name!r}>"
        )


class ReservaItemLink(db.Model):
    """
    Linha de pedido de reserva — associa Reserva, ReservaItem (tarifa) e ReservaResource (ativo).

    O resource_id armazena QUAL ativo físico específico foi escolhido pelo atendente
    no momento da reserva (ex: Churrasqueira A1). Isso é a implementação de
    Seleção Dinâmica de Recurso: a tarifa é genérica, o ativo é escolhido no pedido.

    Constraint: (reserva_id, resource_id) — um ativo físico não pode aparecer duas vezes
    na mesma reserva. Se resource_id for NULL, a linha não participa desta constraint.

    locked_price: Preço congelado no momento da reserva (Numeric, não FK).
    deleted_at: marca exclusão lógica de item da reserva.
    """

    __tablename__ = "sistur_reserva_item_links"

    id = db.Column(db.Integer, primary_key=True)
    reserva_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reservas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_items.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    resource_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_resources.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    quantity = db.Column(db.Integer, nullable=False, default=1)
    locked_price = db.Column(db.Numeric(10, 2), nullable=False)
    # Pricing breakdown from ReservaPricingService (server-calculated snapshot)
    unit_price = db.Column(db.Numeric(10, 2), nullable=True)
    item_total = db.Column(db.Numeric(10, 2), nullable=True)
    # Controle de entrega — preenchido quando o operador confirma que o item foi entregue
    entregue_em = db.Column(db.DateTime, nullable=True, default=None)
    entregue_por_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    deleted_at = db.Column(db.DateTime, nullable=True, default=None)

    __table_args__ = (
        # Um ativo físico (resource) não pode aparecer duas vezes na mesma reserva.
        # NULL resource_id não participa do constraint (MySQL: NULL != NULL).
        db.UniqueConstraint("reserva_id", "resource_id", name="uq_reserva_resource_link"),
    )

    # Relationships
    reserva = db.relationship("Reserva", back_populates="items")
    item = db.relationship("ReservaItem", lazy="select")
    resource = db.relationship("ReservaResource", back_populates="links", lazy="select")
    entregue_por = db.relationship("Funcionario", foreign_keys=[entregue_por_id], lazy="select")

    def __repr__(self) -> str:
        return (
            f"<ReservaItemLink reserva_id={self.reserva_id} item_id={self.item_id} "
            f"resource_id={self.resource_id} qty={self.quantity} price={self.locked_price}>"
        )


class ReservaConfig(db.Model):
    """
    Configuração dinâmica — key-value store JSON para settings de reservas.

    scope_id: NULL = global a todos sources. Integer = específico para um source.

    Exemplos de config_key:
      - 'taxa_servico' (float): 10.0
      - 'precificacao_dinamica' (bool): true
      - 'margem_lucro_minima' (float): 25.0
      - 'limite_antecedencia_dias' (int): 90
      - 'email_confirmacao' (bool): true

    config_value: JSON field — pode conter qualquer estrutura (object, array, scalar).
    """

    __tablename__ = "sistur_reserva_config"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_sources.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    config_key = db.Column(db.String(100), nullable=False)
    config_value = db.Column(db.JSON, nullable=False)
    description = db.Column(db.String(255), nullable=True)
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    atualizado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint("source_id", "config_key", name="uq_reserva_config_source_key"),
    )

    # Relationships
    source = db.relationship("ReservaSource", back_populates="config")

    def __repr__(self) -> str:
        return (
            f"<ReservaConfig id={self.id} source_id={self.source_id} "
            f"key={self.config_key!r} value={self.config_value}>"
        )


# ---------------------------------------------------------------------------
# AdvanceDiscountRule — Motor de desconto por reserva antecipada
# ---------------------------------------------------------------------------


class AdvanceDiscountRule(db.Model):
    """Regra de desconto automático por antecedência de reserva (Early Bird).

    Define descontos aplicados quando o cliente reserva com antecedência mínima
    configurada. Regras são vinculadas a uma fonte (ReservaSource) e
    opcionalmente a uma categoria (ReservaCategory).

    Hierarquia de prioridade na seleção:
      1. Regra com category_id específico prevalece sobre regra da fonte (NULL).
      2. Dentro do mesmo nível, seleciona a regra com maior min_days_in_advance
         dentre as que o booking qualifica (days_in_advance >= min_days_in_advance).

    Attributes:
        source_id: FK obrigatória para ReservaSource. Regra sempre pertence a uma fonte.
        category_id: FK opcional. NULL = aplica-se a todas categorias da fonte.
        min_days_in_advance: Quantidade mínima de dias de antecedência para ativar.
        discount_percentage: Percentual de desconto sobre o subtotal (ex: 10.00 = 10%).
        is_active: Desativa sem necessidade de exclusão — permite suspensão temporária.
        criado_em: Timestamp de criação (UTC).
        atualizado_em: Timestamp da última atualização (UTC).
    """

    __tablename__ = "sistur_advance_discount_rules"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=False,  # índice explícito em __table_args__ para preservar nome do DB
    )
    category_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_categories.id", ondelete="CASCADE"),
        nullable=True,
        index=False,  # índice explícito em __table_args__ para preservar nome do DB
    )
    min_days_in_advance = db.Column(db.Integer, nullable=False)
    discount_percentage = db.Column(db.Numeric(5, 2), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    atualizado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Índices com nomes explícitos que correspondem ao banco de dados existente.
    # index=True nos FK columns geraria nomes no formato ix_sistur_<table>_<col>
    # que divergem dos nomes criados pela migration original (ix_advance_discount_rules_*),
    # causando falso-positivo no Alembic autogenerate e erro 1553 no MySQL.
    __table_args__ = (
        db.Index("ix_advance_discount_rules_source", "source_id"),
        db.Index("ix_advance_discount_rules_category", "category_id"),
    )

    # Relationships
    source = db.relationship("ReservaSource", lazy="select")
    category = db.relationship("ReservaCategory", lazy="select")
    stay_tiers = db.relationship(
        "AdvanceDiscountStayTier",
        back_populates="rule",
        lazy="select",
        cascade="all, delete-orphan",
        order_by="AdvanceDiscountStayTier.min_stay_days",
    )

    def __repr__(self) -> str:
        cat = f" cat={self.category_id}" if self.category_id else " cat=*"
        return (
            f"<AdvanceDiscountRule id={self.id} source={self.source_id}{cat} "
            f"min_days={self.min_days_in_advance} pct={self.discount_percentage}%>"
        )


# ---------------------------------------------------------------------------
# AdvanceDiscountStayTier — Tiers de desconto por dias de estadia
# ---------------------------------------------------------------------------


class AdvanceDiscountStayTier(db.Model):
    """Tier de desconto adicional baseado em número de dias de estadia.

    Opcional: quando uma AdvanceDiscountRule possui tiers cadastrados, o sistema
    seleciona o tier com maior min_stay_days que a estadia ainda qualifica e usa
    seu discount_percentage no lugar do percentual base da regra.
    Quando nenhum tier se aplica (estadia curta), usa o discount_percentage da regra.

    Attributes:
        rule_id: FK obrigatória para AdvanceDiscountRule.
        min_stay_days: Mínimo de dias de estadia para ativar este tier.
        discount_percentage: Percentual de desconto deste tier (substitui o da regra pai).
    """

    __tablename__ = "sistur_advance_discount_stay_tiers"

    id = db.Column(db.Integer, primary_key=True)
    rule_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_advance_discount_rules.id", ondelete="CASCADE"),
        nullable=False,
    )
    min_stay_days = db.Column(db.Integer, nullable=False)
    discount_percentage = db.Column(db.Numeric(5, 2), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("rule_id", "min_stay_days", name="uq_stay_tier_rule_days"),
        db.Index("ix_stay_tier_rule_id", "rule_id"),
    )

    rule = db.relationship("AdvanceDiscountRule", back_populates="stay_tiers")

    def __repr__(self) -> str:
        return (
            f"<AdvanceDiscountStayTier id={self.id} rule={self.rule_id} "
            f"min_stay={self.min_stay_days} pct={self.discount_percentage}%>"
        )


# ---------------------------------------------------------------------------
# CalendarDiscount — Desconto por data específica de check-in
# ---------------------------------------------------------------------------


class CalendarDiscount(db.Model):
    """Desconto por data específica de check-in com tiers de antecedência.

    Tem prioridade máxima sobre AdvanceDiscountRule. O operador marca uma data
    especial (ex: Black Friday) e define tiers de antecedência; o desconto só é
    aplicado se o cliente qualifica para pelo menos um tier.

    Attributes:
        source_id: FK obrigatória para ReservaSource.
        category_id: FK opcional. NULL = aplica-se a todas as categorias da fonte.
        discount_date: Data específica de check-in a que se aplica.
        discount_percentage: Não usado em pricing — mantido para compatibilidade de schema.
        label: Nome descritivo opcional (ex: "Black Friday", "Carnaval 2027").
        is_active: Desativa sem excluir — permite suspensão temporária.
    """

    __tablename__ = "sistur_calendar_discounts"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_categories.id", ondelete="CASCADE"),
        nullable=True,
    )
    discount_date = db.Column(db.Date, nullable=False)
    discount_percentage = db.Column(db.Numeric(5, 2), nullable=True)
    label = db.Column(db.String(100), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    atualizado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint(
            "source_id", "category_id", "discount_date",
            name="uq_calendar_discount_source_cat_date",
        ),
        db.Index("ix_calendar_discount_source", "source_id"),
        db.Index("ix_calendar_discount_date", "discount_date"),
    )

    source = db.relationship("ReservaSource", lazy="select")
    category = db.relationship("ReservaCategory", lazy="select")
    advance_tiers = db.relationship(
        "CalendarDiscountAdvanceTier",
        back_populates="calendar_discount",
        lazy="select",
        cascade="all, delete-orphan",
        order_by="CalendarDiscountAdvanceTier.min_days_in_advance",
    )

    def __repr__(self) -> str:
        cat = f" cat={self.category_id}" if self.category_id else " cat=*"
        return (
            f"<CalendarDiscount id={self.id} source={self.source_id}{cat} "
            f"date={self.discount_date}>"
        )


class CalendarDiscountAdvanceTier(db.Model):
    """Tier 2D de antecedência + estadia vinculado a um CalendarDiscount.

    Cada tier combina dois critérios de qualificação (ambos obrigatórios):
      - min_days_in_advance: o cliente deve ter reservado com N+ dias de antecedência.
      - min_stay_days (opcional): a estadia deve ter N+ noites. NULL = sem restrição de estadia.

    O tier mais específico que o cliente qualifica é aplicado, usando como critério
    de desempate: maior min_days_in_advance, depois maior min_stay_days (NULL = menos específico).
    Se nenhum tier qualifica em ambas as dimensões, o CalendarDiscount não produz desconto.

    Attributes:
        calendar_discount_id: FK obrigatória para CalendarDiscount.
        min_days_in_advance:  Antecedência mínima em dias para ativar este tier.
        min_stay_days:        Estadia mínima em noites (opcional). None = qualquer estadia.
        discount_percentage:  Percentual de desconto aplicado quando o tier qualifica.
    """

    __tablename__ = "sistur_calendar_discount_stay_tiers"

    id = db.Column(db.Integer, primary_key=True)
    calendar_discount_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_calendar_discounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    min_days_in_advance = db.Column(db.Integer, nullable=False)
    min_stay_days = db.Column(db.Integer, nullable=True)
    discount_percentage = db.Column(db.Numeric(5, 2), nullable=False)

    __table_args__ = (
        # MySQL trata NULLs como distintos em unique indexes, por isso a unicidade
        # para min_stay_days=NULL é aplicada via validação na rota (ver tier_criar).
        db.UniqueConstraint(
            "calendar_discount_id", "min_days_in_advance", "min_stay_days",
            name="uq_cal_stay_tier_discount_days",
        ),
        db.Index("ix_cal_stay_tier_discount_id", "calendar_discount_id"),
    )

    calendar_discount = db.relationship("CalendarDiscount", back_populates="advance_tiers")

    def __repr__(self) -> str:
        stay = f" stay≥{self.min_stay_days}d" if self.min_stay_days else ""
        return (
            f"<CalendarDiscountAdvanceTier id={self.id} "
            f"cd={self.calendar_discount_id} adv≥{self.min_days_in_advance}d"
            f"{stay} pct={self.discount_percentage}%>"
        )


# ---------------------------------------------------------------------------
# ReservaStatusConfig — Dynamic Status Configuration
# ---------------------------------------------------------------------------


_STATUS_CONFIG_DEFAULTS = [
    {
        "internal_name": "PENDING",
        "label": "Pendente",
        "color_hex": "#F59E0B",
        "blocks_resource": True,
        "show_on_dashboard": True,
        "is_system_reserved": True,
    },
    {
        "internal_name": "PARTIALLY_PAID",
        "label": "Parcialmente Pago",
        "color_hex": "#3B82F6",
        "blocks_resource": True,
        "show_on_dashboard": True,
        "is_system_reserved": True,
    },
    {
        "internal_name": "PAID",
        "label": "Pago",
        "color_hex": "#10B981",
        "blocks_resource": True,
        "show_on_dashboard": True,
        "is_system_reserved": True,
    },
    {
        "internal_name": "COMPLETED",
        "label": "Finalizada",
        "color_hex": "#0D9488",
        "blocks_resource": False,
        "show_on_dashboard": True,
        "is_system_reserved": True,
    },
    {
        "internal_name": "CANCELED",
        "label": "Cancelada",
        "color_hex": "#EF4444",
        "blocks_resource": False,
        "show_on_dashboard": True,
        "is_system_reserved": True,
    },
    {
        "internal_name": "DEPOSIT",
        "label": "Com Caução",
        "color_hex": "#7C3AED",
        "blocks_resource": True,
        "show_on_dashboard": False,
        "is_system_reserved": False,
    },
]


class ReservaStatusConfig(db.Model):
    """Configuração dinâmica de status de reserva.

    Permite customizar rótulos, cores e regras de negócio dos status de reserva
    sem alterar código-fonte. Substitui checagens hardcoded de ReservaStatus
    por flags consultadas em tempo de execução.

    A tabela é pré-populada com valores padrão (seed) via event listener
    after_create para garantir consistência em instalações novas e testes.

    Attributes:
        internal_name: Slug único mapeado ao enum ReservaStatus (ex: 'PENDING').
            O valor especial 'DEPOSIT' controla a cor visual de slots com caução
            no Mapa de Ativos.
        label: Rótulo em pt-BR exibido na interface (ex: 'Pendente').
        color_hex: Cor hexadecimal para badges e indicadores visuais (ex: '#F59E0B').
        blocks_resource: Se True, reservas neste status ocupam estoque físico
            e são contadas na verificação de disponibilidade.
        show_on_dashboard: Se True, este status aparece nos cards do dashboard.
        is_system_reserved: Se True, impede exclusão ou renomeação via painel admin.
        auto_cancel_hours: Horas após expires_at para cancelar automaticamente uma
            reserva ainda neste status. None desativa — é o padrão, para que nenhuma
            reserva existente seja cancelada só por esta coluna passar a existir.
            Só faz sentido em status não-finais (PENDING, PARTIALLY_PAID); em status
            finais o campo fica sem efeito, pois a varredura nunca os encontra lá.
    """

    __tablename__ = "sistur_reserva_status_config"

    id = db.Column(db.Integer, primary_key=True)
    internal_name = db.Column(db.String(50), unique=True, nullable=False)
    label = db.Column(db.String(100), nullable=False)
    color_hex = db.Column(db.String(7), nullable=False, default="#6B7280")
    blocks_resource = db.Column(db.Boolean, nullable=False, default=False)
    show_on_dashboard = db.Column(db.Boolean, nullable=False, default=True)
    is_system_reserved = db.Column(db.Boolean, nullable=False, default=False)
    auto_cancel_hours = db.Column(db.Integer, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<ReservaStatusConfig id={self.id} internal_name={self.internal_name!r} "
            f"blocks_resource={self.blocks_resource}>"
        )


@event.listens_for(ReservaStatusConfig.__table__, "after_create")
def _seed_status_config(target, connection, **kw):
    """Popula a tabela com valores padrão ao ser criada pela primeira vez.

    Args:
        target: SQLAlchemy Table object (ReservaStatusConfig.__table__).
        connection: Database connection usada pelo DDL.
        **kw: Argumentos adicionais passados pelo evento (não utilizados).
    """
    connection.execute(target.insert(), _STATUS_CONFIG_DEFAULTS)


# ---------------------------------------------------------------------------
# SQLAlchemy Event Listener — Dynamic RBAC
# ---------------------------------------------------------------------------


@event.listens_for(ReservaSource, "after_insert")
def _criar_permissoes_source(mapper, connection, target):
    """Cria permissões RBAC automáticas para roles super_admin ao inserir nova fonte.

    Quando uma nova ReservaSource é inserida, esta função dispara e adiciona
    permissões (view + edit) para a fonte em todos os roles com is_super_admin=True.

    Args:
        mapper: SQLAlchemy mapper (unused).
        connection: Database connection da transação em andamento.
        target: A instância de ReservaSource que foi inserida.
    """
    del mapper
    try:
        from app.models.role import Role, RolePermission

        # Cria nome do módulo baseado no nome da fonte
        modulo = f"reservas_{target.name.lower().replace(' ', '_').replace('-', '_')}"

        roles_table = Role.__table__
        permissions_table = RolePermission.__table__
        super_role_ids = connection.execute(
            select(roles_table.c.id).where(
                roles_table.c.is_super_admin.is_(True),
                roles_table.c.ativo.is_(True),
            )
        ).scalars()

        inserts = []
        now = datetime.now(timezone.utc)
        for role_id in super_role_ids:
            for acao in ("view", "edit"):
                exists = connection.execute(
                    select(permissions_table.c.id)
                    .where(
                        and_(
                            permissions_table.c.role_id == role_id,
                            permissions_table.c.modulo == modulo,
                            permissions_table.c.acao == acao,
                            permissions_table.c.source_id.is_(None),
                        )
                    )
                    .limit(1)
                ).first()
                if not exists:
                    inserts.append(
                        {
                            "role_id": role_id,
                            "modulo": modulo,
                            "acao": acao,
                            "source_id": None,
                            "criado_em": now,
                        }
                    )

        if inserts:
            connection.execute(
                permissions_table.insert(),
                inserts,
            )
    except Exception as e:
        # Log silenciosamente — não deve causar falha na inserção do source
        import sys

        print(
            f"Warning: Falha ao criar permissoes automaticas para ReservaSource: {e}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# PhysicalAsset — Ativo Físico Rastreável
# ---------------------------------------------------------------------------


class PhysicalAsset(db.Model):
    """Ativo físico individual rastreável — item que pode exigir caução.

    Complementa o ReservaResource (controle de estoque/pool) com rastreamento
    individual de itens específicos: churrasqueiras, câmeras, chaves, equipamentos.

    Diferença conceitual:
      - ReservaResource: pool de estoque (ex: 'Capacidade Day Use' = 50 vagas).
      - PhysicalAsset: item único e identificável (ex: 'Churrasqueira A1 — grelhador inox').

    Um PhysicalAsset pode opcionalmente referenciar um ReservaResource quando o
    ativo é também reservável. Se resource_id for NULL, o ativo existe apenas para
    fins de caução (ex: chave do cofre, câmera de segurança emprestada).

    Attributes:
        source_id: Fonte de reserva à qual o ativo pertence (nullable).
        resource_id: Recurso físico relacionado para mapa de ocupação (nullable).
        name: Nome descritivo do ativo (ex: 'Churrasqueira A1 — Coberta').
        asset_type: Categoria livre do ativo (ex: 'Churrasqueira', 'Câmera', 'Chave').
        current_status: Estado operacional atual do ativo (enum AssetStatus).
        serial_number: Número de série ou identificador externo (opcional).
        deleted_at: soft-delete — ativo = deleted_at IS NULL.
    """

    __tablename__ = "sistur_physical_assets"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_sources.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    resource_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_resources.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name = db.Column(db.String(200), nullable=False)
    asset_type = db.Column(db.String(100), nullable=True)
    current_status = db.Column(
        db.Enum(AssetStatus, native_enum=False),
        nullable=False,
        default=AssetStatus.AVAILABLE,
        index=True,
    )
    serial_number = db.Column(db.String(100), nullable=True)
    deleted_at = db.Column(db.DateTime, nullable=True, default=None)
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    atualizado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    source = db.relationship("ReservaSource", lazy="select")
    resource = db.relationship("ReservaResource", lazy="select")
    deposits = db.relationship(
        "SecurityDeposit",
        back_populates="asset",
        lazy="select",
        primaryjoin=(
            "and_(SecurityDeposit.linked_asset_id == PhysicalAsset.id,"
            " SecurityDeposit.deleted_at.is_(None))"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<PhysicalAsset id={self.id} name={self.name!r} "
            f"type={self.asset_type!r} status={self.current_status.value}>"
        )


# ---------------------------------------------------------------------------
# SecurityDeposit — Caução de Reserva
# ---------------------------------------------------------------------------


class SecurityDeposit(db.Model):
    """Caução de reserva — depósito de segurança independente dos itens.

    Representa um valor caução cobrado do cliente, opcionalmente vinculado a um
    PhysicalAsset específico (ex: R$ 200 de caução pela Churrasqueira A1).

    A caução é um componente financeiro *separado* do total_amount da Reserva.
    Cauções não-pagas incrementam o pending_balance sem alterar total_amount
    ou subtotal, e aparecem separadas nos logs financeiros para rastreabilidade.

    Ciclo de vida:
      1. Criada: is_paid=False, is_refunded=False.
      2. Paga (cliente entregou o valor): is_paid=True.
         → PhysicalAsset.current_status → UNDER_DEPOSIT (se vinculado).
      3. Devolvida (ativo retornado em bom estado): is_refunded=True.
         → PhysicalAsset.current_status → AVAILABLE (se vinculado).

    Attributes:
        reserva_id: Versão específica da reserva à qual esta caução pertence.
        reserva_group_id: UUID do grupo de versões — permite queries cross-version.
        amount: Valor da caução em Reais.
        description: Descrição livre (ex: 'Caução Churrasqueira A1').
        is_paid: True quando o valor foi recebido do cliente.
        is_refunded: True quando a caução foi devolvida ao cliente.
        linked_asset_id: Ativo físico vinculado (opcional).
        deleted_at: soft-delete — ativo = deleted_at IS NULL.
    """

    __tablename__ = "sistur_security_deposits"

    id = db.Column(db.Integer, primary_key=True)
    reserva_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reservas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reserva_group_id = db.Column(db.String(36), nullable=False, index=True)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    description = db.Column(db.Text, nullable=True)
    is_paid = db.Column(db.Boolean, nullable=False, default=False)
    is_refunded = db.Column(db.Boolean, nullable=False, default=False)
    linked_asset_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_physical_assets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    deleted_at = db.Column(db.DateTime, nullable=True, default=None)
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    atualizado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    reserva = db.relationship(
        "Reserva",
        back_populates="deposits",
        foreign_keys=[reserva_id],
    )
    asset = db.relationship(
        "PhysicalAsset",
        back_populates="deposits",
        foreign_keys=[linked_asset_id],
    )

    def __repr__(self) -> str:
        return (
            f"<SecurityDeposit id={self.id} reserva_id={self.reserva_id} "
            f"amount={self.amount} paid={self.is_paid} refunded={self.is_refunded}>"
        )


# ---------------------------------------------------------------------------
# ResourceNotice — Aviso de Recurso
# ---------------------------------------------------------------------------


class ResourceNotice(db.Model):
    """
    Aviso de recurso — notificação associada a um período e opcionalmente a um source.

    scope:
        source_id = NULL  → aviso GLOBAL (afeta todos os sources)
        source_id = int   → aviso LOCAL  (afeta apenas o source especificado)

    Níveis de bloqueio (não mutuamente exclusivos):
        blocks_source: bloqueia criação de qualquer reserva no source durante o período.
        blocks_items:  torna itens do source indisponíveis no picker de reserva.
            - blocked_item_ids = NULL → bloqueia TODOS os itens do source.
            - blocked_item_ids = [1,2,3] → bloqueia apenas os itens listados.

    Recorrência semanal (opcional):
        recurring_weekday: quando preenchido (0=segunda ... 6=domingo, convenção de
            date.weekday()), o aviso NÃO cobre o intervalo [start_date, end_date]
            inteiro — cobre apenas as ocorrências desse dia da semana dentro do
            intervalo. Ex.: "toda quarta-feira, de 01/09 a 31/12" com um único
            registro, em vez de uma linha por quarta-feira. NULL = comportamento
            padrão (intervalo contínuo, sem recorrência).

    Quando ambos False, o aviso é apenas informativo e exige aceite (checkbox) do cliente.
    deleted_at: soft-delete — ativo = deleted_at IS NULL.
    """

    __tablename__ = "sistur_resource_notices"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_sources.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    event_name = db.Column(db.String(200), nullable=False)
    start_date = db.Column(db.Date, nullable=False, index=True)
    end_date = db.Column(db.Date, nullable=False, index=True)
    recurring_weekday = db.Column(db.Integer, nullable=True)
    blocks_source = db.Column(db.Boolean, default=False, nullable=False)
    blocks_items = db.Column(db.Boolean, default=False, nullable=False)
    blocked_item_ids = db.Column(db.JSON, nullable=True, default=None)
    deleted_at = db.Column(db.DateTime, nullable=True, default=None)
    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationship
    source = db.relationship("ReservaSource", lazy="select")

    def __repr__(self) -> str:
        scope = f"source={self.source_id}" if self.source_id else "GLOBAL"
        return (
            f"<ResourceNotice id={self.id} {scope!r} "
            f"event={self.event_name!r} {self.start_date}→{self.end_date}>"
        )


class ReservaPreferencia(db.Model):
    """Preferências do operador para o módulo de reservas (Balcão/PDV).

    Armazena os valores padrão que serão pré-preenchidos no formulário
    'Nova Reserva' para cada funcionário. O operador pode sobrescrever
    qualquer valor antes de submeter o formulário.

    Unique: um registro por funcionário (upsert na rota de predefinições).
    """

    __tablename__ = "sistur_reserva_preferencias"

    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_funcionarios.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    default_source_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    default_category_id = db.Column(
        db.Integer,
        db.ForeignKey("sistur_reserva_categories.id", ondelete="SET NULL"),
        nullable=True,
    )
    # internal_name de ReservaStatusConfig (ex: "PAID", "PENDING", ou slug customizado)
    default_balcao_status = db.Column(db.String(50), nullable=True)
    # internal_name de ReservaStatusConfig para vendas PDV com pagamento imediato.
    pdv_default_status = db.Column(db.String(50), nullable=True)
    # Lista ordenada de ReservaItem.id que aparecem no topo do catálogo PDV.
    pdv_favorite_item_ids = db.Column(db.JSON, nullable=True, default=list)

    criado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    atualizado_em = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    funcionario = db.relationship(
        "Funcionario",
        foreign_keys=[funcionario_id],
        lazy="select",
    )
    default_source = db.relationship(
        "ReservaSource",
        foreign_keys=[default_source_id],
        lazy="select",
    )
    default_category = db.relationship(
        "ReservaCategory",
        foreign_keys=[default_category_id],
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<ReservaPreferencia funcionario={self.funcionario_id} "
            f"source={self.default_source_id} cat={self.default_category_id} "
            f"status={self.default_balcao_status!r} "
            f"pdv_status={self.pdv_default_status!r}>"
        )
