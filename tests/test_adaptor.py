from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import httpx
import pytest

from app import adaptor as adaptor_module


@pytest.fixture(autouse=True)
def reset_adaptor_cooldown():
    adaptor_module._not_before = 0.0
    yield
    adaptor_module._not_before = 0.0


class FakeClient:
    def __init__(self, responses: list[httpx.Response], *, health_ok: bool = True):
        self.responses = responses
        self.calls = 0
        self.health_calls = 0
        self.health_ok = health_ok
        self.urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def get(self, url, *args, **kwargs):
        target = str(url)
        self.urls.append(target)
        if "/health" in target:
            self.health_calls += 1
            request = httpx.Request("GET", target)
            if self.health_ok:
                return httpx.Response(200, json={"status": "ok"}, request=request)
            return httpx.Response(
                502,
                text="<!DOCTYPE html><title>502</title>",
                headers={"content-type": "text/html"},
                request=request,
            )
        response = self.responses[self.calls]
        self.calls += 1
        return response


def response(status: int, *, text: str = "", headers: dict | None = None) -> httpx.Response:
    request = httpx.Request(
        "GET",
        "https://mercosadaptor.onrender.com/v1/orders",
    )
    merged = {"content-type": "text/html"}
    if headers:
        merged.update(headers)
    if status == 200:
        return httpx.Response(
            status,
            json={"data": [], "nextCursor": None},
            request=request,
        )
    return httpx.Response(
        status,
        text=text,
        headers=merged,
        request=request,
    )


@pytest.mark.asyncio
async def test_list_wakes_adaptor_before_orders(monkeypatch):
    fake = FakeClient(
        [
            response(502, text="<!DOCTYPE html><title>502</title>"),
            response(502, text="<!DOCTYPE html><title>502</title>"),
            response(502, text="<!DOCTYPE html><title>502</title>"),
            response(502, text="<!DOCTYPE html><title>502</title>"),
            response(502, text="<!DOCTYPE html><title>502</title>"),
            response(200),
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(adaptor_module.httpx, "AsyncClient", lambda **kwargs: fake)
    monkeypatch.setattr(adaptor_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(
        adaptor_module,
        "settings",
        lambda: SimpleNamespace(
            mercos_adaptor_url="https://mercosadaptor.onrender.com",
            mercos_adaptor_api_key="test-key",
        ),
    )

    result = await adaptor_module.Adaptor().list("orders")

    assert result == {"data": [], "nextCursor": None}
    assert fake.health_calls == 1
    assert fake.calls == 6
    assert fake.urls[0].endswith("/health")
    assert [call.args[0] for call in sleep.await_args_list] == pytest.approx(
        [1, 2, 4, 8, 16], abs=0.05
    )


@pytest.mark.asyncio
async def test_list_retries_429_then_succeeds(monkeypatch):
    fake = FakeClient(
        [
            response(429, text="Too Many Requests", headers={"Retry-After": "7"}),
            response(200),
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(adaptor_module.httpx, "AsyncClient", lambda **kwargs: fake)
    monkeypatch.setattr(adaptor_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(
        adaptor_module,
        "settings",
        lambda: SimpleNamespace(
            mercos_adaptor_url="https://mercosadaptor.onrender.com",
            mercos_adaptor_api_key="test-key",
        ),
    )

    result = await adaptor_module.Adaptor().list("customers")

    assert result == {"data": [], "nextCursor": None}
    assert fake.calls == 2
    assert sleep.await_args_list[0].args[0] == pytest.approx(7, abs=0.05)


@pytest.mark.asyncio
async def test_list_uses_default_429_backoff_without_retry_after(monkeypatch):
    fake = FakeClient(
        [
            response(429, text="Too Many Requests"),
            response(200),
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(adaptor_module.httpx, "AsyncClient", lambda **kwargs: fake)
    monkeypatch.setattr(adaptor_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(
        adaptor_module,
        "settings",
        lambda: SimpleNamespace(
            mercos_adaptor_url="https://mercosadaptor.onrender.com",
            mercos_adaptor_api_key="test-key",
        ),
    )

    result = await adaptor_module.Adaptor().list("products")

    assert result == {"data": [], "nextCursor": None}
    assert sleep.await_args_list[0].args[0] == pytest.approx(30, abs=0.05)


@pytest.mark.asyncio
async def test_list_does_not_persist_provider_html(monkeypatch):
    fake = FakeClient([response(502, text="<!DOCTYPE html><title>502</title>")])
    monkeypatch.setattr(adaptor_module.httpx, "AsyncClient", lambda **kwargs: fake)
    monkeypatch.setattr(
        adaptor_module,
        "settings",
        lambda: SimpleNamespace(
            mercos_adaptor_url="https://mercosadaptor.onrender.com",
            mercos_adaptor_api_key="test-key",
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await adaptor_module.Adaptor().list("orders", retries=1)

    assert "resposta HTML temporária do provedor" in exc_info.value.detail
    assert "<!DOCTYPE" not in exc_info.value.detail
    assert fake.health_calls == 1
    assert fake.calls == 1
