import httpx
from app.config import settings

class Adaptor:
    async def list(self, resource: str, cursor: str|None=None):
        cfg=settings(); params={"alterado_apos":cursor} if cursor else {}
        async with httpx.AsyncClient(timeout=120) as client:
            r=await client.get(f"{cfg.mercos_adaptor_url.rstrip('/')}/v1/{resource}", params=params, headers={"X-API-Key":cfg.mercos_adaptor_api_key})
            r.raise_for_status(); return r.json()
    async def health(self):
        async with httpx.AsyncClient(timeout=20) as client:
            r=await client.get(f"{settings().mercos_adaptor_url.rstrip('/')}/health"); r.raise_for_status(); return r.json()
adaptor=Adaptor()

