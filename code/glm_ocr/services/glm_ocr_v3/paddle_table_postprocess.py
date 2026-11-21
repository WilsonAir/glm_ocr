"""Small dependency-free post-processor for PaddleOCR-VL table output.

PaddleOCR-VL served through vLLM can return one cell per line followed by
eight ``<|LOC_n|>`` tokens (the four corners of the cell box).  The full
PaddleOCR/PaddleX pipeline normally converts those tokens into structured
table HTML.  This module provides the compatible, local fallback needed by
the service when PaddleX is unavailable.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass


LOC_RE = re.compile(r"<\|LOC_(\d+)\|>")
PARTIAL_LOC_RE = re.compile(r"<\|LOC_\d*$")


@dataclass(frozen=True)
class TableCell:
    text: str
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2


def _parse_cell(line: str) -> TableCell | None:
    matches = list(LOC_RE.finditer(line))
    if len(matches) < 8:
        return None
    # Paddle emits the four corners as x1,y1,x2,y1,x2,y2,x1,y2.
    coords = [int(match.group(1)) for match in matches[:8]]
    text = line[: matches[0].start()].strip()
    return TableCell(
        text=text,
        x1=min(coords[0], coords[6]),
        y1=min(coords[1], coords[3]),
        x2=max(coords[2], coords[4]),
        y2=max(coords[5], coords[7]),
    )


def _cluster_rows(cells: list[TableCell]) -> list[list[TableCell]]:
    rows: list[list[TableCell]] = []
    # LOC coordinates are normalized to a 0..1000 grid.  A row's vertical
    # center is much more stable than its top edge for mixed font sizes.
    for cell in sorted(cells, key=lambda item: (item.cy, item.cx)):
        target = None
        tolerance = max(8.0, min(18.0, (cell.y2 - cell.y1) * 0.35))
        for row in rows:
            mean_y = sum(item.cy for item in row) / len(row)
            if abs(cell.cy - mean_y) <= tolerance:
                target = row
                break
        if target is None:
            rows.append([cell])
        else:
            target.append(cell)
    for row in rows:
        row.sort(key=lambda item: item.cx)
    return rows


def _render_table(cells: list[TableCell]) -> str:
    rows = _cluster_rows(cells)
    if not rows:
        return ""
    # HTML is intentional: it remains valid when a cell contains pipes,
    # math, or line breaks, and is closer to Paddle's native table output.
    rendered = ["<table>"]
    for row_index, row in enumerate(rows):
        rendered.append("<tr>")
        for cell in row:
            tag = "th" if row_index == 0 else "td"
            value = html.escape(cell.text, quote=False).replace("\n", "<br>")
            rendered.append(f"<{tag}>{value}</{tag}>")
        rendered.append("</tr>")
    rendered.append("</table>")
    return "\n".join(rendered)


def _replace_loc_block(lines: list[str]) -> tuple[list[str], bool]:
    cells = [_parse_cell(line) for line in lines]
    parsed = [cell for cell in cells if cell is not None]
    # Ordinary Paddle text blocks can also carry one LOC box.  Only promote a
    # block when its geometry looks table-like: multiple cells, rows, and
    # columns.  This prevents headings and paragraph lines from becoming
    # one-cell tables.
    if (
        len(parsed) < 4
        or len(parsed) != len(lines)
        or len({round(cell.cy, 1) for cell in parsed}) < 2
        or len({round(cell.cx, 1) for cell in parsed}) < 2
    ):
        return lines, False
    return [_render_table(parsed)], True


def normalize_paddle_tables(markdown: str) -> tuple[str, int]:
    """Replace Paddle LOC-token table blocks with structured HTML tables.

    A block is a consecutive group of non-empty lines where every line is a
    Paddle cell record.  The returned count is the number of converted tables.
    """
    lines = markdown.splitlines()
    output: list[str] = []
    converted = 0
    index = 0
    while index < len(lines):
        if not LOC_RE.search(lines[index]):
            output.append(lines[index])
            index += 1
            continue
        end = index
        block: list[str] = []
        while end < len(lines) and lines[end].strip() and LOC_RE.search(lines[end]):
            block.append(lines[end])
            end += 1
        # A truncated model response may leave one malformed LOC record at
        # the end of an otherwise valid table. Process valid runs separately
        # so one bad cell does not discard the complete table.
        run: list[str] = []
        for line in block + [""]:
            if line and _parse_cell(line) is not None:
                run.append(line)
                continue
            if run:
                replacement, ok = _replace_loc_block(run)
                if ok:
                    output.extend(replacement)
                    converted += 1
                else:
                    output.extend(PARTIAL_LOC_RE.sub("", LOC_RE.sub("", item)) for item in run)
                run = []
            if line:
                output.append(PARTIAL_LOC_RE.sub("", LOC_RE.sub("", line)))
        else:
            pass
        index = end
    return "\n".join(output) + ("\n" if markdown.endswith("\n") else ""), converted

