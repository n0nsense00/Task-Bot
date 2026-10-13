"""Read complete CSV records while skipping comments between records."""
from __future__ import annotations

import csv
from itertools import chain
from pathlib import Path


def read_records(
    path: Path, accepted_headers: tuple[tuple[str, ...], ...]
) -> list[tuple[int, dict[str, str]]]:
    """Return source line numbers and exact fields, preserving quoted newlines.

    Read the first physical line of each record ourselves, then let csv.reader
    consume its continuation lines. This keeps comments/blank lines inside a
    quoted value intact and makes error locations match the original file.
    """
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    rows: list[tuple[int, dict[str, str]]] = []
    header: tuple[str, ...] | None = None
    line_no = 0
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        while first_line := source.readline():
            line_no += 1
            if not first_line.strip() or first_line.lstrip().startswith("#"):
                continue
            start_line = line_no
            reader = csv.reader(chain((first_line,), source), strict=True)
            try:
                values = next(reader)
            except csv.Error as exc:
                raise ValueError(f"line {start_line}: invalid CSV: {exc}") from exc
            line_no += reader.line_num - 1
            if header is None:
                header = tuple(values)
                if header not in accepted_headers:
                    expected = "\n  ".join(",".join(fields) for fields in accepted_headers)
                    raise ValueError(f"CSV header must be one of:\n  {expected}\n  got: {values}")
                continue
            if len(values) != len(header):
                raise ValueError(
                    f"line {start_line}: expected {len(header)} fields, got {len(values)}"
                )
            rows.append((start_line, dict(zip(header, values))))

    if header is None:
        raise ValueError(f"{path} contains no data rows (only comments and blanks).")
    return rows
