"""Manutenção local de produtos e carga (Subir) para o Mercos.

O cadastro grava na hora no ERP. O botão Subir só enfileira o envio quando o
checkbox Mercos está marcado e a capacidade de escrita está ligada. O saldo
editado aqui é o saldo do produto; a publicação absoluta no Mercos entra na
mesma carga quando o produto já tem ID numérico.
"""

from decimal import Decimal
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.erp import capabilities
from app.erp.audit import audit
from app.erp.auth import ErpUser
from app.erp.common import http_error, iso, money, quantity, utcnow
from app.erp.models.commercial import ErpProduct, ErpProductVariant, ErpSalesOrder, ErpSalesOrderItem
from app.erp.models.local import ErpInventoryMovement
from app.erp.outbox import enqueue_product
from app.erp.schemas.commands import ProductCreate, ProductPatch
from app.erp.serializers import marked_for_mercos

_ATTRS = {
    "name": "name",
    "code": "code",
    "unit": "unit",
    "categoryId": "category_external_id",
    "listPrice": "list_price",
    "minimumPrice": "minimum_price",
    "externalStock": "external_stock",
    "ncm": "ncm",
    "notes": "notes",
    "imageUrl": "image_url",
    "active": "active",
    "onMercos": "publish_to_mercos",
    "description": "description",
    "photos": "photos",
    "videos": "videos",
    "grossWeight": "gross_weight",
    "width": "width",
    "height": "height",
    "length": "length",
    "supplierCode": "supplier_code",
    "barcode": "barcode",
    "lot": "lot",
    "receivedAt": "received_at",
    "packKind": "pack_kind",
    "cost": "cost_price",
    "stockLocation": "stock_location",
}


def get_product(db: Session, connection_id: str, external_id: str) -> ErpProduct:
    row = db.scalar(
        select(ErpProduct).where(
            ErpProduct.connection_id == connection_id,
            ErpProduct.external_id == external_id,
        )
    )
    if row is None:
        raise http_error(404, "not_found", "Produto não encontrado")
    return row


def _touch(row: ErpProduct) -> None:
    row.version = (row.version or 1) + 1
    row.source_updated_at = utcnow()
    row.captured_at = utcnow()
    row.fingerprint = None


def create_product(db: Session, user: ErpUser, connection_id: str, body: ProductCreate) -> ErpProduct:
    data = body.model_dump(exclude_unset=True)
    variants = data.pop("variants", None)
    row = ErpProduct(
        connection_id=connection_id,
        external_id=f"local-{uuid4().hex[:20]}",
        name=data["name"],
        list_price=data["listPrice"],
        kind="simple",
        sellable=True,
        active=True if data.get("active") is None else data["active"],
        publish_to_mercos=bool(data.get("onMercos")),
        source_deleted=False,
        version=1,
        source_updated_at=utcnow(),
        captured_at=utcnow(),
    )
    for key, attr in _ATTRS.items():
        if key in data and key not in ("name", "listPrice", "active", "onMercos"):
            setattr(row, attr, data[key])
    db.add(row)
    db.flush()
    if variants is not None:
        _save_variants(db, row, variants)
    audit(
        db, operator=user.username, action="product.create", connection_id=connection_id,
        resource="products", resource_id=row.external_id, result="ok",
        detail={"name": row.name},
    )
    return row


def update_product(
    db: Session, user: ErpUser, connection_id: str, external_id: str, body: ProductPatch,
) -> ErpProduct:
    row = get_product(db, connection_id, external_id)
    if row.source_deleted:
        raise http_error(409, "product_deleted", "Produto excluído não pode ser alterado")
    data = body.model_dump(exclude_unset=True)
    expected = data.pop("expectedVersion")
    variants = data.pop("variants", None)
    if row.version != expected:
        raise http_error(
            409, "version_mismatch", "A versão local mudou; recarregue antes de editar",
            currentVersion=row.version,
        )
    if not data and variants is None:
        raise http_error(422, "empty_patch", "Nenhum campo para alterar")
    for key, value in data.items():
        setattr(row, _ATTRS[key], value)
    if variants is not None:
        _save_variants(db, row, variants)
    _touch(row)
    db.add(row)
    audit(
        db, operator=user.username, action="product.update", connection_id=connection_id,
        resource="products", resource_id=row.external_id, result="ok",
        detail={"fields": sorted(data)},
    )
    return row


def delete_product(db: Session, user: ErpUser, connection_id: str, external_id: str) -> ErpProduct:
    row = get_product(db, connection_id, external_id)
    if row.source_deleted:
        return row
    row.source_deleted = True
    row.active = False
    _touch(row)
    db.add(row)
    audit(
        db, operator=user.username, action="product.delete", connection_id=connection_id,
        resource="products", resource_id=row.external_id, result="ok",
    )
    if marked_for_mercos(row) and str(row.external_id).isdigit():
        enabled, _reason = capabilities.capability_enabled(db, connection_id, "write.products")
        if enabled:
            enqueue_product(
                db,
                connection_id=connection_id,
                user=user,
                kind="publish_product",
                mercos_payload={"excluido": True, "ativo": False},
                dto={"id": row.external_id, "deleted": True},
                target_external_id=row.external_id,
                idempotency_key=f"del-{row.external_id}-{row.version}"[:120],
            )
    return row


def _save_variants(db: Session, row: ErpProduct, items: list[dict]) -> None:
    db.execute(delete(ErpProductVariant).where(ErpProductVariant.product_id == row.id))
    position = 0
    for item in items:
        if not any(item.get(key) not in (None, "") for key in ("code", "name", "externalStock", "price")):
            continue
        position += 1
        db.add(ErpProductVariant(
            product_id=row.id,
            position=position,
            code=item.get("code"),
            name=item.get("name"),
            external_stock=item.get("externalStock"),
            price=item.get("price"),
        ))


def product_history(db: Session, connection_id: str, external_id: str) -> dict:
    """Pedidos e movimentos de estoque do produto. O Mercos não manda geolocalização."""
    get_product(db, connection_id, external_id)
    orders = db.execute(
        select(ErpSalesOrderItem, ErpSalesOrder)
        .join(ErpSalesOrder, ErpSalesOrder.id == ErpSalesOrderItem.order_id)
        .where(
            ErpSalesOrder.connection_id == connection_id,
            ErpSalesOrderItem.product_external_id == external_id,
            ErpSalesOrderItem.excluded.is_(False),
        )
        .order_by(ErpSalesOrder.id.desc())
        .limit(40)
    ).all()
    movements = db.scalars(
        select(ErpInventoryMovement)
        .where(
            ErpInventoryMovement.connection_id == connection_id,
            ErpInventoryMovement.product_external_id == external_id,
        )
        .order_by(ErpInventoryMovement.occurred_at.desc())
        .limit(40)
    ).all()
    return {
        "mercosGeolocation": None,
        "mercosGeolocationNote": (
            "O cadastro de produto do Mercos não traz geolocalização. "
            "O local no estoque é informado neste ERP."
        ),
        "dimensionsSource": "mercos",
        "dimensionsNote": (
            "Peso e dimensões vêm do espelho do Mercos (peso_bruto, largura, altura e comprimento). "
            "Este ERP não consulta a Tray."
        ),
        "orders": [
            {
                "orderId": order.external_id,
                "number": order.number,
                "issuedAt": iso(order.issued_at),
                "status": order.commercial_status,
                "quantity": quantity(item.quantity),
                "total": money(item.total),
            }
            for item, order in orders
        ],
        "movements": [
            {
                "id": movement.id,
                "kind": movement.kind,
                "quantityDelta": quantity(movement.quantity_delta),
                "occurredAt": iso(movement.occurred_at),
                "reason": movement.reason,
                "warehouseId": movement.warehouse_id,
            }
            for movement in movements
        ],
    }


def _number(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _external_id(value: str | None):
    if not value:
        return None
    return int(value) if value.isdigit() else value


def mercos_payload(row: ErpProduct, *, deleted: bool = False) -> dict:
    if deleted:
        return {"excluido": True, "ativo": False}
    name = (row.name or "").strip()
    if not name:
        raise http_error(422, "invalid_product", "Informe o nome antes de subir")
    if len(name) > 100:
        raise http_error(422, "name_too_long", "O Mercos aceita nome de até 100 caracteres")
    if row.list_price is None:
        raise http_error(422, "invalid_product", "Informe o preço de tabela antes de subir")
    payload: dict = {"nome": name, "preco_tabela": float(row.list_price), "ativo": row.active is not False}
    optional = (
        ("codigo", row.code),
        ("unidade", row.unit),
        ("categoria_id", _external_id(row.category_external_id)),
        ("preco_minimo", _number(row.minimum_price)),
        ("saldo_estoque", _number(row.external_stock)),
        ("comissao", _number(row.commission_percent)),
        ("ipi", _number(row.ipi_percent)),
        ("codigo_ncm", row.ncm),
        ("multiplo", _number(row.multiple)),
        ("peso_bruto", _number(row.gross_weight)),
        ("largura", _number(row.width)),
        ("altura", _number(row.height)),
        ("comprimento", _number(row.length)),
        ("observacoes", row.notes),
    )
    for key, value in optional:
        if value not in (None, ""):
            payload[key] = value
    return payload


def publish_product(
    db: Session,
    user: ErpUser,
    connection_id: str,
    external_id: str,
    idempotency_key: str | None,
) -> tuple[object, bool, str | None]:
    """Enfileira a carga. Devolve (operação, criada, nota do estoque)."""
    row = get_product(db, connection_id, external_id)
    if not marked_for_mercos(row):
        raise http_error(422, "mercos_not_marked", "Marque Mercos antes de subir o produto")
    deleted = bool(row.source_deleted)
    numeric = str(row.external_id).isdigit()
    if deleted and not numeric:
        raise http_error(
            409, "nothing_to_publish", "Produto local excluído: não há cadastro no Mercos para encerrar",
        )
    payload = mercos_payload(row, deleted=deleted)
    kind = "publish_product" if numeric else "create_product"
    op, created = enqueue_product(
        db,
        connection_id=connection_id,
        user=user,
        kind=kind,
        mercos_payload=payload,
        dto={"id": row.external_id, "deleted": deleted},
        target_external_id=row.external_id,
        idempotency_key=idempotency_key,
    )
    stock_note = None
    if created and not deleted and numeric and row.external_stock is not None:
        enabled, reason = capabilities.capability_enabled(db, connection_id, "write.inventory_publish")
        if not enabled:
            stock_note = reason or "Publicação de estoque desabilitada"
        else:
            stock_key = f"{idempotency_key}:stock"
            if len(stock_key) > 120:
                stock_key = stock_key[:120]
            enqueue_product(
                db,
                connection_id=connection_id,
                user=user,
                kind="publish_product_stock",
                mercos_payload={"novo_saldo": float(row.external_stock)},
                dto={"id": row.external_id, "externalStock": str(row.external_stock)},
                target_external_id=row.external_id,
                idempotency_key=stock_key,
            )
            stock_note = "queued"
    return op, created, stock_note
