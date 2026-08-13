from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.rate_limit import ApiRateLimitMiddleware


def test_api_rate_limit_returns_retry_after():
    test_app = FastAPI()
    test_app.add_middleware(ApiRateLimitMiddleware, requests_per_minute=2)

    @test_app.get("/api/v1/ping")
    def ping():
        return {"ok": True}

    with TestClient(test_app) as client:
        assert client.get("/api/v1/ping").status_code == 200
        assert client.get("/api/v1/ping").status_code == 200
        response = client.get("/api/v1/ping")

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
