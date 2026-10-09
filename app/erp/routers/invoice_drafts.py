"""Montagem fiscal: rascunhos. Nenhuma rota aqui emite nota fiscal."""

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.erp.auth import ErpUser, require
from app.erp.config import erp_settings
from app.erp.db import erp_db
from app.erp.routers import idem
from app.erp.schemas.operations import InvoiceCancel, InvoiceDraftCreate, InvoiceDraftPatch, InvoiceVersioned
from app.erp.services import invoice_drafts as drafts

router = APIRouter(tags=["erp-invoice-drafts"])
drafts.register()


def cid() -> str:
    return erp_settings().erp_connection_id


@router.get("/sales-orders/{external_id}/invoice-drafts", summary="Rascunhos fiscais do pedido")
def list_drafts(
    external_id: str, user: ErpUser = Depends(require("invoices:read")), db: Session = Depends(erp_db)
):
    return drafts.list_drafts(db, user, cid(), external_id)


@router.post(
    "/sales-orders/{external_id}/invoice-drafts",
    status_code=201,
    summary="Cria rascunho de montagem fiscal (não emite nota)",
)
def create_draft(
    external_id: str,
    body: InvoiceDraftCreate,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("invoices:write")),
    db: Session = Depends(erp_db),
):
    return idem.run(
        db, user, f"invoice.draft_create:{external_id}", idempotency_key,
        body.model_dump(mode="json"),
        lambda: drafts.create_draft(db, user, cid(), external_id, body),
        status=201,
    )


@router.get("/invoice-drafts/{draft_id}", summary="Leitura do rascunho fiscal")
def get_draft(
    draft_id: int, user: ErpUser = Depends(require("invoices:read")), db: Session = Depends(erp_db)
):
    return drafts.get_draft(db, user, cid(), draft_id)


@router.patch("/invoice-drafts/{draft_id}", summary="Edita o rascunho com controle de versão")
def patch_draft(
    draft_id: int,
    body: InvoiceDraftPatch,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("invoices:write")),
    db: Session = Depends(erp_db),
):
    return idem.run(
        db, user, f"invoice.draft_patch:{draft_id}", idempotency_key,
        body.model_dump(mode="json"),
        lambda: drafts.patch_draft(db, user, cid(), draft_id, body),
        status=200,
    )


@router.post("/invoice-drafts/{draft_id}/organize", summary="Organiza itens até o alvo (determinístico)")
def organize_draft(
    draft_id: int,
    body: InvoiceVersioned,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("invoices:write")),
    db: Session = Depends(erp_db),
):
    return idem.run(
        db, user, f"invoice.draft_organize:{draft_id}", idempotency_key,
        body.model_dump(mode="json"),
        lambda: drafts.organize_draft(db, user, cid(), draft_id, body),
        status=200,
    )


@router.post("/invoice-drafts/{draft_id}/cancel", summary="Cancela o rascunho e libera a alocação")
def cancel_draft(
    draft_id: int,
    body: InvoiceCancel,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("invoices:write")),
    db: Session = Depends(erp_db),
):
    return idem.run(
        db, user, f"invoice.draft_cancel:{draft_id}", idempotency_key,
        body.model_dump(mode="json"),
        lambda: drafts.cancel_draft(db, user, cid(), draft_id, body),
        status=200,
    )


@router.post("/invoice-drafts/{draft_id}/issue", status_code=409, summary="Emissão fiscal (indisponível: sem emissor)")
def issue_draft(draft_id: int, user: ErpUser = Depends(require("invoices:write")), db: Session = Depends(erp_db)):
    drafts.issue_unavailable()
