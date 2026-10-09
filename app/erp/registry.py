"""Registro explícito dos 12 recursos do Adaptor e do mapeamento campo a campo.

Cada recurso define a chave externa, o modelo tipado do ERP, os campos mapeados
(origem → coluna) e as listas filhas. Campo ausente no payload não altera o
valor local; `null` explícito grava null; lista vazia explícita limpa os filhos;
lista ausente preserva os filhos. Tudo o que não está mapeado entra no inventário
de campos e continua disponível no snapshot restrito.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.domain.order_status import (
    CANCELLED_ORDER_STATUSES,
    QUOTE_ORDER_STATUSES,
    VALID_SALE_STATUSES,
    normalize_order_status,
)
from app.erp.common import parse_source_date, parse_source_instant, to_decimal
from app.erp.models import commercial as m

# --- conversores ---------------------------------------------------------------


def text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def ident(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip() or None


def dec(value: Any) -> Decimal | None:
    return to_decimal(value)


def boolean(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "sim", "s", "yes"}


def instant(value: Any) -> datetime | None:
    return parse_source_instant(value)


def day(value: Any) -> date | None:
    return parse_source_date(value)


def integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(Decimal(str(value)))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Inteiro inválido: {value!r}") from exc


def json_value(value: Any) -> Any:
    return value


# --- especificação -------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    attr: str
    keys: tuple[str, ...]
    conv: Callable[[Any], Any] = text
    whole_row: bool = False
    note: str = ""


@dataclass(frozen=True)
class Child:
    name: str
    keys: tuple[str, ...]
    model: type
    fk: str
    fields: tuple[Field, ...]
    note: str = ""


@dataclass(frozen=True)
class ResourceDef:
    alias: str
    label: str
    upstream: str
    version: str
    model: type
    fields: tuple[Field, ...]
    key: Callable[[dict], str]
    children: tuple[Child, ...] = ()
    handled_keys: frozenset[str] = frozenset()
    kind: str = "catalog"
    depends_on: tuple[str, ...] = ()
    note: str = ""
    finalize: Callable[[Any, dict, "ResourceDef"], None] | None = None
    lookup: Callable[..., Any] | None = None
    delete_flag_keys: tuple[str, ...] = ("excluido",)

    @property
    def known_keys(self) -> frozenset[str]:
        keys = {"id", "ultima_alteracao", *self.handled_keys}
        for item in self.fields:
            keys.update(item.keys)
        for child in self.children:
            keys.update(child.keys)
        keys.update(self.delete_flag_keys)
        return frozenset(keys)


def _key_id(row: dict) -> str:
    value = ident(row.get("id"))
    if value is None:
        raise ValueError("Registro sem id no payload de origem")
    return value


def _first_present(row: dict, keys: tuple[str, ...]) -> tuple[bool, Any]:
    """Chave presente com valor útil. Nulo e contêiner vazio (`[]`, `{}`) valem como nulo: o
    Mercos devolve `[]` para campos escalares sem valor, e isso não é número/texto/booleano."""
    present = False
    for key in keys:
        if key in row:
            present = True
            value = row[key]
            if value is None or (isinstance(value, (list, dict)) and not value):
                continue
            return True, value
    return present, None


def evaluate_fields(row: dict, fields: tuple[Field, ...]) -> dict[str, Any]:
    """Aplica somente campos presentes. Erros de conversão sobem como ValueError."""
    values: dict[str, Any] = {}
    for item in fields:
        present, raw = _first_present(row, item.keys)
        if not present:
            continue
        values[item.attr] = item.conv(row) if item.whole_row else item.conv(raw)
    return values


def collect_keys(row: dict) -> set[str]:
    """Campos de topo e de listas filhas (`itens[].preco`), para o inventário."""
    keys: set[str] = set()
    for key, value in row.items():
        keys.add(str(key))
        if isinstance(value, list):
            for element in value[:200]:
                if isinstance(element, dict):
                    keys.update(f"{key}[].{inner}" for inner in element)
    return keys


# --- clientes ------------------------------------------------------------------


def _first_email(row: dict) -> str | None:
    emails = row.get("emails")
    if isinstance(emails, list):
        for item in emails:
            value = item.get("email") if isinstance(item, dict) else item
            if text(value):
                return text(value)
        return None
    return text(row.get("email"))


def _credit_total(value: Any) -> Decimal | None:
    """`limite_credito` no Mercos é uma lista `[{limite_total, limite_disponivel}]` (vazia quando
    não há limite). O limite local é o total do primeiro item; a lista inteira, com o disponível,
    fica preservada em `extras.limite_credito`. Número solto (formato antigo) também é aceito."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("limite_total")
    return to_decimal(value)


def _customer_extras(row: dict) -> dict:
    return {
        "limite_credito": row.get("limite_credito"),
        "emails": row.get("emails"),
        "telefones": row.get("telefones"),
        "campos_extras": row.get("extras"),
        "tags": row.get("tags"),
    }


def _contact_name(contact: dict) -> str | None:
    return text(contact.get("nome"))


CUSTOMER_FIELDS = (
    Field("name", ("razao_social", "nome"), lambda r: text(r.get("razao_social") or r.get("nome")) or "Sem nome", True),
    Field("trade_name", ("nome_fantasia",)),
    Field("person_type", ("tipo",)),
    Field("document", ("cnpj", "cpf"), lambda r: text(r.get("cnpj") or r.get("cpf")), True,
          "Documento alfanumérico preservado; sem remoção de caracteres."),
    Field("state_registration", ("inscricao_estadual",)),
    Field("suframa", ("suframa",)),
    Field("street", ("rua",)),
    Field("number", ("numero",)),
    Field("complement", ("complemento",)),
    Field("district", ("bairro",)),
    Field("zip_code", ("cep",)),
    Field("city", ("cidade",)),
    Field("state", ("estado",)),
    Field("email", ("emails", "email"), _first_email, True, "Primeiro e-mail; demais em extras.emails."),
    Field("phone", ("telefone",)),
    Field("mobile", ("celular",)),
    Field("segment_external_id", ("segmento_id",), ident),
    Field("seller_external_id", ("vendedor_id", "usuario_id"), ident),
    Field("blocked", ("bloqueado",), boolean),
    Field("block_reason", ("motivo_bloqueio",)),
    Field("credit_limit", ("limite_credito",), _credit_total, False,
          "Total do primeiro item da lista `limite_credito`; o disponível fica em extras."),
    Field("active", ("ativo",), boolean),
    Field("notes", ("observacao", "observacoes")),
    Field("extras", ("extras", "emails", "telefones", "tags", "limite_credito"), _customer_extras, True,
          "Campos extras, e-mails/telefones adicionais e tags."),
    Field("source_created_at", ("data_criacao",), instant),
)

CUSTOMER_CHILDREN = (
    Child(
        "contacts",
        ("contatos",),
        m.ErpCustomerContact,
        "customer_id",
        (
            Field("external_id", ("id",), ident),
            Field("name", ("nome",)),
            Field("role", ("cargo",)),
            Field("email", ("emails", "email"), _first_email, True),
            Field("phone", ("telefone",)),
            Field("mobile", ("celular",)),
        ),
    ),
    Child(
        "addresses",
        ("enderecos", "enderecos_adicionais"),
        m.ErpCustomerAddress,
        "customer_id",
        (
            Field("external_id", ("id",), ident),
            Field("kind", ("tipo",)),
            Field("street", ("rua",)),
            Field("number", ("numero",)),
            Field("complement", ("complemento",)),
            Field("district", ("bairro",)),
            Field("zip_code", ("cep",)),
            Field("city", ("cidade",)),
            Field("state", ("estado",)),
        ),
    ),
)

# --- produtos ------------------------------------------------------------------


def _product_kind(row: dict) -> str:
    variations = row.get("variacoes") or row.get("variantes")
    grid = row.get("grade_cores") or row.get("grade_tamanhos")
    if variations or grid:
        return "aggregator"
    return "simple"


def _product_images(row: dict) -> list | None:
    for key in ("imagens", "imagens_hash", "hashes_imagens"):
        if key in row:
            value = row[key]
            return value if isinstance(value, list) else None
    return None


PRODUCT_FIELDS = (
    Field("code", ("codigo",), ident),
    Field("name", ("nome",), lambda v: text(v) or "Sem nome"),
    Field("unit", ("unidade",)),
    Field("category_external_id", ("categoria_id",), ident),
    Field("kind", ("variacoes", "variantes", "grade_cores", "grade_tamanhos"), _product_kind, True,
          "Heurística de agregador de grade; validar com payload real da conta."),
    Field("list_price", ("preco_tabela",), dec),
    Field("minimum_price", ("preco_minimo",), dec),
    Field("external_stock", ("saldo_estoque",), dec),
    Field("commission_percent", ("comissao",), dec),
    Field("ipi_percent", ("ipi",), dec),
    Field("ncm", ("codigo_ncm", "ncm")),
    Field("multiple", ("multiplo",), dec),
    Field("gross_weight", ("peso_bruto",), dec),
    Field("width", ("largura",), dec),
    Field("height", ("altura",), dec),
    Field("length", ("comprimento",), dec),
    Field("active", ("ativo",), boolean),
    Field("image_hashes", ("imagens", "imagens_hash", "hashes_imagens"), _product_images, True,
          "Somente hashes SHA-512; nenhum link é fabricado a partir do hash."),
    Field("notes", ("observacoes",)),
    Field("source_created_at", ("data_criacao",), instant),
)

PRODUCT_CHILDREN = (
    Child(
        "variants",
        ("variacoes", "variantes"),
        m.ErpProductVariant,
        "product_id",
        (
            Field("external_id", ("id",), ident),
            Field("code", ("codigo",), ident),
            Field("name", ("nome",)),
            Field("attributes", ("atributos", "grade"), json_value),
            Field("external_stock", ("saldo_estoque",), dec),
            Field("price", ("preco_tabela", "preco"), dec),
        ),
    ),
)


def _product_finalize(entity: Any, row: dict, _definition: ResourceDef) -> None:
    entity.sellable = entity.kind != "aggregator"


# --- pedidos -------------------------------------------------------------------


def _order_kind(row: dict) -> str:
    raw = row.get("status") if row.get("status") is not None else row.get("situacao")
    status = normalize_order_status(raw)
    if status in CANCELLED_ORDER_STATUSES:
        return "cancelled"
    if status in QUOTE_ORDER_STATUSES:
        return "quote"
    if status in VALID_SALE_STATUSES:
        return "order"
    return "unknown"


def _order_status_text(row: dict) -> str | None:
    raw = row.get("status") if row.get("status") is not None else row.get("situacao")
    return text(raw)


def _first_decimal(*keys: str) -> Callable[[dict], Decimal | None]:
    def convert(row: dict) -> Decimal | None:
        for key in keys:
            if row.get(key) not in (None, ""):
                return to_decimal(row[key])
        return None

    return convert


def _order_issue_instant(row: dict) -> datetime | None:
    for key in ("data_emissao", "data_criacao", "ultima_alteracao"):
        if row.get(key):
            return parse_source_instant(row[key])
    return None


def _address_dict(value: Any) -> dict | None:
    return value if isinstance(value, dict) else None


def _item_total(row: dict) -> Decimal | None:
    for key in ("subtotal", "total"):
        if row.get(key) not in (None, ""):
            return to_decimal(row[key])
    quantity = to_decimal(row.get("quantidade"), default=Decimal("0")) or Decimal("0")
    for key in ("preco_liquido", "preco_unitario", "preco", "preco_tabela"):
        if row.get(key) not in (None, ""):
            return quantity * (to_decimal(row[key]) or Decimal("0"))
    return None


ORDER_FIELDS = (
    Field("number", ("numero",), ident),
    Field("kind", ("status", "situacao"), _order_kind, True),
    Field("commercial_status", ("status", "situacao"), _order_status_text, True,
          "Valor bruto da origem; separado de faturamento/atendimento/pagamento."),
    Field("billing_status", ("status_faturamento", "faturamento")),
    Field("customer_external_id", ("cliente_id",), ident),
    Field("seller_external_id", ("criador_id", "usuario_id", "vendedor_id"), ident),
    Field("order_type_external_id", ("tipo_pedido_id",), ident),
    Field("payment_condition_external_id", ("condicao_pagamento_id",), ident),
    Field("price_table_external_id", ("tabela_preco_id",), ident),
    Field("carrier_external_id", ("transportadora_id",), ident),
    Field("commercial_policy_external_id", ("politica_comercial_id",), ident),
    Field("issued_at", ("data_emissao", "data_criacao", "ultima_alteracao"), _order_issue_instant, True),
    Field("issue_date", ("data_emissao",), day,
          note="Data sem hora permanece data."),
    Field("expected_delivery_date", ("data_entrega", "previsao_entrega"), day),
    Field("gross_total", ("total_bruto", "valor_bruto"), _first_decimal("total_bruto", "valor_bruto"), True),
    Field("discount_total", ("valor_desconto", "desconto_valor", "desconto"),
          _first_decimal("valor_desconto", "desconto_valor", "desconto"), True),
    Field("freight_total", ("valor_frete", "frete"), _first_decimal("valor_frete", "frete"), True),
    Field("net_total", ("total_liquido", "valor_liquido", "total"),
          _first_decimal("total_liquido", "valor_liquido", "total"), True),
    Field("notes", ("observacoes",)),
    Field("shipping_address", ("endereco_entrega",), _address_dict),
    Field("extras", ("extras",), json_value),
    Field("source_created_at", ("data_criacao",), instant),
)

ORDER_CHILDREN = (
    Child(
        "items",
        ("itens", "items"),
        m.ErpSalesOrderItem,
        "order_id",
        (
            Field("external_id", ("id", "item_id", "pedido_item_id"), ident),
            Field("product_external_id", ("produto_id",), ident),
            Field("code", ("produto_codigo", "codigo"), ident),
            Field("name", ("produto_nome", "nome", "descricao")),
            Field("quantity", ("quantidade",), lambda v: to_decimal(v, default=Decimal("0"))),
            Field("list_unit_price", ("preco_tabela",), dec),
            Field("unit_price", ("preco_liquido", "preco_unitario", "preco"), dec),
            Field("discount", ("desconto", "desconto_de_cupom"), dec),
            Field("total", ("subtotal", "total", "quantidade"), _item_total, True),
            Field("excluded", ("excluido",), lambda v: bool(boolean(v))),
            Field("notes", ("observacoes",)),
        ),
    ),
)


def _order_finalize(entity: Any, row: dict, _definition: ResourceDef) -> None:
    items = row.get("itens") if "itens" in row else row.get("items")
    if isinstance(items, list):
        live = [item for item in items if not boolean(item.get("excluido"))]
        entity.item_count = len(live)
        entity.items_complete = bool(live)


# --- catálogos auxiliares ------------------------------------------------------


def _name(value: Any) -> str:
    return text(value) or "Sem nome"


def _catalog_name(row: dict) -> str:
    return _name(row.get("nome") or row.get("descricao"))


def _policy_lookup(db: Any, connection_id: str, row: dict, key: str) -> Any:
    """Política comercial: identidade por slug + conta; o ID pode mudar."""
    from sqlalchemy import select

    entity = db.scalar(
        select(m.ErpCommercialPolicy).where(
            m.ErpCommercialPolicy.connection_id == connection_id,
            m.ErpCommercialPolicy.external_id == key,
        )
    )
    slug = text(row.get("slug"))
    if entity is None and slug:
        entity = db.scalar(
            select(m.ErpCommercialPolicy).where(
                m.ErpCommercialPolicy.connection_id == connection_id,
                m.ErpCommercialPolicy.slug == slug,
            )
        )
        if entity is not None and entity.external_id != key:
            history = list(entity.id_history or [])
            if entity.external_id not in history:
                history.append(entity.external_id)
            entity.id_history = history
            entity.external_id = key
    return entity


def _price_key(row: dict) -> str:
    product = ident(row.get("produto_id"))
    table = ident(row.get("tabela_preco_id"))
    if not product or not table:
        raise ValueError("Preço de produto sem produto_id ou tabela_preco_id")
    return f"{product}:{table}"


_CAT_NAME = Field("name", ("nome", "descricao"), _catalog_name, True)
_ACTIVE = Field("active", ("ativo",), boolean)

REGISTRY: dict[str, ResourceDef] = {
    definition.alias: definition
    for definition in (
        ResourceDef("categories", "Categorias", "categorias", "v1", m.ErpCategory,
                    (_CAT_NAME, Field("parent_external_id", ("categoria_pai_id", "pai_id"), ident),
                     Field("represented_external_id", ("representada_id",), ident,
                           note="Representada/divisão (observado em produção); atributo, não faz parte da chave."), _ACTIVE),
                    _key_id, kind="catalog"),
        ResourceDef("segments", "Segmentos", "segmentos", "v1", m.ErpSegment,
                    (_CAT_NAME, _ACTIVE), _key_id),
        ResourceDef("order-types", "Tipos de pedido", "pedidos/tipo", "v1", m.ErpOrderType,
                    (_CAT_NAME, _ACTIVE), _key_id,
                    note="`pedidos/tipo` permanece v1; não segue a regra v2 de pedidos."),
        ResourceDef("payment-conditions", "Condições de pagamento", "condicoes_pagamento", "v1",
                    m.ErpPaymentCondition,
                    (_CAT_NAME,
                     Field("minimum_order_value", ("valor_minimo",), dec),
                     Field("consider_credit_limit", ("considerar_limite_credito",), boolean),
                     Field("available_b2b", ("disponivel_b2b",), boolean),
                     Field("represented_external_id", ("representada_id",), ident,
                           note="Representada/divisão da conta (observado em produção)."),
                     _ACTIVE),
                    _key_id),
        ResourceDef("price-tables", "Tabelas de preço", "tabelas_preco", "v1", m.ErpPriceTable,
                    (_CAT_NAME,
                     Field("price_type", ("tipo",), note="Livre/acréscimo/desconto; imutável após criação."),
                     Field("percentage", ("percentual", "acrescimo_desconto"), dec),
                     Field("surcharge_percent", ("acrescimo",), dec, note="Observado em produção (10/11 linhas)."),
                     Field("discount_percent", ("desconto",), dec,
                           note="Existe no payload; nulo em todas as 11 linhas da amostra."),
                     Field("represented_external_id", ("representada_id",), ident,
                           note="Representada/divisão (observado em produção); atributo, não faz parte da chave."),
                     _ACTIVE),
                    _key_id),
        ResourceDef("carriers", "Transportadoras", "transportadoras", "v1", m.ErpCarrier,
                    (_CAT_NAME,
                     Field("document", ("cnpj", "cpf")),
                     Field("phone", ("telefone",)),
                     Field("email", ("email",)),
                     Field("city", ("cidade",)),
                     Field("state", ("estado",)),
                     _ACTIVE),
                    _key_id),
        ResourceDef("commercial-policies", "Políticas comerciais", "politicas_comerciais", "v1",
                    m.ErpCommercialPolicy,
                    (_CAT_NAME,
                     Field("slug", ("slug",)),
                     Field("valid_from", ("data_inicio", "vigencia_inicio"), day),
                     Field("valid_to", ("data_fim", "vigencia_fim"), day),
                     _ACTIVE),
                    _key_id, lookup=_policy_lookup,
                    note="Identidade por slug + conta; IDs anteriores em id_history."),
        ResourceDef("users", "Vendedores", "usuarios", "v1", m.ErpSeller,
                    (Field("name", ("nome", "email"), lambda r: _name(r.get("nome") or r.get("email")), True),
                     Field("email", ("email",)),
                     Field("phone", ("telefone",)),
                     Field("is_admin", ("administrador", "admin"), boolean),
                     Field("access_blocked", ("acesso_bloqueado",), boolean),
                     _ACTIVE),
                    _key_id, kind="seller",
                    note="Vendedor externo; não é operador de login do ERP."),
        ResourceDef("customers", "Clientes", "clientes", "v1", m.ErpCustomer,
                    CUSTOMER_FIELDS, _key_id, children=CUSTOMER_CHILDREN,
                    handled_keys=frozenset({"emails", "telefones", "tags"}),
                    kind="customer", depends_on=("segments", "users")),
        ResourceDef("products", "Produtos", "produtos", "v1", m.ErpProduct,
                    PRODUCT_FIELDS, _key_id, children=PRODUCT_CHILDREN,
                    kind="product", depends_on=("categories",), finalize=_product_finalize),
        ResourceDef("product-prices", "Preços por produto/tabela", "produtos_tabela_preco", "v1",
                    m.ErpProductPrice,
                    (Field("product_external_id", ("produto_id",), ident),
                     Field("price_table_external_id", ("tabela_preco_id",), ident),
                     Field("price", ("preco",), dec)),
                    _price_key, handled_keys=frozenset({"produto_id", "tabela_preco_id"}),
                    kind="price", depends_on=("products", "price-tables"),
                    note="Chave externa composta; exclusão é sinalizada, não aplicada como delete genérico."),
        ResourceDef("orders", "Pedidos e orçamentos", "pedidos", "v2", m.ErpSalesOrder,
                    ORDER_FIELDS, _key_id, children=ORDER_CHILDREN,
                    handled_keys=frozenset({"itens", "items", "status", "situacao"}),
                    kind="order", finalize=_order_finalize,
                    depends_on=("customers", "products", "users", "payment-conditions",
                                "price-tables", "carriers", "commercial-policies", "order-types")),
    )
}

# Ordem de dependência: auxiliares, clientes/produtos/vendedores, preços, pedidos.
SYNC_ORDER = (
    "categories",
    "segments",
    "order-types",
    "payment-conditions",
    "price-tables",
    "carriers",
    "commercial-policies",
    "users",
    "customers",
    "products",
    "product-prices",
    "orders",
)

assert set(SYNC_ORDER) == set(REGISTRY) and len(SYNC_ORDER) == 12


def render_field_mapping() -> str:
    """Gera docs/erp/field-mapping.md a partir do registro (fonte única)."""
    lines = [
        "# Mapeamento campo a campo (gerado)",
        "",
        "Gerado por `python scripts/erp_field_mapping.py`. Não edite à mão.",
        "Campos de origem sem coluna tipada ficam no snapshot restrito e no inventário",
        "de campos (`erp_field_inventory`), nunca descartados em silêncio.",
        "",
    ]
    for alias in SYNC_ORDER:
        d = REGISTRY[alias]
        lines += [
            f"## `{alias}` → `{d.upstream}` ({d.version}) → `{d.model.__tablename__}`",
            "",
            f"{d.label}. Chave externa: `id`." + (f" {d.note}" if d.note else ""),
            "",
            "| Coluna ERP | Campo(s) de origem | Tipo | Observação |",
            "|---|---|---|---|",
        ]
        for f in d.fields:
            lines.append(
                f"| `{f.attr}` | {', '.join(f'`{k}`' for k in f.keys)} | {f.conv.__name__ if f.conv.__name__ != '<lambda>' else 'derivado'} | {f.note} |"
            )
        lines.append("| `source_updated_at` | `ultima_alteracao` | instant | Versão de origem |")
        lines.append(f"| `source_deleted` | {', '.join(f'`{k}`' for k in d.delete_flag_keys)} | boolean | Exclusão externa conserva histórico |")
        for c in d.children:
            lines += [
                "",
                f"Lista filha `{', '.join(c.keys)}` → `{c.model.__tablename__}`:",
                "",
                "| Coluna ERP | Campo de origem | Tipo |",
                "|---|---|---|",
            ]
            for f in c.fields:
                lines.append(
                    f"| `{f.attr}` | {', '.join(f'`{k}`' for k in f.keys)} | {f.conv.__name__ if f.conv.__name__ != '<lambda>' else 'derivado'} |"
                )
        lines.append("")
    return "\n".join(lines) + "\n"


@dataclass
class Mapped:
    values: dict[str, Any] = field(default_factory=dict)
    children: dict[str, list[dict[str, Any]] | None] = field(default_factory=dict)


def map_row(definition: ResourceDef, row: dict) -> Mapped:
    result = Mapped(values=evaluate_fields(row, definition.fields))
    result.values["source_updated_at"] = (
        instant(row.get("ultima_alteracao")) if "ultima_alteracao" in row else None
    )
    if "ultima_alteracao" not in row:
        result.values.pop("source_updated_at")
    for flag in definition.delete_flag_keys:
        if flag in row:
            result.values["source_deleted"] = bool(boolean(row[flag]))
            break
    for child in definition.children:
        present, raw = _first_present(row, child.keys)
        if not present:
            continue  # lista ausente preserva os filhos locais
        if raw is None:
            result.children[child.name] = None
            continue
        if not isinstance(raw, list):
            raise ValueError(f"Lista `{child.keys[0]}` inválida")
        rows = []
        for position, element in enumerate(raw):
            if not isinstance(element, dict):
                raise ValueError(f"Elemento inválido em `{child.keys[0]}`")
            data = evaluate_fields(element, child.fields)
            data["position"] = position
            rows.append(data)
        result.children[child.name] = rows
    return result
