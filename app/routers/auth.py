from fastapi import APIRouter, Depends, Request, Response

from app.auth import (
    AuthResponse,
    AuthUser,
    LoginRequest,
    authenticate,
    check_login_rate_limit,
    clear_refresh_cookie,
    current_user,
    issue_tokens,
    refresh_user,
)


router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, request: Request, response: Response):
    check_login_rate_limit(request)
    return issue_tokens(authenticate(payload), response)


@router.post("/refresh", response_model=AuthResponse)
def refresh(response: Response, user: AuthUser = Depends(refresh_user)):
    return issue_tokens(user, response)


@router.post("/logout", status_code=204)
def logout(response: Response):
    clear_refresh_cookie(response)


@router.get("/me", response_model=AuthUser)
def me(user: AuthUser = Depends(current_user)):
    return user
