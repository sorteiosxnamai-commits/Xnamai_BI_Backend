"""Manutenção de produtos: CRUD local, estoque, checkbox Mercos e carga."""

from decimal import Decimal

from sqlalchemy import select

from app.erp.models import ErpOperation, ErpProduct
from tests.erp.helpers import add_operator, bearer, client


def test_local_crud_stock_and_mercos_flag(erp_cfg, erp_session_factory):
    erp_cfg()
    c = client()
    created = c.post(
        "/api/v1/erp/products",
        json={"name": "Caneca", "listPrice": "12.50", "code": "CAN", "imageUrl": "https://cdn.example/c.jpg"},
        headers=bearer(),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "Caneca" and body["listPrice"] == "12.50"
    assert body["onMercos"] is False and body["localOnly"] is True
    assert body["imageUrl"] == "https://cdn.example/c.jpg"
    product_id = body["id"]

    listed = c.get("/api/v1/erp/products?search=caneca", headers=bearer()).json()
    assert listed["totalItems"] == 1 and listed["items"][0]["id"] == product_id

    patched = c.patch(
        f"/api/v1/erp/products/{product_id}",
        json={"expectedVersion": body["version"], "externalStock": "8", "onMercos": True},
        headers=bearer(),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["externalStock"] == "8" and patched.json()["onMercos"] is True

    blocked = c.post(
        f"/api/v1/erp/products/{product_id}/publish",
        headers={**bearer(), "Idempotency-Key": "subir-caneca-01"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "capability_disabled"

    removed = c.delete(f"/api/v1/erp/products/{product_id}", headers=bearer())
    assert removed.status_code == 200 and removed.json()["sourceDeleted"] is True
    assert c.get("/api/v1/erp/products?search=caneca", headers=bearer()).json()["totalItems"] == 0
    with erp_session_factory() as db:
        assert db.scalar(select(ErpOperation.id)) is None


def test_publish_queues_product_and_stock_when_flags_are_on(erp_cfg, erp_session_factory):
    erp_cfg(erp_write_products=True, erp_write_inventory_publish=True)
    c = client()
    created = c.post(
        "/api/v1/erp/products",
        json={"name": "Copo", "listPrice": "4.00", "externalStock": "3", "onMercos": True},
        headers=bearer(),
    ).json()
    with erp_session_factory() as db:
        row = db.scalar(select(ErpProduct).where(ErpProduct.external_id == created["id"]))
        row.external_id = "55"
        db.commit()
    unmarked = c.post(
        "/api/v1/erp/products/55/publish",
        headers={**bearer(), "Idempotency-Key": "subir-copo-00"},
    )
    assert unmarked.status_code == 202, unmarked.text
    with erp_session_factory() as db:
        kinds = set(db.scalars(select(ErpOperation.kind)))
        stock = db.scalar(select(ErpOperation).where(ErpOperation.kind == "publish_product_stock"))
        product = db.scalar(select(ErpOperation).where(ErpOperation.kind == "publish_product"))
    assert kinds == {"publish_product", "publish_product_stock"}
    assert product.payload["mercos"]["nome"] == "Copo"
    assert product.payload["mercos"]["preco_tabela"] == 4.0
    assert product.payload["mercos"]["saldo_estoque"] == 3.0
    assert stock.payload["mercos"] == {"novo_saldo": 3.0}
    assert unmarked.json()["stockPublish"] == "queued"
    removed = c.delete("/api/v1/erp/products/55", headers=bearer())
    assert removed.status_code == 200
    with erp_session_factory() as db:
        exclusion = db.scalar(
            select(ErpOperation).where(ErpOperation.idempotency_key.like("del-55-%"))
        )
    assert exclusion.payload["mercos"] == {"excluido": True, "ativo": False}


def test_subir_requires_the_mercos_checkbox(erp_cfg, erp_session_factory):
    erp_cfg(erp_write_products=True)
    c = client()
    created = c.post(
        "/api/v1/erp/products",
        json={"name": "Prato", "listPrice": "9.00"},
        headers=bearer(),
    ).json()
    response = c.post(
        f"/api/v1/erp/products/{created['id']}/publish",
        headers={**bearer(), "Idempotency-Key": "subir-prato-01"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "mercos_not_marked"


def test_consulta_cannot_change_products_and_bad_photo_is_rejected(erp_cfg, erp_session_factory):
    erp_cfg()
    add_operator(erp_session_factory, "consulta@x.com", ["consulta"])
    add_operator(erp_session_factory, "vend@x.com", ["comercial"])
    c = client()
    denied = c.post(
        "/api/v1/erp/products",
        json={"name": "X", "listPrice": "1.00"},
        headers=bearer("consulta@x.com", "viewer"),
    )
    assert denied.status_code == 403
    allowed = c.post(
        "/api/v1/erp/products",
        json={"name": "X", "listPrice": "1.00"},
        headers=bearer("vend@x.com", "viewer"),
    )
    assert allowed.status_code == 201
    photo = c.post(
        "/api/v1/erp/products",
        json={"name": "Y", "listPrice": "1.00", "imageUrl": "javascript:alert(1)"},
        headers=bearer(),
    )
    assert photo.status_code == 422
    stale = c.patch(
        f"/api/v1/erp/products/{allowed.json()['id']}",
        json={"expectedVersion": 9, "externalStock": "1"},
        headers=bearer("vend@x.com", "viewer"),
    )
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_mismatch"


def test_synced_product_is_marked_for_mercos_until_the_operator_unchecks(erp_cfg, erp_session_factory):
    erp_cfg()
    with erp_session_factory() as db:
        db.add(ErpProduct(
            connection_id="test", external_id="20", name="Produto 1", kind="simple", sellable=True,
            list_price=Decimal("9.90"), external_stock=Decimal("12"), active=True, fingerprint="f",
        ))
        db.commit()
    c = client()
    detail = c.get("/api/v1/erp/products/20", headers=bearer()).json()
    assert detail["onMercos"] is True and detail["localOnly"] is False
    updated = c.patch(
        "/api/v1/erp/products/20",
        json={"expectedVersion": detail["version"], "onMercos": False},
        headers=bearer(),
    )
    assert updated.status_code == 200 and updated.json()["onMercos"] is False
