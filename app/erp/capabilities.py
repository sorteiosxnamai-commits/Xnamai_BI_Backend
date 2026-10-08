"""Matriz de capacidades do ERP.

Cada linha registra: suportado pelo Adaptor, documentado pelo provedor, acesso
da conta (unknown/allowed/denied), implementado no ERP, habilitado, data da
última validação e motivo. "Desabilitado" nunca aparece como "sincronizado".
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.erp.common import as_utc, utcnow
from app.erp.config import erp_settings
from app.erp.models.core import ErpCapability


@dataclass(frozen=True)
class CapabilityDef:
    key: str
    label: str
    area: str
    situation: str  # A, D, V, E conforme o documento de especificação
    adaptor: bool
    documented: bool
    implemented: bool
    write_flag: str | None = None
    source: str = ""
    reason: str = ""


READ_RESOURCES = (
    ("customers", "Clientes", "F05"),
    ("products", "Produtos", "F06"),
    ("orders", "Pedidos e orçamentos (v2)", "F07"),
    ("price-tables", "Tabelas de preço", "F26"),
    ("payment-conditions", "Condições de pagamento", "F22"),
    ("carriers", "Transportadoras", "F23"),
    ("commercial-policies", "Políticas comerciais", "F16"),
    ("categories", "Categorias", "F06"),
    ("segments", "Segmentos", "F05"),
    ("order-types", "Tipos de pedido", "F07"),
    ("product-prices", "Preços por produto/tabela", "F17"),
    ("users", "Vendedores", "F15"),
)

PENDING_ADAPTOR = (
    "O Adaptor já tem a rota (desligada até validar no sandbox, MERCOS_ENABLED_EXTENSIONS); falta a ligação no ERP e o payload real da conta."
)

CAPABILITIES: tuple[CapabilityDef, ...] = (
    *(
        CapabilityDef(
            f"read.{alias}",
            f"Leitura: {label}",
            "sync",
            "A",
            True,
            True,
            True,
            source=ref,
        )
        for alias, label, ref in READ_RESOURCES
    ),
    CapabilityDef("write.customers", "Criar/editar cliente", "write", "A", True, True,
                  True, "customers", "F05",
                  "Escrita assíncrona via outbox; sem retry cego."),
    CapabilityDef("write.orders", "Criar/editar pedido", "write", "A", True, True,
                  True, "orders", "F07",
                  "Escrita assíncrona via outbox; v2."),
    CapabilityDef("write.titles", "Criar/editar títulos", "write", "A", True, True,
                  False, "titles", "F09",
                  "Rota existe no Adaptor, mas só habilita após leitura/reconciliação de títulos."),
    CapabilityDef("read.titles", "Consultar títulos", "sync", "D", True, True, False,
                  source="F09", reason=PENDING_ADAPTOR + " Alias titles → titulos."),
    CapabilityDef("read.payments", "Pagamentos Mercos Pay", "sync", "D", True, True,
                  False, source="F10", reason=PENDING_ADAPTOR + " Ativação da conta a verificar."),
    CapabilityDef("write.products", "Criar/editar produtos", "write", "D", True, True,
                  False, "products", "F06", PENDING_ADAPTOR),
    CapabilityDef("write.inventory_publish", "Publicar ajuste de estoque", "write", "D",
                  True, True, False, "inventory_publish", "F08",
                  PENDING_ADAPTOR + " Saldo é absoluto; exige autoridade definida."),
    CapabilityDef("write.order_cancel", "Cancelar pedido", "write", "D", True, True,
                  False, "orders", "F27", PENDING_ADAPTOR + " Operação dedicada documentada."),
    CapabilityDef("write.billing", "Registrar/alterar faturamento", "write", "D", True,
                  True, False, "billing", "F28", PENDING_ADAPTOR),
    CapabilityDef("read.commissions", "Comissões", "sync", "D", True, True, False,
                  source="F12", reason=PENDING_ADAPTOR + " Chave própria `comissao_id`."),
    CapabilityDef("read.product_images", "Imagens de produto", "sync", "D", False, True,
                  False, source="F25",
                  reason=PENDING_ADAPTOR + " Leitura devolve hashes, não URLs."),
    CapabilityDef("read.payment_methods", "Formas de pagamento", "sync", "D", True, True,
                  False, source="F21", reason=PENDING_ADAPTOR),
    CapabilityDef("read.promotions", "Promoções", "sync", "D", True, True, False,
                  source="F24", reason=PENDING_ADAPTOR + " ID pode mudar."),
    CapabilityDef("webhooks.receive", "Receptor de webhooks Mercos", "inbound", "D/V",
                  False, True, True, source="F13/F14",
                  reason="Receptor implementado no ERP; habilitação e payloads reais da conta pendentes."),
    CapabilityDef("scope.divisions", "Divisões/representadas", "scope", "D/V", False,
                  True, False, source="F29",
                  reason="Escopo da conta precisa ser inventariado; HTTP 200 não prova recorte."),
    CapabilityDef("ref.tags_networks_extras", "Tags, redes, motivos de bloqueio, campos extras",
                  "reference", "V", False, False, False,
                  reason="Campos de referência chegam no payload; endpoints relacionados a inventariar."),
    CapabilityDef("ref.goals_tasks_crm", "Metas, tarefas, atividades, oportunidades",
                  "reference", "V", False, False, False,
                  reason="Sem API validada; consta na matriz sem promessa."),
    CapabilityDef("ref.tax_b2b_config", "Configurações tributárias e B2B", "reference", "V",
                  False, False, False, reason="Sem API validada; consta na matriz sem promessa."),
    CapabilityDef("local.suppliers_purchases", "Fornecedores e compras", "local", "E",
                  False, False, True,
                  reason="Processo próprio do ERP."),
    CapabilityDef("local.inventory", "Depósitos, razão e reservas", "local", "E", False,
                  False, True, reason="Processo próprio; publicação ao Mercos desabilitada."),
    CapabilityDef("local.finance", "Contas a pagar, caixa e centros de custo", "local", "E",
                  False, False, True, reason="Processo próprio; não confunde título Mercos."),
    CapabilityDef("local.fiscal_banking", "Fiscal eletrônico, bancos, boletos, logística",
                  "local", "E/V", False, False, False,
                  reason="Integrações específicas futuras; não inferir de campos ou links."),
    CapabilityDef("legacy.bi_crm_retail", "BI, CRM e Análise Varejo atuais", "legacy",
                  "Existente", False, False, True,
                  reason="Permanecem acessíveis e inalterados."),
)

BY_KEY = {item.key: item for item in CAPABILITIES}


def _enabled(
    item: CapabilityDef, access: str, adaptor_supported: bool = False
) -> tuple[bool, str | None]:
    cfg = erp_settings()
    if not item.implemented:
        if adaptor_supported:
            return False, "O Adaptor já oferece esta capacidade; falta a ligação no ERP (tela/serviço)"
        return False, item.reason or "Não implementado"
    if not cfg.erp_enabled:
        return False, "ERP_ENABLED desligado"
    if access == "denied":
        return False, "Conta sem acesso confirmado"
    if item.write_flag:
        if not cfg.write_enabled(item.write_flag):
            return False, f"Flag ERP_WRITE_{item.write_flag.upper()} desligada"
        if not cfg.has_write_key:
            return False, "Chave ERP de escrita do Adaptor não configurada"
    return True, None


# Chave ERP -> chave do Adaptor (GET /v1/capabilities).
ADAPTOR_KEYS: dict[str, str] = {
    **{f"read.{alias}": f"read.{alias}" for alias in (
        "customers", "products", "orders", "price-tables", "payment-conditions", "carriers",
        "commercial-policies", "categories", "segments", "order-types", "product-prices", "users",
    )},
    "write.customers": "write.customers",
    "write.orders": "write.orders",
    "write.titles": "write.titles",
    "read.titles": "read.titles",
    "read.payments": "read.payments",
    "read.payment_methods": "read.payment-methods",
    "read.promotions": "read.promotions",
    "read.commissions": "read.commissions",
    "write.products": "write.products",
    "write.inventory_publish": "write.stock",
    "write.order_cancel": "write.order-cancel",
    "write.billing": "write.billing",
}


def apply_discovery(db: Session, connection_id: str, payload: dict | None) -> dict:
    """Atualiza `supportedByAdaptor` com o que o Adaptor DECLARA (build + chave + escopo).

    Isto não prova acesso da conta Mercos: `accountAccess` continua só com 200/403 reais."""
    rows = ensure_rows(db, connection_id)
    if payload is None:
        return {"adaptor": "legacy-build", "updated": 0, "note": "Adaptor sem /v1/capabilities"}
    scopes = set(payload.get("scopes") or [])
    declared = {c.get("key"): c for c in payload.get("capabilities", []) if isinstance(c, dict)}
    updated = 0
    for erp_key, adaptor_key in ADAPTOR_KEYS.items():
        row = rows.get(erp_key)
        cap = declared.get(adaptor_key)
        if row is None:
            continue
        supported = bool(cap and cap.get("enabled") and cap.get("scope") in scopes)
        row.supported_by_adaptor = supported
        if cap is None:
            row.reason = "Adaptor não declara esta capacidade"
        elif not cap.get("enabled"):
            row.reason = "Adaptor: capacidade desabilitada (MERCOS_ENABLED_EXTENSIONS)"
        elif cap.get("scope") not in scopes:
            row.reason = f"Chave ERP do Adaptor sem o escopo '{cap.get('scope')}'"
        else:
            row.reason = None
        row.last_validated_at = utcnow()
        db.add(row)
        updated += 1
    return {
        "adaptor": "extended",
        "adaptorVersion": payload.get("version"),
        "principal": payload.get("principal"),
        "scopes": sorted(scopes),
        "quotaCoordination": payload.get("quotaCoordination"),
        "updated": updated,
    }


def _load_rows(db: Session, connection_id: str) -> dict[str, ErpCapability]:
    return {
        row.key: row
        for row in db.scalars(
            select(ErpCapability).where(ErpCapability.connection_id == connection_id)
        )
    }


def ensure_rows(db: Session, connection_id: str) -> dict[str, ErpCapability]:
    rows = _load_rows(db, connection_id)
    missing = [item for item in CAPABILITIES if item.key not in rows]
    if missing:
        # Duas primeiras requisições simultâneas criariam as mesmas linhas: a segunda
        # viola a unicidade. Cria em SAVEPOINT e, se perdeu a corrida, relê.
        try:
            with db.begin_nested():
                for item in missing:
                    db.add(
                        ErpCapability(
                            connection_id=connection_id,
                            key=item.key,
                            supported_by_adaptor=item.adaptor,
                        )
                    )
                db.flush()
        except IntegrityError:
            pass
        rows = _load_rows(db, connection_id)
    for item in CAPABILITIES:
        row = rows[item.key]
        row.documented_by_provider = item.documented
        row.implemented_in_erp = item.implemented
        db.add(row)
    db.flush()
    return rows


def record_access(
    db: Session,
    connection_id: str,
    key: str,
    access: str,
    reason: str | None = None,
) -> None:
    """Registra evidência real (403/200) sobre o acesso da conta."""
    rows = ensure_rows(db, connection_id)
    row = rows.get(key)
    if row is None:
        return
    row.account_access = access
    row.last_validated_at = utcnow()
    if reason:
        row.reason = reason
    db.add(row)


def build_matrix(db: Session, connection_id: str) -> list[dict]:
    rows = ensure_rows(db, connection_id)
    result = []
    for item in CAPABILITIES:
        row = rows[item.key]
        access = row.account_access or "unknown"
        enabled, why = _enabled(item, access, row.supported_by_adaptor)
        validated: datetime | None = as_utc(row.last_validated_at)
        result.append(
            {
                "key": item.key,
                "label": item.label,
                "area": item.area,
                "situation": item.situation,
                "supportedByAdaptor": row.supported_by_adaptor,
                "documentedByProvider": item.documented,
                "accountAccess": access,
                "implementedInErp": item.implemented,
                "enabled": enabled,
                "lastValidatedAt": validated.isoformat() if validated else None,
                "reason": row.reason or why or item.reason or None,
                "source": item.source or None,
            }
        )
    return result


def capability_enabled(db: Session, connection_id: str, key: str) -> tuple[bool, str | None]:
    item = BY_KEY[key]
    rows = ensure_rows(db, connection_id)
    return _enabled(item, rows[key].account_access or "unknown", rows[key].supported_by_adaptor)
