"""Benchmark execution strategies.

Three strategies, each returning a RunMetrics instance:

- ``run_baseline``   — single monolithic SELECT * query
- ``run_pipeline``   — psycopg3 pipeline mode, all tiers on one connection
- ``run_parallel``   — each tier on its own async pool connection
"""

from __future__ import annotations

import asyncio
from typing import Any

import psycopg
from psycopg import AsyncConnection, sql as psql
from psycopg_pool import AsyncConnectionPool

from bench.metrics import (
    RunMetrics,
    TierMetrics,
    capture_rss,
    now_ns,
    parse_explain_buffers,
)
from bench.queries import explain_wrap, monolithic_query, tier_queries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fetch_with_metrics(
    conn: AsyncConnection[Any],
    query: str,
    tier: int,
    start_ns: int,
    *,
    explain: bool = False,
) -> TierMetrics:
    """Execute *query* on *conn* and populate a TierMetrics."""
    tm = TierMetrics(tier=tier)
    cur = await conn.execute(psql.SQL(query))
    row_count = 0
    async for _row in cur:
        if row_count == 0:
            tm.first_row_ns = now_ns()
        row_count += 1
    tm.row_count = row_count
    tm.complete_ns = now_ns()

    # Optionally run EXPLAIN ANALYZE for buffer stats.
    if explain:
        ex_cur = await conn.execute(psql.SQL(explain_wrap(query)))
        rows = await ex_cur.fetchall()
        stats = parse_explain_buffers([r[0] for r in rows])
        tm.planning_time_ms = stats.get("planning_time_ms")
        tm.execution_time_ms = stats.get("execution_time_ms")
        tm.shared_hits = stats.get("shared_hits")
        tm.shared_reads = stats.get("shared_reads")

    return tm


# ---------------------------------------------------------------------------
# Baseline — single monolithic query
# ---------------------------------------------------------------------------

async def run_baseline(
    dsn: str,
    *,
    row_limit: int,
    toast_size: str,
    explain: bool = True,
) -> RunMetrics:
    """Run a standard ``SELECT *`` query and collect metrics."""
    metrics = RunMetrics(
        strategy="baseline",
        row_count=row_limit,
        toast_size=toast_size,
    )
    query = monolithic_query(limit=row_limit)
    metrics.memory_rss_before = capture_rss()
    metrics.start_ns = now_ns()

    async with await AsyncConnection.connect(dsn) as conn:
        await conn.set_autocommit(False)
        await conn.execute(
            psql.SQL("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        )

        tm = TierMetrics(tier=-1)  # -1 signals "monolithic"
        cur = await conn.execute(psql.SQL(query))
        row_count = 0
        async for _row in cur:
            if row_count == 0:
                tm.first_row_ns = now_ns()
            row_count += 1
        tm.row_count = row_count
        tm.complete_ns = now_ns()

        if explain:
            ex_cur = await conn.execute(psql.SQL(explain_wrap(query)))
            rows = await ex_cur.fetchall()
            stats = parse_explain_buffers([r[0] for r in rows])
            tm.planning_time_ms = stats.get("planning_time_ms")
            tm.execution_time_ms = stats.get("execution_time_ms")
            tm.shared_hits = stats.get("shared_hits")
            tm.shared_reads = stats.get("shared_reads")

        metrics.tiers.append(tm)

    metrics.end_ns = now_ns()
    metrics.memory_rss_after = capture_rss()
    return metrics


# ---------------------------------------------------------------------------
# Strategy A — Pipeline mode
# ---------------------------------------------------------------------------

async def run_pipeline(
    dsn: str,
    *,
    row_limit: int,
    toast_size: str,
    explain: bool = True,
) -> RunMetrics:
    """Send all tier queries through psycopg3's pipeline on one connection."""
    metrics = RunMetrics(
        strategy="pipeline",
        row_count=row_limit,
        toast_size=toast_size,
    )
    queries = tier_queries(limit=row_limit)
    metrics.memory_rss_before = capture_rss()
    metrics.start_ns = now_ns()

    async with await AsyncConnection.connect(dsn) as conn:
        await conn.set_autocommit(False)
        await conn.execute(
            psql.SQL("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        )

        async with conn.pipeline():
            # Send all tier queries in the pipeline batch.
            cursors = {}
            for tier_id in sorted(queries):
                cursors[tier_id] = await conn.execute(
                    psql.SQL(queries[tier_id])
                )

        # Pipeline has been flushed; now iterate results in tier order.
        for tier_id in sorted(cursors):
            tm = TierMetrics(tier=tier_id)
            cur = cursors[tier_id]
            row_count = 0
            async for _row in cur:
                if row_count == 0:
                    tm.first_row_ns = now_ns()
                row_count += 1
            tm.row_count = row_count
            tm.complete_ns = now_ns()
            metrics.tiers.append(tm)

        # Collect EXPLAIN stats outside the pipeline.
        if explain:
            for tm in metrics.tiers:
                tier_id = tm.tier
                ex_cur = await conn.execute(
                    psql.SQL(explain_wrap(queries[tier_id]))
                )
                rows = await ex_cur.fetchall()
                stats = parse_explain_buffers([r[0] for r in rows])
                tm.planning_time_ms = stats.get("planning_time_ms")
                tm.execution_time_ms = stats.get("execution_time_ms")
                tm.shared_hits = stats.get("shared_hits")
                tm.shared_reads = stats.get("shared_reads")

    metrics.end_ns = now_ns()
    metrics.memory_rss_after = capture_rss()
    return metrics


# ---------------------------------------------------------------------------
# Strategy B — Parallel connections
# ---------------------------------------------------------------------------

async def _run_tier_parallel(
    pool: AsyncConnectionPool,
    query: str,
    tier_id: int,
    start_ns: int,
    *,
    explain: bool = True,
) -> TierMetrics:
    """Execute a single tier query on its own pool connection."""
    tm = TierMetrics(tier=tier_id)
    checkout_start = now_ns()

    async with pool.connection() as conn:
        tm.checkout_ns = now_ns() - checkout_start
        await conn.set_autocommit(False)
        await conn.execute(
            psql.SQL("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        )

        cur = await conn.execute(psql.SQL(query))
        row_count = 0
        async for _row in cur:
            if row_count == 0:
                tm.first_row_ns = now_ns()
            row_count += 1
        tm.row_count = row_count
        tm.complete_ns = now_ns()

        if explain:
            ex_cur = await conn.execute(psql.SQL(explain_wrap(query)))
            rows = await ex_cur.fetchall()
            stats = parse_explain_buffers([r[0] for r in rows])
            tm.planning_time_ms = stats.get("planning_time_ms")
            tm.execution_time_ms = stats.get("execution_time_ms")
            tm.shared_hits = stats.get("shared_hits")
            tm.shared_reads = stats.get("shared_reads")

    return tm


async def run_parallel(
    dsn: str,
    *,
    row_limit: int,
    toast_size: str,
    explain: bool = True,
) -> RunMetrics:
    """Run each tier on its own connection from an async pool."""
    metrics = RunMetrics(
        strategy="parallel",
        row_count=row_limit,
        toast_size=toast_size,
    )
    queries = tier_queries(limit=row_limit)
    metrics.memory_rss_before = capture_rss()
    metrics.start_ns = now_ns()

    async with AsyncConnectionPool(dsn, min_size=3, max_size=5) as pool:
        tasks = [
            _run_tier_parallel(
                pool,
                queries[tier_id],
                tier_id,
                metrics.start_ns,
                explain=explain,
            )
            for tier_id in sorted(queries)
        ]
        tier_results = await asyncio.gather(*tasks)

    metrics.tiers = sorted(tier_results, key=lambda t: t.tier)
    metrics.end_ns = now_ns()
    metrics.memory_rss_after = capture_rss()
    return metrics
