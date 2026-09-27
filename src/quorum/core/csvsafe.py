"""CSV export that is safe to open in a spreadsheet (OWASP CSV injection).

String cells that begin with a formula trigger (= + - @ tab CR, or full-width variants)
are prefixed with a single quote. Numbers are written as numbers, so -5 stays -5."""

import csv
import io

_TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "＝", "＋", "－", "＠")


def safe_cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    if s.startswith(_TRIGGERS):
        return "'" + s
    return s


def to_csv(header: list[str], rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for row in rows:
        w.writerow([safe_cell(v) for v in row])
    return buf.getvalue()
