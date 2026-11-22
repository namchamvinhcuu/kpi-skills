#!/usr/bin/env python3
"""Reconcile: the numbers a KPI spec gives against the numbers the Excel had typed in the same cells.

    python scripts/reconcile_kpi.py --baseline baseline.json --spec kpi_spec.json --dashboard dashboard.json \\
        --year 2026 [--tz Area/City] [--out reconcile_report.json] [--force] [--confirm-instance HOST] [--project NAME]

Why: the QA/QC's Excel usually carries the numbers somebody typed (the report of last month). After the binder replaced
those cells by live reads, this script shows how many of them still equal the live number, so nobody has to compare
two or three cells by eye.

How: it runs `bind_kpi.bind` ITSELF with the shipped catalog of `--project` (no `--catalog`), so every list and pivot it reads is
produced in this process from the closed catalog: there is no definition read from a file to trust. `--dashboard` is the
file the user will pass to `publish_dashboard.py`; it is read for its BYTES only (never parsed) and must equal the bind
output byte for byte, otherwise the numbers shown would not be the numbers of that file (exit 2, before any RPC). The
live rows are read over RPC as the user (`search_read` of each list with the Year range on its chain field; one
`read_group` per pivot, months keyed in the user's time zone like the client does) and every cell the binder replaced
is evaluated with `validate_formulas.SheetEvaluator`, the same evaluator that is proven against the real client.

Cells (a REPLACED cell is a cell of the design area whose dashboard content differs from the baseline):
  typed number N    equal / equal after rounding to N's decimals / differ / error (the evaluator refused)
  blank or formula  untyped (nothing typed to compare with)
  text              had text (the binder replaces text only with `overwrite_text`)
Cells that keep an Excel formula (totals) are only counted: `left to Excel formulas`.
The output holds cell references and numbers only (never a label, never a server text). Exit 0 whatever the counts;
2 = input / spec / file problem; 1 = RPC or time-zone problem. Standard library only.
"""
import argparse
import datetime
import hashlib
import math
import os
import re
import sys
from decimal import Decimal, InvalidOperation

import bind_kpi
import intake_xlsx
import odoo_rpc
import validate_formulas

MAX_SHOWN = 50
RELATIVE_TOLERANCE = 1e-9
PLAIN_TEXT_RESULTS = ("-", "N/A", "")          # the only texts a generated formula can show: printed as they are
TZ_NAME = re.compile(r"[A-Za-z0-9_+\-/]{1,64}", re.ASCII)
LANG = "en_US"


class ReconcileError(Exception):
    """A problem with the input (message is safe to print)."""


class TzError(ReconcileError):
    """The time zone cannot be decided: reported like an RPC problem (exit 1)."""


# ---------------------------------------------------------------- the bound dashboard

def _read_bytes(path, what):
    try:
        with open(path, "rb") as fh:
            data = fh.read(bind_kpi.MAX_FILE + 1)
    except OSError as exc:
        raise ReconcileError("cannot read %s: %s" % (what, exc.strerror or "error")) from None
    if len(data) > bind_kpi.MAX_FILE:
        raise ReconcileError("%s is larger than %d MB" % (what, bind_kpi.MAX_FILE // (1024 * 1024)))
    return data


def bound_document(baseline_path, spec_path, dashboard_path, project=None):
    """(baseline object, bound dashboard document) for the files; the dashboard file must equal the bind output."""
    baseline_bytes, baseline = bind_kpi._read_json(baseline_path, "the baseline")
    _, spec = bind_kpi._read_json(spec_path, "the spec")
    document, _ = bind_kpi.bind(baseline, spec, bind_kpi.load_catalog(project=project), baseline_bytes)
    produced = hashlib.sha256(intake_xlsx.dump_bytes(document)).hexdigest()
    if hashlib.sha256(_read_bytes(dashboard_path, "the dashboard file")).hexdigest() != produced:
        raise ReconcileError("the dashboard file is not what the spec produces: run bind_kpi.py again and pass its "
                             "--out file")
    return baseline, document, produced


# ---------------------------------------------------------------- live data

def _year_range(field, year):
    """The client's Year filter on a `date` chain: date-only bounds (the probe pivot declares type date)."""
    return [[field, ">=", "%04d-01-01" % year], [field, "<=", "%04d-12-31" % year]]


def _number(value, what):
    """A number out of an RPC value: NULL / False is 0 (an all-NULL aggregate), anything else non-numeric is refused."""
    if value is None or value is False:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise odoo_rpc.RpcError("the server answered a non-number for %s" % what)
    return float(value)


def fetch_lists(client, document, year):
    """{list id: [row]}: position 1 of each list, as the client reads it."""
    lists = {}
    for list_id, definition in sorted(document.get("lists", {}).items(), key=lambda kv: int(kv[0])):
        chain = next(iter(definition["fieldMatching"].values()))["chain"]
        domain = [list(leaf) for leaf in definition["domain"]] + _year_range(chain, year)
        rows = client.execute(definition["model"], "search_read", [domain],
                              {"fields": list(definition["columns"]), "limit": 1})
        if not isinstance(rows, list) or len(rows) > 1 or not all(isinstance(row, dict) for row in rows):
            raise odoo_rpc.RpcError("the server answered an unexpected shape for list %s" % list_id)   # limit was 1
        lists[int(list_id)] = [{name: _list_value(row.get(name), name) for name in definition["columns"]}
                               for row in rows]
    return lists


def _list_value(value, name):
    """Text (a selector column) stays text; a number is a float; NULL / False is 0."""
    return value if isinstance(value, str) else _number(value, name)


def _pivot_target(document):
    return [(pivot_id, definition) for pivot_id, definition in sorted(document.get("pivots", {}).items(),
                                                                      key=lambda kv: int(kv[0]))]


def resolve_tz(client, tz_arg):
    """The time zone the client would group months in: --tz, else the user's own `tz`; never UTC silently."""
    selection = client.execute("res.users", "fields_get", [["tz"]], {"attributes": ["selection"]})
    try:
        names = {item[0] for item in selection["tz"]["selection"]}
    except (KeyError, TypeError, IndexError):
        raise TzError("cannot read the time zone names of the server") from None
    tz = tz_arg
    if not tz:
        rows = client.execute("res.users", "read", [[client.uid], ["tz"]])
        tz = rows[0].get("tz") if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    if not tz:
        raise TzError("your Odoo user has no time zone: set it in your Odoo preferences or pass --tz Area/City "
                      "(the dashboard groups months by it)")
    if not isinstance(tz, str) or not TZ_NAME.fullmatch(tz) or tz not in names:
        raise TzError("the time zone %r is not one the server knows" % str(tz)[:40])
    return tz


def _month_key(group, groupby):
    """'MM/yyyy' of a read_group group. With a tz the server gives `__range.from` as the UTC instant of the LOCAL month
    start (April in Seoul starts on 03-31 15:00 UTC), so the month is the UTC date of `from` + 15 days: any offset
    is within +-14 h, hence that date lies in the local month. No tz database is needed."""
    try:
        start = group["__range"][groupby]["from"]
        date = datetime.datetime.strptime(str(start)[:10], "%Y-%m-%d").date() + datetime.timedelta(days=15)
    except (KeyError, TypeError, ValueError):
        raise odoo_rpc.RpcError("the server answered an unexpected month group") from None
    return "%02d/%04d" % (date.month, date.year)


def fetch_pivots(client, document, year, tz):
    """{pivot id: {"measures": [...], "months": {"MM/yyyy": {measure: value}}}}. The year probe is matched on the Year
    filter; a data pivot is not (the previous year is read from it)."""
    pivots = {}
    for pivot_id, definition in _pivot_target(document):
        groupby = definition["colGroupBys"][0]
        domain = [list(leaf) for leaf in definition["domain"]]
        matching = definition.get("fieldMatching") or {}
        if matching:
            domain += _year_range(next(iter(matching.values()))["chain"], year)
        measures = [m["field"] for m in definition["measures"]]
        fields = [m for m in measures if m != "__count"] or ["__count"]
        groups = client.execute(definition["model"], "read_group", [domain, fields, [groupby]],
                                {"lazy": False, "context": {"tz": tz, "lang": LANG}})
        if not isinstance(groups, list):
            raise odoo_rpc.RpcError("the server answered an unexpected shape for pivot %s" % pivot_id)
        months = {}
        for group in groups:
            if not isinstance(group, dict):
                raise odoo_rpc.RpcError("the server answered an unexpected shape for pivot %s" % pivot_id)
            if isinstance(group.get("__range"), dict) and group["__range"].get(groupby) is False:
                continue          # a record with NO date: Odoo gives `__range[group] = False`; the client's pivot has a
                #                   column for it that no `MM/yyyy` read ever touches, so it counts for nothing here
            column = months.setdefault(_month_key(group, groupby), {m: 0.0 for m in measures})
            for measure in measures:
                column[measure] += _number(group.get(measure), measure)
        pivots[int(pivot_id)] = {"measures": measures, "months": months}
    return pivots


# ---------------------------------------------------------------- comparison

def _decimals(text):
    try:
        exponent = Decimal(text).as_tuple().exponent
    except InvalidOperation:
        return 0
    return max(0, -exponent) if isinstance(exponent, int) else 0


def _typed(content):
    return bind_kpi._typed_number(content)


def classify(typed, live):
    """('equal' | 'rounded' | 'differ', ...) for a typed number (JSON number or numeric text) and a live value."""
    try:
        typed_value = float(typed)
    except OverflowError:                              # a JSON integer above 1e308 (never typed by Excel)
        return "differ"
    if isinstance(live, str):
        return "equal" if live == "-" and typed_value == 0 else "differ"
    if not math.isfinite(typed_value) or not math.isfinite(live):
        return "differ"                        # a typed 1E400 is infinity: it must not equal every live value
    if abs(live - typed_value) <= RELATIVE_TOLERANCE * max(1.0, abs(typed_value)):
        return "equal"
    if round(live, _decimals(str(typed))) == typed_value:
        return "rounded"
    return "differ"


def _show(value):
    if isinstance(value, str):
        return value if value in PLAIN_TEXT_RESULTS else "<text>"
    return ("%.10g" % value) if isinstance(value, float) else str(value)


def _show_typed(content):
    """The number the Excel had typed, as typed (the baseline keeps numbers as text of digits, sign, `.` and `e`), cut
    at 40 characters: `bind_kpi.NUMBER` accepts any Unicode digit run, so the text is bounded here."""
    text = str(content)
    return text if len(text) <= 40 else text[:39] + "…"


def compare(baseline, document, lists, pivots):
    """The report dict: counts and the list of differing / failing cells (all of them)."""
    base_sheet, new_sheet = baseline["sheets"][0], document["sheets"][0]
    base_cells, new_cells = base_sheet["cells"], new_sheet["cells"]
    area_rows = base_sheet["rowNumber"]
    evaluator = validate_formulas.SheetEvaluator(document, lists=lists, pivots=pivots)
    counts = {"replaced": 0, "typed": 0, "equal": 0, "rounded": 0, "differ": 0, "error": 0, "untyped": 0,
              "had_text": 0, "formula_left": 0}
    shown = []
    for ref in sorted(new_cells, key=intake_xlsx.split_ref):
        if intake_xlsx.split_ref(ref)[0] > area_rows:
            continue                                   # helper / probe rows sit below the design area
        before = base_cells.get(ref, {}).get("content")
        after = new_cells[ref].get("content")
        if after == before:
            if isinstance(before, str) and before.startswith("="):
                counts["formula_left"] += 1
            continue
        counts["replaced"] += 1
        if _typed(before):
            counts["typed"] += 1
            try:
                live = evaluator.value(ref)
            except (validate_formulas.SheetError, ArithmeticError, ValueError, RecursionError) as exc:
                counts["error"] += 1
                shown.append({"cell": ref, "excel": _show_typed(before), "live": "error:%s" % type(exc).__name__,
                              "class": "error"})
                continue
            if live is None:
                live = 0.0
            outcome = classify(before, live) if isinstance(live, (str, int, float)) else "differ"
            counts[outcome] += 1
            if outcome == "differ":
                shown.append({"cell": ref, "excel": _show_typed(before), "live": _show(live), "class": "differ"})
        elif isinstance(before, str) and before and not before.startswith("="):
            counts["had_text"] += 1
        else:
            counts["untyped"] += 1
    return {"counts": counts, "cells": shown}


def summary_line(counts):
    return ("replaced %d cells: %d typed (%d equal, %d equal after rounding, %d differ, %d error), %d untyped, "
            "%d had text; %d formula cells left to Excel (not compared)"
            % (counts["replaced"], counts["typed"], counts["equal"], counts["rounded"], counts["differ"],
               counts["error"], counts["untyped"], counts["had_text"], counts["formula_left"]))


# ---------------------------------------------------------------- command line

def _year(text):
    value = int(text)
    if not 1900 <= value <= 2200:
        raise argparse.ArgumentTypeError("a year between 1900 and 2200")
    return value


def build_parser():
    parser = argparse.ArgumentParser(prog="reconcile_kpi.py", description=__doc__.split("\n\n")[0],
                                     allow_abbrev=False)
    parser.add_argument("--baseline", required=True, help="baseline JSON written by intake_xlsx.py")
    parser.add_argument("--spec", required=True, help="the KPI spec JSON")
    parser.add_argument("--dashboard", required=True,
                        help="the dashboard JSON written by bind_kpi.py, the file you will publish")
    parser.add_argument("--project", help="project whose shipped source catalog to use (default: the only one installed)")
    parser.add_argument("--year", required=True, type=_year, help="the year the Excel numbers belong to")
    parser.add_argument("--tz", help="time zone for the QMS months (default: the one of your Odoo user)")
    parser.add_argument("--out", help="also write the full report here (JSON)")
    parser.add_argument("--force", action="store_true", help="replace --out when it exists")
    parser.add_argument("--config", help="path of the config file (default: see odoo_rpc.py config-path)")
    parser.add_argument("--confirm-instance", help="the full host name typed by the user (non-local hosts)")
    return parser


def run(args, out, err, client_factory=odoo_rpc.Client):
    baseline, document, produced = bound_document(args.baseline, args.spec, args.dashboard, args.project)
    if args.out and os.path.exists(args.out):
        for other in (args.baseline, args.spec, args.dashboard):
            if os.path.exists(other) and os.path.samefile(args.out, other):
                raise ReconcileError("--out is an input file (%s): refusing to overwrite it" % os.path.basename(other))
        if not args.force:                             # before any RPC and before anything is printed
            raise ReconcileError("%s exists already: pass --force to replace it" % args.out)
    client = client_factory(odoo_rpc.load_config(path=args.config), confirm_instance=args.confirm_instance)
    client.login()
    lists = fetch_lists(client, document, args.year)
    pivots = {}
    tz = None
    if document.get("pivots"):
        tz = resolve_tz(client, args.tz)
        pivots = fetch_pivots(client, document, args.year, tz)
    report = compare(baseline, document, lists, pivots)
    counts = report["counts"]
    print("dashboard sha256 %s, year %d%s" % (produced, args.year, ", time zone %s" % tz if tz else ""), file=out)
    print(summary_line(counts), file=out)
    for item in report["cells"][:MAX_SHOWN]:
        print("%s: Excel %s, live %s (%s)" % (item["cell"], item["excel"], item["live"], item["class"]), file=out)
    if len(report["cells"]) > MAX_SHOWN:
        print("... %d more (use --out for the full list)" % (len(report["cells"]) - MAX_SHOWN), file=out)
    if args.out:
        payload = dict(report, sha256=produced, year=args.year, time_zone=tz)
        intake_xlsx.write_baseline(args.out, intake_xlsx.dump_bytes(payload), force=args.force,
                                   source_path=args.baseline)
    return 0


def main(argv=None, out=None, err=None, **kwargs):
    out, err = out or sys.stdout, err or sys.stderr
    for stream in (out, err):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (ValueError, OSError):
                pass
    args = build_parser().parse_args(argv)
    try:
        return run(args, out, err, **kwargs)
    except TzError as exc:
        print("error: %s" % exc, file=err)
        return 1
    except (ReconcileError, odoo_rpc.ConfigError, odoo_rpc.GuardError, bind_kpi.BindError, intake_xlsx.IntakeError,
            validate_formulas.ValidateError) as exc:
        print("error: %s" % exc, file=err)
        return 2
    except odoo_rpc.RpcError as exc:
        print("error: %s" % exc, file=err)
        return 1
    except RecursionError:
        print("error: the input is nested too deeply", file=err)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=err)
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort: one line, no traceback, never the exception text
        print("error: unexpected %s" % type(exc).__name__, file=err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
