"""Google Apps Script gateway.

The ticket spreadsheet is still fronted by an Apps Script web app. Every call is
authenticated by a shared secret appended to the query string — the deployment's
own design, not a choice made here — which drives three rules this module
enforces so that no caller has to remember them.

**The URL is never logged.** It carries the secret. The monolith had a
``_redact`` helper for exactly this, and still leaked the key on the failure path:
``_log('PROXY_GET: ' + _redact(full_url)[:160])`` was redacted, but
``_log('CSV fetch attempt %d failed: %s' % (attempt+1, str(e)))`` embedded the
full URL inside the exception's own text. Here the URL is assembled immediately
before the request and only the action name is ever logged.

**The host is pinned.** The base URL is operator-configurable, and this gateway
fetches it server-side. Unpinned, that is a server-side request forgery
primitive: point it at ``http://169.254.169.254/`` and the server fetches cloud
instance metadata and returns the body. On a VPS that is a credential
disclosure. The host is therefore re-checked here, not only where the setting is
written.

**Client parameters cannot become control parameters.** ``key`` is stripped from
caller-supplied parameters before the real one is appended, so a request cannot
smuggle its own.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from zz2.core.errors import NotConfigured, UpstreamError
from zz2.core.logging import logger
from zz2.gateways.http import get_client
from zz2.services.runtime_config import runtime_config
from zz2.settings import get_settings

ALLOWED_HOST = "script.google.com"
"""The only host this gateway will fetch. See the module docstring."""

RESERVED_PARAMS = frozenset({"key"})
"""Parameters a caller may not set; they are authentication, not data."""

WRITE_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)
"""Sheet writes are slow. Generous, but still bounded."""


class AppScriptGateway:
    """Signed access to the Apps Script web app."""

    @property
    def configured(self) -> bool:
        return bool(self._base_url() and self._secret())

    def _base_url(self) -> str:
        """Configured deployment URL, environment taking precedence.

        The environment value is the deployment's own; the runtime value is
        operator-editable from the Settings page. Preferring the environment means
        a compromised Settings page cannot repoint the integration.
        """
        return get_settings().appscript_url or runtime_config.appscript_url

    def _secret(self) -> str:
        return get_settings().appscript_secret.get_secret_value()

    def _validated_url(self) -> str:
        """Return the base URL after re-checking the host.

        Raises:
            NotConfigured: No URL or secret configured.
            UpstreamError: The configured URL is not an Apps Script deployment.
        """
        url = self._base_url()
        if not url or not self._secret():
            raise NotConfigured("The ticket sheet integration is not configured.")

        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
            # Logged as an error, not a warning: reaching here means the stored
            # configuration is unsafe and someone should look at it.
            logger.error("rejected non-Apps-Script integration URL",
                         extra={"host": parsed.hostname or "", "scheme": parsed.scheme})
            raise UpstreamError("The ticket sheet integration is misconfigured.")
        return url

    def call(self, action: str, params: dict[str, Any] | None = None,
             *, timeout: httpx.Timeout | None = None) -> Any:
        """Invoke one Apps Script action and return its parsed JSON.

        Args:
            action: The ``action`` parameter, e.g. ``"recent"``, ``"export"``.
            params: Additional query parameters. ``key`` is ignored.
            timeout: Override for slow operations.

        Returns:
            Parsed JSON — a dict or a list, depending on the action.

        Raises:
            NotConfigured: Integration not configured.
            UpstreamError: Transport failure, non-200, unparseable body, or an
                explicit ``{"error": ...}`` from the script.
        """
        base = self._validated_url()
        query: dict[str, Any] = {
            key: value for key, value in (params or {}).items()
            if key not in RESERVED_PARAMS
        }
        query["action"] = action
        query["key"] = self._secret()

        separator = "&" if "?" in base else "?"
        url = f"{base}{separator}{urlencode(query, doseq=False)}"

        logger.info("appscript call", extra={"action": action})
        try:
            # follow_redirects is required here specifically: Apps Script answers
            # /exec with a 302 to a googleusercontent.com URL that carries the
            # response. Safe because the initial host is pinned above and the
            # secret travels only on that first request.
            response = get_client().get(
                url, timeout=timeout or WRITE_TIMEOUT, follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            # str(exc) can contain the request URL, and the URL contains the
            # secret. Only the action and exception type are recorded.
            logger.warning("appscript transport failure",
                           extra={"action": action, "error_type": type(exc).__name__})
            raise UpstreamError(log_detail=f"appscript {action}: {type(exc).__name__}") from exc

        if response.status_code != 200:
            raise UpstreamError(
                log_detail=f"appscript {action} HTTP {response.status_code}")

        try:
            parsed = response.json()
        except ValueError as exc:
            raise UpstreamError(log_detail=f"appscript {action} returned non-JSON") from exc

        if isinstance(parsed, dict) and parsed.get("error"):
            raise UpstreamError(
                "The ticket sheet rejected the request.",
                log_detail=f"appscript {action} app error: {parsed.get('error')}",
            )
        logger.info("appscript ok", extra={"action": action, "bytes": len(response.content)})
        return parsed

    def write(self, params: dict[str, Any]) -> dict[str, Any]:
        """Perform one queued write operation.

        The action lives inside ``params`` for these, matching the existing
        script's contract.

        Returns:
            The script's response dict, empty when it returned a non-dict.

        Raises:
            UpstreamError: The write did not land. The caller retries.
        """
        action = str(params.get("action") or "")
        payload = {key: value for key, value in params.items() if key != "action"}
        result = self.call(action, payload)
        if isinstance(result, dict) and result.get("ok") is False:
            raise UpstreamError(
                log_detail=f"appscript write {action} refused: {result.get('error')}")
        return result if isinstance(result, dict) else {}


appscript = AppScriptGateway()
