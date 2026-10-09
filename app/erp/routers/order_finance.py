"""Financeiro por pedido: vínculo com títulos e baixas, métricas e relatório."""

from datetime import date

from fastapi import APIRouter, Depends, Header
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.erp.auth import ErpUser, require
from app.erp.common import http_error
from app.erp.config import erp_settings
from app.erp.db import erp_db
from app.erp.routers import idem
from app.erp.schemas.operations import OrderReceivableInput
from app.erp.services import order_finance
from app.erp.services import order_operations as oo

router = APIRouter(tags=["erp-order-finance"])
order_finance.register()


def cid() -> str:
    return erp_settings().erp_connection_id


@router.get("/sales-orders/{external_id}/finance", summary="Obrigação, parcelas e baixas ligadas ao pedido")
def order_finance_view(
    external_id: str, user: ErpUser = Depends(require("finance:read")), db: Session = Depends(erp_db)
):
    return order_finance.order_finance(db, user, cid(), external_id)


@router.post(
    "/sales-orders/{external_id}/receivable",
    status_code=201,
    summary="Cria o título local a receber do pedido (origem sales_order)",
)
def create_receivable(
    external_id: str,
    body: OrderReceivableInput,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("finance:write")),
    db: Session = Depends(erp_db),
):
    return idem.run(
        db, user, f"finance.order_receivable:{external_id}", idempotency_key, body.model_dump(mode="json"),
        lambda: order_finance.create_order_receivable(db, user, cid(), external_id, body), status=201,
    )


@router.get("/finance/orders-summary", summary="Vendas x caixa x a receber x reembolsos (fórmulas documentadas)")
def orders_summary(
    dateFrom: date | None = None,
    dateTo: date | None = None,
    user: ErpUser = Depends(require("finance:read")),
    db: Session = Depends(erp_db),
):
    if dateFrom and dateTo and dateTo < dateFrom:
        raise http_error(422, "invalid_period", "A data final não pode ser anterior à inicial")
    return order_finance.metrics(db, cid(), dateFrom, dateTo)


@router.get("/finance/orders-report.csv", summary="Relatório CSV pelo escopo e filtros autorizados")
def orders_report(
    search: str | None = None,
    kind: str | None = None,
    customerId: str | None = None,
    financeStatus: str | None = None,
    dateFrom: date | None = None,
    dateTo: date | None = None,
    user: ErpUser = Depends(require("finance:read")),
    db: Session = Depends(erp_db),
):
    extra = (("financeStatus", financeStatus),) if financeStatus else ()
    filters = oo.OrderFilters(
        search=search, kind=kind, customer_id=customerId, date_from=dateFrom, date_to=dateTo, extra=extra
    )
    body, truncated = order_finance.report_csv(db, user, cid(), filters)
    return Response(
        content="﻿" + body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="financeiro-pedidos.csv"',
            "X-Report-Truncated": "true" if truncated else "false",
        },
    )
