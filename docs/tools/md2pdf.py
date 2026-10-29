"""Render a short Markdown document to an A4 PDF and fail if it runs past --max-pages.

Handles the subset our one-page reports use: #/##/### headings, paragraphs, "- " and "1. "
lists, **bold**, `code` and [links](url). Pipe tables are rejected because they do not render. Uses only the standard library and
macOS AppKit (through html2pdf.js), so it needs no installs.

    uv run python docs/tools/md2pdf.py docs/checkpoint.md docs/checkpoint.pdf --max-pages 1
"""

import argparse
import html
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CSS = """
body { font-family: Helvetica; font-size: 9.5pt; }
h1 { font-size: 13pt; margin: 0 0 2pt 0; }
h2 { font-size: 10.5pt; margin: 6pt 0 1pt 0; }
h3 { font-size: 9.5pt; margin: 4pt 0 1pt 0; }
p { margin: 0 0 2pt 0; }
p.li { margin: 0 0 1pt 10pt; text-indent: -9pt; }
code { font-family: Menlo; font-size: 8.5pt; }
"""


def inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', out)


def to_html(markdown: str) -> str:
    blocks: list[str] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif m := re.match(r"(#{1,3}) (.*)", line):
            level = len(m.group(1))
            blocks.append(f"<h{level}>{inline(m.group(2))}</h{level}>")
            i += 1
        elif line.startswith("|"):
            # AppKit imports the table but its print path lays every cell out on its own line.
            raise SystemExit(f"md2pdf: line {i + 1} is a pipe table, which does not render; use a list")
        elif re.match(r"(- |\d+\. )", line):
            # AppKit draws its own marker on imported <li> as well as the bullet text, so a list
            # item is a paragraph with a literal marker and a hanging indent instead.
            while i < len(lines) and (m := re.match(r"(- |(\d+)\. )(.*)", lines[i])):
                marker = "&#8226;" if m.group(1) == "- " else f"{m.group(2)}."
                blocks.append(f'<p class="li">{marker}&nbsp;&nbsp;{inline(m.group(3))}</p>')
                i += 1
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"(#{1,3} |\||- |\d+\. )", lines[i]):
                para.append(lines[i].strip())
                i += 1
            blocks.append(f"<p>{inline(' '.join(para))}</p>")
    return f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{''.join(blocks)}</body></html>"


def page_count(pdf: Path) -> int:
    return len(re.findall(rb"/Type\s*/Page(?!s)", pdf.read_bytes()))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("markdown", type=Path)
    p.add_argument("pdf", type=Path)
    p.add_argument("--max-pages", type=int)
    args = p.parse_args()
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as f:
        f.write(to_html(args.markdown.read_text()))
    script = Path(__file__).with_name("html2pdf.js")
    subprocess.run(["osascript", "-l", "JavaScript", str(script), f.name, str(args.pdf.resolve())], check=True)
    pages = page_count(args.pdf)
    print(f"{args.pdf}: {pages} page(s)")
    if args.max_pages is not None and pages > args.max_pages:
        print(f"over the {args.max_pages}-page limit", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
