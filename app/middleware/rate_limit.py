from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import asyncio

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class ApiRateLimitMiddleware(BaseHTTPMiddleware):
    """Small single-instance guard; login also has a stricter dedicated limit."""

    def __init__(self, app, requests_per_minute: int = 300):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.requests: defaultdict[str, deque[datetime]] = defaultdict(deque)
        self.lock = asyncio.Lock()

    async def dispatch(self, request, call_next):
        if not request.url.path.startswith("/api/v1"):
            return await call_next(request)
        client = request.client.host if request.client else "unknown"
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=1)
        async with self.lock:
            bucket = self.requests[client]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.requests_per_minute:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Limite de requisições excedido"},
                    headers={"Retry-After": "60"},
                )
            bucket.append(now)
        return await call_next(request)
