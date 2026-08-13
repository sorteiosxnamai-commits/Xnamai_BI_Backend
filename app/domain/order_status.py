from collections.abc import Iterable

from sqlalchemy import func


CANCELLED_ORDER_STATUSES = frozenset({"0", "5", "cancelled", "cancelado"})
QUOTE_ORDER_STATUSES = frozenset({"1", "orcamento", "orçamento", "budget", "quote"})
VALID_SALE_STATUSES = frozenset({"2", "pedido", "order"})
RECOGNIZED_ORDER_STATUSES = (
    CANCELLED_ORDER_STATUSES | QUOTE_ORDER_STATUSES | VALID_SALE_STATUSES
)


def normalize_order_status(value: object) -> str:
    return str(value or "").strip().lower()


def is_cancelled_order(value: object) -> bool:
    return normalize_order_status(value) in CANCELLED_ORDER_STATUSES


def is_valid_sale(value: object) -> bool:
    return normalize_order_status(value) in VALID_SALE_STATUSES


def status_sql_in(column, values: Iterable[str]):
    """Return a SQLAlchemy predicate using the canonical normalized status rule."""
    return func.lower(func.trim(column)).in_(tuple(values))
