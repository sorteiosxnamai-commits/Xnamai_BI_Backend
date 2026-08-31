from app.services.retail_listings import (
    collapse_platform_market_price,
    collect_listings,
    pick_representative_listing,
)


def test_collect_listings_dedupes_and_keeps_multiple_sellers():
    payload = {
        "price": 800,
        "seller": "A",
        "listings": [
            {"price": 790, "seller": "B", "url": "https://shopee/1", "packMatch": True},
            {"price": 810, "seller": "C", "url": "https://shopee/2", "packMatch": True},
            {"price": 790, "seller": "B", "url": "https://shopee/1", "packMatch": True},
        ],
    }
    listings = collect_listings(payload, list_price=100)
    assert len(listings) == 3
    assert {row["seller"] for row in listings} == {"A", "B", "C"}


def test_pick_representative_uses_median():
    listings = [
        {"price": 100.0, "seller": "low", "packMatch": True},
        {"price": 200.0, "seller": "mid", "packMatch": True},
        {"price": 900.0, "seller": "outlier", "packMatch": True},
    ]
    best = pick_representative_listing(listings)
    assert best is not None
    assert best["seller"] == "mid"


def test_collapse_rejects_table_price_copy():
    collapsed = collapse_platform_market_price(
        {"price": 50.0, "seller": "fake", "listings": []},
        list_price=50.0,
    )
    assert collapsed["price"] is None
    assert collapsed["sellersCompared"] == 0
