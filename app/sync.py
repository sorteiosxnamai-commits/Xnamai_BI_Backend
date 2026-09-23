import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text

from app.adaptor import adaptor
from app.database import SessionLocal
from app.domain.order_status import VALID_SALE_STATUSES, status_sql_in
from app.models import (
    Carrier,
    Category,
    CommercialPolicy,
    Customer,
    CustomerSegment,
    Order,
    OrderItem,
    OrderType,
    PaymentCondition,
    PriceTable,
    Product,
    ProductPrice,
    Seller,
    SyncRun,
    SyncState,
)

log = logging.getLogger("uvicorn.error")
BR_TZ = ZoneInfo("America/Sao_Paulo")

MAX_PAGES = 5000
RESOURCE_PAUSE_SECONDS = 5
RATE_LIMIT_PAUSE_SECONDS = 180
MISSING_DETAIL_BATCH = 5
MISSING_DETAIL_MAX_BATCHES = 20
SYNC_LEASE_TTL = timedelta(minutes=15)
SYNC_COORDINATION_LOCK = "xnamai_sync_mercos"
_order_detail_blocked = False
DIMENSION_MODELS = {
    "categories": Category,
    "segments": CustomerSegment,
    "order-types": OrderType,
    "payment-conditions": PaymentCondition,
    "price-tables": PriceTable,
    "carriers": Carrier,
    "commercial-policies": CommercialPolicy,
}
CATALOG_RESOURCES = (
    "categories",
    "segments",
    "order-types",
    "payment-conditions",
    "price-tables",
    "carriers",
    "commercial-policies",
    "customers",
    "products",
    "product-prices",
    "users",
)
OPTIONAL_CATALOG_RESOURCES = {
    "categories",
    "segments",
    "order-types",
    "payment-conditions",
    "price-tables",
    "carriers",
    "commercial-policies",
    "product-prices",
}
# Pedidos alimentam os indicadores do BI e precisam ser atualizados antes dos
# cadastros auxiliares. Assim, um rate limit em uma dimensao opcional nao deixa
# a atualizacao comercial presa no fim de uma sincronizacao completa.
SYNC_RESOURCES = ("orders", *CATALOG_RESOURCES)


class OrderDetailBatchError(Exception):
    def __init__(self, failed: int, first_error: Exception):
        self.failed = failed
        self.first_error = first_error
        super().__init__(f"{failed} detalhe(s) de pedido falharam: {first_error}")


def dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BR_TZ)
    return parsed.astimezone(timezone.utc)


def f(value):
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, (int, float)):
        number = Decimal(str(value))
    else:
        text = str(value).strip()
        # BR: 1.234.567,89 → 1234567.89
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", ".")
        try:
            number = Decimal(text)
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"Valor numérico inválido recebido da fonte: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"Valor numérico não finito recebido da fonte: {value!r}")
    return number


def optional_decimal(row: dict, *keys: str) -> Decimal | None:
    for key in keys:
        if row.get(key) is not None and row.get(key) != "":
            return f(row[key])
    return None


def _order_line_items(row: dict) -> list | None:
    if "itens" in row:
        items = row.get("itens")
    elif "items" in row:
        items = row.get("items")
    else:
        return None
    return items if isinstance(items, list) else None


async def _hydrate_order_details(rows: list[dict]) -> list[dict]:
    """Fetch details when the v2 list payload omits the items key.

    Mercos allows GET-by-id only in sandbox. Production returns 401, so the
    list payload must be persisted even when the individual endpoint is blocked.
    """
    global _order_detail_blocked
    hydrated: list[dict] = []
    for row in rows:
        items = _order_line_items(row)
        if items is not None or _order_detail_blocked:
            hydrated.append(row)
            continue
        mercos_id = str(row.get("id") or "")
        if not mercos_id:
            raise ValueError("Pedido sem id no payload de listagem")
        try:
            detail = await adaptor.detail("orders", mercos_id)
        except HTTPException as exc:
            if exc.status_code in {401, 403, 404}:
                _order_detail_blocked = True
                log.warning(
                    "Detalhe do pedido %s indisponível (%s); seguindo com a listagem",
                    mercos_id,
                    exc.status_code,
                )
                hydrated.append(row)
                continue
            raise
        detail_id = str(detail.get("id") or mercos_id)
        if detail_id != mercos_id:
            raise ValueError(
                f"Detalhe do pedido {mercos_id} retornou id divergente {detail_id}"
            )
        hydrated.append({**row, **detail, "id": mercos_id})
    return hydrated


def _orders_missing_items(limit: int) -> list[str]:
    with SessionLocal() as db:
        candidates = db.scalars(
            select(Order)
            .where(
                func.coalesce(Order.item_count, 0) == 0,
                status_sql_in(Order.status, VALID_SALE_STATUSES),
            )
            .order_by(Order.issued_at.desc())
            .limit(max(limit * 5, limit))
        ).all()
        missing: list[str] = []
        for order in candidates:
            raw = order.raw if isinstance(order.raw, dict) else {}
            if "itens" in raw or "items" in raw:
                continue
            missing.append(str(order.mercos_id))
            if len(missing) >= limit:
                break
        return missing


def _persist_order_detail_rows(rows: list[dict]) -> int:
    with SessionLocal() as db:
        result = _upsert_rows(db, "orders", rows)
        db.commit()
        return int(result["persisted"])


async def _backfill_missing_order_details() -> int:
    if _order_detail_blocked:
        return 0
    repaired = 0
    for _ in range(MISSING_DETAIL_MAX_BATCHES):
        ids = await asyncio.to_thread(_orders_missing_items, MISSING_DETAIL_BATCH)
        if not ids:
            break
        try:
            rows = await _hydrate_order_details([{"id": mercos_id} for mercos_id in ids])
        except OrderDetailBatchError as exc:
            log.warning("Backfill de itens Mercos interrompido: %s", exc)
            break
        if not any(_order_line_items(row) for row in rows):
            log.warning("Backfill de itens Mercos sem detalhe disponível; interrompendo")
            break
        persisted = await asyncio.to_thread(_persist_order_detail_rows, rows)
        repaired += persisted
    if repaired:
        log.info("Backfill de itens Mercos: %s pedidos atualizados", repaired)
    return repaired


def _upsert_rows(db, resource: str, rows: list):
    persisted = 0
    items_persisted = 0
    for row in rows:
        mid = str(row.get("id"))
        if resource == "customers":
            obj = db.scalar(select(Customer).where(Customer.mercos_id == mid)) or Customer(mercos_id=mid)
            obj.name = row.get("nome") or row.get("razao_social") or "Sem nome"
            obj.document = row.get("cnpj") or row.get("cpf")
            obj.city = row.get("cidade")
            obj.state = row.get("estado")
            emails = row.get("emails") or [{}]
            first = emails[0] if emails else {}
            obj.email = first.get("email") if isinstance(first, dict) else None
            obj.phone = row.get("celular") or row.get("telefone")
            if "segmento_id" in row:
                obj.segment_mercos_id = str(row.get("segmento_id") or "") or None
            if "data_criacao" in row:
                obj.created_at_source = dt(row.get("data_criacao"))
            if "ativo" in row:
                obj.active = bool(row["ativo"])
            obj.source_updated_at = dt(row.get("ultima_alteracao"))
            obj.raw = row
            db.add(obj)
            persisted += 1
        elif resource == "products":
            obj = db.scalar(select(Product).where(Product.mercos_id == mid)) or Product(mercos_id=mid)
            obj.code = str(row.get("codigo") or "")
            obj.name = row.get("nome") or "Sem nome"
            obj.category_id = str(row.get("categoria_id") or "") or None
            obj.category_mercos_id = obj.category_id
            obj.unit = row.get("unidade")
            obj.list_price = f(row.get("preco_tabela"))
            if "preco_minimo" in row:
                obj.minimum_price = optional_decimal(row, "preco_minimo")
            obj.stock = f(row.get("saldo_estoque"))
            if "ultima_alteracao" in row:
                obj.source_updated_at = dt(row.get("ultima_alteracao"))
            if "data_criacao" in row:
                obj.created_at_source = dt(row.get("data_criacao"))
            obj.active = bool(row.get("ativo", True))
            obj.raw = row
            db.add(obj)
            persisted += 1
        elif resource == "users":
            obj = db.scalar(select(Seller).where(Seller.mercos_id == mid)) or Seller(mercos_id=mid)
            obj.name = row.get("nome") or row.get("email") or "Sem nome"
            obj.active = bool(row.get("ativo", True))
            obj.raw = row
            db.add(obj)
            persisted += 1
        elif resource == "orders":
            obj = db.scalar(select(Order).where(Order.mercos_id == mid)) or Order(
                mercos_id=mid, number=mid, status="unknown"
            )
            obj.number = str(row.get("numero") or mid)
            obj.customer_mercos_id = str(row.get("cliente_id") or "") or None
            obj.seller_mercos_id = (
                str(row.get("criador_id") or row.get("usuario_id") or row.get("vendedor_id") or "") or None
            )
            obj.status = str(row.get("status") if row.get("status") is not None else row.get("situacao") or "unknown")
            obj.issued_at = dt(row.get("data_emissao") or row.get("data_criacao") or row.get("ultima_alteracao"))
            obj.total = f(row.get("total"))
            obj.discount = f(row.get("desconto"))
            source_items = row.get("itens") or row.get("items") or []
            items = [
                item
                for item in source_items
                if str(item.get("excluido", False)).lower() not in {"true", "1"}
            ]
            derived_discount = Decimal("0")
            complete_item_prices = bool(items)
            for item in items:
                list_unit = optional_decimal(item, "preco_tabela")
                net_unit = optional_decimal(
                    item,
                    "preco_liquido",
                    "preco_unitario",
                    "preco",
                )
                if list_unit is None or net_unit is None:
                    complete_item_prices = False
                    continue
                derived_discount += max(
                    (list_unit - net_unit) * f(item.get("quantidade")),
                    Decimal("0"),
                )
            if "tipo_pedido_id" in row:
                obj.order_type_mercos_id = (
                    str(row.get("tipo_pedido_id") or "") or None
                )
            if "condicao_pagamento_id" in row:
                obj.payment_condition_mercos_id = (
                    str(row.get("condicao_pagamento_id") or "") or None
                )
            if "tabela_preco_id" in row:
                obj.price_table_mercos_id = (
                    str(row.get("tabela_preco_id") or "") or None
                )
            if "transportadora_id" in row:
                obj.carrier_mercos_id = (
                    str(row.get("transportadora_id") or "") or None
                )
            if "politica_comercial_id" in row:
                obj.commercial_policy_mercos_id = (
                    str(row.get("politica_comercial_id") or "") or None
                )
            explicit_gross = optional_decimal(row, "total_bruto", "valor_bruto")
            obj.net_total = optional_decimal(row, "total_liquido", "valor_liquido", "total")
            explicit_discount = optional_decimal(
                row,
                "valor_desconto",
                "desconto_valor",
            )
            obj.discount_value = (
                explicit_discount
                if explicit_discount is not None
                else derived_discount if complete_item_prices else None
            )
            obj.gross_total = (
                explicit_gross
                if explicit_gross is not None
                else (
                    obj.net_total + derived_discount
                    if complete_item_prices and obj.net_total is not None
                    else None
                )
            )
            explicit_discount_percent = optional_decimal(
                row,
                "desconto_percentual",
                "percentual_desconto",
            )
            obj.discount_percent = explicit_discount_percent
            if (
                explicit_discount_percent is None
                and obj.gross_total
                and obj.discount_value is not None
            ):
                obj.discount_percent = obj.discount_value / obj.gross_total * 100
            obj.item_count = len(items)
            obj.sku_count = len(
                {
                    str(item.get("produto_id"))
                    for item in items
                    if item.get("produto_id") is not None
                }
            )
            if "data_criacao" in row:
                obj.source_created_at = dt(row.get("data_criacao"))
            obj.source_updated_at = dt(row.get("ultima_alteracao"))
            obj.raw = row
            db.add(obj)
            db.flush()
            db.execute(delete(OrderItem).where(OrderItem.order_mercos_id == mid))
            for pos, item in enumerate(source_items):
                q = f(item.get("quantidade"))
                list_unit = optional_decimal(item, "preco_tabela")
                unit = optional_decimal(
                    item,
                    "preco_liquido",
                    "preco_unitario",
                    "preco",
                    "preco_tabela",
                ) or Decimal("0")
                total = f(item.get("subtotal") or item.get("total") or (q * unit))
                db.add(
                    OrderItem(
                        order_mercos_id=mid,
                        position=pos,
                        mercos_item_id=(
                            str(
                                item.get("id")
                                or item.get("item_id")
                                or item.get("pedido_item_id")
                                or ""
                            )
                            or None
                        ),
                        product_mercos_id=str(item.get("produto_id") or "") or None,
                        code=str(item.get("produto_codigo") or item.get("codigo") or ""),
                        name=item.get("produto_nome") or item.get("nome") or item.get("descricao") or "Produto",
                        quantity=q,
                        list_unit_price=list_unit,
                        unit_price=unit,
                        discount=f(item.get("desconto") or item.get("desconto_de_cupom")),
                        total=total,
                        excluded=(
                            str(item.get("excluido", False)).lower()
                            in {"true", "1"}
                        ),
                        raw=item,
                    )
                )
                items_persisted += 1
            persisted += 1
        elif resource in DIMENSION_MODELS:
            model = DIMENSION_MODELS[resource]
            obj = db.scalar(select(model).where(model.mercos_id == mid)) or model(
                mercos_id=mid
            )
            obj.name = row.get("nome") or row.get("descricao") or "Sem nome"
            if "ativo" in row:
                obj.active = bool(row["ativo"])
            obj.source_updated_at = dt(row.get("ultima_alteracao"))
            if resource == "categories":
                obj.parent_mercos_id = (
                    str(
                        row.get("categoria_pai_id")
                        or row.get("pai_id")
                        or ""
                    )
                    or None
                )
            obj.raw = row
            db.add(obj)
            persisted += 1
        elif resource == "product-prices":
            product_id = str(row.get("produto_id") or "")
            price_table_id = str(row.get("tabela_preco_id") or "")
            if not product_id or not price_table_id:
                raise ValueError(
                    "Preço de produto sem produto_id ou tabela_preco_id"
                )
            obj = db.scalar(
                select(ProductPrice).where(
                    ProductPrice.product_mercos_id == product_id,
                    ProductPrice.price_table_mercos_id == price_table_id,
                )
            ) or ProductPrice(
                product_mercos_id=product_id,
                price_table_mercos_id=price_table_id,
            )
            obj.price = f(row.get("preco"))
            obj.source_updated_at = dt(row.get("ultima_alteracao"))
            obj.raw = row
            db.add(obj)
            persisted += 1
    return {"persisted": persisted, "itemsPersisted": items_persisted}


def _lease_is_active(state, now: datetime) -> bool:
    if state is None or state.status != "running":
        return False
    heartbeat = state.heartbeat_at
    if heartbeat is None:
        return False
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    return heartbeat >= now - SYNC_LEASE_TTL


def _acquire_sync_coordination_lock(db) -> None:
    """Serialize short sync state transactions across service instances."""
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:resource))"),
            {"resource": SYNC_COORDINATION_LOCK},
        )


def interrupt_running_syncs(reason: str = "Sincronização interrompida") -> int:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        _acquire_sync_coordination_lock(db)
        states = list(
            db.scalars(
                select(SyncState)
                .where(SyncState.status == "running")
                .order_by(SyncState.resource)
                .with_for_update()
            )
        )
        runs = list(
            db.scalars(
                select(SyncRun)
                .where(SyncRun.status == "running")
                .order_by(SyncRun.id)
                .with_for_update()
            )
        )
        for state in states:
            state.status = "interrupted"
            state.error = reason
            state.lease_token = None
            db.add(state)
        for run in runs:
            run.status = "interrupted"
            run.finished_at = now
            run.error = reason
            db.add(run)
        db.commit()
        return len(states)


def _claim_sync(resource: str, full: bool, started_at: datetime):
    """Atomically claim a resource across workers and service instances."""
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        _acquire_sync_coordination_lock(db)
        busy = db.scalars(select(SyncState)).all()
        for other in busy:
            if other.resource == resource:
                continue
            if _lease_is_active(other, now):
                return None
        state = db.scalar(
            select(SyncState)
            .where(SyncState.resource == resource)
            .with_for_update()
        )
        if state is None:
            state = SyncState(resource=resource)
            db.add(state)
            db.flush()
        heartbeat = state.heartbeat_at
        if heartbeat is not None and heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        if (
            state.status == "running"
            and heartbeat is not None
            and heartbeat >= now - SYNC_LEASE_TTL
        ):
            return None

        if state.status == "running":
            stale_runs = db.scalars(
                select(SyncRun).where(
                    SyncRun.resource == resource,
                    SyncRun.status == "running",
                )
            ).all()
            for stale_run in stale_runs:
                stale_run.status = "interrupted"
                stale_run.finished_at = now
                stale_run.error = "Lease de sincronização expirou"
                db.add(stale_run)

        token = str(uuid4())
        cursor_before = state.cursor
        state.status = "running"
        state.error = None
        state.lease_token = token
        state.heartbeat_at = now
        db.add(state)
        run = SyncRun(
            resource=resource,
            mode="full" if full else "incremental",
            status="running",
            started_at=started_at,
            cursor_before=cursor_before,
            cursor_after=cursor_before,
            details={},
        )
        db.add(run)
        db.flush()
        run_id = run.id
        db.commit()
        return run_id, token, cursor_before


def _persist_sync_page(
    run_id: int,
    lease_token: str,
    resource: str,
    rows: list,
    *,
    page_cursor: str | None,
    next_pages: int,
    received: int,
    persisted: int,
    failed: int,
    details_consulted: int,
    items_persisted: int,
):
    with SessionLocal() as db:
        _acquire_sync_coordination_lock(db)
        state = db.scalar(
            select(SyncState)
            .where(SyncState.resource == resource)
            .with_for_update()
        )
        if state is None or state.lease_token != lease_token:
            raise RuntimeError(f"Lease de sincronização perdida para {resource}")
        stats = _upsert_rows(db, resource, rows)
        next_persisted = persisted + stats["persisted"]
        next_items_persisted = items_persisted + stats["itemsPersisted"]
        state.cursor = page_cursor or state.cursor
        state.records = next_persisted
        state.status = "running"
        state.error = None
        state.heartbeat_at = datetime.now(timezone.utc)
        db.add(state)
        run = db.get(SyncRun, run_id)
        if run is None:
            raise RuntimeError(f"Execução de sync {run_id} não encontrada")
        run.cursor_after = page_cursor
        run.pages = next_pages
        run.received = received
        run.persisted = next_persisted
        run.failed = failed
        run.details = {
            "detailsConsulted": details_consulted,
            "itemsPersisted": next_items_persisted,
        }
        db.add(run)
        db.commit()
        return next_persisted, next_items_persisted


def _finish_sync_run(
    run_id: int,
    lease_token: str,
    resource: str,
    *,
    status: str,
    pages: int,
    received: int,
    persisted: int,
    failed: int,
    cursor_after: str | None,
    details_consulted: int,
    items_persisted: int,
    started_at: datetime,
    error: str | None,
):
    finished_at = datetime.now(timezone.utc)
    with SessionLocal() as db:
        _acquire_sync_coordination_lock(db)
        state = db.get(SyncState, resource) or SyncState(resource=resource)
        owns_lease = state.lease_token == lease_token
        if owns_lease:
            state.status = status
            state.records = persisted
            state.error = error
            state.lease_token = None
            state.heartbeat_at = finished_at
            if status == "success":
                state.last_success_at = finished_at
            db.add(state)
        run = db.get(SyncRun, run_id)
        if run is not None:
            run.status = status
            run.finished_at = finished_at
            run.cursor_after = cursor_after
            run.pages = pages
            run.received = received
            run.persisted = persisted
            run.failed = failed
            run.details = {
                "detailsConsulted": details_consulted,
                "itemsPersisted": items_persisted,
                "durationSeconds": round(
                    (finished_at - started_at).total_seconds(),
                    3,
                ),
            }
            run.error = error
            db.add(run)
        db.commit()
        return {
            "resource": state.resource,
            "cursor": state.cursor,
            "records": state.records,
            "status": status if owns_lease else "interrupted",
            "runId": run_id,
        }


async def sync_resource(resource: str, full=False, *, raise_http=True):
    # Short DB sessions only — never hold a pooler connection during Mercos HTTP waits
    started_at = datetime.now(timezone.utc)
    claim = await asyncio.to_thread(_claim_sync, resource, full, started_at)
    if claim is None:
        return {
            "resource": resource,
            "status": "running",
            "message": "Sincronização Mercos já está em andamento",
        }
    run_id, lease_token, cursor_before = claim
    cursor = None if full else cursor_before

    pages = 0
    received = 0
    persisted = 0
    failed = 0
    details_consulted = 0
    items_persisted = 0
    committed_cursor = cursor_before
    try:
        for _ in range(MAX_PAGES):
            result = await adaptor.list(resource, cursor)
            rows = result.get("data") or []
            if not rows:
                break
            received += len(rows)
            if resource == "orders":
                details_needed = sum(1 for row in rows if _order_line_items(row) is None)
                try:
                    rows = await _hydrate_order_details(rows)
                    details_consulted += details_needed
                except OrderDetailBatchError as exc:
                    details_consulted += details_needed
                    failed += exc.failed
                    raise
            next_cursor = result.get("nextCursor")
            page_cursor = result.get("pageCursor") or next_cursor or cursor
            next_pages = pages + 1
            next_persisted, next_items_persisted = await asyncio.to_thread(
                _persist_sync_page,
                run_id,
                lease_token,
                resource,
                rows,
                page_cursor=page_cursor or committed_cursor,
                next_pages=next_pages,
                received=received,
                persisted=persisted,
                failed=failed,
                details_consulted=details_consulted,
                items_persisted=items_persisted,
            )
            pages = next_pages
            persisted = next_persisted
            items_persisted = next_items_persisted
            committed_cursor = page_cursor or committed_cursor
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        else:
            error = f"Limite de {MAX_PAGES} páginas; rode sync incremental para continuar"
            snapshot = await asyncio.to_thread(
                _finish_sync_run,
                run_id,
                lease_token,
                resource,
                status="partial",
                pages=pages,
                received=received,
                persisted=persisted,
                failed=failed,
                cursor_after=committed_cursor,
                details_consulted=details_consulted,
                items_persisted=items_persisted,
                started_at=started_at,
                error=error,
            )
            log.warning("Sync %s partial after %s pages (%s records)", resource, MAX_PAGES, persisted)
            return {**snapshot, "records": persisted, "status": "partial"}

        if resource == "orders":
            details_consulted += await _backfill_missing_order_details()

        snapshot = await asyncio.to_thread(
            _finish_sync_run,
            run_id,
            lease_token,
            resource,
            status="success",
            pages=pages,
            received=received,
            persisted=persisted,
            failed=failed,
            cursor_after=committed_cursor,
            details_consulted=details_consulted,
            items_persisted=items_persisted,
            started_at=started_at,
            error=None,
        )
        return {
            "resource": resource,
            "records": persisted,
            "cursor": snapshot["cursor"],
            "status": "success",
            "runId": run_id,
        }
    except Exception as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        # Network blips / Render cold starts — keep cursor and allow auto-resume
        source_exc = exc.first_error if isinstance(exc, OrderDetailBatchError) else exc
        unavailable = (
            resource in OPTIONAL_CATALOG_RESOURCES
            and isinstance(source_exc, HTTPException)
            and source_exc.status_code in {403, 404}
        )
        if unavailable:
            snapshot = await asyncio.to_thread(
                _finish_sync_run,
                run_id,
                lease_token,
                resource,
                status="unavailable",
                pages=pages,
                received=received,
                persisted=persisted,
                failed=0,
                cursor_after=committed_cursor,
                details_consulted=details_consulted,
                items_persisted=items_persisted,
                started_at=started_at,
                error=str(detail)[:1000],
            )
            log.warning(
                "Sync %s unavailable in the connected Mercos account",
                resource,
            )
            return {
                **snapshot,
                "records": persisted,
                "status": "unavailable",
                "error": str(detail),
            }
        transient = isinstance(source_exc, HTTPException) and (
            "inacessível" in str(source_exc.detail).lower()
            or source_exc.status_code in {409, 429, 502, 503}
        )
        status = "interrupted" if transient else "error"
        failed = max(failed, received - persisted)
        await asyncio.to_thread(
            _finish_sync_run,
            run_id,
            lease_token,
            resource,
            status=status,
            pages=pages,
            received=received,
            persisted=persisted,
            failed=failed,
            cursor_after=committed_cursor,
            details_consulted=details_consulted,
            items_persisted=items_persisted,
            started_at=started_at,
            error=str(detail)[:1000],
        )
        if isinstance(source_exc, HTTPException) and source_exc.status_code == 409:
            log.warning("Sync %s cancelled by operator", resource)
        else:
            log.exception("Sync failed for %s", resource)
        if raise_http:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(502, f"Sync {resource}: {detail}") from exc
        return {
            "resource": resource,
            "status": status,
            "error": str(detail),
            "statusCode": (
                source_exc.status_code
                if isinstance(source_exc, HTTPException)
                else None
            ),
        }


def _should_stop_pipeline(result: dict) -> bool:
    if result.get("status") == "running":
        return True
    # A transient failure (especially Mercos 429) affects the shared account,
    # not only one endpoint. Continuing with categories/products just spends
    # more quota and delays the priority orders sync.
    return result.get("status") == "interrupted"


async def _pause_after_resource(result: dict, *, last: bool) -> None:
    if last:
        return
    if result.get("status") == "interrupted":
        log.warning(
            "Sync %s interrupted; waiting %ss before the next resource",
            result.get("resource"),
            RATE_LIMIT_PAUSE_SECONDS,
        )
        await asyncio.sleep(RATE_LIMIT_PAUSE_SECONDS)
        return
    await asyncio.sleep(RESOURCE_PAUSE_SECONDS)


async def _run_resource_sequence(resources: tuple[str, ...], full: bool) -> list[dict]:
    results = []
    total = len(resources)
    for index, resource in enumerate(resources):
        result = await sync_resource(resource, full, raise_http=False)
        results.append(result)
        if _should_stop_pipeline(result):
            log.warning(
                "Stopping Mercos pipeline after %s on %s",
                result.get("status"),
                resource,
            )
            break
        await _pause_after_resource(result, last=index + 1 == total)
    return results


async def sync_all(full=False, *, raise_http=True):
    results = await _run_resource_sequence(SYNC_RESOURCES, full)
    if raise_http and results and all(r.get("status") == "error" for r in results):
        raise HTTPException(502, {"message": "Sync falhou", "results": results})
    return results


async def sync_orders_job():
    log.info("Scheduled orders sync starting")
    result = await sync_resource("orders", full=False, raise_http=False)
    if result.get("status") == "running":
        log.warning(
            "Scheduled orders sync skipped: %s",
            result.get("message", "another sync owns the lease"),
        )
        return
    log.info(
        "Scheduled orders sync finished: status=%s records=%s run_id=%s",
        result.get("status"),
        result.get("records", 0),
        result.get("runId"),
    )


async def sync_catalog_job():
    await _run_resource_sequence(CATALOG_RESOURCES, False)
