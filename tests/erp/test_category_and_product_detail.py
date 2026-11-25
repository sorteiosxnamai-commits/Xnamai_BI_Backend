"""Categorias locais e detalhe extra do produto."""

from decimal import Decimal

from tests.erp.helpers import bearer, client


def test_category_tree_and_product_detail(erp_cfg, erp_session_factory):
    erp_cfg()
    c = client()
    parent = c.post("/api/v1/erp/categories", json={"name": "Bebidas"}, headers=bearer())
    assert parent.status_code == 201, parent.text
    parent_id = parent.json()["id"]
    assert parent.json()["localOnly"] is True and parent.json()["parentId"] is None

    child = c.post(
        "/api/v1/erp/categories",
        json={"name": "Cafés", "parentId": parent_id},
        headers=bearer(),
    )
    assert child.status_code == 201, child.text
    assert child.json()["parentId"] == parent_id

    cycle = c.patch(
        f"/api/v1/erp/categories/{parent_id}",
        json={"expectedVersion": parent.json()["version"], "parentId": child.json()["id"]},
        headers=bearer(),
    )
    assert cycle.status_code == 422
    assert cycle.json()["detail"]["code"] == "invalid_parent"

    blocked = c.delete(f"/api/v1/erp/categories/{parent_id}", headers=bearer())
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "category_has_children"

    listed = c.get("/api/v1/erp/categories", headers=bearer())
    assert listed.status_code == 200
    names = {item["name"] for item in listed.json()["items"]}
    assert {"Bebidas", "Cafés"} <= names

    created = c.post(
        "/api/v1/erp/products",
        json={
            "name": "Café torrado",
            "listPrice": "18.90",
            "categoryId": child.json()["id"],
            "description": "Grãos com foto https://cdn.example/cafe.jpg",
            "photos": ["https://cdn.example/cafe.jpg"],
            "videos": ["https://cdn.example/cafe.mp4"],
            "grossWeight": "0.5",
            "width": "10",
            "height": "20",
            "length": "8",
            "supplierCode": "FOR-1",
            "barcode": "7890000000000",
            "lot": "L2026",
            "receivedAt": "2026-11-25",
            "packKind": "box",
            "cost": "9.40",
            "stockLocation": "A1-P2",
            "variants": [{"code": "250G", "name": "250 g", "externalStock": "4", "price": "18.90"}],
        },
        headers=bearer(),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["description"].startswith("Grãos")
    assert body["photos"] == ["https://cdn.example/cafe.jpg"]
    assert body["videos"] == ["https://cdn.example/cafe.mp4"]
    assert Decimal(body["grossWeight"]) == Decimal("0.5")
    assert body["supplierCode"] == "FOR-1"
    assert body["barcode"] == "7890000000000"
    assert body["lot"] == "L2026"
    assert body["receivedAt"] == "2026-11-25"
    assert body["packKind"] == "box"
    assert body["cost"] == "9.40"
    assert body["stockLocation"] == "A1-P2"
    assert body["mercosGeolocation"] is None
    assert body["variants"][0]["code"] == "250G"
    assert body["variants"][0]["externalStock"] == "4"

    history = c.get(f"/api/v1/erp/products/{body['id']}/history", headers=bearer())
    assert history.status_code == 200, history.text
    assert history.json()["orders"] == []
    assert history.json()["movements"] == []
    assert history.json()["mercosGeolocation"] is None
    assert "Tray" in history.json()["dimensionsNote"]

    removed = c.delete(f"/api/v1/erp/categories/{child.json()['id']}", headers=bearer())
    assert removed.status_code == 200 and removed.json()["sourceDeleted"] is True
    assert c.delete(f"/api/v1/erp/categories/{parent_id}", headers=bearer()).status_code == 200
