"""Synthetic baselines for the scan tests (not a test module). Every sheet is built in code from invented numbers.

`Grid` collects cells and merges and turns them into a cropped one-sheet baseline through `bind_fixtures.make_baseline`
(the same route the real flow takes: a raw snapshot cropped by `intake_xlsx.crop`). `twin()` is the synthetic twin of
a design sheet with two blocks: a title row, a header row of 12 months, seven labelled rows under a vertically merged
year cell, a totals row of formulas; typed numbers are kept as text digits (the xlsx import keeps `<v>` as text).
"""
import unicodedata

from helpers import REPO  # noqa: F401  (puts scripts/ on sys.path)

import bind_fixtures as bf  # noqa: E402

import bind_kpi  # noqa: E402
import intake_xlsx  # noqa: E402

LABELS = ["DK 냉장고", "DK 에어컨", "DK 기타", "SK 세탁기", "SK 기타", "SZ DK 냉장고", "SZ DK 세탁기"]
CODES = ["DK_REF", "DK_AC", "DK_ETC", "SK_WM", "SK_ETC", "SZDK_REF", "SZDK_WM"]
SHIPPED_TITLE, PRODUCED_TITLE = "출하 수량", "생산 수량"
ENGLISH_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                "November", "December"]
ENGLISH_SHORT = [name[:3] for name in ENGLISH_LONG]
FIRST_MONTH_COL = 3                                  # column C


def month_texts(form, year=26):
    """The 12 header texts of one of the accepted forms."""
    return {
        "ko_year": ["%d년 %d월" % (year, m) for m in range(1, 13)],
        "ko": ["%d월" % m for m in range(1, 13)],
        "vi": ["Tháng %d" % m for m in range(1, 13)],
        "en_month": ["Month %d" % m for m in range(1, 13)],
        "en_short": list(ENGLISH_SHORT),
        "en_long_year": ["%s 20%d" % (name, year) for name in ENGLISH_LONG],
    }[form]


MONTH_FORMS = ("ko_year", "ko", "vi", "en_month", "en_short", "en_long_year")


def number(row, col):
    """An invented typed number, as the text digits the xlsx import writes (a few typed zeros)."""
    return "0" if (row + col) % 11 == 0 else str(100 * row + 7 * col)


class Grid:
    def __init__(self, rows=31, cols=15):
        self.rows, self.cols = rows, cols
        self.cells, self.merges = {}, []

    def put(self, row, col, content):
        self.cells[bind_kpi.ref_of(row, col)] = {"content": content}
        return self

    def merge(self, text):
        self.merges.append(text)
        return self

    def header(self, row, texts, first_col=FIRST_MONTH_COL):
        for offset, text in enumerate(texts):
            self.put(row, first_col + offset, text)
        return self

    def numbers(self, row, months=range(1, 13), first_col=FIRST_MONTH_COL, blank=()):
        for month in months:
            if month not in blank:
                self.put(row, first_col + month - 1, number(row, month))
        return self

    def totals(self, row, first_row, last_row, first_col=FIRST_MONTH_COL):
        for month in range(12):
            letters = intake_xlsx.col_letters(first_col + month)
            self.put(row, first_col + month, "=SUM(%s%d:%s%d)" % (letters, first_row, letters, last_row))
        return self

    def baseline(self):
        return bf.make_baseline(cells=self.cells, merges=list(self.merges), row_number=self.rows, col_number=self.cols)


def block(grid, title_row, title, year_text, forms="ko_year", labels=LABELS, header_year=26, first_col=FIRST_MONTH_COL,
          months_filled=range(1, 11), label_col=2, year_col=1):
    """A title row, a header row (`title_row + 1`), `labels` rows with a merged year cell, a totals row. Returns the
    row numbers of the label rows."""
    header_row = title_row + 1
    if title is not None:
        grid.put(title_row, 1, title)
    grid.put(header_row, 1, "년도").put(header_row, 2, "모델별")
    grid.header(header_row, month_texts(forms, header_year), first_col)
    rows = list(range(header_row + 1, header_row + 1 + len(labels)))
    for row, label in zip(rows, labels):
        grid.put(row, label_col, label)
        grid.numbers(row, months_filled, first_col)
    if year_text is not None:
        grid.put(rows[0], year_col, year_text)
        if len(rows) > 1:
            grid.merge("%s:%s" % (bind_kpi.ref_of(rows[0], year_col), bind_kpi.ref_of(rows[-1], year_col)))
    total_row = rows[-1] + 1
    grid.put(total_row, 1, "월별 합계").merge("A%d:B%d" % (total_row, total_row))
    grid.totals(total_row, rows[0], rows[-1], first_col)
    return rows


def twin(shipped_year="26년", produced_year="25년", forms="ko_year", shipped_title=SHIPPED_TITLE,
         produced_title=PRODUCED_TITLE):
    """The synthetic twin of the design sheet: a `출하 수량` block (rows 4-10, header row 3, year cell A4:A10) and a
    `생산 수량` block (rows 23-29, header row 22, year cell A23:A29), 14 label rows, months 1-10 typed (M:N blank)."""
    grid = Grid(31, 15)
    block(grid, 2, shipped_title, shipped_year, forms)
    block(grid, 21, produced_title, produced_year, forms)
    for row in range(3, 31):                           # the 13th column: a total formula, never a typed number
        if row not in (3, 22) and (row in range(4, 11) or row in range(23, 30)):
            grid.put(row, 15, "=SUM(C%d:N%d)" % (row, row))
    return grid.baseline()


def expected_twin_bindings():
    """The 14 bindings the twin must give, written out by hand (not by the code under test)."""
    out = []
    for first_row, kind in ((4, "shipped"), (23, "produced")):
        for offset, code in enumerate(CODES):
            row = first_row + offset
            out.append({"cells": "C%d:N%d" % (row, row), "source": "report_group",
                        "where": {"kind": kind, "group_code": code}, "columns": "months"})
    return out


def expected_twin_labels():
    return {"B%d" % (first_row + offset): label for first_row in (4, 23) for offset, label in enumerate(LABELS)}


def nfd(text):
    return unicodedata.normalize("NFD", text)
