#!/usr/bin/env python3
"""Shared helpers for tartci timing.tsv files."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass


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
    if "tartci-macos" in parts or "pulp-vm" in text or "macos" in text:
        return "macos"
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
