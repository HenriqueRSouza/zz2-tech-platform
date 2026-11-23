"""SAML sign-in with Microsoft Entra ID, end to end through the HTTP layer."""

from __future__ import annotations

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient
from lxml import etree

from tests import saml_idp
from tests.conftest import auth_header

SESSION_SECRET = "saml-session-secret-for-tests-0123456789abcdef"  # noqa: S105


@pytest.fixture(scope="module")
def idp_keys():
    return saml_idp.make_keypair()


@pytest.fixture
def saml_env(monkeypatch, settings_env, idp_keys):
    from zz2.settings import get_settings

    _key, cert = idp_keys
    for name, value in {
        "ZZ2_PUBLIC_URL": saml_idp.SP_PUBLIC_URL,
        "ZZ2_SAML_ENABLED": "true",
        "ZZ2_SAML_IDP_ENTITY_ID": saml_idp.IDP_ENTITY,
        "ZZ2_SAML_IDP_SSO_URL": saml_idp.IDP_SSO_URL,
        "ZZ2_SAML_IDP_X509_CERT": saml_idp.cert_body(cert),
        "ZZ2_SAML_SESSION_SECRET": SESSION_SECRET,
        "ZZ2_SAML_ALLOWED_DOMAINS": "mackenzista.com.br,mackenzie.br",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def browser(app, saml_env) -> TestClient:
    """An https client — the state cookie is Secure and would not be sent over http."""
    return TestClient(app.app, base_url="https://testserver")


def start(browser: TestClient, next_path: str = "/index.html") -> str:
    response = browser.get("/api/auth/saml/login", params={"next": next_path},
                           follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith(saml_idp.IDP_SSO_URL + "?SAMLRequest=")
    return saml_idp.request_id_from_redirect(response.headers["location"])


def post_response(browser: TestClient, document: bytes):
    return browser.post("/api/auth/saml/acs",
                        data={"SAMLResponse": saml_idp.encode(document)},
                        follow_redirects=False)


def sign_in(browser: TestClient, idp_keys, **overrides) -> dict:
    key, cert = idp_keys
    request_id = start(browser)
    response = post_response(browser, saml_idp.build_response(
        key_pem=key, cert_pem=cert, in_response_to=request_id, **overrides))
    assert response.status_code == 303
    location = response.headers["location"]
    match = re.fullmatch(r"/login\.html#sso=([\w-]+)", location)
    assert match, location
    exchanged = browser.post("/api/auth/saml/exchange", json={"code": match.group(1)})
    assert exchanged.status_code == 200
    return exchanged.json()


def rejected(response) -> bool:
    return (response.status_code == 303
            and response.headers["location"] == "/login.html#sso_error=rejected")


@pytest.fixture
def reasons(monkeypatch) -> list[str]:
    """Why each response was refused, as written to the audit log."""
    import zz2.api.routers.saml as saml_router

    seen: list[str] = []
    real_audit = saml_router.audit

    def capture(event: str, **fields):
        if event in ("saml.rejected", "saml.denied"):
            seen.append(fields["reason"])
        real_audit(event, **fields)

    monkeypatch.setattr(saml_router, "audit", capture)
    return seen


class TestHappyPath:
    def test_university_account_signs_in_and_gets_a_working_session(self, browser, idp_keys):
        body = sign_in(browser, idp_keys)
        assert body["next"] == "/index.html"

        verify = browser.post("/api/session/verify",
                              headers={"Authorization": f"Bearer {body['token']}"})
        assert verify.status_code == 200
        user = verify.json()["user"]
        assert user["username"] == "aluno@mackenzista.com.br"
        assert user["display"] == "Aluno Teste"
        assert user["role"] == "tech"

    @pytest.mark.parametrize(("sign", "sign_response"), [(False, True), (True, True)],
                             ids=["response-only", "response-and-assertion"])
    def test_every_entra_signing_option_is_accepted(self, browser, idp_keys,
                                                     sign, sign_response):
        body = sign_in(browser, idp_keys, sign=sign, sign_response=sign_response)
        assert body["token"]

    def test_metadata_names_our_entity_and_acs(self, browser):
        response = browser.get("/api/auth/saml/metadata")
        assert response.status_code == 200
        root = etree.fromstring(response.content)
        assert root.get("entityID") == saml_idp.SP_ENTITY
        assert saml_idp.ACS_URL.encode() in response.content

    def test_public_config_advertises_saml(self, browser):
        assert browser.get("/api/config").json()["saml_enabled"] is True

    def test_logout_revokes_the_saml_session(self, browser, idp_keys):
        token = sign_in(browser, idp_keys)["token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert browser.post("/api/session/logout", headers=headers).status_code == 200
        assert browser.post("/api/session/verify", headers=headers).status_code == 401

    def test_code_is_single_use(self, browser, idp_keys):
        key, cert = idp_keys
        request_id = start(browser)
        location = post_response(browser, saml_idp.build_response(
            key_pem=key, cert_pem=cert, in_response_to=request_id)).headers["location"]
        code = location.split("sso=", 1)[1]
        assert browser.post("/api/auth/saml/exchange", json={"code": code}).status_code == 200
        assert browser.post("/api/auth/saml/exchange", json={"code": code}).status_code == 401


class TestRejections:
    def test_replayed_response_is_refused(self, browser, idp_keys, reasons):
        key, cert = idp_keys
        request_id = start(browser)
        document = saml_idp.build_response(key_pem=key, cert_pem=cert,
                                           in_response_to=request_id)
        assert post_response(browser, document).status_code == 303
        start(browser)  # fresh cookie, same captured response
        assert rejected(post_response(browser, document))
        assert reasons == ["unknown_or_reused_request"]

    def test_tampered_assertion_is_refused(self, browser, idp_keys, reasons):
        key, cert = idp_keys
        request_id = start(browser)
        document = saml_idp.build_response(key_pem=key, cert_pem=cert,
                                           in_response_to=request_id)
        forged = document.replace(b"aluno@mackenzista.com.br", b"reitor@mackenzista.com.br")
        assert rejected(post_response(browser, forged))
        assert reasons == ["signature_invalid"]

    def test_signature_from_another_key_is_refused(self, browser, reasons):
        key, cert = saml_idp.make_keypair("attacker")
        request_id = start(browser)
        assert rejected(post_response(browser, saml_idp.build_response(
            key_pem=key, cert_pem=cert, in_response_to=request_id)))
        assert reasons == ["signature_invalid"]

    def test_unsigned_assertion_is_refused(self, browser, idp_keys, reasons):
        key, cert = idp_keys
        request_id = start(browser)
        assert rejected(post_response(browser, saml_idp.build_response(
            key_pem=key, cert_pem=cert, in_response_to=request_id, sign=False)))
        assert reasons == ["signature_invalid"]

    def test_signature_wrapping_with_an_injected_assertion_is_refused(self, browser, idp_keys,
                                                                       reasons):
        """XSW: keep the genuine signed assertion, add an attacker's one first."""
        key, cert = idp_keys
        request_id = start(browser)
        genuine = saml_idp.build_response(key_pem=key, cert_pem=cert,
                                          in_response_to=request_id)
        evil = saml_idp.build_response(key_pem=key, cert_pem=cert, in_response_to=request_id,
                                       email="admin@mackenzista.com.br", sign=False)
        evil_assertion = etree.fromstring(evil).find(
            "{urn:oasis:names:tc:SAML:2.0:assertion}Assertion")
        root = etree.fromstring(genuine)
        root.insert(2, evil_assertion)
        assert rejected(post_response(browser, etree.tostring(root)))
        assert reasons == ["assertion_count"]

    @pytest.mark.parametrize(("override", "reason"), [
        ({"audience": "https://another-app.example/"}, "audience_mismatch"),
        ({"issuer": "https://sts.windows.net/another-tenant/"}, "issuer_mismatch"),
        ({"recipient": "https://another-app.example/acs"}, "destination_mismatch"),
        ({"now": dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)}, "expired"),
        ({"status": "urn:oasis:names:tc:SAML:2.0:status:Requester"}, "idp_status_Requester"),
    ], ids=["audience", "issuer", "recipient", "expired", "idp-error"])
    def test_condition_violations_are_refused(self, browser, idp_keys, reasons,
                                              override, reason):
        key, cert = idp_keys
        request_id = start(browser)
        assert rejected(post_response(browser, saml_idp.build_response(
            key_pem=key, cert_pem=cert, in_response_to=request_id, **override)))
        assert reasons == [reason]

    def test_unsolicited_response_is_refused(self, browser, idp_keys, reasons):
        key, cert = idp_keys
        start(browser)
        assert rejected(post_response(browser, saml_idp.build_response(
            key_pem=key, cert_pem=cert, in_response_to=None)))
        assert reasons == ["unsolicited_response"]

    def test_response_delivered_to_a_different_browser_is_refused(self, app, browser, idp_keys,
                                                                   reasons):
        """Login CSRF: the attacker's own valid response, posted from the victim's browser."""
        key, cert = idp_keys
        request_id = start(browser)
        victim = TestClient(app.app, base_url="https://testserver")
        assert rejected(post_response(victim, saml_idp.build_response(
            key_pem=key, cert_pem=cert, in_response_to=request_id)))
        assert reasons == ["browser_binding_mismatch"]

    def test_doctype_is_refused(self, browser, reasons):
        start(browser)
        bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><r>&a;</r>'
        assert rejected(post_response(browser, bomb))
        assert reasons == ["dtd_forbidden"]


class TestAuthorization:
    def test_domain_outside_the_allowlist_is_denied(self, browser, idp_keys):
        key, cert = idp_keys
        request_id = start(browser)
        response = post_response(browser, saml_idp.build_response(
            key_pem=key, cert_pem=cert, in_response_to=request_id, email="x@gmail.com"))
        assert response.headers["location"] == "/login.html#sso_error=denied"

    def test_existing_account_keeps_its_role(self, browser, idp_keys, fake_admin):
        fake_admin.seed(email="aluno@mackenzista.com.br", role="admin")
        token = sign_in(browser, idp_keys)["token"]
        user = browser.post("/api/session/verify",
                            headers={"Authorization": f"Bearer {token}"}).json()["user"]
        assert user["role"] == "admin"

    def test_deactivated_account_cannot_walk_back_in(self, browser, idp_keys, fake_admin):
        fake_admin.seed(email="aluno@mackenzista.com.br", role="admin", active=False)
        key, cert = idp_keys
        request_id = start(browser)
        response = post_response(browser, saml_idp.build_response(
            key_pem=key, cert_pem=cert, in_response_to=request_id))
        assert response.headers["location"] == "/login.html#sso_error=denied"

    def test_group_mapping_picks_the_highest_ranked_role(self, browser, idp_keys, monkeypatch):
        from zz2.settings import get_settings

        monkeypatch.setenv("ZZ2_SAML_GROUP_ROLE_MAP", "grp-techs=tech,grp-staff=admin")
        get_settings.cache_clear()
        token = sign_in(browser, idp_keys, groups=("grp-techs", "grp-staff"))["token"]
        user = browser.post("/api/session/verify",
                            headers={"Authorization": f"Bearer {token}"}).json()["user"]
        assert user["role"] == "admin"


class TestSessionTokens:
    def test_token_signed_with_another_key_is_refused(self, browser):
        from jose import jwt

        forged = jwt.encode({"iss": "zz2-saml", "aud": "zz2", "sub": "saml:x", "jti": "1",
                             "exp": 9_999_999_999,
                             "app_metadata": {"role": "super_admin"}},
                            "a-guessed-secret", algorithm="HS256")
        response = browser.post("/api/session/verify",
                                headers={"Authorization": f"Bearer {forged}"})
        assert response.status_code == 401

    def test_saml_session_dies_when_saml_is_switched_off(self, browser, idp_keys, monkeypatch):
        from zz2.settings import get_settings

        token = sign_in(browser, idp_keys)["token"]
        monkeypatch.setenv("ZZ2_SAML_ENABLED", "false")
        get_settings.cache_clear()
        from zz2.services.auth import _principal_cache

        _principal_cache.clear()
        response = browser.post("/api/session/verify",
                                headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401

    def test_supabase_tokens_still_work(self, browser):
        assert browser.post("/api/session/verify",
                            headers=auth_header()).status_code == 200


class TestConfiguration:
    def test_endpoints_do_not_exist_while_disabled(self, app):
        for method, path in (("GET", "/api/auth/saml/metadata"),
                             ("GET", "/api/auth/saml/login"),
                             ("POST", "/api/auth/saml/acs")):
            assert app.request(method, path, follow_redirects=False).status_code == 404

    @pytest.mark.parametrize("value", ["//evil.example/x", "https://evil.example",
                                       "/\\evil.example", "javascript:alert(1)"])
    def test_next_cannot_redirect_off_site(self, value):
        from zz2.api.routers.saml import DEFAULT_NEXT, safe_next_path

        assert safe_next_path(value) == DEFAULT_NEXT

    def test_enabled_without_a_certificate_refuses_to_boot(self, monkeypatch, saml_env):
        from pydantic import ValidationError

        from zz2.settings import Settings

        monkeypatch.setenv("ZZ2_SAML_IDP_X509_CERT", "")
        with pytest.raises(ValidationError, match="ZZ2_SAML_IDP_X509_CERT"):
            Settings()

    def test_session_secret_must_be_long_enough(self, monkeypatch, saml_env):
        from pydantic import ValidationError

        from zz2.settings import Settings

        monkeypatch.setenv("ZZ2_SESSION_SECRET", "short")
        monkeypatch.setenv("ZZ2_SAML_SESSION_SECRET", "")
        with pytest.raises(ValidationError, match="at least 32"):
            Settings()
