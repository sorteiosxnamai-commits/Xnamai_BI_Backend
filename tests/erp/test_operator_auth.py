from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.erp import sessions
from app.erp.models import ErpAuditEvent, ErpOperator, ErpSession
from tests.erp.helpers import add_operator, bearer, client

STRONG = "Correct-Horse-Battery-9"


@pytest.fixture(autouse=True)
def _setup(erp_cfg, erp_session_factory):
    # vínculo legado DESLIGADO: só login individual (ou bootstrap pelo BI)
    erp_cfg(erp_allow_bi_operator_link=False)


def admin_h():
    return bearer()  # admin de bootstrap entra pelo BI para criar os operadores


def create_operator(c, username="ana@x.com", roles=("comercial",)):
    r = c.put("/api/v1/erp/operators", json={"username": username, "roles": list(roles)}, headers=admin_h())
    assert r.status_code == 200, r.text
    r = c.post(f"/api/v1/erp/operators/{username}/password", json={}, headers=admin_h())
    assert r.status_code == 200, r.text
    return r.json()["temporaryPassword"]


def login(c, username, password):
    return c.post("/api/v1/erp/auth/login", json={"username": username, "password": password})


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_full_lifecycle_temp_password_forced_change_then_access():
    c = client()
    temp = create_operator(c)
    r = login(c, "ana@x.com", temp)
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["mustChangePassword"] is True
    cookie = r.headers["set-cookie"]
    assert "erp_refresh=" in cookie and "HttpOnly" in cookie and "Path=/api/v1/erp/auth" in cookie
    token = body["accessToken"]
    # /me funciona, o resto do ERP exige trocar a senha antes
    me = c.get("/api/v1/erp/me", headers=auth(token)).json()
    assert me["authMethod"] == "erp_session" and me["mustChangePassword"] is True
    blocked = c.get("/api/v1/erp/customers", headers=auth(token))
    assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "password_change_required"
    # política de senha
    weak = c.post("/api/v1/erp/auth/change-password", headers=auth(token),
                  json={"currentPassword": temp, "newPassword": "ana12345678"})
    assert weak.status_code == 422 and weak.json()["detail"]["code"] == "weak_password"
    wrong = c.post("/api/v1/erp/auth/change-password", headers=auth(token),
                   json={"currentPassword": "errada", "newPassword": STRONG})
    assert wrong.status_code == 403
    ok = c.post("/api/v1/erp/auth/change-password", headers=auth(token),
                json={"currentPassword": temp, "newPassword": STRONG})
    assert ok.status_code == 200
    assert c.get("/api/v1/erp/customers", headers=auth(token)).status_code == 200
    # a senha temporária não vale mais
    assert login(c, "ana@x.com", temp).status_code == 401
    assert login(c, "ANA@x.com ", STRONG).status_code == 200  # login normalizado


def test_identity_is_individual_and_permissions_follow_the_operator():
    c = client()
    for name, role, suffix in (("ana@x.com", "comercial", "-Q1"), ("leo@x.com", "consulta", "-Z2")):
        temp = create_operator(c, name, (role,))
        token = login(c, name, temp).json()["accessToken"]
        c.post("/api/v1/erp/auth/change-password", headers=auth(token),
               json={"currentPassword": temp, "newPassword": STRONG + suffix})
    ana = login(c, "ana@x.com", STRONG + "-Q1").json()["accessToken"]
    leo = login(c, "leo@x.com", STRONG + "-Z2").json()["accessToken"]
    assert c.get("/api/v1/erp/me", headers=auth(ana)).json()["username"] == "ana@x.com"
    assert c.get("/api/v1/erp/me", headers=auth(leo)).json()["username"] == "leo@x.com"
    assert "customers:write" in c.get("/api/v1/erp/me", headers=auth(ana)).json()["permissions"]
    assert "customers:write" not in c.get("/api/v1/erp/me", headers=auth(leo)).json()["permissions"]
    assert c.get("/api/v1/erp/operators", headers=auth(ana)).status_code == 403


def test_failures_look_the_same_and_lock_the_account(erp_session_factory):
    c = client()
    temp = create_operator(c)
    unknown = login(c, "naoexiste@x.com", "qualquer")
    wrong = login(c, "ana@x.com", "errada")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()  # não revela se o login existe
    for _ in range(4):
        assert login(c, "ana@x.com", "errada").status_code == 401
    locked = login(c, "ana@x.com", temp)  # senha CORRETA, mas a conta bloqueou
    assert locked.status_code == 423 and locked.json()["detail"]["code"] == "account_locked"
    with erp_session_factory() as db:
        op = db.scalar(select(ErpOperator))
        op.locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert login(c, "ana@x.com", temp).status_code == 200
    with erp_session_factory() as db:
        events = db.scalars(select(ErpAuditEvent).where(ErpAuditEvent.action == "auth.login")).all()
        assert {e.result for e in events} >= {"failed", "locked", "ok"}
        assert all("password" not in str(e.detail).lower() for e in events)


def test_refresh_rotates_and_reuse_revokes_the_session(erp_session_factory):
    c = client()
    temp = create_operator(c)
    first = login(c, "ana@x.com", temp)
    old_cookie = first.cookies.get("erp_refresh")
    assert old_cookie
    c.cookies.clear()
    r1 = c.post("/api/v1/erp/auth/refresh", cookies={"erp_refresh": old_cookie})
    assert r1.status_code == 200
    new_cookie = r1.cookies.get("erp_refresh")
    assert new_cookie and new_cookie != old_cookie
    # reapresentar o cookie antigo = suspeita de cópia: derruba a sessão inteira
    reuse = c.post("/api/v1/erp/auth/refresh", cookies={"erp_refresh": old_cookie})
    assert reuse.status_code == 401
    assert c.post("/api/v1/erp/auth/refresh", cookies={"erp_refresh": new_cookie}).status_code == 401
    assert c.get("/api/v1/erp/me", headers=auth(r1.json()["accessToken"])).status_code in (401, 403)
    with erp_session_factory() as db:
        assert db.scalar(select(ErpSession.revoked_reason)) == "refresh_reuse"


def test_logout_deactivation_and_admin_revoke_cut_access_immediately():
    c = client()
    temp = create_operator(c)
    token = login(c, "ana@x.com", temp).json()["accessToken"]
    assert c.get("/api/v1/erp/me", headers=auth(token)).status_code == 200
    revoke = c.post("/api/v1/erp/operators/ana@x.com/revoke-sessions", headers=admin_h())
    assert revoke.json()["sessionsRevoked"] == 1
    assert c.get("/api/v1/erp/me", headers=auth(token)).status_code in (401, 403)
    token2 = login(c, "ana@x.com", temp).json()["accessToken"]
    assert c.get("/api/v1/erp/me", headers=auth(token2)).status_code == 200
    c.put("/api/v1/erp/operators", json={"username": "ana@x.com", "roles": ["comercial"], "active": False},
          headers=admin_h())
    assert c.get("/api/v1/erp/me", headers=auth(token2)).status_code in (401, 403)
    assert login(c, "ana@x.com", temp).status_code == 401


def test_logout_revokes_only_that_session_and_sessions_are_listable():
    c = client()
    temp = create_operator(c)
    a = login(c, "ana@x.com", temp)
    b = login(c, "ana@x.com", temp)
    ta, tb = a.json()["accessToken"], b.json()["accessToken"]
    listing = c.get("/api/v1/erp/auth/sessions", headers=auth(ta)).json()["items"]
    assert len(listing) == 2 and sum(1 for s in listing if s["current"]) == 1
    cookie_b = b.cookies.get("erp_refresh")
    c.cookies.clear()
    assert c.post("/api/v1/erp/auth/logout", cookies={"erp_refresh": cookie_b}).status_code == 204
    assert c.get("/api/v1/erp/me", headers=auth(tb)).status_code in (401, 403)
    assert c.get("/api/v1/erp/me", headers=auth(ta)).status_code == 200
    other = [s for s in listing if not s["current"]][0]["id"]
    assert c.delete(f"/api/v1/erp/auth/sessions/{other}", headers=auth(ta)).status_code == 204


def test_bi_logins_do_not_identify_people_unless_explicitly_linked(erp_session_factory, erp_cfg):
    add_operator(erp_session_factory, "viewer", ["comercial"])
    c = client()
    # operador cadastrado com o login compartilhado do BI: recusado por padrão
    denied = c.get("/api/v1/erp/me", headers=bearer("viewer", "viewer"))
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "no_erp_access"
    # o admin de bootstrap continua entrando pelo BI
    assert c.get("/api/v1/erp/me", headers=admin_h()).json()["authMethod"] == "bi_bootstrap"
    erp_cfg(erp_allow_bi_operator_link=True)
    assert c.get("/api/v1/erp/me", headers=bearer("viewer", "viewer")).status_code == 200


def test_token_types_are_not_interchangeable_with_the_bi():
    c = client()
    temp = create_operator(c)
    erp_token = login(c, "ana@x.com", temp).json()["accessToken"]
    # token ERP não autentica rotas do BI
    assert c.get("/api/v1/auth/me", headers=auth(erp_token)).status_code == 401
    # a chave de serviço do BI continua sem acesso ao ERP
    assert c.get("/api/v1/erp/me", headers={"X-API-Key": "x"}).status_code in (401, 403)


def test_operator_without_password_cannot_login_and_inactive_cannot_either(erp_session_factory):
    add_operator(erp_session_factory, "semsenha@x.com", ["consulta"])
    c = client()
    assert login(c, "semsenha@x.com", "qualquer-coisa-123").status_code == 401
    temp = create_operator(c, "off@x.com")
    with erp_session_factory() as db:
        op = db.scalar(select(ErpOperator).where(ErpOperator.username == "off@x.com"))
        op.active = False
        db.commit()
    assert login(c, "off@x.com", temp).status_code == 401


def test_password_hash_is_salted_scrypt_never_plaintext(erp_session_factory):
    c = client()
    temp = create_operator(c)
    with erp_session_factory() as db:
        stored = db.scalar(select(ErpOperator.password_hash))
    assert stored.startswith("scrypt$") and temp not in stored
    other = create_operator(c, "bia@x.com")
    with erp_session_factory() as db:
        hashes = [h for (h,) in db.execute(select(ErpOperator.password_hash)).all()]
    assert len(set(hashes)) == 2 and other != temp


def test_expired_access_session_is_rejected(erp_session_factory):
    c = client()
    temp = create_operator(c)
    token = login(c, "ana@x.com", temp).json()["accessToken"]
    with erp_session_factory() as db:
        s = db.scalar(select(ErpSession))
        s.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        assert sessions.validate_access(db, token) is None
    assert c.get("/api/v1/erp/me", headers=auth(token)).status_code in (401, 403)
