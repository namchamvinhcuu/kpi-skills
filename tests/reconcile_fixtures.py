"""Synthetic inputs of the reconcile tests (not a test module).

Everything is invented. The baselines are built the way the real flow builds them (`bind_fixtures.make_baseline`: a raw
snapshot cropped by `intake_xlsx.crop`), the specs name the SHIPPED catalog sources (reconcile has no `--catalog`), and the
fake Odoo world holds KPI-like models with the field names of those sources. No customer figure or text is used.
"""
import json

import bind_fixtures as bf
from fake_odoo import FAKE_LOGIN, FAKE_UID
from helpers import REPO  # noqa: F401  (puts scripts/ on sys.path)

import bind_kpi
import intake_xlsx

REPORT, PRODUCTION, QMS = "dksk.kpi.report_group", "dksk.kpi.production_group", "mes.qc_form.history"
LETTERS = "DEFGHIJKLMNO"                                  # the twelve month columns D..O
AREA_ROWS, AREA_COLS = 12, 16                              # the design area A1:P12
MONTH_COLUMNS = ["month_%d" % i for i in range(1, 13)]
LIST_SCHEMA = dict({"group_code": "char", "kind": "char", "year": "date", "next_year": "date"},
                   **{name: "float" for name in MONTH_COLUMNS})
QMS_SCHEMA = {"check_date": "datetime", "form_type": "char", "overall_result": "char", "qty": "float",
              "insp_qty_sampling": "float", "ng_qty": "float", "ok_lot_qty": "float", "ng_lot_qty": "float"}
LABEL = "Sentinel label ZQX"                               # a text that must never reach a stdout


def ref(row, month):
    """The cell of `row` for month 1..12 (D5 for row 5, month 1)."""
    return "%s%d" % (LETTERS[month - 1], row)


# ---------------------------------------------------------------- the fake server

def make_world(fake, tz="Asia/Seoul"):
    """The users and KPI models every reconcile test starts from; `tz` is the time zone of the authenticated user."""
    world = fake.enable_world()
    world.add_user(FAKE_UID, FAKE_LOGIN, "QA User", groups=("base.group_user",), tz=tz)
    for model in (REPORT, PRODUCTION):
        world.add_model(model, LIST_SCHEMA)
    world.add_model(QMS, QMS_SCHEMA)
    return world


def add_list_row(world, model, year, values, group_code, kind=None, **extra):
    """One year row of a list source: `values` are the month_1.. numbers (None = unset)."""
    row = {"group_code": group_code, "year": "%04d-01-01" % year, "next_year": "%04d-01-01" % (year + 1)}
    if kind:
        row["kind"] = kind
    row.update({"month_%d" % i: value for i, value in enumerate(values, 1) if value is not None})
    row.update(extra)
    return world.add_row(model, **row)


def add_qms(world, when, form="pqc", result="OK", **measures):
    """One QMS check; `when` is the UTC instant 'YYYY-MM-DD HH:MM:SS'."""
    return world.add_row(QMS, check_date=when, form_type=form, overall_result=result, **measures)


# ---------------------------------------------------------------- baseline and spec

def baseline_of(cells, **kwargs):
    """A cropped baseline: `cells` {ref: content text | "" (a styled blank) | None (not in the baseline at all)} plus a
    title and a label text that no binding touches."""
    design = {"A1": {"content": "Synthetic title", "style": 1}, "A3": {"content": LABEL, "style": 1}}
    for key, content in cells.items():
        if content is None:
            continue
        design[key] = {"style": 2, "format": 1} if content == "" else {"content": content, "style": 2, "format": 1}
    return bf.make_baseline(cells=design, merges=[], row_number=kwargs.pop("rows", AREA_ROWS),
                            col_number=kwargs.pop("cols", AREA_COLS), **kwargs)


def typed_row(row, values, first=1):
    """{ref: text} for the months first.. of `row`; a value None leaves the cell out of the baseline, "" is a blank."""
    return {ref(row, first + offset): (None if value is None else str(value) if value != "" else "")
            for offset, value in enumerate(values)}


def list_binding(row, group="DK_REF", kind="shipped", source="report_group", count=12, **extra):
    """A list binding over D<row>.. with the first `count` month columns (the where of the source)."""
    where = {"group_code": group}
    if source == "report_group":
        where["kind"] = kind
    binding = {"cells": "%s:%s" % (ref(row, 1), ref(row, count)) if count > 1 else ref(row, 1), "source": source,
               "where": where, "columns": MONTH_COLUMNS[:count]}
    binding.update(extra)
    return binding


def qms_binding(row, form="pqc", count=12, **extra):
    """A pivot binding of `qms_history` over D<row>:O<row> (the 12 months of the Year filter)."""
    binding = {"cells": "%s:%s" % (ref(row, 1), ref(row, count)), "source": "qms_history", "where": {"form_type": form}}
    binding.update(extra)
    return binding


def derived_binding(cells, op, **fields):
    return bf.derived(cells, op, **fields)


def spec_of(bindings, **over):
    return bf.make_spec(bindings, **over)


def produce(baseline, spec):
    """(baseline bytes, spec bytes, dashboard bytes) exactly as the real flow writes them (intake, spec file, bind)."""
    baseline_bytes = intake_xlsx.dump_bytes(baseline)
    spec_bytes = json.dumps(spec).encode("utf-8")
    document, _ = bind_kpi.bind(json.loads(baseline_bytes.decode("utf-8")), spec, bind_kpi.load_catalog(project="dksk"),
                                baseline_bytes)
    return baseline_bytes, spec_bytes, intake_xlsx.dump_bytes(document)
