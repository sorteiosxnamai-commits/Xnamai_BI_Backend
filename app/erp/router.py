"""Roteador agregado do ERP em /api/v1/erp.

O webhook fica fora do guard de login de navegador (autentica por assinatura).
Todas as demais rotas passam por `erp_user`, que nega por padrão e responde 404
quando ERP_ENABLED está desligado.
"""

from fastapi import APIRouter

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
