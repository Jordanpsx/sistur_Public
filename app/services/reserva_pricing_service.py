# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""
Serviço de Precificação para Reservas — cálculos de carrinho sem persistência.

Responsabilidade única: calcular totais de reservas com base em:
  1. Base: preço × quantidade × dias/horas (conforme billing_type)
  2. Descontos dinâmicos: early bird, promoções, etc. (via ReservaConfig)
  3. Taxas: taxa de serviço, taxa de processamento (via ReservaConfig)

Resultado: breakdown detalhado sem salvar nada no banco (read-only).

Billing types suportados:
  FIXED                 — preço fixo por quantidade (sem multiplicador temporal)
  PER_DAY               — diária × dias; cada dia tarifado individualmente (tiered)
  PER_HOUR              — por hora × horas totais
  DAILY_PRORATED_HOURLY — 1ª diária inteira + pró-rata horário para horas excedentes
                          (modalidade camping / estadas com horário variável)
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Union

from app.extensions import db
from app.models.reservas import (
    AdvanceDiscountRule,
    AdvanceDiscountStayTier,
    CalendarDiscount,
    CalendarDiscountAdvanceTier,
    ReservaItem,
    ReservaConfig,
    BillingType,
)
from app.services.calendar_service import CalendarService
from sqlalchemy import or_


class ReservaPricingService:
    """Serviço de precificação — cálculos de carrinho puro, sem side effects."""

    @staticmethod
    def calculate(
        source_id: int,
        check_in_date: Union[date, datetime],
        check_out_date: Union[date, datetime],
        items: list[dict[str, Any]],
        category_id: int | None = None,
        manual_discount_percent: float = 0.0,
    ) -> dict[str, Any]:
        """Calcula total da reserva com descontos e taxas.

        Aceita tanto `date` quanto `datetime` para check_in_date / check_out_date.
        Quando `datetime` é fornecido, a precisão de hora é usada pelo billing type
        DAILY_PRORATED_HOURLY. Os demais tipos (FIXED, PER_DAY, PER_HOUR) continuam
        operando normalmente com a parte de data extraída do datetime.

        Args:
            source_id:      ID da ReservaSource (venue).
            check_in_date:  Data (ou datetime) de entrada.
            check_out_date: Data (ou datetime) de saída.
            items:          Lista de dicts com {'item_id': int, 'quantity': int}.
            category_id:    ID da ReservaCategory, opcional. Quando fornecido, regras
                            de desconto específicas da categoria têm prioridade sobre
                            regras da fonte. None = considera apenas regras da fonte.
            manual_discount_percent: Percentual de desconto manual aplicado pelo operador
                            (ex: 10.0 = 10%). Aplicado sobre o subtotal e somado aos
                            descontos automáticos. Limitado ao subtotal (resultado ≥ 0).
                            Usar percentual garante que o desconto seja proporcional ao
                            subtotal correto após adição ou remoção de itens.

        Returns:
            Dict com:
                - items_breakdown: lista detalhada de items com preço calculado
                - subtotal: soma dos items
                - discount_amount: desconto total aplicado
                - discount_details: dict explicando cada desconto aplicado
                - service_fee: taxa de serviço calculada
                - service_fee_details: dict com percent e amount
                - total: subtotal - desconto + taxa
                - original_price: alias de subtotal (conveniência para frontend)
                - discount_percentage: percentual de desconto antecipado aplicado
                - discount_value: alias de discount_amount (conveniência para frontend)
                - final_price: alias de total (conveniência para frontend)

        Raises:
            ValueError: Se item_id não existir, datas inválidas, billing_type desconhecido.
        """
        # ------------------------------------------------------------------ #
        # 0. Normalização de entrada — datetime interno unificado             #
        # ------------------------------------------------------------------ #
        # Converte date → datetime(date, 00:00:00) para cálculos de hora.
        # Datetime passado diretamente é usado com precisão total.
        _ci: datetime = (
            check_in_date
            if isinstance(check_in_date, datetime)
            else datetime.combine(check_in_date, time.min)
        )
        _co: datetime = (
            check_out_date
            if isinstance(check_out_date, datetime)
            else datetime.combine(check_out_date, time.min)
        )

        # Versões date puras para iteração de diárias e carregamento de feriados
        ci_date: date = _ci.date()
        co_date: date = _co.date()

        # ------------------------------------------------------------------ #
        # 1. Validações básicas                                               #
        # ------------------------------------------------------------------ #
        if not items:
            raise ValueError("Items list cannot be empty")

        if _ci > _co:
            raise ValueError("check_in_date não pode ser posterior a check_out_date.")

        # ------------------------------------------------------------------ #
        # 2. Pré-carrega feriados do range (uma única query)                  #
        # ------------------------------------------------------------------ #
        # Garante ao menos 1 dia de range para cobrir reservas de dia único
        # (FIXED/check-in == check-out). Sem isso, a query usa < check_out e
        # o loop while current < co_date não executa, retornando set vazio.
        _holiday_end = co_date if co_date > ci_date else ci_date + timedelta(days=1)
        holiday_set = ReservaPricingService._load_holidays(ci_date, _holiday_end)

        # ------------------------------------------------------------------ #
        # 3. Calcular base de cada item                                       #
        # ------------------------------------------------------------------ #
        items_breakdown: list[dict[str, Any]] = []
        subtotal = Decimal("0.00")

        for item_spec in items:
            item_id = item_spec.get("item_id")
            quantity = item_spec.get("quantity", 1)

            if not item_id:
                raise ValueError("Each item must have item_id")

            if quantity <= 0:
                raise ValueError(f"Quantity must be > 0 for item {item_id}")

            item = db.session.query(ReservaItem).filter_by(
                id=item_id, deleted_at=None
            ).first()

            if not item:
                raise ValueError(f"ReservaItem {item_id} not found or deleted")

            if item.category.source_id != source_id:
                raise ValueError(
                    f"Item {item_id} belongs to different source "
                    f"(expected {source_id}, got {item.category.source_id})"
                )

            # -------------------------------------------------------------- #
            # FIXED — preço fixo; tiered pricing por data de check-in         #
            # -------------------------------------------------------------- #
            if item.billing_type == BillingType.FIXED:
                item_price = ReservaPricingService._price_for_day(
                    ci_date, item, holiday_set
                )
                item_total = item_price * Decimal(quantity)
                items_breakdown.append({
                    "item_id": item_id,
                    "item_name": item.name,
                    "quantity": quantity,
                    "unit_price": float(item_price),
                    "billing_type": item.billing_type.value,
                    "num_days": None,
                    "item_total": float(item_total),
                })

            # -------------------------------------------------------------- #
            # PER_DAY — diária; cada dia tarifado individualmente (tiered)    #
            # -------------------------------------------------------------- #
            elif item.billing_type == BillingType.PER_DAY:
                daily_breakdown: dict[str, int] = {
                    "holiday_days": 0,
                    "weekend_days": 0,
                    "weekday_days": 0,
                }
                item_total = Decimal("0.00")
                current_day = ci_date
                while current_day < co_date:
                    day_price = ReservaPricingService._price_for_day(
                        current_day, item, holiday_set
                    )
                    item_total += day_price * Decimal(quantity)

                    if current_day in holiday_set:
                        daily_breakdown["holiday_days"] += 1
                    elif current_day.weekday() in (5, 6):
                        daily_breakdown["weekend_days"] += 1
                    else:
                        daily_breakdown["weekday_days"] += 1

                    current_day += timedelta(days=1)

                num_days = (co_date - ci_date).days
                items_breakdown.append({
                    "item_id": item_id,
                    "item_name": item.name,
                    "quantity": quantity,
                    "unit_price": None,  # varia por dia — ver daily_breakdown
                    "billing_type": item.billing_type.value,
                    "num_days": num_days,
                    "daily_breakdown": daily_breakdown,
                    "item_total": float(item_total),
                })

            # -------------------------------------------------------------- #
            # PER_HOUR — por hora; usa precisão de datetime quando disponível  #
            # -------------------------------------------------------------- #
            elif item.billing_type == BillingType.PER_HOUR:
                item_price = Decimal(str(item.price))
                num_hours = (_co - _ci).total_seconds() / 3600
                item_total = item_price * Decimal(quantity) * Decimal(str(num_hours))
                items_breakdown.append({
                    "item_id": item_id,
                    "item_name": item.name,
                    "quantity": quantity,
                    "unit_price": float(item_price),
                    "billing_type": item.billing_type.value,
                    "num_days": None,
                    "total_hours": round(num_hours, 4),
                    "item_total": float(item_total),
                })

            # -------------------------------------------------------------- #
            # DAILY_PRORATED_HOURLY — diária + pró-rata horário               #
            #                                                                  #
            # Lógica:                                                          #
            #   - Cobranças mínimas: 1 diária completa (primeiras 24h).        #
            #   - Horas excedentes: (tarifa / 24) por hora adicional.          #
            #   - Fórmula: se horas > 24 →                                    #
            #       total = tarifa + ((horas - 24) × (tarifa / 24))           #
            #   - A tarifa diária usa tiered pricing do dia de check-in        #
            #     (feriado > fim-de-semana > dia-útil).                        #
            #   - Requer datetime com hora para precisão sub-diária;           #
            #     com date puro, total_hours = dias × 24 (diárias inteiras).   #
            # ---------------------------------------------------------------- #
            elif item.billing_type == BillingType.DAILY_PRORATED_HOURLY:
                # Tarifa aplicável ao dia de check-in (tiered pricing)
                daily_rate = ReservaPricingService._price_for_day(
                    ci_date, item, holiday_set
                )

                # Duração total em horas (precisão de datetime)
                total_seconds = (_co - _ci).total_seconds()
                total_hours = total_seconds / 3600

                # Cálculo do total com pró-rata horário
                if total_hours <= 24:
                    # Dentro da primeira diária — cobra tarifa cheia × quantidade
                    unit_total = daily_rate
                else:
                    # Horas excedentes após a primeira diária completa
                    hourly_rate = daily_rate / Decimal("24")
                    extra_hours = Decimal(str(total_hours - 24))
                    unit_total = daily_rate + (extra_hours * hourly_rate)

                # Arredonda para 2 casas decimais (moeda) e aplica quantidade
                unit_total = unit_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                item_total = unit_total * Decimal(quantity)

                items_breakdown.append({
                    "item_id": item_id,
                    "item_name": item.name,
                    "quantity": quantity,
                    "unit_price": float(daily_rate),
                    "billing_type": item.billing_type.value,
                    "num_days": None,
                    "total_hours": round(total_hours, 4),
                    "unit_total": float(unit_total),
                    "item_total": float(item_total),
                })

            else:
                raise ValueError(f"Unknown billing_type: {item.billing_type}")

            subtotal += item_total

        # ------------------------------------------------------------------ #
        # 4. Descontos dinâmicos                                              #
        # ------------------------------------------------------------------ #
        discount_amount = Decimal("0.00")
        discount_details: dict[str, Any] = {}
        advance_discount_pct = 0.0

        # 4a. Desconto por data específica (CalendarDiscount) tem prioridade máxima.
        #     Se existir e o cliente qualificar para algum tier de antecedência,
        #     substitui completamente o Early Bird (não acumula).
        stay_days = (co_date - ci_date).days
        calendar_discount = ReservaPricingService._find_calendar_discount(
            source_id=source_id,
            category_id=category_id,
            check_in_date=ci_date,
            stay_days=stay_days,
        )
        if calendar_discount:
            pct = Decimal(str(calendar_discount["percent"]))
            ad_amount = (subtotal * pct / Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            discount_amount += ad_amount
            advance_discount_pct = float(pct)
            discount_details["advance_booking"] = {
                **calendar_discount,
                "amount": float(ad_amount),
            }
        else:
            # 4b. Desconto por reserva antecipada (AdvanceDiscountRule DB-backed)
            advance_discount = ReservaPricingService._find_best_advance_discount(
                source_id=source_id,
                category_id=category_id,
                check_in_date=ci_date,
                stay_days=stay_days,
            )
            if advance_discount:
                pct = Decimal(str(advance_discount["percent"]))
                ad_amount = (subtotal * pct / Decimal("100")).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                discount_amount += ad_amount
                advance_discount_pct = float(pct)
                discount_details["advance_booking"] = {
                    **advance_discount,
                    "amount": float(ad_amount),
                }

        # 4b. Desconto fixo legado (pricing_rules JSON — mantido para compatibilidade)
        pricing_rules = ReservaPricingService._get_config_value(
            source_id, "pricing_rules", {}
        )
        if pricing_rules.get("flat_discount_percent"):
            flat_discount = subtotal * Decimal(
                str(pricing_rules["flat_discount_percent"]) or "0"
            ) / Decimal("100")
            if flat_discount > 0:
                discount_amount += flat_discount
                discount_details["flat_discount"] = {
                    "percent": pricing_rules["flat_discount_percent"],
                    "amount": float(flat_discount),
                }

        # 4c. Desconto manual do operador (percentual sobre subtotal)
        # Uso de percentual garante proporcionalidade: ao adicionar novos itens
        # o desconto cresce junto, preservando o benefício acordado com o cliente.
        if manual_discount_percent and manual_discount_percent > 0:
            manual_pct = Decimal(str(manual_discount_percent))
            manual_d = (subtotal * manual_pct / Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            discount_amount += manual_d
            discount_details["manual_discount"] = {
                "percent": float(manual_pct),
                "amount": float(manual_d),
            }

        discount_amount = min(discount_amount, subtotal)

        # ------------------------------------------------------------------ #
        # 5. Taxas de serviço                                                 #
        # ------------------------------------------------------------------ #
        subtotal_after_discount = subtotal - discount_amount
        service_fee = Decimal("0.00")
        service_fee_details = None

        service_fee_percent = ReservaPricingService._get_config_value(
            source_id, "service_fee_percent", 0
        )
        if service_fee_percent:
            service_fee = subtotal_after_discount * Decimal(
                str(service_fee_percent)
            ) / Decimal("100")
            service_fee_details = {
                "percent": service_fee_percent,
                "amount": float(service_fee),
            }

        # ------------------------------------------------------------------ #
        # 6. Total final                                                       #
        # ------------------------------------------------------------------ #
        total = subtotal_after_discount + service_fee

        return {
            "items_breakdown": items_breakdown,
            "subtotal": float(subtotal),
            "discount_amount": float(discount_amount),
            "discount_details": discount_details,
            "service_fee": float(service_fee),
            "service_fee_details": service_fee_details,
            "total": float(total),
            # Campos de conveniência para exibição no frontend
            "original_price": float(subtotal),
            "discount_percentage": advance_discount_pct,
            "discount_value": float(discount_amount),
            "final_price": float(total),
        }

    @staticmethod
    def _get_config_value(source_id: int, config_key: str, default: Any = None) -> Any:
        """Busca valor de config com fallback para configuração global (source_id=NULL).

        Args:
            source_id:  ID da fonte. Prioridade sobre config global.
            config_key: Chave de configuração a buscar.
            default:    Valor padrão quando a chave não existir em nenhuma camada.

        Returns:
            Valor da configuração ou `default` quando não encontrado.
        """
        config = db.session.query(ReservaConfig).filter_by(
            source_id=source_id, config_key=config_key
        ).first()

        if config:
            return config.config_value

        global_config = db.session.query(ReservaConfig).filter_by(
            source_id=None, config_key=config_key
        ).first()

        if global_config:
            return global_config.config_value

        return default

    @staticmethod
    def _load_holidays(check_in: date, check_out: date) -> set[date]:
        """Retorna as datas especiais (tarifa diferenciada) em [check_in, check_out).

        Delega ao `CalendarService`, obedecendo ao mandato do CLAUDE.md: toda
        consulta de datas especiais passa pelo Calendário Global — nunca por
        tabelas próprias de feriado por módulo.

        O gatilho é o flag `afeta_precificacao`, NÃO o tipo do evento: feriado
        nacional, feriado local e alta temporada valem igual desde que o flag
        esteja ligado. Eventos cadastrados só para fins de folha de ponto
        (`afeta_precificacao=False`) não afetam o cálculo de preços.

        O nome é mantido por compatibilidade com os chamadores existentes
        (`bot_api`), mas a semântica é "data especial", não só feriado.

        Args:
            check_in:  Primeira diária cobrada (inclusive).
            check_out: Último dia do booking (exclusive — checkout não é cobrado).

        Returns:
            Set de `date` com tarifa especial dentro do intervalo.
        """
        return CalendarService.datas_que_afetam_preco(check_in, check_out)

    @staticmethod
    def _price_for_day(day: date, item: ReservaItem, holiday_set: set[date]) -> Decimal:
        """Retorna o preço correto para um dia específico respeitando tiered pricing.

        Ordem de prioridade:
            1. Data especial → item.price_holiday  (prioridade máxima)
            2. Fim de semana (Sáb=5, Dom=6) → item.price_weekend
            3. Dia de semana (Seg–Sex, weekday 0–4) → item.price_weekday
        Cada nível faz fallback para item.price quando o campo for NULL.
        Sexta-feira (weekday=4) é dia útil — NÃO é fim de semana.

        "Data especial" é qualquer evento do Calendário Global com
        `afeta_precificacao=True` — feriado nacional/local ou alta temporada.
        A coluna `price_holiday` mantém o nome por compatibilidade de schema.

        Usado por FIXED (check-in como referência), PER_DAY (cada diária),
        e DAILY_PRORATED_HOURLY (check-in como referência da tarifa base).

        Args:
            day:         Data de referência para o lookup de tarifa.
            item:        ReservaItem com os campos de preço opcionais.
            holiday_set: Conjunto de datas especiais pré-carregado.

        Returns:
            Decimal com o preço aplicável para o dia.
        """
        base = Decimal(str(item.price))

        if day in holiday_set:
            return Decimal(str(item.price_holiday)) if item.price_holiday is not None else base

        if day.weekday() in (5, 6):  # 5=Sábado, 6=Domingo
            return Decimal(str(item.price_weekend)) if item.price_weekend is not None else base

        # Dias úteis: weekday 0 (Seg) a 4 (Sex)
        return Decimal(str(item.price_weekday)) if item.price_weekday is not None else base

    @classmethod
    def precos_do_dia(cls, itens: list[ReservaItem], dia: date) -> dict[int, Decimal]:
        """Retorna o preço aplicável a cada item em um único dia.

        Aplica a mesma regra de tiered pricing usada em calculate()
        (feriado > fim de semana > dia útil, com fallback para item.price),
        carregando os feriados numa única query para todos os itens.

        Serve para telas que precisam EXIBIR o preço de hoje — notadamente o
        catálogo do PDV — garantindo que o valor mostrado seja o mesmo que a
        venda vai cobrar.

        Args:
            itens: ReservaItem a precificar.
            dia:   Data de referência.

        Returns:
            Dict {item_id: preço aplicável no dia}.
        """
        holiday_set = cls._load_holidays(dia, dia + timedelta(days=1))
        return {
            item.id: cls._price_for_day(dia, item, holiday_set)
            for item in itens
        }

    @staticmethod
    def _find_best_advance_discount(
        source_id: int,
        category_id: int | None,
        check_in_date: date,
        stay_days: int = 0,
    ) -> dict | None:
        """Encontra a melhor regra de desconto por antecedência aplicável ao booking.

        Consulta a tabela AdvanceDiscountRule filtrando por source_id, is_active=True
        e min_days_in_advance <= dias_de_antecedência. Quando category_id é fornecido,
        considera tanto regras da categoria específica quanto regras da fonte (NULL),
        dando prioridade às regras específicas da categoria. Dentro do mesmo nível
        de especificidade, seleciona a regra com maior min_days_in_advance.

        Após selecionar a regra base, verifica se ela possui AdvanceDiscountStayTier
        cadastrados. Se existirem tiers e algum qualificar para stay_days, usa o
        percentual do tier com maior min_stay_days aplicável — substituindo o
        discount_percentage base da regra.

        Args:
            source_id:      ID da ReservaSource.
            category_id:    ID da ReservaCategory. None = considera só regras da fonte.
            check_in_date:  Data de check-in para calcular dias de antecedência.
            stay_days:      Número de dias de estadia (check_out - check_in). Usado para
                            selecionar o tier de desconto por estadia, quando definido.

        Returns:
            Dict com name, percent, days_in_advance, required_days, rule_id,
            stay_tier_id, stay_tier_min_days.
            None se nenhuma regra aplicável existir ou a reserva não for antecipada.
        """
        today = date.today()
        days_in_advance = (check_in_date - today).days

        if days_in_advance <= 0:
            return None

        base_q = (
            db.session.query(AdvanceDiscountRule)
            .filter(
                AdvanceDiscountRule.source_id == source_id,
                AdvanceDiscountRule.is_active.is_(True),
                AdvanceDiscountRule.min_days_in_advance <= days_in_advance,
            )
        )

        if category_id is not None:
            rules = base_q.filter(
                or_(
                    AdvanceDiscountRule.category_id == category_id,
                    AdvanceDiscountRule.category_id.is_(None),
                )
            ).all()
        else:
            rules = base_q.filter(
                AdvanceDiscountRule.category_id.is_(None)
            ).all()

        if not rules:
            return None

        # Regras específicas da categoria têm prioridade sobre regras da fonte.
        # Dentro do mesmo nível, escolhe a com maior min_days_in_advance (tier mais alto).
        category_rules = sorted(
            [r for r in rules if r.category_id is not None],
            key=lambda r: r.min_days_in_advance,
            reverse=True,
        )
        source_rules = sorted(
            [r for r in rules if r.category_id is None],
            key=lambda r: r.min_days_in_advance,
            reverse=True,
        )

        best = (category_rules or source_rules)[0]

        # Verifica tiers de estadia: substitui o percentual base se houver tier aplicável.
        effective_pct = float(best.discount_percentage)
        stay_tier_id = None
        stay_tier_min_days = None

        if best.stay_tiers and stay_days > 0:
            applicable = [t for t in best.stay_tiers if stay_days >= t.min_stay_days]
            if applicable:
                best_tier = max(applicable, key=lambda t: t.min_stay_days)
                effective_pct = float(best_tier.discount_percentage)
                stay_tier_id = best_tier.id
                stay_tier_min_days = best_tier.min_stay_days

        return {
            "name": "Reserva Antecipada",
            "percent": effective_pct,
            "days_in_advance": days_in_advance,
            "required_days": best.min_days_in_advance,
            "rule_id": best.id,
            "stay_tier_id": stay_tier_id,
            "stay_tier_min_days": stay_tier_min_days,
        }

    @staticmethod
    def _find_calendar_discount(
        source_id: int,
        category_id: int | None,
        check_in_date: date,
        stay_days: int = 0,
    ) -> dict | None:
        """Encontra o desconto de calendário para uma data específica de check-in.

        Consulta CalendarDiscount filtrando por source_id, is_active=True e
        discount_date == check_in_date. Regras específicas de categoria têm
        prioridade sobre regras de fonte (NULL).

        Cada tier é qualificado por dois critérios simultâneos:
          1. days_in_advance >= tier.min_days_in_advance
          2. tier.min_stay_days is None OR stay_days >= tier.min_stay_days

        O tier mais específico que qualifica em ambas as dimensões é aplicado:
        prioridade por (min_days_in_advance DESC, min_stay_days DESC, None=último).

        Args:
            source_id:      ID da ReservaSource.
            category_id:    ID da ReservaCategory. None = considera só descontos da fonte.
            check_in_date:  Data de check-in para busca exata.
            stay_days:      Número de noites da reserva (check_out - check_in). Padrão 0.

        Returns:
            Dict com name, percent, discount_date, calendar_discount_id,
            advance_tier_id, advance_tier_min_days, advance_tier_min_stay.
            None se não houver desconto de calendário aplicável.
        """
        from datetime import date as date_cls
        days_in_advance = (check_in_date - date_cls.today()).days

        base_q = db.session.query(CalendarDiscount).filter(
            CalendarDiscount.source_id == source_id,
            CalendarDiscount.is_active.is_(True),
            CalendarDiscount.discount_date == check_in_date,
        )

        if category_id is not None:
            candidates = base_q.filter(
                or_(
                    CalendarDiscount.category_id == category_id,
                    CalendarDiscount.category_id.is_(None),
                )
            ).all()
        else:
            candidates = base_q.filter(
                CalendarDiscount.category_id.is_(None)
            ).all()

        if not candidates:
            return None

        # Regra específica de categoria tem prioridade sobre regra de fonte.
        category_specific = [c for c in candidates if c.category_id is not None]
        best = category_specific[0] if category_specific else candidates[0]

        # Qualificação 2D: antecedência + estadia (min_stay_days=None = sem restrição de estadia)
        applicable = [
            t for t in best.advance_tiers
            if days_in_advance >= t.min_days_in_advance
            and (t.min_stay_days is None or stay_days >= t.min_stay_days)
        ]
        if not applicable:
            return None

        # Tier mais específico: maior antecedência exigida; entre empatados, maior estadia exigida.
        best_tier = max(
            applicable,
            key=lambda t: (
                t.min_days_in_advance,
                t.min_stay_days if t.min_stay_days is not None else -1,
            ),
        )

        return {
            "name": best.label or "Desconto de Calendário",
            "percent": float(best_tier.discount_percentage),
            "discount_date": check_in_date.isoformat(),
            "calendar_discount_id": best.id,
            "advance_tier_id": best_tier.id,
            "advance_tier_min_days": best_tier.min_days_in_advance,
            "advance_tier_min_stay": best_tier.min_stay_days,
        }
