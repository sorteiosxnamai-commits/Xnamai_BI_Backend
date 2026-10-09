"""O caminho em lote tem que produzir exatamente o mesmo estado que o linha a linha."""

import pytest
from sqlalchemy import event, inspect, select

from app.erp.db import session_scope
from app.erp.models import (
    ErpCustomer,
    ErpOperation,
    ErpQuarantine,
    ErpSalesOrder,
    ErpSalesOrderItem,
    ErpSourceSnapshot,
)
from app.erp.registry import REGISTRY
from app.erp.sync import rows as rows_mod
from tests.erp.pg import PG_URL

IGNORED = {"id", "connection_id", "captured_at", "created_at", "updated_at", "sale_order_id", "order_id"}


def state(model, connection_id):
    with session_scope() as db:
        out = {}
        for row in db.scalars(select(model).where(model.connection_id == connection_id)):
            out[row.external_id] = {
                c.key: getattr(row, c.key) for c in inspect(model).column_attrs if c.key not in IGNORED
            }
        return out


def customer(i, **extra):
    return {"id": i, "razao_social": f"Cliente {i}", "cnpj": f"{i:014d}", "cidade": "SP",
            "emails": [{"email": f"c{i}@x.test"}], "ultima_alteracao": "2026-10-08 09:00:00", **extra}


def order(i, items=2, ts="2026-10-08 09:00:00"):
    return {"id": i, "numero": str(i), "cliente_id": 5, "status": 2, "total": "10",
            "data_emissao": "2026-10-06", "ultima_alteracao": ts,
            "itens": [{"id": n, "produto_id": n, "quantidade": 1, "preco_liquido": "5"} for n in range(1, items + 1)]}


def run(fn, connection_id, alias, pages):
    definition = REGISTRY[alias]
    totals = []
    for page in pages:
        with session_scope() as db:
            totals.append(fn(db, connection_id, definition, page, 1))
    return totals


def quarantine_state(connection_id):
    with session_scope() as db:
        return sorted(
            (q.external_key, q.resolved_at is not None)
            for q in db.scalars(select(ErpQuarantine).where(ErpQuarantine.connection_id == connection_id))
        )


@pytest.fixture(autouse=True)
def _setup(erp_cfg, erp_session_factory):
    erp_cfg()


PAGES = [
    # 1ª página: novos, repetição do mesmo id com conteúdo diferente, linha inválida e linha sem id
    [customer(1), customer(2), customer(2, cidade="RJ"), customer(3, limite_credito="abc"),
     {"razao_social": "Sem id"}, customer(4, limite_credito=[])],
    # 2ª página: inalterado, alterado, versão mais antiga (stale) e o inválido agora corrigido
    [customer(1), customer(2, cidade="BH", ultima_alteracao="2026-10-08 10:00:00"),
     customer(4, ultima_alteracao="2026-10-07 09:00:00", cidade="ANTIGA"),
     customer(3, limite_credito="12.5")],
]


def test_batch_matches_rowwise_for_customers():
    fast = run(rows_mod.persist_rows, "fast", "customers", PAGES)
    slow = run(rows_mod._persist_rowwise, "slow", "customers", PAGES)
    assert fast == slow
    assert state(ErpCustomer, "fast") == state(ErpCustomer, "slow")
    assert quarantine_state("fast") == quarantine_state("slow")
    cities = {k: v["city"] for k, v in state(ErpCustomer, "fast").items()}
    assert cities["2"] == "BH"  # a alteração mais nova vale
    assert cities["4"] == "SP"  # a versão mais antiga atrasada não regride a entidade
    with session_scope() as db:
        snap = {
            c: db.scalar(select(__import__("sqlalchemy").func.count(ErpSourceSnapshot.id)).where(
                ErpSourceSnapshot.connection_id == c)) for c in ("fast", "slow")
        }
    assert snap["fast"] == snap["slow"]


def test_batch_matches_rowwise_for_orders_with_children_and_operation_confirmation():
    pages = [[order(10), order(11, items=3), order(10, items=1)], [order(10, items=4, ts="2026-10-08 11:00:00"), order(12)]]
    for cid in ("fast", "slow"):
        with session_scope() as db:
            db.add(ErpOperation(id=f"op-{cid}", connection_id=cid, kind="create_order", status="succeeded",
                                target_resource="orders", external_id="11", idempotency_key=f"k-{cid}",
                                payload_hash="h", operator="op", payload={}))
    fast = run(rows_mod.persist_rows, "fast", "orders", pages)
    slow = run(rows_mod._persist_rowwise, "slow", "orders", pages)
    assert fast == slow
    assert state(ErpSalesOrder, "fast") == state(ErpSalesOrder, "slow")
    with session_scope() as db:
        def items(cid):
            orders = {o.id: o.external_id for o in db.scalars(select(ErpSalesOrder).where(ErpSalesOrder.connection_id == cid))}
            return sorted(
                (orders[i.order_id], i.position)
                for i in db.scalars(select(ErpSalesOrderItem))
                if i.order_id in orders
            )
        assert items("fast") == items("slow")
        confirmed = {
            c: db.scalar(select(ErpOperation.mirror_confirmed_at).where(ErpOperation.connection_id == c))
            for c in ("fast", "slow")
        }
    assert (confirmed["fast"] is not None) and (confirmed["slow"] is not None)


def test_batch_failure_falls_back_to_rowwise_with_same_result(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("falha simulada no lote")

    monkeypatch.setattr(rows_mod, "_persist_batch", boom)
    result = run(rows_mod.persist_rows, "fallback", "customers", PAGES)
    expected = run(rows_mod._persist_rowwise, "expected", "customers", PAGES)
    assert result == expected
    assert state(ErpCustomer, "fallback") == state(ErpCustomer, "expected")


def test_batch_uses_far_fewer_statements(erp_session_factory):
    bind = erp_session_factory.kw["bind"]
    counts = {"fast": 0, "slow": 0}
    current = {"name": "fast"}

    def count(conn, cursor, statement, *args):
        counts[current["name"]] += 1

    event.listen(bind, "before_cursor_execute", count)
    page = [customer(i) for i in range(1, 101)]
    run(rows_mod.persist_rows, "fast", "customers", [page])
    current["name"] = "slow"
    run(rows_mod._persist_rowwise, "slow", "customers", [page])
    event.remove(bind, "before_cursor_execute", count)
    assert counts["slow"] > 500, counts
    if PG_URL:  # no PostgreSQL as inserções saem agrupadas: custo por página, não por linha
        assert counts["fast"] < 40, counts
    else:  # SQLite não agrupa INSERT, mas ainda economiza savepoints/consultas por linha
        assert counts["fast"] < counts["slow"] / 3, counts
