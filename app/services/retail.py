"""Retail B2C candidate pool, composite recommendation score and list APIs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import Session

from app.analytics import MAX_ORDER_TOTAL, REVENUE_STATUSES, _aware
from app.domain.order_status import status_sql_in
from app.models import Order, OrderItem, Product, RetailProductAnalysis
from app.services.retail_economics import (
    default_economics,
    estimated_cost,
    evaluate_channels,
    merge_economics,
)

CANDIDATE_POOL_SIZE = 250
TOP_RECOMMENDED = 100
CACHE_HOURS = 24

PLATFORM_LABELS = {
    "mercado_livre": "Mercado Livre",
    "shopee": "Shopee",
    "tiktok": "TikTok Shop",
    "nuvemshop": "Nuvemshop",
    "site_proprio": "Site proprio",
}

APPEAL_SCORE = {"alto": 90.0, "medio": 60.0, "m?dio": 60.0, "baixo": 30.0}


def _money(value) -> float:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, amount), 2)


def _iso(value) -> str | None:
    aware = _aware(value)
    return aware.isoformat() if aware else None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _product_sales_subquery(db: Session, *, days: int | None = 90):
    amount = func.coalesce(OrderItem.total, 0)
    conditions = [
        status_sql_in(Order.status, REVENUE_STATUSES),
        OrderItem.excluded.is_(False),
        OrderItem.product_mercos_id.is_not(None),
        func.coalesce(Order.total, 0) < MAX_ORDER_TOTAL,
    ]
    if days is not None:
        start = datetime.now(timezone.utc) - timedelta(days=days)
        conditions.append(Order.issued_at >= start)
    return (
        select(
            OrderItem.product_mercos_id.label("product_id"),
            func.coalesce(func.sum(OrderItem.quantity), 0).label("quantity"),
            func.coalesce(func.sum(amount), 0).label("revenue"),
            func.count(func.distinct(Order.mercos_id)).label("orders"),
        )
        .join(Order, Order.mercos_id == OrderItem.order_mercos_id)
        .where(*conditions)
        .group_by(OrderItem.product_mercos_id)
        .subquery()
    )


def candidate_pool(db: Session, *, limit: int = CANDIDATE_POOL_SIZE, days: int | None = 90) -> list[dict[str, Any]]:
    sales = _product_sales_subquery(db, days=days)
    rows = db.execute(
        select(Product, sales.c.quantity, sales.c.revenue, sales.c.orders)
        .join(sales, sales.c.product_id == Product.mercos_id)
        .where(Product.active.is_(True))
        .order_by(cast(sales.c.revenue, Float).desc(), cast(sales.c.quantity, Float).desc(), Product.name.asc())
        .limit(limit)
    ).all()
    pool: list[dict[str, Any]] = []
    for product, quantity, revenue, orders in rows:
        pool.append(
            {
                "id": product.mercos_id,
                "code": product.code,
                "name": product.name,
                "categoryId": product.category_mercos_id or product.category_id,
                "listPrice": _money(product.list_price),
                "minimumPrice": _money(product.minimum_price) if product.minimum_price is not None else None,
                "stock": _money(product.stock),
                "active": bool(product.active),
                "mercosRevenue": _money(revenue),
                "mercosQuantity": _money(quantity),
                "mercosOrders": int(orders or 0),
            }
        )
    return pool


def _analysis_map(db: Session, product_ids: list[str]) -> dict[str, RetailProductAnalysis]:
    if not product_ids:
        return {}
    rows = db.scalars(
        select(RetailProductAnalysis).where(RetailProductAnalysis.product_mercos_id.in_(product_ids))
    ).all()
    return {row.product_mercos_id: row for row in rows}


def _appeal_score(ai_payload: dict | None, scores: dict | None) -> float:
    if scores and scores.get("appealScore") is not None:
        try:
            return _clamp(float(scores["appealScore"]))
        except (TypeError, ValueError):
            pass
    appeal = None
    if ai_payload:
        appeal = ai_payload.get("apelo") or ai_payload.get("appeal")
    if isinstance(appeal, (int, float)):
        return _clamp(float(appeal))
    if isinstance(appeal, str):
        return APPEAL_SCORE.get(appeal.strip().lower(), 50.0)
    return 50.0


def _sell_potential(candidate: dict[str, Any], ai_payload: dict | None, scores: dict | None) -> float:
    if scores and scores.get("potencialScore") is not None:
        try:
            return _clamp(float(scores["potencialScore"]))
        except (TypeError, ValueError):
            pass
    if ai_payload and ai_payload.get("potencialScore") is not None:
        try:
            return _clamp(float(ai_payload["potencialScore"]))
        except (TypeError, ValueError):
            pass
    revenue = float(candidate.get("mercosRevenue") or 0)
    qty = float(candidate.get("mercosQuantity") or 0)
    # Soft normalize against pool-typical values without needing global max.
    revenue_part = _clamp((revenue / 50_000.0) * 100.0)
    qty_part = _clamp((qty / 500.0) * 100.0)
    stock = float(candidate.get("stock") or 0)
    stock_part = 70.0 if stock > 0 else 35.0
    return round(0.5 * revenue_part + 0.35 * qty_part + 0.15 * stock_part, 2)


def _margin_score(best_margin_pct: float | None) -> float:
    if best_margin_pct is None:
        return 40.0
    # 0% -> 20, 20% -> 60, 40%+ -> ~100
    return _clamp(20.0 + float(best_margin_pct) * 2.0)


def _logistics_score(ai_payload: dict | None, scores: dict | None) -> float:
    if scores and scores.get("logisticsScore") is not None:
        try:
            return _clamp(float(scores["logisticsScore"]))
        except (TypeError, ValueError):
            pass
    if not ai_payload:
        return 55.0
    risk = str(ai_payload.get("risco") or ai_payload.get("risk") or "").lower()
    if "alto" in risk or "peso" in risk or "frete caro" in risk:
        return 35.0
    if "baixo" in risk:
        return 80.0
    return 55.0


def _spread_score(list_price: float, best_retail_price: float | None) -> float:
    if not list_price or best_retail_price is None or best_retail_price <= 0:
        return 50.0
    # Positive spread (retail > list) is good for B2C markup potential.
    spread_pct = ((best_retail_price - list_price) / list_price) * 100.0
    return _clamp(50.0 + spread_pct)


def _reason_short(
    *,
    ai_payload: dict | None,
    scores: dict | None,
    best_channel: dict | None,
    candidate: dict[str, Any],
) -> str:
    if scores and scores.get("reasonShort"):
        return str(scores["reasonShort"])
    if ai_payload and ai_payload.get("motivoEscolha"):
        return str(ai_payload["motivoEscolha"])
    platform = (best_channel or {}).get("label") or "varejo"
    margin = (best_channel or {}).get("marginPct")
    margin_txt = f"{margin:.0f}% margem" if margin is not None else "margem estimada"
    revenue = candidate.get("mercosRevenue") or 0
    return f"Giro atacado R$ {revenue:,.0f}; indicado em {platform} com {margin_txt}".replace(",", ".")


def compose_product_score(
    candidate: dict[str, Any],
    analysis: RetailProductAnalysis | None,
    *,
    economics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    eco = merge_economics(economics)
    ai_payload = analysis.ai_payload if analysis else None
    market_prices = analysis.market_prices if analysis else None
    cached_scores = analysis.scores if analysis else None
    channels = evaluate_channels(
        market_prices=market_prices,
        list_price=float(candidate.get("listPrice") or 0),
        economics=eco,
    )
    best = channels[0] if channels else None
    appeal = _appeal_score(ai_payload, cached_scores)
    sell = _sell_potential(candidate, ai_payload, cached_scores)
    margin_pct = float(best["marginPct"]) if best else None
    margin = _margin_score(margin_pct)
    logistics = _logistics_score(ai_payload, cached_scores)
    spread = _spread_score(
        float(candidate.get("listPrice") or 0),
        float(best["retailPrice"]) if best else None,
    )
    recommendation = round(
        0.25 * appeal + 0.25 * sell + 0.30 * margin + 0.10 * logistics + 0.10 * spread,
        2,
    )
    analyzed = bool(analysis and analysis.generated_at)
    stale = False
    if analyzed:
        generated = _aware(analysis.generated_at)
        stale = not generated or (datetime.now(timezone.utc) - generated) >= timedelta(hours=CACHE_HOURS)

    platform = (cached_scores or {}).get("bestPlatform") or (best or {}).get("platform") or "site_proprio"
    if ai_payload and ai_payload.get("melhorPlataforma"):
        platform = ai_payload["melhorPlataforma"]
    shipping = (cached_scores or {}).get("bestShipping") or (best or {}).get("shippingKey") or "melhor_envio"
    if ai_payload and ai_payload.get("melhorEnvio"):
        shipping = ai_payload["melhorEnvio"]

    reasons: list[str] = []
    if cached_scores and isinstance(cached_scores.get("reasonDetail"), list):
        reasons = [str(x) for x in cached_scores["reasonDetail"] if x]
    elif ai_payload and isinstance(ai_payload.get("razoes"), list):
        reasons = [str(x) for x in ai_payload["razoes"] if x]
    if not reasons:
        reasons = [
            f"Apelo {appeal:.0f}/100",
            f"Potencial de venda {sell:.0f}/100",
            f"Margem liquida {margin_pct:.1f}%" if margin_pct is not None else "Margem estimada",
        ]

    cost = estimated_cost(float(candidate.get("listPrice") or 0), eco["custoPct"])
    return {
        **candidate,
        "analyzed": analyzed,
        "stale": stale,
        "generatedAt": _iso(analysis.generated_at) if analysis else None,
        "recomendacaoScore": recommendation,
        "appealScore": round(appeal, 2),
        "potencialScore": round(sell, 2),
        "logisticsScore": round(logistics, 2),
        "spreadScore": round(spread, 2),
        "apelo": (ai_payload or {}).get("apelo") or ("alto" if appeal >= 75 else "medio" if appeal >= 45 else "baixo"),
        "melhorPlataforma": platform,
        "melhorPlataformaLabel": PLATFORM_LABELS.get(str(platform), str(platform)),
        "melhorEnvio": shipping,
        "melhorEnvioLabel": (best or {}).get("shippingLabel") or shipping,
        "porquePlataforma": (ai_payload or {}).get("porquePlataforma"),
        "margemLiquida": best["netMargin"] if best else None,
        "margemLiquidaPct": margin_pct,
        "custoEstimado": cost,
        "motivoCurto": _reason_short(
            ai_payload=ai_payload,
            scores=cached_scores,
            best_channel=best,
            candidate=candidate,
        ),
        "motivos": reasons,
        "channels": channels,
        "confidence": (ai_payload or {}).get("confidence"),
        "sources": (ai_payload or {}).get("sources") or [],
        "aiPayload": ai_payload,
        "marketPrices": market_prices,
    }


def recommended_products(
    db: Session,
    *,
    top: int = TOP_RECOMMENDED,
    pool_size: int = CANDIDATE_POOL_SIZE,
    days: int | None = 90,
    economics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pool = candidate_pool(db, limit=pool_size, days=days)
    analyses = _analysis_map(db, [item["id"] for item in pool])
    scored = [compose_product_score(item, analyses.get(item["id"]), economics=economics) for item in pool]
    # Analyzed first by score; pending heuristic scores still compete but sort after ties via analyzed flag.
    scored.sort(
        key=lambda row: (
            1 if row["analyzed"] else 0,
            row["recomendacaoScore"],
            row["mercosRevenue"],
        ),
        reverse=True,
    )
    top_rows = scored[:top]
    for index, row in enumerate(top_rows, start=1):
        row["rank"] = index

    analyzed_count = sum(1 for row in pool if analyses.get(row["id"]) and analyses[row["id"]].generated_at)
    platform_dist: dict[str, int] = {}
    appeal_dist = {"alto": 0, "medio": 0, "baixo": 0}
    margins = []
    for row in top_rows:
        key = str(row.get("melhorPlataforma") or "site_proprio")
        platform_dist[key] = platform_dist.get(key, 0) + 1
        apelo = str(row.get("apelo") or "medio").lower().replace("?", "e")
        if apelo not in appeal_dist:
            apelo = "medio"
        appeal_dist[apelo] += 1
        if row.get("margemLiquidaPct") is not None:
            margins.append(float(row["margemLiquidaPct"]))

    return {
        "items": top_rows,
        "poolSize": len(pool),
        "analyzedCount": analyzed_count,
        "pendingCount": max(0, len(pool) - analyzed_count),
        "top": top,
        "dashboard": {
            "platformDistribution": [
                {"platform": key, "label": PLATFORM_LABELS.get(key, key), "count": count}
                for key, count in sorted(platform_dist.items(), key=lambda x: -x[1])
            ],
            "appealDistribution": appeal_dist,
            "avgMarginPct": round(sum(margins) / len(margins), 2) if margins else None,
            "avgRecommendationScore": round(
                sum(float(row["recomendacaoScore"]) for row in top_rows) / len(top_rows), 2
            )
            if top_rows
            else None,
        },
        "economics": merge_economics(economics),
        "disclaimer": (
            "Precos de mercado via busca publica/IA. "
            "Custo estimado = preco de tabela x (1 - custoPct/100). "
            "Ranking por recomendacao B2C, nao por faturamento Mercos."
        ),
    }


def list_candidates(
    db: Session,
    *,
    pool_size: int = CANDIDATE_POOL_SIZE,
    days: int | None = 90,
    economics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pool = candidate_pool(db, limit=pool_size, days=days)
    analyses = _analysis_map(db, [item["id"] for item in pool])
    items = []
    for item in pool:
        scored = compose_product_score(item, analyses.get(item["id"]), economics=economics)
        items.append(
            {
                "id": scored["id"],
                "code": scored["code"],
                "name": scored["name"],
                "listPrice": scored["listPrice"],
                "mercosRevenue": scored["mercosRevenue"],
                "mercosQuantity": scored["mercosQuantity"],
                "analyzed": scored["analyzed"],
                "stale": scored["stale"],
                "recomendacaoScore": scored["recomendacaoScore"],
                "melhorPlataforma": scored["melhorPlataforma"],
                "motivoCurto": scored["motivoCurto"],
                "generatedAt": scored["generatedAt"],
            }
        )
    analyzed_count = sum(1 for row in items if row["analyzed"])
    return {
        "items": items,
        "poolSize": len(items),
        "analyzedCount": analyzed_count,
        "pendingCount": max(0, len(items) - analyzed_count),
    }


def product_analysis_detail(
    db: Session,
    product_id: str,
    *,
    economics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    product = db.scalar(select(Product).where(Product.mercos_id == product_id))
    if not product:
        raise HTTPException(404, "Produto nao encontrado")
    sales = _product_sales_subquery(db, days=90)
    sales_row = db.execute(
        select(sales.c.quantity, sales.c.revenue, sales.c.orders).where(sales.c.product_id == product_id)
    ).first()
    candidate = {
        "id": product.mercos_id,
        "code": product.code,
        "name": product.name,
        "categoryId": product.category_mercos_id or product.category_id,
        "listPrice": _money(product.list_price),
        "minimumPrice": _money(product.minimum_price) if product.minimum_price is not None else None,
        "stock": _money(product.stock),
        "active": bool(product.active),
        "mercosRevenue": _money(sales_row.revenue) if sales_row else 0.0,
        "mercosQuantity": _money(sales_row.quantity) if sales_row else 0.0,
        "mercosOrders": int(sales_row.orders or 0) if sales_row else 0,
    }
    analysis = db.scalar(
        select(RetailProductAnalysis).where(RetailProductAnalysis.product_mercos_id == product_id)
    )
    scored = compose_product_score(candidate, analysis, economics=economics)
    return scored


def economics_config() -> dict[str, Any]:
    return default_economics()
