"""ERP × Mercos Adaptor REAL, ponta a ponta, contra um Mercos falso local.

Sobe o Adaptor (repositório vizinho `MercosAdaptor`) como processo separado, com um
Mercos simulado por trás. Valida o contrato de verdade: chaves e escopos, envelope
de paginação, escrita com `MeusPedidosID`, 429 compartilhado entre consumidores e
descoberta de capacidades. Pulado se `ADAPTOR_PYTHON` (interpretador com as
dependências do Adaptor) não estiver definido.
"""

import asyncio
import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.erp import capabilities
from app.erp.integrations.mercos_client import AdaptorError, MercosAdaptorClient
from app.erp.models import ErpCustomer
from app.erp.sync import engine

ADAPTOR_PYTHON = os.environ.get("ADAPTOR_PYTHON", "")
ADAPTOR_DIR = Path(__file__).resolve().parents[3] / "MercosAdaptor"

pytestmark = pytest.mark.skipif(
    not (ADAPTOR_PYTHON and ADAPTOR_DIR.exists()),
    reason="defina ADAPTOR_PYTHON (interpretador do Adaptor) para o teste ponta a ponta",
)

ERP_KEY, LEGACY_KEY = "erp-key-e2e", "legacy-key-e2e"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeMercos:
    def __init__(self):
        self.requests: list[dict] = []
        self.routes: dict[tuple[str, str], object] = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode() if length else ""
                path = self.path.split("?")[0]
                fake.requests.append({
                    "method": self.command, "path": path, "query": self.path.partition("?")[2],
                    "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body,
                })
                route = fake.routes.get((self.command, path))
                status, headers, payload = route() if route else (404, {}, {"erro": "sem rota"})
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = _serve

            def log_message(self, *args):
                pass

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def calls(self, method=None, path=None):
        return [r for r in self.requests
                if (method is None or r["method"] == method) and (path is None or r["path"] == path)]


@pytest.fixture(scope="module")
def stack():
    mercos = FakeMercos()
    port = free_port()
    env = {
        **os.environ,
        "MERCOS_BASE_URL": f"http://127.0.0.1:{mercos.port}/api/v1",
        "MERCOS_APPLICATION_TOKEN": "app-token", "MERCOS_COMPANY_TOKEN": "company-token",
        "MERCOS_ADAPTOR_API_KEY": LEGACY_KEY, "MERCOS_ERP_API_KEY": ERP_KEY,
        "MERCOS_ERP_SCOPES": "read,write:customers,write:order-cancel",
        "MERCOS_PAGE_PAUSE_SECONDS": "0", "MERCOS_DEFAULT_RETRY_SECONDS": "1",
        "MERCOS_ENABLED_EXTENSIONS": "", "MERCOS_REDIS_URL": "",
    }
    proc = subprocess.Popen(
        [ADAPTOR_PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ADAPTOR_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("Adaptor não subiu")
    yield mercos, url
    proc.terminate()
    proc.wait(timeout=10)
    mercos.server.shutdown()


@pytest.fixture(autouse=True)
def _setup(erp_cfg, erp_session_factory, stack):
    erp_cfg()
    mercos, _ = stack
    mercos.requests.clear()
    mercos.routes.clear()


def erp_client(url, key=ERP_KEY):
    return MercosAdaptorClient(base_url=url, read_key=key, write_key=key)


def run(coro):
    return asyncio.run(coro)


def test_read_through_the_real_adaptor_uses_the_pagination_envelope(stack):
    mercos, url = stack
    mercos.routes[("GET", "/api/v1/clientes")] = lambda: (
        200, {}, [{"id": 1, "razao_social": "A", "ultima_alteracao": "2026-10-07T10:00:00"}])
    page = run(erp_client(url).list_page("customers", "2026-10-01T00:00:00"))
    assert [r["id"] for r in page.data] == [1]
    assert page.page_cursor == "2026-10-07T10:00:00" and page.next_cursor is None
    call = mercos.calls("GET", "/api/v1/clientes")[0]
    # formato aceito pelo Mercos: "%Y-%m-%d %H:%M:%S" (espaço codificado como +)
    assert call["query"] == "alterado_apos=2026-10-01+00%3A00%3A00"
    # tokens do Mercos vão ao Mercos; a chave interna do ERP nunca vai junto
    assert call["headers"]["applicationtoken"] == "app-token" and call["headers"]["companytoken"] == "company-token"
    assert ERP_KEY not in json.dumps(call["headers"])


def test_sync_engine_runs_against_the_real_adaptor(stack, erp_session_factory):
    mercos, url = stack
    mercos.routes[("GET", "/api/v1/clientes")] = lambda: (200, {}, [
        {"id": 7, "razao_social": "Cliente Real", "cnpj": "AB12CD34000199", "ultima_alteracao": "2026-10-07T10:00:00"}])
    result = run(engine.sync_resource(erp_client(url), "test", "customers"))
    assert result.status == "success" and result.persisted == 1
    with erp_session_factory() as db:
        row = db.scalar(select(ErpCustomer))
        assert row.external_id == "7" and row.document == "AB12CD34000199"


def test_write_with_scope_returns_created_id_and_never_retries(stack):
    mercos, url = stack
    mercos.routes[("POST", "/api/v1/clientes")] = lambda: (201, {"MeusPedidosID": "4321"}, None)
    outcome = run(erp_client(url).write("create_customer", {"razao_social": "Novo", "tipo": "F"}))
    assert outcome.kind == "success" and outcome.external_id == "4321"
    sent = mercos.calls("POST", "/api/v1/clientes")
    assert len(sent) == 1 and json.loads(sent[0]["body"])["razao_social"] == "Novo"


def test_write_without_the_scope_is_rejected_before_mercos(stack):
    mercos, url = stack
    for kind in ("create_order", "create_title"):
        outcome = run(erp_client(url).write(kind, {"cliente_id": 1}))
        assert outcome.kind == "rejected" and outcome.error_code == "forbidden", kind
    assert mercos.calls("POST") == []  # nada chegou ao Mercos


def test_legacy_key_still_reads_and_writes_but_not_the_new_routes(stack):
    mercos, url = stack
    mercos.routes[("GET", "/api/v1/clientes")] = lambda: (200, {}, [])
    mercos.routes[("POST", "/api/v1/clientes")] = lambda: (201, {"MeusPedidosID": "9"}, None)
    legacy = erp_client(url, LEGACY_KEY)
    assert run(legacy.list_page("customers", None)).data == []
    assert run(legacy.write("create_customer", {"tipo": "F"})).external_id == "9"
    r = httpx.post(f"{url}/v1/products", json={"nome": "X", "preco_tabela": 1}, headers={"X-API-Key": LEGACY_KEY})
    assert r.status_code == 403
    assert httpx.get(f"{url}/v1/customers", headers={"X-API-Key": "errada"}).status_code == 401


def test_429_blocks_every_consumer_of_the_account_until_the_deadline(stack):
    mercos, url = stack
    mercos.routes[("GET", "/api/v1/clientes")] = lambda: (
        429, {}, {"tempo_ate_permitir_novamente": 120})
    erp = erp_client(url)
    legacy = erp_client(url, LEGACY_KEY)  # "BI/agente": outro consumidor, mesma conta
    with pytest.raises(AdaptorError) as first:
        run(erp.list_page("customers", None))
    assert first.value.kind == "rate_limited" and first.value.retry_after >= 100
    hits = len(mercos.calls("GET", "/api/v1/clientes"))
    with pytest.raises(AdaptorError) as second:
        run(legacy.list_page("products", None))
    assert second.value.kind == "rate_limited"
    assert len(mercos.calls("GET")) == hits  # a segunda chamada nem chegou ao Mercos


def test_capabilities_discovery_updates_the_matrix_from_the_real_adaptor(stack, erp_session_factory):
    _, url = stack
    payload = run(erp_client(url).get_capabilities())
    assert payload["principal"] == "erp" and "write:order-cancel" in payload["scopes"]
    assert "company-token" not in json.dumps(payload) and ERP_KEY not in json.dumps(payload)
    with erp_session_factory() as db:
        result = capabilities.apply_discovery(db, "test", payload)
        db.commit()
        matrix = {i["key"]: i for i in capabilities.build_matrix(db, "test")}
    assert result["adaptor"] == "extended"
    assert matrix["read.customers"]["supportedByAdaptor"] is True
    assert matrix["write.order_cancel"]["supportedByAdaptor"] is True       # ligada no Adaptor e com escopo
    assert matrix["write.products"]["supportedByAdaptor"] is False          # sem escopo para a chave ERP
    assert "escopo" in matrix["write.products"]["reason"]
    assert matrix["read.titles"]["supportedByAdaptor"] is False             # extensão desligada no Adaptor
    # suportado pelo Adaptor mas o ERP ainda não tem a tela: mensagem honesta
    assert matrix["write.order_cancel"]["implementedInErp"] is False
    assert "falta a ligação no ERP" in matrix["write.order_cancel"]["reason"]
    # descoberta não prova a conta Mercos
    assert matrix["read.customers"]["accountAccess"] in ("unknown", "allowed")


def test_discovery_against_an_old_adaptor_without_the_route(erp_session_factory):
    transport = httpx.MockTransport(lambda r: httpx.Response(404, json={"error": "Recurso não suportado"}))
    client = MercosAdaptorClient(base_url="https://old.test", read_key="k", write_key="k", transport=transport)
    assert run(client.get_capabilities()) is None
    with erp_session_factory() as db:
        assert capabilities.apply_discovery(db, "test", None)["adaptor"] == "legacy-build"
