# ZZ2 Tech Support Platform.
#
# Build from the repository root:
#   docker build -f deploy/Dockerfile -t zz2:latest --build-arg BUILD=$(git rev-parse --short HEAD) .
#
# Two stages so the runtime image carries no compiler and no build cache. The
# smaller the image, the smaller the set of things in it that can have a CVE.

# -- Stage 1: dependencies -----------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Requirements first, as their own layer: application code changes on every
# commit, dependencies do not, so this layer stays cached across builds.
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt gunicorn

# -- Stage 2: runtime ----------------------------------------------------------
FROM python:3.12-slim AS runtime

ARG BUILD=docker
ENV ZZ2_BUILD=${BUILD} \
    ZZ2_DATA_DIR=/var/lib/zz2 \
    ZZ2_HOST=0.0.0.0 \
    ZZ2_PORT=8080 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root, with no login shell and no home directory to write into.
RUN groupadd --system --gid 1001 zz2 \
 && useradd --system --uid 1001 --gid zz2 --no-create-home --shell /usr/sbin/nologin zz2

COPY --from=builder /opt/venv /opt/venv

WORKDIR /opt/zz2
COPY --chown=root:root zz2/ ./zz2/
COPY --chown=root:root web/ ./web/
# Imported once into an empty persistent volume by CatalogService.
COPY --chown=root:root backups/20260820_000013/data/kits.json ./seed/catalog/kits.json
COPY --chown=root:root backups/20260820_000013/data/troubleshooting.json ./seed/catalog/troubleshooting.json
COPY --chown=root:root deploy/gunicorn.conf.py ./deploy/gunicorn.conf.py

# Application code stays root-owned and read-only to the service user: the
# process can execute it and cannot rewrite it.
RUN chmod -R a-w /opt/zz2 \
 && install -d -o zz2 -g zz2 -m 0750 /var/lib/zz2

USER zz2

# The only writable path. Mount a volume here — everything else is disposable.
VOLUME ["/var/lib/zz2"]

EXPOSE 8080

# Liveness only: no disk, no network. A readiness check that fails because
# Supabase is slow must not get the container killed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["gunicorn", "--config", "/opt/zz2/deploy/gunicorn.conf.py", "zz2.main:app"]
