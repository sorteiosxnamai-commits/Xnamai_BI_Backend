"""Financeiro local: títulos a pagar/receber, parcelas, baixas e estornos.

Não confunde com o título Mercos do cliente (`erp_external_titles`). A baixa usa
UPDATE condicional na parcela (`settled_amount + :x <= amount`): duas baixas
simultâneas nunca passam do valor em aberto. Estorno é um novo registro causal,
no máximo um por baixa; nunca edição do histórico.
"""

import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.erp.audit import audit
from app.erp.auth import ErpUser
from app.erp.common import BR_TZ, as_utc, http_error, utcnow
from app.erp.models.local import (
    ErpCostCenter,
    ErpFinAccount,
    ErpFinCategory,
    ErpFinInstallment,
    ErpFinSettlement,
    ErpFinTitle,
)

CENT = Decimal("0.01")


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def split_amount(total: Decimal, parts: int) -> list[Decimal]:
    """Divide em centavos; a diferença de arredondamento vai à última parcela."""
    base = (total / parts).quantize(CENT, rounding=ROUND_HALF_UP)
    amounts = [base] * parts
    amounts[-1] = total - base * (parts - 1)
    return amounts


def _unique(db: Session, row, message: str):
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise http_error(409, "duplicate_code", message) from exc
    return row


def create_account(db, user: ErpUser, connection_id, *, code, name, kind, opening):
    row = _unique(
        db,
        ErpFinAccount(connection_id=connection_id, code=code.strip().upper(), name=name,
                      kind=kind, opening_balance=opening),
        "Código de conta já existe",
    )
    audit(db, operator=user.username, action="finance.account.create",
          connection_id=connection_id, resource="fin_account", resource_id=row.id)
    return row


def create_category(db, user: ErpUser, connection_id, *, code, name, kind):
    row = _unique(
        db,
        ErpFinCategory(connection_id=connection_id, code=code.strip().upper(), name=name, kind=kind),
        "Código de categoria já existe",
    )
    audit(db, operator=user.username, action="finance.category.create",
          connection_id=connection_id, resource="fin_category", resource_id=row.id)
    return row


def create_cost_center(db, user: ErpUser, connection_id, *, code, name):
    row = _unique(
        db,
        ErpCostCenter(connection_id=connection_id, code=code.strip().upper(), name=name),
        "Código de centro de custo já existe",
    )
    audit(db, operator=user.username, action="finance.cost_center.create",
          connection_id=connection_id, resource="cost_center", resource_id=row.id)
    return row


def create_title(
    db: Session,
    user: ErpUser,
    connection_id: str,
    *,
    kind: str,
    description: str,
    total: Decimal,
    first_due: date,
    installments: int,
    supplier_id: int | None = None,
    customer_id: str | None = None,
    category_id: int | None = None,
    cost_center_id: int | None = None,
    number: str | None = None,
    origin_type: str | None = None,
    origin_ref: str | None = None,
    causal_key: str | None = None,
) -> tuple[ErpFinTitle, bool]:
    if causal_key:
        existing = db.scalar(
            select(ErpFinTitle).where(
                ErpFinTitle.connection_id == connection_id,
                ErpFinTitle.causal_key == causal_key,
            )
        )
        if existing is not None:
            return existing, False
    if kind == "payable" and not supplier_id and not origin_type:
        raise http_error(422, "supplier_required", "Conta a pagar exige fornecedor")
    if kind == "receivable" and not customer_id:
        raise http_error(422, "customer_required", "Conta a receber exige cliente")
    title = ErpFinTitle(
        connection_id=connection_id, kind=kind, number=number, description=description,
        supplier_id=supplier_id, customer_external_id=customer_id, category_id=category_id,
        cost_center_id=cost_center_id, origin_type=origin_type, origin_ref=origin_ref,
        causal_key=causal_key, total=total, issued_date=utcnow().date(), created_by=user.username,
    )
    db.add(title)
    db.flush()
    for index, amount in enumerate(split_amount(total, installments), start=1):
        db.add(
            ErpFinInstallment(
                title_id=title.id,
                number=index,
                due_date=add_months(first_due, index - 1),
                amount=amount,
            )
        )
    db.flush()
    audit(db, operator=user.username, action="finance.title.create", connection_id=connection_id,
          resource="fin_title", resource_id=title.id,
          detail={"kind": kind, "total": str(total), "installments": installments})
    return title, True


def _refresh_status(db: Session, installment: ErpFinInstallment) -> None:
    db.refresh(installment)
    if installment.settled_amount <= 0:
        installment.status = "open"
    elif installment.settled_amount >= installment.amount:
        installment.status = "settled"
    else:
        installment.status = "partial"
    db.add(installment)
    db.flush()
    title = db.get(ErpFinTitle, installment.title_id)
    statuses = set(
        db.scalars(
            select(ErpFinInstallment.status).where(ErpFinInstallment.title_id == title.id)
        )
    )
    if title.status != "cancelled":
        if statuses == {"settled"}:
            title.status = "settled"
        elif statuses == {"open"}:
            title.status = "open"
        else:
            title.status = "partially_settled"
        db.add(title)


def settle(
    db: Session,
    user: ErpUser,
    connection_id: str,
    *,
    installment_id: int,
    account_id: int,
    amount: Decimal,
    reference: str | None,
    key: str,
) -> ErpFinSettlement:
    causal = f"settle:{user.username}:{key}"
    existing = db.scalar(
        select(ErpFinSettlement).where(
            ErpFinSettlement.connection_id == connection_id,
            ErpFinSettlement.causal_key == causal,
        )
    )
    if existing is not None:
        return existing
    installment = db.get(ErpFinInstallment, installment_id)
    # trava o título: baixas simultâneas de parcelas diferentes recalculam o status em série
    title = db.get(ErpFinTitle, installment.title_id, with_for_update=True) if installment else None
    if title is None or title.connection_id != connection_id:
        raise http_error(404, "installment_not_found", "Parcela não encontrada")
    if title.status == "cancelled":
        raise http_error(409, "title_cancelled", "Título cancelado não recebe baixa")
    account = db.get(ErpFinAccount, account_id)
    if account is None or account.connection_id != connection_id or not account.active:
        raise http_error(404, "account_not_found", "Conta financeira não encontrada ou inativa")
    result = db.execute(
        update(ErpFinInstallment)
        .where(
            ErpFinInstallment.id == installment_id,
            ErpFinInstallment.settled_amount + amount <= ErpFinInstallment.amount,
        )
        .values(settled_amount=ErpFinInstallment.settled_amount + amount)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise http_error(
            409, "over_settlement", "Baixa acima do valor em aberto da parcela"
        )
    row = ErpFinSettlement(
        connection_id=connection_id, installment_id=installment_id, account_id=account_id,
        kind="payment", amount=amount, causal_key=causal, reference=reference,
        operator=user.username,
    )
    db.add(row)
    db.flush()
    _refresh_status(db, installment)
    audit(db, operator=user.username, action="finance.settle", connection_id=connection_id,
          resource="fin_installment", resource_id=installment_id,
          detail={"amount": str(amount), "settlementId": row.id})
    return row


def reverse(
    db: Session,
    user: ErpUser,
    connection_id: str,
    *,
    settlement_id: int,
    reason: str,
    key: str,
) -> ErpFinSettlement:
    original = db.get(ErpFinSettlement, settlement_id)
    if original is None or original.connection_id != connection_id:
        raise http_error(404, "settlement_not_found", "Baixa não encontrada")
    if original.kind != "payment":
        raise http_error(409, "not_reversible", "Somente baixas podem ser estornadas")
    parent = db.get(ErpFinInstallment, original.installment_id)
    db.get(ErpFinTitle, parent.title_id, with_for_update=True)
    causal = f"reverse:{original.id}"
    existing = db.scalar(
        select(ErpFinSettlement).where(
            ErpFinSettlement.connection_id == connection_id,
            ErpFinSettlement.causal_key == causal,
        )
    )
    if existing is not None:
        return existing  # no máximo um estorno por baixa
    result = db.execute(
        update(ErpFinInstallment)
        .where(
            ErpFinInstallment.id == original.installment_id,
            ErpFinInstallment.settled_amount - original.amount >= 0,
        )
        .values(settled_amount=ErpFinInstallment.settled_amount - original.amount)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise http_error(409, "reversal_exceeds_settled", "Estorno maior que o valor baixado")
    row = ErpFinSettlement(
        connection_id=connection_id, installment_id=original.installment_id,
        account_id=original.account_id, kind="reversal", amount=original.amount,
        reversal_of_id=original.id, causal_key=causal, reason=reason, operator=user.username,
        reference=f"estorno:{original.id}",
    )
    db.add(row)
    db.flush()
    _refresh_status(db, db.get(ErpFinInstallment, original.installment_id))
    audit(db, operator=user.username, action="finance.reverse", connection_id=connection_id,
          resource="fin_settlement", resource_id=original.id, reason=reason,
          detail={"amount": str(original.amount), "reversalId": row.id, "idempotencyKey": key})
    return row


def cancel_title(db: Session, user: ErpUser, connection_id: str, title_id: int, reason: str) -> ErpFinTitle:
    title = db.get(ErpFinTitle, title_id, with_for_update=True)
    if title is None or title.connection_id != connection_id:
        raise http_error(404, "title_not_found", "Título não encontrado")
    settled = db.scalar(
        select(func.coalesce(func.sum(ErpFinInstallment.settled_amount), 0)).where(
            ErpFinInstallment.title_id == title.id
        )
    )
    if settled and Decimal(settled) > 0:
        raise http_error(409, "has_settlements", "Estorne as baixas antes de cancelar o título")
    title.status = "cancelled"
    title.cancelled_at = utcnow()
    db.add(title)
    audit(db, operator=user.username, action="finance.title.cancel", connection_id=connection_id,
          resource="fin_title", resource_id=title.id, reason=reason)
    return title


def cash_flow(
    db: Session, connection_id: str, date_from: date, date_to: date
) -> dict:
    """Fluxo previsto (parcelas em aberto por vencimento) e realizado (baixas)."""
    planned: dict[str, Decimal] = {}
    rows = db.execute(
        select(ErpFinInstallment, ErpFinTitle.kind)
        .join(ErpFinTitle, ErpFinTitle.id == ErpFinInstallment.title_id)
        .where(
            ErpFinTitle.connection_id == connection_id,
            ErpFinTitle.status != "cancelled",
            ErpFinInstallment.due_date >= date_from,
            ErpFinInstallment.due_date <= date_to,
        )
    ).all()
    for installment, kind in rows:
        open_amount = installment.amount - installment.settled_amount
        if open_amount <= 0:
            continue
        sign = Decimal(1) if kind == "receivable" else Decimal(-1)
        label = installment.due_date.isoformat()
        planned[label] = planned.get(label, Decimal(0)) + sign * open_amount
    realized: dict[str, Decimal] = {}
    # dia civil de Brasília: uma baixa às 22h BRT não pode cair no dia seguinte (UTC)
    # limites convertidos para UTC antes de ligar ao SQL (o banco guarda UTC)
    start = datetime.combine(date_from, time.min, tzinfo=BR_TZ).astimezone(timezone.utc)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=BR_TZ).astimezone(timezone.utc)
    settlements = db.execute(
        select(ErpFinSettlement, ErpFinTitle.kind)
        .join(ErpFinInstallment, ErpFinInstallment.id == ErpFinSettlement.installment_id)
        .join(ErpFinTitle, ErpFinTitle.id == ErpFinInstallment.title_id)
        .where(
            ErpFinSettlement.connection_id == connection_id,
            ErpFinSettlement.settled_at >= start,
            ErpFinSettlement.settled_at < end,
        )
    ).all()
    for settlement, kind in settlements:
        sign = Decimal(1) if kind == "receivable" else Decimal(-1)
        if settlement.kind == "reversal":
            sign = -sign
        label = as_utc(settlement.settled_at).astimezone(BR_TZ).date().isoformat()
        realized[label] = realized.get(label, Decimal(0)) + sign * settlement.amount
    days = sorted(set(planned) | set(realized))
    return {
        "from": date_from.isoformat(),
        "to": date_to.isoformat(),
        "days": [
            {
                "date": label,
                "planned": format(planned.get(label, Decimal(0)), "f"),
                "realized": format(realized.get(label, Decimal(0)), "f"),
            }
            for label in days
        ],
        "totals": {
            "planned": format(sum(planned.values(), Decimal(0)), "f"),
            "realized": format(sum(realized.values(), Decimal(0)), "f"),
        },
    }


def account_balance(db: Session, account: ErpFinAccount) -> Decimal:
    rows = db.execute(
        select(ErpFinSettlement, ErpFinTitle.kind)
        .join(ErpFinInstallment, ErpFinInstallment.id == ErpFinSettlement.installment_id)
        .join(ErpFinTitle, ErpFinTitle.id == ErpFinInstallment.title_id)
        .where(ErpFinSettlement.account_id == account.id)
    ).all()
    balance = Decimal(account.opening_balance)
    for settlement, kind in rows:
        sign = Decimal(1) if kind == "receivable" else Decimal(-1)
        if settlement.kind == "reversal":
            sign = -sign
        balance += sign * settlement.amount
    return balance
