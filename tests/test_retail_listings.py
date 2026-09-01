from app.services.retail_listings import (
    collapse_platform_market_price,
    collect_listings,
    is_placeholder_seller,
    is_usable_listing_url,
    merge_platform_payloads,
    pick_representative_listing,
    sources_from_market_prices,
)
from app.services.retail_economics import evaluate_channels, select_recommended_channel


def test_collect_listings_dedupes_and_keeps_multiple_sellers():
    payload = {
        "price": 800,
        "seller": "Loja Real A",
        "url": "https://shopee.com.br/a",
        "listings": [
            {"price": 790, "seller": "Loja Real B", "url": "https://shopee.com.br/1", "packMatch": True},
            {"price": 810, "seller": "Loja Real C", "url": "https://shopee.com.br/2", "packMatch": True},
            {"price": 790, "seller": "Loja Real B", "url": "https://shopee.com.br/1", "packMatch": True},
        ],
    }
    listings = collect_listings(payload, list_price=100, platform="shopee")
    assert len(listings) == 3
    assert {row["seller"] for row in listings} == {"Loja Real A", "Loja Real B", "Loja Real C"}


def test_rejects_placeholder_loja_abc_and_fake_urls():
    payload = {
        "platform": "mercado_livre",
        "listings": [
            {"price": 145, "seller": "Loja A", "url": "https://...", "packMatch": True},
            {"price": 145, "seller": "Loja B", "url": "https://example.com/x", "packMatch": True},
            {"price": 162, "seller": "Loja C", "url": "https://mercadolivre.com.br/real", "packMatch": True},
            {
                "price": 150,
                "seller": "Full Time Informatica",
                "url": "https://produto.mercadolivre.com.br/MLB-123",
                "packMatch": True,
            },
        ],
    }
    listings = collect_listings(payload, list_price=85, platform="mercado_livre")
    assert len(listings) == 1
    assert listings[0]["seller"] == "Full Time Informatica"
    assert is_placeholder_seller("Loja A")
    assert not is_usable_listing_url("https://...")


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
            "listings": [
                {
                    "price": 260,
                    "seller": "A",
                    "url": "https://shopee.com.br/a",
                    "packMatch": True,
                }
            ],
            "searchesTried": 3,
        },
        {
            "platform": "shopee",
            "listings": [
                {
                    "price": 255,
                    "seller": "B",
                    "url": "https://shopee.com.br/b",
                    "packMatch": True,
                },
                {
                    "price": 270,
                    "seller": "C",
                    "url": "https://shopee.com.br/c",
                    "packMatch": True,
                },
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
                    {"price": 260, "seller": "A", "url": "https://shopee.com.br/a", "title": "A"},
                    {"price": 250, "seller": "B", "url": "https://shopee.com.br/b", "title": "B"},
                    {"price": 240, "seller": "fake", "url": "https://...", "title": "fake"},
                ],
            },
            "mercado_livre": {
                "price": 240,
                "listings": [
                    {
                        "price": 240,
                        "seller": "C",
                        "url": "https://produto.mercadolivre.com.br/c",
                        "title": "C",
                    }
                ],
            },
        }
    )
    assert len(sources) == 3


def test_select_recommended_ignores_inflated_own_store_vs_marketplace():
    rows = evaluate_channels(
        market_prices={
            "nuvemshop": {"price": 200, "freight": 18, "seller": "Full Time", "sellersCompared": 4},
            "site_proprio": {"price": 200, "freight": 20, "seller": "Site", "sellersCompared": 3},
            "mercado_livre": {"price": 145, "freight": 22, "seller": "ML Real", "sellersCompared": 3},
            "tiktok": {"price": 109.92, "freight": 8.39, "seller": "TT", "sellersCompared": 3},
            "shopee": {"price": 106.4, "freight": 8.39, "seller": "Shopee", "sellersCompared": 4},
        },
        list_price=85.09,
        economics={},
    )
    best = select_recommended_channel(rows)
    assert best is not None
    # Must not pick Nuvemshop/site at R$200 when marketplaces clear near R$106-145.
    assert best["platform"] != "nuvemshop"
    assert best["platform"] != "site_proprio"
    # Among competitive marketplace prices, prefer non-negative / best margin (ML ~145).
    assert best["platform"] == "mercado_livre"
    assert best["retailPrice"] == 145.0
