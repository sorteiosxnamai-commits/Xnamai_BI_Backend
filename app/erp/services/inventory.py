"""Estoque operacional: razão imutável + saldo derivado, sem saldo negativo.

O saldo só muda por UPDATE condicional (`WHERE on_hand + :d >= 0 ...`), que é
atômico em qualquer banco: duas operações simultâneas nunca levam o saldo abaixo
de zero nem a reserva acima do disponível. Cada movimento tem `causal_key`
única: o mesmo evento causal nunca produz o efeito duas vezes.

Autoridade: o saldo do Mercos é a referência até um corte explícito. Sem
autoridade `erp` no escopo, movimentos que alteram o saldo oficial são
recusados com motivo; o saldo externo nunca é sobrescrito por este módulo.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.erp.audit import audit
from app.erp.auth import ErpUser
from app.erp.common import http_error, utcnow
from app.erp.config import erp_settings
from app.erp.models.local import (
    ErpInventoryAuthority,
    ErpInventoryBalance,
    ErpInventoryMovement,
    ErpInventoryReservation,
    ErpWarehouse,
)

ZERO = Decimal("0")


def get_authority(db: Session, connection_id: str, scope: str = "") -> ErpInventoryAuthority:
    row = db.scalar(
        select(ErpInventoryAuthority).where(
            ErpInventoryAuthority.connection_id == connection_id,
            ErpInventoryAuthority.scope == scope,
        )
    )
    if row is None:
        row = ErpInventoryAuthority(
            connection_id=connection_id,
            scope=scope,
            authority=erp_settings().erp_inventory_authority
            if erp_settings().erp_inventory_authority in ("mercos", "erp")
            else "mercos",
            publish_enabled=False,
        )
        db.add(row)
        db.flush()
    return row


def is_erp_authority(row: ErpInventoryAuthority) -> bool:
    """Autoridade ERP só vale com corte conciliado, nunca só por configuração."""
    return row.authority == "erp" and row.reconciled_at is not None


def require_erp_authority(db: Session, connection_id: str, scope: str = "") -> None:
    row = get_authority(db, connection_id, scope)
    if not is_erp_authority(row):
        raise http_error(
            409,
            "inventory_authority_not_erp",
            "Estoque em modo consulta ao saldo Mercos; movimentos oficiais exigem corte "
            "e autoridade ERP explicitamente configurados",
            authority=row.authority,
        )


def set_authority(
    db: Session,
    user: ErpUser,
    connection_id: str,
    *,
    authority: str,
    scope: str,
    reason: str,
    cutover_reconciled: bool,
) -> ErpInventoryAuthority:
    row = get_authority(db, connection_id, scope)
    if authority == "erp":
        if not cutover_reconciled:
            raise http_error(
                422,
                "cutover_not_reconciled",
                "Para virar fonte do saldo, confirme o corte e a conciliação com o Mercos",
            )
        row.cutover_at = utcnow()
        row.reconciled_at = utcnow()
    row.authority = authority
    row.publish_enabled = False  # publicação exige capacidade própria e conciliação
    row.set_by = user.username
    row.reason = reason
    db.add(row)
    audit(
        db,
        operator=user.username,
        action="inventory.authority",
        connection_id=connection_id,
        resource="inventory",
        resource_id=scope or "default",
        reason=reason,
        detail={"authority": authority},
    )
    return row


def _warehouse(db: Session, connection_id: str, warehouse_id: int) -> ErpWarehouse:
    row = db.get(ErpWarehouse, warehouse_id)
    if row is None or row.connection_id != connection_id or not row.active:
        raise http_error(404, "warehouse_not_found", "Depósito não encontrado ou inativo")
    return row


def _ensure_balance(db: Session, connection_id: str, warehouse_id: int, product: str) -> ErpInventoryBalance:
    balance = db.scalar(
        select(ErpInventoryBalance).where(
            ErpInventoryBalance.warehouse_id == warehouse_id,
            ErpInventoryBalance.product_external_id == product,
        )
    )
    if balance is None:
        try:
            with db.begin_nested():
                balance = ErpInventoryBalance(
                    connection_id=connection_id,
                    warehouse_id=warehouse_id,
                    product_external_id=product,
                    on_hand=ZERO,
                    reserved=ZERO,
                )
                db.add(balance)
                db.flush()
        except IntegrityError:
            balance = db.scalar(
                select(ErpInventoryBalance).where(
                    ErpInventoryBalance.warehouse_id == warehouse_id,
                    ErpInventoryBalance.product_external_id == product,
                )
            )
    return balance


def apply_movement(
    db: Session,
    *,
    connection_id: str,
    warehouse_id: int,
    product: str,
    kind: str,
    quantity_delta: Decimal = ZERO,
    reserved_delta: Decimal = ZERO,
    unit_cost: Decimal | None = None,
    causal_key: str,
    reference_type: str | None = None,
    reference_id: str | None = None,
    operator: str,
    reason: str | None = None,
    reversal_of_id: int | None = None,
    expected_on_hand: Decimal | None = None,
) -> tuple[ErpInventoryMovement, bool]:
    """Aplica um movimento. Devolve (movimento, aplicado_agora)."""
    existing = db.scalar(
        select(ErpInventoryMovement).where(
            ErpInventoryMovement.connection_id == connection_id,
            ErpInventoryMovement.causal_key == causal_key,
        )
    )
    if existing is not None:
        return existing, False
    balance = _ensure_balance(db, connection_id, warehouse_id, product)
    conditions = [
        ErpInventoryBalance.id == balance.id,
        ErpInventoryBalance.on_hand + quantity_delta >= 0,
        ErpInventoryBalance.reserved + reserved_delta >= 0,
        ErpInventoryBalance.reserved + reserved_delta
        <= ErpInventoryBalance.on_hand + quantity_delta,
    ]
    if expected_on_hand is not None:
        conditions.append(ErpInventoryBalance.on_hand == expected_on_hand)
    result = db.execute(
        update(ErpInventoryBalance)
        .where(*conditions)
        .values(
            on_hand=ErpInventoryBalance.on_hand + quantity_delta,
            reserved=ErpInventoryBalance.reserved + reserved_delta,
            updated_at=utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise http_error(
            409,
            "insufficient_stock" if expected_on_hand is None else "stock_changed",
            "Saldo insuficiente, reserva acima do disponível ou saldo alterado por outra operação",
        )
    movement = ErpInventoryMovement(
        connection_id=connection_id,
        warehouse_id=warehouse_id,
        product_external_id=product,
        kind=kind,
        quantity_delta=quantity_delta,
        reserved_delta=reserved_delta,
        unit_cost=unit_cost,
        causal_key=causal_key,
        reference_type=reference_type,
        reference_id=reference_id,
        reversal_of_id=reversal_of_id,
        operator=operator,
        reason=reason,
    )
    db.add(movement)
    try:
        db.flush()
    except IntegrityError as exc:
        raise http_error(409, "duplicate_causal_event", "Evento causal já aplicado") from exc
    db.expire(balance)
    return movement, True


def adjust_to(
    db: Session,
    user: ErpUser,
    connection_id: str,
    *,
    warehouse_id: int,
    product: str,
    new_quantity: Decimal,
    reason: str,
    key: str,
) -> ErpInventoryMovement:
    """Ajuste de saldo absoluto (nunca incremento disfarçado)."""
    require_erp_authority(db, connection_id)
    _warehouse(db, connection_id, warehouse_id)
    balance = _ensure_balance(db, connection_id, warehouse_id, product)
    delta = new_quantity - balance.on_hand
    movement, _ = apply_movement(
        db,
        connection_id=connection_id,
        warehouse_id=warehouse_id,
        product=product,
        kind="adjustment",
        quantity_delta=delta,
        causal_key=f"adjust:{user.username}:{key}",
        reference_type="adjustment",
        operator=user.username,
        reason=reason,
        expected_on_hand=balance.on_hand,
    )
    audit(
        db,
        operator=user.username,
        action="inventory.adjust",
        connection_id=connection_id,
        resource="inventory",
        resource_id=product,
        reason=reason,
        detail={"warehouseId": warehouse_id, "delta": str(delta), "newQuantity": str(new_quantity)},
    )
    return movement


def transfer(
    db: Session,
    user: ErpUser,
    connection_id: str,
    *,
    from_warehouse: int,
    to_warehouse: int,
    product: str,
    quantity: Decimal,
    reason: str | None,
    key: str,
) -> tuple[ErpInventoryMovement, ErpInventoryMovement]:
    require_erp_authority(db, connection_id)
    if from_warehouse == to_warehouse:
        raise http_error(422, "same_warehouse", "Origem e destino devem ser diferentes")
    _warehouse(db, connection_id, from_warehouse)
    _warehouse(db, connection_id, to_warehouse)
    base = f"transfer:{user.username}:{key}"
    out, _ = apply_movement(
        db, connection_id=connection_id, warehouse_id=from_warehouse, product=product,
        kind="transfer_out", quantity_delta=-quantity, causal_key=f"{base}:out",
        reference_type="transfer", reference_id=base, operator=user.username, reason=reason,
    )
    incoming, _ = apply_movement(
        db, connection_id=connection_id, warehouse_id=to_warehouse, product=product,
        kind="transfer_in", quantity_delta=quantity, causal_key=f"{base}:in",
        reference_type="transfer", reference_id=base, operator=user.username, reason=reason,
    )
    audit(
        db, operator=user.username, action="inventory.transfer", connection_id=connection_id,
        resource="inventory", resource_id=product, reason=reason,
        detail={"from": from_warehouse, "to": to_warehouse, "quantity": str(quantity)},
    )
    return out, incoming


REVERSIBLE_KINDS = ("receipt", "adjustment", "sale_issue")


def reverse_movement(
    db: Session,
    user: ErpUser,
    connection_id: str,
    movement_id: int,
    *,
    reason: str,
    enforce_authority: bool = True,
) -> ErpInventoryMovement:
    """Reversão causal: novo movimento oposto, no máximo um por original.

    O histórico não é editado nem recalculado. Transferências se desfazem com
    outra transferência (as duas pernas juntas), e reservas se liberam.
    """
    if enforce_authority:
        require_erp_authority(db, connection_id)
    original = db.get(ErpInventoryMovement, movement_id)
    if original is None or original.connection_id != connection_id:
        raise http_error(404, "movement_not_found", "Movimento não encontrado")
    if original.kind not in REVERSIBLE_KINDS:
        raise http_error(
            409,
            "not_reversible",
            "Este tipo de movimento não é revertido isoladamente "
            "(transferência: faça outra transferência; reserva: libere)",
            kind=original.kind,
        )
    movement, applied = apply_movement(
        db,
        connection_id=connection_id,
        warehouse_id=original.warehouse_id,
        product=original.product_external_id,
        kind="reversal",
        quantity_delta=-original.quantity_delta,
        reserved_delta=-original.reserved_delta,
        unit_cost=original.unit_cost,
        causal_key=f"reverse:{original.id}",
        reference_type="movement",
        reference_id=str(original.id),
        operator=user.username,
        reason=reason,
        reversal_of_id=original.id,
    )
    if applied:
        audit(
            db,
            operator=user.username,
            action="inventory.reverse",
            connection_id=connection_id,
            resource="inventory",
            resource_id=original.product_external_id,
            reason=reason,
            detail={"movementId": original.id, "reversalId": movement.id},
        )
    return movement


def reserve(
    db: Session,
    user: ErpUser,
    connection_id: str,
    *,
    warehouse_id: int,
    product: str,
    quantity: Decimal,
    order_id: str | None,
    key: str,
) -> ErpInventoryReservation:
    require_erp_authority(db, connection_id)
    _warehouse(db, connection_id, warehouse_id)
    causal = f"reserve:{user.username}:{key}"
    existing = db.scalar(
        select(ErpInventoryReservation).where(
            ErpInventoryReservation.connection_id == connection_id,
            ErpInventoryReservation.causal_key == causal,
        )
    )
    if existing is not None:
        return existing
    apply_movement(
        db, connection_id=connection_id, warehouse_id=warehouse_id, product=product,
        kind="reservation", reserved_delta=quantity, causal_key=causal,
        reference_type="reservation", reference_id=order_id, operator=user.username,
    )
    row = ErpInventoryReservation(
        connection_id=connection_id,
        warehouse_id=warehouse_id,
        product_external_id=product,
        quantity=quantity,
        order_external_id=order_id,
        causal_key=causal,
        created_by=user.username,
    )
    db.add(row)
    db.flush()
    return row


def release(db: Session, user: ErpUser, connection_id: str, reservation_id: int) -> ErpInventoryReservation:
    row = db.get(ErpInventoryReservation, reservation_id)
    if row is None or row.connection_id != connection_id:
        raise http_error(404, "reservation_not_found", "Reserva não encontrada")
    if row.status != "active":
        return row  # liberar duas vezes não repete o efeito
    apply_movement(
        db, connection_id=connection_id, warehouse_id=row.warehouse_id,
        product=row.product_external_id, kind="reservation_release",
        reserved_delta=-row.quantity, causal_key=f"release:{row.id}",
        reference_type="reservation", reference_id=str(row.id), operator=user.username,
    )
    row.status = "released"
    row.closed_at = utcnow()
    db.add(row)
    return row


def receive_stock(
    db: Session,
    *,
    connection_id: str,
    warehouse_id: int,
    product: str,
    quantity: Decimal,
    unit_cost: Decimal,
    causal_key: str,
    reference_id: str,
    operator: str,
) -> str:
    """Entrada de compra. Devolve o efeito aplicado (nunca silencioso)."""
    if not is_erp_authority(get_authority(db, connection_id)):
        return "not_applied_authority_mercos"
    apply_movement(
        db, connection_id=connection_id, warehouse_id=warehouse_id, product=product,
        kind="receipt", quantity_delta=quantity, unit_cost=unit_cost, causal_key=causal_key,
        reference_type="purchase_receipt", reference_id=reference_id, operator=operator,
    )
    return "applied"


def create_warehouse(db: Session, user: ErpUser, connection_id: str, code: str, name: str) -> ErpWarehouse:
    row = ErpWarehouse(connection_id=connection_id, code=code.strip().upper(), name=name.strip())
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise http_error(409, "duplicate_code", "Código de depósito já existe") from exc
    audit(db, operator=user.username, action="warehouse.create", connection_id=connection_id,
          resource="warehouse", resource_id=row.id)
    return row


def occurred(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
