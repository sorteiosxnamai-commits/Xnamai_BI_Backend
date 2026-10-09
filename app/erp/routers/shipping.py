"""Frete local: cotações cadastradas, opções e seleção (nada é contratado)."""

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.erp.auth import ErpUser, require
from app.erp.config import erp_settings
from app.erp.db import erp_db
from app.erp.routers import idem
from app.erp.schemas.operations import ShippingOptionInput, ShippingQuoteCreate, ShippingSelectInput
from app.erp.services import shipping

router = APIRouter(tags=["erp-shipping"])
shipping.register()


def cid() -> str:
    return erp_settings().erp_connection_id


@router.get("/sales-orders/{external_id}/shipping-quotes", summary="Cotações de frete do pedido")
def list_quotes(
    external_id: str, user: ErpUser = Depends(require("shipping:read")), db: Session = Depends(erp_db)
):
    return shipping.list_quotes(db, user, cid(), external_id)


@router.post(
    "/sales-orders/{external_id}/shipping-quotes",
    status_code=201,
    summary="Cria rascunho de cotação (cadastro manual; automática exige conector)",
)
def create_quote(
    external_id: str,
    body: ShippingQuoteCreate,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("shipping:write")),
    db: Session = Depends(erp_db),
):
    return idem.run(
        db, user, f"shipping.quote_create:{external_id}", idempotency_key,
        body.model_dump(mode="json"),
        lambda: shipping.create_quote(db, user, cid(), external_id, body),
        status=201,
    )


@router.get("/shipping-quotes/{quote_id}", summary="Detalhe da cotação")
def get_quote(
    quote_id: int, user: ErpUser = Depends(require("shipping:read")), db: Session = Depends(erp_db)
):
    return shipping.get_quote(db, user, cid(), quote_id)


@router.post("/shipping-quotes/{quote_id}/options", status_code=201, summary="Registra opção obtida fora do sistema")
def add_option(
    quote_id: int,
    body: ShippingOptionInput,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("shipping:write")),
    db: Session = Depends(erp_db),
):
    return idem.run(
        db, user, f"shipping.option_add:{quote_id}", idempotency_key,
        body.model_dump(mode="json"),
        lambda: shipping.add_option(db, user, cid(), quote_id, body),
        status=201,
    )


@router.post("/shipping-quotes/{quote_id}/select", summary="Seleciona a opção (seleção local, sem contratação)")
def select_option(
    quote_id: int,
    body: ShippingSelectInput,
    idempotency_key: str | None = Header(None),
    user: ErpUser = Depends(require("shipping:write")),
    db: Session = Depends(erp_db),
):
    return idem.run(
        db, user, f"shipping.select:{quote_id}", idempotency_key,
        body.model_dump(mode="json"),
        lambda: shipping.select_option(db, user, cid(), quote_id, body),
        status=200,
    )
