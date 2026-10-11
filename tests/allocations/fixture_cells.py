"""Cells cut verbatim from the 2025 NTIA grid (row 96 = 5.25-5.275 MHz, row 1 = 0-8.3 kHz).

Grid columns: 1-3 ITU Regions, 4 US Federal, 5 US Non-Federal, 6 FCC rule parts.
"""

_WRAP = '<div style="padding: 4px; display: flex; flex-direction: column; overflow: hidden;">'
_LABEL = (
    '<div style="font-size: 12px; font-weight: bold; margin-bottom: 4px; word-wrap: break-word; '
    'overflow-wrap: break-word;">'
)
_LINE = (
    '<div style="font-size: 10px; margin: 2px 0; word-wrap: break-word; '
    'overflow-wrap: break-word;">'
)
_FOOT = (
    '<div style="font-size: 10px; margin-top: auto; padding-top: 4px; word-wrap: break-word; '
    'overflow-wrap: break-word;">'
)


def cell(
    aid: str, col: str, row: str, label: str | None, lines: list[str], foot: str | None
) -> str:
    body = _WRAP
    if label is not None:
        body += f"{_LABEL}{label}</div>"
    body += "".join(f"{_LINE}{t}</div>" for t in lines)
    if foot is not None:
        body += f"{_FOOT}{foot}</div>"
    body += "</div>"
    return (
        f'<div\n  class="allocation-cell"\n  data-allocation-id="{aid}"\n  tabindex="1"\n'
        '  style="\n'
        f"    grid-column: {col};\n    grid-row: {row};\n    border-right: 1px solid #000;\n"
        '    background: #fff;\n    overflow: hidden;\n    cursor: pointer;\n  "\n>\n'
        f"  {body}\n</div>\n"
    )


ROW_96 = [
    cell(
        "alloc-375",
        "1 / 2",
        "96 / 97",
        "5.25-5.275 MHz",
        ["FIXED", "MOBILE Except Aeronautical Mobile", "Radiolocation (5.132A)"],
        "5.133A",
    ),
    cell(
        "alloc-376",
        "2 / 3",
        "96 / 97",
        "5.25-5.275 MHz",
        ["FIXED", "MOBILE Except Aeronautical Mobile", "RADIOLOCATION (5.132A)"],
        None,
    ),
    cell(
        "alloc-377",
        "3 / 4",
        "96 / 97",
        "5.25-5.275 MHz",
        ["FIXED", "MOBILE Except Aeronautical Mobile", "Radiolocation (5.132A)"],
        None,
    ),
    cell(
        "alloc-378",
        "4 / 6",
        "96 / 97",
        "5.25-5.275 MHz",
        ["FIXED", "MOBILE Except Aeronautical Mobile", "RADIOLOCATION (5.132A)"],
        "US340",
    ),
    cell(
        "alloc-380", "6 / 7", "96 / 97", None, ["Maritime (80)", "Private Land Mobile (90)"], None
    ),
]

ROW_1 = [
    cell("alloc-0", "1 / 4", "1 / 2", "0-8.3 kHz", ["NOT ALLOCATED"], "5.53, 5.54"),
    cell("alloc-3", "4 / 6", "1 / 2", "0-8.3 kHz", ["NOT ALLOCATED"], "5.53, 5.54"),
]

# Federal column empty by rule; Non-Federal with a two-footnote service line and
# entities, as in the 420-450 MHz rows (grid rows 339-344 in the real page).
ROW_339 = [
    cell(
        "alloc-1425",
        "4 / 5",
        "339 / 344",
        "420-450 MHz",
        ["RADIOLOCATION (G129, G2)"],
        "5.286, G8, US230, US269, US270, US397, US64, US87",
    ),
    cell(
        "alloc-1426",
        "5 / 6",
        "339 / 344",
        "420-450 MHz",
        ["Amateur (US270)"],
        "5.282, 5.286, US230, US269, US397, US64, US87",
    ),
    cell("alloc-1427", "6 / 7", "339 / 340", None, ["Private Land Mobile (90)"], None),
    cell("alloc-1428", "6 / 7", "340 / 344", None, ["MedRadio (95I)", "Amateur Radio  (97)"], None),
]


def page(*rows: list[str]) -> str:
    cells = "".join(c for row in rows for c in row)
    return (
        '<!DOCTYPE html><html><body><div class="allocation-table">'
        '<div class="table-allocation-body" style="display: grid;">'
        f"{cells}</div></div></body></html>"
    )
