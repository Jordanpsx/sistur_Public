#!/usr/bin/env python3
# Copyright (c) 2026 Jordan Barbosa Machado — All Rights Reserved

"""Harness de concorrência do PDV — prova o lock FOR UPDATE multi-canal.

Dispara threads simultâneas criando reservas para o MESMO recurso físico
de estoque finito pelos três canais (PDV/BALCAO, WEB/WordPress, BOT) e
verifica que o lock pessimista de ReservaService._verificar_estoque()
impede overbooking independente do canal.

⚠️  Requer Postgres real (FOR UPDATE não serializa em SQLite). O script
cria um banco DESCARTÁVEL (sistur_concurrency) no mesmo servidor Postgres
de staging, roda os cenários e derruba o banco no final. Nunca toca no
sistur_db.

Uso (dentro do container flask de staging):
    docker exec -w /app -e PYTHONPATH=/app sistur-teste-sistur-flask-1 \
        python tests/harness/run_concurrency_pdv.py

Sai com código 0 se todos os cenários passarem; 1 caso contrário.
"""

from __future__ import annotations

import os
import sys
import threading
import uuid
from dataclasses import dataclass, field

# ── Config do banco descartável ───────────────────────────────────────────
# Sem credencial no código: usa CONCURRENCY_ADMIN_DB_URL ou, na falta, o DATABASE_URL
# que o container do Sistur já tem.
ADMIN_DB_URL = os.environ.get("CONCURRENCY_ADMIN_DB_URL") or os.environ.get("DATABASE_URL", "")
if not ADMIN_DB_URL.startswith("postgresql"):
    sys.exit("Defina CONCURRENCY_ADMIN_DB_URL (ou DATABASE_URL) apontando para um Postgres.")
SCRATCH_DB_NAME = "sistur_concurrency"
SCRATCH_DB_URL = ADMIN_DB_URL.rsplit("/", 1)[0] + "/" + SCRATCH_DB_NAME

GREEN, RED, BOLD, RESET = "\033[92m", "\033[91m", "\033[1m", "\033[0m"


def _admin_exec(sql: str) -> None:
    """Executa DDL com AUTOCOMMIT no banco administrativo."""
    from sqlalchemy import create_engine, text

    engine = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text(sql))
    engine.dispose()


def _criar_banco_descartavel() -> None:
    _admin_exec(f"DROP DATABASE IF EXISTS {SCRATCH_DB_NAME} WITH (FORCE)")
    _admin_exec(f"CREATE DATABASE {SCRATCH_DB_NAME}")


def _dropar_banco_descartavel() -> None:
    _admin_exec(f"DROP DATABASE IF EXISTS {SCRATCH_DB_NAME} WITH (FORCE)")


# ── Resultado por thread ──────────────────────────────────────────────────
@dataclass
class Resultado:
    sucessos: list = field(default_factory=list)
    overbookings: int = 0
    erros: list = field(default_factory=list)
    lock = threading.Lock()

    def sucesso(self, canal: str, reserva_id: int) -> None:
        with self.lock:
            self.sucessos.append((canal, reserva_id))

    def overbooking(self) -> None:
        with self.lock:
            self.overbookings += 1

    def erro(self, canal: str, exc: Exception) -> None:
        with self.lock:
            self.erros.append((canal, f"{type(exc).__name__}: {exc}"))


def _montar_app():
    """Cria o app Flask apontando para o banco descartável."""
    os.environ["DATABASE_URL"] = SCRATCH_DB_URL
    from app import create_app

    return create_app("development")


def _seed(app, stock: int):
    """Recria o schema do zero e semeia venue/categoria/item/recurso + turno."""
    from app.extensions import db
    from app.models.funcionario import Funcionario
    from app.models.reservas import (
        _STATUS_CONFIG_DEFAULTS,
        ReservaCategory,
        ReservaGroup,
        ReservaItem,
        ReservaResource,
        ReservaSource,
        ReservaStatusConfig,
    )
    from app.services.pdv_service import PdvService

    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        # O seed do create_app rodou antes das tabelas existirem — repõe aqui.
        if db.session.query(ReservaStatusConfig).count() == 0:
            for cfg in _STATUS_CONFIG_DEFAULTS:
                db.session.add(ReservaStatusConfig(**cfg))
            db.session.commit()

        operador = Funcionario(nome="Operador Concorrência", cpf="52998224725", ativo=True)
        source = ReservaSource(name="Cachoeira")
        db.session.add_all([operador, source])
        db.session.flush()

        cat = ReservaCategory(name="Day Use", source_id=source.id)
        grupo = ReservaGroup(name="Acessos", sources=[source])
        db.session.add_all([cat, grupo])
        db.session.flush()

        recurso = ReservaResource(
            name="Capacidade Day Use", stock_quantity=stock,
            source_id=source.id, group_id=grupo.id,
        )
        item = ReservaItem(
            name="Day Use Inteira", billing_type="FIXED", price=60.00,
            requires_deposit=False, category_id=cat.id, group_id=grupo.id,
        )
        db.session.add_all([recurso, item])
        db.session.commit()

        shift = PdvService.abrir_turno(
            source_id=source.id, operador_id=operador.id,
            opening_float_amount=0.0, ator_id=operador.id,
        )
        return {
            "source_id": source.id,
            "item_id": item.id,
            "resource_id": recurso.id,
            "shift_id": shift.id,
            "operador_id": operador.id,
        }


def _thread_pdv(app, ids, barreira, res: Resultado, n: int):
    """Canal PDV: venda CASH via PdvService (nasce PAID)."""
    from app.services.pdv_service import PdvService
    from app.services.reserva_service import OverbookingError

    with app.app_context():
        barreira.wait()
        try:
            r = PdvService.registrar_venda(
                client_sale_uuid=str(uuid.uuid4()),
                shift_id=ids["shift_id"],
                source_name="Cachoeira",
                category_name="Day Use",
                items=[{"item_id": ids["item_id"], "quantity": 1,
                        "resource_id": ids["resource_id"]}],
                payment_method="CASH",
                customer_name=f"PDV {n}",
                amount_tendered=100.0,
                ator_id=ids["operador_id"],
            )
            res.sucesso("PDV", r["reserva_id"])
        except OverbookingError:
            res.overbooking()
        except Exception as exc:  # noqa: BLE001 — harness reporta tudo
            res.erro("PDV", exc)


def _thread_canal_web_ou_bot(app, ids, barreira, res: Resultado, canal: str, n: int):
    """Canais WEB (WordPress) e BOT: ReservaService.criar direto (PENDING bloqueante)."""
    from datetime import datetime, timezone

    from app.services.reserva_service import OverbookingError, ReservaService

    with app.app_context():
        barreira.wait()
        agora = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            r = ReservaService.criar(
                source_name="Cachoeira",
                category_name="Day Use",
                customer_name=f"{canal} {n}",
                customer_document=None,
                origin=canal if canal == "BOT" else "WEB",
                check_in_date=agora,
                check_out_date=None,
                items_payload=[{"item_id": ids["item_id"], "quantity": 1,
                                "resource_id": ids["resource_id"],
                                "price_override": 60.00}],
                ator_id=None,
            )
            res.sucesso(canal, r.id)
        except OverbookingError:
            res.overbooking()
        except Exception as exc:  # noqa: BLE001
            res.erro(canal, exc)


def _consumo_real(app, ids) -> int:
    """Soma ReservaItemLink.quantity de reservas bloqueantes no recurso."""
    from app.extensions import db
    from app.models.reservas import Reserva, ReservaItemLink, ReservaStatusConfig

    with app.app_context():
        bloqueantes = [
            c.internal_name
            for c in db.session.query(ReservaStatusConfig).filter_by(blocks_resource=True)
        ]
        total = 0
        links = db.session.query(ReservaItemLink).filter(
            ReservaItemLink.resource_id == ids["resource_id"],
            ReservaItemLink.deleted_at.is_(None),
        ).all()
        for link in links:
            reserva = db.session.get(Reserva, link.reserva_id)
            status = reserva.status.value if hasattr(reserva.status, "value") else str(reserva.status)
            if reserva and reserva.deleted_at is None and status in bloqueantes:
                total += link.quantity
        return total


def _cenario(app, nome: str, stock: int, n_pdv: int, n_web: int, n_bot: int) -> bool:
    """Roda um cenário completo em schema recriado. True = passou."""
    total_threads = n_pdv + n_web + n_bot
    print(f"\n{BOLD}── Cenário: {nome} — stock={stock}, "
          f"{n_pdv} PDV + {n_web} WEB + {n_bot} BOT simultâneos ──{RESET}")

    ids = _seed(app, stock)

    res = Resultado()
    barreira = threading.Barrier(total_threads)
    threads = (
        [threading.Thread(target=_thread_pdv, args=(app, ids, barreira, res, i))
         for i in range(n_pdv)]
        + [threading.Thread(target=_thread_canal_web_ou_bot,
                            args=(app, ids, barreira, res, "WEB", i))
           for i in range(n_web)]
        + [threading.Thread(target=_thread_canal_web_ou_bot,
                            args=(app, ids, barreira, res, "BOT", i))
           for i in range(n_bot)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    consumo = _consumo_real(app, ids)
    por_canal: dict[str, int] = {}
    for canal, _ in res.sucessos:
        por_canal[canal] = por_canal.get(canal, 0) + 1

    print(f"   Sucessos: {len(res.sucessos)} {por_canal} · "
          f"Overbooking rejeitados: {res.overbookings} · Erros: {len(res.erros)}")
    for canal, msg in res.erros:
        print(f"   {RED}ERRO [{canal}]{RESET} {msg}")
    print(f"   Consumo real no recurso (reservas bloqueantes): {consumo}/{stock}")

    ok = (
        len(res.sucessos) == stock
        and res.overbookings == total_threads - stock
        and not res.erros
        and consumo == stock
    )
    print(f"   {GREEN}✓ PASSOU{RESET}" if ok else f"   {RED}✗ FALHOU{RESET}"
          f" — esperado {stock} sucesso(s), {total_threads - stock} rejeição(ões), consumo == stock")
    return ok


def main() -> int:
    print(f"{BOLD}PDV Concurrency Harness — Postgres FOR UPDATE multi-canal{RESET}")
    print(f"Banco descartável: {SCRATCH_DB_NAME} (mesmo servidor do staging)")
    try:
        # App criado UMA vez (segundo create_app no mesmo processo re-inicializa
        # o APScheduler e quebra); entre cenários o schema é recriado no _seed.
        _criar_banco_descartavel()
        app = _montar_app()
        resultados = [
            _cenario(app, "última vaga disputada", stock=1, n_pdv=3, n_web=3, n_bot=2),
            _cenario(app, "pool pequeno multi-canal", stock=3, n_pdv=4, n_web=4, n_bot=4),
        ]
    finally:
        try:
            _dropar_banco_descartavel()
            print(f"\nBanco descartável {SCRATCH_DB_NAME} removido.")
        except Exception as exc:  # noqa: BLE001
            print(f"{RED}Aviso: falha ao dropar {SCRATCH_DB_NAME}: {exc}{RESET}")

    if all(resultados):
        print(f"\n{GREEN}{BOLD}TODOS OS CENÁRIOS PASSARAM{RESET} — o lock FOR UPDATE "
              "impediu overbooking em disputa simultânea PDV × WEB × BOT.")
        return 0
    print(f"\n{RED}{BOLD}HÁ CENÁRIO FALHANDO{RESET} — investigar antes de promover.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
