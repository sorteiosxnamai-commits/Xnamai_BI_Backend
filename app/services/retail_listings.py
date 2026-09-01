"""Helpers for multi-seller marketplace listing selection."""

from __future__ import annotations

import re
from statistics import median
from typing import Any
from urllib.parse import urlparse

PLACEHOLDER_SELLER_RE = re.compile(
    r"^(loja\s*[abc]|loja\s*\d+|seller\s*[abc]|exemplo|example|store\s*[abc])$",
    re.IGNORECASE,
)

PLATFORM_HOST_HINTS: dict[str, tuple[str, ...]] = {
    "mercado_livre": ("mercadolivre.", "mercadolibre.", "mlb."),
    "shopee": ("shopee.",),
    "tiktok": ("tiktok.", "shop.tiktok."),
    "nuvemshop": ("nuvemshop.", "lojavirtualnuvem."),
}


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


def is_placeholder_seller(seller: Any) -> bool:
    text = str(seller or "").strip()
    if not text:
        return False
    return bool(PLACEHOLDER_SELLER_RE.match(text))


def is_usable_listing_url(url: Any, *, platform: str | None = None) -> bool:
    """Reject invented/placeholder URLs that open nowhere or show a padlock."""
    text = str(url or "").strip()
    if not text:
        return False
    if "..." in text or text.endswith("…"):
        return False
    lower = text.lower()
    if lower in {"https://...", "http://...", "https://", "http://"}:
        return False
    try:
        parsed = urlparse(text)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.netloc or "").lower()
    if not host or host in {"example.com", "example.org", "localhost"}:
        return False
    hints = PLATFORM_HOST_HINTS.get(str(platform or ""), ())
    if hints and not any(token in host for token in hints):
        # Keep URL only when host matches the marketplace being searched.
        return False
    return True


def normalize_listing_entry(
    raw: Any,
    *,
    list_price: float | None = None,
    platform: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    price = _safe_float(raw.get("price"))
    if price is None:
        return None
    cost = _safe_float(list_price)
    if cost is not None and abs(price - cost) < 0.01:
        return None
    seller = raw.get("seller") or raw.get("loja") or raw.get("store")
    if is_placeholder_seller(seller):
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
    raw_url = raw.get("url")
    url = str(raw_url).strip() if raw_url else None
    if url and not is_usable_listing_url(url, platform=None):
        url = None
    elif url and platform in PLATFORM_HOST_HINTS and not is_usable_listing_url(url, platform=platform):
        # Wrong host for this marketplace — hide the link, keep the price if seller looks real.
        url = None
    if not seller and not url and platform in PLATFORM_HOST_HINTS:
        return None
    return {
        "price": price,
        "units": units,
        "freight": freight,
        "url": url,
        "source": raw.get("source"),
        "seller": seller,
        "shippingKey": raw.get("shippingKey"),
        "packMatch": bool(pack_match) if pack_match is not None else None,
        "title": raw.get("title") or raw.get("name"),
    }


def collect_listings(
    platform_payload: Any,
    *,
    list_price: float | None = None,
    platform: str | None = None,
) -> list[dict[str, Any]]:
    """Extract unique seller listings from a platform payload."""
    if not isinstance(platform_payload, dict):
        return []
    platform_key = platform or platform_payload.get("platform")
    raw_listings = platform_payload.get("listings") or platform_payload.get("sellers") or []
    entries: list[Any] = list(raw_listings) if isinstance(raw_listings, list) else []
    # Backward compatible single listing shape.
    if platform_payload.get("price") is not None or platform_payload.get("seller"):
        entries.insert(0, platform_payload)

    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in entries:
        normalized = normalize_listing_entry(
            item,
            list_price=list_price,
            platform=str(platform_key) if platform_key else None,
        )
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
    platform: str | None = None,
) -> dict[str, Any]:
    platform_key = platform
    if platform_key is None and isinstance(platform_payload, dict):
        platform_key = platform_payload.get("platform")
    listings = collect_listings(
        platform_payload,
        list_price=list_price,
        platform=str(platform_key) if platform_key else None,
    )
    best = pick_representative_listing(listings)
    if not best:
        notes = None
        searches = None
        if isinstance(platform_payload, dict):
            notes = platform_payload.get("notes") or platform_payload.get("searchNotes")
            searches = platform_payload.get("searchesTried")
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
            "searchesTried": searches,
            "notes": notes,
        }
    return {
        **best,
        "listings": listings,
        "sellersCompared": len(listings),
        "searchesTried": (platform_payload or {}).get("searchesTried")
        if isinstance(platform_payload, dict)
        else None,
        "notes": (platform_payload or {}).get("notes") if isinstance(platform_payload, dict) else None,
    }


def merge_platform_payloads(
    *payloads: Any,
    list_price: float | None = None,
) -> dict[str, Any]:
    """Merge multiple search rounds for the same platform."""
    combined_listings: list[dict[str, Any]] = []
    notes: list[str] = []
    searches = 0
    platform = None
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        platform = platform or payload.get("platform")
        combined_listings.extend(
            collect_listings(payload, list_price=list_price, platform=str(platform) if platform else None)
        )
        note = payload.get("notes") or payload.get("searchNotes")
        if note:
            notes.append(str(note))
        try:
            searches += int(payload.get("searchesTried") or 0)
        except (TypeError, ValueError):
            pass
    return collapse_platform_market_price(
        {
            "platform": platform,
            "listings": combined_listings,
            "notes": " | ".join(notes) if notes else None,
            "searchesTried": searches or None,
        },
        list_price=list_price,
        platform=str(platform) if platform else None,
    )


def sources_from_market_prices(market_prices: dict[str, Any] | None) -> list[dict[str, str]]:
    """Build public source list from every listing URL found."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for platform, payload in (market_prices or {}).items():
        if not isinstance(payload, dict):
            continue
        listings = payload.get("listings") if isinstance(payload.get("listings"), list) else []
        rows = listings or ([payload] if payload.get("url") else [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "").strip()
            if not url or url in seen:
                continue
            if not is_usable_listing_url(url, platform=str(platform)):
                continue
            seen.add(url)
            seller = row.get("seller") or platform
            title = row.get("title") or f"{platform}: {seller}"
            out.append({"title": str(title), "url": url})
    return out
