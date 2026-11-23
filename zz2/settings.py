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
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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

    cors_origins: Annotated[tuple[str, ...], NoDecode] = ()
    """Explicit origin allowlist. Empty means same-origin only, which is correct
    for this app: the pages are served by the very process that serves the API.
    The monolith sent ``Access-Control-Allow-Origin: *`` on every response,
    including authenticated ones."""

    trusted_hosts: Annotated[tuple[str, ...], NoDecode] = ("*",)
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

    # -- Database (Azure Database for PostgreSQL) ------------------------------
    database_url: SecretStr = SecretStr("")
    """libpq connection string, e.g.
    ``postgresql://user:pass@host.postgres.database.azure.com:5432/zz2?sslmode=require``.
    Held as a SecretStr: it carries the database password."""

    db_schema: str = "techSupport"
    """Schema holding the application tables."""

    customer_history_schema: str = "zoho"
    """Read-only schema with the Zoho customer-history tables."""

    db_pool_min_size: int = Field(default=1, ge=0, le=20)
    db_pool_max_size: int = Field(default=5, ge=1, le=50)
    db_connect_timeout_seconds: int = Field(default=10, ge=1, le=60)

    # -- Sessions --------------------------------------------------------------
    session_secret: SecretStr = SecretStr("")
    """HS256 key for every session this server issues (password and Microsoft
    sign-ins). At least 32 characters. ``ZZ2_SAML_SESSION_SECRET`` is accepted as a
    fallback so an existing deployment keeps its sessions across the upgrade."""

    session_ttl_seconds: int = Field(default=8 * 3600, ge=300, le=7 * 24 * 3600)

    # -- Azure Blob Storage ----------------------------------------------------
    azure_storage_connection_string: SecretStr = SecretStr("")
    """Storage account connection string. Server-only."""

    azure_storage_container: str = "zz2-manuals"
    """Private container that holds kit PDF manuals and diagrams."""

    enable_blob_storage: bool = False
    """Use Azure Blob Storage as the durable source for manuals and diagrams. The
    application still writes a local cache so an outage does not make a file that
    was already read locally unavailable."""

    # -- Microsoft sign-in (OpenID Connect, Entra ID) --------------------------
    microsoft_enabled: bool = False
    """Offer "Entrar com Microsoft" through OpenID Connect. Works with Outlook /
    Microsoft 365 work and school accounts (and personal accounts when the tenant
    is ``common``)."""

    microsoft_tenant: str = "organizations"
    """Entra tenant ID or ``organizations`` / ``common`` / ``consumers``."""

    microsoft_client_id: str = ""
    microsoft_client_secret: SecretStr = SecretStr("")

    # -- Google Apps Script (legacy ticket sheet) ------------------------------
    appscript_url: str = ""
    appscript_secret: SecretStr = SecretStr("")

    # -- SAML single sign-on (Microsoft Entra ID / Azure AD) -------------------
    saml_enabled: bool = False
    """Offer "Sign in with Microsoft" through SAML 2.0. Requires the IdP values
    below and a session secret."""

    saml_idp_entity_id: str = ""
    """Entra ID "Microsoft Entra Identifier", e.g.
    ``https://sts.windows.net/<tenant-id>/``. Compared exactly with the assertion
    ``Issuer``."""

    saml_idp_sso_url: str = ""
    """Entra ID "Login URL", e.g.
    ``https://login.microsoftonline.com/<tenant-id>/saml2``."""

    saml_idp_x509_cert: SecretStr = SecretStr("")
    """The IdP signing certificate (Base64 from the Entra "SAML Certificates"
    section), with or without the PEM armour. Not secret in itself, but held as a
    SecretStr so it is never dumped wholesale into a log."""

    saml_sp_entity_id: str = ""
    """Our Identifier (Entity ID). Defaults to ``<public_url>/api/auth/saml/metadata``."""

    saml_allowed_domains: Annotated[tuple[str, ...], NoDecode] = ()
    """Email domains allowed to sign in, e.g. ``mackenzista.com.br``. Empty means
    any account the tenant vouches for — acceptable only for a single-tenant app
    registration restricted to assigned users."""

    saml_default_role: str = "tech"
    """Role for a SAML user with no matching user account and no group
    mapping."""

    saml_group_role_map: Annotated[tuple[str, ...], NoDecode] = ()
    """``<group-object-id>=<role>`` pairs, comma separated. Requires the groups
    claim to be enabled on the Entra enterprise application."""

    saml_session_secret: SecretStr = SecretStr("")
    """HS256 key for the sessions this server issues after a SAML sign-in. Must
    be at least 32 characters. Deprecated: use ``ZZ2_SESSION_SECRET``."""

    saml_session_ttl_seconds: int = Field(default=8 * 3600, ge=300, le=7 * 24 * 3600)

    # -- Brute-force protection ------------------------------------------------
    auth_lockout_enabled: bool = True
    auth_lockout_threshold: int = Field(default=20, ge=3, le=10_000)
    """Distinct rejected credentials from one IP, within the window, that trigger
    a block. Distinct, so one expired token retried by twelve parallel requests
    counts once."""

    auth_lockout_window_seconds: int = Field(default=600, ge=10, le=86_400)
    auth_lockout_seconds: int = Field(default=900, ge=10, le=86_400)

    # -- Troubleshooting sessions ----------------------------------------------
    troubleshooting_store: Literal["auto", "database", "local"] = "auto"
    """Where executed troubleshooting sessions are stored. ``auto`` uses the
    database ``troubleshooting_sessions`` table when ``ZZ2_DATABASE_URL`` is set and
    the local data directory otherwise."""

    # -- Observability ---------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    """Structured JSON lines. The monolith suppressed access logs entirely
    (``log_message`` was a no-op), leaving no audit trail."""

    # -- Feature flags ---------------------------------------------------------
    enable_appscript_sync: bool = True
    enable_db_mirror: bool = True
    enable_background_workers: bool = True

    @field_validator("cors_origins", "trusted_hosts", "saml_allowed_domains",
                     "saml_group_role_map", mode="before")
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

    @field_validator("saml_allowed_domains")
    @classmethod
    def _normalise_domains(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(domain.lower().lstrip("@") for domain in value)

    @model_validator(mode="after")
    def _saml_guardrails(self) -> Settings:
        """Refuse a half-configured SAML setup in any environment.

        A SAML endpoint with no certificate cannot verify anything, and one whose
        session key is too short would make every issued session forgeable.
        Both are configuration mistakes far easier to catch at boot than in production.
        """
        if not self.saml_enabled:
            return self
        problems: list[str] = []
        if not self.saml_idp_entity_id:
            problems.append("ZZ2_SAML_IDP_ENTITY_ID is required")
        if not self.saml_idp_sso_url.startswith("https://"):
            problems.append("ZZ2_SAML_IDP_SSO_URL must be an https URL")
        if not self.saml_idp_x509_cert.get_secret_value().strip():
            problems.append("ZZ2_SAML_IDP_X509_CERT is required")
        if len(self.effective_session_secret) < 32:
            problems.append("ZZ2_SESSION_SECRET must be at least 32 characters")
        if problems:
            raise ValueError("Invalid SAML configuration:\n  - " + "\n  - ".join(problems))
        return self

    @model_validator(mode="after")
    def _microsoft_guardrails(self) -> Settings:
        """Refuse a half-configured Microsoft sign-in."""
        if not self.microsoft_enabled:
            return self
        problems: list[str] = []
        if not self.microsoft_client_id:
            problems.append("ZZ2_MICROSOFT_CLIENT_ID is required")
        if not self.microsoft_client_secret.get_secret_value():
            problems.append("ZZ2_MICROSOFT_CLIENT_SECRET is required")
        if not self.microsoft_tenant:
            problems.append("ZZ2_MICROSOFT_TENANT is required")
        if len(self.effective_session_secret) < 32:
            problems.append("ZZ2_SESSION_SECRET must be at least 32 characters")
        if problems:
            raise ValueError("Invalid Microsoft sign-in configuration:\n  - "
                             + "\n  - ".join(problems))
        return self

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
        if not self.database_url.get_secret_value():
            problems.append("ZZ2_DATABASE_URL is required")
        if len(self.effective_session_secret) < 32:
            problems.append("ZZ2_SESSION_SECRET must be at least 32 characters")
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
    def effective_session_secret(self) -> str:
        return (self.session_secret.get_secret_value()
                or self.saml_session_secret.get_secret_value())

    @property
    def database_configured(self) -> bool:
        return bool(self.database_url.get_secret_value())

    @property
    def microsoft_redirect_uri(self) -> str:
        return f"{self.public_url}/api/auth/microsoft/callback"

    @property
    def saml_sp_entity(self) -> str:
        return self.saml_sp_entity_id or f"{self.public_url}/api/auth/saml/metadata"

    @property
    def saml_acs_url(self) -> str:
        return f"{self.public_url}/api/auth/saml/acs"

    def public_config(self) -> dict[str, str | bool]:
        """The only settings the browser is allowed to see.

        An explicit allowlist, not a filtered dump: adding a secret to
        ``Settings`` can never leak it through this endpoint by accident.
        """
        return {
            "build": self.build,
            "environment": self.environment,
            "saml_enabled": self.saml_enabled,
            "microsoft_enabled": self.microsoft_enabled,
            "password_login_enabled": True,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (memoised, so importers stay decoupled)."""
    return Settings()
