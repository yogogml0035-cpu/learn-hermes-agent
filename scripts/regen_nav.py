#!/usr/bin/env python3
"""Regenerate per-chapter nav bars across docs/zh/ and docs/en/.

Each `docs/{zh,en}/sNN-*.md` file (NN in 00..27) carries a nav line on line 3,
wrapped in backticks, enumerating every chapter with the current one in `[ ]`.
This script rewrites that line from a canonical template so nav stays in sync
as chapters are added or renumbered.

Skips non-chapter markdown files (e.g. glossary.md) and chapter files that
don't have a recognisable nav line on line 3.

Usage:
    python scripts/regen_nav.py

Re-run after adding new chapters.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TOTAL_CHAPTERS = 28  # s00..s27 inclusive
NAV_LINE_INDEX = 2   # third line, 0-indexed
NAV_LINE_PATTERN = re.compile(r"^`s\d{2}\s*>")
CHAPTER_FILE_PATTERN = re.compile(r"^s(\d{2})-.*\.md$")


def build_nav(current_chapter: int) -> str:
    parts = [f"s{i:02d}" for i in range(TOTAL_CHAPTERS)]
    parts[current_chapter] = f"[ s{current_chapter:02d} ]"
    return "`" + " > ".join(parts) + "`"


def process_file(path: Path) -> tuple[str, str | None]:
    match = CHAPTER_FILE_PATTERN.match(path.name)
    if not match:
        return "skipped", "not-chapter-file"
    chapter = int(match.group(1))
    if chapter >= TOTAL_CHAPTERS:
        return "skipped", f"chapter-out-of-range:s{chapter:02d}"

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if len(lines) <= NAV_LINE_INDEX:
        return "skipped", "file-too-short"

    nav_line = lines[NAV_LINE_INDEX]
    if not NAV_LINE_PATTERN.match(nav_line):
        return "skipped", "no-nav-line"

    # Preserve trailing newline style
    newline = "\n" if nav_line.endswith("\n") else ""
    new_line = build_nav(chapter) + newline
    if nav_line == new_line:
        return "unchanged", None

    lines[NAV_LINE_INDEX] = new_line
    path.write_text("".join(lines), encoding="utf-8")
    return "updated", None


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    targets = [root / "docs" / "zh", root / "docs" / "en"]
    summary = {"updated": 0, "unchanged": 0, "skipped": 0}

    for target_dir in targets:
        if not target_dir.exists():
            print(f"missing directory: {target_dir}", file=sys.stderr)
            continue
        for md_path in sorted(target_dir.glob("s*.md")):
            status, detail = process_file(md_path)
            summary[status] += 1
            rel = md_path.relative_to(root)
            tag = status.upper().ljust(9)
            suffix = f" ({detail})" if detail else ""
            print(f"  [{tag}] {rel}{suffix}")

    print()
    print(
        f"updated={summary['updated']}, "
        f"unchanged={summary['unchanged']}, "
        f"skipped={summary['skipped']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
