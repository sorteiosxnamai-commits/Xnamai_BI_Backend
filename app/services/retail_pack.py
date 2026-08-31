"""Detect pack/unit quantities from product titles for fair marketplace comparisons."""

from __future__ import annotations

import re
from typing import Any

_PACK_PATTERNS = [
    re.compile(r"pacote\s*(?:com|c/?|de)?\s*(\d+)", re.I),
    re.compile(r"kit\s*(?:com|c/?|de)?\s*(\d+)", re.I),
    re.compile(r"pack\s*(?:com|c/?|de|of)?\s*(\d+)", re.I),
    re.compile(r"(\d+)\s*(?:unidades|unidade|und\.?|un\.?|pcs|pe[c?]as|figurinhas)", re.I),
    re.compile(r"c/?\s*(\d+)(?:\s|$)", re.I),
]


def detect_pack_units(name: str | None, code: str | None = None) -> int:
    text = " ".join(part for part in [name or "", code or ""] if part).strip()
    if not text:
        return 1
    for pattern in _PACK_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            units = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if 1 <= units <= 500:
            return units
    return 1


def normalize_listing_price(
    *,
    listing_price: float | None,
    listing_units: int | None,
    product_units: int,
) -> tuple[float | None, dict[str, Any]]:
    """Return comparable price for our SKU pack size, or None if mismatch is unsafe."""
    meta: dict[str, Any] = {
        "productUnits": max(1, int(product_units or 1)),
        "listingUnits": None,
        "packMatch": False,
        "normalized": False,
    }
    if listing_price is None or listing_price <= 0:
        return None, meta
    listing_u = int(listing_units or 0)
    product_u = max(1, int(product_units or 1))
    if listing_u <= 0:
        # Unknown listing size: accept only when our product is single-unit.
        if product_u == 1:
            meta["listingUnits"] = 1
            meta["packMatch"] = True
            return float(listing_price), meta
        meta["rejectReason"] = "anuncio sem quantidade clara; produto e pack/kit"
        return None, meta

    meta["listingUnits"] = listing_u
    if listing_u == product_u:
        meta["packMatch"] = True
        return float(listing_price), meta

    # Normalize via unit price when listing is a different pack of the same item.
    unit_price = float(listing_price) / listing_u
    comparable = round(unit_price * product_u, 2)
    meta["packMatch"] = False
    meta["normalized"] = True
    meta["unitPrice"] = round(unit_price, 4)
    meta["note"] = (
        f"Anuncio com {listing_u} un. normalizado para {product_u} un. "
        f"(R$ {unit_price:.4f}/un)"
    )
    return comparable, meta
