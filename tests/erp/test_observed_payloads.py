"""Formato observado em leituras reais (08/10/2026), reduzido a valores sintéticos.

Fixa o que foi visto: campos de escopo (`representada_id`) viram atributo, a chave de
identidade continua sendo só o `id`, e campos opcionais ausentes/nulos não geram erro.
"""

import asyncio

import pytest
from sqlalchemy import select

from app.erp.integrations.mercos_client import ListPage
from app.erp.models import ErpCategory, ErpPaymentCondition, ErpPriceTable, ErpSeller, ErpSyncRun
from app.erp.sync import engine

C = "observado"


class OnePage:
    def __init__(self, rows):
        self.rows = rows

    async def list_page(self, alias, cursor):
        return ListPage(alias, len(self.rows), "2026-10-08T10:00:00", None, self.rows)


def run(alias, rows):
    result = asyncio.run(engine.sync_resource(OnePage(rows), C, alias))
    assert result.status == "success", result.error
    return result


@pytest.fixture(autouse=True)
def _db(erp_session_factory):
    return erp_session_factory


def test_categories_keep_scope_as_attribute(erp_session_factory):
    run("categories", [
        {"id": 1, "nome": "Raiz", "excluido": False, "ultima_alteracao": "2026-10-08 09:00:00",
         "categoria_pai_id": None, "representada_id": 7},
        {"id": 2, "nome": "Filha", "excluido": False, "ultima_alteracao": "2026-10-08 09:00:00",
         "categoria_pai_id": 1, "representada_id": 7},
    ])
    with erp_session_factory() as db:
        rows = {r.external_id: r for r in db.scalars(select(ErpCategory))}
    assert set(rows) == {"1", "2"}  # identidade segue só o id
    assert rows["2"].parent_external_id == "1" and rows["2"].represented_external_id == "7"
    assert rows["1"].parent_external_id is None


def test_price_tables_with_null_discount_and_surcharge(erp_session_factory):
    run("price-tables", [
        {"id": 5, "nome": "T1", "tipo": "A", "acrescimo": 10.5, "desconto": None, "excluido": False,
         "ultima_alteracao": "2026-10-08 09:00:00", "representada_id": 7},
        {"id": 6, "nome": "T2", "tipo": "L", "acrescimo": None, "desconto": None, "excluido": False,
         "ultima_alteracao": "2026-10-08 09:00:00", "representada_id": 8},
    ])
    with erp_session_factory() as db:
        rows = {r.external_id: r for r in db.scalars(select(ErpPriceTable))}
    assert str(rows["5"].surcharge_percent) == "10.5000" and rows["5"].discount_percent is None
    assert rows["6"].surcharge_percent is None
    assert rows["5"].represented_external_id == "7" and rows["6"].represented_external_id == "8"


def test_payment_conditions_and_sellers_real_fields(erp_session_factory):
    run("payment-conditions", [
        {"id": 3, "nome": "30 dias", "valor_minimo": 0, "considerar_limite_credito": True,
         "disponivel_b2b": False, "representada_id": 7, "excluido": False,
         "ultima_alteracao": "2026-10-08 09:00:00"},
    ])
    run("users", [
        {"id": 9, "nome": "Vendedor Teste", "email": "v@example.test", "telefone": "000",
         "administrador": False, "acesso_bloqueado": True, "excluido": False,
         "ultima_alteracao": "2026-10-08 09:00:00"},
    ])
    with erp_session_factory() as db:
        cond = db.scalar(select(ErpPaymentCondition))
        seller = db.scalar(select(ErpSeller))
        assert db.scalar(select(ErpSyncRun)) is not None
    assert cond.consider_credit_limit is True and cond.available_b2b is False
    assert cond.represented_external_id == "7"
    assert seller.access_blocked is True and seller.is_admin is False and seller.phone == "000"


def test_customer_with_empty_list_in_scalar_fields_is_persisted_not_quarantined(erp_session_factory):
    """Observado em produção: o Mercos devolve `[]` em campos escalares sem valor
    (3500 clientes caíram em quarentena com "Valor numérico inválido: []")."""
    from app.erp.models import ErpCustomer, ErpQuarantine

    result = run("customers", [
        {"id": 77, "razao_social": "Cliente Teste", "limite_credito": [], "bloqueado": [],
         "segmento_id": [], "vendedor_id": [], "cidade": [], "emails": [], "telefones": [],
         "ultima_alteracao": "2026-10-08 09:00:00"},
    ])
    assert result.persisted == 1 and result.quarantined == 0
    with erp_session_factory() as db:
        row = db.scalar(select(ErpCustomer))
        assert db.scalar(select(ErpQuarantine)) is None
    assert row.credit_limit is None
    assert row.blocked is None  # lista vazia não vira False
    assert row.segment_external_id is None and row.city is None  # nem a string "[]"


def test_cursor_is_sent_in_the_only_format_mercos_accepts():
    from app.erp.integrations.mercos_client import format_cursor
    from app.erp.sync.engine import overlap_cursor

    assert format_cursor("2022-02-09T09:36:28") == "2022-02-09 09:36:28"
    assert format_cursor("2026-10-08 14:13:05") == "2026-10-08 14:13:05"
    assert format_cursor("2026-10-08T14:13:05.123456") == "2026-10-08 14:13:05"
    # o cursor que o próprio engine devolve após a sobreposição também é aceito
    assert format_cursor(overlap_cursor("2026-10-08 14:13:05", 60)) == "2026-10-08 14:12:05"
    assert format_cursor("não é data") == "não é data"
