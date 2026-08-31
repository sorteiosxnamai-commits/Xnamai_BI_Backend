from app.services.retail_pack import detect_pack_units, normalize_listing_price
from app.services.retail_economics import evaluate_channels


def test_detect_pack_units_from_figurinha_title():
    name = "Figurinha Colecionavel Copa do Mundo 2026 Sortido Pacote com 7 Figurinhas ORIGINAL"
    assert detect_pack_units(name) == 7


def test_reject_or_normalize_different_pack_sizes():
    # Same pack size: keep price
    price, meta = normalize_listing_price(listing_price=99.9, listing_units=7, product_units=7)
    assert price == 99.9
    assert meta["packMatch"] is True

    # Different pack: normalize via unit price (14 units listing -> our 7-pack)
    price2, meta2 = normalize_listing_price(listing_price=140.0, listing_units=14, product_units=7)
    assert price2 == 70.0
    assert meta2["normalized"] is True

    # Unknown listing units for multi-unit SKU: reject
    price3, meta3 = normalize_listing_price(listing_price=110.0, listing_units=None, product_units=7)
    assert price3 is None
    assert "rejectReason" in meta3


def test_evaluate_channels_ignores_unknown_pack_for_kit():
    rows = evaluate_channels(
        market_prices={
            "mercado_livre": {"price": 99.9, "units": 7, "seller": "Loja"},
            "site_proprio": {"price": 110.0},  # no units, product is pack 7 -> rejected
        },
        list_price=20,
        economics={},
        product_units=7,
    )
    priced = [r for r in rows if r["hasPrice"]]
    assert len(priced) == 1
    assert priced[0]["platform"] == "mercado_livre"
    assert priced[0]["retailPrice"] == 99.9
