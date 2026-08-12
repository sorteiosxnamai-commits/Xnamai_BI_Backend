import httpx
from fastapi import HTTPException

from app.config import settings


class Adaptor:
    async def list(self, resource: str, cursor: str | None = None):
        cfg = settings()
        if not cfg.mercos_adaptor_url or not cfg.mercos_adaptor_api_key:
            raise HTTPException(503, "MERCOS_ADAPTOR_URL/API_KEY não configurados")
        params = {"alterado_apos": cursor} if cursor else {}
        url = f"{cfg.mercos_adaptor_url.rstrip('/')}/v1/{resource}"
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.get(
                    url,
                    params=params,
                    headers={"X-API-Key": cfg.mercos_adaptor_api_key},
                )
        except httpx.RequestError as exc:
            raise HTTPException(502, f"Adaptor inacessível: {type(exc).__name__}") from exc
        if r.is_error:
            detail = r.text[:500] or r.reason_phrase
            raise HTTPException(
                status_code=502 if r.status_code >= 500 else r.status_code,
                detail=f"Adaptor {r.status_code} em /v1/{resource}: {detail}",
            )
        return r.json()

    async def health(self):
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{settings().mercos_adaptor_url.rstrip('/')}/health")
            r.raise_for_status()
            return r.json()


adaptor = Adaptor()
