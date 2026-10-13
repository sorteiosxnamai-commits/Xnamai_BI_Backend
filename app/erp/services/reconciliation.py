"""Conciliação somente leitura entre o espelho do ERP e as tabelas do BI.

Compara pelo ID Mercos, no mesmo escopo (período e filtros) e no MESMO instante de atualização
(`asOf`: o menor entre os cursores confirmados dos dois lados, ou o informado). Não escreve em
nenhum dos lados, não chama o Mercos/Adaptor e não tenta igualar contagens: diferença de filtro ou de
cobertura da carga é classificada como *explicada*, o resto como *inexplicada* e investigado.

O BI nunca sobrescreve o espelho do ERP: este módulo só relata."""

from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, Numeric, column, func, select, table
from sqlalchemy.orm import Session

from app.domain.order_status import is_cancelled_order, normalize_order_status
from app.erp import serializers as ser
from app.erp.common import BR_TZ, as_utc, parse_source_instant
from app.erp.models import commercial as m
from app.erp.models.core import ErpSyncCheckpoint

RESOURCES = ("orders", "customers", "products")

# Tabelas do BI como construções SOMENTE LEITURA (sem importar os modelos legados: o contrato de isolamento
# do ERP proíbe esse import). Nenhuma escrita é emitida contra elas.
_TS = DateTime(timezone=True)
BI_CUSTOMERS = table(
    "customers", column("mercos_id"), column("name"), column("document"), column("active", Boolean),
    column("source_updated_at", _TS),
)
BI_PRODUCTS = table(
    "products", column("mercos_id"), column("code"), column("name"), column("active", Boolean),
    column("list_price", Numeric), column("source_updated_at", _TS),
)
BI_ORDERS = table(
    "orders", column("mercos_id"), column("number"), column("status"), column("net_total", Numeric),
    column("total", Numeric), column("gross_total", Numeric), column("discount_value", Numeric),
    column("discount", Numeric), column("item_count", Integer), column("customer_mercos_id"),
    column("seller_mercos_id"), column("issued_at", _TS), column("source_updated_at", _TS),
)
BI_ITEMS = table("order_items", column("order_mercos_id"), column("total", Numeric), column("excluded", Boolean))
BI_SYNC = table("sync_states", column("resource"), column("cursor"), column("status"), column("last_success_at", _TS))
TOLERANCE = Decimal("0.01")

CATALOG = {
    "only_in_erp": (
        "Registro existe no ERP e não no BI",
        "O BI usa outra janela/carga ou ainda não importou",
        "Nenhum no ERP; o BI tem de alcançar o mesmo instante. Não copiar do ERP para o BI.",
    ),
    "only_in_bi": (
        "Registro existe no BI e não no ERP",
        "Pedido/cadastro aparece no BI e falta no ERP: ERP incompleto até a carga terminar",
        "Concluir a carga do ERP (retoma do checkpoint). Não copiar do BI para o ERP.",
    ),
    "kind_mismatch": (
        "Tipo (pedido/orçamento/cancelado) diferente",
        "Cancelamentos e vendas somam diferente entre as telas",
        "Conferir a versão mais recente no Mercos; o lado com `source_updated_at` menor está desatualizado.",
    ),
    "net_total_mismatch": (
        "Total líquido diferente", "Valor vendido diverge entre BI e ERP",
        "Conferir a versão mais recente; se a versão for a mesma, investigar o mapeamento do campo.",
    ),
    "gross_total_mismatch": (
        "Total bruto diferente", "Desconto/margem calculados diferem",
        "Conferir a versão mais recente e o mapeamento de `total_bruto`.",
    ),
    "discount_mismatch": (
        "Desconto diferente", "Desconto total e percentuais divergem",
        "Conferir a versão mais recente e o mapeamento de desconto.",
    ),
    "item_count_mismatch": (
        "Quantidade de itens diferente", "Análises por item divergem",
        "Conferir a versão mais recente; itens excluídos contam à parte.",
    ),
    "items_total_mismatch": (
        "Soma dos itens diferente", "Análises por produto divergem",
        "Conferir a versão mais recente e a regra de itens excluídos.",
    ),
    "customer_mismatch": (
        "Cliente do pedido diferente", "Pedido atribuído a clientes diferentes",
        "Conferir a versão mais recente no Mercos.",
    ),
    "seller_mismatch": (
        "Vendedor do pedido diferente", "Responsável diferente entre telas",
        "Conferir a versão mais recente no Mercos.",
    ),
    "name_mismatch": (
        "Nome diferente", "Cadastro exibido com nomes diferentes",
        "Conferir a versão mais recente no Mercos.",
    ),
    "code_mismatch": (
        "Código diferente", "Produto identificado por códigos diferentes",
        "Conferir a versão mais recente no Mercos.",
    ),
    "active_mismatch": (
        "Situação ativa/inativa diferente", "Cadastro ativo num lado e inativo no outro",
        "Conferir a versão mais recente no Mercos.",
    ),
    "price_mismatch": (
        "Preço de lista diferente", "Preço de tabela divergente", "Conferir a versão mais recente no Mercos.",
    ),
    "document_mismatch": (
        "Documento (CPF/CNPJ) diferente", "Identificação fiscal divergente (valores não são exibidos)",
        "Conferir no Mercos por quem tem permissão de documentos.",
    ),
    "customer_link_gap": (
        "Cliente do pedido existe no BI e falta no ERP",
        "Pedido sem nome de cliente no ERP apesar de o cadastro existir no BI",
        "Concluir/reconciliar a carga de Clientes do ERP; o BI não preenche o espelho.",
    ),
}


def _num(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _same(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return True  # lado sem valor não prova divergência
    return abs(a - b) <= TOLERANCE


def _kind_of_bi(status) -> str:
    if is_cancelled_order(status):
        return "cancelled"
    normalized = normalize_order_status(status)
    if normalized in ("1", "orcamento", "orçamento", "budget", "quote"):
        return "quote"
    if normalized in ("2", "pedido", "order"):
        return "order"
    return "unclassified"


def _bounds(start: date | None, end: date | None):
    low = datetime.combine(start, time.min, tzinfo=BR_TZ).astimezone(timezone.utc) if start else None
    high = (
        datetime.combine(end, time.max, tzinfo=BR_TZ).astimezone(timezone.utc) if end else None
    )
    return low, high


def _cursor_instant(text: str | None) -> datetime | None:
    return parse_source_instant(text) if text else None


class _Bucket:
    def __init__(self, code: str):
        self.code = code
        self.count = 0
        self.explained = 0
        self.samples: list[dict] = []

    def add(self, sample: dict, explained: bool, limit: int, why: str | None = None):
        self.count += 1
        if explained:
            self.explained += 1
        if len(self.samples) < limit:
            self.samples.append({**sample, "explained": explained, **({"why": why} if why else {})})

    def as_dict(self) -> dict:
        title, impact, fix = CATALOG[self.code]
        return {
            "code": self.code,
            "title": title,
            "cause": None,
            "impact": impact,
            "fix": fix,
            "count": self.count,
            "explained": self.explained,
            "unexplained": self.count - self.explained,
            "samples": self.samples,
        }


def _erp_cursor(db: Session, connection_id: str, resource: str):
    cp = db.scalar(
        select(ErpSyncCheckpoint).where(
            ErpSyncCheckpoint.connection_id == connection_id, ErpSyncCheckpoint.resource == resource
        )
    )
    return cp, _cursor_instant(cp.cursor) if cp else None


def _bi_cursor(db: Session, resource: str):
    state = db.execute(
        select(BI_SYNC.c.cursor, BI_SYNC.c.status, BI_SYNC.c.last_success_at).where(BI_SYNC.c.resource == resource)
    ).first()
    return state, _cursor_instant(state.cursor) if state else None


def _as_of(requested: datetime | None, erp_at, bi_at) -> tuple[datetime | None, str]:
    if requested is not None:
        return requested, "informado"
    known = [t for t in (erp_at, bi_at) if t is not None]
    if len(known) == 2:
        return min(known), "menor cursor confirmado entre ERP e BI"
    return None, "sem cursor confirmado nos dois lados: todo o histórico foi comparado"


def reconcile(
    db: Session,
    connection_id: str,
    resource: str,
    *,
    user,
    date_from: date | None = None,
    date_to: date | None = None,
    as_of: datetime | None = None,
    sample: int = 10,
) -> dict:
    if resource not in RESOURCES:
        raise ValueError(resource)
    cp, erp_at = _erp_cursor(db, connection_id, resource)
    state, bi_at = _bi_cursor(db, resource)
    instant, basis = _as_of(as_of, erp_at, bi_at)
    if resource == "orders":
        buckets, counts = _orders(db, connection_id, date_from, date_to, instant, erp_at, bi_at, sample)
    elif resource == "customers":
        buckets, counts = _customers(db, connection_id, instant, erp_at, bi_at, sample, user)
    else:
        buckets, counts = _products(db, connection_id, instant, erp_at, bi_at, sample, user)
    divergences = [b.as_dict() for b in buckets.values() if b.count]
    for item in divergences:
        item["cause"] = CATALOG[item["code"]][0]
    return {
        "resource": resource,
        "readOnly": True,
        "scope": {
            "dateFrom": date_from.isoformat() if date_from else None,
            "dateTo": date_to.isoformat() if date_to else None,
            "filters": "mesmo período e mesmo instante de atualização nos dois lados",
        },
        "asOf": instant.isoformat() if instant else None,
        "asOfBasis": basis,
        "erp": {
            "count": counts["erp"],
            "checkpoint": {
                "status": cp.status if cp else "never",
                "cursor": cp.cursor if cp else None,
                "dataThrough": ser.iso(cp.data_through) if cp else None,
                "lastSuccessAt": ser.iso(cp.last_success_at) if cp else None,
            },
        },
        "bi": {
            "count": counts["bi"],
            "syncState": {
                "status": state.status if state else "never",
                "cursor": state.cursor if state else None,
                "lastSuccessAt": ser.iso(state.last_success_at) if state else None,
            },
        },
        "matched": counts["matched"],
        "totals": {
            "divergences": sum(d["count"] for d in divergences),
            "explained": sum(d["explained"] for d in divergences),
            "unexplained": sum(d["unexplained"] for d in divergences),
        },
        "divergences": sorted(divergences, key=lambda d: (-d["unexplained"], -d["count"])),
        "notes": [
            "Contagens diferentes não são tratadas como erro quando o filtro ou a cobertura da carga explicam.",
            "Nenhum dos lados foi alterado; o BI não sobrescreve o espelho do ERP.",
            *counts.get("notes", []),
        ],
    }


# ------------------------------------------------------------------------- pedidos


def _orders(db, connection_id, date_from, date_to, as_of, erp_at, bi_at, sample):
    E, B = m.ErpSalesOrder, BI_ORDERS
    low, high = _bounds(date_from, date_to)
    erp_q = select(
        E.external_id, E.number, E.kind, E.net_total, E.gross_total, E.discount_total, E.item_count,
        E.items_complete, E.customer_external_id, E.seller_external_id, E.source_updated_at, E.id,
    ).where(E.connection_id == connection_id, E.source_deleted.is_(False))
    bi_q = select(
        B.c.mercos_id, B.c.number, B.c.status, B.c.net_total, B.c.total, B.c.gross_total, B.c.discount_value, B.c.discount,
        B.c.item_count, B.c.customer_mercos_id, B.c.seller_mercos_id, B.c.source_updated_at,
    )
    if date_from:
        erp_q = erp_q.where(E.issue_date >= date_from)
    if date_to:
        erp_q = erp_q.where(E.issue_date <= date_to)
    if low:
        bi_q = bi_q.where(B.c.issued_at >= low)
    if high:
        bi_q = bi_q.where(B.c.issued_at <= high)
    if as_of:
        erp_q = erp_q.where(E.source_updated_at <= as_of)
        bi_q = bi_q.where(B.c.source_updated_at <= as_of)
    erp = {r[0]: r for r in db.execute(erp_q)}
    biw = {r[0]: r for r in db.execute(bi_q)}
    matched_ids = erp.keys() & biw.keys()

    # itens (soma e contagem por pedido, sem excluídos) só dos pedidos casados
    erp_items = {
        key: (int(count), _num(total) or Decimal("0"))
        for key, count, total in db.execute(
            select(E.external_id, func.count(m.ErpSalesOrderItem.id), func.coalesce(func.sum(m.ErpSalesOrderItem.total), 0))
            .join(E, E.id == m.ErpSalesOrderItem.order_id)
            .where(E.connection_id == connection_id, m.ErpSalesOrderItem.excluded.is_(False))
            .group_by(E.external_id)
        )
    }
    bi_items = {
        key: (int(count), _num(total) or Decimal("0"))
        for key, count, total in db.execute(
            select(BI_ITEMS.c.order_mercos_id, func.count(), func.coalesce(func.sum(BI_ITEMS.c.total), 0))
            .where(BI_ITEMS.c.excluded.is_(False))
            .group_by(BI_ITEMS.c.order_mercos_id)
        )
    }
    erp_customers = set(db.scalars(select(m.ErpCustomer.external_id).where(m.ErpCustomer.connection_id == connection_id)))
    bi_customers = set(db.scalars(select(BI_CUSTOMERS.c.mercos_id)))

    buckets = {code: _Bucket(code) for code in CATALOG}
    notes: list[str] = []

    def iso(value):
        return value.isoformat() if value else None

    for key in erp.keys() - biw.keys():
        row = erp[key]
        lagging = bi_at is None or (row[10] is not None and as_utc(row[10]) > bi_at)
        buckets["only_in_erp"].add(
            {"id": key, "number": row[1], "erpUpdatedAt": iso(row[10])}, lagging, sample,
            "o BI ainda não alcançou este instante" if lagging else None,
        )
    for key in biw.keys() - erp.keys():
        row = biw[key]
        lagging = erp_at is None or (row[11] is not None and as_utc(row[11]) > erp_at)
        buckets["only_in_bi"].add(
            {"id": key, "number": row[1], "biUpdatedAt": iso(row[11])}, lagging, sample,
            "a carga do ERP ainda não alcançou este instante" if lagging else None,
        )
    erp_incomplete = 0
    for key in matched_ids:
        e, b = erp[key], biw[key]
        e_when, b_when = e[10], biw[key][11]
        differing_versions = (e_when is None) != (b_when is None) or (
            e_when is not None and b_when is not None and as_utc(e_when) != as_utc(b_when)
        )
        why = "versões diferentes (um lado está desatualizado)" if differing_versions else None

        def flag(code, erp_value, bi_value, _key=key, _diff=differing_versions, _why=why):
            buckets[code].add({"id": _key, "erp": erp_value, "bi": bi_value}, _diff, sample, _why)

        e_kind, b_kind = e[2], _kind_of_bi(b[2])
        if e_kind != b_kind:
            flag("kind_mismatch", e_kind, b_kind)
        incomplete = not e[7]
        erp_total_unknown = incomplete and not (e[3] or 0)
        if incomplete:
            erp_incomplete += 1
        b_net = _num(b[3] if b[3] is not None else b[4])
        if not erp_total_unknown and not _same(_num(e[3]), b_net):
            flag("net_total_mismatch", str(e[3]), str(b_net))
        if not erp_total_unknown and not _same(_num(e[4]), _num(b[5])):
            flag("gross_total_mismatch", str(e[4]), str(b[5]))
        b_disc = _num(b[6] if b[6] is not None else b[7])
        if not erp_total_unknown and not _same(_num(e[5]), b_disc):
            flag("discount_mismatch", str(e[5]), str(b_disc))
        if (e[8] or None) != (b[9] or None):
            flag("customer_mismatch", e[8], b[9])
        if (e[9] or None) != (b[10] or None):
            flag("seller_mismatch", e[9], b[10])
        if not incomplete:
            e_items, b_items = erp_items.get(key, (0, Decimal("0"))), bi_items.get(key)
            if b_items is not None:
                if e_items[0] != b_items[0]:
                    flag("item_count_mismatch", e_items[0], b_items[0])
                if not _same(e_items[1], b_items[1]):
                    flag("items_total_mismatch", str(e_items[1]), str(b_items[1]))
        if e[8] and e[8] not in erp_customers and e[8] in bi_customers:
            buckets["customer_link_gap"].add(
                {"id": key, "customerId": e[8]},
                False, sample,
            )
    if erp_incomplete:
        notes.append(
            f"{erp_incomplete} pedido(s) do ERP estão sem itens (hidratação pendente): totais e itens deles "
            "não foram comparados com o BI."
        )
    return buckets, {"erp": len(erp), "bi": len(biw), "matched": len(matched_ids), "notes": notes}


# ------------------------------------------------------------------------- clientes e produtos


def _norm(text) -> str:
    return " ".join(str(text or "").casefold().split())


def _digits(text) -> str:
    return "".join(ch for ch in str(text or "") if ch.isalnum()).casefold()


def _customers(db, connection_id, as_of, erp_at, bi_at, sample, user):
    E, B = m.ErpCustomer, BI_CUSTOMERS
    erp_q = select(E.external_id, E.name, E.document, E.active, E.source_updated_at).where(
        E.connection_id == connection_id, E.source_deleted.is_(False)
    )
    bi_q = select(B.c.mercos_id, B.c.name, B.c.document, B.c.active, B.c.source_updated_at)
    if as_of:
        erp_q = erp_q.where(E.source_updated_at <= as_of)
        bi_q = bi_q.where(B.c.source_updated_at <= as_of)
    erp = {r[0]: r for r in db.execute(erp_q)}
    biw = {r[0]: r for r in db.execute(bi_q)}
    buckets = {code: _Bucket(code) for code in CATALOG}
    for key in erp.keys() - biw.keys():
        r = erp[key]
        lagging = bi_at is None or (r[4] is not None and as_utc(r[4]) > bi_at)
        buckets["only_in_erp"].add({"id": key, "name": ser.label(r[1], user)}, lagging, sample)
    for key in biw.keys() - erp.keys():
        r = biw[key]
        lagging = erp_at is None or (r[4] is not None and as_utc(r[4]) > erp_at)
        buckets["only_in_bi"].add({"id": key, "name": ser.label(r[1], user)}, lagging, sample)
    for key in erp.keys() & biw.keys():
        e, b = erp[key], biw[key]
        diff_versions = (e[4] is None) != (b[4] is None) or (e[4] is not None and as_utc(e[4]) != as_utc(b[4]))
        if _norm(e[1]) != _norm(b[1]):
            buckets["name_mismatch"].add(
                {"id": key, "erp": ser.label(e[1], user), "bi": ser.label(b[1], user)}, diff_versions, sample
            )
        if e[3] is not None and b[3] is not None and bool(e[3]) != bool(b[3]):
            buckets["active_mismatch"].add({"id": key, "erp": e[3], "bi": b[3]}, diff_versions, sample)
        if e[2] and b[2] and _digits(e[2]) != _digits(b[2]):
            buckets["document_mismatch"].add({"id": key}, diff_versions, sample)  # sem valores
    return buckets, {"erp": len(erp), "bi": len(biw), "matched": len(erp.keys() & biw.keys())}


def _products(db, connection_id, as_of, erp_at, bi_at, sample, user):
    E, B = m.ErpProduct, BI_PRODUCTS
    erp_q = select(E.external_id, E.code, E.name, E.active, E.list_price, E.source_updated_at).where(
        E.connection_id == connection_id, E.source_deleted.is_(False)
    )
    bi_q = select(B.c.mercos_id, B.c.code, B.c.name, B.c.active, B.c.list_price, B.c.source_updated_at)
    if as_of:
        erp_q = erp_q.where(E.source_updated_at <= as_of)
        bi_q = bi_q.where(B.c.source_updated_at <= as_of)
    erp = {r[0]: r for r in db.execute(erp_q)}
    biw = {r[0]: r for r in db.execute(bi_q)}
    buckets = {code: _Bucket(code) for code in CATALOG}
    for key in erp.keys() - biw.keys():
        r = erp[key]
        lagging = bi_at is None or (r[5] is not None and as_utc(r[5]) > bi_at)
        buckets["only_in_erp"].add({"id": key, "name": r[2]}, lagging, sample)
    for key in biw.keys() - erp.keys():
        r = biw[key]
        lagging = erp_at is None or (r[5] is not None and as_utc(r[5]) > erp_at)
        buckets["only_in_bi"].add({"id": key, "name": r[2]}, lagging, sample)
    for key in erp.keys() & biw.keys():
        e, b = erp[key], biw[key]
        diff_versions = (e[5] is None) != (b[5] is None) or (e[5] is not None and as_utc(e[5]) != as_utc(b[5]))
        if _norm(e[1]) != _norm(b[1]):
            buckets["code_mismatch"].add({"id": key, "erp": e[1], "bi": b[1]}, diff_versions, sample)
        if _norm(e[2]) != _norm(b[2]):
            buckets["name_mismatch"].add({"id": key, "erp": e[2], "bi": b[2]}, diff_versions, sample)
        if e[3] is not None and b[3] is not None and bool(e[3]) != bool(b[3]):
            buckets["active_mismatch"].add({"id": key, "erp": e[3], "bi": b[3]}, diff_versions, sample)
        if not _same(_num(e[4]), _num(b[4])):
            buckets["price_mismatch"].add({"id": key, "erp": str(e[4]), "bi": str(b[4])}, diff_versions, sample)
    return buckets, {"erp": len(erp), "bi": len(biw), "matched": len(erp.keys() & biw.keys())}
