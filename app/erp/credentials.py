"""Senhas dos operadores ERP: scrypt (stdlib) com sal por senha e política mínima."""

import base64
import hashlib
import hmac
import secrets

from fastapi import HTTPException

from app.erp.common import error_detail

_N, _R, _P = 2**14, 8, 1
COMMON = {
    "123456789012", "password1234", "senha1234567", "xnamai123456", "qwertyuiop12",
    "administrador", "mudar123456a",
}


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=64 * 1024 * 1024)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _derive(password, salt, _N, _R, _P)
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Tempo constante também quando não há hash (evita revelar se o login existe)."""
    if not stored:
        _derive(password, b"\0" * 16, _N, _R, _P)
        return False
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        actual = _derive(password, base64.b64decode(salt_b64), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def check_policy(password: str, username: str, minimum: int) -> None:
    problems = []
    if len(password) < minimum:
        problems.append(f"mínimo de {minimum} caracteres")
    local = username.split("@")[0].casefold()
    if local and len(local) >= 3 and local in password.casefold():
        problems.append("não pode conter o seu login")
    if password.casefold() in COMMON or len(set(password)) < 5:
        problems.append("senha comum ou repetitiva")
    if problems:
        raise HTTPException(
            422, error_detail("weak_password", "Senha fraca: " + "; ".join(problems))
        )


def generate_temporary_password(username: str = "", minimum: int = 12) -> str:
    """Gera até passar na própria política (evita sortear algo que contenha o login)."""
    for _ in range(50):
        candidate = secrets.token_urlsafe(12)
        try:
            check_policy(candidate, username, minimum)
        except HTTPException:
            continue
        return candidate
    raise RuntimeError("não foi possível gerar senha temporária")
