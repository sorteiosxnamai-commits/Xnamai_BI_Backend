import asyncio
import logging

import httpx
from fastapi import HTTPException

from app.config import settings

log = logging.getLogger("uvicorn.error")

TRANSIENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError)


class Adaptor:
    async def list(self, resource: str, cursor: str | None = None, *, retries: int = 5):
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
                detail = r.text[:500] or r.reason_phrase
                raise HTTPException(
                    status_code=502 if r.status_code >= 500 else r.status_code,
                    detail=f"Adaptor {r.status_code} em /v1/{resource}: {detail}",
                )
            return r.json()

        raise HTTPException(
            502,
            f"Adaptor inacessível após {retries} tentativas: {type(last_exc).__name__ if last_exc else 'erro'}",
        )

    async def health(self):
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{settings().mercos_adaptor_url.rstrip('/')}/health")
            r.raise_for_status()
            return r.json()


adaptor = Adaptor()
