from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import secrets
from typing import Literal
from uuid import uuid4

from fastapi import Cookie, Depends, Header, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from app.config import settings


Role = Literal["admin", "viewer"]
ALGORITHM = "HS256"
bearer = HTTPBearer(auto_error=False)
login_attempts: defaultdict[str, deque[datetime]] = defaultdict(deque)


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthUser(BaseModel):
    username: str
    role: Role


class AuthResponse(BaseModel):
    accessToken: str
    tokenType: str = "bearer"
    expiresIn: int
    user: AuthUser


def _credentials(username: str, password: str) -> AuthUser | None:
    config = settings()
    candidates = (
        (
            config.auth_admin_username,
            config.auth_admin_password,
            "admin",
        ),
        (
            config.auth_viewer_username,
            config.auth_viewer_password,
            "viewer",
        ),
    )
    for expected_user, expected_password, role in candidates:
        if (
            expected_password
            and secrets.compare_digest(username, expected_user)
            and secrets.compare_digest(password, expected_password)
        ):
            return AuthUser(username=expected_user, role=role)
    return None


def check_login_rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = datetime.now(timezone.utc)
    window = now - timedelta(minutes=1)
    attempts = login_attempts[key]
    while attempts and attempts[0] < window:
        attempts.popleft()
    if len(attempts) >= 10:
        raise HTTPException(429, "Muitas tentativas. Aguarde um minuto.")
    attempts.append(now)


def _token(user: AuthUser, token_type: str, lifetime: timedelta) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": user.username,
            "role": user.role,
            "type": token_type,
            "iat": now,
            "exp": now + lifetime,
            "jti": uuid4().hex,
        },
        settings().jwt_secret,
        algorithm=ALGORITHM,
    )


def issue_tokens(user: AuthUser, response: Response) -> AuthResponse:
    config = settings()
    access_seconds = config.auth_access_minutes * 60
    access = _token(
        user,
        "access",
        timedelta(seconds=access_seconds),
    )
    refresh = _token(
        user,
        "refresh",
        timedelta(days=config.auth_refresh_days),
    )
    response.set_cookie(
        "bi_refresh",
        refresh,
        max_age=config.auth_refresh_days * 86400,
        httponly=True,
        secure=config.auth_cookie_secure,
        samesite=config.auth_cookie_samesite,
        path="/api/v1/auth",
    )
    return AuthResponse(
        accessToken=access,
        expiresIn=access_seconds,
        user=user,
    )


def authenticate(payload: LoginRequest) -> AuthUser:
    user = _credentials(payload.username, payload.password)
    if user is None:
        raise HTTPException(401, "Usuário ou senha inválidos")
    return user


def decode_token(token: str, expected_type: str) -> AuthUser:
    try:
        payload = jwt.decode(
            token,
            settings().jwt_secret,
            algorithms=[ALGORITHM],
        )
    except JWTError as exc:
        raise HTTPException(401, "Sessão inválida ou expirada") from exc
    if payload.get("type") != expected_type:
        raise HTTPException(401, "Tipo de token inválido")
    username = payload.get("sub")
    role = payload.get("role")
    if not username or role not in {"admin", "viewer"}:
        raise HTTPException(401, "Token sem identidade válida")
    return AuthUser(username=username, role=role)


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    x_api_key: str | None = Header(None),
) -> AuthUser:
    if credentials and credentials.scheme.lower() == "bearer":
        return decode_token(credentials.credentials, "access")

    expected = settings().bi_api_key
    if (
        x_api_key
        and expected
        and len(x_api_key) == len(expected)
        and secrets.compare_digest(x_api_key, expected)
    ):
        return AuthUser(username="service-api-key", role="admin")
    raise HTTPException(401, "Autenticação necessária")


def require_admin(user: AuthUser = Depends(current_user)) -> AuthUser:
    if user.role != "admin":
        raise HTTPException(403, "Acesso exclusivo para administradores")
    return user


def refresh_user(bi_refresh: str | None = Cookie(None)) -> AuthUser:
    if not bi_refresh:
        raise HTTPException(401, "Refresh token ausente")
    return decode_token(bi_refresh, "refresh")


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie("bi_refresh", path="/api/v1/auth")
