import asyncio
import logging
import time
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from app.config import settings

log = logging.getLogger("uvicorn.error")

TRANSIENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError)
DEFAULT_RETRIES = 20
WARMUP_RETRIES = 6
RETRYABLE_STATUS = {429, 502, 503, 504}
MAX_RETRY_WAIT = 300.0
RATE_LIMIT_BUDGET = 600.0
DEFAULT_429_WAIT = 30.0

_request_lock = asyncio.Lock()
_not_before = 0.0
_cancel = asyncio.Event()


def request_cancel() -> None:
    _cancel.set()


def clear_cancel() -> None:
    _cancel.clear()


def _response_detail(response: httpx.Response) -> str:
    text = response.text[:500].strip()
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" in content_type or text.lower().startswith(("<!doctype", "<html")):
        return "resposta HTML temporária do provedor"
    return text or response.reason_phrase


def _retry_wait(response: httpx.Response, attempt: int) -> float:
    try:
        payload = response.json()
        if isinstance(payload, dict) and payload.get("tempo_ate_permitir_novamente") is not None:
            wait = float(payload["tempo_ate_permitir_novamente"])
            if wait > 0:
                return min(wait + 0.5, MAX_RETRY_WAIT)
    except (ValueError, TypeError):
        pass
    header = response.headers.get("Retry-After")
    if header:
        try:
            return min(max(float(header), 1.0), MAX_RETRY_WAIT)
        except ValueError:
            pass
    if response.status_code == 429:
        return DEFAULT_429_WAIT
    return min(2 ** attempt, 30)


def _should_retry_status(
    response: httpx.Response,
    *,
    attempt: int,
    retries: int,
    waited: float,
) -> tuple[bool, float]:
    if response.status_code not in RETRYABLE_STATUS:
        return False, 0.0
    wait = _retry_wait(response, attempt)
    if attempt + 1 >= retries or waited >= RATE_LIMIT_BUDGET:
        return False, wait
    return True, wait


def _extend_cooldown(wait: float) -> None:
    global _not_before
    _not_before = max(_not_before, time.monotonic() + max(wait, 0))


async def _raise_if_cancelled() -> None:
    if _cancel.is_set():
        raise HTTPException(409, "Sincronização interrompida pelo operador")


async def _respect_cooldown() -> None:
    await _raise_if_cancelled()
    delay = _not_before - time.monotonic()
    if delay <= 0.05:
        return
    log.warning("Adaptor cooldown %.1ss before next Mercos call", delay)
    waiter = asyncio.create_task(_cancel.wait())
    sleeper = asyncio.create_task(asyncio.sleep(delay))
    done, pending = await asyncio.wait(
        {waiter, sleeper},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    for task in pending:
        try:
            await task
        except asyncio.CancelledError:
            pass
    if waiter in done and _cancel.is_set():
        raise HTTPException(409, "Sincronização interrompida pelo operador")


class Adaptor:
    async def wake(self, *, retries: int = WARMUP_RETRIES) -> None:
        cfg = settings()
        if not cfg.mercos_adaptor_url:
            return
        url = f"{cfg.mercos_adaptor_url.rstrip('/')}/health"
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.get(url)
                if not response.is_error:
                    if attempt:
                        log.info("Adaptor ready after %s health attempts", attempt + 1)
                    return
            except TRANSIENT:
                pass
            if attempt + 1 < retries:
                await asyncio.sleep(min(2 ** attempt, 15))

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
        await self.wake()
        params = {"alterado_apos": cursor} if cursor else {}
        url = f"{cfg.mercos_adaptor_url.rstrip('/')}/v1/{resource}"
        last_exc: Exception | None = None
        rate_limit_waited = 0.0

        async with _request_lock:
            for attempt in range(retries):
                try:
                    await _respect_cooldown()
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
                    _extend_cooldown(wait)
                    log.warning(
                        "Adaptor %s attempt %s/%s failed (%s); retry in %ss",
                        resource,
                        attempt + 1,
                        retries,
                        type(exc).__name__,
                        wait,
                    )
                    continue
                except httpx.RequestError as exc:
                    raise HTTPException(502, f"Adaptor inacessível: {type(exc).__name__}") from exc

                if r.status_code in RETRYABLE_STATUS:
                    retry, wait = _should_retry_status(
                        r,
                        attempt=attempt,
                        retries=retries,
                        waited=rate_limit_waited,
                    )
                    if retry:
                        _extend_cooldown(wait)
                        rate_limit_waited += wait
                        log.warning(
                            "Adaptor %s HTTP %s attempt %s/%s; retry in %ss",
                            resource,
                            r.status_code,
                            attempt + 1,
                            retries,
                            wait,
                        )
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
        await self.wake()
        safe_id = quote(str(mercos_id), safe="")
        url = f"{cfg.mercos_adaptor_url.rstrip('/')}/v1/{resource}/{safe_id}"
        last_exc: Exception | None = None
        rate_limit_waited = 0.0
        async with _request_lock:
            for attempt in range(retries):
                try:
                    await _respect_cooldown()
                    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
                        response = await client.get(
                            url,
                            headers={"X-API-Key": cfg.mercos_adaptor_api_key},
                        )
                except TRANSIENT as exc:
                    last_exc = exc
                    if attempt + 1 < retries:
                        _extend_cooldown(min(2 ** attempt, 30))
                        continue
                    break
                except httpx.RequestError as exc:
                    raise HTTPException(502, f"Adaptor inacessível: {type(exc).__name__}") from exc

                if response.status_code in RETRYABLE_STATUS:
                    retry, wait = _should_retry_status(
                        response,
                        attempt=attempt,
                        retries=retries,
                        waited=rate_limit_waited,
                    )
                    if retry:
                        _extend_cooldown(wait)
                        rate_limit_waited += wait
                        log.warning(
                            "Adaptor %s/%s HTTP %s attempt %s/%s; retry in %ss",
                            resource,
                            mercos_id,
                            response.status_code,
                            attempt + 1,
                            retries,
                            wait,
                        )
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


async def keep_adaptor_warm() -> None:
    try:
        await adaptor.wake(retries=2)
    except Exception as exc:
        log.warning("Adaptor keep-warm failed: %s", type(exc).__name__)
