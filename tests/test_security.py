"""Regression tests for the vulnerabilities the monolith shipped with.

Each test names the specific weakness it pins closed. They are the ones worth
keeping forever: a refactor that reintroduces any of them fails here rather than
in production.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from tests.conftest import auth_header, make_token
from zz2.core.errors import PayloadTooLarge, ValidationError
from zz2.core.text import safe_filename, safe_key


# -- Static file serving -------------------------------------------------------
class TestStaticFileExposure:
    """``GET /server.py`` used to return the entire server source."""

    @pytest.mark.parametrize("path", [
        "/server.py",
        "/config.json",
        "/config.example.json",
        "/credentials.json",
        "/roles.json",
        "/sessions.json",
        "/supabase_setup.sql",
        "/deploy.sh",
        "/gitignore",
        "/.gitignore",
        "/.env",
        "/config.json.tmp.1234",
        "/pyproject.toml",
        "/requirements.txt",
    ])
    def test_sensitive_paths_are_not_served(self, app, path: str) -> None:
        assert app.get(path).status_code == 404

    @pytest.mark.parametrize("path", [
        "/static/../../server.py",
        "/static/..%2f..%2fserver.py",
        "/static/%2e%2e/%2e%2e/server.py",
        "/images/../../../etc/passwd",
        "/manuals/..%2F..%2Fconfig.json",
        "/static/./../zz2/settings.py",
    ])
    def test_traversal_attempts_are_refused(self, app, path: str) -> None:
        """404 rather than 403: a 403 confirms the target exists."""
        assert app.get(path, headers=auth_header()).status_code in (400, 404)

    def test_only_the_known_pages_are_served(self, app) -> None:
        assert app.get("/not-a-page.html").status_code == 404

    def test_media_requires_authentication(self, app) -> None:
        """Wiring diagrams are internal documentation; the monolith served them
        to anyone who could reach the port."""
        assert app.get("/images/ITZ-GX1-A/wiring.jpg").status_code == 401


# -- Token handling ------------------------------------------------------------
class TestTokenHandling:
    """A token in a query string lands in access logs, Referer headers and
    browser history."""

    def test_query_string_token_is_not_accepted(self, app) -> None:
        token = make_token(permissions=["tickets"])
        assert app.get(f"/api/tickets/recent?token={token}").status_code == 401

    def test_body_token_is_not_accepted(self, app) -> None:
        token = make_token()
        assert app.post("/verify", json={"token": token}).status_code == 401

    def test_bearer_header_is_accepted(self, app) -> None:
        response = app.post("/api/session/verify", headers=auth_header())
        assert response.status_code == 200
        assert response.json()["user"]["username"] == "tech@zz2.com.br"

    def test_malformed_authorization_header_is_rejected(self, app) -> None:
        for header in ({"Authorization": "Basic abc"},
                       {"Authorization": "Bearer"},
                       {"Authorization": "Bearer "},
                       {"Authorization": "abc"}):
            assert app.post("/api/session/verify", headers=header).status_code == 401

    def test_expired_token_is_rejected(self, app) -> None:
        header = {"Authorization": f"Bearer {make_token(expires_in=-60)}"}
        assert app.post("/api/session/verify", headers=header).status_code == 401

    def test_token_signed_with_the_wrong_secret_is_rejected(self, app) -> None:
        import time

        from jose import jwt

        forged = jwt.encode(
            {"sub": "x", "email": "x@y.z", "aud": "authenticated",
             "exp": int(time.time()) + 600,
             "app_metadata": {"role": "super_admin"}},
            "not-the-configured-secret",
            algorithm="HS256",
        )
        header = {"Authorization": f"Bearer {forged}"}
        assert app.post("/api/session/verify", headers=header).status_code == 401


class TestForeignTokens:
    """Only sessions this server issued are accepted. A Supabase-era token, an
    ES256 token or one signed with another key is rejected without a network call."""

    def test_es256_token_is_rejected(self, app) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from jose import jwt

        key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())
        token = jwt.encode({"iss": "zz2", "aud": "zz2", "sub": "u",
                            "exp": 9999999999, "jti": "x"}, key, algorithm="ES256")
        response = app.post("/api/session/verify",
                            headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401

    def test_supabase_style_token_is_rejected(self, app) -> None:
        import time

        from jose import jwt

        token = jwt.encode({"sub": "u", "aud": "authenticated",
                            "exp": int(time.time()) + 600,
                            "app_metadata": {"role": "super_admin"}},
                           "test-session-secret-value-at-least-32-chars", algorithm="HS256")
        response = app.post("/api/session/verify",
                            headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401


class TestMetadataAuthorization:
    """Roles must come from ``app_metadata``, never ``user_metadata``.

    ``user_metadata`` is writable by the user's own Supabase session, so a role
    read from there is a self-service promotion. The monolith read
    ``app_metadata.get('role') or user_metadata.get('role')``.
    """

    def test_user_metadata_role_is_ignored(self, app) -> None:
        import os
        import time

        from jose import jwt

        token = jwt.encode(
            {
                "iss": "zz2", "aud": "zz2", "jti": "metadata-test",
                "sub": "u", "email": "u@zz2.com.br",
                "exp": int(time.time()) + 600,
                "app_metadata": {},
                # A user-writable claim asking for the keys to the building.
                "user_metadata": {"role": "super_admin",
                                  "permissions": ["system_settings"]},
            },
            os.environ["ZZ2_SESSION_SECRET"],
            algorithm="HS256",
        )
        response = app.get("/api/system/info",
                           headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403

    def test_unknown_role_grants_nothing(self, app) -> None:
        """A token naming a deleted role must fail closed, not fall back."""
        header = auth_header(role="role_that_was_deleted")
        assert app.get("/api/system/info", headers=header).status_code == 403
        assert app.get("/api/users", headers=header).status_code == 403


# -- Endpoint authorization ----------------------------------------------------
class TestEndpointAuthorization:
    """Every API route requires a principal, and the right permission."""

    @pytest.mark.parametrize(("method", "path"), [
        ("get", "/api/analytics"),
        ("get", "/api/analytics/store"),
        ("get", "/api/catalog"),
        ("get", "/api/customers"),
        ("get", "/api/draft"),
        ("get", "/api/kit/coverage"),
        ("get", "/api/roles"),
        ("get", "/api/stats"),
        ("get", "/api/system/info"),
        ("get", "/api/tickets/recent"),
        ("get", "/api/users"),
        ("post", "/api/kit/save"),
        ("post", "/api/system/backup"),
        ("post", "/api/tickets"),
        ("post", "/api/upload"),
    ])
    def test_unauthenticated_requests_are_rejected(self, app, method: str, path: str) -> None:
        response = (app.get(path) if method == "get"
                    else app.post(path, json={}))
        assert response.status_code == 401, f"{method.upper()} {path} was not gated"

    def test_config_is_intentionally_public(self, app) -> None:
        """The sign-in page needs it before any session exists."""
        response = app.get("/api/config")
        assert response.status_code == 200
        assert "microsoft_enabled" in response.json()

    def test_config_never_exposes_a_secret(self, app) -> None:
        body = app.get("/api/config").text
        for secret in ("test-session-secret-value-at-least-32-chars",):
            assert secret not in body

    def test_catalog_now_requires_authentication(self, app) -> None:
        """The monolith served the kit catalogue and every diagnostic tree
        without a session."""
        assert app.get("/api/catalog").status_code == 401
        assert app.get("/api/catalog", headers=auth_header()).status_code == 200

    def test_permission_is_enforced_not_just_authentication(self, app) -> None:
        tech = auth_header(role="tech", permissions=[])
        assert app.get("/api/system/info", headers=tech).status_code == 403
        assert app.get("/api/users", headers=tech).status_code == 403
        assert app.get("/api/roles", headers=tech).status_code == 403

    def test_system_settings_alone_does_not_permit_a_restore(self, app) -> None:
        """Restores overwrite live state, so they additionally require root."""
        header = auth_header(role="ops", permissions=["system_settings"])
        response = app.post("/api/system/restore",
                            json={"name": "20260101_000000", "files": "all"},
                            headers=header)
        assert response.status_code == 403


# -- Response headers ----------------------------------------------------------
class TestSecurityHeaders:
    def test_hardening_headers_are_present(self, app) -> None:
        headers = app.get("/api/config").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    def test_no_external_script_host_is_allowed(self, app) -> None:
        """Both CDN dependencies are vendored, so a compromised CDN response can
        no longer execute on this origin."""
        policy = app.get("/api/config").headers["Content-Security-Policy"]
        script_src = next(part for part in policy.split("; ")
                          if part.startswith("script-src"))
        assert "cdn.jsdelivr.net" not in script_src
        assert "cdnjs.cloudflare.com" not in script_src
        assert script_src == "script-src 'self' 'unsafe-inline'"

    def test_connect_src_is_limited_to_self(self, app) -> None:
        """The directive that stops an injected script phoning home. Since the
        move off Supabase every call goes to this origin."""
        policy = app.get("/api/config").headers["Content-Security-Policy"]
        connect_src = next(part for part in policy.split("; ")
                           if part.startswith("connect-src"))
        assert connect_src == "connect-src 'self'"
        assert "supabase" not in policy

    def test_frames_are_limited_to_self_and_official_manuals(self, app) -> None:
        policy = app.get("/api/config").headers["Content-Security-Policy"]
        frame_src = next(part for part in policy.split("; ")
                         if part.startswith("frame-src"))
        assert frame_src == "frame-src 'self' blob: https://drive.google.com"

    def test_cors_is_not_wildcarded(self, app) -> None:
        """The monolith sent ``Access-Control-Allow-Origin: *`` on every
        response, authenticated ones included."""
        headers = app.get("/api/config", headers={"Origin": "https://evil.example"}).headers
        assert headers.get("Access-Control-Allow-Origin") != "*"

    def test_api_responses_are_not_cacheable(self, app) -> None:
        headers = app.get("/api/config").headers
        assert "no-store" in headers["Cache-Control"]

    def test_every_response_carries_a_request_id(self, app) -> None:
        assert app.get("/api/config").headers["X-Request-ID"]


# -- Error disclosure ----------------------------------------------------------
class TestErrorDisclosure:
    """``'Server error: %s' % e`` leaked paths, field names and upstream
    messages."""

    def test_not_found_does_not_echo_internals(self, app) -> None:
        body = app.get("/api/tickets/does-not-exist", headers=auth_header()).text
        for leak in ("Traceback", "/Users/", "zz2/services", "File \""):
            assert leak not in body

    def test_validation_errors_do_not_echo_the_submitted_value(self, app) -> None:
        header = auth_header(role="admin", permissions=["manage_users"])
        response = app.post(
            "/api/users",
            json={"email": "not-an-email", "display": "X",
                  "password": "hunter2-secret-value", "role": "tech"},
            headers=header,
        )
        assert response.status_code == 422
        assert "hunter2-secret-value" not in response.text


# -- Input sanitisation --------------------------------------------------------
class TestSanitisers:
    @pytest.mark.parametrize("value", [
        "../../etc", "..", ".", "a/b", "a\\b", ".hidden", "", "  ",
        "with\x00null", "x" * 65, "a..b",
    ])
    def test_safe_key_rejects_unsafe_values(self, value: str) -> None:
        assert safe_key(value) == ""

    @pytest.mark.parametrize("value", ["ITZ-GX1-A", "DZZ AUDI", "IT2_INF.Q70", "a"])
    def test_safe_key_accepts_real_kit_keys(self, value: str) -> None:
        assert safe_key(value) == value

    @pytest.mark.parametrize(("given", "expected"), [
        ("../../etc/passwd", "passwd"),
        ("/absolute/path.pdf", "path.pdf"),
        ("..", "file"),
        ("....//..//x.pdf", "x.pdf"),
        ("manual (v2).pdf", "manual__v2_.pdf"),
    ])
    def test_safe_filename_reduces_to_one_component(self, given: str, expected: str) -> None:
        assert safe_filename(given) == expected

    def test_safe_filename_caps_length(self) -> None:
        assert len(safe_filename("a" * 500 + ".pdf")) <= 120


# -- Upload validation ---------------------------------------------------------
class TestUploadValidation:
    """The monolith checked the declared type and the extension, never the bytes.

    Media is served from the application's own origin, so a file whose contents
    are HTML is stored cross-site scripting.
    """

    def _upload(self, **kwargs):
        from zz2.services.uploads import upload_service

        return upload_service.store(actor="t@zz2.com.br", **kwargs)

    def test_html_disguised_as_an_image_is_rejected(self, data_dir) -> None:
        payload = base64.b64encode(b"<script>fetch('//evil')</script>").decode()
        with pytest.raises(ValidationError, match="not a valid"):
            self._upload(kit_key="ITZ-X", kind="wiring", encoded=payload)

    def test_html_disguised_as_a_pdf_is_rejected(self, data_dir) -> None:
        payload = base64.b64encode(b"<html><body>hi</body></html>").decode()
        with pytest.raises(ValidationError, match="not a valid"):
            self._upload(kit_key="ITZ-X", kind="manual", encoded=payload,
                         filename="innocent.pdf")

    def test_a_real_jpeg_is_accepted(self, data_dir) -> None:
        payload = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 128).decode()
        stored = self._upload(kit_key="ITZ-X", kind="wiring", encoded=payload)
        assert stored.filename == "wiring.jpg"

    def test_a_real_pdf_is_accepted(self, data_dir) -> None:
        payload = base64.b64encode(b"%PDF-1.7\n" + b"\x00" * 128).decode()
        stored = self._upload(kit_key="ITZ-X", kind="manual", encoded=payload,
                              filename="install.pdf")
        assert stored.filename == "install.pdf"

    def test_riff_that_is_not_webp_is_rejected(self, data_dir) -> None:
        """RIFF is a container family; only WebP is an image."""
        payload = base64.b64encode(b"RIFF____AVI LIST" + b"\x00" * 64).decode()
        with pytest.raises(ValidationError):
            self._upload(kit_key="ITZ-X", kind="misc", encoded=payload)

    def test_image_filename_is_chosen_by_the_server(self, data_dir) -> None:
        """A client-supplied name never reaches the filesystem for images."""
        payload = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 64).decode()
        stored = self._upload(kit_key="ITZ-X", kind="dip", encoded=payload,
                              filename="../../evil.html")
        assert stored.filename == "dip.jpg"

    def test_traversal_in_a_kit_key_is_rejected(self, data_dir) -> None:
        payload = base64.b64encode(b"%PDF-1.7\n").decode()
        with pytest.raises(ValidationError, match="kit key"):
            self._upload(kit_key="../../etc", kind="manual", encoded=payload)

    def test_oversized_uploads_are_rejected(self, settings_env, data_dir,
                                            monkeypatch) -> None:
        """The effective limit is the lower of the format cap and the setting."""
        from zz2.settings import get_settings

        monkeypatch.setenv("ZZ2_MAX_UPLOAD_BYTES", "64")
        get_settings.cache_clear()

        payload = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 4096).decode()
        with pytest.raises(PayloadTooLarge):
            self._upload(kit_key="ITZ-X", kind="wiring", encoded=payload)

    def test_undecodable_base64_is_rejected(self, data_dir) -> None:
        with pytest.raises(ValidationError, match="decoded"):
            self._upload(kit_key="ITZ-X", kind="wiring", encoded="not!valid!base64!")

    def test_unknown_kind_is_rejected(self, data_dir) -> None:
        payload = base64.b64encode(b"\xff\xd8\xff\xe0").decode()
        with pytest.raises(ValidationError, match="Unsupported"):
            self._upload(kit_key="ITZ-X", kind="executable", encoded=payload)


# -- Request limits ------------------------------------------------------------
class TestRequestLimits:
    def test_oversized_body_is_refused(self, app) -> None:
        response = app.post(
            "/api/tickets",
            content=b"x" * 32,
            headers={**auth_header(permissions=["create_tickets"]),
                     "Content-Length": str(64 * 1024 * 1024),
                     "Content-Type": "application/json"},
        )
        assert response.status_code == 413

    def test_invalid_content_length_is_refused(self, app) -> None:
        response = app.post(
            "/api/tickets",
            content=b"{}",
            headers={**auth_header(permissions=["create_tickets"]),
                     "Content-Length": "not-a-number",
                     "Content-Type": "application/json"},
        )
        assert response.status_code == 400


# -- Log redaction -------------------------------------------------------------
class TestLogRedaction:
    """The monolith had a ``_redact`` helper callers had to remember to use, and
    leaked the secret on the one path that forgot."""

    @pytest.mark.parametrize("message", [
        "PROXY_GET: https://script.google.com/exec?action=recent&key=s3cr3t-value",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig",
        'failed with {"apikey": "sb_secret_abcdefghijkl"}',
        "password=hunter2hunter2",
        "token: eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9",
    ])
    def test_credentials_are_scrubbed(self, message: str) -> None:
        from zz2.core.logging import scrub

        cleaned = scrub(message)
        for secret in ("s3cr3t-value", "hunter2hunter2", "sb_secret_abcdefghijkl"):
            assert secret not in cleaned
        assert "eyJ" not in cleaned or "[REDACTED]" in cleaned


class TestAssetCaching:
    """A deploy's JavaScript must reach the browser with its HTML.

    The pages are revalidated by ETag, but their scripts are addressed by stable
    URLs with no build hash. A long ``max-age`` on those means new HTML arrives
    immediately while the old JavaScript is served from cache — a page running a
    mix of two builds, which is worse than either.
    """

    def test_static_assets_are_revalidated_not_cached_for_an_hour(self, app) -> None:
        headers = app.get("/static/js/shared/session.js").headers
        assert headers["Cache-Control"] == "no-cache"
        assert "max-age" not in headers["Cache-Control"]
        assert headers["ETag"]

    def test_an_unchanged_asset_returns_304(self, app) -> None:
        """Revalidation must be cheap, or no-cache means re-downloading."""
        first = app.get("/static/js/shared/session.js")
        assert first.status_code == 200

        second = app.get("/static/js/shared/session.js",
                         headers={"If-None-Match": first.headers["ETag"]})
        assert second.status_code == 304
        assert not second.content

    def test_a_changed_asset_returns_the_new_body(self, app, data_dir) -> None:
        from zz2.paths import STATIC_DIR

        target = STATIC_DIR / "css" / "login.css"
        original = target.read_text(encoding="utf-8")
        first = app.get("/static/css/login.css")
        try:
            target.write_text(original + "\n/* changed */\n", encoding="utf-8")
            second = app.get("/static/css/login.css",
                             headers={"If-None-Match": first.headers["ETag"]})
            assert second.status_code == 200
            assert "/* changed */" in second.text
        finally:
            target.write_text(original, encoding="utf-8")

    def test_pages_are_revalidated_by_content(self, app) -> None:
        """Content-derived, not mtime: a deploy that preserves timestamps must
        still serve the new page."""
        first = app.get("/login.html")
        assert first.headers["Cache-Control"] == "no-cache"

        second = app.get("/login.html",
                         headers={"If-None-Match": first.headers["ETag"]})
        assert second.status_code == 304


class TestStoredDataIsNotTrusted:
    """State files are inputs too.

    ``kits.json`` and a backup manifest are operator-editable and restorable, so
    a hostile value can reach a path join without any HTTP request being
    involved. Both places validate rather than trusting their own storage.
    """

    def test_mirror_skips_a_kit_whose_stored_key_is_unsafe(self, data_dir) -> None:
        from zz2.repositories.kits import KitCatalogRepository
        from zz2.services.mirror import MirrorService

        catalog = KitCatalogRepository()
        # Written directly, bypassing the repository's own validation — exactly
        # what a hand-edited file or an older build could leave behind.
        catalog.kits.write({
            "../../etc": {"vehicle": "hostile"},
            "ITZ-OK": {"vehicle": "fine"},
        })
        catalog.trees.write({"../../etc": [], "ITZ-OK": []})

        _generation, batches = MirrorService(catalog)._snapshot()
        manuals = next(rows for table, _conflict, rows in batches
                       if table == "manual_assets")

        assert all(".." not in row["kit_key"] for row in manuals)
        assert all(".." not in row["local_path"] for row in manuals)

    def test_restore_rejects_a_manifest_path_that_escapes(self, data_dir) -> None:
        from zz2.services.backup import BackupService

        service = BackupService()

        # "/etc/passwd" is the important one: pathlib discards the left operand
        # of a join when the right one is absolute, so DATA_DIR / "/etc/passwd"
        # is "/etc/passwd" — an arbitrary file write.
        for hostile in ("../../etc/passwd", "/etc/passwd", "//etc/passwd",
                        "backups/x", "cache/x", "logs/x", ".tmp/x",
                        "a/../../b", "a/./b", "", "  ", "x\x00y"):
            assert service._safe_relative(hostile) is None, hostile

        assert service._safe_relative("store/kits.json") == "store/kits.json"
        assert service._safe_relative("roles.json") == "roles.json"
        assert service._safe_relative("media/images/ITZ-X/wiring.jpg") == \
            "media/images/ITZ-X/wiring.jpg"

    def test_restore_refuses_an_absolute_manifest_path(self, data_dir) -> None:
        """End to end: a snapshot whose manifest names an absolute path must not
        write outside the data directory."""
        import json

        from zz2.core.jsonstore import write_json
        from zz2.paths import BACKUPS_DIR
        from zz2.services.backup import BackupService

        snapshot = BACKUPS_DIR / "20260101_000000"
        snapshot.mkdir(parents=True)
        (snapshot / "payload").write_text("pwned", encoding="utf-8")
        write_json(snapshot / "manifest.json", {
            "created": "2026-01-01T00:00:00Z", "label": "hostile",
            "ts": "20260101_000000", "file_count": 1, "total_bytes": 5,
            "files": [{"path": "/tmp/zz2-should-not-exist", "size": 5, "sha256": ""}],
        })
        assert json  # the import is used by write_json's serialisation path

        with pytest.raises(Exception) as caught:
            BackupService().restore("20260101_000000", "all", actor="root")
        assert "No valid files" in str(caught.value)
        assert not Path("/tmp/zz2-should-not-exist").exists()
