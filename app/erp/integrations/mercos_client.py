"""Cliente do Mercos Adaptor para o ERP.

Regras:
- uma tentativa por chamada; retry é decisão do worker, nunca do cliente;
- escrita nunca é repetida automaticamente (timeout pode esconder sucesso);
- o resultado de escrita é classificado: success / rejected / rate_limited /
  unknown. `unknown` jamais volta sozinho para a fila.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.erp.config import erp_settings

READ_ALIASES = (
    "categories",
    "segments",
    "order-types",
    "payment-conditions",
    "price-tables",
    "carriers",
    "commercial-policies",
    "customers",
    "products",
    "product-prices",
    "users",
    "orders",
)

# Rotas de escrita existentes no Adaptor analisado. Nada além disto é chamado.
WRITE_ROUTES = {
    "create_customer": ("POST", "/v1/customers"),
    "update_customer": ("PUT", "/v1/customers/{id}"),
    "create_order": ("POST", "/v1/orders"),
    "update_order": ("PUT", "/v1/orders/{id}"),
    "cancel_order": ("POST", "/v1/orders/{id}/cancel"),
    "bill_order": ("POST", "/v1/billings"),
    "create_title": ("POST", "/v1/titles"),
    "update_title": ("PUT", "/v1/titles/{id}"),
}

DEFAULT_RATE_LIMIT_WAIT = 30.0
MAX_RATE_LIMIT_WAIT = 600.0


def format_cursor(cursor: str) -> str:
    """O Mercos só aceita `%Y-%m-%d %H:%M:%S` em `alterado_apos` (422 caso contrário).
    Mantém o horário de relógio do cursor, sem fuso nem fração de segundo."""
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(str(cursor).strip().replace(" ", "T", 1))
    except ValueError:
        return str(cursor)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


class AdaptorError(Exception):
    def __init__(
        self,
        kind: str,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.retry_after = retry_after
        self.message = message


@dataclass
class ListPage:
    resource: str
    count: int
    page_cursor: str | None
    next_cursor: str | None
    data: list[dict]


@dataclass
class WriteOutcome:
    kind: str  # success | rejected | rate_limited | unknown
    status_code: int | None = None
    body: Any = None
    external_id: str | None = None
    error_code: str | None = None
    error: str | None = None
    retry_after: float | None = None
    extra: dict = field(default_factory=dict)


def retry_after_seconds(response: httpx.Response) -> float:
    header = response.headers.get("Retry-After")
    if header:
        try:
            return min(max(float(header), 1.0), MAX_RATE_LIMIT_WAIT)
        except ValueError:
            try:
                when = parsedate_to_datetime(header)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                delta = (when - datetime.now(timezone.utc)).total_seconds()
                return min(max(delta, 1.0), MAX_RATE_LIMIT_WAIT)
            except (TypeError, ValueError, OverflowError):
                pass
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        raw = payload.get("tempo_ate_permitir_novamente")
        if raw is None and isinstance(payload.get("details"), dict):
            raw = payload["details"].get("tempo_ate_permitir_novamente")
        try:
            if raw is not None and float(raw) > 0:
                return min(float(raw) + 0.5, MAX_RATE_LIMIT_WAIT)
        except (TypeError, ValueError):
            pass
    return DEFAULT_RATE_LIMIT_WAIT


def _detail(response: httpx.Response) -> str:
    text = response.text[:400].strip()
    if "text/html" in response.headers.get("content-type", "").lower():
        return "resposta HTML do provedor"
    return text or response.reason_phrase


def extract_external_id(body: Any) -> str | None:
    if isinstance(body, dict):
        for key in ("MeusPedidosID", "id", "ID"):
            value = body.get(key)
            if value not in (None, ""):
                return str(value)
    return None


class MercosAdaptorClient:
    """Chamadas ao Adaptor. Tokens Mercos nunca passam por aqui."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        read_key: str | None = None,
        write_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = None,
    ):
        cfg = erp_settings()
        self.base_url = (base_url if base_url is not None else cfg.adaptor_url).rstrip("/")
        self.read_key = read_key if read_key is not None else cfg.read_key
        self.write_key = write_key if write_key is not None else cfg.erp_adaptor_api_key
        self._transport = transport
        self._timeout = timeout or cfg.erp_adaptor_timeout_seconds
        # Concorrência local limitada; a cota global é do gateway.
        self._gate = asyncio.Semaphore(1)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.read_key)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(self._timeout, connect=15.0),
        )

    async def list_page(self, alias: str, cursor: str | None) -> ListPage:
        if alias not in READ_ALIASES:
            raise AdaptorError("invalid", f"Recurso não suportado pelo ERP: {alias}")
        if not self.configured:
            raise AdaptorError("unavailable", "Adaptor não configurado para o ERP")
        params = {"alterado_apos": format_cursor(cursor)} if cursor else {}
        try:
            async with self._gate, self._client() as client:
                response = await client.get(
                    f"{self.base_url}/v1/{alias}",
                    params=params,
                    headers={"X-API-Key": self.read_key},
                )
        except httpx.TimeoutException as exc:
            raise AdaptorError("transport", f"Tempo esgotado no Adaptor: {type(exc).__name__}") from exc
        except httpx.RequestError as exc:
            raise AdaptorError("transport", f"Adaptor inacessível: {type(exc).__name__}") from exc
        if response.status_code == 429:
            raise AdaptorError(
                "rate_limited",
                "Mercos limitou as requisições",
                status_code=429,
                retry_after=retry_after_seconds(response),
            )
        if response.status_code == 401:
            # Falha de autenticação do ERP no Adaptor: NÃO é restrição do recurso.
            raise AdaptorError(
                "unauthorized",
                f"Autenticação recusada pelo Adaptor (401) em {alias}",
                status_code=401,
            )
        if response.status_code == 403:
            raise AdaptorError(
                "forbidden",
                f"Acesso negado (403) em {alias}: {_detail(response)}",
                status_code=403,
            )
        if response.status_code == 404:
            raise AdaptorError("not_found", f"Recurso {alias} indisponível", status_code=404)
        if response.status_code in (400, 422):
            # Requisição recusada por validação (ex.: formato de data): falha explícita,
            # não "indisponível" nem tentativa cega de repetir.
            raise AdaptorError(
                "invalid",
                f"Adaptor {response.status_code} em {alias}: {_detail(response)}",
                status_code=response.status_code,
            )
        if response.is_error:
            raise AdaptorError(
                "unavailable",
                f"Adaptor {response.status_code} em {alias}: {_detail(response)}",
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise AdaptorError("invalid", f"Resposta inválida do Adaptor em {alias}") from exc
        if not isinstance(body, dict) or not isinstance(body.get("data", []), list):
            raise AdaptorError("invalid", f"Envelope inválido do Adaptor em {alias}")
        data = body.get("data") or []
        return ListPage(
            resource=str(body.get("resource") or alias),
            count=int(body.get("count") or len(data)),
            page_cursor=body.get("pageCursor"),
            next_cursor=body.get("nextCursor"),
            data=[row for row in data if isinstance(row, dict)],
        )

    async def get_detail(self, alias: str, external_id: str) -> dict:
        if alias not in ("customers", "products", "orders"):
            raise AdaptorError("invalid", f"Detalhe não disponível para {alias}")
        if not self.configured:
            raise AdaptorError("unavailable", "Adaptor não configurado para o ERP")
        try:
            async with self._gate, self._client() as client:
                response = await client.get(
                    f"{self.base_url}/v1/{alias}/{quote(str(external_id), safe='')}",
                    headers={"X-API-Key": self.read_key},
                )
        except httpx.RequestError as exc:
            raise AdaptorError("transport", f"Adaptor inacessível: {type(exc).__name__}") from exc
        if response.status_code == 429:
            raise AdaptorError(
                "rate_limited", "Mercos limitou as requisições", status_code=429,
                retry_after=retry_after_seconds(response),
            )
        if response.status_code == 401:
            raise AdaptorError(
                "unauthorized", "Autenticação recusada pelo Adaptor (401)", status_code=401
            )
        if response.status_code == 403:
            raise AdaptorError(
                "forbidden", "Detalhe por ID indisponível na conta (403)", status_code=403
            )
        if response.is_error:
            raise AdaptorError(
                "unavailable", f"Adaptor {response.status_code}: {_detail(response)}",
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise AdaptorError("invalid", "Resposta de detalhe inválida") from exc
        if not isinstance(body, dict):
            raise AdaptorError("invalid", "Detalhe retornou formato inválido")
        return body

    async def get_capabilities(self) -> dict | None:
        """O que o Adaptor oferece (aditivo). None = build antigo, sem a rota."""
        if not self.configured:
            raise AdaptorError("unavailable", "Adaptor não configurado para o ERP")
        try:
            async with self._gate, self._client() as client:
                response = await client.get(
                    f"{self.base_url}/v1/capabilities",
                    headers={"X-API-Key": self.write_key or self.read_key},
                )
        except httpx.RequestError as exc:
            raise AdaptorError("transport", f"Adaptor inacessível: {type(exc).__name__}") from exc
        if response.status_code == 404:
            return None
        if response.status_code in (401, 403):
            raise AdaptorError("forbidden", f"Acesso negado ao Adaptor ({response.status_code})",
                               status_code=response.status_code)
        if response.is_error:
            raise AdaptorError("unavailable", f"Adaptor {response.status_code}", status_code=response.status_code)
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("capabilities"), list):
            raise AdaptorError("invalid", "Resposta de capacidades inválida")
        return body

    async def list_resources(self) -> list[str]:
        """Aliases que o build do Adaptor declara (não prova acesso da conta)."""
        if not self.configured:
            raise AdaptorError("unavailable", "Adaptor não configurado para o ERP")
        try:
            async with self._gate, self._client() as client:
                response = await client.get(
                    f"{self.base_url}/v1/resources",
                    headers={"X-API-Key": self.read_key},
                )
        except httpx.RequestError as exc:
            raise AdaptorError("transport", f"Adaptor inacessível: {type(exc).__name__}") from exc
        if response.is_error:
            raise AdaptorError("unavailable", f"Adaptor {response.status_code}", status_code=response.status_code)
        body = response.json()
        items = body.get("resources", body) if isinstance(body, dict) else body
        return [str(item.get("alias", item)) if isinstance(item, dict) else str(item) for item in items]

    async def write(
        self, kind: str, payload: dict, external_id: str | None = None
    ) -> WriteOutcome:
        """Uma única tentativa. Nunca repete, nem em timeout."""
        route = WRITE_ROUTES.get(kind)
        if route is None:
            return WriteOutcome("rejected", error_code="unsupported_operation",
                                error=f"Operação sem rota no Adaptor: {kind}")
        method, template = route
        if "{id}" in template:
            if not external_id:
                return WriteOutcome("rejected", error_code="missing_id",
                                    error="ID externo obrigatório para atualização")
            template = template.replace("{id}", quote(str(external_id), safe=""))
        if not self.base_url or not self.write_key:
            return WriteOutcome("rejected", error_code="write_key_missing",
                                error="Chave ERP de escrita do Adaptor não configurada")
        try:
            async with self._gate, self._client() as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{template}",
                    json=payload,
                    headers={"X-API-Key": self.write_key},
                )
        except httpx.RequestError as exc:
            # Timeout ou falha após o envio: o Mercos pode ter aceitado.
            return WriteOutcome(
                "unknown",
                error_code="transport",
                error=f"Resultado desconhecido: {type(exc).__name__}",
            )
        status = response.status_code
        if status == 429:
            return WriteOutcome(
                "rate_limited",
                status_code=429,
                error_code="rate_limited",
                error="Mercos limitou as requisições",
                retry_after=retry_after_seconds(response),
            )
        if 200 <= status < 300:
            try:
                body = response.json() if response.content else None
            except ValueError:
                return WriteOutcome("unknown", status_code=status, error_code="invalid_response",
                                    error="Resposta de sucesso ilegível")
            external = extract_external_id(body)
            if external is None and kind == "cancel_order" and external_id:
                # O cancelamento não devolve ID; o alvo já é conhecido. 2xx = ACEITO, nunca confirmado:
                # a confirmação vem do espelho (pedido volta cancelado).
                external = str(external_id)
            if external is None:
                return WriteOutcome(
                    "unknown",
                    status_code=status,
                    body=body,
                    error_code="success_without_id",
                    error="Sucesso HTTP sem ID externo; exige reconciliação",
                )
            return WriteOutcome("success", status_code=status, body=body, external_id=external)
        if status in (400, 401, 403, 404, 409, 412, 422):
            code = "forbidden" if status in (401, 403) else (
                "precondition_failed" if status == 412 else "rejected"
            )
            try:
                body = response.json()
            except ValueError:
                body = None
            return WriteOutcome("rejected", status_code=status, body=body,
                                error_code=code, error=_detail(response))
        return WriteOutcome(
            "unknown",
            status_code=status,
            error_code="server_error",
            error=f"Adaptor {status}: {_detail(response)}",
        )
