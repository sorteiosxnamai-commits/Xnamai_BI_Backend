"""Solicitações de reembolso (fluxo interno).

O que cada estado significa de fato:

- `requested`: alguém pediu. Nada mudou no financeiro.
- `approved`: aprovado internamente. A devolução AINDA não aconteceu.
- `external_confirmed`: uma pessoa confirmou, com referência, que a devolução foi feita FORA do
  sistema (banco/PSP). O sistema não comprova a devolução bancária.
- `reversed_local`: o estorno LOCAL da baixa foi aplicado pelo serviço financeiro existente. É
  ajuste de contabilidade interna, não devolução bancária.

Regras: o valor reembolsável é o da baixa menos o que já está comprometido em outras solicitações
ativas (e zero se a baixa já foi estornada); a checagem roda com o título travado, então duas
solicitações simultâneas não ultrapassam o limite. O estorno local existente é INTEGRAL: reembolso
parcial não o usa (409 explicando) e segue por confirmação externa.
"""

from decimal import Decimal

from sqlalchemy import func, select, update

from app.erp import serializers as ser
from app.erp.audit import audit
from app.erp.auth import ErpUser
from app.erp.common import http_error, money, paginate, utcnow
from app.erp.models import local as lo
from app.erp.models import operations as op
from app.erp.schemas.operations import RefundExternalConfirm, RefundReason, RefundRequestCreate, RefundVersioned
from app.erp.services import finance, order_finance

STATEMENTS = {
    "requested": "Solicitação registrada. Não altera nenhuma baixa e não devolve dinheiro.",
    "approved": "Aprovada internamente. A devolução ainda não foi feita.",
    "rejected": "Rejeitada. Nada foi alterado.",
    "cancelled": "Cancelada. Nada foi alterado.",
    "external_confirmed": "Devolução externa confirmada manualmente; o sistema não comprova a devolução bancária.",
    "reversed_local": "Estorno local aplicado na baixa; é ajuste interno e não é devolução bancária.",
}
ZERO = Decimal("0")


def _request(db, connection_id: str, request_id: int) -> op.ErpRefundRequest:
    row = db.scalar(
        select(op.ErpRefundRequest).where(
            op.ErpRefundRequest.connection_id == connection_id, op.ErpRefundRequest.id == request_id
        )
    )
    if row is None:
        raise http_error(404, "not_found", "Solicitação não encontrada")
    return row


def _settlement_context(db, connection_id: str, settlement_id: int, order_id: str):
    settlement = db.get(lo.ErpFinSettlement, settlement_id)
    if settlement is None or settlement.connection_id != connection_id or settlement.kind != "payment":
        raise http_error(404, "settlement_not_found", "Baixa de pagamento não encontrada")
    installment = db.get(lo.ErpFinInstallment, settlement.installment_id)
    # trava o título: solicitações simultâneas sobre a mesma baixa se serializam
    title = db.get(lo.ErpFinTitle, installment.title_id, with_for_update=True)
    if title.origin_type != "sales_order" or title.origin_ref != order_id:
        raise http_error(422, "settlement_not_linked", "Esta baixa não pertence a um título deste pedido")
    return settlement, title


def _reversed(db, connection_id: str, settlement_id: int) -> bool:
    return bool(
        db.scalar(
            select(func.count(lo.ErpFinSettlement.id)).where(
                lo.ErpFinSettlement.connection_id == connection_id,
                lo.ErpFinSettlement.kind == "reversal",
                lo.ErpFinSettlement.reversal_of_id == settlement_id,
            )
        )
    )


def _events(db, request_id: int) -> list[dict]:
    return [
        {"at": ser.iso(e.at), "operator": e.operator, "action": e.action, "reason": e.reason, "detail": e.detail}
        for e in db.scalars(
            select(op.ErpRefundEvent).where(op.ErpRefundEvent.request_id == request_id).order_by(op.ErpRefundEvent.id)
        )
    ]


def serialize(db, row: op.ErpRefundRequest, *, with_events: bool = False) -> dict:
    data = {
        "id": row.id,
        "orderId": row.order_external_id,
        "settlementId": row.settlement_id,
        "amount": money(row.amount),
        "reason": row.reason,
        "status": row.status,
        "version": row.version,
        "requestedBy": row.requested_by,
        "requestedAt": ser.iso(row.requested_at),
        "decidedBy": row.decided_by,
        "decidedAt": ser.iso(row.decided_at),
        "decisionNote": row.decision_note,
        "externalReference": row.external_reference,
        "externalConfirmedBy": row.external_confirmed_by,
        "externalConfirmedAt": ser.iso(row.external_confirmed_at),
        "reversalSettlementId": row.reversal_settlement_id,
        "statement": STATEMENTS[row.status],
    }
    if with_events:
        data["events"] = _events(db, row.id)
    return data


def _event(db, request_id: int, user: ErpUser, action: str, reason: str | None = None, detail: dict | None = None):
    db.add(op.ErpRefundEvent(request_id=request_id, operator=user.username, action=action, reason=reason, detail=detail))


def create_request(db, user: ErpUser, connection_id: str, body: RefundRequestCreate) -> dict:
    settlement, title = _settlement_context(db, connection_id, body.settlementId, body.orderId)
    if _reversed(db, connection_id, settlement.id):
        raise http_error(409, "already_reversed", "Esta baixa já foi estornada; não há valor a reembolsar")
    committed = order_finance.refund_commitment(db, connection_id, settlement.id)
    refundable = settlement.amount - committed
    if body.amount > refundable:
        raise http_error(
            409, "refund_exceeds_refundable",
            "Valor acima do reembolsável: a baixa já tem reembolsos solicitados, aprovados ou confirmados",
            refundable=money(max(refundable, ZERO)), settlementAmount=money(settlement.amount),
            committed=money(committed),
        )
    row = op.ErpRefundRequest(
        connection_id=connection_id, order_external_id=body.orderId, settlement_id=settlement.id,
        amount=body.amount, reason=body.reason.strip(), status="requested", requested_by=user.username,
    )
    db.add(row)
    db.flush()
    _event(db, row.id, user, "requested", body.reason, {"amount": str(body.amount), "settlementId": settlement.id})
    audit(
        db, operator=user.username, action="refund.requested", connection_id=connection_id,
        resource="sales_order", resource_id=body.orderId, reason=body.reason,
        detail={"refundId": row.id, "amount": str(body.amount), "settlementId": settlement.id},
    )
    db.flush()
    return serialize(db, row, with_events=True)


def _move(
    db, user: ErpUser, connection_id: str, request_id: int, expected: int, *, allowed: tuple[str, ...],
    to: str, action: str, reason: str | None = None, **fields,
) -> op.ErpRefundRequest:
    row = _request(db, connection_id, request_id)
    claimed = db.execute(
        update(op.ErpRefundRequest)
        .where(
            op.ErpRefundRequest.id == request_id,
            op.ErpRefundRequest.version == expected,
            op.ErpRefundRequest.status.in_(allowed),
        )
        .values(status=to, version=expected + 1, updated_at=utcnow(), **fields)
    )
    if claimed.rowcount != 1:
        db.refresh(row)
        if row.status not in allowed:
            raise http_error(409, "invalid_transition", f"Solicitação está '{row.status}' e não aceita esta ação")
        raise http_error(
            409, "version_conflict", "A solicitação foi alterada por outra pessoa; recarregue e confira",
            currentVersion=row.version,
        )
    db.refresh(row)
    _event(db, row.id, user, action, reason, {"from": allowed, "to": to})
    audit(
        db, operator=user.username, action=f"refund.{action}", connection_id=connection_id,
        resource="sales_order", resource_id=row.order_external_id, reason=reason,
        detail={"refundId": row.id, "status": to},
    )
    return row


def approve(db, user: ErpUser, connection_id: str, request_id: int, body: RefundVersioned) -> dict:
    row = _request(db, connection_id, request_id)
    settlement, _ = _settlement_context(db, connection_id, row.settlement_id, row.order_external_id)
    if _reversed(db, connection_id, settlement.id):
        raise http_error(409, "already_reversed", "A baixa foi estornada depois da solicitação; não há o que aprovar")
    row = _move(
        db, user, connection_id, request_id, body.expectedVersion, allowed=("requested",), to="approved",
        action="approved", reason=body.note, decided_by=user.username, decided_at=utcnow(), decision_note=body.note,
    )
    return serialize(db, row, with_events=True)


def reject(db, user: ErpUser, connection_id: str, request_id: int, body: RefundReason) -> dict:
    row = _move(
        db, user, connection_id, request_id, body.expectedVersion, allowed=("requested",), to="rejected",
        action="rejected", reason=body.reason, decided_by=user.username, decided_at=utcnow(), decision_note=body.reason,
    )
    return serialize(db, row, with_events=True)


def cancel(db, user: ErpUser, connection_id: str, request_id: int, body: RefundReason) -> dict:
    current = _request(db, connection_id, request_id)
    if current.requested_by != user.username and not user.can("refunds:approve"):
        raise http_error(403, "not_owner", "Somente quem solicitou, ou quem aprova reembolsos, pode cancelar")
    row = _move(
        db, user, connection_id, request_id, body.expectedVersion, allowed=("requested", "approved"),
        to="cancelled", action="cancelled", reason=body.reason, decision_note=body.reason,
    )
    return serialize(db, row, with_events=True)


def confirm_external(db, user: ErpUser, connection_id: str, request_id: int, body: RefundExternalConfirm) -> dict:
    row = _move(
        db, user, connection_id, request_id, body.expectedVersion, allowed=("approved",), to="external_confirmed",
        action="external_confirmed", reason=body.note, external_reference=body.reference.strip(),
        external_confirmed_by=user.username, external_confirmed_at=utcnow(),
    )
    return serialize(db, row, with_events=True)


def apply_local_reversal(db, user: ErpUser, connection_id: str, request_id: int, body: RefundVersioned) -> dict:
    row = _request(db, connection_id, request_id)
    settlement, _ = _settlement_context(db, connection_id, row.settlement_id, row.order_external_id)
    if row.status == "approved" and row.version == body.expectedVersion:
        if row.amount != settlement.amount:
            raise http_error(
                409, "partial_reversal_unsupported",
                "O estorno local existente é integral e este reembolso é parcial. Registre a devolução "
                "externa e confirme manualmente; não há estorno parcial local nesta base.",
                refundAmount=money(row.amount), settlementAmount=money(settlement.amount),
            )
        if _reversed(db, connection_id, settlement.id):
            raise http_error(409, "already_reversed", "Esta baixa já foi estornada")
        reversal = finance.reverse(
            db, user, connection_id, settlement_id=settlement.id, reason=f"Reembolso #{row.id}: {row.reason}",
            key=f"refund:{row.id}",
        )
        row = _move(
            db, user, connection_id, request_id, body.expectedVersion, allowed=("approved",), to="reversed_local",
            action="reversed_local", reason=body.note, reversal_settlement_id=reversal.id,
        )
        return serialize(db, row, with_events=True)
    # estado/versão inesperados: deixa _move produzir o 409 correto
    row = _move(
        db, user, connection_id, request_id, body.expectedVersion, allowed=("approved",), to="reversed_local",
        action="reversed_local",
    )
    return serialize(db, row, with_events=True)


def get(db, connection_id: str, request_id: int) -> dict:
    return serialize(db, _request(db, connection_id, request_id), with_events=True)


def list_requests(
    db, connection_id: str, *, status: str | None, order_id: str | None, page: int, page_size: int, order: str
):
    R = op.ErpRefundRequest
    query = select(R).where(R.connection_id == connection_id)
    if status:
        query = query.where(R.status == status)
    if order_id:
        query = query.where(R.order_external_id == order_id)
    result, key, direction = paginate(
        db, query, id_column=R.id, sort_columns={"requestedAt": R.requested_at, "amount": R.amount},
        sort=None, default_sort="requestedAt", order=order, page=page, page_size=page_size,
    )
    return result.envelope(
        lambda r: serialize(db, r), sort=key, order=direction, filters={"status": status, "orderId": order_id}
    )
