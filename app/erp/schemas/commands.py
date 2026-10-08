"""Entradas (comandos) da API ERP. Dinheiro e quantidades entram como Decimal."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DOCUMENT_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789./- ")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomerFields(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    tradeName: str | None = Field(default=None, max_length=300)
    personType: Literal["F", "J"] | None = None
    document: str | None = Field(default=None, min_length=3, max_length=40)
    stateRegistration: str | None = Field(default=None, max_length=40)
    street: str | None = Field(default=None, max_length=300)
    number: str | None = Field(default=None, max_length=40)
    complement: str | None = Field(default=None, max_length=200)
    district: str | None = Field(default=None, max_length=200)
    zipCode: str | None = Field(default=None, max_length=20)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=5)
    email: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=60)
    mobile: str | None = Field(default=None, max_length=60)
    segmentId: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("document")
    @classmethod
    def document_charset(cls, value: str | None) -> str | None:
        # Documentos alfanuméricos são válidos: não se removem letras.
        if value is not None and not set(value) <= DOCUMENT_CHARS:
            raise ValueError("Documento contém caracteres inválidos")
        return value.strip() if value else value


class CustomerCreate(CustomerFields):
    name: str = Field(min_length=1, max_length=300)


class CustomerPatch(CustomerFields):
    expectedVersion: int = Field(ge=1)


class OrderItemInput(Strict):
    productId: str = Field(min_length=1, max_length=200)
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    unitPrice: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    discount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)


class OrderCreate(Strict):
    customerId: str = Field(min_length=1, max_length=200)
    items: list[OrderItemInput] = Field(min_length=1, max_length=500)
    paymentConditionId: str | None = None
    priceTableId: str | None = None
    carrierId: str | None = None
    orderTypeId: str | None = None
    sellerId: str | None = None
    notes: str | None = Field(default=None, max_length=4000)


class OrderPatch(Strict):
    expectedVersion: int = Field(ge=1)
    notes: str | None = Field(default=None, max_length=4000)
    paymentConditionId: str | None = None
    carrierId: str | None = None
    expectedDeliveryDate: date | None = None
    items: list[OrderItemInput] | None = Field(default=None, min_length=1, max_length=500)


class ReconcileInput(Strict):
    decision: Literal["check", "confirm_created", "confirm_not_created"]
    externalId: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


class ConflictResolveInput(Strict):
    resolution: Literal["use_local", "use_external"]
    note: str | None = Field(default=None, max_length=1000)


class SyncRequest(Strict):
    resource: str = "all"
    full: bool = False


class TitleInput(Strict):
    customerId: str
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    dueDate: date
    orderId: str | None = None


class OperatorInput(Strict):
    username: str = Field(min_length=1, max_length=200)
    displayName: str | None = Field(default=None, max_length=200)
    roles: list[str] = Field(default_factory=list)
    active: bool = True


class SupplierInput(Strict):
    code: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=300)
    document: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=60)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=5)
    paymentTerms: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)


class PurchaseItemInput(Strict):
    productId: str | None = Field(default=None, max_length=200)
    description: str = Field(min_length=1, max_length=400)
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    unitCost: Decimal = Field(ge=0, max_digits=18, decimal_places=4)


class PurchaseOrderInput(Strict):
    supplierId: int
    expectedDate: date | None = None
    notes: str | None = Field(default=None, max_length=4000)
    items: list[PurchaseItemInput] = Field(min_length=1, max_length=500)


class PurchaseCancelInput(Strict):
    reason: str = Field(min_length=3, max_length=1000)


class ReceiptLine(Strict):
    itemId: int
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)


class PayableInput(Strict):
    dueDate: date
    installments: int = Field(default=1, ge=1, le=60)
    categoryId: int | None = None
    costCenterId: int | None = None


class ReceiptInput(Strict):
    warehouseId: int
    invoiceNumber: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[ReceiptLine] = Field(min_length=1)
    payable: PayableInput | None = None


class WarehouseInput(Strict):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)


class AdjustmentInput(Strict):
    warehouseId: int
    productId: str = Field(min_length=1, max_length=200)
    newQuantity: Decimal = Field(ge=0, max_digits=18, decimal_places=4)
    reason: str = Field(min_length=3, max_length=1000)


class TransferInput(Strict):
    fromWarehouseId: int
    toWarehouseId: int
    productId: str = Field(min_length=1, max_length=200)
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    reason: str | None = Field(default=None, max_length=1000)


class ReservationInput(Strict):
    warehouseId: int
    productId: str = Field(min_length=1, max_length=200)
    quantity: Decimal = Field(gt=0, max_digits=18, decimal_places=4)
    orderId: str | None = None


class AuthorityInput(Strict):
    authority: Literal["mercos", "erp"]
    scope: str = ""
    reason: str = Field(min_length=3, max_length=1000)
    cutoverReconciled: bool = False


class FinAccountInput(Strict):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["cash", "bank"] = "bank"
    openingBalance: Decimal = Field(default=Decimal("0"), max_digits=18, decimal_places=2)


class FinCategoryInput(Strict):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["income", "expense"] = "expense"


class CostCenterInput(Strict):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)


class FinTitleInput(Strict):
    kind: Literal["payable", "receivable"]
    description: str = Field(min_length=1, max_length=400)
    total: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    firstDueDate: date
    installments: int = Field(default=1, ge=1, le=60)
    supplierId: int | None = None
    customerId: str | None = None
    categoryId: int | None = None
    costCenterId: int | None = None
    number: str | None = Field(default=None, max_length=60)


class SettlementInput(Strict):
    accountId: int
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    reference: str | None = Field(default=None, max_length=120)


class ReversalInput(Strict):
    reason: str = Field(min_length=3, max_length=1000)


class LoginInput(Strict):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordInput(Strict):
    currentPassword: str = Field(min_length=1, max_length=200)
    newPassword: str = Field(min_length=1, max_length=200)


class SetPasswordInput(Strict):
    """Sem `temporaryPassword`, o servidor gera uma senha temporária."""

    temporaryPassword: str | None = Field(default=None, max_length=200)
