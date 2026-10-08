"""Financeiro local: títulos, parcelas, baixas, estornos e fluxo de caixa."""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.erp.auth import ErpUser, require
from app.erp.common import http_error, iso, money, paginate
from app.erp.config import erp_settings
from app.erp.db import erp_db
from app.erp.models.local import (
    ErpCostCenter,
    ErpFinAccount,
    ErpFinCategory,
    ErpFinInstallment,
    ErpFinSettlement,
    ErpFinTitle,
)
from app.erp.routers import idem
from app.erp.schemas.commands import (
    CostCenterInput,
    FinAccountInput,
    FinCategoryInput,
    FinTitleInput,
    PurchaseCancelInput,
    ReversalInput,
    SettlementInput,
)
from app.erp.services import finance

router = APIRouter(prefix="/finance", tags=["erp-finance"])
PAGE = Query(1, ge=1)
PAGE_SIZE = Query(25, ge=1, le=100)


def cid() -> str:
    return erp_settings().erp_connection_id


def _installment(i: ErpFinInstallment) -> dict:
    return {
        "id": i.id, "number": i.number, "dueDate": i.due_date.isoformat(),
        "amount": money(i.amount), "settledAmount": money(i.settled_amount),
        "openAmount": money(i.amount - i.settled_amount), "status": i.status,
        "overdue": i.status != "settled" and i.due_date < date.today(),
    }


def _title(t: ErpFinTitle, installments=None) -> dict:
    data = {
        "id": t.id, "kind": t.kind, "number": t.number, "description": t.description,
        "supplierId": t.supplier_id, "customerId": t.customer_external_id,
        "categoryId": t.category_id, "costCenterId": t.cost_center_id,
        "originType": t.origin_type, "originRef": t.origin_ref,
        "total": money(t.total), "status": t.status, "createdBy": t.created_by,
        "createdAt": iso(t.created_at),
        "scope": "Financeiro local; não é título Mercos do cliente",
    }
    if installments is not None:
        data["installments"] = [_installment(i) for i in installments]
    return data


def _settlement(s: ErpFinSettlement) -> dict:
    return {
        "id": s.id, "installmentId": s.installment_id, "accountId": s.account_id,
        "kind": s.kind, "amount": money(s.amount), "settledAt": iso(s.settled_at),
        "reversalOfId": s.reversal_of_id, "reference": s.reference, "reason": s.reason,
        "operator": s.operator,
    }


@router.get("/accounts", summary="Contas financeiras e saldos")
def list_accounts(user: ErpUser = Depends(require("finance:read")), db: Session = Depends(erp_db)):
    rows = db.scalars(
        select(ErpFinAccount).where(ErpFinAccount.connection_id == cid()).order_by(ErpFinAccount.code)
    ).all()
    return {
        "items": [
            {"id": a.id, "code": a.code, "name": a.name, "kind": a.kind, "active": a.active,
             "openingBalance": money(a.opening_balance),
             "balance": money(finance.account_balance(db, a))}
            for a in rows
        ]
    }


@router.post("/accounts", status_code=201, summary="Cria conta financeira")
def create_account(
    body: FinAccountInput,
    user: ErpUser = Depends(require("finance:write")),
    db: Session = Depends(erp_db),
):
    row = finance.create_account(
        db, user, cid(), code=body.code, name=body.name, kind=body.kind, opening=body.openingBalance
    )
    db.commit()
    return {"id": row.id, "code": row.code, "name": row.name}


@router.get("/categories", summary="Categorias financeiras")
def list_categories(user: ErpUser = Depends(require("finance:read")), db: Session = Depends(erp_db)):
    rows = db.scalars(select(ErpFinCategory).where(ErpFinCategory.connection_id == cid())).all()
    return {"items": [{"id": c.id, "code": c.code, "name": c.name, "kind": c.kind} for c in rows]}


@router.post("/categories", status_code=201, summary="Cria categoria financeira")
def create_category(
    body: FinCategoryInput,
    user: ErpUser = Depends(require("finance:write")),
    db: Session = Depends(erp_db),
):
    row = finance.create_category(db, user, cid(), code=body.code, name=body.name, kind=body.kind)
    db.commit()
    return {"id": row.id, "code": row.code, "name": row.name}


@router.get("/cost-centers", summary="Centros de custo")
def list_cost_centers(user: ErpUser = Depends(require("finance:read")), db: Session = Depends(erp_db)):
    rows = db.scalars(select(ErpCostCenter).where(ErpCostCenter.connection_id == cid())).all()
    return {"items": [{"id": c.id, "code": c.code, "name": c.name} for c in rows]}


@router.post("/cost-centers", status_code=201, summary="Cria centro de custo")
def create_cost_center(
    body: CostCenterInput,
    user: ErpUser = Depends(require("finance:write")),
    db: Session = Depends(erp_db),
):
    row = finance.create_cost_center(db, user, cid(), code=body.code, name=body.name)
    db.commit()
    return {"id": row.id, "code": row.code, "name": row.name}


@router.get("/titles", summary="Títulos locais a pagar/receber")
def list_titles(
    kind: str | None = None,
    status: str | None = None,
    page: int = PAGE,
    page_size: int = PAGE_SIZE,
    order: str = Query("desc", pattern="^(asc|desc)$"),
    user: ErpUser = Depends(require("finance:read")),
    db: Session = Depends(erp_db),
):
    M = ErpFinTitle
    query = select(M).where(M.connection_id == cid())
    if kind:
        query = query.where(M.kind == kind)
    if status:
        query = query.where(M.status == status)
    result, key, direction = paginate(
        db, query, id_column=M.id, sort_columns={"createdAt": M.created_at, "total": M.total},
        sort=None, default_sort="createdAt", order=order, page=page, page_size=page_size,
    )
    return result.envelope(_title, sort=key, order=direction, filters={"kind": kind, "status": status})


@router.get("/titles/{title_id}", summary="Detalhe do título com parcelas")
def get_title(
    title_id: int, user: ErpUser = Depends(require("finance:read")), db: Session = Depends(erp_db)
):
    title = db.get(ErpFinTitle, title_id)
    if title is None or title.connection_id != cid():
        raise http_error(404, "title_not_found", "Título não encontrado")
    installments = db.scalars(
        select(ErpFinInstallment).where(ErpFinInstallment.title_id == title.id)
        .order_by(ErpFinInstallment.number)
    ).all()
    settlements = db.scalars(
        select(ErpFinSettlement).where(
            ErpFinSettlement.installment_id.in_([i.id for i in installments])
        ).order_by(ErpFinSettlement.id)
    ).all()
    return {**_title(title, installments), "settlements": [_settlement(s) for s in settlements]}


@router.post("/titles", status_code=201, summary="Cria título local (parcelado)")
def create_title(
    body: FinTitleInput,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("finance:write")),
    db: Session = Depends(erp_db),
):
    def handler():
        title, _ = finance.create_title(
            db, user, cid(), kind=body.kind, description=body.description, total=body.total,
            first_due=body.firstDueDate, installments=body.installments,
            supplier_id=body.supplierId, customer_id=body.customerId,
            category_id=body.categoryId, cost_center_id=body.costCenterId, number=body.number,
            origin_type="manual",
            causal_key=f"manual:{user.username}:{idempotency_key}",
        )
        installments = db.scalars(
            select(ErpFinInstallment).where(ErpFinInstallment.title_id == title.id)
            .order_by(ErpFinInstallment.number)
        ).all()
        return _title(title, installments)

    return idem.run(db, user, "finance.title", idempotency_key, body.model_dump(mode="json"), handler)


@router.post("/titles/{title_id}/cancel", summary="Cancela título sem baixas")
def cancel_title(
    title_id: int,
    body: PurchaseCancelInput,
    user: ErpUser = Depends(require("finance:write")),
    db: Session = Depends(erp_db),
):
    title = finance.cancel_title(db, user, cid(), title_id, body.reason)
    db.commit()
    return _title(title)


@router.post("/installments/{installment_id}/settlements", status_code=201, summary="Baixa de parcela")
def settle_installment(
    installment_id: int,
    body: SettlementInput,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("finance:settle")),
    db: Session = Depends(erp_db),
):
    def handler():
        row = finance.settle(
            db, user, cid(), installment_id=installment_id, account_id=body.accountId,
            amount=body.amount, reference=body.reference, key=idempotency_key or "",
        )
        return _settlement(row)

    return idem.run(
        db, user, f"finance.settle:{installment_id}", idempotency_key,
        body.model_dump(mode="json"), handler,
    )


@router.post("/settlements/{settlement_id}/reversals", status_code=201, summary="Estorna uma baixa")
def reverse_settlement(
    settlement_id: int,
    body: ReversalInput,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("finance:settle")),
    db: Session = Depends(erp_db),
):
    def handler():
        row = finance.reverse(
            db, user, cid(), settlement_id=settlement_id, reason=body.reason,
            key=idempotency_key or "",
        )
        return _settlement(row)

    return idem.run(
        db, user, f"finance.reverse:{settlement_id}", idempotency_key,
        body.model_dump(mode="json"), handler,
    )


@router.get("/cash-flow", summary="Fluxo de caixa previsto x realizado")
def cash_flow(
    dateFrom: date | None = None,
    dateTo: date | None = None,
    user: ErpUser = Depends(require("finance:read")),
    db: Session = Depends(erp_db),
):
    start = dateFrom or date.today()
    end = dateTo or (start + timedelta(days=30))
    if end < start or (end - start).days > 366:
        raise http_error(422, "invalid_range", "Período inválido (máximo de 366 dias)")
    return finance.cash_flow(db, cid(), start, end)
