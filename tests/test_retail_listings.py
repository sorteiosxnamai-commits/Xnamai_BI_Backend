from app.services.retail_listings import (
    collapse_platform_market_price,
    collect_listings,
    merge_platform_payloads,
    pick_representative_listing,
    sources_from_market_prices,
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


def test_merge_platform_payloads_combines_sellers():
    merged = merge_platform_payloads(
        {
            "platform": "shopee",
            "listings": [{"price": 260, "seller": "A", "url": "https://a", "packMatch": True}],
            "searchesTried": 3,
        },
        {
            "platform": "shopee",
            "listings": [
                {"price": 255, "seller": "B", "url": "https://b", "packMatch": True},
                {"price": 270, "seller": "C", "url": "https://c", "packMatch": True},
            ],
            "searchesTried": 4,
        },
        list_price=50,
    )
    assert merged["sellersCompared"] == 3
    assert {row["seller"] for row in merged["listings"]} == {"A", "B", "C"}


def test_sources_from_market_prices_lists_all_urls():
    sources = sources_from_market_prices(
        {
            "shopee": {
                "price": 260,
                "listings": [
                    {"price": 260, "seller": "A", "url": "https://a", "title": "A"},
                    {"price": 250, "seller": "B", "url": "https://b", "title": "B"},
                ],
            },
            "mercado_livre": {
                "price": 240,
                "listings": [{"price": 240, "seller": "C", "url": "https://c", "title": "C"}],
            },
        }
    )
    assert len(sources) == 3
