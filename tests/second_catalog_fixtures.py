"""A SYNTHETIC second project, `acme`, for the two-catalog tests (not a test module).

Plan kpi-multi-project step 2b (AC3). Everything is invented: neutral model names (`acme.*`), generic columns and
selector values, label aliases like `Alpha Line`. No real project data is used. The catalog has the same schema 1 as the
shipped one, three sources that the shipped catalog does NOT have (`line_month`, `line_group`, `qc_log`: two list sources
and one pivot source) and one source with a name the shipped catalog HAS (`month_metric`, another model and title), so
that a test can tell "this source is missing here" from "this name exists in both but means two different things".

The fake Odoo world needs no new code: `DashboardWorld.add_model` already takes any model name and a `{field: kind}` schema
(`search_read` and the month `read_group` with the time-zone context work on any of them); `make_world` below only adds the
generic models next to the ones of `reconcile_fixtures`.
"""
import copy
import json

import reconcile_fixtures as rf
import scan_fixtures as sf
from helpers import REPO  # noqa: F401  (puts scripts/ on sys.path)

import bind_kpi  # noqa: E402
import intake_xlsx  # noqa: E402

PROJECT = "acme"
LINE_MONTH, LINE_GROUP, QC_LOG, SHARED = "line_month", "line_group", "qc_log", "month_metric"
MODEL_LINE_MONTH, MODEL_LINE_GROUP, MODEL_QC, MODEL_SHARED = ("acme.line.month", "acme.line.group", "acme.qc.history",
                                                              "acme.month.metric")
ACME_MODELS = (MODEL_LINE_MONTH, MODEL_LINE_GROUP, MODEL_QC, MODEL_SHARED)
MONTHS = rf.MONTH_COLUMNS
LABELS = ["Alpha Line", "Beta Line", "Gamma Line"]
CODES = ["L_A", "L_B", "L_C"]
TITLE = "Actual qty"
EXTRA_LABEL = "Delta Line"                    # a label that no shipped alias knows (L_D has none): the overlay adds it


def _list_source(model, selectors, title, previous=True, aliases=None):
    source = {"caveats": ["Invented source of the synthetic project: no customer data."],
              "column_groups": {"months": list(MONTHS)}, "model": model, "read_audience": "base.group_user",
              "selectors": selectors, "title": title, "unit": "pcs", "value_columns": sorted(MONTHS),
              "year_chain": {"current": "year", "previous": "next_year"} if previous else {"current": "year"}}
    if aliases:
        source["aliases"] = aliases
    return source


def acme_catalog():
    """A fresh dict of the synthetic catalog (the callers may edit their copy)."""
    return copy.deepcopy({"note": "Synthetic second project for the tests: invented names, no customer data.",
                          "schema": 1,
                          "sources": {
        LINE_MONTH: _list_source(
            MODEL_LINE_MONTH,
            {"line_code": {"values": ["L_A", "L_B", "L_C", "L_D"]}, "kind": {"values": ["planned", "actual"]}},
            "Planned and actual quantity by line, per month",
            aliases={"line_code": {"L_A": ["Alpha Line", "Line A", "L_A"], "L_B": ["Beta Line", "Line B", "L_B"],
                                   "L_C": ["Gamma Line", "Line C", "L_C"]},
                     "kind": {"planned": ["Planned qty", "Plan"], "actual": ["Actual qty", "Actual"]}}),
        LINE_GROUP: _list_source(MODEL_LINE_GROUP, {"group_no": {"min": 1, "max": 5}},
                                 "Quantity by line group number, per month", previous=False),
        SHARED: _list_source(MODEL_SHARED, {"metric": {"values": ["working_days", "ng_qty"]}},
                             "Plant figures per month: days and rejects", previous=False),
        QC_LOG: {"caveats": ["Invented pivot source: the month is the check time (Datetime)."],
                 "date_field": "checked_at", "kind": "pivot", "measures": ["__count", "qty", "reject_qty"],
                 "model": MODEL_QC, "optional_filters": {"verdict": {"values": ["good", "bad"]}},
                 "read_audience": "base.group_user", "selectors": {"station": {"values": ["st1", "st2", "st3"]}},
                 "title": "QC history: checks counted or summed per month", "unit": "checks (__count) or pcs"},
    }})


def acme_bytes():
    return json.dumps(acme_catalog(), indent=1, sort_keys=True, ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------- the fake server

YEAR_FIELDS = {"year": "date", "next_year": "date"}
MONTH_FIELDS = {name: "float" for name in MONTHS}
SHARED_SCHEMA = dict({"metric": "char"}, **YEAR_FIELDS, **MONTH_FIELDS)     # the shape of `month_metric` in both projects


def add_models(world):
    """The generic models next to whatever `world` already holds."""
    world.add_model(MODEL_LINE_MONTH, dict({"line_code": "char", "kind": "char"}, **YEAR_FIELDS, **MONTH_FIELDS))
    world.add_model(MODEL_LINE_GROUP, dict({"group_no": "integer"}, **YEAR_FIELDS, **MONTH_FIELDS))
    world.add_model(MODEL_SHARED, dict(SHARED_SCHEMA))
    world.add_model(MODEL_QC, {"checked_at": "datetime", "station": "char", "verdict": "char", "qty": "float",
                               "reject_qty": "float"})


def make_world(fake, tz="Asia/Seoul"):
    """The world of `reconcile_fixtures` (users, the shipped project's models) plus the generic models."""
    world = rf.make_world(fake, tz)
    add_models(world)
    return world


def add_row(world, model, year, values, **fields):
    """One year row of a list model: `values` are month_1.. (None = unset); `fields` are the selector columns."""
    row = {"year": "%04d-01-01" % year, "next_year": "%04d-01-01" % (year + 1)}
    row.update({"month_%d" % i: value for i, value in enumerate(values, 1) if value is not None})
    row.update(fields)
    return world.add_row(model, **row)


def add_check(world, when, station="st1", verdict="good", **measures):
    """One QC check; `when` is the UTC instant 'YYYY-MM-DD HH:MM:SS'."""
    return world.add_row(MODEL_QC, checked_at=when, station=station, verdict=verdict, **measures)


# ---------------------------------------------------------------- bindings and files

def line_binding(row, line="L_A", kind="actual", count=12, **extra):
    binding = {"cells": "%s:%s" % (rf.ref(row, 1), rf.ref(row, count)) if count > 1 else rf.ref(row, 1),
               "source": LINE_MONTH, "where": {"line_code": line, "kind": kind}, "columns": MONTHS[:count]}
    binding.update(extra)
    return binding


def group_binding(row, group_no=2, count=12, **extra):
    binding = {"cells": "%s:%s" % (rf.ref(row, 1), rf.ref(row, count)) if count > 1 else rf.ref(row, 1),
               "source": LINE_GROUP, "where": {"group_no": group_no}, "columns": MONTHS[:count]}
    binding.update(extra)
    return binding


def shared_binding(row, metric="working_days", count=12, **extra):
    binding = {"cells": "%s:%s" % (rf.ref(row, 1), rf.ref(row, count)) if count > 1 else rf.ref(row, 1),
               "source": SHARED, "where": {"metric": metric}, "columns": MONTHS[:count]}
    binding.update(extra)
    return binding


def qc_binding(row, station="st1", count=12, **extra):
    binding = {"cells": "%s:%s" % (rf.ref(row, 1), rf.ref(row, count)), "source": QC_LOG, "where": {"station": station}}
    binding.update(extra)
    return binding


def produce(baseline, spec, project):
    """(baseline bytes, spec bytes, dashboard bytes) of the real flow with the catalog of `project` (the caller keeps
    `kpi_catalogs.CATALOG_DIR` patched at the folder that holds it)."""
    baseline_bytes = intake_xlsx.dump_bytes(baseline)
    spec_bytes = json.dumps(spec).encode("utf-8")
    document, _ = bind_kpi.bind(json.loads(baseline_bytes.decode("utf-8")), spec, bind_kpi.load_catalog(project=project),
                                baseline_bytes)
    return baseline_bytes, spec_bytes, intake_xlsx.dump_bytes(document)


# ---------------------------------------------------------------- scan

def acme_sheet(extra_labels=()):
    """One block: title `Actual qty`, header `Month 1..12` in row 3, one row per label from row 4, typed numbers."""
    grid = sf.Grid(12, 15)
    sf.block(grid, 2, TITLE, None, "en_month", labels=LABELS + list(extra_labels))
    return grid.baseline()


def expected_acme_bindings(codes=CODES):
    """The bindings the sheet must give, written out by hand (not by the code under test)."""
    return [{"cells": "C%d:N%d" % (4 + offset, 4 + offset), "source": LINE_MONTH,
             "where": {"kind": "actual", "line_code": code}, "columns": "months"} for offset, code in enumerate(codes)]
