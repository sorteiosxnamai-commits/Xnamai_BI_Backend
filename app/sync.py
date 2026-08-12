import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete, select

from app.adaptor import adaptor
from app.database import SessionLocal
from app.models import Customer, Order, OrderItem, Product, Seller, SyncState

log = logging.getLogger("uvicorn.error")

MAX_PAGES = 5000


def dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def f(value):
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (ValueError, TypeError):
            return 0.0
    text = str(value).strip()
    # BR: 1.234.567,89 → 1234567.89
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        n = float(text)
    except (ValueError, TypeError):
        return 0.0
    # Descarta totais absurdos na origem
    if n < 0 or n > 5_000_000:
        return 0.0
    return n


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
            obj.seller_mercos_id = (
                str(row.get("criador_id") or row.get("usuario_id") or row.get("vendedor_id") or "") or None
            )
            obj.status = str(row.get("status") if row.get("status") is not None else row.get("situacao") or "unknown")
            obj.issued_at = dt(row.get("data_emissao") or row.get("data_criacao") or row.get("ultima_alteracao"))
            obj.total = f(row.get("total"))
            obj.discount = f(row.get("desconto"))
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


def _set_state(resource: str, **fields):
    with SessionLocal() as db:
        state = db.get(SyncState, resource) or SyncState(resource=resource)
        for key, value in fields.items():
            setattr(state, key, value)
        db.add(state)
        db.commit()
        return {
            "resource": state.resource,
            "cursor": state.cursor,
            "records": state.records,
            "status": state.status,
        }


async def sync_resource(resource: str, full=False, *, raise_http=True):
    # Short DB sessions only — never hold a pooler connection during Mercos HTTP waits
    with SessionLocal() as db:
        state = db.get(SyncState, resource) or SyncState(resource=resource)
        cursor = None if full else state.cursor
        state.status = "running"
        state.error = None
        db.add(state)
        db.commit()

    total = 0
    try:
        for _ in range(MAX_PAGES):
            result = await adaptor.list(resource, cursor)
            rows = result.get("data") or []
            if not rows:
                break
            next_cursor = result.get("nextCursor")
            with SessionLocal() as db:
                _upsert_rows(db, resource, rows)
                total += len(rows)
                state = db.get(SyncState, resource) or SyncState(resource=resource)
                state.cursor = next_cursor or cursor or state.cursor
                state.records = total
                state.status = "running"
                state.error = None
                db.add(state)
                db.commit()
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        else:
            snapshot = _set_state(
                resource,
                status="partial",
                records=total,
                error=f"Limite de {MAX_PAGES} páginas; rode sync incremental para continuar",
            )
            log.warning("Sync %s partial after %s pages (%s records)", resource, MAX_PAGES, total)
            return {**snapshot, "records": total, "status": "partial"}

        snapshot = _set_state(
            resource,
            status="success",
            records=total,
            error=None,
            last_success_at=datetime.now(timezone.utc),
        )
        return {"resource": resource, "records": total, "cursor": snapshot["cursor"], "status": "success"}
    except Exception as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        # Network blips / Render cold starts — keep cursor and allow auto-resume
        transient = isinstance(exc, HTTPException) and (
            "inacessível" in str(exc.detail).lower() or exc.status_code in {502, 503}
        )
        status = "interrupted" if transient else "error"
        _set_state(resource, status=status, error=str(detail)[:1000], records=total)
        log.exception("Sync failed for %s", resource)
        if raise_http:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(502, f"Sync {resource}: {detail}") from exc
        return {"resource": resource, "status": status, "error": str(detail)}


async def sync_all(full=False, *, raise_http=True):
    results = []
    for resource in ("customers", "products", "users", "orders"):
        results.append(await sync_resource(resource, full, raise_http=False))
    if raise_http and results and all(r.get("status") == "error" for r in results):
        raise HTTPException(502, {"message": "Sync falhou", "results": results})
    return results


async def sync_orders_job():
    await sync_resource("orders", full=False, raise_http=False)


async def sync_catalog_job():
    for resource in ("customers", "products", "users"):
        await sync_resource(resource, full=False, raise_http=False)
