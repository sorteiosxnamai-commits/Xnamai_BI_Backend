"""Solicitações de reembolso. Nenhuma rota aqui devolve dinheiro: aprovar não é devolver."""

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from app.erp.auth import ErpUser, require
from app.erp.common import http_error
from app.erp.config import erp_settings
from app.erp.db import erp_db
from app.erp.routers import idem
from app.erp.schemas.operations import (
    RefundExternalConfirm,
    RefundReason,
    RefundRequestCreate,
    RefundVersioned,
)
from app.erp.services import refunds

router = APIRouter(prefix="/refund-requests", tags=["erp-refunds"])


def cid() -> str:
    return erp_settings().erp_connection_id


@router.get("", summary="Solicitações de reembolso")
def list_requests(
    status: str | None = Query(None, pattern="^(requested|approved|rejected|cancelled|external_confirmed|reversed_local)$"),
    orderId: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    user: ErpUser = Depends(require("refunds:read")),
    db: Session = Depends(erp_db),
):
    return refunds.list_requests(db, cid(), status=status, order_id=orderId, page=page, page_size=page_size, order=order)


@router.post("", status_code=201, summary="Solicita reembolso (não altera baixa nem devolve dinheiro)")
def create_request(
    body: RefundRequestCreate,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("refunds:request")),
    db: Session = Depends(erp_db),
):
    return idem.run(
        db, user, f"refund.create:{body.settlementId}", idempotency_key, body.model_dump(mode="json"),
        lambda: refunds.create_request(db, user, cid(), body), status=201,
    )


@router.get("/{request_id}", summary="Solicitação com histórico")
def get_request(
    request_id: int, user: ErpUser = Depends(require("refunds:read")), db: Session = Depends(erp_db)
):
    return refunds.get(db, cid(), request_id)


@router.post("/{request_id}/approve", summary="Aprova internamente (a devolução ainda não foi feita)")
def approve(
    request_id: int,
    body: RefundVersioned,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("refunds:approve")),
    db: Session = Depends(erp_db),
):
    return idem.run(
        db, user, f"refund.approve:{request_id}", idempotency_key, body.model_dump(mode="json"),
        lambda: refunds.approve(db, user, cid(), request_id, body), status=200,
    )


@router.post("/{request_id}/reject", summary="Rejeita a solicitação")
def reject(
    request_id: int,
    body: RefundReason,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("refunds:approve")),
    db: Session = Depends(erp_db),
):
    return idem.run(
        db, user, f"refund.reject:{request_id}", idempotency_key, body.model_dump(mode="json"),
        lambda: refunds.reject(db, user, cid(), request_id, body), status=200,
    )


@router.post("/{request_id}/cancel", summary="Cancela (quem solicitou ou quem aprova)")
def cancel(
    request_id: int,
    body: RefundReason,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("refunds:request")),
    db: Session = Depends(erp_db),
):
    return idem.run(
        db, user, f"refund.cancel:{request_id}", idempotency_key, body.model_dump(mode="json"),
        lambda: refunds.cancel(db, user, cid(), request_id, body), status=200,
    )


@router.post("/{request_id}/confirm-external", summary="Confirma manualmente devolução feita fora do sistema")
def confirm_external(
    request_id: int,
    body: RefundExternalConfirm,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("refunds:approve")),
    db: Session = Depends(erp_db),
):
    return idem.run(
        db, user, f"refund.confirm:{request_id}", idempotency_key, body.model_dump(mode="json"),
        lambda: refunds.confirm_external(db, user, cid(), request_id, body), status=200,
    )


@router.post("/{request_id}/apply-local-reversal", summary="Aplica o estorno LOCAL integral da baixa")
def apply_local_reversal(
    request_id: int,
    body: RefundVersioned,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("refunds:approve")),
    db: Session = Depends(erp_db),
):
    if not user.can("finance:settle"):
        raise http_error(403, "forbidden", "Estornar baixa exige permissão financeira de baixa")
    return idem.run(
        db, user, f"refund.reverse:{request_id}", idempotency_key, body.model_dump(mode="json"),
        lambda: refunds.apply_local_reversal(db, user, cid(), request_id, body), status=200,
    )
