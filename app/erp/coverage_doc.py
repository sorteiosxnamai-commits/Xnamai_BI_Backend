"""Gera docs/erp/coverage.md a partir do registro e da matriz de capacidades."""

from app.erp.capabilities import CAPABILITIES
from app.erp.registry import REGISTRY, SYNC_ORDER

GAP_RESOURCES = {
    "read.titles": "alias `titles` → `titulos` (GET)",
    "read.payments": "alias `payments` → `pagamentos` (GET)",
    "write.products": "POST/PUT de produtos, com grades e IDs retornados",
    "write.inventory_publish": "PUT de ajuste de estoque (saldo absoluto)",
    "write.order_cancel": "`POST /api/v1/pedidos/cancelar/{id}`",
    "write.billing": "entidade v1 `faturamento` (criar/alterar)",
    "read.commissions": "alias `commissions` → `comissoes` (paginação própria, `comissao_id`)",
    "read.product_images": "alias `product-images` → `imagens_produto`",
    "read.payment_methods": "alias `payment-methods` → `formas_pagamento`",
    "read.promotions": "alias `promotions` → `promocoes`",
}


def render_coverage() -> str:
    lines = [
        "# Matriz de cobertura ERP × Mercos (gerada)",
        "",
        "Gerado por `python scripts/erp_coverage.py`. Não edite à mão.",
        "",
        "Legenda: **A** implementado no Adaptor analisado · **D** documentado pelo provedor, exige",
        "extensão do Adaptor · **V** depende de inventário/plano da conta · **E** processo próprio do ERP.",
        "",
        "> Cobertura completa da conta Mercos só pode ser afirmada depois de conciliar esta matriz com",
        "> um inventário autorizado da conta (plano, recursos ativos, amostras reais). Nada aqui prova",
        "> acesso real: `accountAccess` começa em `unknown` e só muda com evidência (200/403).",
        "",
        "## 1. Os 12 recursos do Adaptor (leitura)",
        "",
        "| # | Alias | Entidade Mercos | Versão | Tabela ERP | Depende de | Campos mapeados |",
        "|---|---|---|---|---|---|---|",
    ]
    for index, alias in enumerate(SYNC_ORDER, start=1):
        d = REGISTRY[alias]
        deps = ", ".join(f"`{x}`" for x in d.depends_on) or "—"
        lines.append(
            f"| {index} | `{alias}` | `{d.upstream}` | {d.version} | `{d.model.__tablename__}` | {deps} | {len(d.fields)} (+{len(d.children)} listas filhas) |"
        )
    lines += [
        "",
        "Cada recurso tem: importação paginada com checkpoint atômico, campos tipados, tela/detalhe,",
        "snapshot de origem restrito e inventário de campos. Mapeamento campo a campo em",
        "`field-mapping.md`.",
        "",
        "## 2. Capacidades",
        "",
        "| Chave | Capacidade | Sit. | Adaptor | Provedor | ERP implementa | Flag de escrita | Fonte | Motivo / pendência |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in CAPABILITIES:
        flag = f"`ERP_WRITE_{c.write_flag.upper()}`" if c.write_flag else "—"
        lines.append(
            f"| `{c.key}` | {c.label} | {c.situation} | {'sim' if c.adaptor else 'não'} | "
            f"{'sim' if c.documented else 'não'} | {'sim' if c.implemented else 'não'} | {flag} | "
            f"{c.source or '—'} | {c.reason or '—'} |"
        )
    lines += [
        "",
        "## 3. Extensões do Adaptor (Prompt 1 executado)",
        "",
        "O `MercosAdaptor` agora oferece as rotas abaixo (ver `MercosAdaptor/docs/erp-extensions.md`),",
        "mas as que não têm caminho/paginação confirmados vêm **desligadas** até a validação no sandbox",
        "(`MERCOS_ENABLED_EXTENSIONS`). No ERP continuam fora de `enabled` até existir a ligação (serviço,",
        "tela e permissão) e o payload real da conta:",
        "",
    ]
    for key, text in GAP_RESOURCES.items():
        lines.append(f"- `{key}`: {text}")
    lines += [
        "",
        "Já entregue no Adaptor: cota única da conta (local ou Redis, fail-closed), chave ERP com escopos",
        "separados da chave legada, DTOs e allowlist de filtros, `GET /v1/capabilities`. Pendente lá:",
        "imagens de produto (formato diferente) e escrita dos demais cadastros (métodos não confirmados).",
        "",
    ]
    return "\n".join(lines) + "\n"
