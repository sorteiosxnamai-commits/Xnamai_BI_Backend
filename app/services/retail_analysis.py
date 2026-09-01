"""Retail product AI analysis with public web search and batch processing."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from app.services.retail_listings import (
    collapse_platform_market_price,
    collect_listings,
    merge_platform_payloads,
    sources_from_market_prices,
)
from app.services.retail_pack import detect_pack_units
from app.cache.redis_cache import invalidate_retail_lists, cache_set, RETAIL_ANALYSIS_PREFIX

log = logging.getLogger(__name__)

# Cap total simultaneous OpenAI calls across product workers + platform searches.
_openai_lock = threading.Lock()
_openai_semaphore: threading.Semaphore | None = None


def _openai_gate() -> threading.Semaphore:
    global _openai_semaphore
    with _openai_lock:
        if _openai_semaphore is None:
            limit = int(getattr(settings(), "retail_openai_max_inflight", 4) or 4)
            _openai_semaphore = threading.Semaphore(max(1, min(8, limit)))
        return _openai_semaphore

PLATFORM_KEYS = (
    "mercado_livre",
    "shopee",
    "tiktok",
    "nuvemshop",
    "site_proprio",
)

PLATFORM_SEARCH_HINTS = {
    "mercado_livre": {
        "label": "Mercado Livre",
        "sites": "mercadolivre.com.br OR mercadolivre.com",
        "queryHint": "site:mercadolivre.com.br",
    },
    "shopee": {
        "label": "Shopee",
        "sites": "shopee.com.br",
        "queryHint": "site:shopee.com.br",
    },
    "tiktok": {
        "label": "TikTok Shop",
        "sites": "shop.tiktok.com OR tiktok.com",
        "queryHint": "TikTok Shop Brasil",
    },
    "nuvemshop": {
        "label": "Nuvemshop",
        "sites": "nuvemshop.com.br OR lojaintegrada.com.br",
        "queryHint": "Nuvemshop Brasil",
    },
    "site_proprio": {
        "label": "Site proprio / loja oficial",
        "sites": "loja oficial OR site da marca",
        "queryHint": "loja oficial Brasil",
    },
}

PLATFORM_SEARCH_INSTRUCTIONS = (
    "Voce e um pesquisador de precos de varejo B2C no Brasil. "
    "Use a ferramenta web_search VARIAS VEZES (no minimo 4 queries diferentes) "
    "focando SOMENTE na plataforma pedida. "
    "Meta: encontrar de 3 a 5 anuncios/vendedores DIFERENTES do MESMO produto "
    "(mesmo modelo/marca/quantidade). "
    "Nao pare no primeiro resultado. Compare precos entre lojas. "
    "NUNCA copie o preco de tabela Mercos como preco de venda. "
    "Nao invente precos, vendedores ou URLs. "
    "PROIBIDO usar nomes ficticios (Loja A, Loja B, Loja C, Seller A, etc). "
    "Use o nome REAL do vendedor/loja do anuncio. "
    "URL deve ser o link HTTPS completo e real do anuncio na plataforma pedida "
    "(ex: mercadolivre.com.br, shopee.com.br). Nunca use https://... nem placeholders. "
    "Se nao tiver URL real, omita o campo url. "
    "Se achar menos de 3, continue buscando com queries alternativas "
    "(modelo, marca, sinonimos, 'comprar', 'preco', 'oferta'). "
    "Responda SOMENTE JSON valido (sem markdown): "
    '{"platform":"shopee","listings":['
    '{"price":260.1,"units":1,"freight":18,"seller":"<nome real do vendedor>",'
    '"url":"https://shopee.com.br/...","source":"Shopee","packMatch":true,"title":"<titulo real>"},'
    '{"price":249.9,"units":1,"freight":15,"seller":"<outro vendedor real>",'
    '"url":"https://shopee.com.br/...","source":"Shopee","packMatch":true,"title":"<titulo real>"}'
    '],"notes":"resumo da busca","searchesTried":5}'
)

MIN_SELLERS_TARGET = 3

SYNTHESIS_INSTRUCTIONS = (
    "Voce e um analista de varejo B2C no Brasil (XNamai). "
    "Com base nos precos reais ja coletados por plataforma (multiplos vendedores), "
    "escolha o melhor canal para VENDER no preco COMPETITIVO daquele canal "
    "(mediana dos anuncios reais), nao no preco mais alto de outro canal. "
    "Nao invente novos precos. "
    "Nao indique um marketplace so porque o preco de la e o mais baixo se a margem "
    "liquida nesse preco for negativa e existir outro canal competitivo com margem melhor. "
    "Ignore precos de site proprio/Nuvemshop muito acima da mediana dos marketplaces "
    "(nao sao referencia de venda competitiva). "
    "Responda SOMENTE com JSON valido (sem markdown), neste formato: "
    '{"apelo":"alto|medio|baixo","apeloJustificativa":"texto",'
    '"potencialScore":75,'
    '"melhorPlataforma":"mercado_livre|shopee|tiktok|nuvemshop|site_proprio",'
    '"porquePlataforma":"resumo curto",'
    '"porqueCanalDetalhe":"paragrafo explicando por que este canal vs alternativas",'
    '"comparativoCanais":['
    '{"plataforma":"mercado_livre","pros":["..."],"contras":["..."],"veredito":"melhor|boa|evitar"},'
    '{"plataforma":"shopee","pros":["..."],"contras":["..."],"veredito":"boa"},'
    '{"plataforma":"site_proprio","pros":["..."],"contras":["..."],"veredito":"boa"}'
    "],"
    '"melhorEnvio":"mercado_envios|shopee_entrega|melhor_envio|correios_pac|correios_sedex",'
    '"envioJustificativa":"texto",'
    '"motivoEscolha":"frase curta do porque indicar no Top 100 varejo",'
    '"razoes":["razao 1","razao 2"],'
    '"risco":"baixo|medio|alto - detalhe",'
    '"productUnits":1,'
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


def _search_tokens(product: dict[str, Any]) -> list[str]:
    name = str(product.get("name") or "")
    code = str(product.get("code") or "")
    parts = [part for part in re.split(r"[\s\-_/|,]+", f"{name} {code}") if len(part) >= 3]
    # Prefer distinctive tokens (model codes, brand-like words).
    ranked = sorted(set(parts), key=lambda item: (-len(item), item.lower()))
    return ranked[:10]


def _build_platform_prompt(product: dict[str, Any], platform_key: str, *, deepen: bool = False) -> str:
    units = int(product.get("packUnits") or detect_pack_units(product.get("name"), product.get("code")))
    hint = PLATFORM_SEARCH_HINTS.get(platform_key) or {"label": platform_key, "queryHint": platform_key}
    name = product.get("name") or ""
    code = product.get("code") or ""
    tokens = _search_tokens(product)
    core = " ".join(tokens[:5])
    brandish = " ".join(tokens[:3])
    queries = [
        f'"{name}" {hint.get("queryHint")}',
        f"{core} {hint.get('queryHint')}",
        f"{brandish} preco {hint['label']} Brasil",
        f"{core} comprar {hint['label']}",
        f"{code} {hint['label']} oferta" if code else f"{brandish} oferta {hint['label']}",
        f"{core} site:{hint.get('sites', '')}",
    ]
    deepen_block = ""
    if deepen:
        deepen_block = (
            "\nMODO APROFUNDAMENTO: a primeira rodada trouxe poucos vendedores. "
            "OBRIGATORIO executar pelo menos 5 web_search novas com queries alternativas, "
            "sinonimos e termos como 'original', 'novo', 'barato', 'frete gratis'. "
            "Priorize vendedores/URLs ainda nao listados.\n"
        )
    return (
        f"Plataforma alvo: {hint['label']} ({platform_key}).\n"
        f"{deepen_block}"
        f"Execute TODAS estas buscas (e outras se precisar):\n- "
        + "\n- ".join(queries)
        + "\n"
        f"Produto: {name}\n"
        f"Codigo interno: {code}\n"
        f"Quantidade do SKU: {units} unidade(s). Aceite so mesma quantidade "
        f"(ou informe units e packMatch=false).\n"
        f"Custo Mercos (NAO e preco de venda): {product.get('listPrice')}.\n"
        f"Meta minima: {MIN_SELLERS_TARGET} vendedores diferentes com price, seller e url. "
        f"Ideal: 4-5. Nao invente dados."
    )


def _build_synthesis_prompt(product: dict[str, Any], market_prices: dict[str, Any]) -> str:
    units = int(product.get("packUnits") or detect_pack_units(product.get("name"), product.get("code")))
    seller_stats = {
        key: {
            "sellersCompared": (value or {}).get("sellersCompared"),
            "price": (value or {}).get("price"),
            "listings": len((value or {}).get("listings") or []),
        }
        for key, value in (market_prices or {}).items()
    }
    context = {
        "id": product.get("id"),
        "name": product.get("name"),
        "code": product.get("code"),
        "listPriceAsCost": product.get("listPrice"),
        "packUnits": units,
        "stock": product.get("stock"),
        "mercosRevenue90d": product.get("mercosRevenue"),
        "mercosQuantity90d": product.get("mercosQuantity"),
        "marketPrices": market_prices,
        "sellerCoverage": seller_stats,
    }
    return (
        "Com os precos reais abaixo (pesquisados em paralelo por plataforma, "
        "com multiplos vendedores quando disponiveis), monte a recomendacao B2C. "
        "Inclua em sources TODAS as URLs de anuncios encontrados.\n"
        f"{json.dumps(context, ensure_ascii=False, default=str)}"
    )


def _call_openai(
    prompt: str,
    *,
    instructions: str,
    use_web_search: bool = True,
    timeout_seconds: float = 90.0,
    retries: int = 2,
) -> dict:
    cfg = settings()
    if not cfg.openai_api_key:
        raise HTTPException(503, "OPENAI_API_KEY nao configurada")
    body: dict[str, Any] = {
        "model": cfg.openai_model,
        "instructions": instructions,
        "input": prompt,
    }
    if use_web_search:
        body["tools"] = [
            {
                "type": "web_search",
                "user_location": {"type": "approximate", "country": "BR"},
            }
        ]
        body["include"] = ["web_search_call.action.sources"]

    last_error: Exception | None = None
    attempts = max(1, int(retries) + 1)
    for attempt in range(1, attempts + 1):
        try:
            with _openai_gate():
                with httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=15.0)) as client:
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
                # Retry transient OpenAI overload/rate limits.
                if response.status_code in {408, 429, 500, 502, 503, 504} and attempt < attempts:
                    time.sleep(min(8.0, attempt * 1.5))
                    continue
                raise HTTPException(502, "Falha ao gerar analise varejo com IA")
            payload = response.json()
            text = _extract_output_text(payload)
            if not text:
                raise HTTPException(502, "IA nao retornou analise")
            analysis = _parse_analysis(text)
            if use_web_search and not analysis.get("sources"):
                analysis["sources"] = _extract_sources(payload)
            elif use_web_search:
                # Keep tool sources too.
                merged = list(analysis.get("sources") or [])
                for source in _extract_sources(payload):
                    if source not in merged:
                        merged.append(source)
                analysis["sources"] = merged
            return analysis
        except HTTPException:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as error:
            last_error = error
            log.warning(
                "OpenAI retail timeout/transport attempt %s/%s: %s",
                attempt,
                attempts,
                error,
            )
            if attempt < attempts:
                time.sleep(min(8.0, attempt * 1.5))
                continue
        except Exception as error:  # noqa: BLE001
            last_error = error
            log.exception("OpenAI retail unexpected error")
            break

    detail = "Timeout OpenAI na analise varejo"
    if last_error:
        detail = f"{detail}: {last_error}"
    raise HTTPException(504, detail)


def _search_platform_listings(product: dict[str, Any], platform_key: str) -> dict[str, Any]:
    list_price = float(product.get("listPrice") or 0)
    rounds: list[dict[str, Any]] = []

    def _one(deepen: bool) -> dict[str, Any]:
        try:
            raw = _call_openai(
                _build_platform_prompt(product, platform_key, deepen=deepen),
                instructions=PLATFORM_SEARCH_INSTRUCTIONS,
                use_web_search=True,
                timeout_seconds=110.0,
                retries=1,
            )
        except Exception as error:  # noqa: BLE001 - one platform must not kill the batch
            log.warning(
                "Platform search failed for %s/%s deepen=%s: %s",
                product.get("id"),
                platform_key,
                deepen,
                error,
            )
            return {"platform": platform_key, "listings": [], "notes": f"busca falhou: {error}"}
        if not isinstance(raw, dict):
            return {"platform": platform_key, "listings": [], "notes": "resposta invalida"}
        raw["platform"] = platform_key
        return raw

    first = _one(deepen=False)
    rounds.append(first)
    first_count = len(collect_listings(first, list_price=list_price))
    if first_count < MIN_SELLERS_TARGET:
        rounds.append(_one(deepen=True))

    merged = merge_platform_payloads(*rounds, list_price=list_price)
    # Keep raw-ish shape for gather collapse (already collapsed).
    return {
        "platform": platform_key,
        "listings": merged.get("listings") or [],
        "notes": merged.get("notes"),
        "searchesTried": merged.get("searchesTried"),
        "price": merged.get("price"),
        "seller": merged.get("seller"),
        "url": merged.get("url"),
        "freight": merged.get("freight"),
        "units": merged.get("units"),
        "packMatch": merged.get("packMatch"),
        "source": merged.get("source"),
        "sellersCompared": merged.get("sellersCompared"),
    }


def gather_market_prices_parallel(product: dict[str, Any]) -> dict[str, Any]:
    """Search each marketplace in parallel and keep multiple seller listings."""
    results: dict[str, Any] = {}
    # Modest fan-out; deepen pass runs only when coverage is thin.
    workers = max(1, min(3, len(PLATFORM_KEYS)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="retail-mkt") as pool:
        futures = {
            pool.submit(_search_platform_listings, product, key): key for key in PLATFORM_KEYS
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as error:  # noqa: BLE001
                log.exception("Platform future failed for %s", key)
                results[key] = {"platform": key, "listings": [], "notes": str(error)}
    list_price = float(product.get("listPrice") or 0)
    return {
        key: collapse_platform_market_price(
            results.get(key),
            list_price=list_price,
            platform=key,
        )
        for key in PLATFORM_KEYS
    }


def _fallback_recommendation(market_prices: dict[str, Any]) -> dict:
    priced = [
        key for key, value in market_prices.items() if isinstance(value, dict) and value.get("price")
    ]
    best = priced[0] if priced else "site_proprio"
    return {
        "apelo": "medio",
        "apeloJustificativa": "Sintese automatica apos busca de precos",
        "potencialScore": 60,
        "melhorPlataforma": best,
        "porquePlataforma": "Melhor canal entre os que retornaram anuncio real",
        "porqueCanalDetalhe": "Recomendacao derivada dos precos coletados em paralelo.",
        "comparativoCanais": [],
        "melhorEnvio": "melhor_envio",
        "envioJustificativa": "Padrao",
        "motivoEscolha": "Precos reais coletados por plataforma",
        "razoes": ["Busca paralela multi-vendedor"],
        "risco": "medio",
        "confidence": "media",
        "sources": [],
    }


def _synthesize_recommendation(product: dict[str, Any], market_prices: dict[str, Any]) -> dict:
    try:
        return _call_openai(
            _build_synthesis_prompt(product, market_prices),
            instructions=SYNTHESIS_INSTRUCTIONS,
            use_web_search=False,
            timeout_seconds=75.0,
            retries=2,
        )
    except Exception as error:  # noqa: BLE001 - keep market prices even if synthesis times out
        log.warning("Retail synthesis fallback for %s: %s", product.get("id"), error)
        return _fallback_recommendation(market_prices)


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


def _normalize_market_prices(raw: Any, *, list_price: float | None = None, product_units: int = 1) -> dict[str, Any]:
    del product_units  # pack matching applied later in evaluate_channels
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in PLATFORM_KEYS:
        if key in raw:
            out[key] = collapse_platform_market_price(
                raw.get(key),
                list_price=list_price,
                platform=key,
            )
    for key, value in raw.items():
        if key in out or not isinstance(value, dict):
            continue
        out[str(key)] = collapse_platform_market_price(
            value,
            list_price=list_price,
            platform=str(key),
        )
    return out


def _build_scores(product: dict[str, Any], ai_payload: dict, economics: dict | None = None) -> dict:
    units = int(product.get("packUnits") or detect_pack_units(product.get("name"), product.get("code")))
    market_prices = _normalize_market_prices(
        ai_payload.get("marketPrices"),
        list_price=float(product.get("listPrice") or 0),
        product_units=units,
    )
    channels = evaluate_channels(
        market_prices=market_prices,
        list_price=float(product.get("listPrice") or 0),
        economics=merge_economics(economics),
        product_units=units,
    )
    priced = [row for row in channels if row.get("hasPrice")]
    best = priced[0] if priced else None
    # Platform/margin come from compose_product_score (competitive channel), not raw AI.
    temp = RetailProductAnalysis(
        product_mercos_id=product["id"],
        ai_payload=ai_payload,
        market_prices=market_prices,
        scores={
            "potencialScore": ai_payload.get("potencialScore"),
            "bestPlatform": ai_payload.get("melhorPlataforma")
            or (best or {}).get("platform")
            or "site_proprio",
            "bestShipping": ai_payload.get("melhorEnvio")
            or (best or {}).get("shippingKey")
            or "melhor_envio",
            "reasonShort": ai_payload.get("motivoEscolha"),
            "reasonDetail": ai_payload.get("razoes") or [],
        },
        generated_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    composed = compose_product_score(product, temp, economics=economics)
    platform = composed.get("melhorPlataforma") or (best or {}).get("platform") or "site_proprio"
    shipping = composed.get("melhorEnvio") or (best or {}).get("shippingKey") or "melhor_envio"
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
        "packUnits": detect_pack_units(detail.get("name"), detail.get("code")),
    }

    used_heuristic = False
    try:
        market_prices = gather_market_prices_parallel(product)
        ai_payload = _synthesize_recommendation(product, market_prices)
        ai_payload["marketPrices"] = market_prices
    except HTTPException as error:
        if not allow_heuristic or error.status_code not in {502, 503}:
            raise
        log.warning("Retail AI fallback to heuristic for %s: %s", product_id, error.detail)
        ai_payload = _heuristic_analysis(product, economics)
        used_heuristic = True
        market_prices = _normalize_market_prices(
            ai_payload.get("marketPrices"),
            list_price=float(product.get("listPrice") or 0),
            product_units=int(product.get("packUnits") or 1),
        )

    units = int(product.get("packUnits") or detect_pack_units(product.get("name"), product.get("code")))
    market_prices = _normalize_market_prices(
        ai_payload.get("marketPrices") or market_prices,
        list_price=float(product.get("listPrice") or 0),
        product_units=units,
    )
    ai_payload["marketPrices"] = market_prices
    listing_sources = sources_from_market_prices(market_prices)
    existing_sources = ai_payload.get("sources") if isinstance(ai_payload.get("sources"), list) else []
    merged_sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for source in [*listing_sources, *existing_sources]:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        merged_sources.append(
            {"title": str(source.get("title") or url), "url": url}
        )
    ai_payload["sources"] = merged_sources
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
    try:
        cache_set(f"{RETAIL_ANALYSIS_PREFIX}{product_id}", refreshed, ttl_seconds=3600)
        invalidate_retail_lists()
    except Exception:
        pass
    return {**refreshed, "cached": False, "heuristic": used_heuristic}


def pending_candidate_ids(
    db: Session,
    *,
    pool_size: int | None = CANDIDATE_POOL_SIZE,
    refresh_stale: bool = False,
    limit: int | None = None,
) -> list[str]:
    """Return pending product ids, optionally capped to avoid huge payloads/timeouts."""
    from sqlalchemy import Float, cast, func

    from app.services.retail import _product_sales_subquery

    # Stale refresh still needs row-level freshness checks.
    if refresh_stale:
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
            if row is None or row.generated_at is None or not _is_fresh(row):
                pending.append(item["id"])
            if limit is not None and len(pending) >= int(limit):
                break
        return pending

    sales = _product_sales_subquery(db, days=90)
    analyzed_ids = select(RetailProductAnalysis.product_mercos_id).where(
        RetailProductAnalysis.generated_at.is_not(None)
    )
    stmt = (
        select(Product.mercos_id)
        .outerjoin(sales, sales.c.product_id == Product.mercos_id)
        .where(
            Product.active.is_(True),
            Product.mercos_id.notin_(analyzed_ids),
        )
        .order_by(
            cast(func.coalesce(sales.c.revenue, 0), Float).desc(),
            cast(func.coalesce(sales.c.quantity, 0), Float).desc(),
            Product.name.asc(),
        )
    )
    if pool_size is not None:
        # Preserve legacy semantics: only consider top-N of the active catalog.
        ranked = (
            select(Product.mercos_id)
            .outerjoin(sales, sales.c.product_id == Product.mercos_id)
            .where(Product.active.is_(True))
            .order_by(
                cast(func.coalesce(sales.c.revenue, 0), Float).desc(),
                cast(func.coalesce(sales.c.quantity, 0), Float).desc(),
                Product.name.asc(),
            )
            .limit(max(1, int(pool_size)))
            .subquery()
        )
        stmt = (
            select(Product.mercos_id)
            .join(ranked, ranked.c.mercos_id == Product.mercos_id)
            .outerjoin(sales, sales.c.product_id == Product.mercos_id)
            .where(Product.mercos_id.notin_(analyzed_ids))
            .order_by(
                cast(func.coalesce(sales.c.revenue, 0), Float).desc(),
                cast(func.coalesce(sales.c.quantity, 0), Float).desc(),
                Product.name.asc(),
            )
        )
    if limit is not None:
        stmt = stmt.limit(max(1, int(limit)))
    return list(db.scalars(stmt).all())


def analyze_batch(
    db: Session,
    *,
    limit: int = 10,
    pool_size: int | None = CANDIDATE_POOL_SIZE,
    refresh: bool = False,
    economics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app.services.retail import catalog_progress

    limit = max(1, min(20, int(limit)))
    ids = pending_candidate_ids(db, pool_size=pool_size, refresh_stale=refresh, limit=limit)
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

    progress = catalog_progress(db)
    return {
        "processed": processed,
        "processedCount": len(processed),
        "errors": errors,
        "pendingCount": progress["pendingCount"],
        "poolSize": progress["poolSize"],
        "analyzedCount": progress["analyzedCount"],
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
