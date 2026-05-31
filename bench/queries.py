"""Query generation using SQLAlchemy Core.

Builds the tier-scoped SELECT projections that share identical FROM / WHERE /
ORDER BY clauses, differing only in their column lists.  Queries are compiled
to raw SQL strings for hand-off to psycopg3's pipeline or async pool.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from bench.schema import TIER_0_COLS, TIER_1_COLS, TIER_2_COLS, articles


def _base_query(
    columns: list[sa.Column[Any]],
    *,
    where: sa.ColumnElement[bool] | None = None,
    order_by: Any | None = None,
    limit: int | None = None,
) -> sa.Select[Any]:
    stmt = sa.select(*columns)
    if where is not None:
        stmt = stmt.where(where)
    if order_by is not None:
        stmt = stmt.order_by(order_by)
    if limit is not None:
        stmt = stmt.limit(limit)
    return stmt


def compile_sql(stmt: sa.Select[Any]) -> str:
    """Compile a SQLAlchemy select to a literal SQL string (Postgres dialect)."""
    from sqlalchemy.dialects import postgresql

    compiled = stmt.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    return str(compiled)


def tier_queries(
    *,
    limit: int,
    where: sa.ColumnElement[bool] | None = None,
    order_by: Any | None = None,
) -> dict[int, str]:
    """Return compiled SQL for each tier (0, 1, 2).

    All queries share the same FROM, WHERE, ORDER BY — only SELECT differs.
    """
    if order_by is None:
        order_by = articles.c.created_at.desc()
    tiers = {
        0: TIER_0_COLS,
        1: TIER_1_COLS,
        2: TIER_2_COLS,
    }
    return {
        tier: compile_sql(
            _base_query(cols, where=where, order_by=order_by, limit=limit)
        )
        for tier, cols in tiers.items()
    }


def monolithic_query(
    *,
    limit: int,
    where: sa.ColumnElement[bool] | None = None,
    order_by: Any | None = None,
) -> str:
    """Return compiled SQL for the baseline SELECT * query."""
    if order_by is None:
        order_by = articles.c.created_at.desc()
    all_cols = TIER_0_COLS + TIER_1_COLS + TIER_2_COLS
    return compile_sql(
        _base_query(all_cols, where=where, order_by=order_by, limit=limit)
    )


def explain_wrap(sql: str) -> str:
    """Wrap a query with EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)."""
    return f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}"
