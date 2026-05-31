"""Benchmark runner — seeds the database, executes all strategy × variable
combinations, and emits a JSON report.

Usage::

    quicapi-bench                            # all defaults
    quicapi-bench --dsn postgresql://...     # custom DSN
    quicapi-bench --rows 100 --toast medium  # single combo
    quicapi-bench --output results.json      # write to file
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import psycopg
from psycopg import AsyncConnection, sql as psql

from bench.metrics import RunMetrics
from bench.schema import SETUP_SQL, ToastSize, generate_rows
from bench.strategies import run_baseline, run_parallel, run_pipeline

ROW_COUNTS = [10, 100, 1_000, 10_000]
TOAST_SIZES = [ToastSize.SMALL, ToastSize.MEDIUM, ToastSize.LARGE, ToastSize.MIXED]


async def _seed(dsn: str, count: int, toast_size: ToastSize) -> None:
    """Recreate the table, insert seed rows, vacuum analyze."""
    async with await AsyncConnection.connect(dsn) as conn:
        await conn.set_autocommit(True)
        await conn.execute(psql.SQL(SETUP_SQL))

    async with await AsyncConnection.connect(dsn) as conn:
        await conn.set_autocommit(True)
        await conn.execute(psql.SQL("SET synchronous_commit = off"))

        rows = generate_rows(count, toast_size)
        # Batch insert using executemany.
        insert_sql = psql.SQL(
            "INSERT INTO articles (id, slug, author_id, title, summary, body, metadata) "
            "VALUES (%(id)s, %(slug)s, %(author_id)s, %(title)s, %(summary)s, "
            "%(body)s, %(metadata)s::jsonb)"
        )
        # psycopg3 async doesn't have executemany on the connection directly;
        # use a cursor.
        async with conn.cursor() as cur:
            for row in rows:
                row_copy = dict(row)
                row_copy["metadata"] = json.dumps(row_copy["metadata"])
                await cur.execute(insert_sql, row_copy)

    # Re-enable synchronous commit and run VACUUM ANALYZE.
    async with await AsyncConnection.connect(dsn) as conn:
        await conn.set_autocommit(True)
        await conn.execute(psql.SQL("SET synchronous_commit = on"))
        await conn.execute(psql.SQL("VACUUM ANALYZE articles"))

    # Reset pg_stat_statements if available.
    async with await AsyncConnection.connect(dsn) as conn:
        await conn.set_autocommit(True)
        try:
            await conn.execute(
                psql.SQL("SELECT pg_stat_statements_reset()")
            )
        except psycopg.errors.UndefinedFunction:
            pass  # Extension not installed — skip.


async def _run_combination(
    dsn: str,
    row_count: int,
    toast_size: ToastSize,
    strategies: list[str],
    *,
    explain: bool = True,
    warmup_runs: int = 1,
) -> list[dict[str, Any]]:
    """Seed the database and run all requested strategies for one combination."""
    await _seed(dsn, row_count, toast_size)

    results: list[dict[str, Any]] = []
    strategy_map = {
        "baseline": run_baseline,
        "pipeline": run_pipeline,
        "parallel": run_parallel,
    }

    for name in strategies:
        fn = strategy_map[name]

        # Warmup runs (not recorded).
        for _ in range(warmup_runs):
            await fn(dsn, row_limit=row_count, toast_size=toast_size.value, explain=False)

        # Actual measured run.
        metrics: RunMetrics = await fn(
            dsn,
            row_limit=row_count,
            toast_size=toast_size.value,
            explain=explain,
        )
        results.append(metrics.to_dict())

    return results


async def run_all(
    dsn: str,
    *,
    row_counts: list[int] | None = None,
    toast_sizes: list[ToastSize] | None = None,
    strategies: list[str] | None = None,
    explain: bool = True,
    warmup_runs: int = 1,
) -> list[dict[str, Any]]:
    """Execute the full benchmark matrix and return a list of result dicts."""
    row_counts = row_counts or ROW_COUNTS
    toast_sizes = toast_sizes or TOAST_SIZES
    strategies = strategies or ["baseline", "pipeline", "parallel"]

    all_results: list[dict[str, Any]] = []
    total = len(row_counts) * len(toast_sizes)
    done = 0

    for rc in row_counts:
        for ts in toast_sizes:
            done += 1
            print(
                f"[{done}/{total}] rows={rc} toast={ts.value} ...",
                file=sys.stderr,
                flush=True,
            )
            combo_results = await _run_combination(
                dsn, rc, ts, strategies,
                explain=explain, warmup_runs=warmup_runs,
            )
            all_results.extend(combo_results)

    return all_results


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Postgres tiered query benchmark runner"
    )
    p.add_argument(
        "--dsn",
        default="postgresql://postgres:postgres@localhost:5432/quicapi_bench",
        help="PostgreSQL connection string",
    )
    p.add_argument(
        "--rows",
        type=int,
        nargs="+",
        default=None,
        help="Row counts to test (default: 10 100 1000 10000)",
    )
    p.add_argument(
        "--toast",
        choices=["small", "medium", "large", "mixed"],
        nargs="+",
        default=None,
        help="TOAST payload sizes to test (default: all including mixed)",
    )
    p.add_argument(
        "--strategies",
        choices=["baseline", "pipeline", "parallel"],
        nargs="+",
        default=None,
        help="Strategies to benchmark (default: all)",
    )
    p.add_argument(
        "--no-explain",
        action="store_true",
        help="Skip EXPLAIN ANALYZE collection",
    )
    p.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="Number of warmup runs per strategy (default: 1)",
    )
    p.add_argument(
        "--output",
        default=None,
        help="Write JSON results to file (default: stdout)",
    )
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    toast_sizes = (
        [ToastSize(t) for t in args.toast] if args.toast else None
    )

    results = asyncio.run(
        run_all(
            args.dsn,
            row_counts=args.rows,
            toast_sizes=toast_sizes,
            strategies=args.strategies,
            explain=not args.no_explain,
            warmup_runs=args.warmup,
        )
    )

    output = json.dumps(results, indent=2)
    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
            f.write("\n")
        print(f"Results written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
