import asyncio
import json
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from alembic import command
from alembic.config import Config
from app.database import Base
from app.erp.db import ErpBase
from app.erp.integrations.mercos_client import ListPage
from app.erp.registry import REGISTRY, SYNC_ORDER, render_field_mapping
from app.erp.sync import engine as sync_engine
from app.main import app
from tests.erp.pg import PG_URL, fresh_database

ROOT = Path(__file__).resolve().parents[2]
ERP_DIR = ROOT / "app" / "erp"


def test_erp_tables_are_separate_from_legacy_metadata():
    assert ErpBase.metadata is not Base.metadata
    assert all(name.startswith("erp_") for name in ErpBase.metadata.tables)
    assert not any(name.startswith("erp_") for name in Base.metadata.tables)
    assert not set(ErpBase.metadata.tables) & set(Base.metadata.tables)


def test_erp_code_never_imports_legacy_sync_or_models():
    forbidden = re.compile(
        r"from app\.models|import app\.models|from app import models|app\.sync\b|"
        r"app\.adaptor\b|(?<![A-Za-z])SyncState|(?<![A-Za-z])SyncRun\b|"
        r"from app\.database import .*Base"
    )
    offenders = []
    for path in ERP_DIR.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if forbidden.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert offenders == []


def test_sync_and_operations_never_write_legacy_tables():
    if PG_URL:
        engine = create_engine(fresh_database("erp_isolation"))
    else:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    Base.metadata.create_all(engine)
    ErpBase.metadata.create_all(engine)
    from app.erp import db as erp_db_module

    erp_db_module.set_session_factory(sessionmaker(bind=engine, expire_on_commit=False))
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO customers (mercos_id, name, active, raw) VALUES ('1', 'BI', TRUE, '{}')")
            )

        def snapshot():
            with engine.connect() as connection:
                return {
                    table: connection.execute(text(f"SELECT * FROM {table}")).fetchall()
                    for table in Base.metadata.tables
                }

        before = snapshot()

        class Fake:
            async def list_page(self, alias, cursor):
                return ListPage(alias, 1, "2026-10-07T10:00:00", None,
                                [{"id": 1, "nome": "ERP", "razao_social": "ERP",
                                  "ultima_alteracao": "2026-10-07T10:00:00"}])

        for resource in ("customers", "products", "orders", "segments"):
            asyncio.run(sync_engine.sync_resource(Fake(), "iso", resource))
        assert snapshot() == before
    finally:
        erp_db_module.set_session_factory(None)


def test_field_mapping_document_is_generated_from_registry_and_current():
    path = ROOT / "docs" / "erp" / "field-mapping.md"
    assert path.exists(), "rode: python scripts/erp_field_mapping.py"
    assert path.read_text(encoding="utf-8") == render_field_mapping()
    content = path.read_text(encoding="utf-8")
    for alias in SYNC_ORDER:
        assert f"## `{alias}`" in content


def test_twelve_resources_registered_with_version_rules():
    assert len(REGISTRY) == 12 and set(REGISTRY) == set(SYNC_ORDER)
    assert REGISTRY["orders"].version == "v2"
    assert REGISTRY["order-types"].upstream == "pedidos/tipo" and REGISTRY["order-types"].version == "v1"
    assert all(d.version == "v1" for alias, d in REGISTRY.items() if alias != "orders")
    # dependências antes dos dependentes
    for alias in SYNC_ORDER:
        for dependency in REGISTRY[alias].depends_on:
            assert SYNC_ORDER.index(dependency) < SYNC_ORDER.index(alias), (alias, dependency)


def test_every_mapped_column_exists_on_its_model():
    for alias, definition in REGISTRY.items():
        columns = set(definition.model.__table__.columns.keys())
        for field in definition.fields:
            assert field.attr in columns, (alias, field.attr)
        for child in definition.children:
            child_columns = set(child.model.__table__.columns.keys())
            assert child.fk in child_columns
            for field in child.fields:
                assert field.attr in child_columns, (alias, child.name, field.attr)


def test_migration_adds_only_erp_tables_and_downgrades_cleanly(tmp_path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "20260831_02")
    legacy = set(inspect(create_engine(url)).get_table_names()) - {"alembic_version"}
    legacy_columns = {
        t: sorted(c["name"] for c in inspect(create_engine(url)).get_columns(t)) for t in legacy
    }
    command.upgrade(config, "head")
    after = inspect(create_engine(url))
    tables = set(after.get_table_names()) - {"alembic_version"}
    assert {t for t in tables if t.startswith("erp_")} == set(ErpBase.metadata.tables)
    assert tables - set(ErpBase.metadata.tables) == legacy  # nada legado criado/apagado
    for table in legacy:
        assert sorted(c["name"] for c in after.get_columns(table)) == legacy_columns[table]
    command.downgrade(config, "20260831_02")
    back = set(inspect(create_engine(url)).get_table_names()) - {"alembic_version"}
    assert back == legacy


def test_migration_sql_has_no_legacy_ddl():
    source = (ROOT / "alembic" / "versions" / "20261007_01_erp_foundation.py").read_text(encoding="utf-8")
    assert "down_revision = \"20260831_02\"" in source
    names = set(re.findall(r"op\.(?:create_table|drop_table)\('([^']+)'", source))
    assert names and all(n.startswith("erp_") for n in names)
    assert not re.search(r"op\.(alter_column|add_column|drop_column)\(", source)
    assert not re.search(r"op\.drop_table\('(?!erp_)", source)
    assert all(
        t.startswith("erp_") for t in re.findall(r"table_name='([^']+)'", source)
    )


def test_openapi_publishes_erp_contract_and_keeps_legacy_paths():
    schema = app.openapi()
    paths = schema["paths"]
    required = {
        "/api/v1/erp/me", "/api/v1/erp/capabilities", "/api/v1/erp/overview",
        "/api/v1/erp/customers", "/api/v1/erp/customers/{external_id}",
        "/api/v1/erp/products", "/api/v1/erp/products/{external_id}/prices",
        "/api/v1/erp/products/{external_id}/variants",
        "/api/v1/erp/sales-orders", "/api/v1/erp/sales-orders/{external_id}/cancel",
        "/api/v1/erp/sales-orders/{external_id}/billings",
        "/api/v1/erp/catalogs/{resource}", "/api/v1/erp/external-titles",
        "/api/v1/erp/payments", "/api/v1/erp/commissions", "/api/v1/erp/promotions",
        "/api/v1/erp/integration/status", "/api/v1/erp/integration/sync",
        "/api/v1/erp/integration/runs", "/api/v1/erp/integration/runs/{run_id}",
        "/api/v1/erp/integration/operations",
        "/api/v1/erp/integration/operations/{operation_id}/reconcile",
        "/api/v1/erp/integration/conflicts",
        "/api/v1/erp/integration/conflicts/{conflict_id}/resolve",
        "/api/v1/erp/audit-events", "/api/v1/erp/webhooks/mercos",
        "/api/v1/erp/suppliers", "/api/v1/erp/purchase-orders",
        "/api/v1/erp/inventory/balances", "/api/v1/erp/inventory/movements",
        "/api/v1/erp/inventory/adjustments", "/api/v1/erp/inventory/transfers",
        "/api/v1/erp/finance/titles", "/api/v1/erp/finance/cash-flow",
    }
    assert required <= set(paths)
    legacy = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))["paths"]
    # contrato legado antes/depois: nenhuma rota ou método legado some ou muda
    for path, methods in legacy.items():
        if path.startswith("/api/v1/erp"):
            continue
        assert path in paths, path
        assert set(methods) <= set(paths[path]), path


def test_every_erp_list_endpoint_is_paginated_in_server(client_paths=None):
    paths = app.openapi()["paths"]
    for path in (
        "/api/v1/erp/customers", "/api/v1/erp/products", "/api/v1/erp/sales-orders",
        "/api/v1/erp/integration/runs", "/api/v1/erp/integration/operations",
    ):
        names = {p["name"] for p in paths[path]["get"]["parameters"]}
        assert {"page", "page_size"} <= names, path


@pytest.mark.parametrize("alias", SYNC_ORDER)
def test_registry_key_function_rejects_rows_without_id(alias):
    with pytest.raises(ValueError):
        REGISTRY[alias].key({"nome": "sem id"})
    if select is None:  # pragma: no cover
        raise AssertionError


def test_coverage_document_is_generated_and_current():
    from app.erp.capabilities import CAPABILITIES
    from app.erp.coverage_doc import render_coverage

    path = ROOT / "docs" / "erp" / "coverage.md"
    assert path.exists(), "rode: python scripts/erp_coverage.py"
    assert path.read_text(encoding="utf-8") == render_coverage()
    content = path.read_text(encoding="utf-8")
    assert all(f"`{item.key}`" in content for item in CAPABILITIES)
    # nada pendente de extensão aparece como habilitado/implementado sem estar no código
    pending = [c for c in CAPABILITIES if c.situation == "D" and not c.adaptor]
    assert pending and all(not c.implemented or c.key == "webhooks.receive" for c in pending)


@pytest.mark.skipif(not PG_URL, reason="requer ERP_TEST_DATABASE_URL (PostgreSQL real)")
def test_full_migration_chain_on_postgres_up_and_down():
    """Cadeia legada + ERP em PostgreSQL real, e rollback do ERP sem tocar o legado."""
    url = fresh_database("erp_migration")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "20260831_02")
    engine = create_engine(url)
    legacy = set(inspect(engine).get_table_names()) - {"alembic_version"}
    legacy_cols = {t: sorted(c["name"] for c in inspect(engine).get_columns(t)) for t in legacy}
    command.upgrade(config, "head")
    after = inspect(create_engine(url))
    tables = set(after.get_table_names()) - {"alembic_version"}
    assert tables - set(ErpBase.metadata.tables) == legacy
    assert {t for t in tables if t.startswith("erp_")} == set(ErpBase.metadata.tables)
    for table in legacy:
        assert sorted(c["name"] for c in after.get_columns(table)) == legacy_cols[table]
    # as constraints de saldo existem de fato no banco (não só no modelo)
    checks = {c["name"] for c in after.get_check_constraints("erp_inventory_balances")}
    assert any("on_hand_non_negative" in n for n in checks)
    assert any("reserved_within_on_hand" in n for n in checks)
    assert any("settled_within_amount" in c["name"] for c in after.get_check_constraints("erp_fin_installments"))
    command.downgrade(config, "20260831_02")
    back = set(inspect(create_engine(url)).get_table_names()) - {"alembic_version"}
    assert back == legacy
    command.upgrade(config, "head")  # idempotente após o rollback
    command.downgrade(config, "base")


def _legacy_fingerprint(engine) -> dict[str, str]:
    """Hash do conteúdo completo de cada tabela legada (ordem estável)."""
    import hashlib

    result = {}
    with engine.connect() as conn:
        for table in sorted(Base.metadata.tables):
            rows = conn.execute(text(f'SELECT * FROM "{table}"')).fetchall()
            result[table] = hashlib.sha256(
                repr(sorted(map(repr, rows))).encode()
            ).hexdigest() + f":{len(rows)}"
    return result


@pytest.mark.skipif(not PG_URL, reason="requer ERP_TEST_DATABASE_URL (PostgreSQL real)")
def test_erp_installs_over_existing_legacy_data_and_keeps_every_legacy_row():
    from datetime import datetime, timezone
    from decimal import Decimal

    from sqlalchemy.orm import sessionmaker

    from app import models as legacy
    from app.erp import db as erp_db_module
    from app.erp.integrations.mercos_client import ListPage

    url = fresh_database("erp_over_legacy")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    # Estado real do legado: as tabelas-base vêm do create_all do app (lifespan) e o Alembic
    # legado só as ajusta. Reproduz esse caminho antes de instalar o ERP.
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    command.upgrade(config, "20260831_02")
    now = datetime.now(timezone.utc)
    with sessionmaker(bind=engine)() as db:
        db.add_all([
            legacy.Customer(mercos_id="c1", name="Cliente Teste", document="000", raw={"a": 1}),
            legacy.Product(mercos_id="p1", code="P1", name="Produto", list_price=Decimal("9.90"), raw={}),
            legacy.Seller(mercos_id="s1", name="Vendedor", raw={}),
            legacy.Order(mercos_id="o1", number="1", status="2", total=Decimal("10.00"),
                         customer_mercos_id="c1", issued_at=now, raw={}),
            legacy.OrderItem(order_mercos_id="o1", position=0, name="Produto", quantity=Decimal("1"),
                             unit_price=Decimal("10.00"), total=Decimal("10.00"), raw={}),
            legacy.SyncState(resource="orders", status="success", records=1),
            legacy.CrmAttendance(customer_mercos_id="c1", status="open", created_at=now, updated_at=now),
        ])
        db.commit()
    before = _legacy_fingerprint(engine)
    assert before["customers"].endswith(":1") and before["orders"].endswith(":1")

    command.upgrade(config, "head")
    assert _legacy_fingerprint(engine) == before  # a migração não tocou nenhuma linha legada

    # o ERP funcionando em cima do mesmo banco também não escreve no legado
    erp_db_module.set_session_factory(sessionmaker(bind=engine, expire_on_commit=False))
    try:
        class Fake:
            async def list_page(self, alias, cursor):
                return ListPage(alias, 1, "2026-10-07T10:00:00", None,
                                [{"id": 1, "razao_social": "ERP", "ultima_alteracao": "2026-10-07T10:00:00"}])

        for resource in ("customers", "segments"):
            asyncio.run(sync_engine.sync_resource(Fake(), "over-legacy", resource))
    finally:
        erp_db_module.set_session_factory(None)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM erp_customers")).scalar() == 1
    assert _legacy_fingerprint(engine) == before

    command.downgrade(config, "20260831_02")  # rollback do ERP
    assert _legacy_fingerprint(engine) == before  # dados legados intactos após o rollback
    tables = set(inspect(create_engine(url)).get_table_names())
    assert not any(t.startswith("erp_") for t in tables)


NEW_PANEL_TABLES = {
    "erp_shipping_quotes", "erp_shipping_volumes", "erp_shipping_options",
    "erp_invoice_drafts", "erp_invoice_draft_items", "erp_refund_requests", "erp_refund_events",
}


def _fingerprint_tables(engine, names) -> dict[str, str]:
    import hashlib

    result = {}
    with engine.connect() as conn:
        for table in sorted(names):
            rows = conn.execute(text(f'SELECT * FROM "{table}"')).fetchall()
            result[table] = hashlib.sha256(repr(sorted(map(repr, rows))).encode()).hexdigest() + f":{len(rows)}"
    return result


def test_panel_migration_only_creates_new_erp_tables():
    """Estática: a migração do painel só mexe nas 7 tabelas novas (nada legado, nada do ERP publicado)."""
    text_ = (ROOT / "alembic" / "versions" / "20261009_01_erp_operational_panel.py").read_text(encoding="utf-8")
    created = set(re.findall(r"op\.create_table\('([a-z_]+)'", text_))
    dropped = set(re.findall(r"op\.drop_table\('([a-z_]+)'", text_))
    assert created == NEW_PANEL_TABLES == dropped
    assert 'down_revision = "20261007_01"' in text_
    assert "alter_column" not in text_ and "add_column" not in text_ and "drop_column" not in text_


@pytest.mark.skipif(not PG_URL, reason="requer ERP_TEST_DATABASE_URL (PostgreSQL real)")
def test_panel_migration_is_additive_over_published_erp_data_and_reversible():
    from datetime import datetime, timezone

    from app.erp.models import ErpAuditEvent, ErpCustomer, ErpSalesOrder

    url = fresh_database("erp_panel_migration")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    command.upgrade(config, "20261007_01")  # estado já publicado em produção
    with sessionmaker(bind=engine)() as db:
        db.add_all([
            ErpCustomer(connection_id="xnamai", external_id="c1", name="Cliente publicado"),
            ErpSalesOrder(connection_id="xnamai", external_id="o1", number="1"),
            ErpAuditEvent(operator="ana@x.com", action="x", connection_id="xnamai", at=datetime.now(timezone.utc)),
        ])
        db.commit()
    published = set(inspect(engine).get_table_names()) - NEW_PANEL_TABLES - {"alembic_version"}  # marcador de revisão muda por definição
    assert not (set(inspect(engine).get_table_names()) & NEW_PANEL_TABLES)
    before = _fingerprint_tables(engine, published)

    command.upgrade(config, "head")
    assert NEW_PANEL_TABLES <= set(inspect(engine).get_table_names())
    assert _fingerprint_tables(engine, published) == before  # legado e ERP publicado intactos, linha a linha
    # as tabelas novas funcionam com as restrições de verdade (índice parcial: uma seleção por pedido)
    with engine.begin() as conn:
        for number in (1, 2):
            conn.execute(text(
                "INSERT INTO erp_shipping_quotes (connection_id, order_external_id, order_version, status, source, "
                "version, created_by, created_at, updated_at) VALUES ('xnamai','o1',1,:s,'manual',1,'t',now(),now())"
            ), {"s": "selected" if number == 1 else "draft"})
    with pytest.raises(Exception):  # segunda cotação selecionada do mesmo pedido
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO erp_shipping_quotes (connection_id, order_external_id, order_version, status, source, "
                "version, created_by, created_at, updated_at) VALUES ('xnamai','o1',1,'selected','manual',1,'t',now(),now())"
            ))

    command.downgrade(config, "20261007_01")
    assert not (set(inspect(create_engine(url)).get_table_names()) & NEW_PANEL_TABLES)
    assert _fingerprint_tables(create_engine(url), published) == before  # retorno preserva o que já existia
