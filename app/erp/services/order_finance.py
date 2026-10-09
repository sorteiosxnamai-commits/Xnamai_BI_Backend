"""Financeiro por pedido: vínculo pedido → título → parcelas → baixas, métricas e relatório.

Convenções (documentadas porque indicadores de dinheiro confundem fácil):

- **Vendas** = valor líquido dos pedidos (`kind = order`) por data de emissão. NÃO é receita recebida.
- **Obrigação** = soma dos títulos locais ATIVOS ligados ao pedido (`origin_type = sales_order`).
- **Recebido (caixa)** = baixas de pagamento menos estornos, por data da baixa (America/Sao_Paulo).
  `settled_amount` da parcela já é líquido de estornos.
- **A receber** = obrigação ainda em aberto (parcela não baixada), por vencimento.
- Título do Mercos NUNCA é somado ao título local da mesma obrigação: se o pedido já tem título no
  Mercos, criar o local exige confirmação explícita e o relatório conta só o local.
- Pix e devolução automática não têm provedor: aparecem como indisponíveis, nunca como zero.
"""

import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import and_, exists, func, not_, select

from app.erp import serializers as ser
from app.erp.audit import audit
from app.erp.auth import ErpUser
from app.erp.common import BR_TZ, http_error, money
from app.erp.models import commercial as m
from app.erp.models import local as lo
from app.erp.models import operations as op
from app.erp.schemas.operations import OrderReceivableInput
from app.erp.services import finance
from app.erp.services import order_operations as oo

ZERO = Decimal("0")
ACTIVE_REFUND = ("requested", "approved", "external_confirmed", "reversed_local")
FORMULAS = {
    "sales": "Vendas = soma do valor líquido dos pedidos (tipo pedido) por data de emissão. Não é valor recebido.",
    "receivable": "A receber = valor em aberto das parcelas de títulos locais ativos ligados a pedidos, por vencimento.",
    "cash": "Recebido = baixas de pagamento menos estornos, por data da baixa (America/Sao_Paulo).",
    "refunds": "Reembolsos = solicitações do período; só 'confirmado' ou 'estorno local' contam como devolvido.",
}


def today_br() -> date:
    return datetime.now(BR_TZ).date()


def _bounds(start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]:
    # dia civil de Brasília -> instante UTC (o banco guarda UTC; SQLite compara texto sem fuso)
    low = datetime.combine(start, time.min, tzinfo=BR_TZ).astimezone(timezone.utc) if start else None
    high = (
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=BR_TZ).astimezone(timezone.utc) if end else None
    )
    return low, high


def active_titles_query(connection_id: str):
    T = lo.ErpFinTitle
    return select(T).where(
        T.connection_id == connection_id, T.origin_type == "sales_order", T.status != "cancelled"
    )


def _order(db, connection_id: str, order_id: str) -> m.ErpSalesOrder:
    row = db.scalar(
        select(m.ErpSalesOrder).where(
            m.ErpSalesOrder.connection_id == connection_id, m.ErpSalesOrder.external_id == order_id
        )
    )
    if row is None:
        raise http_error(404, "not_found", "Pedido não encontrado")
    return row


def refund_commitment(db, connection_id: str, settlement_id: int) -> Decimal:
    value = db.scalar(
        select(func.coalesce(func.sum(op.ErpRefundRequest.amount), 0)).where(
            op.ErpRefundRequest.connection_id == connection_id,
            op.ErpRefundRequest.settlement_id == settlement_id,
            op.ErpRefundRequest.status.in_(ACTIVE_REFUND),
        )
    )
    return Decimal(str(value or 0))


def _derive(obligation: Decimal, paid: Decimal, overdue: bool) -> str:
    if obligation <= 0:
        return "none"
    if paid <= 0:
        return "open"
    return "paid" if paid >= obligation else "partial"


def order_finance(db, user: ErpUser, connection_id: str, order_id: str) -> dict:
    order = _order(db, connection_id, order_id)
    titles = db.scalars(
        select(lo.ErpFinTitle)
        .where(
            lo.ErpFinTitle.connection_id == connection_id,
            lo.ErpFinTitle.origin_type == "sales_order",
            lo.ErpFinTitle.origin_ref == order_id,
        )
        .order_by(lo.ErpFinTitle.id)
    ).all()
    out_titles, payments = [], []
    obligation = paid = ZERO
    overdue = False
    today = today_br()
    for title in titles:
        installments = db.scalars(
            select(lo.ErpFinInstallment).where(lo.ErpFinInstallment.title_id == title.id).order_by(lo.ErpFinInstallment.number)
        ).all()
        active = title.status != "cancelled"
        if active:
            obligation += title.total
            paid += sum((i.settled_amount for i in installments), ZERO)
            overdue = overdue or any(i.status != "settled" and i.due_date < today for i in installments)
        settlements = db.scalars(
            select(lo.ErpFinSettlement)
            .where(lo.ErpFinSettlement.installment_id.in_([i.id for i in installments]))
            .order_by(lo.ErpFinSettlement.id)
        ).all()
        reversed_ids = {s.reversal_of_id for s in settlements if s.kind == "reversal"}
        for s in settlements:
            if s.kind != "payment":
                continue
            was_reversed = s.id in reversed_ids
            committed = refund_commitment(db, connection_id, s.id)
            refundable = ZERO if was_reversed else max(s.amount - committed, ZERO)
            payments.append({
                "settlementId": s.id, "titleId": title.id, "installmentId": s.installment_id,
                "amount": money(s.amount), "settledAt": ser.iso(s.settled_at), "operator": s.operator,
                "reference": s.reference, "reversed": was_reversed,
                "refundable": money(refundable), "refundCommitted": money(committed),
            })
        out_titles.append({
            "id": title.id, "status": title.status, "total": money(title.total), "origin": "local",
            "installments": [
                {"id": i.id, "number": i.number, "dueDate": i.due_date.isoformat(), "amount": money(i.amount),
                 "settledAmount": money(i.settled_amount), "status": i.status,
                 "overdue": i.status != "settled" and i.due_date < today and active}
                for i in installments
            ],
        })
    external = db.scalars(
        select(m.ErpExternalTitle).where(
            m.ErpExternalTitle.connection_id == connection_id, m.ErpExternalTitle.order_external_id == order_id
        )
    ).all()
    state = _derive(obligation, paid, overdue)
    return {
        "orderId": order_id,
        "orderTotal": money(order.net_total),
        "state": state,
        "overdue": overdue and state != "paid",
        "obligation": money(obligation),
        "paid": money(paid),
        "open": money(max(obligation - paid, ZERO)),
        "titles": out_titles,
        "payments": payments,
        "externalTitles": [
            {"id": t.external_id, "amount": money(t.amount), "status": t.status, "dueDate": t.due_date and t.due_date.isoformat()}
            for t in external
        ],
        "externalNote": "Títulos do Mercos são exibidos à parte e não somados ao financeiro local."
        if external else None,
        "pix": {"state": "unknown", "reason": "Sem provedor de Pix configurado"},
        "formulas": FORMULAS,
    }


def create_order_receivable(db, user: ErpUser, connection_id: str, order_id: str, body: OrderReceivableInput) -> dict:
    order = _order(db, connection_id, order_id)
    db.get(m.ErpSalesOrder, order.id, with_for_update=True)  # serializa a criação de obrigações do pedido
    if order.kind == "cancelled":
        raise http_error(409, "order_cancelled", "Pedido cancelado não gera título")
    if not order.customer_external_id:
        raise http_error(409, "customer_required", "O pedido não tem cliente identificado")
    amount = body.amount if body.amount is not None else order.net_total
    if amount is None or amount <= 0:
        raise http_error(409, "order_total_unavailable", "O pedido não tem valor conhecido; informe o valor do título")
    existing = db.scalars(
        select(lo.ErpFinTitle).where(
            lo.ErpFinTitle.connection_id == connection_id,
            lo.ErpFinTitle.origin_type == "sales_order",
            lo.ErpFinTitle.origin_ref == order_id,
        )
    ).all()
    if any(t.status != "cancelled" for t in existing):
        raise http_error(409, "obligation_exists", "Este pedido já tem um título local ativo; cancele-o antes de criar outro")
    external = db.scalars(
        select(m.ErpExternalTitle).where(
            m.ErpExternalTitle.connection_id == connection_id,
            m.ErpExternalTitle.order_external_id == order_id,
        )
    ).all()
    if external and not body.acknowledgeExternalTitle:
        raise http_error(
            409, "external_title_exists",
            "Este pedido já tem título no Mercos. Criar também um título local duplicaria a obrigação; "
            "confirme explicitamente se é intencional (o relatório contará só o local).",
            externalTitles=[t.external_id for t in external],
        )
    title, _ = finance.create_title(
        db, user, connection_id, kind="receivable",
        description=body.description or f"Pedido {order.number or order_id}", total=amount,
        first_due=body.firstDueDate, installments=body.installments, customer_id=order.customer_external_id,
        origin_type="sales_order", origin_ref=order_id,
        causal_key=f"sales_order:{order_id}:{len(existing) + 1}",
    )
    if external:
        title.extra = {"externalTitleAcknowledged": [t.external_id for t in external]}
    audit(
        db, operator=user.username, action="finance.order_receivable_created", connection_id=connection_id,
        resource="sales_order", resource_id=order_id,
        detail={"titleId": title.id, "amount": str(amount), "installments": body.installments,
                "externalAcknowledged": bool(external)},
    )
    db.flush()
    return order_finance(db, user, connection_id, order_id)


# --- estados e filtros para a lista de pedidos ----------------------------------


def _obligation_sq(connection_id: str):
    T = lo.ErpFinTitle
    return (
        select(func.coalesce(func.sum(T.total), 0))
        .where(T.connection_id == connection_id, T.origin_type == "sales_order",
               T.origin_ref == m.ErpSalesOrder.external_id, T.status != "cancelled")
        .correlate(m.ErpSalesOrder)
        .scalar_subquery()
    )


def _paid_sq(connection_id: str):
    T, Inst = lo.ErpFinTitle, lo.ErpFinInstallment
    return (
        select(func.coalesce(func.sum(Inst.settled_amount), 0))
        .select_from(Inst)
        .join(T, T.id == Inst.title_id)
        .where(T.connection_id == connection_id, T.origin_type == "sales_order",
               T.origin_ref == m.ErpSalesOrder.external_id, T.status != "cancelled")
        .correlate(m.ErpSalesOrder)
        .scalar_subquery()
    )


def filter_provider(connection_id: str, value: str):
    T, Inst, M = lo.ErpFinTitle, lo.ErpFinInstallment, m.ErpSalesOrder
    obligation, paid = _obligation_sq(connection_id), _paid_sq(connection_id)
    has_title = exists().where(T.connection_id == connection_id, T.origin_type == "sales_order",
                               T.origin_ref == M.external_id, T.status != "cancelled")
    if value == "none":
        return not_(has_title)
    if value == "open":
        return and_(has_title, paid <= 0)
    if value == "partial":
        return and_(paid > 0, paid < obligation)
    if value == "paid":
        return and_(obligation > 0, paid >= obligation)
    if value == "overdue":
        return exists().where(
            Inst.title_id == T.id, T.connection_id == connection_id, T.origin_type == "sales_order",
            T.origin_ref == M.external_id, T.status != "cancelled", Inst.status != "settled",
            Inst.due_date < today_br(),
        )
    raise http_error(422, "invalid_filter", "Valor inválido para o filtro de situação financeira")


def state_provider(db, connection_id: str, orders: list[Any]) -> dict[str, dict]:
    ids = [o.external_id for o in orders]
    if not ids:
        return {}
    T, Inst = lo.ErpFinTitle, lo.ErpFinInstallment
    today = today_br()
    agg: dict[str, dict] = {i: {"obligation": ZERO, "paid": ZERO, "overdue": False} for i in ids}
    titles = db.execute(
        select(T.id, T.origin_ref, T.total).where(
            T.connection_id == connection_id, T.origin_type == "sales_order",
            T.origin_ref.in_(ids), T.status != "cancelled",
        )
    ).all()
    owner = {title_id: ref for title_id, ref, _ in titles}
    for _, ref, total in titles:
        agg[ref]["obligation"] += total
    if owner:
        for title_id, settled, status, due in db.execute(
            select(Inst.title_id, Inst.settled_amount, Inst.status, Inst.due_date).where(Inst.title_id.in_(list(owner)))
        ):
            entry = agg[owner[title_id]]
            entry["paid"] += settled
            entry["overdue"] = entry["overdue"] or (status != "settled" and due < today)
    states = {}
    for order_id, entry in agg.items():
        state = _derive(entry["obligation"], entry["paid"], entry["overdue"])
        states[order_id] = {
            "state": state,
            "reason": "Financeiro local (títulos e baixas do ERP); independe do estado de pagamento do Mercos",
            "price": money(entry["obligation"]) if entry["obligation"] > 0 else None,
            "deadline": "overdue" if entry["overdue"] and state != "paid" else None,
        }
    return states


def summary_section(db, connection_id: str, filtered_ids) -> dict:
    M = m.ErpSalesOrder
    counts = {}
    for state in ("none", "open", "partial", "paid", "overdue"):
        counts[state] = int(
            db.scalar(select(func.count()).select_from(M).where(M.id.in_(filtered_ids), filter_provider(connection_id, state)))
            or 0
        )
    return {"available": True, "counts": counts}


def register() -> None:
    oo.STATE_PROVIDERS["finance"] = state_provider
    oo.FILTER_PROVIDERS["financeStatus"] = filter_provider
    oo.SUMMARY_SECTIONS["finance"] = summary_section
    oo.AVAILABLE_FILTERS.add("financeStatus")


# --- métricas e relatório -------------------------------------------------------


def _sales(db, connection_id: str, start: date | None, end: date | None) -> tuple[int, Decimal]:
    M = m.ErpSalesOrder
    query = select(func.count(), func.coalesce(func.sum(M.net_total), 0)).where(
        M.connection_id == connection_id, M.kind == "order"
    )
    if start:
        query = query.where(M.issue_date >= start)
    if end:
        query = query.where(M.issue_date <= end)
    count, total = db.execute(query).one()
    return int(count or 0), Decimal(str(total or 0))


def _cash(db, connection_id: str, start: date | None, end: date | None) -> dict:
    T, Inst, S = lo.ErpFinTitle, lo.ErpFinInstallment, lo.ErpFinSettlement
    low, high = _bounds(start, end)
    query = (
        select(S.kind, func.coalesce(func.sum(S.amount), 0))
        .select_from(S)
        .join(Inst, Inst.id == S.installment_id)
        .join(T, T.id == Inst.title_id)
        .where(T.connection_id == connection_id, T.origin_type == "sales_order")
        .group_by(S.kind)
    )
    if low:
        query = query.where(S.settled_at >= low)
    if high:
        query = query.where(S.settled_at < high)
    by_kind = {kind: Decimal(str(total or 0)) for kind, total in db.execute(query)}
    received = by_kind.get("payment", ZERO)
    reversed_ = by_kind.get("reversal", ZERO)
    return {"received": received, "reversed": reversed_, "net": received - reversed_}


def _receivable(db, connection_id: str, start: date | None, end: date | None) -> dict:
    T, Inst = lo.ErpFinTitle, lo.ErpFinInstallment
    query = (
        select(Inst.amount, Inst.settled_amount, Inst.due_date)
        .select_from(Inst)
        .join(T, T.id == Inst.title_id)
        .where(T.connection_id == connection_id, T.origin_type == "sales_order", T.status != "cancelled")
    )
    if start:
        query = query.where(Inst.due_date >= start)
    if end:
        query = query.where(Inst.due_date <= end)
    open_total = overdue_total = ZERO
    today = today_br()
    for amount, settled, due in db.execute(query):
        remaining = amount - settled
        if remaining <= 0:
            continue
        open_total += remaining
        if due < today:
            overdue_total += remaining
    return {"open": open_total, "overdue": overdue_total}


def _refunds(db, connection_id: str, start: date | None, end: date | None) -> dict:
    R = op.ErpRefundRequest
    low, high = _bounds(start, end)
    query = select(R.status, func.count(), func.coalesce(func.sum(R.amount), 0)).where(R.connection_id == connection_id).group_by(R.status)
    if low:
        query = query.where(R.requested_at >= low)
    if high:
        query = query.where(R.requested_at < high)
    buckets = {status: {"count": int(c), "amount": money(Decimal(str(a or 0)))} for status, c, a in db.execute(query)}
    returned = sum(
        (Decimal(b["amount"]) for k, b in buckets.items() if k in ("external_confirmed", "reversed_local")), ZERO
    )
    return {"byStatus": buckets, "returned": money(returned)}


def _variation(current: Decimal, previous: Decimal, comparable: bool) -> dict:
    if not comparable:
        return {"available": False, "reason": "Sem período definido para comparar"}
    if previous == ZERO:
        return {"available": False, "reason": "Período anterior sem base de comparação"}
    percent = ((current - previous) / previous * Decimal(100)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return {"available": True, "percent": str(percent), "previous": money(previous)}


def metrics(db, connection_id: str, start: date | None, end: date | None) -> dict:
    comparable = bool(start and end and end >= start)
    count, sales = _sales(db, connection_id, start, end)
    cash = _cash(db, connection_id, start, end)
    previous = {"sales": ZERO, "cash": ZERO}
    if comparable:
        days = (end - start).days + 1
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=days - 1)
        previous["sales"] = _sales(db, connection_id, prev_start, prev_end)[1]
        previous["cash"] = _cash(db, connection_id, prev_start, prev_end)["net"]
    return {
        "period": {"from": start and start.isoformat(), "to": end and end.isoformat()},
        "sales": {"orders": count, "net": money(sales), "variation": _variation(sales, previous["sales"], comparable)},
        "cash": {
            "received": money(cash["received"]), "reversed": money(cash["reversed"]), "net": money(cash["net"]),
            "variation": _variation(cash["net"], previous["cash"], comparable),
        },
        "receivable": {k: money(v) for k, v in _receivable(db, connection_id, start, end).items()},
        "refunds": _refunds(db, connection_id, start, end),
        "pix": {"available": False, "reason": "Sem provedor de Pix configurado"},
        "formulas": FORMULAS,
    }


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    # evita injeção de fórmula ao abrir em planilha
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def report_csv(db, user: ErpUser, connection_id: str, filters: oo.OrderFilters, limit: int = 5000) -> tuple[str, bool]:
    M = m.ErpSalesOrder
    query = oo.apply_filters(select(M), connection_id, filters).order_by(M.issued_at.desc(), M.id.desc()).limit(limit + 1)
    orders = list(db.scalars(query))
    truncated = len(orders) > limit
    orders = orders[:limit]
    customers = {
        c.external_id: c.name
        for c in db.scalars(
            select(m.ErpCustomer).where(
                m.ErpCustomer.connection_id == connection_id,
                m.ErpCustomer.external_id.in_([o.customer_external_id for o in orders if o.customer_external_id]),
            )
        )
    }
    fin = state_provider(db, connection_id, orders)
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\n")
    writer.writerow(["pedido", "cliente", "status_mercos", "tipo", "valor_liquido", "obrigacao_local",
                     "situacao_financeira_local", "vencida", "estado_pagamento_mercos"])
    for o in orders:
        f = fin.get(o.external_id, {})
        writer.writerow([_csv_cell(v) for v in [
            o.number or o.external_id, customers.get(o.customer_external_id), o.commercial_status, o.kind,
            money(o.net_total), f.get("price"), f.get("state"), "sim" if f.get("deadline") == "overdue" else "não",
            o.payment_status,
        ]])
    return out.getvalue(), truncated
