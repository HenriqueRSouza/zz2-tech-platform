"""Gunicorn configuration.

**One worker process, deliberately.** Three pieces of application state live in
process memory and would be wrong or duplicated across workers:

* **Background workers.** The poller, the write-behind queue and the nightly
  scheduler run as threads inside the application. Four worker processes means
  four nightly backups at midnight, four pollers hitting the Apps Script quota,
  and four write-behind loops racing for the head of the same queue file.
* **Rate limiting.** The counters are in memory, so N workers means an effective
  limit of N x the configured value.
* **Caches.** The analytics view and the resolved-principal cache would each be
  held per process, multiplying memory and cache misses.

One worker is not a compromise for this workload. Endpoints are ``def``, so
uvicorn runs them in a thread pool: concurrency comes from ``threads`` below, and
the real limit is local disk, not CPU. This is an internal tool for a support
team, not a public service.

If it ever needs to scale horizontally, the order of work is: move the rate
limiter to Redis, then move the background workers into a separate systemd
service, and only then raise this number.
"""

from __future__ import annotations

import multiprocessing
import os

# -- Binding -------------------------------------------------------------------
bind = f"{os.environ.get('ZZ2_HOST', '127.0.0.1')}:{os.environ.get('ZZ2_PORT', '8080')}"

# -- Workers -------------------------------------------------------------------
workers = 1  # See the module docstring before changing this.
worker_class = "uvicorn.workers.UvicornWorker"

#: Thread pool for the sync endpoints. Bounded well below the file-descriptor
#: limit; each concurrent request may hold an open file.
threads = min(32, (multiprocessing.cpu_count() or 2) * 4)

# -- Timeouts ------------------------------------------------------------------
timeout = 120
"""Long enough for a full analytics rebuild, which reads and consolidates the
whole ticket store. Anything slower than this is stuck, not busy."""

graceful_timeout = 30
"""Time the workers get to finish in-flight requests and drain the write-behind
queue on shutdown. The monolith had no graceful shutdown at all — deploying meant
`pkill -f server.py`, which killed the queue worker mid-write."""

keepalive = 5

# -- Robustness ----------------------------------------------------------------
max_requests = 0
"""No periodic worker recycling. Recycling would restart the background threads
and drop the in-memory caches on a schedule, for no benefit — there is no known
leak to work around."""

# -- Logging -------------------------------------------------------------------
accesslog = None
"""The application's own middleware logs every request with a request id and the
authenticated actor attached. A second access log would duplicate it without
either of those."""

errorlog = "-"
loglevel = os.environ.get("ZZ2_LOG_LEVEL", "info").lower()

# -- Proxy ---------------------------------------------------------------------
forwarded_allow_ips = "127.0.0.1"
"""Only accept forwarding headers from the local nginx. Without this bound, a
client could send its own X-Forwarded-For and defeat rate limiting."""

proxy_protocol = False
