"""Roteador agregado do ERP em /api/v1/erp.

O webhook fica fora do guard de login de navegador (autentica por assinatura).
Todas as demais rotas passam por `erp_user`, que nega por padrão e responde 404
quando ERP_ENABLED está desligado.
"""

import os
from datetime import datetime, timezone

from fastapi import APIRouter

from app.erp.common import http_error
from app.erp.config import erp_settings

from app.erp.routers import (
    access,
    admin,
    auth,
    commercial,
    finance,
    integration,
    inventory,
    purchasing,
    webhooks,
)

router = APIRouter(prefix="/api/v1/erp")
_STARTED_AT = datetime.now(timezone.utc)


@router.get("/build", summary="Versão em execução (sem dados de negócio)")
def build():
    """Commit do deploy e flags do consumidor da fila. Sem autenticação: não expõe segredos
    nem dados; responde 404 `erp_disabled` quando o módulo está desligado."""
    cfg = erp_settings()
    if not cfg.erp_enabled:
        raise http_error(404, "erp_disabled", "Módulo ERP desativado")
    return {
        "commit": (os.getenv("RENDER_GIT_COMMIT") or "")[:7] or None,
        "startedAt": _STARTED_AT.isoformat(),
        "queueInScheduler": cfg.erp_queue_in_scheduler,
        "autoSync": cfg.erp_auto_sync,
        "writes": {
            "customers": cfg.erp_write_customers,
            "orders": cfg.erp_write_orders,
            "titles": cfg.erp_write_titles,
            "products": cfg.erp_write_products,
            "inventoryPublish": cfg.erp_write_inventory_publish,
            "billing": cfg.erp_write_billing,
        },
    }

for module in (
    access,
    auth,
    commercial,
    integration,
    purchasing,
    inventory,
    finance,
    admin,
    webhooks,
):
    router.include_router(module.router)
