import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete, select

from app.adaptor import adaptor
from app.database import SessionLocal
from app.models import Customer, Order, OrderItem, Product, Seller, SyncState

log = logging.getLogger("uvicorn.error")

MAX_PAGES = 200


def dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def f(value):
    try:
        return float(value or 0)
    except (ValueError, TypeError):
        return 0


def _upsert_rows(db, resource: str, rows: list):
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
            obj.source_updated_at = dt(row.get("ultima_alteracao"))
            obj.raw = row
            db.add(obj)
        elif resource == "products":
            obj = db.scalar(select(Product).where(Product.mercos_id == mid)) or Product(mercos_id=mid)
            obj.code = str(row.get("codigo") or "")
            obj.name = row.get("nome") or "Sem nome"
            obj.category_id = str(row.get("categoria_id") or "") or None
            obj.unit = row.get("unidade")
            obj.list_price = f(row.get("preco_tabela"))
            obj.stock = f(row.get("saldo_estoque"))
            obj.active = bool(row.get("ativo", True))
            obj.raw = row
            db.add(obj)
        elif resource == "users":
            obj = db.scalar(select(Seller).where(Seller.mercos_id == mid)) or Seller(mercos_id=mid)
            obj.name = row.get("nome") or row.get("email") or "Sem nome"
            obj.active = bool(row.get("ativo", True))
            obj.raw = row
            db.add(obj)
        elif resource == "orders":
            obj = db.scalar(select(Order).where(Order.mercos_id == mid)) or Order(
                mercos_id=mid, number=mid, status="unknown"
            )
            obj.number = str(row.get("numero") or mid)
            obj.customer_mercos_id = str(row.get("cliente_id") or "") or None
            obj.seller_mercos_id = str(row.get("usuario_id") or row.get("vendedor_id") or "") or None
            obj.status = str(row.get("status") or row.get("situacao") or "unknown")
            obj.issued_at = dt(row.get("data_emissao") or row.get("data_criacao"))
            obj.total = f(row.get("total"))
            obj.discount = f(row.get("desconto"))
            obj.source_updated_at = dt(row.get("ultima_alteracao"))
            obj.raw = row
            db.add(obj)
            db.flush()
            db.execute(delete(OrderItem).where(OrderItem.order_mercos_id == mid))
            for pos, item in enumerate(row.get("itens") or row.get("items") or []):
                q = f(item.get("quantidade"))
                unit = f(item.get("preco_unitario") or item.get("preco"))
                total = f(item.get("total") or q * unit)
                db.add(
                    OrderItem(
                        order_mercos_id=mid,
                        position=pos,
                        product_mercos_id=str(item.get("produto_id") or "") or None,
                        code=str(item.get("codigo") or ""),
                        name=item.get("nome") or item.get("descricao") or "Produto",
                        quantity=q,
                        unit_price=unit,
                        discount=f(item.get("desconto")),
                        total=total,
                        raw=item,
                    )
                )


async def sync_resource(resource: str, full=False):
    with SessionLocal() as db:
        state = db.get(SyncState, resource) or SyncState(resource=resource)
        cursor = None if full else state.cursor
        state.status = "running"
        state.error = None
        db.add(state)
        db.commit()
        try:
            total = 0
            for _ in range(MAX_PAGES):
                result = await adaptor.list(resource, cursor)
                rows = result.get("data") or []
                if not rows:
                    break
                _upsert_rows(db, resource, rows)
                total += len(rows)
                next_cursor = result.get("nextCursor")
                state.cursor = next_cursor or cursor or state.cursor
                state.records = total
                db.add(state)
                db.commit()
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
            state.last_success_at = datetime.now(timezone.utc)
            state.status = "success"
            state.records = total
            db.add(state)
            db.commit()
            return {"resource": resource, "records": total, "cursor": state.cursor, "status": "success"}
        except Exception as exc:
            db.rollback()
            state = db.get(SyncState, resource) or SyncState(resource=resource)
            state.status = "error"
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            state.error = str(detail)[:1000]
            db.add(state)
            db.commit()
            log.exception("Sync failed for %s", resource)
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(502, f"Sync {resource}: {detail}") from exc


async def sync_all(full=False):
    results = []
    errors = []
    for resource in ("customers", "products", "users", "orders"):
        try:
            results.append(await sync_resource(resource, full))
        except HTTPException as exc:
            errors.append({"resource": resource, "status": "error", "error": str(exc.detail)})
            results.append({"resource": resource, "status": "error", "error": str(exc.detail)})
    if errors and not any(r.get("status") == "success" for r in results):
        raise HTTPException(502, {"message": "Sync falhou", "results": results})
    return results


async def sync_orders_job():
    await sync_resource("orders", full=False)


async def sync_catalog_job():
    for resource in ("customers", "products", "users"):
        await sync_resource(resource, full=False)
