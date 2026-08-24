"""Immutable deployment settings, loaded from the environment.

The monolith kept every secret in ``config.json`` — a file that lived *inside the
web root* and was protected only by a filename denylist in the static handler. A
crash between ``open(tmp)`` and ``os.rename`` left ``config.json.tmp.<pid>``
behind, and that name was not on the denylist, so the Apps Script secret and the
Supabase service key were downloadable by anyone who guessed the PID.

Secrets now come from the process environment (systemd ``EnvironmentFile`` or
Docker secrets) and never touch the web root. Operator-tunable, non-secret values
still live in ``config.json`` and are handled by
:class:`zz2.services.runtime_config.RuntimeConfigService`.

Access is through :func:`get_settings`, memoised so the object behaves as a
singleton without a mutable global.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Everything fixed for the lifetime of the process."""

    model_config = SettingsConfigDict(
        env_prefix="ZZ2_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # -- Identity / build ------------------------------------------------------
    build: str = Field(default="dev", description="Stamped by the deploy pipeline.")
    environment: Literal["development", "staging", "production"] = "production"

    # -- HTTP ------------------------------------------------------------------
    host: str = "127.0.0.1"
    """Bind address. Defaults to loopback: nginx terminates TLS and proxies in,
    so the application must never be reachable directly from the internet."""

    port: int = 8080

    public_url: str = Field(
        default="http://localhost:8080",
        description="Canonical external origin, e.g. https://suporte.zz2.com.br.",
    )

    cors_origins: tuple[str, ...] = ()
    """Explicit origin allowlist. Empty means same-origin only, which is correct
    for this app: the pages are served by the very process that serves the API.
    The monolith sent ``Access-Control-Allow-Origin: *`` on every response,
    including authenticated ones."""

    trusted_hosts: tuple[str, ...] = ("*",)
    """Host header allowlist. Set to real hostnames in production to block
    Host-header poisoning through the reverse proxy."""

    behind_proxy: bool = True
    """When true, ``X-Forwarded-For`` / ``X-Forwarded-Proto`` from the reverse
    proxy are trusted for client IP and scheme. Only ever enable this when a
    proxy you control actually sets them — otherwise clients can spoof their IP
    and defeat rate limiting."""

    # -- Body / upload limits --------------------------------------------------
    max_body_bytes: int = 8 * 1024 * 1024
    max_upload_bytes: int = 25 * 1024 * 1024

    # -- Rate limiting ---------------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 240
    rate_limit_window_seconds: int = 60
    rate_limit_write_requests: int = 60
    """Separate, tighter bucket for mutating requests."""

    # -- Supabase (authentication + mirror) ------------------------------------
    supabase_url: str = ""
    supabase_publishable_key: str = ""
    """Browser-safe anon/publishable key. Sent to the client by design."""

    supabase_service_key: SecretStr = SecretStr("")
    """Server-only. Bypasses RLS — must never reach a browser."""

    supabase_jwt_secret: SecretStr = SecretStr("")
    """HS256 secret for local JWT verification. When set, access tokens are
    verified in-process instead of by calling ``/auth/v1/user`` on every request,
    which removes a network round-trip from the hot path and a hard dependency on
    Supabase being reachable."""

    supabase_jwt_audience: str = "authenticated"
    supabase_schema: str = "techSupport"
    customer_history_schema: str = "zoho"

    # -- Supabase Storage ------------------------------------------------------
    supabase_storage_bucket: str = "zz2-manuals"
    """Private bucket that holds kit PDF manuals."""

    enable_supabase_storage: bool = False
    """Use Supabase Storage as the durable source for PDF manuals.

    The application still writes a local cache so backups and an upstream
    outage do not make a manual that was already read locally unavailable.
    """

    # -- Google Apps Script (legacy ticket sheet) ------------------------------
    appscript_url: str = ""
    appscript_secret: SecretStr = SecretStr("")

    # -- Observability ---------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    """Structured JSON lines. The monolith suppressed access logs entirely
    (``log_message`` was a no-op), leaving no audit trail."""

    # -- Feature flags ---------------------------------------------------------
    enable_appscript_sync: bool = True
    enable_supabase_mirror: bool = True
    enable_background_workers: bool = True

    @field_validator("cors_origins", "trusted_hosts", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept ``a,b,c`` from an env var as well as a real sequence."""
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("public_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _production_guardrails(self) -> Settings:
        """Refuse to boot a production process that is obviously unsafe.

        Failing at startup is the whole point: a misconfigured deploy should
        never reach the "serving requests" state, because by then the only signal
        is a subtle one (permissive CORS, missing auth) that nobody notices.
        """
        if self.environment != "production":
            return self

        problems: list[str] = []
        if not self.supabase_url:
            problems.append("ZZ2_SUPABASE_URL is required (authentication depends on it)")
        if not self.supabase_publishable_key:
            problems.append("ZZ2_SUPABASE_PUBLISHABLE_KEY is required")
        if "*" in self.cors_origins:
            problems.append("ZZ2_CORS_ORIGINS must not contain '*' in production")
        if self.public_url.startswith("http://") and "localhost" not in self.public_url:
            problems.append("ZZ2_PUBLIC_URL must use https in production")
        if problems:
            raise ValueError("Unsafe production configuration:\n  - " + "\n  - ".join(problems))
        return self

    # -- Convenience -----------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def supabase_rest_url(self) -> str:
        return self.supabase_url.rstrip("/") + "/rest/v1/"

    @property
    def supabase_auth_url(self) -> str:
        return self.supabase_url.rstrip("/") + "/auth/v1/"

    @property
    def supabase_storage_url(self) -> str:
        return self.supabase_url.rstrip("/") + "/storage/v1/"

    def public_config(self) -> dict[str, str]:
        """The only settings the browser is allowed to see.

        An explicit allowlist, not a filtered dump: adding a secret to
        ``Settings`` can never leak it through this endpoint by accident.
        """
        return {
            "build": self.build,
            "environment": self.environment,
            "supabase_url": self.supabase_url,
            "supabase_publishable_key": self.supabase_publishable_key,
            "supabase_schema": self.supabase_schema,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (memoised, so importers stay decoupled)."""
    return Settings()
