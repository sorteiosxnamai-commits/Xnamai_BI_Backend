import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException
from sqlalchemy import Float, case, cast, func, select
from sqlalchemy.orm import Session

from app.analytics import REVENUE_STATUSES, _aware, _now, classify_customer
from app.config import settings
from app.domain.order_status import status_sql_in
from app.models import CrmAttendance, Customer, Order, OrderItem, Product
from app.services.crm import (
    _customer_extras,
    _customer_filters,
    _days_since_last_order,
    _iso,
    _money_total,
    _order_stats,
    _order_stats_subquery,
)

log = logging.getLogger(__name__)
CACHE_HOURS = 24
AI_BATCH_SIZE = 40

INSTRUCTIONS = (
    "Voce e um analista comercial B2B. Recebera clientes com historico de compras. "
    "Atribua potencialScore de 0 a 100 para chance de recompra/atendimento comercial agora. "
    "Considere faturamento, frequencia, ticket, dias sem comprar, segmento e mix de produtos. "
    "Priorize alto valor com inatividade recente (30-180 dias) e clientes em risco/recuperar. "
    "Penalize quem comprou nos ultimos 7 dias. "
    "Responda SOMENTE JSON valido: "
    '{"rankings":[{"id":"mercos_id","potencialScore":85,"motivo":"frase curta"}]} '
    "Inclua todos os ids recebidos, ordenados do maior potencialScore para o menor."
)


def heuristic_potential_score(
    *,
    revenue: float,
    orders: int,
    days_since: int | None,
    segment: str,
) -> float:
    rev = max(revenue, 0.0)
    ord_n = max(orders, 0)
    days = days_since if days_since is not None else 9999

    if days < 7:
        recency = 0.15
    elif days < 30:
        recency = 0.35 + (days - 7) / 23 * 0.35
    elif days <= 180:
        recency = 0.7 + min(days - 30, 150) / 150 * 0.3
    else:
        recency = max(0.35, 180 / days)

    segment_boost = {
        "recuperar": 1.25,
        "em_risco": 1.15,
        "ativo": 0.85,
        "lead_novo": 0.5,
    }.get(segment, 1.0)

    frequency = min(math.sqrt(ord_n), 8.0)
    return round(math.log1p(rev) * recency * frequency * segment_boost, 2)


def _heuristic_score_expr(db: Session, stats_sq):
    days = _days_since_last_order(db, stats_sq.c.last_order_at)
    revenue = cast(func.coalesce(stats_sq.c.revenue, 0), Float)
    orders = cast(func.coalesce(stats_sq.c.orders, 0), Float)
    recency = case(
        (days < 7, 0.15),
        (days < 30, 0.35 + ((days - 7) / 23.0) * 0.35),
        (days <= 180, 0.7 + ((days - 30) / 150.0) * 0.3),
        else_=func.max(0.35, 180.0 / days),
    )
    frequency = func.min(func.sqrt(orders), 8.0)
    return func.ln(revenue + 1) * recency * frequency


def _compact_products(db: Session, customer_ids: list[str], limit_each: int = 5) -> dict[str, list[dict]]:
    if not customer_ids:
        return {}
    rows = db.execute(
        select(
            Order.customer_mercos_id,
            OrderItem.name,
            OrderItem.quantity,
            OrderItem.total,
        )
        .join(Order, Order.mercos_id == OrderItem.order_mercos_id)
        .where(
            Order.customer_mercos_id.in_(customer_ids),
            status_sql_in(Order.status, REVENUE_STATUSES),
            OrderItem.excluded.is_(False),
        )
        .order_by(Order.issued_at.desc())
    ).all()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        bucket = grouped.setdefault(row.customer_mercos_id, [])
        if len(bucket) >= limit_each:
            continue
        bucket.append(
            {
                "name": (row.name or "Produto")[:80],
                "quantity": float(row.quantity or 0),
                "total": _money_total(row.total),
            }
        )
    return grouped


def _build_ai_batch_payload(db: Session, customers: list[Customer], stats_map: dict) -> list[dict]:
    ids = [c.mercos_id for c in customers]
    products = _compact_products(db, ids, 5)
    now = _now()
    payload = []
    for customer in customers:
        stats = stats_map.get(customer.mercos_id)
        segment = classify_customer(stats, inactive_days=90, risk_days=90, now=now)
        days_since = None
        if stats and stats.last_order_at:
            last = _aware(stats.last_order_at)
            days_since = (now - last).days if last else None
        revenue = _money_total(stats.revenue) if stats else 0.0
        orders = int(stats.orders) if stats else 0
        extras = _customer_extras(customer)
        payload.append(
            {
                "id": customer.mercos_id,
                "name": customer.name,
                "city": customer.city,
                "state": customer.state,
                "branch": extras.get("branch"),
                "segment": segment,
                "orders": orders,
                "revenue": revenue,
                "ticketAverage": round(revenue / orders, 2) if orders else 0,
                "daysSinceLastOrder": days_since,
                "heuristicScore": heuristic_potential_score(
                    revenue=revenue,
                    orders=orders,
                    days_since=days_since,
                    segment=segment,
                ),
                "topProducts": products.get(customer.mercos_id, []),
            }
        )
    return payload


def _parse_ai_rankings(text: str) -> list[dict]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    parsed = json.loads(cleaned)
    rankings = parsed.get("rankings") if isinstance(parsed, dict) else parsed
    if not isinstance(rankings, list):
        raise HTTPException(502, "Resposta da IA em formato invalido")
    return rankings


def _call_openai_rank(batch: list[dict]) -> list[dict]:
    cfg = settings()
    if not cfg.openai_api_key:
        raise HTTPException(503, "OPENAI_API_KEY nao configurada")
    body = {
        "model": cfg.openai_model,
        "instructions": INSTRUCTIONS,
        "input": json.dumps(batch, ensure_ascii=False, default=str),
    }
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
        response = client.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {cfg.openai_api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
    if response.status_code >= 400:
        log.error("OpenAI rank error %s: %s", response.status_code, response.text[:800])
        raise HTTPException(502, "Falha ao priorizar leads com IA")
    payload = response.json()
    text = payload.get("output_text") or ""
    if not text:
        for item in payload.get("output") or []:
            if item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    text += part.get("text") or ""
    if not text.strip():
        raise HTTPException(502, "IA nao retornou ranking")
    return _parse_ai_rankings(text)


def _upsert_priority_row(db: Session, customer_id: str) -> CrmAttendance:
    row = db.scalar(select(CrmAttendance).where(CrmAttendance.customer_mercos_id == customer_id))
    now = datetime.now(timezone.utc)
    if row is None:
        row = CrmAttendance(
            customer_mercos_id=customer_id,
            status="open",
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()
    return row


def _heuristic_rankings(batch: list[dict]) -> list[dict]:
    scores = [float(item.get("heuristicScore") or 0) for item in batch]
    low, high = min(scores), max(scores)
    span = high - low
    rankings = []
    for item, raw in zip(batch, scores):
        if span > 0:
            score = round((raw - low) / span * 100, 1)
        else:
            score = 50.0
        rankings.append(
            {
                "id": item["id"],
                "potencialScore": score,
                "motivo": "Prioridade heuristica (faturamento, recencia e frequencia)",
            }
        )
    rankings.sort(key=lambda row: row["potencialScore"], reverse=True)
    return rankings


def score_leads_with_ai(db: Session, customer_ids: list[str], *, force: bool = False) -> int:
    if not customer_ids:
        return 0
    customers = list(
        db.scalars(select(Customer).where(Customer.mercos_id.in_(customer_ids)))
    )
    if not customers:
        return 0

    from app.services.crm import _order_stats

    stats_map = _order_stats(db)
    batch = _build_ai_batch_payload(db, customers, stats_map)
    if not batch:
        return 0

    cfg = settings()
    rankings: list[dict] = []
    if cfg.openai_api_key:
        try:
            rankings = _call_openai_rank(batch)
        except HTTPException as exc:
            log.warning("OpenAI rank failed, using heuristic fallback: %s", exc.detail)
    if not rankings:
        rankings = _heuristic_rankings(batch)
    now = datetime.now(timezone.utc)
    scored = 0
    for item in rankings:
        customer_id = str(item.get("id") or "")
        if customer_id not in customer_ids:
            continue
        row = db.scalar(select(CrmAttendance).where(CrmAttendance.customer_mercos_id == customer_id))
        if row and row.ai_priority_score is not None and not force:
            cached_at = _aware(row.ai_priority_at)
            if cached_at and (now - cached_at) < timedelta(hours=CACHE_HOURS):
                continue
        try:
            score = float(item.get("potencialScore") or item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(score, 100.0))
        reason = str(item.get("motivo") or item.get("reason") or "").strip() or None
        row = _upsert_priority_row(db, customer_id)
        row.ai_priority_score = score
        row.ai_priority_reason = reason
        row.ai_priority_at = now
        row.updated_at = now
        db.add(row)
        scored += 1
    db.commit()
    return scored


def pick_buyers_for_ai_scoring(
    db: Session,
    *,
    finished_ids: set[str],
    search: str | None,
    limit: int,
    refresh: bool = False,
) -> list[str]:
    stats_sq = _order_stats_subquery()
    filters = _customer_filters(finished_ids=finished_ids, search=search)
    buyer_filters = [*filters, stats_sq.c.customer_mercos_id.isnot(None)]
    heuristic = _heuristic_score_expr(db, stats_sq)
    stmt = (
        select(Customer.mercos_id)
        .select_from(Customer)
        .outerjoin(stats_sq, Customer.mercos_id == stats_sq.c.customer_mercos_id)
        .outerjoin(CrmAttendance, Customer.mercos_id == CrmAttendance.customer_mercos_id)
        .where(*buyer_filters)
    )
    if refresh:
        stmt = stmt.order_by(
            CrmAttendance.ai_priority_at.asc().nulls_first(),
            heuristic.desc(),
            stats_sq.c.revenue.desc().nulls_last(),
        )
    else:
        stmt = stmt.where(CrmAttendance.ai_priority_score.is_(None)).order_by(
            heuristic.desc(),
            stats_sq.c.revenue.desc().nulls_last(),
        )
    return list(db.scalars(stmt.limit(limit)))


def pick_unscored_buyer_ids(
    db: Session,
    *,
    finished_ids: set[str],
    search: str | None,
    limit: int,
) -> list[str]:
    return pick_buyers_for_ai_scoring(
        db,
        finished_ids=finished_ids,
        search=search,
        limit=limit,
        refresh=False,
    )
