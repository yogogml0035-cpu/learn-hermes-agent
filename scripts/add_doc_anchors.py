#!/usr/bin/env python3
"""Insert bidirectional doc anchors into agents/sNN_*.py docstrings.

Each `agents/sNN_*.py` gets a single `See:` line pointing to the matching
`docs/zh/sNN-*.md` and `docs/en/sNN-*.md` chapter docs, placed inside the
module docstring right after the title.

Before:                                After:
    \"\"\"                                   \"\"\"
    s01: The Agent Loop ...                s01: The Agent Loop ...

    The simplest possible agent ...        See: docs/zh/s01-the-agent-loop.md | docs/en/s01-the-agent-loop.md

                                           The simplest possible agent ...

Idempotent: skips files that already contain a `See: docs/` line.

Usage:
    python scripts/add_doc_anchors.py

Re-run after adding new chapters.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

AGENT_FILE_PATTERN = re.compile(r"^s(\d{2})_.*\.py$")
SEE_MARKER = "See: docs/"


def find_doc_for_chapter(docs_root: Path, lang: str, chapter: int) -> Path | None:
    lang_dir = docs_root / lang
    if not lang_dir.exists():
        return None
    prefix = f"s{chapter:02d}-"
    for md_path in sorted(lang_dir.glob(f"{prefix}*.md")):
        return md_path
    return None


def process_file(agent_path: Path, docs_root: Path) -> tuple[str, str | None]:
    match = AGENT_FILE_PATTERN.match(agent_path.name)
    if not match:
        return "skipped", "not-chapter-file"
    chapter = int(match.group(1))

    zh_doc = find_doc_for_chapter(docs_root, "zh", chapter)
    en_doc = find_doc_for_chapter(docs_root, "en", chapter)
    if zh_doc is None or en_doc is None:
        return "skipped", f"no-doc-found:s{chapter:02d}"

    rel_zh = f"docs/zh/{zh_doc.name}"
    rel_en = f"docs/en/{en_doc.name}"
    see_line = f"See: {rel_zh} | {rel_en}"

    text = agent_path.read_text(encoding="utf-8")
    if SEE_MARKER in text:
        return "unchanged", "already-has-anchor"

    lines = text.split("\n")
    # Expected shape: lines[0]='"""', lines[1]=title, lines[2]='', lines[3]=description...
    if (
        len(lines) < 4
        or lines[0].strip() != '"""'
        or lines[1].strip() == ""
        or lines[2].strip() != ""
    ):
        return "skipped", "unexpected-docstring-shape"

    new_lines = lines[:3] + [see_line, ""] + lines[3:]
    agent_path.write_text("\n".join(new_lines), encoding="utf-8")
    return "updated", None


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    agents_dir = root / "agents"
    docs_root = root / "docs"

    if not agents_dir.exists():
        print(f"missing directory: {agents_dir}", file=sys.stderr)
        return 1

    summary = {"updated": 0, "unchanged": 0, "skipped": 0}
    for py_path in sorted(agents_dir.glob("s*.py")):
        status, detail = process_file(py_path, docs_root)
        summary[status] += 1
        rel = py_path.relative_to(root)
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
