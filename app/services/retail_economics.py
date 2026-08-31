"""Retail B2C economics: table price as cost, marketplace fees, freight and packaging."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

DEFAULT_PACKAGING_COST = 4.0

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

PLATFORM_SHIPPING_HINT = {
    "mercado_livre": "mercado_envios",
    "shopee": "shopee_entrega",
    "tiktok": "melhor_envio",
    "nuvemshop": "melhor_envio",
    "site_proprio": "melhor_envio",
}


def default_economics() -> dict[str, Any]:
    return {
        "costMode": "list_price",
        "packagingCost": DEFAULT_PACKAGING_COST,
        "platforms": deepcopy(DEFAULT_PLATFORM_FEES),
        "shipping": deepcopy(DEFAULT_SHIPPING),
        "notes": (
            "Custo = preco de tabela Mercos (list_price). "
            "Preco de venda por plataforma = anuncio publico do mesmo produto (busca IA). "
            "Nao usar preco de tabela como preco de venda."
        ),
    }


def merge_economics(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    base = default_economics()
    if not overrides:
        return base
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


def estimated_cost(list_price: float, *_args, **_kwargs) -> float:
    """Cost is the Mercos list/table price."""
    try:
        return round(max(0.0, float(list_price or 0)), 2)
    except (TypeError, ValueError):
        return 0.0


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


def _parse_price(value: Any) -> float | None:
    if value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return price


def evaluate_channels(
    *,
    market_prices: dict[str, Any] | None,
    list_price: float,
    economics: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build channel rows. Retail price only from real listings - never list_price."""
    eco = merge_economics(economics)
    cost = estimated_cost(list_price)
    packaging = float(eco["packagingCost"])
    prices = market_prices or {}
    rows: list[dict[str, Any]] = []
    for key, platform in eco["platforms"].items():
        raw = prices.get(key) if isinstance(prices.get(key), dict) else {}
        retail_price = _parse_price(raw.get("price"))
        # Reject listing that is just a copy of Mercos table price (not a retail sale).
        if retail_price is not None and cost > 0 and abs(retail_price - cost) < 0.01:
            retail_price = None
        shipping_key = raw.get("shippingKey") or PLATFORM_SHIPPING_HINT.get(key) or "melhor_envio"
        shipping = eco["shipping"].get(shipping_key) or eco["shipping"]["melhor_envio"]
        freight = raw.get("freight")
        try:
            freight = float(freight) if freight is not None else float(shipping["avgCost"])
        except (TypeError, ValueError):
            freight = float(shipping["avgCost"])

        if retail_price is None:
            rows.append(
                {
                    "platform": key,
                    "label": platform["label"],
                    "feePct": float(platform["feePct"]),
                    "shippingKey": shipping_key,
                    "shippingLabel": shipping["label"],
                    "source": raw.get("source"),
                    "url": raw.get("url"),
                    "seller": raw.get("seller"),
                    "hasPrice": False,
                    "retailPrice": None,
                    "cost": cost,
                    "fee": None,
                    "freight": freight,
                    "packaging": packaging,
                    "netMargin": None,
                    "marginPct": None,
                }
            )
            continue

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
                "seller": raw.get("seller"),
                "hasPrice": True,
                **margin,
            }
        )

    rows.sort(
        key=lambda row: (
            1 if row.get("hasPrice") else 0,
            float(row["marginPct"]) if row.get("marginPct") is not None else -9999.0,
            float(row["netMargin"]) if row.get("netMargin") is not None else -9999.0,
        ),
        reverse=True,
    )
    return rows
