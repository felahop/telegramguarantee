"""Declarative base, naming conventions and custom column types."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class MinorAmount(sa.types.TypeDecorator[int]):
    """``NUMERIC(78, 0)`` exposed to Python as ``int`` (uint256 fits in 78 digits).

    Binding a float or a fractional Decimal raises — it would mean a bug in money math upstream.
    """

    impl = sa.Numeric(78, 0)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"minor amounts must be int, got {type(value).__name__}")
        return Decimal(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> int | None:
        if value is None:
            return None
        decimal_value = Decimal(value)
        if decimal_value != decimal_value.to_integral_value():
            raise ValueError("fractional minor amount in database")
        return int(decimal_value)


def str_enum(enum_cls: type[StrEnum], column: str, *, length: int = 32) -> sa.Enum:
    """VARCHAR + CHECK constraint for a StrEnum (values, not member names, are stored).

    ``column`` names the CHECK constraint (``ck_<table>_<column>``): two columns of the same enum in
    one table (``status``/``status_before_dispute``) must not produce clashing constraint names.
    """
    return sa.Enum(
        enum_cls,
        name=column,
        native_enum=False,
        create_constraint=True,
        length=length,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
    )


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        int: sa.BigInteger(),
        str: sa.Text(),
        datetime: sa.DateTime(timezone=True),
        dict[str, Any]: JSONB(),
        bool: sa.Boolean(),
    }


def created_at_column() -> Mapped[datetime]:
    return mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def updated_at_column() -> Mapped[datetime]:
    return mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )
