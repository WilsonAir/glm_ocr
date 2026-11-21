import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "services" / "glm_ocr_v3"))

from paddle_table_postprocess import normalize_paddle_tables


def loc(x1: int, y1: int, x2: int, y2: int) -> str:
    return "".join(
        f"<|LOC_{value}|>"
        for value in (x1, y1, x2, y1, x2, y2, x1, y2)
    )


def test_rebuilds_loc_cells_into_html_table():
    source = """Caption
A{a}
B{b}
C{c}
D{d}
""".format(
        a=loc(10, 10, 100, 40),
        b=loc(110, 10, 200, 40),
        c=loc(10, 50, 100, 80),
        d=loc(110, 50, 200, 80),
    )
    output, count = normalize_paddle_tables(source)
    assert count == 1
    assert "<|LOC_" not in output
    assert "<th>A</th>" in output
    assert "<td>D</td>" in output

