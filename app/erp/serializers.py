"""Serialização para a API ERP.

Dinheiro sai como string decimal; instantes em UTC ISO-8601 (a apresentação em
America/Sao_Paulo é do frontend); datas sem hora saem como data. Dados
pessoais, documentos e links financeiros dependem de permissão. O payload bruto
de origem nunca sai por estes serializadores.
"""

from datetime import date
from decimal import Decimal
from typing import Any

from app.erp.auth import ErpUser
from app.erp.common import iso, money, quantity


def mask(value: str | None, keep: int = 2) -> str | None:
    if not value:
        return value
    if keep <= 0 or len(value) <= keep:
        return "•" * len(value)
    return "•" * (len(value) - keep) + value[-keep:]


def _d(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _meta(row: Any) -> dict:
    return {
        "version": row.version,
        "sourceUpdatedAt": iso(row.source_updated_at),
        "sourceDeleted": bool(row.source_deleted),
        "capturedAt": iso(row.captured_at),
    }


def customer(row: Any, user: ErpUser, *, detail: bool = False, contacts=None, addresses=None) -> dict:
    pii = user.can("pii:read")
    data = {
        "id": row.external_id,
        "localId": row.id,
        "name": row.name,
        "tradeName": row.trade_name,
        "personType": row.person_type,
        "document": row.document if pii else mask(row.document),
        "city": row.city,
        "state": row.state,
        "email": row.email if pii else mask(row.email, 0),
        "phone": row.phone if pii else mask(row.phone),
        "segmentId": row.segment_external_id,
        "sellerId": row.seller_external_id,
        "blocked": row.blocked,
        "active": row.active,
        "piiRestricted": not pii,
        **_meta(row),
    }
    if detail:
        data.update(
            stateRegistration=row.state_registration if pii else None,
            suframa=row.suframa if pii else None,
            street=row.street if pii else None,
            number=row.number if pii else None,
            complement=row.complement if pii else None,
            district=row.district if pii else None,
            zipCode=row.zip_code if pii else None,
            mobile=row.mobile if pii else mask(row.mobile),
            blockReason=row.block_reason,
            creditLimit=money(row.credit_limit),
            notes=row.notes if pii else None,
            extras=row.extras if pii else None,
            sourceCreatedAt=iso(row.source_created_at),
            contacts=[
                {
                    "id": c.external_id,
                    "name": c.name,
                    "role": c.role,
                    "email": c.email if pii else mask(c.email, 0),
                    "phone": c.phone if pii else mask(c.phone),
                    "mobile": c.mobile if pii else mask(c.mobile),
                }
                for c in contacts or []
            ],
            addresses=[
                {
                    "id": a.external_id,
                    "kind": a.kind,
                    "street": a.street if pii else None,
                    "number": a.number if pii else None,
                    "complement": a.complement if pii else None,
                    "district": a.district if pii else None,
                    "zipCode": a.zip_code if pii else None,
                    "city": a.city,
                    "state": a.state,
                }
                for a in addresses or []
            ],
        )
    return data


def product(row: Any, *, detail: bool = False, variants=None) -> dict:
    data = {
        "id": row.external_id,
        "localId": row.id,
        "code": row.code,
        "name": row.name,
        "unit": row.unit,
        "categoryId": row.category_external_id,
        "kind": row.kind,
        "sellable": row.sellable,
        "listPrice": money(row.list_price),
        "minimumPrice": money(row.minimum_price),
        "externalStock": quantity(row.external_stock),
        "active": row.active,
        **_meta(row),
    }
    if detail:
        data.update(
            commissionPercent=quantity(row.commission_percent),
            ipiPercent=quantity(row.ipi_percent),
            ncm=row.ncm,
            multiple=quantity(row.multiple),
            grossWeight=quantity(row.gross_weight),
            width=quantity(row.width),
            height=quantity(row.height),
            length=quantity(row.length),
            imageHashes=row.image_hashes,
            notes=row.notes,
            cost=None,  # custo desconhecido permanece desconhecido
            variants=[
                {
                    "id": v.external_id,
                    "code": v.code,
                    "name": v.name,
                    "attributes": v.attributes,
                    "externalStock": quantity(v.external_stock),
                    "price": money(v.price),
                }
                for v in variants or []
            ],
        )
    return data


def product_price(row: Any) -> dict:
    return {
        "id": row.external_id,
        "productId": row.product_external_id,
        "priceTableId": row.price_table_external_id,
        "price": money(row.price),
        **_meta(row),
    }


def order(row: Any, user: ErpUser, *, detail: bool = False, items=None) -> dict:
    data = {
        "id": row.external_id,
        "localId": row.id,
        "number": row.number,
        "kind": row.kind,
        "customerId": row.customer_external_id,
        "sellerId": row.seller_external_id,
        "issuedAt": iso(row.issued_at),
        "issueDate": _d(row.issue_date),
        "netTotal": money(row.net_total),
        "grossTotal": money(row.gross_total),
        "discountTotal": money(row.discount_total),
        "itemCount": row.item_count,
        "itemsComplete": row.items_complete,
        # Estados separados: nunca colapsar em um único "status".
        "statuses": {
            "commercial": row.commercial_status,
            "billing": row.billing_status,
            "fulfillment": row.fulfillment_status,
            "payment": row.payment_status,
        },
        **_meta(row),
    }
    pii = user.can("pii:read")
    if detail:
        data.update(
            orderTypeId=row.order_type_external_id,
            paymentConditionId=row.payment_condition_external_id,
            priceTableId=row.price_table_external_id,
            carrierId=row.carrier_external_id,
            commercialPolicyId=row.commercial_policy_external_id,
            expectedDeliveryDate=_d(row.expected_delivery_date),
            freightTotal=money(row.freight_total),
            # texto livre, endereço de entrega e extras podem conter dados pessoais
            notes=row.notes if pii else None,
            shippingAddress=row.shipping_address if pii else None,
            extras=row.extras if pii else None,
            piiRestricted=not pii,
            sourceCreatedAt=iso(row.source_created_at),
            incomplete=not row.items_complete,
            incompleteReason=(
                None if row.items_complete
                else "Itens não vieram na listagem v2 e o detalhe por ID não foi obtido"
            ),
            items=[
                {
                    "id": i.external_id,
                    "position": i.position,
                    "productId": i.product_external_id,
                    "code": i.code,
                    "name": i.name,
                    "quantity": quantity(i.quantity),
                    "listUnitPrice": money(i.list_unit_price),
                    "unitPrice": money(i.unit_price),
                    "discount": money(i.discount),
                    "total": money(i.total),
                    "excluded": i.excluded,
                    "notes": i.notes,
                }
                for i in items or []
            ],
        )
    return data


CATALOG_EXTRA = {
    "categories": ("parent_external_id", "represented_external_id"),
    "segments": (),
    "order-types": (),
    "payment-conditions": (
        "minimum_order_value", "consider_credit_limit", "available_b2b", "represented_external_id",
    ),
    "price-tables": (
        "price_type", "percentage", "surcharge_percent", "discount_percent", "represented_external_id",
    ),
    "carriers": ("document", "phone", "email", "city", "state"),
    "commercial-policies": ("slug", "valid_from", "valid_to", "id_history"),
    "users": ("email", "is_admin", "access_blocked"),
}


CATALOG_PII_ATTRS = {"document": 2, "phone": 2, "email": 0}


def catalog_entry(resource: str, row: Any, user: Any) -> dict:
    pii = user.can("pii:read")
    data = {
        "id": row.external_id,
        "localId": row.id,
        "name": row.name,
        "active": row.active,
        **_meta(row),
    }
    for attr in CATALOG_EXTRA.get(resource, ()):
        value = getattr(row, attr, None)
        if isinstance(value, Decimal):
            value = quantity(value)
        elif isinstance(value, date):
            value = value.isoformat()
        elif attr in CATALOG_PII_ATTRS and not pii:
            value = mask(value, CATALOG_PII_ATTRS[attr])
        data[attr] = value
    return data


PII_KEYS = {
    "document", "email", "phone", "mobile", "street", "number", "complement", "district",
    "zipCode", "stateRegistration", "notes", "zip_code", "state_registration", "emails",
    "telefone", "celular", "cnpj", "cpf", "rua", "observacao", "observacoes",
}


def strip_pii(value: Any) -> Any:
    """Remove campos pessoais de estruturas livres (intenção, conflito)."""
    if isinstance(value, dict):
        return {k: ("[restrito]" if k in PII_KEYS else strip_pii(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_pii(v) for v in value]
    return value


def operation(op: Any, user: ErpUser | None = None, *, detail: bool = False) -> dict:
    data = {
        "operationId": op.id,
        "kind": op.kind,
        "status": op.status,
        "targetResource": op.target_resource,
        "targetId": op.target_external_id,
        "externalId": op.external_id,
        "operator": op.operator,
        "attempts": op.attempts,
        "errorCode": op.error_code,
        "error": op.error,
        "nextAttemptAt": iso(op.next_attempt_at),
        "dispatchStartedAt": iso(op.dispatch_started_at),
        "completedAt": iso(op.completed_at),
        "mirrorConfirmedAt": iso(op.mirror_confirmed_at),
        "createdAt": iso(op.created_at),
        "updatedAt": iso(op.updated_at),
        "statusUrl": f"/api/v1/erp/integration/operations/{op.id}",
        # "Sincronizado com Mercos" só depois do retorno da origem.
        "synchronized": op.mirror_confirmed_at is not None,
    }
    if detail:
        data.update(
            idempotencyKey=op.idempotency_key,
            expectedVersion=op.expected_version,
            responseStatus=op.response_status,
            reconcileEvidence=op.reconcile_evidence,
            correlationId=op.correlation_id,
            intent=(
                (op.payload.get("dto") if user is None or user.can("pii:read") else strip_pii(op.payload.get("dto")))
                if isinstance(op.payload, dict)
                else None
            ),
            canReconcile=op.status == "unknown",
            canRetry=False,  # unknown e failed nunca reentram na fila automaticamente
        )
    return data
