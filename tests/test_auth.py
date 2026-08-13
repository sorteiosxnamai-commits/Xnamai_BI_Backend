from fastapi import HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials
import pytest

from app import auth
from app.config import Settings


@pytest.fixture
def auth_settings(monkeypatch):
    config = Settings(
        jwt_secret="test-secret-with-enough-entropy",
        auth_admin_username="admin",
        auth_admin_password="admin-pass",
        auth_viewer_username="viewer",
        auth_viewer_password="viewer-pass",
        auth_cookie_secure=False,
        bi_api_key="service-key",
    )
    monkeypatch.setattr(auth, "settings", lambda: config)
    return config


def test_login_issues_access_and_http_only_refresh(auth_settings):
    user = auth.authenticate(
        auth.LoginRequest(username="admin", password="admin-pass")
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


def test_service_api_key_remains_available_for_non_browser_integrations(auth_settings):
    user = auth.current_user(
        credentials=None,
        x_api_key="service-key",
    )
    assert user.role == "admin"

    token = auth._token(
        auth.AuthUser(username="viewer", role="viewer"),
        "access",
        auth.timedelta(minutes=5),
    )
    viewer = auth.current_user(
        credentials=HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials=token,
        ),
        x_api_key=None,
    )
    assert viewer.role == "viewer"


def test_public_mode_grants_direct_admin_access(auth_settings, monkeypatch):
    public_config = auth_settings.model_copy(update={"auth_disabled": True})
    monkeypatch.setattr(auth, "settings", lambda: public_config)

    user = auth.current_user(credentials=None, x_api_key=None)
    refreshed = auth.refresh_user(bi_refresh=None)

    assert user == auth.AuthUser(username="public-admin", role="admin")
    assert refreshed == user
