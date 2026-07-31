"""
Declarative base and shared column mixins for the Control Tower persistence layer.

Every ORM model of `backend/src/db/models.py` inherits from `Base`, which is
what lets `create_all_tables` (see `backend/src/db/session.py`) discover and
create every table from a single `MetaData` object. `TimestampMixin` and
`TimestampWithUpdateMixin` factor out the repeated `created_at`/`updated_at`
audit columns required by several tables of the schema, so each model only
declares the columns that make it structurally different from the others.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base class shared by every ORM model of the persistence layer."""


class TimestampMixin:
    """Adds a server-generated, timezone-aware `created_at` audit column."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TimestampWithUpdateMixin(TimestampMixin):
    """Adds `created_at` and a server-maintained `updated_at` audit column."""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
