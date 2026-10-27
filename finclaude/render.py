"""Reports as readable pages: the report is written in Markdown and saved only as report.html, with tables, lists
and fold-out explanations (<details markdown="1">) rendered. Nothing is fetched: the page is one file.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

import markdown

CSS = """
:root { --bg: #ffffff; --text: #1f2328; --muted: #59636e; --line: #d1d9e0; --head: #f6f8fa; --accent: #0969da; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1117; --text: #e6edf3; --muted: #9198a1; --line: #3d444d; --head: #151b23; --accent: #4493f8; }
}
body { background: var(--bg); color: var(--text); margin: 0; padding: 24px 16px 64px;
       font: 16px/1.7 "Microsoft YaHei", "PingFang SC", -apple-system, "Segoe UI", sans-serif; }
main { max-width: 920px; margin: 0 auto; }
h1 { font-size: 1.6em; border-bottom: 1px solid var(--line); padding-bottom: .3em; }
h2 { font-size: 1.3em; margin-top: 2em; border-bottom: 1px solid var(--line); padding-bottom: .2em; }
h3 { font-size: 1.15em; margin-top: 2.2em; color: var(--accent); }
table { border-collapse: collapse; margin: 1em 0; display: block; overflow-x: auto; max-width: 100%; }
th, td { border: 1px solid var(--line); padding: 6px 10px; text-align: left; white-space: nowrap; }
th { background: var(--head); }
td:not(:first-child) { font-variant-numeric: tabular-nums; }
li { margin: .2em 0; }
code { background: var(--head); padding: .1em .3em; border-radius: 4px; }
"""


LIST_ITEM = re.compile(r"^(\s*)([-*]|\d+\.)\s")


def _open_lists(md_text: str) -> str:
    """Put a blank line between a line of text and a list that starts right under it (CommonMark allows it,
    Python-Markdown would fold the list into the paragraph)."""
    out = []
    for line in md_text.splitlines():
        prev = out[-1] if out else ""
        if LIST_ITEM.match(line) and prev.strip() and not LIST_ITEM.match(prev) and not prev.startswith((" ", "\t")):
            out.append("")
        out.append(line)
    return "\n".join(out)


def to_html(md_text: str) -> str:
    """One standalone page; the title is the first heading."""
    first = next((line.lstrip("#").strip() for line in md_text.splitlines() if line.startswith("#")), "Finclaude 报告")
    body = markdown.markdown(_open_lists(md_text), extensions=["tables", "sane_lists", "md_in_html"])
    return (f'<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(first)}</title><style>{CSS}</style></head>\n<body><main>\n{body}\n</main></body></html>\n")


def save_report(md_text: str, folder: Path, name: str = "report") -> Path:
    """Write the report as <folder>/<name>.html only (no .md copy) and return its path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{name}.html"
    out.write_text(to_html(md_text), encoding="utf-8")
    return out
