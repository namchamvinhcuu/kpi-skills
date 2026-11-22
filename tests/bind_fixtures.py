"""Synthetic baselines, specs, catalogs and an independent reference scanner for the binder tests (not a test module).

Everything is invented English text. A baseline is built the way the real flow builds it: a raw spreadsheet snapshot
(shape of Odoo's converter output) cropped by `intake_xlsx.crop`, so the keys, the sorted cells and the `START_REVISION`
are the real ones. A test that needs a shape the crop refuses edits the cropped dict afterwards.
"""
import copy
import functools
import json
import re
import unittest

from helpers import REPO  # noqa: F401  (puts scripts/ on sys.path)

import intake_fixtures as ifx  # noqa: E402

import bind_kpi  # noqa: E402
import intake_xlsx  # noqa: E402

AREA_ROWS, AREA_COLS = 10, 16                       # the default design area A1:P10
MONTH_LETTERS = "DEFGHIJKLMNO"
STYLES = {"1": {"bold": True, "fillColor": "#EEEEEE"}, "2": {"textColor": "#112233"}}


def default_cells(columns=12, typed_rows=(5, 6)):
    """The design area: a merged title, text labels, month headers, typed numbers in `typed_rows` (D..), a doubling
    row of formulas that reads row 6, and a total formula that reads row 6 only."""
    letters = MONTH_LETTERS[:columns]
    cells = {"A1": {"content": "Report title", "style": 1, "border": 1}, "B1": {"style": 1}, "C1": {"style": 1},
             "A5": {"content": "Row label one", "style": 1}, "A6": {"content": "Row label two", "style": 1},
             "A7": {"content": "Doubled", "style": 1}, "P8": {"style": 1}, "P9": {"style": 1}}
    for index, col in enumerate(letters):
        cells[col + "4"] = {"content": "M%d" % (index + 1), "style": 1}
        for row in typed_rows:
            cells["%s%d" % (col, row)] = {"content": "0", "style": 2, "format": 1, "border": 1}
        cells[col + "7"] = {"content": "=%s6*2" % col, "format": 1}
    cells["P7"] = {"content": "=SUM(D6:%s6)" % letters[-1], "format": 1}
    return cells


def make_baseline(cells=None, merges=None, rows=None, cols=None, row_number=AREA_ROWS, col_number=AREA_COLS, **top):
    """A cropped one-sheet baseline (A1 .. the last cell of the area), with cells outside the area dropped by the crop."""
    design = default_cells() if cells is None else dict(cells)
    padding = {"Q1": {"content": "outside the area", "style": 1}, "AZ5": {"content": "far right"},
               "AZ300": {"style": 1}}
    rows = {"0": {"size": 30}, "4": {"size": 25}, "9": {"size": 22}, "40": {"size": 18}} if rows is None else rows
    cols = {"0": {"size": 120}, "3": {"size": 70}, "40": {"size": 55}} if cols is None else cols
    sheet = ifx.make_sheet(name=ifx.SHEET, cells={**padding, **design}, merges=["A1:C1", "P8:P9"] if merges is None
                           else merges, rows=rows, cols=cols, col_number=max(60, col_number + 10),
                           row_number=max(300, row_number + 10))
    top.setdefault("styles", copy.deepcopy(STYLES))
    snapshot = ifx.make_snapshot(sheets=[sheet], **top)
    area = "A1:%s%d" % (intake_xlsx.col_letters(col_number), row_number)
    baseline, _ = intake_xlsx.crop(copy.deepcopy(snapshot), ifx.SHEET, area)
    return baseline


def edit(baseline, cells=None, **sheet_changes):
    """A deep copy of `baseline` with `cells` {ref: cell dict, or None to delete} merged in and sheet keys replaced."""
    out = copy.deepcopy(baseline)
    sheet = out["sheets"][0]
    for ref, cell in (cells or {}).items():
        if cell is None:
            sheet["cells"].pop(ref, None)
        else:
            sheet["cells"][ref] = cell
    sheet.update(sheet_changes)
    return out


# ---------------------------------------------------------------- catalogs

def source(model, selectors=None, audience="base.group_user", chain=None, title="Figures, per month", groups=None,
           columns=()):
    groups = {"months": ["m1", "m2", "m3"]} if groups is None else groups
    return {"caveats": [], "column_groups": groups, "model": model, "read_audience": audience,
            "selectors": selectors or {}, "title": title, "unit": "pcs",
            "value_columns": sorted({c for g in groups.values() for c in g} | set(columns)),
            "year_chain": chain or {"current": "year"}}


def small_catalog():
    wide = ["c%03d" % i for i in range(100)]
    return {"schema": 1, "sources": {
        "plain": source("t.plain", {"code": {"values": ["A", "B"]}}, chain={"current": "year", "previous": "next_year"},
                        title="Plain figures, per month", columns=("extra",)),
        "grid": source("t.grid", {"n": {"min": 1, "max": 3}, "kind": {"values": ["x", "y"]}}, title="Grid, per cell"),
        "secret": source("t.secret", {}, audience="base.group_system",
                         chain={"current": "year_date", "previous": "next_year"}, title="Secret figures"),
        "long": source("t.long", {"code": {"values": ["A"]}}, title="T" * 200),
        "wide": source("t.wide", {}, groups={"all": wide}, title="Wide figures"),
    }}


def small_cells(typed_rows=(5, 6)):
    return default_cells(3, typed_rows)


def small_baseline(typed_rows=(5, 6), **kwargs):
    return make_baseline(cells=small_cells(typed_rows), **kwargs)


def plain(cells, code="A", **kwargs):
    binding = {"cells": cells, "source": "plain", "where": {"code": code}, "columns": "months"}
    binding.update(kwargs)
    return binding


def small_bindings():
    return [plain("D5:F5", "A", zero="dash"), plain("D6:F6", "B")]


def make_spec(bindings=None, **over):
    spec = {"spec_version": 1, "name": "Synthetic KPI board", "bindings": small_bindings() if bindings is None
            else bindings}
    spec.update(over)
    return spec


def pivot_source(model, date_field="when", selectors=None, measures=None, filters=None, audience="base.group_user",
                 title="History forms: counted per month"):
    return {"caveats": [], "date_field": date_field, "kind": "pivot", "measures": list(measures or ["__count"]),
            "model": model, "optional_filters": {} if filters is None else filters, "read_audience": audience,
            "selectors": selectors or {}, "title": title, "unit": "pcs"}


def pivot_catalog():
    """`small_catalog` plus four pivot sources: `hist` (two selectors and two filters, declared in a NON-alphabetical
    order on purpose), `other` (no selector, no filter, another date field), `vault` (a restricted audience) and
    `longp` (a 200-character title)."""
    catalog = small_catalog()
    catalog["sources"].update({
        "hist": pivot_source("t.hist", "when", {"zone": {"values": ["N", "S"]}, "form": {"values": ["a", "b", "c"]}},
                             ["__count", "qty", "ng_qty"],
                             {"result": {"values": ["OK", "NG", "Pass"]}, "grade": {"values": ["X", "Y"]}}),
        "other": pivot_source("t.other", "day", {}, ["__count", "amount"], {}, title="Other events: counted per month"),
        "vault": pivot_source("t.vault", "stamp", {}, ["__count"], {}, audience="base.group_system",
                              title="Vault events: counted per month"),
        "longp": pivot_source("t.longp", "when", {}, ["__count"], {}, title="P" * 200),
    })
    return catalog


def hist(cells, form="a", zone="N", **kwargs):
    binding = {"cells": cells, "source": "hist", "where": {"form": form, "zone": zone}}
    binding.update(kwargs)
    return binding


def other(cells, **kwargs):
    binding = {"cells": cells, "source": "other"}
    binding.update(kwargs)
    return binding


def derived(cells, op, **fields):
    return {"cells": cells, "derived": dict(fields, op=op)}


def empty_baseline(**kwargs):
    """The design area with no content at all (nothing typed, no merge): any cell may be bound."""
    return make_baseline(cells={}, merges=[], **kwargs)


def pbind(bindings, baseline=None, catalog=None, **spec_over):
    """(document, report) over the pivot catalog and an empty baseline by default."""
    return bind(make_spec(bindings, **spec_over), empty_baseline() if baseline is None else baseline,
                pivot_catalog() if catalog is None else catalog)


class PivotCase(unittest.TestCase):
    def refuse(self, bindings, baseline=None, catalog=None, contains=(), **spec_over):
        """bind() must raise BindError; the message must contain every text of `contains`; returns the message."""
        with self.assertRaises(bind_kpi.BindError) as ctx:
            pbind(bindings, baseline, catalog, **spec_over)
        message = str(ctx.exception)
        for text in ([contains] if isinstance(contains, str) else contains):
            self.assertIn(text, message)
        return message

    def cell(self, document, ref):
        return document["sheets"][0]["cells"][ref]["content"]


DEFAULT = ...                                       # "not given" (None is a value a test may pass on purpose)


def bind(spec=DEFAULT, baseline=DEFAULT, catalog=DEFAULT, baseline_bytes=None):
    """(document, report) with the small defaults; the inputs are NOT copied (a test checks for mutation itself)."""
    return bind_kpi.bind(small_baseline() if baseline is DEFAULT else baseline,
                         make_spec() if spec is DEFAULT else spec,
                         small_catalog() if catalog is DEFAULT else catalog, baseline_bytes)


def real_catalog():
    return bind_kpi.load_catalog(project="dksk")


# ---------------------------------------------------------------- an independent reference scanner

def col_to_number(letters):
    return functools.reduce(lambda acc, ch: acc * 26 + ord(ch.upper()) - 64, letters, 0)


def number_to_col(number):
    out = ""
    while number:
        number, rest = divmod(number - 1, 26)
        out = chr(65 + rest) + out
    return out


_RANGE = re.compile(r"\$?([A-Za-z]+)\$?([0-9]+):\$?([A-Za-z]+)\$?([0-9]+)")
_CELL = re.compile(r"(?<![A-Za-z0-9_.])\$?([A-Za-z]+)\$?([0-9]+)(?![0-9A-Za-z(])")


def reference_scan(formula):
    """Every cell ref a formula reads, as an upper-case set of 'D5' strings. Written for the tests, independent of
    the binder: string literals are the odd parts of a split on the double quote (the generated formulas never hold
    an escaped quote), ranges are expanded, the function-call lookahead keeps LOG10( out."""
    code = " ".join(formula.split('"')[0::2])
    found = set()
    for match in _RANGE.finditer(code):
        c1, r1, c2, r2 = col_to_number(match.group(1)), int(match.group(2)), col_to_number(match.group(3)), \
            int(match.group(4))
        for col in range(min(c1, c2), max(c1, c2) + 1):
            for row in range(min(r1, r2), max(r1, r2) + 1):
                found.add("%s%d" % (number_to_col(col), row))
    rest = _RANGE.sub(" ", code)
    for match in _CELL.finditer(rest):
        found.add("%s%d" % (match.group(1).upper(), int(match.group(2))))
    return found


def natural_key(ref):
    match = re.fullmatch(r"([A-Z]+)([0-9]+)", ref)
    return int(match.group(2)), col_to_number(match.group(1))


def dumps(document):
    return intake_xlsx.dump_bytes(document)


def json_roundtrip(document):
    return json.loads(dumps(document).decode("utf-8"))
