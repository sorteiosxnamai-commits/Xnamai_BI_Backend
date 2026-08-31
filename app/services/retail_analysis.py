"""Retail product AI analysis with public web search and batch processing."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics import _aware
from app.config import settings
from app.models import Product, RetailProductAnalysis
from app.services.retail import (
    CACHE_HOURS,
    CANDIDATE_POOL_SIZE,
    candidate_pool,
    compose_product_score,
    product_analysis_detail,
)
from app.services.retail_economics import evaluate_channels, merge_economics

log = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Voce e um analista de varejo B2C no Brasil (XNamai). "
    "O preco de tabela Mercos e o CUSTO de compra, NAO o preco de venda ao consumidor. "
    "Pesquise anuncios PUBLICOS do MESMO produto (mesmo nome/codigo) vendidos por vendedores "
    "em Mercado Livre, Shopee, TikTok Shop, Nuvemshop e lojas/site proprio. "
    "Para cada plataforma, informe o preco que o consumidor paga no anuncio, o vendedor/loja e a URL. "
    "NUNCA copie o preco de tabela Mercos como preco de venda. Se nao achar anuncio, use null. "
    "Nao invente precos nem URLs. "
    "Responda SOMENTE com JSON valido (sem markdown), neste formato: "
    '{"apelo":"alto|medio|baixo","apeloJustificativa":"texto",'
    '"potencialScore":75,'
    '"melhorPlataforma":"mercado_livre|shopee|tiktok|nuvemshop|site_proprio",'
    '"porquePlataforma":"frase curta",'
    '"melhorEnvio":"mercado_envios|shopee_entrega|melhor_envio|correios_pac|correios_sedex",'
    '"envioJustificativa":"texto",'
    '"motivoEscolha":"frase curta do porque indicar no Top 100 varejo",'
    '"razoes":["razao 1","razao 2"],'
    '"risco":"baixo|medio|alto - detalhe",'
    '"marketPrices":{'
    '"mercado_livre":{"price":99.9,"freight":22,"seller":"Loja X","url":"https://...","source":"Mercado Livre"},'
    '"shopee":{"price":95.0,"freight":18,"seller":"Seller Y","url":"https://...","source":"Shopee"},'
    '"tiktok":{"price":null,"freight":null,"seller":null,"url":null,"source":null},'
    '"nuvemshop":{"price":null,"freight":null,"seller":null,"url":null,"source":null},'
    '"site_proprio":{"price":null,"freight":null,"seller":null,"url":null,"source":null}'
    "},"
    '"sources":[{"title":"titulo","url":"https://..."}],'
    '"confidence":"alta|media|baixa"}'
)


def _iso(value) -> str | None:
    aware = _aware(value)
    return aware.isoformat() if aware else None


def _extract_output_text(payload: dict) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"]).strip()
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "output_text" and part.get("text"):
                chunks.append(str(part["text"]))
    return "\n".join(chunks).strip()


def _extract_sources(payload: dict) -> list[dict]:
    sources: list[dict] = []
    seen: set[str] = set()
    for item in payload.get("output") or []:
        if item.get("type") != "web_search_call":
            continue
        action = item.get("action") or {}
        for source in action.get("sources") or []:
            url = source.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append({"title": source.get("title") or url, "url": url})
    return sources


def _parse_analysis(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as error:
        log.warning("Retail OpenAI JSON parse failed: %s", error)
        raise HTTPException(502, "Resposta da IA em formato invalido") from error
    if not isinstance(parsed, dict):
        raise HTTPException(502, "Resposta da IA em formato invalido")
    return parsed


def _build_prompt(product: dict[str, Any]) -> str:
    hint = " ".join(
        part for part in [product.get("name"), product.get("code"), "Brasil varejo"] if part
    )
    context = {
        "id": product.get("id"),
        "name": product.get("name"),
        "code": product.get("code"),
        "listPriceAsCost": product.get("listPrice"),
        "stock": product.get("stock"),
        "mercosRevenue90d": product.get("mercosRevenue"),
        "mercosQuantity90d": product.get("mercosQuantity"),
        "mercosOrders90d": product.get("mercosOrders"),
    }
    return (
        f"Pesquise anuncios do MESMO produto vendidos por vendedores em cada plataforma: {hint}.\n"
        f"Custo de compra (preco de tabela Mercos, NAO use como preco de venda): {product.get('listPrice')}.\n"
        f"Dados internos Mercos:\n{json.dumps(context, ensure_ascii=False, default=str)}"
    )


def _call_openai(prompt: str) -> dict:
    cfg = settings()
    if not cfg.openai_api_key:
        raise HTTPException(503, "OPENAI_API_KEY nao configurada")
    body = {
        "model": cfg.openai_model,
        "instructions": INSTRUCTIONS,
        "input": prompt,
        "tools": [
            {
                "type": "web_search",
                "user_location": {"type": "approximate", "country": "BR"},
            }
        ],
        "include": ["web_search_call.action.sources"],
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
        log.error("OpenAI retail error %s: %s", response.status_code, response.text[:800])
        raise HTTPException(502, "Falha ao gerar analise varejo com IA")
    payload = response.json()
    text = _extract_output_text(payload)
    if not text:
        raise HTTPException(502, "IA nao retornou analise")
    analysis = _parse_analysis(text)
    if not analysis.get("sources"):
        analysis["sources"] = _extract_sources(payload)
    return analysis


def _heuristic_analysis(product: dict[str, Any], economics: dict[str, Any] | None = None) -> dict:
    """Fallback without invented retail prices - only Mercos signals."""
    del economics
    revenue = float(product.get("mercosRevenue") or 0)
    qty = float(product.get("mercosQuantity") or 0)
    potencial = min(100.0, round((revenue / 40_000.0) * 50 + (qty / 400.0) * 40 + 10, 1))
    apelo = "alto" if potencial >= 70 else "medio" if potencial >= 40 else "baixo"
    return {
        "apelo": apelo,
        "apeloJustificativa": "Sem busca web: score so com giro Mercos",
        "potencialScore": potencial,
        "melhorPlataforma": "site_proprio",
        "porquePlataforma": "Sem anuncios reais encontrados ainda",
        "melhorEnvio": "melhor_envio",
        "envioJustificativa": "Aguardando precos reais por plataforma",
        "motivoEscolha": f"Giro atacado R$ {revenue:,.0f}; aguardando precos reais".replace(",", "."),
        "razoes": [
            f"Faturamento Mercos 90d: R$ {revenue:,.0f}".replace(",", "."),
            f"Quantidade vendida: {qty:.0f}",
            "Preco por plataforma pendente de busca real",
        ],
        "risco": "alto - sem precos publicos confirmados",
        "marketPrices": {},
        "sources": [],
        "confidence": "baixa",
        "heuristic": True,
    }


def _normalize_market_prices(raw: Any, *, list_price: float | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    cost = None
    try:
        cost = float(list_price) if list_price is not None else None
    except (TypeError, ValueError):
        cost = None
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        price = value.get("price")
        freight = value.get("freight")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        if price is not None and price <= 0:
            price = None
        if price is not None and cost is not None and cost > 0 and abs(price - cost) < 0.01:
            price = None
        try:
            freight = float(freight) if freight is not None else None
        except (TypeError, ValueError):
            freight = None
        out[str(key)] = {
            "price": price,
            "freight": freight,
            "url": value.get("url"),
            "source": value.get("source"),
            "seller": value.get("seller"),
            "shippingKey": value.get("shippingKey"),
        }
    return out


def _build_scores(product: dict[str, Any], ai_payload: dict, economics: dict | None = None) -> dict:
    market_prices = _normalize_market_prices(
        ai_payload.get("marketPrices"),
        list_price=float(product.get("listPrice") or 0),
    )
    channels = evaluate_channels(
        market_prices=market_prices,
        list_price=float(product.get("listPrice") or 0),
        economics=merge_economics(economics),
    )
    priced = [row for row in channels if row.get("hasPrice")]
    best = priced[0] if priced else None
    platform = ai_payload.get("melhorPlataforma") or (best or {}).get("platform") or "site_proprio"
    shipping = ai_payload.get("melhorEnvio") or (best or {}).get("shippingKey") or "melhor_envio"
    temp = RetailProductAnalysis(
        product_mercos_id=product["id"],
        ai_payload=ai_payload,
        market_prices=market_prices,
        scores={
            "potencialScore": ai_payload.get("potencialScore"),
            "bestPlatform": platform,
            "bestShipping": shipping,
            "reasonShort": ai_payload.get("motivoEscolha"),
            "reasonDetail": ai_payload.get("razoes") or [],
        },
        generated_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    composed = compose_product_score(product, temp, economics=economics)
    return {
        "recomendacaoScore": composed["recomendacaoScore"],
        "appealScore": composed["appealScore"],
        "potencialScore": composed["potencialScore"],
        "logisticsScore": composed["logisticsScore"],
        "spreadScore": composed["spreadScore"],
        "bestPlatform": platform,
        "bestShipping": shipping,
        "marginBestChannel": composed.get("margemLiquidaPct"),
        "reasonShort": composed["motivoCurto"],
        "reasonDetail": composed["motivos"],
    }


def _upsert_analysis(db: Session, product_id: str) -> RetailProductAnalysis:
    row = db.scalar(
        select(RetailProductAnalysis).where(RetailProductAnalysis.product_mercos_id == product_id)
    )
    now = datetime.now(timezone.utc)
    if row is None:
        row = RetailProductAnalysis(
            product_mercos_id=product_id,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()
    return row


def _is_fresh(row: RetailProductAnalysis | None) -> bool:
    if not row or not row.generated_at:
        return False
    generated = _aware(row.generated_at)
    if not generated:
        return False
    return (datetime.now(timezone.utc) - generated) < timedelta(hours=CACHE_HOURS)


def analyze_product(
    db: Session,
    product_id: str,
    *,
    refresh: bool = False,
    economics: dict[str, Any] | None = None,
    allow_heuristic: bool = False,
) -> dict[str, Any]:
    detail = product_analysis_detail(db, product_id, economics=economics)
    row = db.scalar(
        select(RetailProductAnalysis).where(RetailProductAnalysis.product_mercos_id == product_id)
    )
    if row and not refresh and _is_fresh(row):
        return {**detail, "cached": True}

    product = {
        "id": detail["id"],
        "code": detail["code"],
        "name": detail["name"],
        "listPrice": detail["listPrice"],
        "stock": detail["stock"],
        "mercosRevenue": detail["mercosRevenue"],
        "mercosQuantity": detail["mercosQuantity"],
        "mercosOrders": detail["mercosOrders"],
    }

    used_heuristic = False
    try:
        ai_payload = _call_openai(_build_prompt(product))
    except HTTPException as error:
        if not allow_heuristic or error.status_code not in {502, 503}:
            raise
        log.warning("Retail AI fallback to heuristic for %s: %s", product_id, error.detail)
        ai_payload = _heuristic_analysis(product, economics)
        used_heuristic = True

    market_prices = _normalize_market_prices(
        ai_payload.get("marketPrices"),
        list_price=float(product.get("listPrice") or 0),
    )
    ai_payload["marketPrices"] = market_prices
    scores = _build_scores(product, ai_payload, economics)
    now = datetime.now(timezone.utc)
    row = _upsert_analysis(db, product_id)
    row.ai_payload = ai_payload
    row.market_prices = market_prices
    row.scores = scores
    row.generated_at = now
    row.updated_at = now
    db.add(row)
    db.commit()

    refreshed = product_analysis_detail(db, product_id, economics=economics)
    return {**refreshed, "cached": False, "heuristic": used_heuristic}


def pending_candidate_ids(
    db: Session,
    *,
    pool_size: int | None = CANDIDATE_POOL_SIZE,
    refresh_stale: bool = False,
) -> list[str]:
    pool = candidate_pool(db, limit=pool_size)
    product_ids = [item["id"] for item in pool]
    rows = {
        row.product_mercos_id: row
        for row in db.scalars(
            select(RetailProductAnalysis).where(RetailProductAnalysis.product_mercos_id.in_(product_ids))
        ).all()
    } if product_ids else {}
    pending: list[str] = []
    for item in pool:
        row = rows.get(item["id"])
        if row is None or row.generated_at is None:
            pending.append(item["id"])
        elif refresh_stale and not _is_fresh(row):
            pending.append(item["id"])
    return pending


def analyze_batch(
    db: Session,
    *,
    limit: int = 10,
    pool_size: int | None = CANDIDATE_POOL_SIZE,
    refresh: bool = False,
    economics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    limit = max(1, min(20, int(limit)))
    ids = pending_candidate_ids(db, pool_size=pool_size, refresh_stale=refresh)[:limit]
    processed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for product_id in ids:
        try:
            result = analyze_product(
                db,
                product_id,
                refresh=True,
                economics=economics,
                allow_heuristic=False,
            )
            processed.append(
                {
                    "id": product_id,
                    "name": result.get("name"),
                    "recomendacaoScore": result.get("recomendacaoScore"),
                    "melhorPlataforma": result.get("melhorPlataforma"),
                    "heuristic": bool(result.get("heuristic")),
                }
            )
        except Exception as error:  # noqa: BLE001 - batch continues on single failures
            log.exception("Retail batch failed for %s", product_id)
            errors.append({"id": product_id, "error": str(getattr(error, "detail", error))})

    pool = candidate_pool(db, limit=pool_size)
    remaining = pending_candidate_ids(db, pool_size=pool_size)
    return {
        "processed": processed,
        "processedCount": len(processed),
        "errors": errors,
        "pendingCount": len(remaining),
        "poolSize": len(pool),
        "analyzedCount": max(0, len(pool) - len(remaining)),
    }


def get_or_analyze(
    db: Session,
    product_id: str,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    product = db.scalar(select(Product).where(Product.mercos_id == product_id))
    if not product:
        raise HTTPException(404, "Produto nao encontrado")
    return analyze_product(db, product_id, refresh=refresh, allow_heuristic=False)
