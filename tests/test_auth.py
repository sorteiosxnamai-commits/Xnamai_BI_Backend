from fastapi import HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials
import pytest

from app import auth
from app.config import Settings


@pytest.fixture
def auth_settings(monkeypatch):
    config = Settings(
        jwt_secret="test-secret-with-enough-entropy",
        auth_admin_username="admin@xnamai.com",
        auth_admin_password="123456",
        auth_viewer_username="viewer",
        auth_viewer_password="viewer-pass",
        auth_cookie_secure=False,
        bi_api_key="service-key",
    )
    monkeypatch.setattr(auth, "settings", lambda: config)
    return config


def test_login_issues_access_and_http_only_refresh(auth_settings):
    user = auth.authenticate(
        auth.LoginRequest(username="admin@xnamai.com", password="123456")
    )
    response = Response()

    result = auth.issue_tokens(user, response)

    decoded = auth.decode_token(result.accessToken, "access")
    assert decoded.role == "admin"
    cookie = response.headers["set-cookie"]
    assert "bi_refresh=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=none" in cookie


def test_viewer_cannot_use_admin_dependency(auth_settings):
    with pytest.raises(HTTPException) as error:
        auth.require_admin(auth.AuthUser(username="viewer", role="viewer"))
    assert error.value.status_code == 403


def test_api_key_grants_service_admin(auth_settings):
    user = auth.current_user(credentials=None, x_api_key="service-key")
    assert user.username == "service"
    assert user.role == "admin"


def test_missing_credentials_are_rejected(auth_settings):
    with pytest.raises(HTTPException) as error:
        auth.current_user(credentials=None, x_api_key=None)
    assert error.value.status_code == 401


def test_refresh_without_cookie_is_rejected(auth_settings):
    with pytest.raises(HTTPException) as error:
        auth.refresh_user(bi_refresh=None)
    assert error.value.status_code == 401
