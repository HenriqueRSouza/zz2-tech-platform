"""PostgreSQL gateway — Azure Database for PostgreSQL (Flexible Server).

Replaces the Supabase REST (PostgREST) gateway. The application now talks SQL
straight to Postgres over TLS, through a small connection pool, with every value
passed as a bound parameter. There is no query-string filter syntax left to
percent-encode, so the class of bug :func:`encode_value` existed to prevent is
gone rather than guarded against.

Each pooled connection's ``search_path`` is the application schema followed by
the customer-history schema, so queries name tables unqualified (``tickets``,
``customer_performance_months``). The two schemas share no table names.

Errors become :class:`~zz2.core.errors.UpstreamError` carrying the driver message
in ``log_detail`` — it reaches the log, never the client — so every caller that
already degraded gracefully when Supabase was unreachable keeps doing so.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal
from typing import Any

from zz2.core.errors import NotConfigured, UpstreamError
from zz2.core.logging import logger
from zz2.settings import get_settings

try:  # Imported lazily so the test suite can run without a database driver.
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    from psycopg_pool import ConnectionPool
except ImportError:  # pragma: no cover - exercised only without the dependency
    psycopg = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    Jsonb = None  # type: ignore[assignment]
    ConnectionPool = None  # type: ignore[assignment]

Params = Sequence[Any] | dict[str, Any] | None


def as_json(value: Any) -> Any:
    """Wrap a Python value for a ``jsonb`` column."""
    return Jsonb(value) if Jsonb is not None else value


def escape_like(value: str) -> str:
    """Escape ``LIKE``/``ILIKE`` metacharacters so user text matches literally.

    PostgreSQL's default ``LIKE`` escape character is the backslash, so the
    backslash itself is escaped first, then ``%`` and ``_``. Wrap the result in
    ``%…%`` for a substring match.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# -- Statement composition -------------------------------------------------------
# For statements whose column set varies (whitelisted PATCH bodies, model dumps).
# Names are quoted as identifiers by the driver and values stay bound parameters,
# so nothing a caller passes is ever spliced into SQL text.
def column_list(names: Iterable[str]) -> Any:
    """``"a", "b", "c"``"""
    return sql.SQL(", ").join(sql.Identifier(name) for name in names)


def placeholder_list(count: int) -> Any:
    """``%s, %s, %s``"""
    return sql.SQL(", ").join([sql.Placeholder()] * count)


def assignment_list(names: Iterable[str]) -> Any:
    """``"a" = %s, "b" = %s``"""
    return sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(name), sql.Placeholder()) for name in names
    )


def sqlstate(exc: BaseException) -> str:
    """The SQLSTATE behind an :class:`UpstreamError` raised by this gateway, or ``""``.

    Lets a caller turn a specific constraint failure into a client error — e.g.
    ``23505`` (unique violation) into a 409 — without importing the driver.
    """
    cause = exc.__cause__
    return str(getattr(cause, "sqlstate", "") or "")


def json_value(value: Any) -> Any:
    """Convert a driver value to its JSON-friendly equivalent.

    PostgREST returned JSON, so every caller was written against strings and
    floats. psycopg returns ``date``/``datetime``/``Decimal``; this maps them back:
    timestamps to ISO 8601 in UTC with a ``Z`` suffix, dates to ``YYYY-MM-DD``,
    numerics to ``int`` when integral and ``float`` otherwise.
    """
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.UTC)
            return value.replace(tzinfo=None).isoformat() + "Z"
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def json_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """:func:`json_value` applied to every column of one row."""
    if row is None:
        return None
    return {key: json_value(value) for key, value in row.items()}


def json_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """:func:`json_value` applied to every column of every row."""
    return [{key: json_value(value) for key, value in row.items()} for row in rows]


class Database:
    """Process-wide pooled access to the application database."""

    def __init__(self) -> None:
        self._pool: Any = None
        self._lock = threading.Lock()

    # -- Guards ----------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return get_settings().database_configured and psycopg is not None

    def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        settings = get_settings()
        dsn = settings.database_url.get_secret_value()
        if not dsn:
            raise NotConfigured("The database is not configured on this server.")
        if psycopg is None:
            raise NotConfigured("The PostgreSQL driver (psycopg) is not installed.")
        with self._lock:
            if self._pool is None:
                search_path = ", ".join(
                    '"' + name.replace('"', '""') + '"'
                    for name in (settings.db_schema, settings.customer_history_schema,
                                 "public")
                )

                def configure(conn: Any) -> None:
                    conn.execute(f"SET search_path TO {search_path}")
                    conn.commit()

                pool = ConnectionPool(
                    dsn,
                    min_size=settings.db_pool_min_size,
                    max_size=settings.db_pool_max_size,
                    kwargs={
                        "row_factory": dict_row,
                        "connect_timeout": settings.db_connect_timeout_seconds,
                        "application_name": "zz2-tech-platform",
                    },
                    configure=configure,
                    check=ConnectionPool.check_connection,
                    open=False,
                    name="zz2",
                )
                pool.open(wait=False)
                self._pool = pool
        return self._pool

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None

    # -- Execution -------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """A cursor inside one transaction: committed on success, rolled back on
        any exception. Driver errors surface as :class:`UpstreamError`."""
        pool = self._get_pool()
        try:
            with pool.connection(timeout=get_settings().db_connect_timeout_seconds) as conn:
                with conn.transaction(), conn.cursor() as cursor:
                    yield cursor
        except UpstreamError:
            raise
        except Exception as exc:
            if psycopg is not None and isinstance(exc, (psycopg.Error, *_pool_errors())):
                logger.warning("database error", extra={"error": type(exc).__name__})
                raise UpstreamError(log_detail=f"database: {exc}") from exc
            raise

    def fetch_all(self, query: Any, params: Params = None) -> list[dict[str, Any]]:
        with self.transaction() as cursor:
            cursor.execute(query, params)
            return list(cursor.fetchall()) if cursor.description else []

    def fetch_one(self, query: Any, params: Params = None) -> dict[str, Any] | None:
        with self.transaction() as cursor:
            cursor.execute(query, params)
            return cursor.fetchone() if cursor.description else None

    def execute(self, query: Any, params: Params = None) -> int:
        """Run a statement and return the affected row count."""
        with self.transaction() as cursor:
            cursor.execute(query, params)
            return cursor.rowcount

    def execute_many(self, query: Any, rows: Iterable[Params]) -> None:
        with self.transaction() as cursor:
            cursor.executemany(query, list(rows))

    def ping(self) -> bool:
        try:
            return self.fetch_one("SELECT 1 AS ok") == {"ok": 1}
        except (UpstreamError, NotConfigured):
            return False

    # -- Helpers ---------------------------------------------------------------
    def upsert(self, table: str, rows: list[dict[str, Any]], *,
               conflict: Sequence[str]) -> None:
        """``INSERT … ON CONFLICT (…) DO UPDATE`` for rows sharing one key set."""
        if not rows:
            return
        columns = list(rows[0].keys())
        updates = [c for c in columns if c not in conflict]
        statement = sql.SQL(
            "INSERT INTO {table} ({cols}) VALUES ({vals}) ON CONFLICT ({conflict}) "
        ).format(
            table=sql.Identifier(table),
            cols=sql.SQL(", ").join(map(sql.Identifier, columns)),
            vals=sql.SQL(", ").join(sql.Placeholder(c) for c in columns),
            conflict=sql.SQL(", ").join(map(sql.Identifier, conflict)),
        )
        if updates:
            statement += sql.SQL("DO UPDATE SET {sets}").format(
                sets=sql.SQL(", ").join(
                    sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
                    for c in updates
                )
            )
        else:
            statement += sql.SQL("DO NOTHING")
        self.execute_many(statement, ({c: _adapt(row.get(c)) for c in columns}
                                      for row in rows))


def _adapt(value: Any) -> Any:
    return as_json(value) if isinstance(value, dict | list) else value


def _pool_errors() -> tuple[type[BaseException], ...]:
    try:
        from psycopg_pool import PoolTimeout
    except ImportError:  # pragma: no cover
        return ()
    return (PoolTimeout,)


db = Database()
