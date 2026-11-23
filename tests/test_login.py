"""Password sign-in, Microsoft (OIDC) sign-in and server-issued sessions."""
# ruff: noqa: S105, S106 - test credentials

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from zz2.gateways.user_directory import hash_password, verify_password

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# -- Hashing -------------------------------------------------------------------
def test_password_hash_round_trip() -> None:
    stored = hash_password("correct horse battery")
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse battery", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("anything", None)
    assert not verify_password("anything", "md5$abc")


# -- Password login --------------------------------------------------------------
def test_password_login_issues_a_working_session(app: Any, fake_admin: Any) -> None:
    fake_admin.seed(email="ana@zz2.com.br", role="tech", password="s3nha-muito-forte")
    response = app.post("/api/auth/login",
                        json={"email": "ANA@zz2.com.br", "password": "s3nha-muito-forte"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["email"] == "ana@zz2.com.br"
    verify = app.post("/api/session/verify",
                      headers={"Authorization": f"Bearer {body['token']}"})
    assert verify.status_code == 200


def test_password_login_rejects_bad_credentials(app: Any, fake_admin: Any) -> None:
    fake_admin.seed(email="ana@zz2.com.br", role="tech", password="s3nha-muito-forte")
    for payload in ({"email": "ana@zz2.com.br", "password": "errada"},
                    {"email": "ninguem@zz2.com.br", "password": "s3nha-muito-forte"}):
        assert app.post("/api/auth/login", json=payload).status_code == 401


def test_deactivated_account_cannot_sign_in(app: Any, fake_admin: Any) -> None:
    user_id = fake_admin.seed(email="ana@zz2.com.br", role="tech", password="s3nha-muito-forte")
    fake_admin.update_user(user_id, {"ban_duration": "infinity"})
    response = app.post("/api/auth/login",
                        json={"email": "ana@zz2.com.br", "password": "s3nha-muito-forte"})
    assert response.status_code == 401


def test_deactivation_ends_a_live_session(app: Any, fake_admin: Any) -> None:
    from zz2.services.auth import _principal_cache

    user_id = fake_admin.seed(email="ana@zz2.com.br", role="tech", password="s3nha-muito-forte")
    token = app.post("/api/auth/login", json={"email": "ana@zz2.com.br",
                                              "password": "s3nha-muito-forte"}).json()["token"]
    header = {"Authorization": f"Bearer {token}"}
    assert app.post("/api/session/verify", headers=header).status_code == 200
    fake_admin.update_user(user_id, {"ban_duration": "infinity"})
    _principal_cache.clear()
    assert app.post("/api/session/verify", headers=header).status_code == 401


def test_logout_revokes_the_token(app: Any, fake_admin: Any) -> None:
    fake_admin.seed(email="ana@zz2.com.br", role="tech", password="s3nha-muito-forte")
    token = app.post("/api/auth/login", json={"email": "ana@zz2.com.br",
                                              "password": "s3nha-muito-forte"}).json()["token"]
    header = {"Authorization": f"Bearer {token}"}
    app.post("/api/session/logout", headers=header)
    assert app.post("/api/session/verify", headers=header).status_code == 401


def test_foreign_tokens_are_rejected(app: Any) -> None:
    forged = jwt.encode({"sub": "x", "aud": "authenticated", "exp": int(time.time()) + 60},
                        "some-other-secret-that-is-long-enough!!", algorithm="HS256")
    response = app.post("/api/session/verify", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


# -- Microsoft (OIDC) ------------------------------------------------------------
@pytest.fixture
def rsa_key() -> tuple[str, dict[str, Any]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo)
    jwk_dict = jwk.construct(public.decode(), "RS256").to_dict()
    jwk_dict.update({"kid": "k1", "use": "sig"})
    return pem, jwk_dict


@pytest.fixture
def microsoft(monkeypatch: pytest.MonkeyPatch, rsa_key: tuple[str, dict[str, Any]]) -> Any:
    monkeypatch.setenv("ZZ2_MICROSOFT_ENABLED", "true")
    monkeypatch.setenv("ZZ2_MICROSOFT_TENANT", "organizations")
    monkeypatch.setenv("ZZ2_MICROSOFT_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("ZZ2_MICROSOFT_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("ZZ2_SAML_ALLOWED_DOMAINS", "zz2.com.br,mackenzista.com.br")
    from zz2.settings import get_settings
    get_settings.cache_clear()

    from zz2.services.microsoft import microsoft_signin
    microsoft_signin.reset()
    pem, public_jwk = rsa_key
    state: dict[str, Any] = {"claims": {}}

    class Resp:
        def __init__(self, status: int, data: dict[str, Any]) -> None:
            self.status_code, self._data = status, data

        def json(self) -> dict[str, Any]:
            return self._data

        def raise_for_status(self) -> None:
            assert self.status_code == 200

    class Client:
        def post(self, url: str, data: dict[str, Any], headers: Any = None) -> Resp:
            assert url.endswith("/oauth2/v2.0/token")
            assert data["code_verifier"] and data["client_secret"] == "client-secret"
            claims = {"aud": CLIENT_ID, "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
                      "tid": TENANT, "iat": int(time.time()), "exp": int(time.time()) + 600,
                      "sub": "pairwise-sub", "oid": "oid-123", "name": "Ana Souza",
                      "preferred_username": "ana@mackenzista.com.br",
                      "nonce": state["nonce"], **state["claims"]}
            token = jwt.encode(claims, pem, algorithm="RS256", headers={"kid": "k1"})
            return Resp(200, {"id_token": token})

        def get(self, url: str) -> Resp:
            assert url.endswith("/discovery/v2.0/keys")
            return Resp(200, {"keys": [public_jwk]})

    monkeypatch.setattr("zz2.services.microsoft.get_client", lambda: Client())
    yield state
    get_settings.cache_clear()


def _start(app: Any, state: dict[str, Any]) -> str:
    response = app.get("/api/auth/microsoft/login?next=/index.html", follow_redirects=False)
    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    state["nonce"] = query["nonce"][0]
    # The state cookie is Secure; the test client talks http, so carry it by hand.
    state["cookie"] = response.cookies.get("zz2_ms_state")
    return query["state"][0]


def _callback(app: Any, ms_state: str, cookie: str | None) -> str:
    response = app.get(f"/api/auth/microsoft/callback?code=abc&state={ms_state}",
                       headers={"Cookie": f"zz2_ms_state={cookie}"} if cookie else {},
                       follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"]


def test_microsoft_sign_in_provisions_and_signs_in(app: Any, fake_admin: Any,
                                                   microsoft: dict[str, Any]) -> None:
    location = _callback(app, _start(app, microsoft), microsoft['cookie'])
    assert location.startswith("/login.html#sso=")
    code = location.split("#sso=", 1)[1]
    body = app.post("/api/auth/sso/exchange", json={"code": code}).json()
    assert body["next"] == "/index.html"
    assert app.post("/api/session/verify",
                    headers={"Authorization": f"Bearer {body['token']}"}).status_code == 200
    assert fake_admin.find_by_email("ana@mackenzista.com.br") is not None
    # Single use.
    assert app.post("/api/auth/sso/exchange", json={"code": code}).status_code == 401


def test_microsoft_existing_account_keeps_its_role(app: Any, fake_admin: Any,
                                                    microsoft: dict[str, Any]) -> None:
    fake_admin.seed(email="ana@mackenzista.com.br", role="admin")
    location = _callback(app, _start(app, microsoft), microsoft['cookie'])
    code = location.split("#sso=", 1)[1]
    token = app.post("/api/auth/sso/exchange", json={"code": code}).json()["token"]
    assert jwt.get_unverified_claims(token)["app_metadata"]["role"] == "admin"


def test_microsoft_rejects_wrong_nonce(app: Any, microsoft: dict[str, Any]) -> None:
    ms_state = _start(app, microsoft)
    microsoft["claims"] = {"nonce": "tampered"}
    assert _callback(app, ms_state, microsoft['cookie']) == "/login.html#sso_error=rejected"


def test_microsoft_rejects_disallowed_domain(app: Any, microsoft: dict[str, Any]) -> None:
    ms_state = _start(app, microsoft)
    microsoft["claims"] = {"preferred_username": "x@gmail.com"}
    assert _callback(app, ms_state, microsoft['cookie']) == "/login.html#sso_error=denied"


def test_microsoft_rejects_unknown_state(app: Any, microsoft: dict[str, Any]) -> None:
    _start(app, microsoft)
    location = _callback(app, "not-a-real-state", microsoft["cookie"])
    assert location == "/login.html#sso_error=rejected"


def test_microsoft_routes_hidden_when_disabled(app: Any) -> None:
    assert app.get("/api/auth/microsoft/login", follow_redirects=False).status_code == 404


def test_microsoft_rejects_callback_from_another_browser(app: Any,
                                                         microsoft: dict[str, Any]) -> None:
    ms_state = _start(app, microsoft)
    assert _callback(app, ms_state, None) == "/login.html#sso_error=rejected"
