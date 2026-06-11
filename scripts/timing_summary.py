#!/usr/bin/env python3
"""Summarize tartci per-job timing.tsv files."""

from __future__ import annotations

import argparse
import os
import pathlib
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass


DEFAULT_LOG_DIRS = (
    pathlib.Path.home() / "VMs/logs/tartci-win",
    pathlib.Path.home() / "VMs/logs/tartci-linux",
)


@dataclass(frozen=True)
class TimingRecord:
    path: pathlib.Path
    provider: str
    runner: str
    phases: dict[str, float]


def parse_timing(path: pathlib.Path) -> dict[str, float]:
    phases: dict[str, float] = {}
    with path.open("r", encoding="utf-8") as fh:
        header = fh.readline().strip().split("\t")
        if header != ["phase", "seconds"]:
            raise ValueError("expected header: phase<TAB>seconds")
        for line_no, line in enumerate(fh, start=2):
            line = line.strip()
            if not line:
                continue
            cols = line.split("\t")
            if len(cols) != 2:
                raise ValueError(f"line {line_no}: expected two tab-separated columns")
            phase, seconds = cols
            try:
                phases[phase] = float(seconds)
            except ValueError as exc:
                raise ValueError(f"line {line_no}: invalid seconds {seconds!r}") from exc
    if "total" not in phases:
        raise ValueError("missing total phase")
    return phases


def infer_provider(path: pathlib.Path) -> str:
    parts = {part.lower() for part in path.parts}
    text = str(path).lower()
    if "tartci-win" in parts or "qemu" in text or "win-ephr" in text:
        return "windows"
    if "tartci-linux" in parts or "linux-ephr" in text:
        return "linux"
    return "unknown"


def collect(paths: list[pathlib.Path]) -> tuple[list[TimingRecord], list[str]]:
    records: list[TimingRecord] = []
    warnings: list[str] = []
    for root in paths:
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = sorted(root.rglob("timing.tsv"))
        else:
            warnings.append(f"missing path: {root}")
            continue
        for path in candidates:
            try:
                phases = parse_timing(path)
            except ValueError as exc:
                warnings.append(f"skip {path}: {exc}")
                continue
            records.append(
                TimingRecord(
                    path=path,
                    provider=infer_provider(path),
                    runner=path.parent.name,
                    phases=phases,
                )
            )
    return records, warnings


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    fraction = index - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def fmt(seconds: float) -> str:
    return f"{seconds:.1f}s"


def print_table(rows: list[list[str]]) -> None:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row_index, row in enumerate(rows):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
        if row_index == 0:
            print("  ".join("-" * widths[i] for i in range(len(row))))


def summarize(records: list[TimingRecord], limit: int) -> None:
    if not records:
        print("No timing.tsv files found.")
        return

    by_provider: dict[str, list[TimingRecord]] = defaultdict(list)
    for record in records:
        by_provider[record.provider].append(record)

    phase_names = sorted({phase for record in records for phase in record.phases})
    preferred = ["boot_to_ssh", "preflight", "runner_process", "post_diag", "cleanup", "total"]
    phase_order = [phase for phase in preferred if phase in phase_names]
    phase_order.extend(phase for phase in phase_names if phase not in phase_order)

    for provider in sorted(by_provider):
        group = by_provider[provider]
        print(f"{provider} ({len(group)} job{'s' if len(group) != 1 else ''})")
        rows = [["phase", "median", "p90", "min", "max"]]
        for phase in phase_order:
            values = [record.phases[phase] for record in group if phase in record.phases]
            if not values:
                continue
            rows.append(
                [
                    phase,
                    fmt(statistics.median(values)),
                    fmt(percentile(values, 0.90)),
                    fmt(min(values)),
                    fmt(max(values)),
                ]
            )
        print_table(rows)
        print()

    recent = sorted(records, key=lambda record: record.path.stat().st_mtime, reverse=True)[:limit]
    if recent:
        print(f"recent ({len(recent)})")
        rows = [["provider", "runner", "total", "path"]]
        for record in recent:
            rows.append(
                [
                    record.provider,
                    record.runner,
                    fmt(record.phases["total"]),
                    str(record.path),
                ]
            )
        print_table(rows)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Summarize tartci timing.tsv files")
    parser.add_argument(
        "paths",
        nargs="*",
        type=pathlib.Path,
        help="timing.tsv files or log roots; defaults to tartci Windows/Linux log roots",
    )
    parser.add_argument("--recent", type=int, default=8, help="recent job rows to print")
    args = parser.parse_args(argv)

    paths = args.paths or [path for path in DEFAULT_LOG_DIRS if path.exists()]
    env_paths = os.environ.get("TARTCI_TIMING_PATHS")
    if not args.paths and env_paths:
        paths = [pathlib.Path(part) for part in env_paths.split(os.pathsep) if part]

    records, warnings = collect(paths)
    summarize(records, max(args.recent, 0))
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0 if records else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
