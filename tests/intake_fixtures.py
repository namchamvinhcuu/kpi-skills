"""Synthetic spreadsheet JSON for the intake tests (not a test module). Built in Python, no data file, no real text.

The shape follows what Odoo's converter produces (see the docstring of scripts/intake_xlsx.py); every text, name and
number here is invented.
"""
import base64
import copy
import json

from helpers import REPO  # noqa: F401  (puts scripts/ on sys.path)

import intake_xlsx  # noqa: E402

AREA = "A1:E6"
SHEET = "Sheet One"
OTHER_SHEET = "Sheet Two"
NON_ASCII = "Größe € — 测试"
FILE_REVISION = "START_REVISION"


def design_cells():
    """Cells of the design area A1:E6, in a scrambled insertion order (the crop must sort them row-major)."""
    return {
        "B2": {"style": 1, "content": "Total", "border": 1, "format": 1},
        "A1": {"style": 1, "content": "Title"},
        "D3": {"content": "=SUM(B2:C2)+$A$1"},
        "C1": {"content": NON_ASCII},
        "E6": {"style": 1},
        "A2": {"content": 12.5, "format": 1},
    }


def padding_cells():
    """Cells outside A1:E6: style-only padding plus a few that hold content (they are dropped and counted)."""
    cells = {"F1": {"style": 1}, "A7": {"style": 1}, "Z50": {"style": 1}, "AD1000": {"style": 1}}
    cells["A10"] = {"style": 1, "content": "outside one"}
    cells["G2"] = {"content": "=B2*2"}
    cells["B30"] = {"content": "outside two", "border": 1}
    return cells


def make_sheet(name=SHEET, cells=None, merges=None, rows=None, cols=None, col_number=30, row_number=1000, **extra):
    sheet = {"id": "sheet-" + name.replace(" ", "-"), "name": name, "colNumber": col_number, "rowNumber": row_number,
             "rows": {"0": {"size": 24}, "5": {"size": 20}, "6": {"size": 22}, "9": {"size": 30}, "20": {"size": 18}}
             if rows is None
             else rows,
             "cols": {"0": {"size": 50}, "4": {"size": 80}, "5": {"size": 60}, "12": {"size": 40}} if cols is None
             else cols,
             "merges": ["A1:B1", "D5:E6", "H1:I2"] if merges is None else merges,
             "cells": {**design_cells(), **padding_cells()} if cells is None else cells,
             "conditionalFormats": [], "figures": [], "filterTables": [], "areGridLinesVisible": True,
             "isVisible": True, "headerGroups": {"ROW": [], "COL": []}, "dataValidationRules": [], "zoom": 100}
    sheet.update(extra)
    return sheet


def make_snapshot(sheets=None, revision_id="3f2b8a4e-1111-4222-8333-444455556666", **extra):
    data = {"version": 14.5,
            "sheets": [make_sheet(), make_sheet(OTHER_SHEET, cells={"A1": {"content": "other"}})]
            if sheets is None else sheets,
            "entities": {},
            "styles": {"1": {"bold": True, "fillColor": "#EEEEEE"}},
            "formats": {"1": "0"},
            "borders": {"1": {"top": {"style": "medium", "color": "#000000"}}},
            "revisionId": revision_id, "uniqueFigureIds": True,
            "settings": {"locale": {"name": "English (US)", "code": "en_US", "thousandsSeparator": ",",
                                    "decimalSeparator": ".", "dateFormat": "m/d/yyyy"}},
            "odooVersion": 6, "globalFilters": [], "pivots": {}, "pivotNextId": 1, "lists": {}, "listNextId": 1,
            "chartOdooMenusReferences": {}}
    data.update(extra)
    return data


def with_cell(ref, cell, sheet_name=SHEET, **snapshot_extra):
    """A snapshot whose first sheet carries one more (or replaced) cell, e.g. a formula to be checked."""
    sheet = make_sheet(name=sheet_name)
    sheet["cells"][ref] = cell
    return make_snapshot(sheets=[sheet], **snapshot_extra)


def with_formula(formula, ref="D4"):
    return with_cell(ref, {"content": formula})


def with_sheet_extra(**extra):
    return make_snapshot(sheets=[make_sheet(**extra)])


def as_bytes(data, indent=None, bom=False):
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


def as_base64(data):
    return base64.b64encode(as_bytes(data)).decode("ascii")


def crop_it(data, sheet=SHEET, area=AREA):
    return intake_xlsx.crop(copy.deepcopy(data), sheet, area)
