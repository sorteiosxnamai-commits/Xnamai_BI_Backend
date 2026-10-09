"""Entradas do painel operacional: frete, rascunho fiscal e reembolso. Decimal em todo valor."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from app.erp.schemas.commands import Strict

# --- frete ---------------------------------------------------------------------


class VolumeInput(Strict):
    weightKg: Decimal = Field(gt=0, le=Decimal("2000"), max_digits=10, decimal_places=3)
    lengthCm: Decimal = Field(gt=0, le=Decimal("600"), max_digits=8, decimal_places=2)
    widthCm: Decimal = Field(gt=0, le=Decimal("600"), max_digits=8, decimal_places=2)
    heightCm: Decimal = Field(gt=0, le=Decimal("600"), max_digits=8, decimal_places=2)


class ShippingQuoteCreate(Strict):
    volumes: list[VolumeInput] = Field(min_length=1, max_length=50)
    originZip: str | None = Field(default=None, max_length=12)
    destinationZip: str | None = Field(default=None, max_length=12)
    declaredValue: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    notes: str | None = Field(default=None, max_length=2000)
    mode: Literal["manual", "automatic"] = "manual"


class ShippingOptionInput(Strict):
    carrier: str = Field(min_length=1, max_length=200)
    service: str = Field(min_length=1, max_length=100)
    price: Decimal = Field(ge=0, max_digits=18, decimal_places=2)
    deadlineMinDays: int | None = Field(default=None, ge=0, le=365)
    deadlineMaxDays: int | None = Field(default=None, ge=0, le=365)
    validUntil: datetime | None = None
    tracking: bool | None = None
    pickupMode: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=1000)


class ShippingSelectInput(Strict):
    optionId: int = Field(ge=1)
    expectedVersion: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=1000)


# --- rascunho fiscal -----------------------------------------------------------


class InvoiceAllocation(Strict):
    sourceKey: str = Field(min_length=1, max_length=260)
    quantity: Decimal = Field(ge=0, max_digits=18, decimal_places=4)


class InvoiceDraftCreate(Strict):
    percent: Decimal = Field(gt=0, le=100, max_digits=7, decimal_places=4)
    organize: bool = False
    notes: str | None = Field(default=None, max_length=2000)


class InvoiceDraftPatch(Strict):
    expectedVersion: int = Field(ge=1)
    percent: Decimal | None = Field(default=None, gt=0, le=100, max_digits=7, decimal_places=4)
    items: list[InvoiceAllocation] | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=2000)
    acknowledgeReview: bool = False


class InvoiceVersioned(Strict):
    expectedVersion: int = Field(ge=1)


class InvoiceCancel(Strict):
    expectedVersion: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=1000)


# --- financeiro por pedido e reembolso -----------------------------------------


class OrderReceivableInput(Strict):
    firstDueDate: date
    installments: int = Field(default=1, ge=1, le=60)
    amount: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=2)
    description: str | None = Field(default=None, max_length=400)
    acknowledgeExternalTitle: bool = False


class RefundRequestCreate(Strict):
    orderId: str = Field(min_length=1, max_length=200)
    settlementId: int = Field(ge=1)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    reason: str = Field(min_length=3, max_length=2000)


class RefundVersioned(Strict):
    expectedVersion: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=1000)


class RefundReason(Strict):
    expectedVersion: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=1000)


class RefundExternalConfirm(Strict):
    expectedVersion: int = Field(ge=1)
    reference: str = Field(min_length=3, max_length=200)
    note: str | None = Field(default=None, max_length=1000)
