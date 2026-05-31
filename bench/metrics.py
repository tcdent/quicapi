"""Metrics collection for the tiered query benchmark.

Captures wall time, time-to-first-row per tier, planning/buffer stats from
EXPLAIN ANALYZE, connection checkout latency, and memory high-water marks.
"""

from __future__ import annotations

import dataclasses
import json
import resource
import time
from typing import Any


@dataclasses.dataclass(slots=True)
class TierMetrics:
    """Metrics for a single tier within a strategy run."""

    tier: int
    first_row_ns: int | None = None  # perf_counter_ns when first row yielded
    complete_ns: int | None = None  # perf_counter_ns when all rows fetched
    row_count: int = 0
    planning_time_ms: float | None = None
    execution_time_ms: float | None = None
    shared_hits: int | None = None
    shared_reads: int | None = None
    # Connection checkout (strategy B only).
    checkout_ns: int | None = None


@dataclasses.dataclass(slots=True)
class RunMetrics:
    """Metrics for a complete strategy run."""

    strategy: str
    row_count: int
    toast_size: str
    start_ns: int = 0
    end_ns: int = 0
    tiers: list[TierMetrics] = dataclasses.field(default_factory=list)
    memory_rss_before: int = 0
    memory_rss_after: int = 0

    @property
    def wall_time_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000_000

    @property
    def time_to_first_row_ms(self) -> float | None:
        """Earliest first-row timestamp across all tiers (or the monolithic
        query).  This is the primary "time-to-first-meaningful-data" metric."""
        earliest: int | None = None
        for t in self.tiers:
            if t.first_row_ns is not None:
                if earliest is None or t.first_row_ns < earliest:
                    earliest = t.first_row_ns
        if earliest is not None:
            return (earliest - self.start_ns) / 1_000_000
        return None

    @property
    def time_to_all_complete_ms(self) -> float | None:
        """Time until the last tier finishes fetching all rows."""
        latest: int | None = None
        for t in self.tiers:
            if t.complete_ns is not None:
                if latest is None or t.complete_ns > latest:
                    latest = t.complete_ns
        if latest is not None:
            return (latest - self.start_ns) / 1_000_000
        return None

    @property
    def memory_delta_kb(self) -> float:
        return (self.memory_rss_after - self.memory_rss_before) / 1024

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "strategy": self.strategy,
            "row_count": self.row_count,
            "toast_size": self.toast_size,
            "wall_time_ms": round(self.wall_time_ms, 3),
            "time_to_first_row_ms": (
                round(self.time_to_first_row_ms, 3)
                if self.time_to_first_row_ms is not None
                else None
            ),
            "time_to_all_complete_ms": (
                round(self.time_to_all_complete_ms, 3)
                if self.time_to_all_complete_ms is not None
                else None
            ),
            "memory_delta_kb": round(self.memory_delta_kb, 1),
            "tiers": [],
        }
        for t in self.tiers:
            td: dict[str, Any] = {"tier": t.tier, "row_count": t.row_count}
            if t.first_row_ns is not None:
                td["first_row_ms"] = round(
                    (t.first_row_ns - self.start_ns) / 1_000_000, 3
                )
            if t.complete_ns is not None:
                td["complete_ms"] = round(
                    (t.complete_ns - self.start_ns) / 1_000_000, 3
                )
            if t.planning_time_ms is not None:
                td["planning_time_ms"] = round(t.planning_time_ms, 3)
            if t.execution_time_ms is not None:
                td["execution_time_ms"] = round(t.execution_time_ms, 3)
            if t.shared_hits is not None:
                td["shared_hits"] = t.shared_hits
            if t.shared_reads is not None:
                td["shared_reads"] = t.shared_reads
            if t.checkout_ns is not None:
                td["checkout_ms"] = round(t.checkout_ns / 1_000_000, 3)
            d["tiers"].append(td)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def capture_rss() -> int:
    """Return current RSS in bytes via getrusage (no extra deps)."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    # ru_maxrss is in KB on Linux, bytes on macOS.
    import sys

    if sys.platform == "linux":
        return usage.ru_maxrss * 1024
    return usage.ru_maxrss


def now_ns() -> int:
    return time.perf_counter_ns()


# -- EXPLAIN ANALYZE parsing -------------------------------------------------

def parse_explain_buffers(explain_output: list[Any]) -> dict[str, Any]:
    """Extract planning time, execution time, and buffer stats from
    ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` output.
    """
    if not explain_output:
        return {}

    # psycopg returns rows; the JSON plan is in the first column of the first
    # row, wrapped in a list.
    plan_json = explain_output
    if isinstance(plan_json, list) and len(plan_json) > 0:
        plan_json = plan_json[0]
    if isinstance(plan_json, (list, tuple)) and len(plan_json) > 0:
        plan_json = plan_json[0]
    if isinstance(plan_json, str):
        plan_json = json.loads(plan_json)
    if isinstance(plan_json, list) and len(plan_json) > 0:
        plan_json = plan_json[0]

    result: dict[str, Any] = {}
    if isinstance(plan_json, dict):
        result["planning_time_ms"] = plan_json.get("Planning Time")
        result["execution_time_ms"] = plan_json.get("Execution Time")

        # Walk the plan tree to sum buffer stats.
        shared_hits, shared_reads = _walk_plan_buffers(
            plan_json.get("Plan", {}), 0, 0
        )
        result["shared_hits"] = shared_hits
        result["shared_reads"] = shared_reads

    return result


def _walk_plan_buffers(
    node: dict[str, Any], hits: int, reads: int
) -> tuple[int, int]:
    hits += node.get("Shared Hit Blocks", 0)
    reads += node.get("Shared Read Blocks", 0)
    for child in node.get("Plans", []):
        hits, reads = _walk_plan_buffers(child, hits, reads)
    return hits, reads
