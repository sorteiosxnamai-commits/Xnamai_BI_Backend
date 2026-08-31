"""Retail B2C economics: cost proxy, marketplace fees, freight and packaging."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_CUSTO_PCT = 40.0

DEFAULT_PLATFORM_FEES = {
    "mercado_livre": {"label": "Mercado Livre", "feePct": 16.0},
    "shopee": {"label": "Shopee", "feePct": 14.0},
    "tiktok": {"label": "TikTok Shop", "feePct": 10.0},
    "nuvemshop": {"label": "Nuvemshop", "feePct": 4.0},
    "site_proprio": {"label": "Site proprio", "feePct": 3.5},
}

DEFAULT_SHIPPING = {
    "mercado_envios": {"label": "Mercado Envios", "avgCost": 22.0},
    "shopee_entrega": {"label": "Shopee Entrega", "avgCost": 18.0},
    "melhor_envio": {"label": "Melhor Envio", "avgCost": 20.0},
    "correios_pac": {"label": "Correios PAC", "avgCost": 24.0},
    "correios_sedex": {"label": "Correios SEDEX", "avgCost": 32.0},
}

DEFAULT_PACKAGING_COST = 4.0

PLATFORM_SHIPPING_HINT = {
    "mercado_livre": "mercado_envios",
    "shopee": "shopee_entrega",
    "tiktok": "melhor_envio",
    "nuvemshop": "melhor_envio",
    "site_proprio": "melhor_envio",
}


def default_economics() -> dict[str, Any]:
    return {
        "custoPct": DEFAULT_CUSTO_PCT,
        "packagingCost": DEFAULT_PACKAGING_COST,
        "platforms": deepcopy(DEFAULT_PLATFORM_FEES),
        "shipping": deepcopy(DEFAULT_SHIPPING),
        "notes": (
            "Custo estimado = preco de tabela x (1 - custoPct/100). "
            "Taxas e fretes sao defaults BR editaveis; precos de mercado vem da busca publica/IA."
        ),
    }


def merge_economics(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    base = default_economics()
    if not overrides:
        return base
    if overrides.get("custoPct") is not None:
        try:
            base["custoPct"] = max(0.0, min(90.0, float(overrides["custoPct"])))
        except (TypeError, ValueError):
            pass
    if overrides.get("packagingCost") is not None:
        try:
            base["packagingCost"] = max(0.0, float(overrides["packagingCost"]))
        except (TypeError, ValueError):
            pass
    platforms = overrides.get("platforms")
    if isinstance(platforms, dict):
        for key, value in platforms.items():
            if key not in base["platforms"] or not isinstance(value, dict):
                continue
            if value.get("feePct") is not None:
                try:
                    base["platforms"][key]["feePct"] = max(0.0, min(50.0, float(value["feePct"])))
                except (TypeError, ValueError):
                    pass
    shipping = overrides.get("shipping")
    if isinstance(shipping, dict):
        for key, value in shipping.items():
            if key not in base["shipping"] or not isinstance(value, dict):
                continue
            if value.get("avgCost") is not None:
                try:
                    base["shipping"][key]["avgCost"] = max(0.0, float(value["avgCost"]))
                except (TypeError, ValueError):
                    pass
    return base


def estimated_cost(list_price: float, custo_pct: float) -> float:
    price = max(0.0, float(list_price or 0))
    pct = max(0.0, min(90.0, float(custo_pct or 0)))
    return round(price * (1.0 - pct / 100.0), 2)


def channel_margin(
    *,
    retail_price: float,
    cost: float,
    fee_pct: float,
    freight: float,
    packaging: float,
) -> dict[str, float]:
    price = max(0.0, float(retail_price or 0))
    fee = round(price * max(0.0, float(fee_pct or 0)) / 100.0, 2)
    freight_v = max(0.0, float(freight or 0))
    pack = max(0.0, float(packaging or 0))
    cost_v = max(0.0, float(cost or 0))
    net = round(price - cost_v - fee - freight_v - pack, 2)
    margin_pct = round((net / price) * 100.0, 2) if price > 0 else 0.0
    return {
        "retailPrice": price,
        "cost": cost_v,
        "fee": fee,
        "freight": freight_v,
        "packaging": pack,
        "netMargin": net,
        "marginPct": margin_pct,
    }


def evaluate_channels(
    *,
    market_prices: dict[str, Any] | None,
    list_price: float,
    economics: dict[str, Any],
) -> list[dict[str, Any]]:
    eco = merge_economics(economics)
    cost = estimated_cost(list_price, eco["custoPct"])
    packaging = float(eco["packagingCost"])
    prices = market_prices or {}
    rows: list[dict[str, Any]] = []
    for key, platform in eco["platforms"].items():
        raw = prices.get(key) if isinstance(prices.get(key), dict) else {}
        retail_price = raw.get("price")
        try:
            retail_price = float(retail_price) if retail_price is not None else float(list_price or 0)
        except (TypeError, ValueError):
            retail_price = float(list_price or 0)
        shipping_key = raw.get("shippingKey") or PLATFORM_SHIPPING_HINT.get(key) or "melhor_envio"
        shipping = eco["shipping"].get(shipping_key) or eco["shipping"]["melhor_envio"]
        freight = raw.get("freight")
        try:
            freight = float(freight) if freight is not None else float(shipping["avgCost"])
        except (TypeError, ValueError):
            freight = float(shipping["avgCost"])
        margin = channel_margin(
            retail_price=retail_price,
            cost=cost,
            fee_pct=float(platform["feePct"]),
            freight=freight,
            packaging=packaging,
        )
        rows.append(
            {
                "platform": key,
                "label": platform["label"],
                "feePct": float(platform["feePct"]),
                "shippingKey": shipping_key,
                "shippingLabel": shipping["label"],
                "source": raw.get("source"),
                "url": raw.get("url"),
                **margin,
            }
        )
    rows.sort(key=lambda row: (-row["marginPct"], -row["netMargin"]))
    return rows
