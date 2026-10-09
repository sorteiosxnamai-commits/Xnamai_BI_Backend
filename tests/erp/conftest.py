import importlib
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.erp.models  # noqa: F401
from app.erp import config as erp_config
from app.erp import db as erp_db_module
from app.erp.db import ErpBase

# Módulos que importam `erp_settings` por nome e precisam ser redirecionados.
SETTINGS_CONSUMERS = (
    "app.erp.capabilities",
    "app.erp.auth",
    "app.erp.sync.engine",
    "app.erp.sync.rows",
    "app.erp.integrations.mercos_client",
    "app.erp.queue",
    "app.erp.outbox",
    "app.erp.inbox",
    "app.erp.workers",
    "app.erp.cli",
    "app.erp.router",
    "app.erp.routers.access",
    "app.erp.routers.shipping",
    "app.erp.routers.invoice_drafts",
    "app.erp.routers.refunds",
    "app.erp.routers.order_finance",
    "app.erp.routers.commercial",
    "app.erp.routers.integration",
    "app.erp.routers.purchasing",
    "app.erp.routers.inventory",
    "app.erp.routers.finance",
    "app.erp.routers.admin",
    "app.erp.routers.idem",
    "app.erp.routers.webhooks",
    "app.erp.services.inventory",
    "app.erp.workers",
    "app.erp.sessions",
    "app.erp.routers.auth",
    "app.erp.routers.purchasing",
)


PG_URL = os.environ.get("ERP_TEST_DATABASE_URL", "")


@pytest.fixture
def erp_engine():
    """SQLite em memória por padrão; PostgreSQL real com ERP_TEST_DATABASE_URL.

    No PostgreSQL cada teste recria o schema ERP (drop_all + create_all)."""
    if PG_URL:
        engine = create_engine(PG_URL, pool_size=20, max_overflow=20)
        ErpBase.metadata.drop_all(engine)
        ErpBase.metadata.create_all(engine)
        yield engine
        engine.dispose()
        return
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ErpBase.metadata.create_all(engine)
    yield engine


@pytest.fixture
def erp_session_factory(erp_engine):
    factory = sessionmaker(bind=erp_engine, expire_on_commit=False)
    erp_db_module.set_session_factory(factory)
    yield factory
    erp_db_module.set_session_factory(None)


@pytest.fixture
def erp_cfg(monkeypatch):
    """Configuração ERP isolada; cada teste ajusta o que precisa."""

    def build(**overrides):
        values = {
            "erp_enabled": True,
            "erp_connection_id": "test",
            "erp_adaptor_api_key": "erp-key",
            "erp_bootstrap_admins": "admin@xnamai.com",
            # os testes de permissão usam tokens do BI vinculados; o fluxo de login
            # individual tem arquivo próprio e liga/desliga isto explicitamente
            "erp_allow_bi_operator_link": True,
        }
        values.update(overrides)
        cfg = erp_config.ErpSettings(**values)
        monkeypatch.setattr(erp_config, "erp_settings", lambda: cfg)
        for name in SETTINGS_CONSUMERS:
            try:
                module = importlib.import_module(name)
            except ModuleNotFoundError:
                continue
            if hasattr(module, "erp_settings"):
                monkeypatch.setattr(module, "erp_settings", lambda: cfg)
        return cfg

    return build


@pytest.fixture(autouse=True)
def _reset_api_rate_limit():
    """O limitador do BI é uma instância por processo; a suíte soma centenas de chamadas por minuto.

    Só esvazia o balde entre testes: o middleware em si não é alterado."""

    def reset():
        from app.main import app
        from app.middleware.rate_limit import ApiRateLimitMiddleware

        layer = getattr(app, "middleware_stack", None)
        while layer is not None:
            if isinstance(layer, ApiRateLimitMiddleware):
                layer.requests.clear()
                return
            layer = getattr(layer, "app", None)

    reset()
    yield
    reset()
