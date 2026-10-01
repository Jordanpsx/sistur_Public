# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
ReservaService — Business logic for the Reservas (Bookings) module.

This service handles all mutations and queries on the Reserva data model,
including create, list, detail, availability checks, and source/category
management.

No Flask imports — pure Python, Cython-ready for future compilation.
"""

from __future__ import annotations

import logging
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


class OverbookingError(ValueError):
    """Lançada quando uma reserva conflita com outra já existente.

    Subclasse de ValueError para compatibilidade com o código existente
    (auto-seleção de recursos usa ``except ValueError: continue``).

    Attributes:
        resource_name: Nome do recurso físico em conflito.
        conflitos: Lista de dicts com dados das reservas conflitantes.
                   Cada dict: {reserva_id, customer_name, check_in, check_out, status}.
    """

    def __init__(self, message: str, resource_name: str, conflitos: list[dict]) -> None:
        super().__init__(message)
        self.resource_name = resource_name
        self.conflitos = conflitos


class ResourceNoticeBlockedError(ValueError):
    """Lançada quando um ResourceNotice (aviso de recurso) bloqueia a reserva.

    Diferente de OverbookingError — ali há um conflito real de disponibilidade
    física; aqui é uma restrição operacional (manutenção, feriado fechado etc.)
    que um funcionário do balcão pode decidir ignorar conscientemente via
    force_notice_override. Clientes (WEB/BOT) nunca recebem esse override —
    só o formulário de balcão o expõe.

    Attributes:
        notice_text: Texto formatado do aviso que bloqueou, ex.: "Manutenção
            de piscinas toda quarta-feira, de 01/09/2026 a 31/12/2026".
    """

    def __init__(self, message: str, notice_text: str) -> None:
        super().__init__(message)
        self.notice_text = notice_text


from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import contains_eager, selectinload

from app.core.audit import AuditService
from app.extensions import db
from app.models.reservas import (
    AssetStatus,
    PhysicalAsset,
    Reserva,
    ReservaCategory,
    ReservaGroup,
    ReservaItem,
    ReservaItemLink,
    ReservaOrigin,
    ReservaResource,
    ReservaSource,
    ReservaStatus,
    ReservaStatusConfig,
    ReservaTemplate,
    SecurityDeposit,
)
from app.services.blacklist_service import BlacklistService
from app.services.base import BaseService
from app.services.reserva_pricing_service import ReservaPricingService
from app.services.resource_notice_service import ResourceNoticeService


class ReservaService(BaseService):
    """Serviço de gerenciamento de reservas (bookings)."""

    _CAMPOS_SNAPSHOT = [
        "id",
        "group_id",
        "version",
        "source_id",
        "category_id",
        "template_id",
        "customer_name",
        "customer_document",
        "origin",
        "status",
        "check_in_date",
        "check_out_date",
        "expires_at",
        "subtotal_amount",
        "discount_amount",
        "service_fee_amount",
        "total_amount",
        "desconto_percentual_manual",
    ]

    @classmethod
    def _proximo_nome_balcao(cls) -> str:
        """Gera o próximo nome sequencial para reservas de balcão sem identificação.

        Conta as reservas criadas hoje cujo nome começa com "Balcão " (exclui
        versões arquivadas, que são artefatos de edição da mesma reserva).
        A contagem reinicia automaticamente a cada dia.

        Returns:
            String no formato "Balcão N" onde N é o próximo número disponível.
        """
        hoje = date.today()
        count = (
            db.session.query(func.count(Reserva.id))
            .filter(
                func.date(Reserva.criado_em) == hoje,
                Reserva.customer_name.like("Balcão %"),
                Reserva.status != ReservaStatus.ARCHIVED_VERSION,
            )
            .scalar()
        ) or 0
        return f"Balcão {count + 1}"

    @staticmethod
    def _normalize_single_day_checkout(
        category: ReservaCategory,
        check_out_date: date | datetime | None,
    ) -> date | datetime | None:
        """Remove check-out de categorias configuradas como dia único."""
        if category.single_day_only:
            return None
        return check_out_date

    @classmethod
    def criar(
        cls,
        source_name: str,
        category_name: str,
        customer_name: str,
        customer_document: str | None,
        origin: str | ReservaOrigin,
        check_in_date: date | datetime,
        check_out_date: date | datetime | None,
        items_payload: list[dict],
        metadata_info: dict | None = None,
        template_id: int | None = None,
        manual_discount_percent: float = 0.0,
        force_overbooking: bool = False,
        force_notice_override: bool = False,
        *,
        ator_id: int | None = None,
    ) -> Reserva:
        """Cria uma nova reserva com itens associados e validação de preço server-side.

        Implementa a lógica de booking com:
        - Lookup ou criação dinâmica de fonte e categoria
        - Validação de disponibilidade de estoque
        - Server-side pricing validation via ReservaPricingService (anti-fraud)
        - Soft lock por 15 minutos (expires_at)
        - Versioning (group_id + version=1)
        - Logging de auditoria com snapshots de preço

        Server-side Pricing Validation (Double-Check):
        Antes de persistir, o método:
        1. Chama ReservaPricingService.calculate() para calcular o preço correto
        2. Compara o total calculado contra o total enviado pelo frontend
        3. Se a diferença for > 0.01, levanta ValueError (divergência de preço)
        4. Se válido, salva os snapshots de preço na Reserva e ReservaItemLink

        Args:
            source_name: Nome da fonte/venue (cria se não existir)
            category_name: Nome da categoria dentro da fonte
            customer_name: Nome completo do cliente
            customer_document: CPF ou CNPJ (opcional)
            origin: ReservaOrigin.WEB ou ReservaOrigin.BALCAO
            check_in_date: Data de check-in
            check_out_date: Data de check-out (opcional)
            items_payload: Lista de dicts com "item_id", "quantity", "price_override" (total esperado do frontend)
            metadata_info: Dicionário de metadados genéricos adicionais (opcional)
            manual_discount_percent: Percentual de desconto manual aplicado pelo operador (balcão).
                Ex: 10.0 = 10% de desconto sobre o subtotal. Salvo em
                Reserva.desconto_percentual_manual e reaplicado em todas as versões
                futuras da reserva (ao adicionar itens, mudar status, etc.).
            force_overbooking: Quando True, ignora conflitos de disponibilidade e
                permite a criação mesmo com recursos já reservados. Exclusivo para
                operadores autorizados (balcão).
            force_notice_override: Quando True, ignora ResourceNotice com
                blocks_source/blocks_items ativo (ex.: manutenção recorrente
                toda quarta-feira) e cria a reserva mesmo assim. Exclusivo para
                operadores autorizados (balcão) — nunca deve ser aceito de
                origin WEB/BOT. O aviso ignorado fica registrado em
                metadata_info["avisos_bloqueio_ignorados"] para auditoria.
            template_id: ID do ReservaTemplate que gerou esta reserva (opcional).
                Quando fornecido, o `expires_at` é calculado como
                `criado_em + timedelta(hours=template.timeout_hours)`.
                Se o template não tiver timeout_hours configurado, expires_at = None.
                Quando None, aplica soft-lock padrão de 15 minutos (comportamento legado).
            ator_id: ID do usuário fazendo a ação (para auditoria)

        Returns:
            Instância de Reserva persistida no banco de dados com snapshots de preço.

        Raises:
            ValueError: Se validação falhar (campos obrigatórios, datas, itens, ou divergência de preço)
            OverbookingError: Se um recurso físico já está ocupado no período e force_overbooking=False.
            ResourceNoticeBlockedError: Se um aviso de recurso bloqueia o período e
                force_notice_override=False.
        """
        # 1. Validações básicas
        cls._require(source_name, "source_name")
        cls._require(category_name, "category_name")
        cls._require(customer_name, "customer_name")

        # 2. Validar origin
        if isinstance(origin, str):
            try:
                origin_enum = ReservaOrigin[origin.upper()]
            except KeyError:
                raise ValueError(f"origin deve ser 'WEB', 'BALCAO' ou 'BOT', recebido: {origin}")
        else:
            origin_enum = origin

        # 3. Normalizar date → datetime (necessário para coluna DateTime e pró-rata horário)
        if isinstance(check_in_date, date) and not isinstance(check_in_date, datetime):
            check_in_date = datetime(check_in_date.year, check_in_date.month, check_in_date.day, 0, 0, 0)
        if isinstance(check_out_date, date) and not isinstance(check_out_date, datetime):
            check_out_date = datetime(check_out_date.year, check_out_date.month, check_out_date.day, 0, 0, 0)

        # 3b. Validar datas
        if check_out_date and check_out_date < check_in_date:
            raise ValueError(
                "check_out_date deve ser igual ou posterior a check_in_date."
            )

        # 4. Validar itens_payload não está vazio
        if not items_payload:
            raise ValueError("A reserva deve conter ao menos um item.")

        # 5. Source lookup-or-create
        # Comparação case-insensitive (func.lower) — o MySQL casava sem distinção
        # de caixa por collation; o Postgres é case-sensitive no operador "=".
        # Sem isso, um source_name com caixa diferente cria fonte duplicada.
        source = db.session.query(ReservaSource).filter(
            func.lower(ReservaSource.name) == source_name.lower(),
            ReservaSource.deleted_at.is_(None),
        ).first()

        if not source:
            try:
                source = ReservaSource(name=source_name)
                db.session.add(source)
                db.session.flush()  # triggers RBAC event listener
            except IntegrityError:
                db.session.rollback()
                source = db.session.query(ReservaSource).filter(
                    func.lower(ReservaSource.name) == source_name.lower()
                ).first()
                if not source:
                    raise ValueError(f"Falha ao criar/recuperar fonte '{source_name}'.")

        metadata_lookup = metadata_info or {}
        BlacklistService.assert_client_allowed(
            name=customer_name,
            cpf=customer_document,
            phone=(
                metadata_lookup.get("telefone")
                or metadata_lookup.get("phone")
                or metadata_lookup.get("customer_phone")
            ),
            email=(
                metadata_lookup.get("email")
                or metadata_lookup.get("customer_email")
            ),
            location_id=source.id,
        )

        # 6. Category lookup-or-create within source
        # Case-insensitive (func.lower) — ver nota no source lookup acima. Sem isso,
        # "Day Use" não casa com "Day use" no Postgres e cria categoria duplicada
        # sem regras de desconto, causando divergência de preço na validação anti-fraude.
        category = db.session.query(ReservaCategory).filter(
            ReservaCategory.source_id == source.id,
            func.lower(ReservaCategory.name) == category_name.lower(),
            ReservaCategory.deleted_at.is_(None),
        ).first()

        if not category:
            try:
                category = ReservaCategory(source_id=source.id, name=category_name)
                db.session.add(category)
                db.session.flush()
            except IntegrityError:
                db.session.rollback()
                category = db.session.query(ReservaCategory).filter(
                    ReservaCategory.source_id == source.id,
                    func.lower(ReservaCategory.name) == category_name.lower(),
                ).first()
                if not category:
                    raise ValueError(
                        f"Falha ao criar/recuperar categoria '{category_name}'."
                    )

        check_out_date = cls._normalize_single_day_checkout(category, check_out_date)

        # 6b. Horário limite para reserva no mesmo dia (WEB/BOT — balcão sempre pode
        # avaliar a situação real no local e faz override manual via force_overbooking).
        if (
            category.same_day_cutoff_time is not None
            and origin_enum in (ReservaOrigin.WEB, ReservaOrigin.BOT)
        ):
            from app.services.timezone_service import TimezoneService

            agora_local = datetime.now(TimezoneService.system_timezone())
            if (
                check_in_date.date() == agora_local.date()
                and agora_local.time() > category.same_day_cutoff_time
            ):
                raise ValueError(
                    f"Reservas de '{category.name}' para hoje só são aceitas até as "
                    f"{category.same_day_cutoff_time.strftime('%H:%M')}. Entre em contato "
                    "diretamente com o local para verificar disponibilidade de última hora."
                )

        # 7. Process items and validate stock
        validated_items = []
        seen_resource_ids: set[int] = set()
        frontend_total_price = Decimal("0.00")
        # Acumulador de consumo por recurso neste payload (previne overbooking
        # quando múltiplos itens consomem o mesmo recurso físico no mesmo pedido)
        resource_consumed_in_payload: dict[int, int] = {}

        for entry in items_payload:
            item_id = entry.get("item_id")
            quantity = entry.get("quantity", 1)
            price_override = entry.get("price_override")
            resource_id_raw = entry.get("resource_id")

            # Fetch item
            item = db.session.query(ReservaItem).filter(
                ReservaItem.id == item_id,
                ReservaItem.deleted_at.is_(None),
            ).first()

            if not item:
                raise ValueError(f"Item com ID {item_id} não encontrado.")

            # price_override é obrigatório; 0 é válido para itens gratuitos
            if price_override is None:
                raise ValueError(
                    f"Item {item_id}: campo 'price_override' é obrigatório."
                )

            # Resolver resource_id (opcional — itens sem recurso físico são permitidos)
            resource = None
            resource_id: int | None = None
            if resource_id_raw is not None:
                try:
                    resource_id = int(resource_id_raw)
                except (ValueError, TypeError):
                    raise ValueError(f"Item {item_id}: resource_id deve ser inteiro.")

                resource = db.session.query(ReservaResource).filter(
                    ReservaResource.id == resource_id,
                    ReservaResource.deleted_at.is_(None),
                ).first()

                if not resource:
                    raise ValueError(
                        f"Item {item_id}: recurso ID {resource_id} não encontrado."
                    )
                if resource.source_id != source.id:
                    raise ValueError(
                        f"Item {item_id}: recurso '{resource.name}' não pertence ao venue selecionado."
                    )
                if item.group_id is None or resource.group_id != item.group_id:
                    raise ValueError(
                        f"Item {item_id}: recurso '{resource.name}' não pertence ao mesmo agrupamento do item."
                    )

                # Prevenir dupla reserva do mesmo ativo na mesma requisição
                if resource_id in seen_resource_ids:
                    raise ValueError(
                        f"Recurso '{resource.name}' aparece mais de uma vez no payload."
                    )
                seen_resource_ids.add(resource_id)

                # Validar estoque do recurso (inclui acumulação intra-payload)
                if resource.stock_quantity is not None:
                    extra = resource_consumed_in_payload.get(resource_id, 0)
                    cls._verificar_estoque(
                        resource, check_in_date, check_out_date, quantity,
                        extra_consumed=extra,
                        force_overbooking=force_overbooking,
                    )
                    resource_consumed_in_payload[resource_id] = extra + quantity

            if item.requires_resource and item.group_id is None:
                raise ValueError(
                    f"Item {item_id}: marque um grupo para exigir vínculo a recurso físico."
                )

            # Smart assignment: atribui automaticamente um recurso disponível
            # quando o item pertence a um grupo e nenhum recurso foi especificado.
            # Ocorre quando o cliente escolhe "qualquer churrasqueira" no wizard.
            if resource_id is None and item.requires_resource and item.group_id is not None:
                auto_resource = cls._auto_selecionar_recurso(
                    source_id=source.id,
                    group_id=item.group_id,
                    check_in=check_in_date,
                    check_out=check_out_date,
                    quantidade=quantity,
                    excluir_ids=seen_resource_ids,
                    resource_consumed_in_payload=resource_consumed_in_payload,
                )
                if auto_resource is not None:
                    resource_id = auto_resource.id
                    seen_resource_ids.add(resource_id)
                    extra = resource_consumed_in_payload.get(resource_id, 0)
                    resource_consumed_in_payload[resource_id] = extra + quantity
                elif cls._pool_compartilhado_ja_atribuido(
                    source.id, item.group_id, seen_resource_ids
                ):
                    # Pool de capacidade compartilhado, já vinculado por outro
                    # item desta mesma reserva. Segue com resource_id=None em vez
                    # de recusar.
                    #
                    # É o caso de "Capacidade Day Use", o recurso que Inteira e
                    # Meia-Entrada dividem por projeto (modelo de 3 camadas, ver
                    # CLAUDE.md). O grupo tem um único recurso: o primeiro item o
                    # consome e o segundo não encontrava nada, derrubando a
                    # reserva mais comum que existe — dois adultos e uma criança
                    # na mesma compra.
                    #
                    # Vincular o mesmo recurso duas vezes não é alternativa:
                    # uq_reserva_resource_link proíbe (reserva_id, resource_id)
                    # repetido. NULL é o que as reservas antigas já gravam — 48 e
                    # 49 têm a Inteira com recurso e a Meia sem — e o constraint
                    # aceita vários NULLs.
                    #
                    # Estoque finito não passa por aqui: duas churrasqueiras
                    # diferentes continuam exigindo recursos distintos, e a falta
                    # de um segue sendo overbooking de verdade.
                    resource_id = None
                else:
                    raise OverbookingError(
                        "Nenhum recurso físico disponível para este item no período.",
                        resource_name=item.name,
                        conflitos=[],
                    )

            price_override_decimal = Decimal(str(price_override))
            frontend_total_price += price_override_decimal

            validated_items.append({
                "item": item,
                "resource_id": resource_id,
                "quantity": quantity,
                "price_override": price_override_decimal,
            })

        # 7b. Validar avisos de bloqueio de recurso (ResourceNotice)
        _notice_end = (check_out_date or check_in_date).date()
        _avisos_ignorados: list[dict] = []
        for _notice in ResourceNoticeService.buscar_avisos_periodo(
            check_in_date.date(), _notice_end, source.id
        ):
            _notice_texto = ResourceNoticeService.formatar_texto(_notice)
            if _notice.blocks_source:
                if not force_notice_override:
                    raise ResourceNoticeBlockedError(
                        f"Reservas bloqueadas para este local no período selecionado "
                        f"({_notice_texto}).",
                        notice_text=_notice_texto,
                    )
                _avisos_ignorados.append({
                    "id": _notice.id, "texto": _notice_texto, "tipo": "blocks_source",
                })
            if _notice.blocks_items:
                for entry in validated_items:
                    if (
                        _notice.blocked_item_ids is None
                        or entry["item"].id in _notice.blocked_item_ids
                    ):
                        if not force_notice_override:
                            raise ResourceNoticeBlockedError(
                                f"Item '{entry['item'].name}' está bloqueado no período "
                                f"selecionado ({_notice_texto}).",
                                notice_text=_notice_texto,
                            )
                        _avisos_ignorados.append({
                            "id": _notice.id, "texto": _notice_texto, "tipo": "blocks_items",
                            "item": entry["item"].name,
                        })

        if _avisos_ignorados:
            metadata_info = dict(metadata_info or {})
            metadata_info["avisos_bloqueio_ignorados"] = _avisos_ignorados

        # 8. Server-side pricing validation (Anti-Fraud)
        # Call ReservaPricingService to calculate the correct total
        pricing_result = ReservaPricingService.calculate(
            source_id=source.id,
            check_in_date=check_in_date,
            check_out_date=check_out_date or check_in_date,
            items=[{"item_id": entry["item"].id, "quantity": entry["quantity"]}
                   for entry in validated_items],
            category_id=category.id,
            manual_discount_percent=manual_discount_percent,
        )

        server_total = Decimal(str(pricing_result["total"]))

        # Compare frontend total against server-calculated total
        # Allow tolerance of 0.01 for floating-point rounding
        price_difference = abs(server_total - frontend_total_price)
        if price_difference > Decimal("0.01"):
            raise ValueError(
                f"Divergência de preço: O valor calculado pelo servidor ({float(server_total):.2f}) "
                f"difere do valor enviado ({float(frontend_total_price):.2f}). "
                f"Diferença: {float(price_difference):.2f}."
            )

        # 9. Resolver template e calcular expires_at
        now = datetime.now(timezone.utc)
        template: ReservaTemplate | None = None

        if template_id is not None:
            template = db.session.get(ReservaTemplate, template_id)
            if not template or template.deleted_at is not None:
                raise ValueError(
                    f"Template com ID {template_id} não encontrado ou removido."
                )
            if not template.is_active:
                raise ValueError(
                    f"Template '{template.name}' está desativado e não aceita novas reservas."
                )
            # timeout_hours = None → reserva não expira automaticamente
            expires_at = (
                now + timedelta(hours=template.timeout_hours)
                if template.timeout_hours is not None
                else None
            )
        else:
            # Comportamento legado: soft-lock de 15 minutos sem template
            expires_at = now + timedelta(minutes=15)

        group_id = str(uuid.uuid4())

        # Normalizar percentual para Decimal (None quando zero — sem desconto manual)
        desconto_pct_manual = (
            Decimal(str(round(manual_discount_percent, 2)))
            if manual_discount_percent and manual_discount_percent > 0
            else None
        )

        reserva = Reserva(
            group_id=group_id,
            version=1,
            source_id=source.id,
            category_id=category.id,
            template_id=template_id,
            customer_name=customer_name,
            customer_document=customer_document,
            origin=origin_enum,
            status=ReservaStatus.PENDING,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            expires_at=expires_at,
            metadata_info=metadata_info,
            # Store pricing snapshots from server calculation
            subtotal_amount=Decimal(str(pricing_result["subtotal"])),
            discount_amount=Decimal(str(pricing_result["discount_amount"])),
            service_fee_amount=Decimal(str(pricing_result["service_fee"])),
            total_amount=server_total,
            desconto_percentual_manual=desconto_pct_manual,
            criado_por_id=ator_id,
        )
        db.session.add(reserva)
        db.session.flush()  # get PK

        # 10. Create ReservaItemLink rows with pricing snapshots
        # Map item_id to its breakdown from pricing service
        breakdown_map = {item["item_id"]: item for item in pricing_result["items_breakdown"]}

        for entry in validated_items:
            item_id = entry["item"].id
            breakdown = breakdown_map.get(item_id, {})

            link = ReservaItemLink(
                reserva_id=reserva.id,
                item_id=item_id,
                resource_id=entry["resource_id"],
                quantity=entry["quantity"],
                locked_price=entry["price_override"],
                # Store pricing breakdown from server calculation.
                # unit_price pode ser None em PER_DAY (varia por dia — ver
                # daily_breakdown no pricing service). Normalizamos para 0
                # antes de cast pra Decimal para não crashar.
                unit_price=Decimal(str(breakdown.get("unit_price") or 0)),
                item_total=Decimal(str(breakdown.get("item_total") or 0)),
            )
            db.session.add(link)

        db.session.flush()

        # Detectar overbooking_risco: verificar se este PENDING conflita com
        # reservas já pagas (bloqueantes). PENDING não bloqueia o slot do outro,
        # mas ao ser confirmado causaria overbooking — sinalizamos aqui para alerta visual.
        risco_detectado = False
        for entry in validated_items:
            rid = entry.get("resource_id")
            if rid is None:
                continue
            rec = db.session.get(ReservaResource, rid)
            if rec is None or rec.stock_quantity is None:
                continue
            try:
                cls._verificar_estoque(
                    rec, check_in_date, check_out_date,
                    entry["quantity"],
                    exclude_reserva_id=reserva.id,
                )
            except OverbookingError:
                risco_detectado = True
                break
        if risco_detectado:
            reserva.overbooking_risco = True

        # 10. Log to audit trail
        AuditService.log_create(
            "reservas",
            entity_id=reserva.id,
            new_state=cls._snapshot(reserva, cls._CAMPOS_SNAPSHOT),
            actor_id=ator_id,
        )

        db.session.commit()
        return reserva

    @classmethod
    def listar(
        cls,
        *,
        source_id: int | None = None,
        categoria_id: int | None = None,
        status: str | None = None,
        q: str | None = None,
        check_in_de: date | None = None,
        check_in_ate: date | None = None,
        item_nome: str | None = None,
        page: int = 1,
        per_page: int = 20,
        sort_by: str = "criado_em",
        sort_dir: str = "desc",
        actor=None,
    ) -> tuple[list[Reserva], int]:
        """Lista reservas ativas com filtros, ordenação e paginação.

        Args:
            source_id: Filtro por ID da fonte (opcional)
            categoria_id: Filtro por ID de categoria (opcional)
            status: Filtro por status (PENDING, PAID, CANCELED, COMPLETED)
            q: Busca livre por nome, CPF ou telefone do cliente
            check_in_de: Filtra reservas com check_in_date >= esta data (opcional)
            check_in_ate: Filtra reservas com check_in_date <= esta data (opcional)
            item_nome: Filtra reservas que possuem ao menos um item/tarifa ativo
                cujo nome contém este texto (case-insensitive, opcional)
            page: Número da página (começa em 1)
            per_page: Quantidade de itens por página
            sort_by: Campo de ordenação (check_in_date, customer_name, status, total_amount, criado_em, visualizada_em)
            sort_dir: Direção (asc | desc)
            actor: Contexto de autorização do operador. Quando informado,
                aplica o escopo de venues antes do count/paginação.

        Returns:
            Tupla (lista de Reserva, total de registros)

        Raises:
            ValueError: Se status inválido
        """
        # Base filter: deleted_at IS NULL AND status != ARCHIVED_VERSION
        query = db.session.query(Reserva).filter(
            Reserva.deleted_at.is_(None),
            Reserva.status != ReservaStatus.ARCHIVED_VERSION,
        )
        if actor is not None:
            from app.core.access_policy import ReservaAccessPolicy

            query = ReservaAccessPolicy.filter_query(query, actor)

        # Apply optional filters
        if source_id:
            query = query.filter(Reserva.source_id == source_id)

        if categoria_id:
            query = query.filter(Reserva.category_id == categoria_id)

        if status:
            # Validate status is a valid enum value
            try:
                status_enum = ReservaStatus[status.upper()]
                query = query.filter(Reserva.status == status_enum)
            except KeyError:
                raise ValueError(f"Status inválido: {status}")

        if q:
            q_filter = f"%{q}%"
            query = query.filter(
                or_(
                    Reserva.customer_name.ilike(q_filter),
                    Reserva.customer_document.ilike(q_filter),
                    Reserva.metadata_info["telefone"].as_string().ilike(q_filter),
                )
            )

        if check_in_de:
            query = query.filter(Reserva.check_in_date >= check_in_de)

        if check_in_ate:
            query = query.filter(Reserva.check_in_date <= check_in_ate)

        if item_nome:
            item_subq = (
                db.session.query(ReservaItemLink.reserva_id)
                .join(ReservaItem, ReservaItemLink.item_id == ReservaItem.id)
                .filter(
                    ReservaItemLink.deleted_at.is_(None),
                    ReservaItem.name.ilike(f"%{item_nome}%"),
                )
            )
            query = query.filter(Reserva.id.in_(item_subq))

        # Count total
        total = query.count()

        # Apply ordering and pagination
        _sort_map = {
            "check_in_date": Reserva.check_in_date,
            "customer_name": Reserva.customer_name,
            "status": Reserva.status,
            "total_amount": Reserva.total_amount,
            "criado_em": Reserva.criado_em,
            "visualizada_em": Reserva.visualizada_em,
        }
        sort_col = _sort_map.get(sort_by, Reserva.criado_em)
        order_expr = sort_col.asc() if sort_dir == "asc" else sort_col.desc()

        reservas = (
            query.order_by(order_expr)
            .limit(per_page)
            .offset((page - 1) * per_page)
            .all()
        )

        return reservas, total

    @classmethod
    def marcar_visualizada(cls, reserva_id: int) -> None:
        """Registra a primeira abertura de uma reserva pelo operador.

        Só atualiza se visualizada_em ainda for NULL (idempotente).

        Args:
            reserva_id: ID da reserva a marcar como visualizada.
        """
        from datetime import timezone
        reserva = db.session.get(Reserva, reserva_id)
        if reserva and reserva.visualizada_em is None:
            reserva.visualizada_em = datetime.now(timezone.utc)
            db.session.commit()

    @classmethod
    def salvar_notas(cls, reserva_id: int, notas: str, ator_id: int | None = None) -> None:
        """Salva ou atualiza as anotações do operador em uma reserva.

        Não cria nova versão — atualiza a coluna diretamente.

        Args:
            reserva_id: ID da reserva.
            notas: Texto das anotações (pode ser vazio para apagar).
            ator_id: ID do operador (para auditoria).

        Raises:
            ValueError: Se reserva não encontrada.
        """
        reserva = db.session.get(Reserva, reserva_id)
        if not reserva or reserva.deleted_at is not None:
            raise ValueError("Reserva não encontrada.")
        reserva.notas_operador = notas.strip() or None
        AuditService.log_update(
            "reservas",
            entity_id=reserva_id,
            previous_state={"notas_operador": None},
            new_state={"notas_operador": reserva.notas_operador},
            actor_id=ator_id,
        )
        db.session.commit()

    @classmethod
    def salvar_nota_relatorio(cls, reserva_id: int, nota: str, ator_id: int | None = None) -> None:
        """Salva ou atualiza a nota de relatório de uma reserva.

        Não cria nova versão — atualiza a coluna diretamente.

        Args:
            reserva_id: ID da reserva.
            nota: Texto da nota (pode ser vazio para apagar).
            ator_id: ID do operador (para auditoria).

        Raises:
            ValueError: Se reserva não encontrada.
        """
        reserva = db.session.get(Reserva, reserva_id)
        if not reserva or reserva.deleted_at is not None:
            raise ValueError("Reserva não encontrada.")
        reserva.nota_relatorio = nota.strip() or None
        AuditService.log_update(
            "reservas",
            entity_id=reserva_id,
            previous_state={"nota_relatorio": None},
            new_state={"nota_relatorio": reserva.nota_relatorio},
            actor_id=ator_id,
        )
        db.session.commit()

    @classmethod
    def detalhe(cls, reserva_id: int) -> Reserva:
        """Retorna os detalhes completos de uma reserva específica.

        Args:
            reserva_id: ID da reserva a recuperar

        Returns:
            Instância de Reserva com itens carregados

        Raises:
            ValueError: Se reserva não encontrada ou foi deletada
        """
        reserva = db.session.get(Reserva, reserva_id)

        if not reserva or reserva.deleted_at is not None:
            raise ValueError("Reserva não encontrada.")

        return reserva

    @classmethod
    def _precificacao_preservavel(
        cls,
        reserva_anterior: Reserva,
        *,
        items_payload: list[dict],
        check_in,
        check_out,
        pct_solicitado: float,
    ) -> bool:
        """Diz se a nova versão pode herdar o preço da anterior sem recalcular.

        Uma reserva é um acordo: o valor combinado com o cliente não pode mudar
        porque alguém trocou o status, corrigiu o nome ou anexou uma observação.
        Só há motivo para reprecificar quando muda algo que o preço depende —
        os itens, as quantidades, as datas ou o desconto pedido.

        Isso importa porque `ReservaPricingService.calculate()` avalia as regras
        **no momento da edição**: preço de tabela do catálogo, taxa de serviço e
        descontos automáticos de antecipação, cujo `days_in_advance` conta a
        partir de hoje. Editar uma reserva antiga fora da janela de antecipação
        fazia o desconto acordado simplesmente sumir; uma mudança de preço no
        catálogo entrava retroativamente numa reserva já paga.

        Args:
            reserva_anterior: Versão vigente, cujo preço está em vigor.
            items_payload: Itens enviados pelo chamador.
            check_in: Data de entrada efetiva da nova versão.
            check_out: Data de saída efetiva da nova versão.
            pct_solicitado: Percentual de desconto manual pedido.

        Returns:
            True quando nada que afeta o preço mudou.
        """
        if check_in != reserva_anterior.check_in_date:
            return False
        anterior_check_out = reserva_anterior.check_out_date or reserva_anterior.check_in_date
        if check_out != anterior_check_out:
            return False

        # Composição: mesmos itens, mesmas quantidades. `resource_id` de fora —
        # trocar a churrasqueira B7 pela B8 não muda o que se cobra.
        composicao_anterior = {
            link.item_id: link.quantity
            for link in reserva_anterior.items
            if link.deleted_at is None and link.item_id is not None
        }
        composicao_nova: dict[int, int] = {}
        for entrada in items_payload:
            item_id = entrada.get("item_id")
            if item_id is None:
                return False
            composicao_nova[int(item_id)] = int(entrada.get("quantity", 1))
        if composicao_anterior != composicao_nova:
            return False

        # Desconto: "não mexer" pode chegar como o percentual gravado ou como o
        # resíduo derivado (o que a rota de edição envia). Qualquer um dos dois
        # significa manter o desconto atual.
        pct_gravado = float(reserva_anterior.desconto_percentual_manual or 0)
        pct_efetivo = cls.desconto_efetivo_pct(reserva_anterior)
        return (
            abs(pct_solicitado - pct_gravado) < 0.01
            or abs(pct_solicitado - pct_efetivo) < 0.01
        )

    @classmethod
    def _desconto_automatico_valor(cls, reserva: Reserva) -> float | None:
        """Quanto de desconto o motor de preços reproduz sozinho para esta reserva.

        Recalcula o preço dos mesmos itens e datas com `manual_discount_percent=0`,
        isolando o que vem das regras automáticas (antecipação, calendário,
        `flat_discount_percent`). Cálculo puro — não grava nada.

        Args:
            reserva: Versão a reprecificar.

        Returns:
            Valor do desconto automático, ou None se não foi possível recalcular
            (item removido do catálogo, fonte ausente, etc).
        """
        itens = [
            {"item_id": link.item_id, "quantity": link.quantity}
            for link in reserva.items
            if link.deleted_at is None and link.item_id is not None
        ]
        if not itens or not reserva.source_id or not reserva.check_in_date:
            return None
        try:
            preco = ReservaPricingService.calculate(
                source_id=reserva.source_id,
                check_in_date=reserva.check_in_date,
                check_out_date=reserva.check_out_date or reserva.check_in_date,
                items=itens,
                category_id=reserva.category_id,
                manual_discount_percent=0.0,
            )
            return float(preco["discount_amount"])
        except Exception:
            logger.warning(
                "Não foi possível reprecificar a reserva %s para isolar o desconto "
                "automático; caindo na razão bruta discount/subtotal.",
                reserva.id, exc_info=True,
            )
            return None

    @classmethod
    def desconto_efetivo_pct(cls, reserva: Reserva) -> float:
        """Percentual de desconto **manual** a repassar para a próxima versão.

        Só interessa a parte que o motor de preços NÃO recria sozinho. Uma nova
        versão sempre reprecifica do zero, e as regras automáticas (antecipação,
        calendário, desconto plano) voltam a incidir por conta própria — somar o
        desconto delas como se fosse manual aplica o mesmo abatimento duas vezes.

        Ordem de resolução:
            1. `desconto_percentual_manual` gravado → é a verdade, devolve direto.
            2. Sem ele, devolve o **resíduo**: o desconto registrado menos o que
               as regras automáticas reproduzem. Isso preserva desconto embutido
               no `price_override` da criação (ex.: cupom do plugin WordPress),
               que nunca virou percentual, sem duplicar o que é automático.
            3. Se a reprecificação falhar, cai na razão bruta — o comportamento
               antigo, que ao menos não perde o desconto.

        Era exatamente o passo 2 que faltava: uma reserva com 40% de "Reserva
        Antecipada" virava 80% ao mudar de status, porque os 40% automáticos
        eram lidos como manuais e reaplicados sobre o desconto que o motor já
        tinha recalculado.

        Args:
            reserva: Instância de Reserva a inspecionar.

        Returns:
            Percentual de 0 a 100, arredondado a 2 casas. 0.0 se todo o desconto
            registrado for reproduzível pelas regras automáticas.
        """
        if reserva.desconto_percentual_manual is not None:
            return float(reserva.desconto_percentual_manual)

        if not (reserva.subtotal_amount and reserva.discount_amount and reserva.subtotal_amount > 0):
            return 0.0

        subtotal = float(reserva.subtotal_amount)
        registrado = float(reserva.discount_amount)

        automatico = cls._desconto_automatico_valor(reserva)
        if automatico is None:
            return round(registrado / subtotal * 100, 2)

        residual = registrado - automatico
        # Tolerância de um centavo: arredondamentos do motor não devem virar
        # um percentual manual fantasma de 0,01%.
        if residual <= 0.01:
            return 0.0
        return round(residual / subtotal * 100, 2)

    @classmethod
    def historico_versoes(cls, reserva_id: int) -> list[Reserva]:
        """Retorna todas as versões de uma reserva ordenadas da mais antiga para a mais recente.

        Busca o group_id da reserva indicada e recupera todas as linhas que
        compartilham esse group_id, incluindo versões arquivadas e canceladas.
        Versões soft-deletadas (deleted_at IS NOT NULL) também são incluídas
        para garantir rastreabilidade completa do histórico.

        Args:
            reserva_id: ID de qualquer versão da reserva (atual ou arquivada).

        Returns:
            Lista de Reserva ordenada por version ASC (v1 primeiro, vN atual por último).
            Itens (ReservaItemLink) são carregados via selectinload.

        Raises:
            ValueError: Se nenhuma reserva for encontrada com o ID informado.
        """
        # Busca a reserva alvo — aceita qualquer status (inclusive ARCHIVED_VERSION)
        reserva_ref = db.session.get(Reserva, reserva_id)
        if not reserva_ref:
            raise ValueError("Reserva não encontrada.")

        versoes = (
            db.session.query(Reserva)
            .filter(Reserva.group_id == reserva_ref.group_id)
            .options(selectinload(Reserva.items).selectinload(ReservaItemLink.item))
            .order_by(Reserva.version.asc())
            .all()
        )
        return versoes

    @classmethod
    def atualizar(
        cls,
        reserva_id: int,
        *,
        items_payload: list[dict],
        metadata_info: dict | None = None,
        customer_name: str | None = None,
        customer_document: str | None = None,
        check_in_date: date | None = None,
        check_out_date: date | None = None,
        status: str | None = None,
        manual_discount_percent: float | None = None,
        forcar_reprecificacao: bool = False,
        ator_id: int | None = None,
    ) -> Reserva:
        """Atualiza uma reserva criando nova versão imutável (group_id + version+1).

        Implementa o padrão de versionamento: a versão anterior é arquivada com
        status=ARCHIVED_VERSION e uma nova linha é inserida com version incrementado,
        preservando o group_id original para rastreabilidade histórica.

        Se o total recalculado for diferente do anterior e o status era PAID,
        o sistema automaticamente rebaixa para PENDING (cliente deve diferença).

        **A edição não reprecifica o que não mudou.** Trocar o status, corrigir o
        nome do cliente ou anexar uma observação preservam o valor acordado: só
        há recálculo quando mudam itens, quantidades, datas ou o desconto pedido.
        Sem isso, editar uma reserva antiga a submetia às regras vigentes hoje —
        preço de catálogo, taxa de serviço e janela de desconto antecipado.

        O desconto percentual manual é herdado automaticamente da versão anterior
        quando não fornecido explicitamente. Isso garante que:
          - Mudanças de status preservem o desconto original (Ex. 1: PAID→EM_USO)
          - Adição de itens aplique o mesmo percentual ao novo subtotal (Ex. 2)

        Args:
            reserva_id: ID da versão atual da reserva a ser editada.
            items_payload: Lista de dicts com 'item_id', 'quantity', 'price_override'.
            metadata_info: Metadados adicionais (opcional; sobrescreve o anterior se fornecido).
            customer_name: Novo nome do cliente (opcional; copia anterior se omitido).
            customer_document: Novo CPF/CNPJ do cliente (opcional).
            check_in_date: Nova data de check-in (opcional).
            check_out_date: Nova data de check-out (opcional).
            status: Novo status (PENDING, PAID, COMPLETED, CANCELED) — opcional. Se omitido, herda anterior.
            manual_discount_percent: Percentual de desconto manual (0–100). None = herda da versão
                anterior. 0.0 = remove o desconto manual explicitamente.
            forcar_reprecificacao: Recalcula o preço mesmo que itens, datas e desconto
                estejam inalterados. Use apenas quando reprecificar for o objetivo
                explícito — por padrão a edição preserva o valor acordado.
            ator_id: ID do usuário executando a ação (para auditoria).

        Returns:
            Nova instância de Reserva persistida (versão N+1).

        Raises:
            ValueError: Se reserva não encontrada, deletada ou já arquivada.
        """
        reserva_anterior = db.session.get(Reserva, reserva_id)

        if not reserva_anterior or reserva_anterior.deleted_at is not None:
            raise ValueError("Reserva não encontrada.")

        if reserva_anterior.status == ReservaStatus.ARCHIVED_VERSION:
            raise ValueError("Não é possível editar uma versão arquivada.")

        snapshot_anterior = cls._snapshot(reserva_anterior, cls._CAMPOS_SNAPSHOT)

        # Resolver desconto percentual manual efetivo:
        # - None explícito → herdar da versão anterior (comportamento padrão)
        # - 0.0 explícito → remover desconto manual
        # - > 0 explícito → usar novo valor fornecido
        if manual_discount_percent is None:
            efetivo_desconto_pct = (
                float(reserva_anterior.desconto_percentual_manual)
                if reserva_anterior.desconto_percentual_manual is not None
                else 0.0
            )
        else:
            efetivo_desconto_pct = max(0.0, float(manual_discount_percent))

        efetivo_desconto_pct_decimal = (
            Decimal(str(round(efetivo_desconto_pct, 2))) if efetivo_desconto_pct > 0 else None
        )

        # Resolver datas efetivas (override ou herdar do anterior)
        efetivo_check_in = check_in_date if check_in_date is not None else reserva_anterior.check_in_date
        efetivo_check_out = check_out_date if check_out_date is not None else (reserva_anterior.check_out_date or reserva_anterior.check_in_date)

        # O preço acordado só muda quando muda algo de que ele depende.
        preservar_preco = (
            bool(items_payload)
            and not forcar_reprecificacao
            and cls._precificacao_preservavel(
                reserva_anterior,
                items_payload=items_payload,
                check_in=efetivo_check_in,
                check_out=efetivo_check_out,
                pct_solicitado=efetivo_desconto_pct,
            )
        )

        # Recalcular preço server-side
        if items_payload:
            item_ids = [it["item_id"] for it in items_payload]
            first_item = db.session.get(ReservaItem, item_ids[0])
            source_id = first_item.category.source_id if first_item else reserva_anterior.source_id
            seen_resource_ids: set[int] = set()
            resource_consumed_in_payload: dict[int, int] = {}
            for entry in items_payload:
                item = db.session.get(ReservaItem, entry["item_id"])
                if not item or item.deleted_at is not None:
                    raise ValueError(f"Item {entry['item_id']} não encontrado.")
                resource_id = entry.get("resource_id")
                if item.requires_resource and not resource_id:
                    raise ValueError(
                        f"Item {item.id}: selecione um recurso físico antes de confirmar."
                    )
                if not resource_id:
                    continue
                resource = db.session.get(ReservaResource, int(resource_id))
                if not resource or resource.deleted_at is not None:
                    raise ValueError(f"Item {item.id}: recurso ID {resource_id} não encontrado.")
                if resource.source_id != source_id:
                    raise ValueError(
                        f"Item {item.id}: recurso '{resource.name}' não pertence ao venue selecionado."
                    )
                if item.group_id is None or resource.group_id != item.group_id:
                    raise ValueError(
                        f"Item {item.id}: recurso '{resource.name}' não pertence ao mesmo agrupamento do item."
                    )
                if resource.id in seen_resource_ids:
                    raise ValueError(
                        f"Recurso '{resource.name}' aparece mais de uma vez no payload."
                    )
                seen_resource_ids.add(resource.id)
                if resource.stock_quantity is not None:
                    extra = resource_consumed_in_payload.get(resource.id, 0)
                    cls._verificar_estoque(
                        resource,
                        efetivo_check_in,
                        efetivo_check_out,
                        int(entry.get("quantity", 1)),
                        extra_consumed=extra,
                        exclude_reserva_id=reserva_anterior.id,
                    )
                    resource_consumed_in_payload[resource.id] = extra + int(entry.get("quantity", 1))

            if preservar_preco:
                # Nada que afeta o preço mudou: a nova versão herda os valores da
                # anterior. `pricing = None` faz o restante do método copiar
                # subtotal, desconto, taxa e total — e não há o que validar
                # contra o payload, já que o preço enviado é simplesmente ignorado.
                pricing = None
                logger.info(
                    "Edição sem reprecificação | reserva_id=%d group_id=%s | "
                    "itens, datas e desconto inalterados; total preservado em R$ %s",
                    reserva_anterior.id, reserva_anterior.group_id,
                    reserva_anterior.total_amount,
                )
            else:
                pricing = ReservaPricingService.calculate(
                    source_id=source_id,
                    check_in_date=efetivo_check_in,
                    check_out_date=efetivo_check_out,
                    items=items_payload,
                    category_id=reserva_anterior.category_id,
                    manual_discount_percent=efetivo_desconto_pct,
                )
                frontend_total = sum(
                    float(it.get("price_override", 0)) for it in items_payload
                )
                # Valida contra o subtotal (soma dos itens sem desconto), não contra o
                # total final. O desconto antecipado é aplicado server-side — o editor
                # envia preços de tabela por item; o servidor aplica as regras de desconto.
                server_subtotal = pricing["subtotal"]
                if abs(frontend_total - server_subtotal) > 0.01:
                    raise ValueError(
                        f"Divergência de preço: subtotal calculado R$ {server_subtotal:.2f}, "
                        f"enviado R$ {frontend_total:.2f}."
                    )
        else:
            pricing = None

        # ── Re-validação de disponibilidade para transições manuais de status ──────
        # Quando um operador muda explicitamente o status para um valor bloqueante
        # (ex: PENDING → PAID), o sistema deve re-verificar se o recurso físico
        # ainda está disponível. Isso cobre o cenário crítico de overbooking:
        #   1. Reserva expira (expires_at vencido) → slot liberado automaticamente
        #   2. Outro cliente reserva o mesmo slot → aprovado com sucesso
        #   3. Operador tenta aprovar a reserva expirada → BLOQUEADO aqui
        # A checagem ocorre ANTES do arquivamento para não sujar a sessão em caso de erro.
        if status is not None:
            _status_preview = status.strip().upper()
            _blocking_rows = (
                db.session.query(ReservaStatusConfig.internal_name)
                .filter(ReservaStatusConfig.blocks_resource.is_(True))
                .all()
            )
            _blocking_names = {r.internal_name for r in _blocking_rows} or {"PENDING", "PAID", "PARTIALLY_PAID"}

            if _status_preview in _blocking_names and _status_preview != "PENDING":
                _links_a_verificar: list[tuple[int, int]] = []
                if items_payload:
                    for _it in items_payload:
                        _rid = _it.get("resource_id")
                        if _rid:
                            _links_a_verificar.append((int(_rid), int(_it.get("quantity", 1))))
                else:
                    for _lk in reserva_anterior.items:
                        if _lk.deleted_at is None and _lk.resource_id is not None:
                            _links_a_verificar.append((_lk.resource_id, _lk.quantity))

                # Acumulador POR RECURSO, não global. `extra_consumed` existe
                # para impedir que dois itens do mesmo payload consumam a
                # mesma unidade física duas vezes — logo só faz sentido somar
                # dentro do mesmo resource_id. Somando num inteiro único, o
                # segundo recurso herdava o consumo do primeiro: uma reserva
                # com duas churrasqueiras distintas (estoque 1 cada) nunca
                # podia virar PAID, porque o segundo item via
                # consumido=0 + extra=1 + solicitado=1 = 2 > 1 e acusava
                # overbooking num recurso que estava livre.
                _extra_por_recurso: dict[int, int] = {}
                for _rid, _qty in _links_a_verificar:
                    _rec = db.session.get(ReservaResource, _rid)
                    if _rec and _rec.stock_quantity is not None:
                        _extra = _extra_por_recurso.get(_rid, 0)
                        try:
                            cls._verificar_estoque(
                                _rec,
                                efetivo_check_in,
                                efetivo_check_out,
                                _qty,
                                extra_consumed=_extra,
                                exclude_reserva_id=reserva_anterior.id,
                            )
                            _extra_por_recurso[_rid] = _extra + _qty
                        except OverbookingError as _exc:
                            logger.warning(
                                "Transição de status bloqueada por overbooking | "
                                "reserva_id=%d group_id=%s novo_status=%s | %s",
                                reserva_anterior.id, reserva_anterior.group_id,
                                _status_preview, _exc,
                            )
                            raise

        efetivo_metadata = metadata_info if metadata_info is not None else reserva_anterior.metadata_info
        metadata_lookup = efetivo_metadata or {}
        efetivo_customer_name = customer_name if customer_name else reserva_anterior.customer_name
        efetivo_customer_document = (
            customer_document
            if customer_document is not None
            else reserva_anterior.customer_document
        )
        if reserva_anterior.source_id is not None:
            BlacklistService.assert_client_allowed(
                name=efetivo_customer_name,
                cpf=efetivo_customer_document,
                phone=(
                    metadata_lookup.get("telefone")
                    or metadata_lookup.get("phone")
                    or metadata_lookup.get("customer_phone")
                ),
                email=(
                    metadata_lookup.get("email")
                    or metadata_lookup.get("customer_email")
                ),
                location_id=reserva_anterior.source_id,
            )

        # Arquivar versão anterior
        reserva_anterior.status = ReservaStatus.ARCHIVED_VERSION

        # Determinar novo total para análise de status
        novo_total = Decimal(str(pricing["total"])) if pricing else reserva_anterior.total_amount
        total_anterior = reserva_anterior.total_amount or Decimal("0")

        # Criar nova versão
        nova = Reserva(
            group_id=reserva_anterior.group_id,
            version=reserva_anterior.version + 1,
            source_id=reserva_anterior.source_id,
            category_id=reserva_anterior.category_id,
            customer_name=efetivo_customer_name,
            customer_document=efetivo_customer_document,
            origin=reserva_anterior.origin,
            status=ReservaStatus.PENDING,  # provisório; corrigido abaixo
            check_in_date=efetivo_check_in,
            check_out_date=efetivo_check_out,
            expires_at=reserva_anterior.expires_at,
            metadata_info=efetivo_metadata,
            subtotal_amount=Decimal(str(pricing["subtotal"])) if pricing else reserva_anterior.subtotal_amount,
            discount_amount=Decimal(str(pricing["discount_amount"])) if pricing else reserva_anterior.discount_amount,
            service_fee_amount=Decimal(str(pricing["service_fee"])) if pricing else reserva_anterior.service_fee_amount,
            total_amount=novo_total,
            # Gravado mesmo quando o preço é preservado: materializar o resíduo
            # estabiliza a inferência. A partir daqui o percentual é lido da
            # coluna, sem depender de re-derivar a razão desconto/subtotal — que
            # é justamente o cálculo frágil quando um item sai do catálogo.
            desconto_percentual_manual=efetivo_desconto_pct_decimal,
            criado_por_id=ator_id,
            notas_operador=reserva_anterior.notas_operador,
            nota_relatorio=reserva_anterior.nota_relatorio,  # preserva nota de relatório entre versões
        )
        # Determinar status: usar override manual ou herdar anterior
        if status:
            # Status fornecido explicitamente (override manual).
            # Tenta resolver como membro do enum; aceita status customizado como string.
            status_upper = status.upper()
            try:
                nova.status = ReservaStatus[status_upper]
            except KeyError:
                # Status customizado criado via ReservaStatusConfig — aceitar como string.
                # A validação contra ReservaStatusConfig é responsabilidade do chamador (blueprint).
                nova.status = status_upper
        else:
            # Herdar status anterior; rebaixar PAID→PENDING se total aumentou
            status_anterior = snapshot_anterior.get("status")
            try:
                status_herdado = ReservaStatus[status_anterior] if status_anterior else ReservaStatus.PENDING
            except KeyError:
                # Status customizado herdado — preservar como string
                status_herdado = status_anterior or ReservaStatus.PENDING

            if (
                status_herdado == ReservaStatus.PAID
                and pricing
                and novo_total > total_anterior + Decimal("0.01")
            ):
                nova.status = ReservaStatus.PENDING
            else:
                nova.status = status_herdado

        db.session.add(nova)
        db.session.flush()

        # Migrar item links se não foram fornecidos novos
        if items_payload:
            # Preço preservado → os valores monetários vêm da versão anterior, não
            # do payload. O payload ainda manda em `resource_id`, porque trocar a
            # unidade física é edição legítima e não mexe no que se cobra.
            valores_anteriores = {
                link.item_id: link
                for link in reserva_anterior.items
                if link.deleted_at is None and link.item_id is not None
            } if preservar_preco else {}

            for it in items_payload:
                item = db.session.get(ReservaItem, it["item_id"])
                if not item:
                    raise ValueError(f"Item {it['item_id']} não encontrado.")
                anterior = valores_anteriores.get(int(it["item_id"]))
                if anterior is not None:
                    link = ReservaItemLink(
                        reserva_id=nova.id,
                        item_id=it["item_id"],
                        resource_id=it.get("resource_id"),
                        quantity=it.get("quantity", 1),
                        locked_price=anterior.locked_price,
                        unit_price=anterior.unit_price,
                        item_total=anterior.item_total,
                    )
                else:
                    link = ReservaItemLink(
                        reserva_id=nova.id,
                        item_id=it["item_id"],
                        resource_id=it.get("resource_id"),
                        quantity=it.get("quantity", 1),
                        locked_price=Decimal(str(it.get("price_override", item.price))),
                    )
                db.session.add(link)
        else:
            for link_anterior in reserva_anterior.items:
                if link_anterior.deleted_at is None:
                    novo_link = ReservaItemLink(
                        reserva_id=nova.id,
                        item_id=link_anterior.item_id,
                        resource_id=link_anterior.resource_id,
                        quantity=link_anterior.quantity,
                        locked_price=link_anterior.locked_price,
                        unit_price=link_anterior.unit_price,
                        item_total=link_anterior.item_total,
                    )
                    db.session.add(novo_link)

        snapshot_novo = cls._snapshot(nova, cls._CAMPOS_SNAPSHOT)
        AuditService.log_update(
            "reservas",
            entity_id=nova.id,
            previous_state=snapshot_anterior,
            new_state=snapshot_novo,
            actor_id=ator_id,
        )

        db.session.commit()
        return nova

    @classmethod
    def cancelar(
        cls,
        reserva_id: int,
        *,
        motivo: str | None = None,
        ator_id: int | None = None,
    ) -> Reserva:
        """Cancela uma reserva criando nova versão com status=CANCELED.

        Preserva o histórico de versões — a versão anterior é arquivada e
        uma nova linha com status CANCELED é inserida.

        Args:
            reserva_id: ID da reserva ativa a ser cancelada.
            motivo: Motivo do cancelamento (armazenado em metadata_info).
            ator_id: ID do usuário executando a ação (para auditoria).

        Returns:
            Nova instância de Reserva com status=CANCELED.

        Raises:
            ValueError: Se reserva não encontrada, deletada ou já cancelada.
        """
        reserva = db.session.get(Reserva, reserva_id)

        if not reserva or reserva.deleted_at is not None:
            raise ValueError("Reserva não encontrada.")

        if reserva.status == ReservaStatus.CANCELED:
            raise ValueError("Reserva já está cancelada.")

        if reserva.status == ReservaStatus.ARCHIVED_VERSION:
            raise ValueError("Não é possível cancelar uma versão arquivada.")

        snapshot_anterior = cls._snapshot(reserva, cls._CAMPOS_SNAPSHOT)

        reserva.status = ReservaStatus.ARCHIVED_VERSION

        nova_meta = dict(reserva.metadata_info or {})
        if motivo:
            nova_meta["motivo_cancelamento"] = motivo

        nova = Reserva(
            group_id=reserva.group_id,
            version=reserva.version + 1,
            source_id=reserva.source_id,
            category_id=reserva.category_id,
            customer_name=reserva.customer_name,
            customer_document=reserva.customer_document,
            origin=reserva.origin,
            status=ReservaStatus.CANCELED,
            check_in_date=reserva.check_in_date,
            check_out_date=reserva.check_out_date,
            expires_at=reserva.expires_at,
            metadata_info=nova_meta,
            subtotal_amount=reserva.subtotal_amount,
            discount_amount=reserva.discount_amount,
            service_fee_amount=reserva.service_fee_amount,
            total_amount=reserva.total_amount,
            desconto_percentual_manual=reserva.desconto_percentual_manual,
        )
        db.session.add(nova)
        db.session.flush()

        for link in reserva.items:
            if link.deleted_at is None:
                novo_link = ReservaItemLink(
                    reserva_id=nova.id,
                    item_id=link.item_id,
                    quantity=link.quantity,
                    locked_price=link.locked_price,
                    unit_price=link.unit_price,
                    item_total=link.item_total,
                )
                db.session.add(novo_link)

        snapshot_novo = cls._snapshot(nova, cls._CAMPOS_SNAPSHOT)
        AuditService.log_update(
            "reservas",
            entity_id=nova.id,
            previous_state=snapshot_anterior,
            new_state=snapshot_novo,
            actor_id=ator_id,
        )

        db.session.commit()
        return nova

    # ------------------------------------------------------------------
    # Reconciliação de pagamento e cancelamento automático
    # ------------------------------------------------------------------
    #
    # Duas rotinas complementares, pensadas para rodar em sequência no mesmo
    # ciclo do scheduler:
    #
    #   1. reconciliar_pagamentos_pendentes — tenta confirmar via MP tudo que
    #      ainda não foi confirmado pelo webhook. Cobre qualquer origem.
    #   2. cancelar_pendentes_vencidas — só depois disso cancela o que segue
    #      pendente além do prazo configurado. Rodar nesta ordem evita
    #      cancelar uma reserva cujo pagamento só não tinha sido reconciliado
    #      ainda.

    @classmethod
    def reconciliar_pagamentos_pendentes(
        cls,
        *,
        min_age_hours: int | None = 2,
        origins: list[ReservaOrigin] | None = None,
    ) -> dict[str, Any]:
        """Reconsulta o Mercado Pago para reservas pendentes com pagamento em aberto.

        Rede de segurança para quando o webhook do MP não chega — reconsulta
        pelo mesmo caminho que o webhook usaria (``MercadoPagoService.
        processar_webhook``), que por sua vez nunca confia no payload, sempre
        confere o status real na API do MP antes de mudar qualquer coisa.

        Args:
            min_age_hours: Só considera reservas cujo ``expires_at`` já passou
                há pelo menos este tanto de horas — dá tempo do webhook chegar
                pelo caminho normal antes de gastar uma chamada ao MP.
            origins: Origens a varrer. Default: todas.

        Returns:
            Dict com ``verificadas``, ``confirmadas`` e ``erros``.
        """
        from app.services.payment_service import MercadoPagoService

        if origins is None:
            origins = list(ReservaOrigin)

        query = db.session.query(Reserva).filter(
            Reserva.deleted_at.is_(None),
            Reserva.status.in_([ReservaStatus.PENDING, ReservaStatus.PARTIALLY_PAID]),
            Reserva.origin.in_(origins),
            Reserva.payment_id.isnot(None),
        )
        if min_age_hours is not None:
            limite = datetime.now(timezone.utc) - timedelta(hours=max(0, int(min_age_hours)))
            query = query.filter(Reserva.expires_at <= limite)

        verificadas = 0
        confirmadas = 0
        erros = 0

        for reserva in query.all():
            verificadas += 1
            status_anterior = reserva.status
            try:
                MercadoPagoService.processar_webhook({
                    "type": "payment",
                    "data": {"id": reserva.payment_id},
                })
            except Exception:
                logger.exception(
                    "reconciliar_pagamentos_pendentes: falha ao reconsultar MP para reserva %s.",
                    reserva.id,
                )
                erros += 1
                continue

            db.session.refresh(reserva)
            if reserva.status != status_anterior:
                confirmadas += 1

        return {"verificadas": verificadas, "confirmadas": confirmadas, "erros": erros}

    @classmethod
    def cancelar_pendentes_vencidas(cls) -> dict[str, Any]:
        """Cancela automaticamente reservas paradas além do prazo configurado.

        Só afeta status com ``ReservaStatusConfig.auto_cancel_hours`` definido
        — por padrão nenhum status tem, então a rotina não faz nada até um
        administrador configurar um prazo em /reservas/admin/config/status. O
        prazo conta a partir de ``expires_at`` (quando o estoque físico já foi
        liberado), não da criação — uma reserva ainda dentro do soft-lock não
        é candidata, esteja o prazo configurado for pequeno ou não.

        Chame depois de ``reconciliar_pagamentos_pendentes`` no mesmo ciclo,
        para não cancelar uma reserva cujo pagamento só não tinha sido
        confirmado ainda.

        Returns:
            Dict com ``avaliadas`` e ``canceladas``.
        """
        prazos = dict(
            db.session.query(
                ReservaStatusConfig.internal_name, ReservaStatusConfig.auto_cancel_hours
            ).filter(ReservaStatusConfig.auto_cancel_hours.isnot(None))
        )
        if not prazos:
            return {"avaliadas": 0, "canceladas": 0}

        agora = datetime.now(timezone.utc)
        avaliadas = 0
        canceladas = 0

        for status_nome, horas in prazos.items():
            try:
                status_enum = ReservaStatus(status_nome)
            except ValueError:
                continue  # entradas visuais como DEPOSIT não mapeiam para ReservaStatus

            limite = agora - timedelta(hours=max(0, int(horas)))
            candidatas = db.session.query(Reserva).filter(
                Reserva.deleted_at.is_(None),
                Reserva.status == status_enum,
                Reserva.expires_at <= limite,
            ).all()

            for reserva in candidatas:
                avaliadas += 1
                try:
                    cls.cancelar(
                        reserva.id,
                        motivo="Cancelamento automático — pendente vencida sem confirmação de pagamento.",
                        ator_id=None,
                    )
                    canceladas += 1
                except ValueError:
                    # Já cancelada ou arquivada por outro caminho entre a consulta e aqui.
                    continue
                except Exception:
                    logger.exception(
                        "cancelar_pendentes_vencidas: falha ao cancelar reserva %s.",
                        reserva.id,
                    )

        return {"avaliadas": avaliadas, "canceladas": canceladas}

    @classmethod
    def deletar(
        cls,
        reserva_id: int,
        *,
        ator_id: int | None = None,
    ) -> None:
        """Soft-deleta uma reserva definindo deleted_at (nunca remove a linha).

        Aplica soft delete em todas as versões do mesmo group_id para
        garantir que o histórico completo seja oculto consistentemente.
        Registra AuditService.log_delete() com snapshot da versão ativa.

        Args:
            reserva_id: ID da reserva a ser deletada.
            ator_id: ID do usuário executando a ação (para auditoria).

        Raises:
            ValueError: Se reserva não encontrada ou já deletada.
        """
        reserva = db.session.get(Reserva, reserva_id)

        if not reserva or reserva.deleted_at is not None:
            raise ValueError("Reserva não encontrada.")

        snapshot = cls._snapshot(reserva, cls._CAMPOS_SNAPSHOT)

        agora = datetime.now(timezone.utc)

        # Soft-deleta todas as versões do mesmo grupo
        todas_versoes = db.session.query(Reserva).filter(
            Reserva.group_id == reserva.group_id,
            Reserva.deleted_at.is_(None),
        ).all()

        ids_deletados = []
        for versao in todas_versoes:
            versao.deleted_at = agora
            ids_deletados.append(versao.id)

        AuditService.log_delete(
            "reservas",
            entity_id=reserva_id,
            previous_state=snapshot,
            actor_id=ator_id,
        )

        db.session.commit()

    @classmethod
    def nomes_de_ids(cls, source_id: Any, category_id: Any) -> tuple[str, str]:
        """Resolve IDs de fonte e categoria para os nomes que ``criar`` espera.

        ``criar`` recebe nomes porque cria a fonte quando ela não existe — um
        comportamento útil para o balcão e perigoso para o site: um nome
        digitado errado abriria um venue novo em silêncio. Consumidores externos
        mandam IDs, e este método falha quando o ID não existe, em vez de
        inventar.

        Args:
            source_id: ID da fonte.
            category_id: ID da categoria, que precisa pertencer à fonte.

        Returns:
            Tupla ``(nome_da_fonte, nome_da_categoria)``.

        Raises:
            LookupError: Se a fonte ou a categoria não existir, estiver removida,
                ou a categoria pertencer a outra fonte.
        """
        fonte = db.session.get(ReservaSource, source_id) if source_id else None
        if fonte is None or fonte.deleted_at is not None:
            raise LookupError("Local de reserva não encontrado.")

        categoria = db.session.get(ReservaCategory, category_id) if category_id else None
        if categoria is None or categoria.deleted_at is not None:
            raise LookupError("Tipo de reserva não encontrado.")

        # Sem esta checagem, um category_id de outro venue passaria e a reserva
        # nasceria no lugar errado.
        if categoria.source_id != fonte.id:
            raise LookupError("Tipo de reserva não pertence a este local.")

        return fonte.name, categoria.name

    @classmethod
    def listar_fontes(cls) -> list[ReservaSource]:
        """Lista todas as fontes (venues) ativas com suas categorias.

        Retorna apenas fontes com `deleted_at IS NULL` e `is_active=True`,
        com categorias ativas aninhadas.

        Returns:
            Lista de ReservaSource com categorias não deletadas carregadas
        """
        sources = (
            db.session.query(ReservaSource)
            .outerjoin(ReservaSource.categories)
            .options(contains_eager(ReservaSource.categories))
            .filter(
                ReservaSource.deleted_at.is_(None),
                ReservaSource.is_active == True,
                or_(
                    ReservaCategory.deleted_at.is_(None),
                    ReservaCategory.id.is_(None),
                ),
            )
            .order_by(ReservaSource.name)
            .all()
        )

        return sources

    @staticmethod
    def catalogo(source_id: int | None = None) -> list[dict]:
        """Retorna catálogo público de fontes, categorias e itens disponíveis.

        Usado pelo endpoint público GET /reservas/api/public/catalogo.
        Não expõe quantidades de estoque — apenas exclui itens esgotados.

        Regras de inclusão de itens:
          - Incluído: sem resource vinculado (resource_id IS NULL)
          - Incluído: resource com stock_quantity IS NULL (ilimitado)
          - Incluído: resource com stock_quantity > 0
          - Excluído: resource com stock_quantity = 0 (esgotado)

        Args:
            source_id: Se informado, filtra pela fonte específica. Se None,
                       retorna todas as fontes ativas.

        Returns:
            Lista de dicts no formato:
            [{"id": int, "name": str, "categories": [{"id": int, "name": str,
            "items": [{"id": int, "name": str, "price": float,
            "billing_type": str, "requires_deposit": bool}]}]}]
        """
        source_query = (
            db.session.query(ReservaSource)
            .filter(
                ReservaSource.deleted_at.is_(None),
                ReservaSource.is_active == True,
            )
            .options(
                selectinload(ReservaSource.categories)
                .selectinload(ReservaCategory.items)
            )
            .order_by(ReservaSource.name)
        )

        if source_id is not None:
            source_query = source_query.filter(ReservaSource.id == source_id)

        sources = source_query.all()

        result = []
        for source in sources:
            categories_out = []
            for category in source.categories:
                if category.deleted_at is not None:
                    continue
                items_out = []
                for item in category.items:
                    if item.deleted_at is not None:
                        continue
                    items_out.append({
                        "id": item.id,
                        "internal_slug": item.internal_slug,
                        "name": item.name,
                        "price": float(item.price),
                        "billing_type": item.billing_type.value,
                        "requires_deposit": item.requires_deposit,
                        "requires_resource": item.requires_resource,
                        "group_id": item.group_id,
                    })
                categories_out.append({
                    "id": category.id,
                    "name": category.name,
                    "items": items_out,
                })
            result.append({
                "id": source.id,
                "name": source.name,
                "categories": categories_out,
            })

        return result

    @classmethod
    def verificar_disponibilidade(
        cls,
        category_id: int,
        check_in: date,
        check_out: date,
    ) -> list[dict]:
        """Calcula a disponibilidade de itens em uma categoria para um período.

        Implementa o motor de disponibilidade: para cada item global,
        calcula estoque consumido por reservas ativas que sobrepõem
        o período solicitado.

        Regras:
        - Respeita `stock_quantity = NULL` como estoque infinito
        - Ignora reservas CANCELED e ARCHIVED_VERSION
        - PENDING incluso apenas se expires_at > agora
        - PAID sempre incluído

        Args:
            category_id: ID da categoria a verificar
            check_in: Data de início (YYYY-MM-DD)
            check_out: Data de término (YYYY-MM-DD)

        Returns:
            Lista de dicts com {"item_id", "name", "billing_type", "requires_deposit",
                                "stock_quantity", "consumed", "available"}
        """
        # Validate dates
        if check_out < check_in:
            raise ValueError("check_out deve ser >= check_in.")

        # Find overlapping reservas — compara apenas a parte de data (sem horário)
        effective_checkout = func.coalesce(Reserva.check_out_date, Reserva.check_in_date)

        overlapping_ids = (
            db.session.query(Reserva.id)
            .filter(
                Reserva.category_id == category_id,
                Reserva.deleted_at.is_(None),
                Reserva.status != ReservaStatus.ARCHIVED_VERSION,
                Reserva.status != ReservaStatus.CANCELED,
                or_(
                    and_(
                        Reserva.status == ReservaStatus.PENDING,
                        Reserva.expires_at > datetime.now(timezone.utc),
                    ),
                    Reserva.status == ReservaStatus.PAID,
                ),
                func.date(Reserva.check_in_date) <= check_out,
                func.date(effective_checkout) >= check_in,
            )
            .all()
        )

        overlapping_ids_list = [row.id for row in overlapping_ids]

        # Agregar consumo por item_id a partir das reservas sobrepostas
        item_consumed_map: dict[int, int] = {}
        if overlapping_ids_list:
            rows = (
                db.session.query(
                    ReservaItemLink.item_id,
                    func.sum(ReservaItemLink.quantity).label("total_consumed"),
                )
                .filter(
                    ReservaItemLink.reserva_id.in_(overlapping_ids_list),
                    ReservaItemLink.deleted_at.is_(None),
                )
                .group_by(ReservaItemLink.item_id)
                .all()
            )
            item_consumed_map = {row.item_id: row.total_consumed for row in rows}

        # Fetch all active items in the category
        items = db.session.query(ReservaItem).filter(
            ReservaItem.category_id == category_id,
            ReservaItem.deleted_at.is_(None)
        ).all()

        # Build result: consumo por item (items não têm mais resource_id próprio;
        # disponibilidade de recursos físicos é consultada via verificar_disponibilidade_recursos)
        result = []
        for item in items:
            consumed = item_consumed_map.get(item.id, 0)

            result.append({
                "item_id": item.id,
                "name": item.name,
                "billing_type": item.billing_type.value,
                "requires_deposit": item.requires_deposit,
                "stock_quantity": None,   # sem recurso fixo no catálogo
                "consumed": consumed,
                "available": None,        # usar verificar_disponibilidade_recursos para recursos físicos
            })

        return result

    @classmethod
    def verificar_disponibilidade_recursos(
        cls,
        source_id: int,
        check_in: date,
        check_out: date,
    ) -> list[dict]:
        """Calcula a disponibilidade de recursos físicos de uma fonte para um período.

        Retorna cada ativo (ReservaResource) com seu status de disponibilidade,
        agrupado pelo ReservaGroup para exibição em accordion no painel do atendente.

        Regras:
        - stock_quantity NULL = infinito (is_available sempre True)
        - Conta consumo de ReservaItemLink.resource_id em reservas PENDING/PAID
        - Ignora CANCELED e ARCHIVED_VERSION
        - PENDING conta apenas se expires_at > agora

        Args:
            source_id: ID da fonte (venue) a verificar
            check_in: Data de início
            check_out: Data de término

        Returns:
            Lista de dicts com {resource_id, name, description, image_url,
            stock_quantity, consumed, available, is_available, group_id,
            group_name, group_path} ordenados por group_path, name.
        """
        if check_out < check_in:
            raise ValueError("check_out deve ser >= check_in.")

        # Statuses de bloqueio permanente, vindos da configuração.
        _blocking_rows = (
            db.session.query(ReservaStatusConfig.internal_name)
            .filter(ReservaStatusConfig.blocks_resource.is_(True))
            .all()
        )
        _blocking_names = {r.internal_name for r in _blocking_rows}
        # Fallback se ReservaStatusConfig estiver vazia (instalação nova)
        if not _blocking_names:
            _blocking_names = {"PAID"}

        _status_conds = [
            Reserva.status == name
            for name in _blocking_names
            if name not in cls._STATUS_COM_SOFT_LOCK
        ]

        # Soft lock: uma reserva não paga ocupa o recurso enquanto expires_at não
        # vence. A mesma regra de `_verificar_estoque`, e pelo mesmo motivo —
        # esta função agora responde ao formulário do site, e sem isto mostraria
        # a Churrasqueira A4 como livre enquanto alguém está pagando por ela. Os
        # dois clientes chegariam ao fim, e o segundo seria recusado na criação.
        #
        # O prazo é essencial nos dois sentidos: sem ele as PENDING abandonadas
        # travariam recursos para sempre, e aplicá-lo aos demais status soltaria
        # todas as reservas pagas, já que `expires_at` nunca é limpo no pagamento.
        _status_conds.append(
            and_(
                Reserva.status == "PENDING",
                Reserva.expires_at.isnot(None),
                Reserva.expires_at > datetime.now(timezone.utc),
            )
        )

        # Encontrar reservas com sobreposição de datas nesta fonte
        effective_checkout = func.coalesce(Reserva.check_out_date, Reserva.check_in_date)

        overlapping_ids_list = [
            row.id
            for row in db.session.query(Reserva.id).filter(
                Reserva.source_id == source_id,
                Reserva.deleted_at.is_(None),
                or_(*_status_conds),
                func.date(Reserva.check_in_date) <= check_out,
                func.date(effective_checkout) >= check_in,
            ).all()
        ]

        # Agregar consumo por resource_id nas reservas sobrepostas
        resource_consumed_map: dict[int, int] = {}
        if overlapping_ids_list:
            rows = (
                db.session.query(
                    ReservaItemLink.resource_id,
                    func.sum(ReservaItemLink.quantity).label("total_consumed"),
                )
                .filter(
                    ReservaItemLink.reserva_id.in_(overlapping_ids_list),
                    ReservaItemLink.resource_id.isnot(None),
                    ReservaItemLink.deleted_at.is_(None),
                )
                .group_by(ReservaItemLink.resource_id)
                .all()
            )
            resource_consumed_map = {row.resource_id: row.total_consumed for row in rows}

        # Buscar todos os recursos ativos da fonte
        resources = (
            db.session.query(ReservaResource)
            .filter(
                ReservaResource.source_id == source_id,
                ReservaResource.deleted_at.is_(None),
            )
            .order_by(ReservaResource.name)
            .all()
        )

        # Pré-carregar itens por group_id em uma única query — evita N+1.
        # Retorna todos os items cujo group_id coincide com algum recurso desta fonte.
        group_ids = {r.group_id for r in resources if r.group_id is not None}
        items_by_group: dict[int, list] = {}
        if group_ids:
            group_item_rows = (
                db.session.query(ReservaItem, ReservaCategory)
                .join(ReservaCategory, ReservaItem.category_id == ReservaCategory.id)
                .filter(
                    ReservaItem.group_id.in_(group_ids),
                    ReservaItem.deleted_at.is_(None),
                    ReservaCategory.source_id == source_id,
                    ReservaCategory.deleted_at.is_(None),
                )
                .all()
            )
            for item, category in group_item_rows:
                gid = item.group_id
                if gid not in items_by_group:
                    items_by_group[gid] = []
                items_by_group[gid].append({
                    "item_id": item.id,
                    "internal_slug": item.internal_slug,
                    "category_name": category.name,
                    "price": float(item.price),
                    "price_weekday": float(item.price_weekday) if item.price_weekday is not None else None,
                    "price_weekend": float(item.price_weekend) if item.price_weekend is not None else None,
                    "price_holiday": float(item.price_holiday) if item.price_holiday is not None else None,
                })

        result = []
        for resource in resources:
            stock = resource.stock_quantity
            consumed = resource_consumed_map.get(resource.id, 0)

            if stock is None:
                available = None
                is_available = True
            else:
                available = max(0, stock - consumed)
                is_available = available > 0

            group_id = resource.group_id
            group_name = None
            group_path = None
            if resource.group and resource.group.deleted_at is None:
                group_name = resource.group.name
                group_path = resource.group.display_path

            # Galeria de imagens: lista ordenada por sort_order.
            # image_url do recurso serve como fallback se a galeria estiver vazia.
            gallery = [
                {"url": img.image_url, "sort_order": img.sort_order}
                for img in resource.images
                # images relationship já filtra deleted_at via primaryjoin
            ]
            primary_image = gallery[0]["url"] if gallery else resource.image_url

            result.append({
                "resource_id": resource.id,
                "name": resource.name,
                "description": resource.description,
                "image_url": primary_image,   # backward compat — primeira imagem ou legado
                "images": gallery,             # galeria completa para carousel
                "stock_quantity": stock,
                "consumed": consumed,
                "available": available,
                "is_available": is_available,
                "group_id": group_id,
                "group_name": group_name,
                "group_path": group_path,
                # Items vinculados ao mesmo grupo — inclui tarifas por tipo de dia.
                "items": items_by_group.get(group_id, []),
            })

        # Ordenar por group_path (None por último), depois name
        result.sort(key=lambda r: (r["group_path"] or "\xff", r["name"]))
        return result

    @classmethod
    def timeline_mensal(
        cls,
        source_id: int,
        year: int,
        month: int,
    ) -> dict:
        """Retorna o timeline mensal de ocupação por recurso físico (visão Gantt).

        Para cada recurso da fonte, lista as reservas ativas que se sobrepõem
        ao mês solicitado, identificando o papel de cada dia: CHECK_IN, STAY
        ou CHECK_OUT. Usado pelo frontend para renderizar o calendário Gantt
        de camping e estadias multi-dia.

        Regras de negócio:
        - Statuses incluídos: PENDING, PARTIALLY_PAID, PAID, COMPLETED.
        - CANCELED e ARCHIVED_VERSION são excluídos.
        - check_out_date NULL → reserva de dia único; o dia é marcado como CHECK_IN.
        - Quando check_in == check_out, o dia é marcado como CHECK_IN.
        - Somente recursos com ReservaItemLink na reserva são incluídos.

        Args:
            source_id: ID da ReservaSource (venue).
            year:  Ano do mês a exibir.
            month: Mês (1–12) a exibir.

        Returns:
            Dict com source_id, source_name, year, month, num_days e lista
            de resources, cada um com suas reservations e mapa days {day: label}.

        Raises:
            ValueError: Se source_id não existir.
        """
        source = db.session.get(ReservaSource, source_id)
        if not source or source.deleted_at is not None:
            raise ValueError(f"Fonte {source_id} não encontrada.")

        _, last_day = monthrange(year, month)
        month_start = date(year, month, 1)
        month_end = date(year, month, last_day)

        # Statuses ativos que aparecem no timeline
        active_statuses = [
            ReservaStatus.PENDING,
            ReservaStatus.PARTIALLY_PAID,
            ReservaStatus.PAID,
            ReservaStatus.COMPLETED,
        ]

        # Reservas que se sobrepõem ao mês: check_in <= fim_do_mês AND check_out >= início_do_mês
        reservas = (
            db.session.query(Reserva)
            .filter(
                Reserva.source_id == source_id,
                Reserva.deleted_at.is_(None),
                Reserva.status.in_(active_statuses),
                func.date(Reserva.check_in_date) <= month_end,
                func.date(
                    func.coalesce(Reserva.check_out_date, Reserva.check_in_date)
                ) >= month_start,
            )
            .order_by(Reserva.check_in_date)
            .all()
        )

        reserva_ids = [r.id for r in reservas]
        reserva_map = {r.id: r for r in reservas}

        # Links com resource_id — cada link é uma linha no Gantt
        links: list[ReservaItemLink] = []
        if reserva_ids:
            links = (
                db.session.query(ReservaItemLink)
                .filter(
                    ReservaItemLink.reserva_id.in_(reserva_ids),
                    ReservaItemLink.resource_id.isnot(None),
                    ReservaItemLink.deleted_at.is_(None),
                )
                .all()
            )

        # Agrupar entradas por resource_id
        # Para recursos com stock_quantity > 1, cada unidade de quantity gera
        # uma entrada separada (row) no Gantt.
        resource_rows: dict[int, list[dict]] = {}
        for link in links:
            reserva = reserva_map[link.reserva_id]

            # Normalizar datas para date puro
            ci: date = (
                reserva.check_in_date.date()
                if isinstance(reserva.check_in_date, datetime)
                else reserva.check_in_date
            )
            co: date = (
                reserva.check_out_date.date()
                if reserva.check_out_date and isinstance(reserva.check_out_date, datetime)
                else (reserva.check_out_date or ci)
            )

            # Calcular dias visíveis neste mês
            visible_start = max(ci, month_start)
            visible_end = min(co, month_end)

            days: dict[int, str] = {}
            current = visible_start
            while current <= visible_end:
                if ci == co:
                    # Reserva de dia único
                    days[current.day] = "CHECK_IN"
                elif current == ci:
                    days[current.day] = "CHECK_IN"
                elif current == co:
                    days[current.day] = "CHECK_OUT"
                else:
                    days[current.day] = "STAY"
                current += timedelta(days=1)

            if not days:
                continue

            entry = {
                "reserva_id": reserva.id,
                "reserva_group_id": reserva.group_id,
                "customer_name": reserva.customer_name or "Sem nome",
                "status": reserva.status.value,
                "check_in_date": ci.isoformat(),
                "check_out_date": co.isoformat(),
                "quantity": link.quantity,
                "subtotal": float(reserva.subtotal_amount) if reserva.subtotal_amount is not None else None,
                "total": float(reserva.total_amount) if reserva.total_amount is not None else None,
                "days": days,
            }

            rid = link.resource_id
            if rid not in resource_rows:
                resource_rows[rid] = []
            resource_rows[rid].append(entry)

        # Carregar todos os recursos da fonte, ordenados por grupo → nome
        resources = (
            db.session.query(ReservaResource)
            .filter(
                ReservaResource.source_id == source_id,
                ReservaResource.deleted_at.is_(None),
            )
            .order_by(ReservaResource.name)
            .all()
        )

        resources_data = []
        for resource in resources:
            group_id = resource.group_id
            group_name = None
            group_path = None
            if resource.group and resource.group.deleted_at is None:
                group_name = resource.group.name
                group_path = resource.group.display_path

            resources_data.append({
                "resource_id": resource.id,
                "resource_name": resource.name,
                "group_id": group_id,
                "group_name": group_name,
                "group_path": group_path,
                "stock_quantity": resource.stock_quantity,
                "reservations": resource_rows.get(resource.id, []),
            })

        # Ordenar por group_path → name (sem grupo por último)
        resources_data.sort(key=lambda r: (r["group_path"] or "\xff", r["resource_name"]))

        return {
            "source_id": source_id,
            "source_name": source.name,
            "year": year,
            "month": month,
            "num_days": last_day,
            "resources": resources_data,
        }

    @classmethod
    def _pool_compartilhado_ja_atribuido(
        cls, source_id: int, group_id: int, seen_resource_ids: set[int]
    ) -> bool:
        """Diz se o grupo é um pool compartilhado já vinculado neste payload.

        Um recurso com ``stock_quantity`` nulo é capacidade ilimitada — vários
        itens da mesma reserva podem consumi-lo. Quando ele já foi vinculado por
        um item anterior, os seguintes não precisam de vínculo próprio.

        Args:
            source_id: ID da fonte (venue).
            group_id: Grupo do item que não encontrou recurso livre.
            seen_resource_ids: Recursos já vinculados neste request.

        Returns:
            True se o grupo tem um recurso de estoque ilimitado que já está em
            ``seen_resource_ids``.
        """
        return (
            db.session.query(ReservaResource.id)
            .filter(
                ReservaResource.source_id == source_id,
                ReservaResource.group_id == group_id,
                ReservaResource.deleted_at.is_(None),
                ReservaResource.stock_quantity.is_(None),
                ReservaResource.id.in_(seen_resource_ids or {-1}),
            )
            .first()
            is not None
        )

    @classmethod
    def _auto_selecionar_recurso(
        cls,
        source_id: int,
        group_id: int,
        check_in: date,
        check_out: date | None,
        quantidade: int,
        excluir_ids: set[int],
        resource_consumed_in_payload: dict[int, int],
    ) -> "ReservaResource | None":
        """Seleciona automaticamente o primeiro recurso disponível do grupo para o período.

        Usado no 'smart assignment': quando o cliente não escolhe um recurso físico
        específico, o servidor seleciona o primeiro ativo disponível do mesmo grupo
        do item, ordenado por nome.

        Args:
            source_id: ID da fonte (venue) para filtrar recursos.
            group_id: ID do grupo a buscar recursos (deve coincidir com item.group_id).
            check_in: Data de início da reserva.
            check_out: Data de término (None = mesmo dia).
            quantidade: Quantidade solicitada para o recurso.
            excluir_ids: IDs de recursos já selecionados neste payload (evita duplicata).
            resource_consumed_in_payload: Consumo acumulado por recurso neste request.

        Returns:
            Primeiro ReservaResource disponível, ou None se nenhum estiver disponível.
        """
        candidatos = (
            db.session.query(ReservaResource)
            .filter(
                ReservaResource.source_id == source_id,
                ReservaResource.group_id == group_id,
                ReservaResource.deleted_at.is_(None),
            )
            .order_by(ReservaResource.name)
            .all()
        )

        for resource in candidatos:
            if resource.id in excluir_ids:
                continue

            if resource.stock_quantity is None:
                # Estoque infinito — disponível sem verificação
                return resource

            extra = resource_consumed_in_payload.get(resource.id, 0)
            try:
                cls._verificar_estoque(
                    resource, check_in, check_out, quantidade, extra_consumed=extra
                )
                return resource
            except ValueError:
                continue

        return None

    # Status cujo bloqueio de recurso é temporário, limitado por expires_at.
    # Separado de `ReservaStatusConfig.blocks_resource`, que é um bloqueio
    # permanente: marcar PENDING lá faria uma reserva abandonada segurar o
    # recurso indefinidamente.
    _STATUS_COM_SOFT_LOCK = frozenset({"PENDING"})

    @classmethod
    def _verificar_estoque(
        cls,
        resource: "ReservaResource",
        check_in: date,
        check_out: date | None,
        requested_qty: int,
        extra_consumed: int = 0,
        exclude_reserva_id: int | None = None,
        force_overbooking: bool = False,
    ) -> None:
        """Verifica se há estoque disponível em um recurso físico para um período.

        Implementa a verificação de disponibilidade com Seleção Dinâmica de Recurso:
        consulta diretamente na coluna resource_id de ReservaItemLink, eliminando
        a necessidade de agregar "itens irmãos" (o recurso é o nível correto de agregação).

        Args:
            resource: ReservaResource (ativo físico) a verificar
            check_in: Data de início
            check_out: Data de término (ou None para 1 dia)
            requested_qty: Quantidade solicitada para este recurso
            extra_consumed: Consumo já acumulado por outros itens no mesmo payload
                (previne overbooking intra-request). Padrão: 0.
            exclude_reserva_id: ID de reserva a excluir da contagem (útil em edições,
                para não contar a própria reserva que está sendo atualizada).
            force_overbooking: Quando True, levanta OverbookingError mesmo assim
                (para que o chamador possa capturar e propagar os detalhes ao operador),
                mas apenas quando o recurso está realmente indisponível. O chamador
                com permissão de operador ignora o erro; outros deixam propagar.

        Raises:
            OverbookingError: Se estoque insuficiente para o recurso solicitado e
                force_overbooking=False. Quando force_overbooking=True, a exceção
                não é levantada — o conflito é silenciado intencionalmente.
        """
        # Lock pessimista: serializa transações concorrentes no mesmo recurso físico.
        # Sem FOR UPDATE, duas requisições simultâneas poderiam ler consumed=0,
        # ambas passariam na checagem e ambas commitariam — race condition clássico.
        resource_locked = (
            db.session.query(ReservaResource)
            .filter(ReservaResource.id == resource.id)
            .with_for_update()
            .first()
        )
        if resource_locked is None:
            return
        stock = resource_locked.stock_quantity

        if stock is None:
            # Estoque infinito — sem restrição
            return

        # Normalizar para date puro (ignora componente de horário na comparação)
        req_out_date = (check_out or check_in)
        if isinstance(req_out_date, datetime):
            req_out_date = req_out_date.date()
        check_in_date_only = check_in.date() if isinstance(check_in, datetime) else check_in

        # Encontrar reservas ativas que se sobrepõem ao período solicitado
        # func.date() extrai apenas a parte de data do campo DateTime, evitando
        # falsos negativos quando reservas têm horário de check-in/out != 00:00.
        effective_checkout = func.coalesce(Reserva.check_out_date, Reserva.check_in_date)

        # Consulta configuração dinâmica de blocking statuses
        blocking_rows = (
            db.session.query(ReservaStatusConfig.internal_name)
            .filter(ReservaStatusConfig.blocks_resource.is_(True))
            .all()
        )
        blocking_names = {row.internal_name for row in blocking_rows}

        # Fallback se ReservaStatusConfig estiver vazia (instalação nova)
        if not blocking_names:
            blocking_names = {"PAID"}

        # Constrói condições por status usando comparação de string direta.
        # Evita ValueError de conversão para enum quando há status customizados
        # (ex: "EM_USO", "DEPOSIT") que não existem no enum ReservaStatus.
        status_conditions = []
        for name in blocking_names:
            if name in cls._STATUS_COM_SOFT_LOCK:
                # Tratado abaixo, sempre com o prazo. Cair aqui faria uma
                # PENDING vencida segurar o recurso para sempre.
                continue
            # Comparação direta de string — funciona para enum e status customizados
            status_conditions.append(Reserva.status == name)

        # Soft lock: uma reserva não paga segura o recurso enquanto expires_at
        # não vence, e o solta sozinha depois.
        #
        # Esta condição é fixa, não vem de `blocks_resource`. PENDING está
        # configurada com blocks_resource=false, e o efeito era grave: um cliente
        # concluía o formulário e a churrasqueira continuava livre para quem
        # chegasse depois — dá para reservar a mesma churrasqueira de estoque 1
        # quantas vezes quiser, e os dois só descobrem no balcão.
        #
        # É também o que `verificar_disponibilidade` já fazia, então as duas
        # funções discordavam: a tela dizia "esgotado" e a criação aceitava.
        #
        # O prazo é o ponto central. Sem ele as 530 PENDING abandonadas (a mais
        # antiga de março) bloqueariam recursos para sempre. E ele NÃO se aplica
        # aos demais status: expires_at nunca é limpo no pagamento, então as 120
        # PAID e 614 EM_USO têm prazo vencido e precisam bloquear assim mesmo.
        status_conditions.append(
            and_(
                Reserva.status == "PENDING",
                Reserva.expires_at.isnot(None),
                # Aware UTC de propósito: a sessão do Postgres é Etc/UTC e a
                # coluna guarda UTC. `datetime.now()` seria hora local e erraria
                # em três horas.
                Reserva.expires_at > datetime.now(timezone.utc),
            )
        )

        if not status_conditions:
            return

        overlap_filter = [
            Reserva.deleted_at.is_(None),
            or_(*status_conditions),
            func.date(Reserva.check_in_date) <= req_out_date,
            func.date(effective_checkout) >= check_in_date_only,
        ]
        if exclude_reserva_id is not None:
            overlap_filter.append(Reserva.id != exclude_reserva_id)

        overlapping_ids_list = [
            row.id
            for row in db.session.query(Reserva.id).filter(*overlap_filter).all()
        ]

        # Agregar consumo deste recurso específico nas reservas sobrepostas
        consumed = 0
        if overlapping_ids_list:
            row = (
                db.session.query(func.sum(ReservaItemLink.quantity).label("total"))
                .filter(
                    ReservaItemLink.reserva_id.in_(overlapping_ids_list),
                    ReservaItemLink.resource_id == resource.id,
                    ReservaItemLink.deleted_at.is_(None),
                )
                .first()
            )
            consumed = row.total or 0

        # Verificar disponibilidade considerando consumo intra-payload
        total_needed = consumed + extra_consumed + requested_qty
        if total_needed > stock:
            available = max(0, stock - consumed - extra_consumed)
            logger.warning(
                "Overbooking bloqueado | recurso=%r id=%d | "
                "estoque=%d consumido=%d extra=%d solicitado=%d disponivel=%d",
                resource.name, resource.id,
                stock, consumed, extra_consumed, requested_qty, available,
            )

            # Buscar detalhes das reservas conflitantes para exibir ao operador
            conflitos: list[dict] = []
            if overlapping_ids_list:
                reservas_conflito = (
                    db.session.query(
                        Reserva.id,
                        Reserva.customer_name,
                        Reserva.check_in_date,
                        Reserva.check_out_date,
                        Reserva.status,
                    )
                    .join(
                        ReservaItemLink,
                        and_(
                            ReservaItemLink.reserva_id == Reserva.id,
                            ReservaItemLink.resource_id == resource.id,
                            ReservaItemLink.deleted_at.is_(None),
                        ),
                    )
                    .filter(Reserva.id.in_(overlapping_ids_list))
                    .all()
                )
                for rc in reservas_conflito:
                    ci = rc.check_in_date
                    co = rc.check_out_date
                    conflitos.append({
                        "reserva_id": rc.id,
                        "customer_name": rc.customer_name or "Sem nome",
                        "check_in": ci.strftime("%d/%m/%Y %H:%M") if isinstance(ci, datetime) else (ci.strftime("%d/%m/%Y") if ci else "-"),
                        "check_out": co.strftime("%d/%m/%Y %H:%M") if isinstance(co, datetime) else (co.strftime("%d/%m/%Y") if co else "-"),
                        "status": rc.status.value if hasattr(rc.status, "value") else str(rc.status),
                    })

            if force_overbooking:
                logger.warning(
                    "Overbooking forçado pelo operador | recurso=%r id=%d | "
                    "consumido=%d disponivel=%d solicitado=%d",
                    resource.name, resource.id, consumed, available, requested_qty,
                )
                return

            raise OverbookingError(
                f"Recurso '{resource.name}' já reservado para este período. "
                f"Disponível: {available}, solicitado: {requested_qty}.",
                resource_name=resource.name,
                conflitos=conflitos,
            )


# ---------------------------------------------------------------------------
# SecurityDepositService — Cauções de Reserva
# ---------------------------------------------------------------------------


class SecurityDepositService(BaseService):
    """Gerencia cauções (security deposits) vinculadas a reservas.

    Uma caução é um valor cobrado do cliente como garantia para uso de um
    ativo físico (churrasqueira, equipamento, etc.). Ela é tratada como
    componente financeiro *separado* do total_amount da Reserva: incrementa
    o pending_balance sem alterar o subtotal/desconto/taxa da reserva.

    Ciclo de vida:
        criar()   → is_paid=False, is_refunded=False
        pagar()   → is_paid=True  (PhysicalAsset → UNDER_DEPOSIT se vinculado)
        devolver() → is_refunded=True (PhysicalAsset → AVAILABLE se vinculado)
        deletar() → soft-delete (only allowed before is_paid=True)
    """

    _CAMPOS_SNAPSHOT: list[str] = [
        "id",
        "reserva_id",
        "reserva_group_id",
        "amount",
        "description",
        "is_paid",
        "is_refunded",
        "linked_asset_id",
    ]

    # ------------------------------------------------------------------
    # Queries (sem mutação)
    # ------------------------------------------------------------------

    @classmethod
    def is_asset_under_deposit(cls, asset_id: int) -> bool:
        """Verifica se um ativo possui caução ativa paga e ainda não devolvida.

        Retorna True quando existe pelo menos uma SecurityDeposit com is_paid=True,
        is_refunded=False e deleted_at IS NULL vinculada ao ativo.

        Args:
            asset_id: ID do PhysicalAsset a verificar.

        Returns:
            True se o ativo tiver caução ativa não-devolvida; False caso contrário.
        """
        return db.session.query(
            db.session.query(SecurityDeposit)
            .filter(
                SecurityDeposit.linked_asset_id == asset_id,
                SecurityDeposit.is_paid.is_(True),
                SecurityDeposit.is_refunded.is_(False),
                SecurityDeposit.deleted_at.is_(None),
            )
            .exists()
        ).scalar()

    @classmethod
    def listar_por_reserva(cls, reserva_id: int) -> list[SecurityDeposit]:
        """Retorna todas as cauções ativas de uma reserva, ordenadas por criação.

        Args:
            reserva_id: ID da Reserva.

        Returns:
            Lista de SecurityDeposit com deleted_at IS NULL, ordem crescente de criação.
        """
        return (
            db.session.query(SecurityDeposit)
            .filter(
                SecurityDeposit.reserva_id == reserva_id,
                SecurityDeposit.deleted_at.is_(None),
            )
            .order_by(SecurityDeposit.criado_em.asc())
            .all()
        )

    @classmethod
    def pendente_total(cls, reserva_group_id: str) -> float:
        """Calcula o total de cauções não-pagas para o grupo de reservas.

        Considera todas as versões do grupo (mesmo group_id). Útil para calcular
        o pending_balance total incluindo cauções.

        Args:
            reserva_group_id: UUID do grupo de versões da Reserva.

        Returns:
            Soma (float) das cauções com is_paid=False e deleted_at IS NULL.
            Retorna 0.0 se não houver cauções pendentes.
        """
        row = (
            db.session.query(func.sum(SecurityDeposit.amount).label("total"))
            .filter(
                SecurityDeposit.reserva_group_id == reserva_group_id,
                SecurityDeposit.is_paid.is_(False),
                SecurityDeposit.is_refunded.is_(False),
                SecurityDeposit.deleted_at.is_(None),
            )
            .first()
        )
        return float(row.total or 0)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    @classmethod
    def criar(
        cls,
        reserva_id: int,
        amount: float,
        description: str | None = None,
        linked_asset_id: int | None = None,
        ator_id: int | None = None,
    ) -> SecurityDeposit:
        """Cria uma nova caução vinculada a uma reserva.

        A caução é criada com is_paid=False e is_refunded=False. O ator deve
        chamar pagar() quando o cliente entregar o valor.

        Args:
            reserva_id: ID da Reserva à qual a caução pertence.
            amount: Valor da caução em Reais. Deve ser positivo.
            description: Descrição livre (ex: 'Caução Churrasqueira A1'). Opcional.
            linked_asset_id: ID do PhysicalAsset vinculado. Opcional.
            ator_id: ID do Funcionario que está criando a caução.

        Returns:
            Instância de SecurityDeposit persistida.

        Raises:
            ValueError: Se a reserva não for encontrada ou já estiver deletada.
            ValueError: Se amount não for positivo.
            ValueError: Se linked_asset_id referenciar ativo inexistente ou deletado.
        """
        reserva = (
            db.session.query(Reserva)
            .filter(Reserva.id == reserva_id, Reserva.deleted_at.is_(None))
            .first()
        )
        if not reserva:
            raise ValueError(f"Reserva {reserva_id} não encontrada.")

        if float(amount) <= 0:
            raise ValueError("O valor da caução deve ser positivo.")

        if linked_asset_id is not None:
            asset = (
                db.session.query(PhysicalAsset)
                .filter(
                    PhysicalAsset.id == linked_asset_id,
                    PhysicalAsset.deleted_at.is_(None),
                )
                .first()
            )
            if not asset:
                raise ValueError(f"Ativo físico {linked_asset_id} não encontrado.")

        deposit = SecurityDeposit(
            reserva_id=reserva_id,
            reserva_group_id=reserva.group_id,
            amount=amount,
            description=description,
            is_paid=False,
            is_refunded=False,
            linked_asset_id=linked_asset_id,
        )
        db.session.add(deposit)
        db.session.flush()

        AuditService.log_create(
            "reservas",
            entity_id=deposit.id,
            new_state=cls._snapshot(deposit, cls._CAMPOS_SNAPSHOT),
            actor_id=ator_id,
        )
        db.session.commit()
        return deposit

    @classmethod
    def pagar(cls, deposit_id: int, ator_id: int | None = None) -> SecurityDeposit:
        """Registra o recebimento do valor de caução do cliente.

        Marca is_paid=True e, se houver ativo vinculado, atualiza
        PhysicalAsset.current_status → UNDER_DEPOSIT.

        Args:
            deposit_id: ID da SecurityDeposit a marcar como paga.
            ator_id: ID do Funcionario que está registrando o pagamento.

        Returns:
            Instância de SecurityDeposit atualizada.

        Raises:
            ValueError: Se a caução não for encontrada ou já estiver deletada.
            ValueError: Se a caução já tiver sido marcada como paga anteriormente.
        """
        deposit = (
            db.session.query(SecurityDeposit)
            .filter(
                SecurityDeposit.id == deposit_id,
                SecurityDeposit.deleted_at.is_(None),
            )
            .first()
        )
        if not deposit:
            raise ValueError(f"Caução {deposit_id} não encontrada.")
        if deposit.is_paid:
            raise ValueError(f"Caução {deposit_id} já foi registrada como paga.")

        snapshot_antes = cls._snapshot(deposit, cls._CAMPOS_SNAPSHOT)
        deposit.is_paid = True

        if deposit.linked_asset_id:
            asset = db.session.get(PhysicalAsset, deposit.linked_asset_id)
            if asset and asset.deleted_at is None:
                asset.current_status = AssetStatus.UNDER_DEPOSIT

        AuditService.log_update(
            "reservas",
            entity_id=deposit.id,
            previous_state=snapshot_antes,
            new_state=cls._snapshot(deposit, cls._CAMPOS_SNAPSHOT),
            actor_id=ator_id,
        )
        db.session.commit()
        return deposit

    @classmethod
    def devolver(cls, deposit_id: int, ator_id: int | None = None) -> SecurityDeposit:
        """Registra a devolução da caução ao cliente após retorno do ativo.

        Marca is_refunded=True e, se houver ativo vinculado, atualiza
        PhysicalAsset.current_status → AVAILABLE.

        Args:
            deposit_id: ID da SecurityDeposit a marcar como devolvida.
            ator_id: ID do Funcionario que está registrando a devolução.

        Returns:
            Instância de SecurityDeposit atualizada.

        Raises:
            ValueError: Se a caução não for encontrada ou já estiver deletada.
            ValueError: Se a caução ainda não tiver sido paga (não faz sentido devolver).
            ValueError: Se a caução já tiver sido devolvida anteriormente.
        """
        deposit = (
            db.session.query(SecurityDeposit)
            .filter(
                SecurityDeposit.id == deposit_id,
                SecurityDeposit.deleted_at.is_(None),
            )
            .first()
        )
        if not deposit:
            raise ValueError(f"Caução {deposit_id} não encontrada.")
        if not deposit.is_paid:
            raise ValueError(
                f"Caução {deposit_id} ainda não foi paga — não é possível devolver."
            )
        if deposit.is_refunded:
            raise ValueError(f"Caução {deposit_id} já foi devolvida anteriormente.")

        snapshot_antes = cls._snapshot(deposit, cls._CAMPOS_SNAPSHOT)
        deposit.is_refunded = True

        if deposit.linked_asset_id:
            asset = db.session.get(PhysicalAsset, deposit.linked_asset_id)
            if asset and asset.deleted_at is None:
                asset.current_status = AssetStatus.AVAILABLE

        AuditService.log_update(
            "reservas",
            entity_id=deposit.id,
            previous_state=snapshot_antes,
            new_state=cls._snapshot(deposit, cls._CAMPOS_SNAPSHOT),
            actor_id=ator_id,
        )
        db.session.commit()
        return deposit

    @classmethod
    def deletar(cls, deposit_id: int, ator_id: int | None = None) -> None:
        """Remove logicamente uma caução ainda não paga.

        Apenas cauções com is_paid=False podem ser deletadas. Se o pagamento
        já foi registrado, a caução deve ser devolvida via devolver().

        Args:
            deposit_id: ID da SecurityDeposit a deletar.
            ator_id: ID do Funcionario que está deletando.

        Raises:
            ValueError: Se a caução não for encontrada.
            ValueError: Se a caução já tiver sido paga (use devolver() em vez disso).
        """
        deposit = (
            db.session.query(SecurityDeposit)
            .filter(
                SecurityDeposit.id == deposit_id,
                SecurityDeposit.deleted_at.is_(None),
            )
            .first()
        )
        if not deposit:
            raise ValueError(f"Caução {deposit_id} não encontrada.")
        if deposit.is_paid:
            raise ValueError(
                f"Caução {deposit_id} já foi paga. "
                "Para encerrar, use devolver() após o retorno do ativo."
            )

        snapshot_antes = cls._snapshot(deposit, cls._CAMPOS_SNAPSHOT)
        deposit.deleted_at = datetime.now(timezone.utc)

        AuditService.log_delete(
            "reservas",
            entity_id=deposit_id,
            previous_state=snapshot_antes,
            actor_id=ator_id,
        )
        db.session.commit()
