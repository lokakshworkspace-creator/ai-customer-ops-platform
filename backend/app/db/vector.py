"""A minimal pgvector column type: just enough for the schema (no pgvector Python package).

Vectors are written and read through explicit SQL (`CAST(:v AS vector)` with a text literal, see
`vector_literal`), never through the ORM, so this type only has to describe the column to
SQLAlchemy and Alembic.
"""

from collections.abc import Sequence

from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType):
    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(self, **kw: object) -> str:
        return f"VECTOR({self.dim})"


def vector_literal(values: Sequence[float]) -> str:
    """The text form pgvector accepts: ``[0.1,0.2,...]`` (cast it with ``CAST(:v AS vector)``)."""
    return "[" + ",".join(repr(float(x)) for x in values) + "]"
