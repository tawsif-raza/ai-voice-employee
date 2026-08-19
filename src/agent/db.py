"""
Database foundation — engine/session management and configuration (Phase
12; plan.md Step 12.2).

Scope discipline (plan.md's own instruction for this step): this module
provides ONLY connection/session infrastructure. It defines no tables, no
ORM models, and no repository classes — those are Steps 12.3 and
12.5-12.9. Nothing in this module makes a business/policy decision;
PolicyEngine, SessionManager, MemoryManager, etc. remain exactly as
authoritative as before (plan.md Phase 12.1 Architecture Constraints:
"Repositories must only provide persistence").

Mode boundary (mirrors src/api/server.py's existing AUTH_MODE pattern
exactly — same discipline, not a new one):

    PERSISTENCE_MODE unset or "dev" (default) -> in-memory repositories
        remain the default everywhere they already are (SessionManager(),
        MemoryManager(), AuditLogger(), ToolOrchestrator() with no
        repository argument) — this module is not even consulted, so
        every one of the 554 pre-Phase-12 tests is completely unaffected.

    PERSISTENCE_MODE="production" -> DATABASE_URL MUST be set and MUST
        resolve to a reachable database, checked at startup
        (`load_database_config()` + an explicit `Database.health_check()`
        call by the caller). Per plan.md Step 12.2's explicit security
        requirement -- "Production must NOT silently fall back to
        in-memory persistence if PostgreSQL is unavailable" -- this
        module raises rather than degrading; it never itself decides to
        substitute an in-memory repository.

Security: the raw DATABASE_URL (which embeds a password) is never logged
or included in an exception message anywhere in this module — every
diagnostic uses `DatabaseConfig.safe_url` (password masked), matching
identity.py's AuthenticationError / oidc_provider.py's existing
"never include credential material in a message" discipline.
"""

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.exc import SQLAlchemyError

_PRODUCTION_MODES = {"production", "postgres", "postgresql"}


class DatabaseConfigurationError(Exception):
    """
    Raised by load_database_config() when PERSISTENCE_MODE requests
    production persistence but the required configuration is missing or
    malformed. Never raised with the raw DATABASE_URL in its message —
    see module docstring.
    """


class DatabaseUnavailableError(Exception):
    """
    Raised by Database.health_check()/session_scope() when the configured
    database cannot actually be reached. Distinct from
    DatabaseConfigurationError (bad/missing config) — this is "config
    looked fine, but the database didn't answer." Never includes the raw
    DATABASE_URL.
    """


def _mask_url(url: str) -> str:
    """
    Returns `url` with any embedded password replaced by `***` — the only
    form of a database URL this module (or any caller) may log or include
    in an error message. Never returns the original string when a
    password is present.
    """
    try:
        parts = urlsplit(url)
        if parts.password is None:
            return url
        userinfo = parts.username or ""
        userinfo += ":***"
        netloc = f"{userinfo}@{parts.hostname or ''}"
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        # Malformed URL -- don't risk echoing raw credential-shaped text.
        return "***MALFORMED_DATABASE_URL***"


@dataclass(frozen=True)
class DatabaseConfig:
    """
    Resolved, validated database configuration. `url` is intentionally
    the only field carrying the real connection string — callers that
    need to log/report configuration MUST use `safe_url`, never `url`.
    """

    url: str
    safe_url: str
    mode: str  # "dev" | "production" (see module docstring)
    pool_size: int
    max_overflow: int
    pool_timeout_seconds: float
    pool_recycle_seconds: int
    echo: bool

    def is_production(self) -> bool:
        return self.mode in _PRODUCTION_MODES


# Dev/test default: file-free, dependency-light, matches this repo's
# existing "offline, stdlib-first" test convention (see
# PHASE_12_1_PERSISTENCE_AUDIT.md §13). Never used when PERSISTENCE_MODE
# requests production.
_DEV_DEFAULT_URL = "sqlite:///:memory:"


def load_database_config(env: Optional[dict] = None) -> DatabaseConfig:
    """
    Reads PERSISTENCE_MODE/DATABASE_URL/pool tuning from `env` (defaults
    to os.environ — injectable for tests, matching reliability_config.py's
    `config_path` parameter convention for the same reason: deterministic,
    isolated tests without mutating real process environment).

    Fail-closed for production (plan.md Step 12.2's explicit requirement):
    PERSISTENCE_MODE in {"production","postgres","postgresql"} with no
    DATABASE_URL raises DatabaseConfigurationError immediately — this
    function never substitutes the dev SQLite default for a production
    request missing its configuration.

    Fail-safe for dev (mirrors reliability_config.py's posture, not
    oidc_provider.py's): an unset/blank DATABASE_URL when
    PERSISTENCE_MODE is unset/"dev" resolves to the in-memory SQLite
    default rather than raising, since dev/test callers overwhelmingly
    never configure a database at all today (they construct managers
    directly) and this function existing must not force them to.
    """
    source = env if env is not None else os.environ
    mode = str(source.get("PERSISTENCE_MODE", "dev")).strip().lower() or "dev"
    raw_url = source.get("DATABASE_URL")

    if mode in _PRODUCTION_MODES:
        if not raw_url or not str(raw_url).strip():
            raise DatabaseConfigurationError(
                "PERSISTENCE_MODE is set to production persistence but DATABASE_URL is not configured. "
                "Refusing to start rather than silently falling back to in-memory storage."
            )
        url = str(raw_url).strip()
    else:
        url = str(raw_url).strip() if raw_url and str(raw_url).strip() else _DEV_DEFAULT_URL

    try:
        pool_size = int(source.get("DB_POOL_SIZE", 5))
        max_overflow = int(source.get("DB_MAX_OVERFLOW", 10))
        pool_timeout_seconds = float(source.get("DB_POOL_TIMEOUT_SECONDS", 30))
        pool_recycle_seconds = int(source.get("DB_POOL_RECYCLE_SECONDS", 1800))
    except (TypeError, ValueError) as exc:
        raise DatabaseConfigurationError(f"Invalid database pool configuration value: {exc}") from None

    if pool_size < 1 or max_overflow < 0 or pool_timeout_seconds <= 0 or pool_recycle_seconds < 0:
        raise DatabaseConfigurationError(
            "Database pool configuration must be positive (pool_size>=1, max_overflow>=0, "
            "pool_timeout_seconds>0, pool_recycle_seconds>=0)."
        )

    echo = str(source.get("DB_ECHO", "false")).strip().lower() == "true"

    return DatabaseConfig(
        url=url, safe_url=_mask_url(url), mode=mode,
        pool_size=pool_size, max_overflow=max_overflow,
        pool_timeout_seconds=pool_timeout_seconds, pool_recycle_seconds=pool_recycle_seconds,
        echo=echo,
    )


def _build_engine(config: DatabaseConfig) -> Engine:
    """
    SQLite (dev/test) gets no pool-size/overflow tuning — those
    parameters are meaningless for SQLite's connection model (a
    :memory: database is one connection per Engine by construction; a
    file-based one doesn't benefit from a real pool the way a networked
    server does) and SQLAlchemy rejects them for the sqlite driver.
    PostgreSQL gets the full configured pool.
    """
    if config.url.startswith("sqlite"):
        connect_args = {"check_same_thread": False} if ":memory:" in config.url else {}
        return create_engine(config.url, echo=config.echo, connect_args=connect_args)
    return create_engine(
        config.url, echo=config.echo,
        pool_size=config.pool_size, max_overflow=config.max_overflow,
        pool_timeout=config.pool_timeout_seconds, pool_recycle=config.pool_recycle_seconds,
        pool_pre_ping=True,  # detects a dropped connection before handing it to a caller, not after
    )


class Database:
    """
    Owns exactly one Engine + sessionmaker for the process. Never a
    global/module-level singleton by construction — every caller
    (build_conversation_manager(), tests) explicitly constructs and
    passes one, matching this repo's existing "no hidden global state"
    convention (PolicyEngine, AuditLogger, etc. are all explicitly
    constructed and threaded through, never module-level singletons).
    """

    def __init__(self, config: DatabaseConfig):
        self.config = config
        self._engine = _build_engine(config)
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False, future=True)

    @property
    def engine(self) -> Engine:
        return self._engine

    def health_check(self) -> bool:
        """
        Returns True if a trivial query round-trips successfully. Never
        raises SQLAlchemyError directly to the caller — wraps it in
        DatabaseUnavailableError with a safe (no-credential) message, so
        every caller has one exception type to handle regardless of
        which underlying driver failed.
        """
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError as exc:
            raise DatabaseUnavailableError(
                f"Database health check failed for {self.config.safe_url}: {type(exc).__name__}"
            ) from None

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        """
        One transaction per `with` block (plan.md Phase 12.1 §9's
        transaction-boundary decision: each repository method is a single
        logical unit of work) — commits on clean exit, rolls back and
        re-raises on any exception. Never leaves a session dangling
        half-committed.
        """
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except SQLAlchemyError as exc:
            session.rollback()
            raise DatabaseUnavailableError(
                f"Database operation failed against {self.config.safe_url}: {type(exc).__name__}"
            ) from None
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        """Closes all pooled connections — used by tests to guarantee isolation between cases, and by graceful shutdown."""
        self._engine.dispose()


def build_database(env: Optional[dict] = None) -> Database:
    """Convenience: load_database_config() + Database(...) in one call, for callers (e.g. build_conversation_manager()) that don't need the intermediate config separately."""
    return Database(load_database_config(env))


def upsert_row(connection, table, values: dict, conflict_columns: list) -> None:
    """
    Dialect-dispatched `INSERT ... ON CONFLICT (conflict_columns) DO
    UPDATE` (Phase 12.5+; plan.md Phase 12.1 audit §9's transaction-
    boundary decision: one statement, one unit of work — never a
    check-then-insert-or-update sequence that could race). Shared by
    every Postgres-backed repository (session/memory/audit/idempotency)
    so the same atomic-upsert primitive isn't reimplemented per table.

    Only PostgreSQL and SQLite are supported (this module's two engines
    — see module docstring); both dialects expose an `ON CONFLICT DO
    UPDATE` construct with an identical `.on_conflict_do_update()` API
    shape, so the dispatch below is a single import swap, not divergent
    logic.
    """
    dialect = connection.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:
        raise DatabaseUnavailableError(f"Unsupported database dialect for upsert: {dialect}")

    stmt = _insert(table).values(**values)
    update_cols = {col: stmt.excluded[col] for col in values if col not in conflict_columns}
    stmt = stmt.on_conflict_do_update(index_elements=conflict_columns, set_=update_cols)
    connection.execute(stmt)
