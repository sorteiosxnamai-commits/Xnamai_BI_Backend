"""Referências pendentes: IDs recebidos cujo alvo ainda não foi sincronizado.

Nada é fabricado; a referência fica explícita como pendente e some quando o
recurso dependente chega na próxima carga.
"""

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.erp.models import commercial as m


def _count(db: Session, connection_id: str, source_col, target_model, key_col, *extra) -> int:
    parent = source_col.class_
    return int(
        db.scalar(
            select(func.count())
            .select_from(parent)
            .where(
                parent.connection_id == connection_id,
                source_col.is_not(None),
                *extra,
                ~exists().where(
                    target_model.connection_id == connection_id,
                    key_col == source_col,
                ),
            )
        )
        or 0
    )


def pending_references(db: Session, connection_id: str) -> list[dict]:
    checks = [
        ("orders", "customerId", "customers", m.ErpSalesOrder.customer_external_id,
         m.ErpCustomer, m.ErpCustomer.external_id),
        ("orders", "sellerId", "users", m.ErpSalesOrder.seller_external_id,
         m.ErpSeller, m.ErpSeller.external_id),
        ("orders", "paymentConditionId", "payment-conditions",
         m.ErpSalesOrder.payment_condition_external_id, m.ErpPaymentCondition,
         m.ErpPaymentCondition.external_id),
        ("orders", "priceTableId", "price-tables", m.ErpSalesOrder.price_table_external_id,
         m.ErpPriceTable, m.ErpPriceTable.external_id),
        ("orders", "carrierId", "carriers", m.ErpSalesOrder.carrier_external_id,
         m.ErpCarrier, m.ErpCarrier.external_id),
        ("customers", "segmentId", "segments", m.ErpCustomer.segment_external_id,
         m.ErpSegment, m.ErpSegment.external_id),
        ("products", "categoryId", "categories", m.ErpProduct.category_external_id,
         m.ErpCategory, m.ErpCategory.external_id),
        ("product-prices", "productId", "products", m.ErpProductPrice.product_external_id,
         m.ErpProduct, m.ErpProduct.external_id),
        ("product-prices", "priceTableId", "price-tables",
         m.ErpProductPrice.price_table_external_id, m.ErpPriceTable, m.ErpPriceTable.external_id),
    ]
    result = []
    for resource, field, target, column, model, key in checks:
        count = _count(db, connection_id, column, model, key)
        if count:
            result.append(
                {"resource": resource, "field": field, "target": target, "pending": count}
            )
    return result
