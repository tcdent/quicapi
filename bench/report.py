"""Generate a Markdown summary report from benchmark results.

Used by the CI workflow to produce a GitHub Actions job summary.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _fmt(val: Any, precision: int = 3) -> str:
    if val is None:
        return "—"
    if isinstance(val, float):
        return f"{val:.{precision}f}"
    return str(val)


def generate_markdown(results: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append("# Postgres Tiered Query Benchmark Results\n")

    # Group by (row_count, toast_size).
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for r in results:
        key = (r["row_count"], r["toast_size"])
        groups.setdefault(key, []).append(r)

    # -- Overview table -------------------------------------------------------
    lines.append("## Summary\n")
    lines.append(
        "| Strategy | Rows | Toast | Wall (ms) | 1st Row (ms) | All Complete (ms) | Mem Δ (KB) |"
    )
    lines.append(
        "|----------|-----:|-------|----------:|-------------:|------------------:|-----------:|"
    )
    for (rc, ts), runs in sorted(groups.items()):
        for r in runs:
            lines.append(
                f"| {r['strategy']} | {rc} | {ts} "
                f"| {_fmt(r['wall_time_ms'])} "
                f"| {_fmt(r.get('time_to_first_row_ms'))} "
                f"| {_fmt(r.get('time_to_all_complete_ms'))} "
                f"| {_fmt(r.get('memory_delta_kb'), 1)} |"
            )

    # -- Tier detail tables ---------------------------------------------------
    lines.append("\n## Tier Detail\n")
    lines.append(
        "| Strategy | Rows | Toast | Tier | 1st Row (ms) | Complete (ms) "
        "| Plan (ms) | Shared Hits | Shared Reads | Checkout (ms) |"
    )
    lines.append(
        "|----------|-----:|-------|-----:|-------------:|--------------:"
        "|----------:|------------:|-------------:|--------------:|"
    )
    for (rc, ts), runs in sorted(groups.items()):
        for r in runs:
            for t in r.get("tiers", []):
                tier_label = "all" if t["tier"] == -1 else str(t["tier"])
                lines.append(
                    f"| {r['strategy']} | {rc} | {ts} | {tier_label} "
                    f"| {_fmt(t.get('first_row_ms'))} "
                    f"| {_fmt(t.get('complete_ms'))} "
                    f"| {_fmt(t.get('planning_time_ms'))} "
                    f"| {_fmt(t.get('shared_hits'))} "
                    f"| {_fmt(t.get('shared_reads'))} "
                    f"| {_fmt(t.get('checkout_ms'))} |"
                )

    # -- Key comparisons ------------------------------------------------------
    lines.append("\n## Key Comparisons\n")

    # For each (row_count, toast_size), compare time-to-first-row.
    lines.append("### Time to First Row — Tiered vs Baseline\n")
    lines.append("| Rows | Toast | Baseline (ms) | Pipeline (ms) | Parallel (ms) | Pipeline Δ | Parallel Δ |")
    lines.append("|-----:|-------|-------------:|-------------:|-------------:|-----------:|-----------:|")
    for (rc, ts), runs in sorted(groups.items()):
        by_strat = {r["strategy"]: r for r in runs}
        base_first = by_strat.get("baseline", {}).get("time_to_first_row_ms")
        pipe_first = by_strat.get("pipeline", {}).get("time_to_first_row_ms")
        para_first = by_strat.get("parallel", {}).get("time_to_first_row_ms")

        def _delta(a: float | None, b: float | None) -> str:
            if a is None or b is None or b == 0:
                return "—"
            pct = ((a - b) / b) * 100
            sign = "+" if pct > 0 else ""
            return f"{sign}{pct:.1f}%"

        lines.append(
            f"| {rc} | {ts} "
            f"| {_fmt(base_first)} "
            f"| {_fmt(pipe_first)} "
            f"| {_fmt(para_first)} "
            f"| {_delta(pipe_first, base_first)} "
            f"| {_delta(para_first, base_first)} |"
        )

    # Total wall time comparison.
    lines.append("\n### Total Wall Time — Tiered vs Baseline\n")
    lines.append("| Rows | Toast | Baseline (ms) | Pipeline (ms) | Parallel (ms) | Pipeline Δ | Parallel Δ |")
    lines.append("|-----:|-------|-------------:|-------------:|-------------:|-----------:|-----------:|")
    for (rc, ts), runs in sorted(groups.items()):
        by_strat = {r["strategy"]: r for r in runs}
        base_wall = by_strat.get("baseline", {}).get("wall_time_ms")
        pipe_wall = by_strat.get("pipeline", {}).get("wall_time_ms")
        para_wall = by_strat.get("parallel", {}).get("wall_time_ms")

        def _delta(a: float | None, b: float | None) -> str:
            if a is None or b is None or b == 0:
                return "—"
            pct = ((a - b) / b) * 100
            sign = "+" if pct > 0 else ""
            return f"{sign}{pct:.1f}%"

        lines.append(
            f"| {rc} | {ts} "
            f"| {_fmt(base_wall)} "
            f"| {_fmt(pipe_wall)} "
            f"| {_fmt(para_wall)} "
            f"| {_delta(pipe_wall, base_wall)} "
            f"| {_delta(para_wall, base_wall)} |"
        )

    lines.append("")
    return "\n".join(lines)


def main() -> None:
    """Read results.json from stdin or first arg, print Markdown to stdout."""
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            results = json.load(f)
    else:
        results = json.load(sys.stdin)

    print(generate_markdown(results))


if __name__ == "__main__":
    main()
