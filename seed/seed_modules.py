"""Bulk-load modules from seed/seed_modules.csv into the modules table.

Usage (from the project root, venv active):

    python seed/seed_modules.py                 # append (insert-or-replace)
    python seed/seed_modules.py --replace       # wipe modules first (prompts y/n)
    python seed/seed_modules.py --file path.csv # load from a different CSV

Validates every row before any DB write. Mirrors the seed_tasks.py
contract — atomic on failure, line-numbered errors point to the source CSV.
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

# Make the project root importable when invoked as ``python seed/seed_modules.py``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from database.db import (  # noqa: E402
    add_module,
    count_modules,
    delete_all_modules,
    init_db,
)
from database.models import Module  # noqa: E402

EXPECTED_COLUMNS: tuple[str, ...] = ("code", "name")
DEFAULT_CSV_PATH: Path = _PROJECT_ROOT / "seed" / "seed_modules.csv"


@dataclass
class _RawRow:
    """A parsed CSV row plus its 1-indexed line number in the source file."""

    line_no: int
    data: dict[str, str]


def _read_rows(path: Path) -> list[_RawRow]:
    """Read ``path``, skipping comments and blanks. Return rows with line numbers.

    UTF-8-sig encoding strips any BOM Excel might inject; same defensive
    handling as seed_tasks.py.
    """
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        raw_lines = f.readlines()

    kept: list[tuple[int, str]] = []
    for i, line in enumerate(raw_lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        kept.append((i, line))

    if not kept:
        raise ValueError(
            f"{path} contains no data rows (only comments and blanks)."
        )

    header_line = kept[0][1]
    data_entries = kept[1:]

    reader = csv.DictReader([header_line] + [ln for _, ln in data_entries])
    if tuple(reader.fieldnames or ()) != EXPECTED_COLUMNS:
        raise ValueError(
            "CSV header must be exactly: "
            + ",".join(EXPECTED_COLUMNS)
            + f"\n  got: {reader.fieldnames}"
        )

    rows: list[_RawRow] = []
    for (line_no, _), parsed in zip(data_entries, reader):
        rows.append(_RawRow(line_no=line_no, data=parsed))
    return rows


def _validate_row(raw: _RawRow) -> Module:
    """Convert a raw CSV row into a ``Module`` or raise ``ValueError``."""
    code = (raw.data.get("code") or "").strip()
    if not code:
        raise ValueError("code is required")
    name = (raw.data.get("name") or "").strip() or None
    return Module(code=code, name=name)


def _confirm_replace(existing_count: int) -> bool:
    """Prompt for y/n confirmation of ``--replace``."""
    prompt = (
        f"--replace will DELETE all {existing_count} existing module(s) and "
        "then insert the new ones. Are you sure? (y/n): "
    )
    response = input(prompt).strip().lower()
    return response in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    """Parse args, validate CSV, optionally replace, insert, print summary."""
    parser = argparse.ArgumentParser(
        description="Bulk-load modules from a CSV into the Task-Bot modules table.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete all existing modules before importing (prompts y/n).",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_CSV_PATH,
        help=f"CSV to import (default: {DEFAULT_CSV_PATH}).",
    )
    args = parser.parse_args(argv)

    try:
        raw_rows = _read_rows(args.file)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not raw_rows:
        print("ERROR: CSV has no data rows to import.", file=sys.stderr)
        return 1

    modules: list[Module] = []
    for raw in raw_rows:
        try:
            modules.append(_validate_row(raw))
        except ValueError as exc:
            print(
                f"ERROR: line {raw.line_no}: {exc}\n  row: {raw.data}",
                file=sys.stderr,
            )
            print("Aborting — no modules inserted.", file=sys.stderr)
            return 1

    init_db()

    if args.replace:
        existing = count_modules()
        if not _confirm_replace(existing):
            print("Aborted. No changes made.")
            return 1
        deleted = delete_all_modules()
        print(f"Deleted {deleted} existing module(s).")

    for module in modules:
        add_module(module)

    print(f"Imported {len(modules)} module(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
