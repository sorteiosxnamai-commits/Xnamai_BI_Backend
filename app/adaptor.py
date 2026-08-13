import asyncio
import logging
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from app.config import settings

log = logging.getLogger("uvicorn.error")

TRANSIENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError)
DEFAULT_RETRIES = 8


def _response_detail(response: httpx.Response) -> str:
    text = response.text[:500].strip()
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" in content_type or text.lower().startswith(("<!doctype", "<html")):
        return "resposta HTML temporária do provedor"
    return text or response.reason_phrase


class Adaptor:
    async def list(
        self,
        resource: str,
        cursor: str | None = None,
        *,
        retries: int = DEFAULT_RETRIES,
    ):
        cfg = settings()
        if not cfg.mercos_adaptor_url or not cfg.mercos_adaptor_api_key:
            raise HTTPException(503, "MERCOS_ADAPTOR_URL/API_KEY não configurados")
        params = {"alterado_apos": cursor} if cursor else {}
        url = f"{cfg.mercos_adaptor_url.rstrip('/')}/v1/{resource}"
        last_exc: Exception | None = None

        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
                    r = await client.get(
                        url,
                        params=params,
                        headers={"X-API-Key": cfg.mercos_adaptor_api_key},
                    )
            except TRANSIENT as exc:
                last_exc = exc
                if attempt + 1 >= retries:
                    break
                wait = min(2 ** attempt, 30)
                log.warning(
                    "Adaptor %s attempt %s/%s failed (%s); retry in %ss",
                    resource,
                    attempt + 1,
                    retries,
                    type(exc).__name__,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            except httpx.RequestError as exc:
                raise HTTPException(502, f"Adaptor inacessível: {type(exc).__name__}") from exc

            if r.status_code in {502, 503, 504} and attempt + 1 < retries:
                wait = min(2 ** attempt, 30)
                log.warning(
                    "Adaptor %s HTTP %s attempt %s/%s; retry in %ss",
                    resource,
                    r.status_code,
                    attempt + 1,
                    retries,
                    wait,
                )
                await asyncio.sleep(wait)
                continue

            if r.is_error:
                detail = _response_detail(r)
                raise HTTPException(
                    status_code=502 if r.status_code >= 500 else r.status_code,
                    detail=f"Adaptor {r.status_code} em /v1/{resource}: {detail}",
                )
            return r.json()

        raise HTTPException(
            502,
            f"Adaptor inacessível após {retries} tentativas: {type(last_exc).__name__ if last_exc else 'erro'}",
        )

    async def detail(
        self,
        resource: str,
        mercos_id: str,
        *,
        retries: int = DEFAULT_RETRIES,
    ):
        cfg = settings()
        if not cfg.mercos_adaptor_url or not cfg.mercos_adaptor_api_key:
            raise HTTPException(503, "MERCOS_ADAPTOR_URL/API_KEY não configurados")
        safe_id = quote(str(mercos_id), safe="")
        url = f"{cfg.mercos_adaptor_url.rstrip('/')}/v1/{resource}/{safe_id}"
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
                    response = await client.get(
                        url,
                        headers={"X-API-Key": cfg.mercos_adaptor_api_key},
                    )
            except TRANSIENT as exc:
                last_exc = exc
                if attempt + 1 < retries:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                break
            except httpx.RequestError as exc:
                raise HTTPException(502, f"Adaptor inacessível: {type(exc).__name__}") from exc

            if response.status_code in {429, 502, 503, 504} and attempt + 1 < retries:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else min(2 ** attempt, 30)
                except ValueError:
                    wait = min(2 ** attempt, 30)
                await asyncio.sleep(max(0, wait))
                continue
            if response.is_error:
                detail = _response_detail(response)
                raise HTTPException(
                    status_code=502 if response.status_code >= 500 else response.status_code,
                    detail=f"Adaptor {response.status_code} em detalhe de {resource}: {detail}",
                )
            payload = response.json()
            if not isinstance(payload, dict):
                raise HTTPException(502, f"Detalhe de {resource} retornou formato inválido")
            return payload

        raise HTTPException(
            502,
            f"Adaptor inacessível após {retries} tentativas: "
            f"{type(last_exc).__name__ if last_exc else 'erro'}",
        )

    async def health(self):
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{settings().mercos_adaptor_url.rstrip('/')}/health")
            r.raise_for_status()
            return r.json()


adaptor = Adaptor()
