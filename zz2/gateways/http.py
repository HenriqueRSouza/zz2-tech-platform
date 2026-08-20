"""Shared HTTP client.

One pooled :class:`httpx.Client` for all outbound traffic instead of the
monolith's per-call ``urllib2.urlopen``, which opened a fresh TCP and TLS
connection for every request. On the ticket-save path that meant a full TLS
handshake per write; the poll loop paid it twice a minute forever.

Why synchronous. Services here are dominated by local file I/O, and FastAPI runs
``def`` endpoints in a worker thread already. Making the gateways async would
force every service, repository and background worker to be async too, for no
gain on a workload where the concurrency limit is disk, not sockets. Sync
throughout keeps one execution model.

Timeouts are mandatory and explicit. ``urllib2.urlopen`` without a timeout —
which the monolith used for the Supabase auth check — blocks forever if the peer
stops responding, and every request thread that touches it blocks with it. A slow
upstream must degrade to an error, not to a hung server.
"""

from __future__ import annotations

import threading

import httpx

from zz2.settings import get_settings

USER_AGENT = "ZZ2Server/2.0"
"""Deliberately not browser-like.

Supabase's secret-key gateway rejects a request whose ``User-Agent`` looks like a
browser ("Forbidden use of secret API key in browser") as a safety net against a
leaked service key being used client-side. That protection only works if genuine
server traffic identifies itself honestly, so this must never be dressed up as
Mozilla the way the monolith's Apps Script calls were.
"""

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def get_client() -> httpx.Client:
    """The process-wide pooled client, created on first use."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.Client(
                    timeout=DEFAULT_TIMEOUT,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                    follow_redirects=False,
                    # Redirects are off on purpose: a followed redirect can move a
                    # request carrying the service key to a host we never chose.
                    trust_env=False,
                )
    return _client


def close_client() -> None:
    """Release pooled connections. Called from the application's shutdown hook."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


def is_configured() -> bool:
    """Whether Supabase credentials are present at all."""
    settings = get_settings()
    return bool(settings.supabase_url and settings.supabase_service_key.get_secret_value())
