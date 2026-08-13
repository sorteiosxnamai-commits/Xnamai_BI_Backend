from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import httpx
import pytest

from app import adaptor as adaptor_module


class FakeClient:
    def __init__(self, responses: list[httpx.Response]):
        self.responses = responses
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def get(self, *args, **kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


def response(status: int, *, text: str = "") -> httpx.Response:
    request = httpx.Request(
        "GET",
        "https://mercosadaptor.onrender.com/v1/orders",
    )
    if status == 200:
        return httpx.Response(
            status,
            json={"data": [], "nextCursor": None},
            request=request,
        )
    return httpx.Response(
        status,
        text=text,
        headers={"content-type": "text/html"},
        request=request,
    )


@pytest.mark.asyncio
async def test_list_waits_through_render_cold_start(monkeypatch):
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
    assert fake.calls == 6
    assert [call.args[0] for call in sleep.await_args_list] == [1, 2, 4, 8, 16]


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
