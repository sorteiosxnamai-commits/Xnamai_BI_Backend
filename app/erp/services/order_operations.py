"""Consulta operacional de pedidos (painel interno).

Reaproveita o espelho `erp_sales_orders`; não chama o Mercos. Regras:

- busca por número, nome do cliente e nome/SKU de produto usa EXISTS, nunca JOIN: um pedido com
  vários itens que casam continua aparecendo uma única vez e a paginação permanece estável;
- estados Mercos (comercial/faturamento/atendimento/pagamento) ficam separados do estado
  operacional local; `concluído` operacional NÃO é inferido de status comercial (o significado de
  pedido concluído ainda é pendência de negócio);
- métrica sem base é devolvida como indisponível (`available: false` + motivo), nunca como zero;
- dinheiro é somado no banco/Decimal e devolvido como texto decimal (contrato existente).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.erp import serializers as ser
from app.erp.common import http_error
from app.erp.models import commercial as m
from app.erp.models.core import ErpAuditEvent, ErpOperation, ErpSyncCheckpoint

ZERO = Decimal("0")


@dataclass(frozen=True)
class OrderFilters:
    search: str | None = None
    customer_id: str | None = None
    kind: str | None = None
    commercial_status: str | None = None
    payment_status: str | None = None
    billing_status: str | None = None
    items_complete: bool | None = None
    pending: bool | None = None
    date_from: date | None = None
    date_to: date | None = None
    # preenchidos pelos provedores de estado (frete/fiscal) quando existirem
    extra: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict:
        return {
            "search": self.search,
            "customerId": self.customer_id,
            "kind": self.kind,
            "commercialStatus": self.commercial_status,
            "paymentStatus": self.payment_status,
            "billingStatus": self.billing_status,
            "itemsComplete": self.items_complete,
            "pending": self.pending,
            "dateFrom": self.date_from and self.date_from.isoformat(),
            "dateTo": self.date_to and self.date_to.isoformat(),
            **dict(self.extra),
        }


# Provedores de filtro/estado adicionais (frete, fiscal). Cada um recebe a conexão e o valor
# pedido e devolve uma condição SQL sobre erp_sales_orders. Registrados pelos módulos que têm fonte.
FILTER_PROVIDERS: dict[str, Callable[[str, str], Any]] = {}
STATE_PROVIDERS: dict[str, Callable[[Session, str, list], dict[str, dict]]] = {}
# seções do resumo (contagens por estado) e nomes dos filtros opcionais que têm fonte real
SUMMARY_SECTIONS: dict[str, Callable[[Session, str, Any], dict]] = {}
AVAILABLE_FILTERS: set[str] = set()


def _like(column, text: str):
    return func.lower(column).contains(text.strip().lower(), autoescape=True)


def _customer_missing(connection_id: str):
    M, C = m.ErpSalesOrder, m.ErpCustomer
    return and_(
        M.customer_external_id.is_not(None),
        ~exists().where(C.connection_id == connection_id, C.external_id == M.customer_external_id),
    )


def _pending_condition(connection_id: str):
    M = m.ErpSalesOrder
    return or_(M.items_complete.is_(False), _customer_missing(connection_id))


def apply_filters(query, connection_id: str, f: OrderFilters):
    M, C, Item = m.ErpSalesOrder, m.ErpCustomer, m.ErpSalesOrderItem
    query = query.where(M.connection_id == connection_id)
    if f.search and f.search.strip():
        text = f.search.strip()
        query = query.where(
            or_(
                _like(M.number, text),
                exists().where(
                    C.connection_id == connection_id,
                    C.external_id == M.customer_external_id,
                    _like(C.name, text),
                ),
                exists().where(
                    Item.order_id == M.id,
                    or_(_like(Item.name, text), _like(Item.code, text)),
                ),
            )
        )
    if f.customer_id:
        query = query.where(M.customer_external_id == f.customer_id)
    if f.kind:
        query = query.where(M.kind == f.kind)
    if f.commercial_status:
        query = query.where(M.commercial_status == f.commercial_status)
    if f.payment_status:
        query = query.where(M.payment_status == f.payment_status)
    if f.billing_status:
        query = query.where(M.billing_status == f.billing_status)
    if f.items_complete is not None:
        query = query.where(M.items_complete == f.items_complete)
    if f.pending is True:
        query = query.where(_pending_condition(connection_id))
    elif f.pending is False:
        query = query.where(~_pending_condition(connection_id))
    if f.date_from:
        query = query.where(M.issue_date >= f.date_from)
    if f.date_to:
        query = query.where(M.issue_date <= f.date_to)
    for name, value in f.extra:
        provider = FILTER_PROVIDERS.get(name)
        if provider is None:
            # filtro sem fonte real nesta base: recusa em vez de fingir que filtrou
            raise http_error(422, "filter_unavailable", f"Filtro `{name}` indisponível: sem fonte de dados")
        query = query.where(provider(connection_id, value))
    return query


def operational_state(order) -> dict:
    """Estado operacional local derivado só do que é comprovável.

    `completed` não existe aqui de propósito: ele depende de regra de negócio ainda não definida."""
    if order.kind == "cancelled":
        return {"code": "cancelled", "label": "Cancelado"}
    if order.kind == "quote":
        return {"code": "quote", "label": "Orçamento"}
    if order.kind == "order":
        return {"code": "in_progress", "label": "Em andamento"}
    return {"code": "unclassified", "label": "Sem classificação"}


def _pendencies(order, customers: dict[str, str]) -> list[dict]:
    items = []
    if not order.items_complete:
        items.append({"code": "items_incomplete", "label": "Itens ainda não sincronizados"})
    if order.customer_external_id and order.customer_external_id not in customers:
        items.append({"code": "customer_missing", "label": "Cliente ainda não sincronizado"})
    return items


def _last_human_actions(db: Session, connection_id: str, ids: list[str]) -> dict[str, dict]:
    """Última ação HUMANA por pedido (auditoria + operações externas). Atualização do espelho vem
    da sincronização e é devolvida à parte, em `lastExternalUpdateAt`."""
    if not ids:
        return {}
    found: dict[str, dict] = {}
    events = db.scalars(
        select(ErpAuditEvent)
        .where(
            ErpAuditEvent.connection_id == connection_id,
            ErpAuditEvent.resource == "sales_order",
            ErpAuditEvent.resource_id.in_(ids),
        )
        .order_by(ErpAuditEvent.at.desc())
    )
    for event in events:
        found.setdefault(
            event.resource_id,
            {"at": ser.iso(event.at), "operator": event.operator, "action": event.action},
        )
    operations = db.scalars(
        select(ErpOperation)
        .where(
            ErpOperation.connection_id == connection_id,
            ErpOperation.target_resource == "orders",
            ErpOperation.target_external_id.in_(ids),
        )
        .order_by(ErpOperation.created_at.desc())
    )
    for op in operations:
        at = ser.iso(op.created_at)
        current = found.get(op.target_external_id)
        if current is None or (at and at > current["at"]):
            found[op.target_external_id] = {"at": at, "operator": op.operator, "action": op.kind}
    return found


def order_history(db: Session, connection_id: str, external_id: str, limit: int = 100) -> list[dict]:
    """Linha do tempo humana do pedido: auditoria local + operações externas (outbox)."""
    entries: list[dict] = []
    for event in db.scalars(
        select(ErpAuditEvent)
        .where(
            ErpAuditEvent.connection_id == connection_id,
            ErpAuditEvent.resource == "sales_order",
            ErpAuditEvent.resource_id == external_id,
        )
        .order_by(ErpAuditEvent.at.desc())
        .limit(limit)
    ):
        entries.append(
            {
                "at": ser.iso(event.at),
                "kind": "local",
                "operator": event.operator,
                "action": event.action,
                "result": event.result,
                "reason": event.reason,
            }
        )
    for op in db.scalars(
        select(ErpOperation)
        .where(
            ErpOperation.connection_id == connection_id,
            ErpOperation.target_resource == "orders",
            ErpOperation.target_external_id == external_id,
        )
        .order_by(ErpOperation.created_at.desc())
        .limit(limit)
    ):
        entries.append(
            {
                "at": ser.iso(op.created_at),
                "kind": "external_request",
                "operator": op.operator,
                "action": op.kind,
                "result": op.status,  # solicitação aceita != resultado confirmado: o status é o do outbox
                "reason": None,
            }
        )
    entries.sort(key=lambda e: e["at"] or "", reverse=True)
    return entries[:limit]


def summarize_page(db: Session, connection_id: str, orders: list, user) -> dict[str, dict]:
    """`operational` de cada pedido da página (consultas em lote, sem N+1)."""
    if not orders:
        return {}
    ids = [o.external_id for o in orders]
    customer_ids = [o.customer_external_id for o in orders if o.customer_external_id]
    customers = {
        c.external_id: c.name
        for c in db.scalars(
            select(m.ErpCustomer).where(
                m.ErpCustomer.connection_id == connection_id,
                m.ErpCustomer.external_id.in_(customer_ids),
            )
        )
    }
    sellers = {
        s.external_id: s.name
        for s in db.scalars(
            select(m.ErpSeller).where(
                m.ErpSeller.connection_id == connection_id,
                m.ErpSeller.external_id.in_([o.seller_external_id for o in orders if o.seller_external_id]),
            )
        )
    }
    previews: dict[int, list[dict]] = {}
    for item in db.scalars(
        select(m.ErpSalesOrderItem)
        .where(m.ErpSalesOrderItem.order_id.in_([o.id for o in orders]), m.ErpSalesOrderItem.excluded.is_(False))
        .order_by(m.ErpSalesOrderItem.order_id, m.ErpSalesOrderItem.position)
    ):
        bucket = previews.setdefault(item.order_id, [])
        if len(bucket) < 3:
            bucket.append({"name": item.name, "code": item.code, "quantity": ser.quantity(item.quantity)})
    human = _last_human_actions(db, connection_id, ids)
    provided = {name: provider(db, connection_id, orders) for name, provider in STATE_PROVIDERS.items()}
    out: dict[str, dict] = {}
    for order in orders:
        states = {name: data.get(order.external_id) for name, data in provided.items()}
        out[order.external_id] = {
            "customerName": customers.get(order.customer_external_id),
            "responsible": {
                "sellerId": order.seller_external_id,
                "name": sellers.get(order.seller_external_id),
            },
            "state": operational_state(order),
            "itemsPreview": previews.get(order.id, []),
            "totals": {
                "gross": ser.money(order.gross_total),
                "discount": ser.money(order.discount_total),
                "freight": ser.money(order.freight_total),
                "net": ser.money(order.net_total),
            },
            "pendencies": _pendencies(order, customers),
            "lastExternalUpdateAt": ser.iso(order.source_updated_at or order.captured_at),
            "lastHumanAction": human.get(order.external_id),
            # frete/fiscal vêm dos módulos locais registrados; ausentes = fonte indisponível
            "shipping": states.get("shipping") or {"state": "unavailable"},
            "invoice": states.get("invoice") or {"state": "unavailable"},
            # pagamento: espelho Mercos; Pix não tem fonte alguma nesta base
            "payment": {"state": order.payment_status or "unknown", "source": "mercos_mirror"},
            "pix": {"state": "unknown", "reason": "Sem provedor de Pix configurado"},
            # financeiro local (títulos e baixas do ERP); separado do pagamento espelhado do Mercos
            "finance": states.get("finance") or {"state": "unavailable"},
        }
    return out


# --- agregados ------------------------------------------------------------------


def _money_sum(db: Session, query, column) -> Decimal:
    value = db.scalar(select(func.coalesce(func.sum(column), 0)).select_from(query.order_by(None).subquery()))
    return Decimal(str(value or 0))


def _totals(db: Session, connection_id: str, f: OrderFilters) -> dict:
    M = m.ErpSalesOrder
    base = apply_filters(select(M), connection_id, f).subquery()
    row = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(base.c.net_total), 0),
            func.coalesce(func.sum(base.c.gross_total), 0),
        ).select_from(base)
    ).one()
    return {"count": int(row[0] or 0), "net": Decimal(str(row[1] or 0)), "gross": Decimal(str(row[2] or 0))}


def _coverage(db: Session, connection_id: str) -> dict:
    status = {
        cp.resource: cp.status
        for cp in db.scalars(
            select(ErpSyncCheckpoint).where(
                ErpSyncCheckpoint.connection_id == connection_id,
                ErpSyncCheckpoint.resource.in_(["orders", "customers"]),
            )
        )
    }
    complete = all(status.get(r) == "success" for r in ("orders", "customers"))
    return {
        "complete": complete,
        "resources": {r: status.get(r, "never") for r in ("orders", "customers")},
        "note": None
        if complete
        else "Importação parcial: totais e pendências refletem só o que já foi sincronizado.",
    }


def _variation(db: Session, connection_id: str, f: OrderFilters, current: dict, coverage: dict) -> dict:
    if not f.date_from or not f.date_to or f.date_to < f.date_from:
        return {"available": False, "reason": "Sem período definido para comparar"}
    if not coverage["complete"]:
        return {"available": False, "reason": "Importação parcial: o período anterior pode estar incompleto"}
    days = (f.date_to - f.date_from).days + 1
    prev_to = f.date_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=days - 1)
    previous = _totals(
        db, connection_id, OrderFilters(**{**f.__dict__, "date_from": prev_from, "date_to": prev_to})
    )
    if previous["net"] == ZERO:
        return {"available": False, "reason": "Período anterior sem base de comparação"}
    percent = (current["net"] - previous["net"]) / previous["net"] * Decimal("100")
    return {
        "available": True,
        "percent": str(percent.quantize(Decimal("0.01"))),
        "previousNet": ser.money(previous["net"]),
        "previousPeriod": {"from": prev_from.isoformat(), "to": prev_to.isoformat()},
    }


def operational_summary(db: Session, connection_id: str, f: OrderFilters) -> dict:
    """Agregados sobre o conjunto filtrado INTEIRO (não só a página visível)."""
    M = m.ErpSalesOrder
    current = _totals(db, connection_id, f)
    coverage = _coverage(db, connection_id)
    filtered = apply_filters(select(M), connection_id, f).subquery()

    def count_where(*conditions) -> int:
        return int(
            db.scalar(
                select(func.count())
                .select_from(M)
                .where(M.id.in_(select(filtered.c.id)), *conditions)
            )
            or 0
        )

    by_kind: dict[str, int] = {}
    for kind, total in db.execute(
        select(M.kind, func.count()).where(M.id.in_(select(filtered.c.id))).group_by(M.kind)
    ):
        by_kind[kind or "unknown"] = int(total)
    by_payment: dict[str, int] = {}
    for status, total in db.execute(
        select(M.payment_status, func.count()).where(M.id.in_(select(filtered.c.id))).group_by(M.payment_status)
    ):
        by_payment[status or "unknown"] = int(total)
    sections = {
        name: provider(db, connection_id, select(filtered.c.id)) for name, provider in SUMMARY_SECTIONS.items()
    }
    for name in ("shipping", "invoice"):
        sections.setdefault(name, {"available": False, "reason": f"Sem fonte de dados de {name} nesta base"})
    return {
        **sections,
        "filtersAvailable": sorted(AVAILABLE_FILTERS),
        "filters": f.as_dict(),
        "coverage": coverage,
        "orders": {"count": current["count"], "byKind": by_kind},
        "values": {
            "net": ser.money(current["net"]),
            "gross": ser.money(current["gross"]),
            "note": "Valor vendido (pedidos e orçamentos filtrados); não é valor recebido.",
        },
        "variation": _variation(db, connection_id, f, current, coverage),
        "payment": {"byStatus": by_payment, "pix": {"available": False, "reason": "Sem provedor de Pix"}},
        "pendencies": {
            "itemsIncomplete": count_where(M.items_complete.is_(False)),
            "customerMissing": count_where(_customer_missing(connection_id)),
            "any": count_where(_pending_condition(connection_id)),
        },
    }
