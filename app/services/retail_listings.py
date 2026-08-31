"""Helpers for multi-seller marketplace listing selection."""

from __future__ import annotations

from statistics import median
from typing import Any


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def normalize_listing_entry(
    raw: Any,
    *,
    list_price: float | None = None,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    price = _safe_float(raw.get("price"))
    if price is None:
        return None
    cost = _safe_float(list_price)
    if cost is not None and abs(price - cost) < 0.01:
        return None
    freight = _safe_float(raw.get("freight"))
    units = raw.get("units") or raw.get("packUnits") or raw.get("quantity")
    try:
        units = int(units) if units is not None else None
    except (TypeError, ValueError):
        units = None
    pack_match = raw.get("packMatch")
    if pack_match is None and units is not None:
        pack_match = True
    return {
        "price": price,
        "units": units,
        "freight": freight,
        "url": raw.get("url"),
        "source": raw.get("source"),
        "seller": raw.get("seller") or raw.get("loja") or raw.get("store"),
        "shippingKey": raw.get("shippingKey"),
        "packMatch": bool(pack_match) if pack_match is not None else None,
        "title": raw.get("title") or raw.get("name"),
    }


def collect_listings(
    platform_payload: Any,
    *,
    list_price: float | None = None,
) -> list[dict[str, Any]]:
    """Extract unique seller listings from a platform payload."""
    if not isinstance(platform_payload, dict):
        return []
    raw_listings = platform_payload.get("listings") or platform_payload.get("sellers") or []
    entries: list[Any] = list(raw_listings) if isinstance(raw_listings, list) else []
    # Backward compatible single listing shape.
    if platform_payload.get("price") is not None or platform_payload.get("seller"):
        entries.insert(0, platform_payload)

    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in entries:
        normalized = normalize_listing_entry(item, list_price=list_price)
        if not normalized:
            continue
        key = (
            str(normalized.get("seller") or "").strip().lower(),
            round(float(normalized["price"]), 2),
            str(normalized.get("url") or "").strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
    out.sort(key=lambda row: float(row["price"]))
    return out


def pick_representative_listing(listings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer pack-matched listings; use median price among them to avoid outliers."""
    if not listings:
        return None
    preferred = [row for row in listings if row.get("packMatch") is not False]
    pool = preferred or listings
    prices = [float(row["price"]) for row in pool]
    target = float(median(prices))
    return min(pool, key=lambda row: abs(float(row["price"]) - target))


def collapse_platform_market_price(
    platform_payload: Any,
    *,
    list_price: float | None = None,
) -> dict[str, Any]:
    listings = collect_listings(platform_payload, list_price=list_price)
    best = pick_representative_listing(listings)
    if not best:
        notes = None
        if isinstance(platform_payload, dict):
            notes = platform_payload.get("notes") or platform_payload.get("searchNotes")
        return {
            "price": None,
            "units": None,
            "freight": None,
            "url": None,
            "source": None,
            "seller": None,
            "shippingKey": None,
            "packMatch": None,
            "listings": [],
            "sellersCompared": 0,
            "notes": notes,
        }
    return {
        **best,
        "listings": listings,
        "sellersCompared": len(listings),
        "notes": (platform_payload or {}).get("notes") if isinstance(platform_payload, dict) else None,
    }
