import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import delete, select

from app.adaptor import adaptor
from app.database import SessionLocal
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

MAX_PAGES = 5000
ORDER_DETAIL_CONCURRENCY = 5
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
SYNC_RESOURCES = (*CATALOG_RESOURCES, "orders")


class OrderDetailBatchError(Exception):
    def __init__(self, failed: int, first_error: Exception):
        self.failed = failed
        self.first_error = first_error
        super().__init__(f"{failed} detalhe(s) de pedido falharam: {first_error}")


def dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


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


async def _hydrate_order_details(rows: list[dict]) -> list[dict]:
    """Fetch every changed order detail before opening the write transaction."""
    semaphore = asyncio.Semaphore(ORDER_DETAIL_CONCURRENCY)

    async def fetch(row: dict):
        mercos_id = str(row.get("id") or "")
        if not mercos_id:
            raise ValueError("Pedido sem id no payload de listagem")
        async with semaphore:
            detail = await adaptor.detail("orders", mercos_id)
        detail_id = str(detail.get("id") or mercos_id)
        if detail_id != mercos_id:
            raise ValueError(
                f"Detalhe do pedido {mercos_id} retornou id divergente {detail_id}"
            )
        return {**row, **detail, "id": mercos_id}

    results = await asyncio.gather(*(fetch(row) for row in rows), return_exceptions=True)
    failures = [result for result in results if isinstance(result, Exception)]
    if failures:
        raise OrderDetailBatchError(len(failures), failures[0])
    return [result for result in results if isinstance(result, dict)]


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
            if "total_bruto" in row or "valor_bruto" in row:
                obj.gross_total = optional_decimal(
                    row,
                    "total_bruto",
                    "valor_bruto",
                )
            obj.net_total = optional_decimal(row, "total_liquido", "valor_liquido", "total")
            if "valor_desconto" in row or "desconto_valor" in row:
                obj.discount_value = optional_decimal(
                    row,
                    "valor_desconto",
                    "desconto_valor",
                )
            if "desconto_percentual" in row or "percentual_desconto" in row:
                obj.discount_percent = optional_decimal(
                    row,
                    "desconto_percentual",
                    "percentual_desconto",
                )
            items = row.get("itens") or row.get("items") or []
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
            for pos, item in enumerate(row.get("itens") or row.get("items") or []):
                q = f(item.get("quantidade"))
                unit = f(
                    item.get("preco_liquido")
                    or item.get("preco_unitario")
                    or item.get("preco")
                    or item.get("preco_tabela")
                )
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
                        unit_price=unit,
                        discount=f(item.get("desconto") or item.get("desconto_de_cupom")),
                        total=total,
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


def _finish_sync_run(
    run_id: int,
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
        state = db.get(SyncState, resource) or SyncState(resource=resource)
        state.status = status
        state.records = persisted
        state.error = error
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
            "status": state.status,
            "runId": run_id,
        }


async def sync_resource(resource: str, full=False, *, raise_http=True):
    # Short DB sessions only — never hold a pooler connection during Mercos HTTP waits
    started_at = datetime.now(timezone.utc)
    with SessionLocal() as db:
        state = db.get(SyncState, resource) or SyncState(resource=resource)
        cursor_before = state.cursor
        cursor = None if full else cursor_before
        state.status = "running"
        state.error = None
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
                try:
                    rows = await _hydrate_order_details(rows)
                    details_consulted += len(rows)
                except OrderDetailBatchError as exc:
                    details_consulted += len(rows)
                    failed += exc.failed
                    raise
            next_cursor = result.get("nextCursor")
            page_cursor = result.get("pageCursor") or next_cursor or cursor
            next_pages = pages + 1
            with SessionLocal() as db:
                stats = _upsert_rows(db, resource, rows)
                next_persisted = persisted + stats["persisted"]
                next_items_persisted = items_persisted + stats["itemsPersisted"]
                state = db.get(SyncState, resource) or SyncState(resource=resource)
                state.cursor = page_cursor or state.cursor
                state.records = next_persisted
                state.status = "running"
                state.error = None
                db.add(state)
                run = db.get(SyncRun, run_id)
                if run is None:
                    raise RuntimeError(f"Execução de sync {run_id} não encontrada")
                run.cursor_after = page_cursor or committed_cursor
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
            pages = next_pages
            persisted = next_persisted
            items_persisted = next_items_persisted
            committed_cursor = page_cursor or committed_cursor
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        else:
            error = f"Limite de {MAX_PAGES} páginas; rode sync incremental para continuar"
            snapshot = _finish_sync_run(
                run_id,
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

        snapshot = _finish_sync_run(
            run_id,
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
        transient = isinstance(source_exc, HTTPException) and (
            "inacessível" in str(source_exc.detail).lower()
            or source_exc.status_code in {429, 502, 503}
        )
        status = "interrupted" if transient else "error"
        failed = max(failed, received - persisted)
        _finish_sync_run(
            run_id,
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
        log.exception("Sync failed for %s", resource)
        if raise_http:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(502, f"Sync {resource}: {detail}") from exc
        return {"resource": resource, "status": status, "error": str(detail)}


async def sync_all(full=False, *, raise_http=True):
    results = []
    for resource in SYNC_RESOURCES:
        results.append(await sync_resource(resource, full, raise_http=False))
    if raise_http and results and all(r.get("status") == "error" for r in results):
        raise HTTPException(502, {"message": "Sync falhou", "results": results})
    return results


async def sync_orders_job():
    await sync_resource("orders", full=False, raise_http=False)


async def sync_catalog_job():
    for resource in CATALOG_RESOURCES:
        await sync_resource(resource, full=False, raise_http=False)
