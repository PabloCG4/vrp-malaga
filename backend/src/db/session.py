"""
Async SQLAlchemy engine and session management for the Control Tower database.

The Control Tower dashboard connects to a Neon Serverless PostgreSQL instance
in the cloud. Credentials are loaded from a project-root `.env` file
(`DATABASE_URL`), automatically adapted for SQLAlchemy's asyncpg driver
(`postgresql+asyncpg://...`), with SSL query parameters that Neon emits in
libpq form (`sslmode`, `channel_binding`) translated into asyncpg-compatible
`connect_args`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# Project root (repository root), where the `.env` file is expected to live.
_PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]
_ENV_FILE_PATH: Path = _PROJECT_ROOT / ".env"

load_dotenv(_ENV_FILE_PATH)

# libpq-style SSL query parameters that Neon includes in its connection
# strings. asyncpg does not accept these as URL query parameters; they are
# stripped from the URL and expressed through `connect_args` instead.
_ASYNCPG_UNSUPPORTED_QUERY_KEYS: frozenset[str] = frozenset(
    {"sslmode", "ssl", "channel_binding"}
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _adapt_scheme_for_asyncpg(database_url: str) -> str:
    """
    Rewrite a plain PostgreSQL URL to the SQLAlchemy asyncpg dialect form.

    Accepts both `postgresql://...` and the shorter `postgres://...` schemes
    that Neon and many managed providers emit, and leaves an already-async
    `postgresql+asyncpg://...` URL unchanged.
    """
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    if database_url.startswith("postgres://"):
        return "postgresql+asyncpg://" + database_url.removeprefix("postgres://")
    if database_url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + database_url.removeprefix("postgresql://")
    return database_url


def normalize_database_url(database_url: str) -> tuple[str, dict[str, object]]:
    """
    Adapt a Neon (or any PostgreSQL) URL for SQLAlchemy + asyncpg.

    Parameters
    ----------
    database_url:
        Raw connection string, typically read from `DATABASE_URL` in `.env`.

    Returns
    -------
    tuple[str, dict[str, object]]
        The rewritten URL (asyncpg scheme, unsupported SSL query keys
        removed) and the `connect_args` dictionary to pass to
        `create_async_engine`. When the original URL requested SSL
        (`sslmode=require` or equivalent), `ssl=True` is included so asyncpg
        negotiates a TLS connection to Neon. `statement_cache_size=0` is
        always set for Neon pooler endpoints, which run through PgBouncer in
        transaction mode and do not support prepared statement caching.
    """
    adapted_url = _adapt_scheme_for_asyncpg(database_url.strip().strip('"').strip("'"))
    parsed = urlparse(adapted_url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)

    ssl_requested = False
    retained_pairs: list[tuple[str, str]] = []
    for key, value in query_pairs:
        lowered_key = key.lower()
        if lowered_key in _ASYNCPG_UNSUPPORTED_QUERY_KEYS:
            if lowered_key in {"sslmode", "ssl"} and value.lower() not in {"disable", "false", "0"}:
                ssl_requested = True
            continue
        retained_pairs.append((key, value))

    cleaned_url = urlunparse(parsed._replace(query=urlencode(retained_pairs)))

    connect_args: dict[str, object] = {
        # Neon’s connection-pooler endpoints use PgBouncer in transaction
        # mode, which is incompatible with asyncpg’s prepared-statement cache.
        "statement_cache_size": 0,
    }
    if ssl_requested or "neon.tech" in (parsed.hostname or ""):
        connect_args["ssl"] = True

    return cleaned_url, connect_args


def resolve_database_url() -> str:
    """
    Resolve the PostgreSQL connection string from the environment.

    Reads `DATABASE_URL` from the process environment after `.env` has been
    loaded. Raises `RuntimeError` when the variable is missing or empty,
    since this layer is cloud-first and no longer falls back to a local
    SQLite file.
    """
    database_url = os.environ.get("DATABASE_URL", "").strip().strip('"').strip("'")
    if not database_url:
        message = (
            "DATABASE_URL is not set. Create a `.env` file at the project root "
            f"({_ENV_FILE_PATH}) with a Neon PostgreSQL connection string, for example:\n"
            'DATABASE_URL="postgresql://user:password@ep-xxx.neon.tech/neondb?sslmode=require"'
        )
        raise RuntimeError(message)
    return database_url


def build_engine(database_url: str | None = None, echo: bool = False) -> AsyncEngine:
    """
    Build a new async SQLAlchemy engine for the given, or resolved, database URL.

    Parameters
    ----------
    database_url:
        Raw PostgreSQL connection string. Defaults to `resolve_database_url()`.
    echo:
        Whether SQLAlchemy should log every emitted SQL statement, useful for
        local debugging but noisy in normal operation.
    """
    raw_url = database_url or resolve_database_url()
    async_url, connect_args = normalize_database_url(raw_url)
    return create_async_engine(
        async_url,
        echo=echo,
        future=True,
        connect_args=connect_args,
        # Neon's serverless endpoints silently close idle connections well
        # before SQLAlchemy's pool would otherwise recycle them; without
        # `pool_pre_ping`, the first checkout of such a stale connection
        # surfaces to the client as a raw `asyncpg.InterfaceError` ("connection
        # is closed") instead of transparently reconnecting.
        pool_pre_ping=True,
    )


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, building it lazily on first use."""
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async session factory, bound to `get_engine()`."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """
    Yield a request-scoped async database session.

    Structured as an async generator so it doubles as a FastAPI dependency
    (`Depends(get_db_session)`) once the API layer of a later phase is
    introduced, while remaining directly usable from scripts and tests via
    `async for session in get_db_session(): ...` or by calling
    `get_session_factory()()` for an explicit `async with` block.
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        yield session


async def create_all_tables(engine: AsyncEngine | None = None) -> None:
    """
    Create every table declared on `Base.metadata`, if it does not already exist.

    Imports `backend.src.db.models` locally so that every ORM model is
    registered on `Base.metadata` before `create_all` runs, without forcing
    every importer of this module to also import the full model set.
    """
    from . import models  # noqa: F401  Registers every table on Base.metadata.
    from .base import Base

    target_engine = engine or get_engine()
    async with target_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    """Dispose of and clear the process-wide engine and session factory."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
