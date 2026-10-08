from datetime import timedelta

from fastapi.testclient import TestClient

from app.auth import AuthUser, _token
from app.erp.models import ErpOperator
from app.main import app


def bearer(username: str = "admin@xnamai.com", role: str = "admin") -> dict:
    token = _token(AuthUser(username=username, role=role), "access", timedelta(minutes=5))
    return {"Authorization": f"Bearer {token}"}


def client() -> TestClient:
    # Sem context manager: não dispara o lifespan (create_all/scheduler do BI).
    return TestClient(app)


def add_operator(factory, username: str, roles: list[str], *, active: bool = True) -> None:
    with factory() as db:
        db.add(ErpOperator(username=username.casefold(), roles=roles, active=active))
        db.commit()
