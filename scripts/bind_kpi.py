#!/usr/bin/env python3
"""Binder: the cropped baseline of the QA/QC's Excel + a KPI spec -> an Odoo spreadsheet dashboard JSON
(`ODOO.LIST` reads over list sources, `ODOO.PIVOT` reads over pivot sources, derived formulas).

    python scripts/bind_kpi.py --baseline baseline.json --spec kpi_spec.json --out dashboard.json [--force]
                               [--project NAME]

The LOOK is the baseline's (fills, merges, sizes, formats: `intake_xlsx.py` made it). The binder only replaces the
cells the spec names by `ODOO.LIST` reads over the sources of the project's catalog (`catalogs/<project>.json`, chosen
by `--project`; with one installed catalog it is the default), adds the lists, ONE global Year filter
and hidden helper rows, and changes nothing else. Same baseline + same spec -> byte-identical output.

KPI spec (JSON):
  {"spec_version": 1, "name": "...",                       checked and reported only (the dashboard name is the publisher's --name)
   "year_filter_label": "Year",                            optional
   "baseline_sha256": "...",                               optional: the intake report's sha256, checked
   "labels": {"B9": "expected label text"},                optional: cells whose text must still contain this (label shift)
   "keep_typed": ["C6:N6"],                                optional: typed numbers that stay typed on purpose
   "bindings": [{"cells": "D9:O9", "source": "production_group", "where": {"group_code": "DK_REF"},
                 "year": "current", "columns": "months", "zero": "dash"}]}
A binding's `cells` is ONE cell or a one-row / one-column range; its cells, read left to right or top to bottom, get
the `columns` in order (a column group of the catalog such as "months", or an explicit list; `column` for one cell).
`where` must give every selector of the source (each selection is exactly one row per year, so position 1 is read).
`year` = "current" | "previous" (a second list matched through the source's next-year column). `zero` = "number"
(the read itself) | "dash" (shows `-` on 0 through `IF(helper=0,"-",helper)`; the helper cell sits in a hidden row
below the design area and no formula reads a display cell). `overwrite_text`: true lets a binding replace a cell that
holds a text label (default: refused).
Pivot bindings (a source with kind "pivot", e.g. `qms_history`) take `where` (its selectors), optional `filter`
({"overall_result": ["OK", "Pass"]}: allowed values of the catalog only), `measure` (one of the source's measures,
default `__count`), `year`, `zero` and `month` (1..12: ONE cell for that month instead of the 12 months of the Year
filter). A derived binding has no source: {"cells": "D11:O11", "derived": {"op": "ratio", "of": "D9:O9",
"over": "D10:O10"}} (`ratio`: of/over, `N/A` when over is 0; `prev_period`: of, the previous cell of the series, the
first cell shows `N/A`; `sum`: of, one cell). A list binding must not carry `measure`, `month` or `filter`.

Guards (all fail loudly, nothing is written): source not in the catalog (a new view is needed: write a requirement
text for a developer and stop), selector missing / not allowed, column not in the source, previous year without a
chain, malformed / overlapping / out-of-area targets, a target inside a merge but not its anchor, a target that holds
a text label, a label that moved, a typed number left outside `keep_typed`, a baseline formula that reads a dash
cell, a function the client does not register, a stale baseline hash, free text that could start a formula.
Free text never enters a formula or a domain: every selector value comes from the catalog's closed lists.
Standard library only.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata

import intake_xlsx
import kpi_catalogs
import validate_formulas

FILTER_ID = "kpi-year-filter"
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*", re.ASCII)      # a plain (dotted) field name, never a quote
SPEC_VERSION = 1
MAX_FILE = 16 * 1024 * 1024
MAX_BINDINGS = 500
MAX_BINDING_CELLS = 1000                      # one binding is a short row or column, never a whole sheet
MAX_KEEP_ENTRIES = 200
ALIAS_MAX_COUNT = 20                          # label texts per catalog value (aliases only match labels)
ALIAS_MAX_LEN = 60
NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")      # Excel writes 1.5E-3 too
BINDING_KEYS = {"cells", "source", "where", "year", "columns", "column", "zero", "overwrite_text", "measure", "month",
                "filter", "derived"}
DERIVED_OPS = ("ratio", "prev_period", "sum")
SPEC_KEYS = {"spec_version", "name", "year_filter_label", "baseline_sha256", "labels", "keep_typed", "bindings",
             "intake"}                    # `intake`: the route record written by the skill, informational
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
NO_CATALOG = ("source %r is not in the catalog: this KPI needs a new view or model, which this plugin cannot create: "
              "write a requirement text for a developer and stop")


class BindError(Exception):
    """A spec / baseline / catalog problem (message is safe to print)."""


# ---------------------------------------------------------------- loading

def _read_json(path, what):
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_FILE + 1)
    except OSError as exc:
        raise BindError("cannot read %s: %s" % (what, exc.strerror or "error")) from None
    if len(data) > MAX_FILE:
        raise BindError("%s is larger than %d MB" % (what, MAX_FILE // (1024 * 1024)))
    try:
        return data, json.loads(data.decode("utf-8-sig"), parse_constant=intake_xlsx._no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise BindError("%s is not valid JSON" % what) from None


def load_catalog(path=None, project=None, catalog_dir=None):
    """The source catalog: an explicit `path` (the soft-guarded `--catalog` override), or the shipped catalog of
    `project` (the only installed one when `project` is None). `path` and `project` together are refused."""
    if path and project is not None:
        raise BindError("--project and --catalog cannot be used together")
    if not path:
        try:
            path = kpi_catalogs.catalog_path(project, catalog_dir)
        except kpi_catalogs.CatalogError as exc:
            raise BindError(str(exc)) from None
    _, catalog = _read_json(path, "the catalog")
    if (not isinstance(catalog, dict) or catalog.get("schema") != 1 or not isinstance(catalog.get("sources"), dict)
            or not catalog["sources"]):
        raise BindError("the catalog has an unexpected shape")
    for name, source in catalog["sources"].items():
        if isinstance(source, dict) and source.get("kind") == "pivot":
            if (not isinstance(source.get("model"), str) or not isinstance(source.get("date_field"), str)
                    or not isinstance(source.get("read_audience"), str) or not isinstance(source.get("selectors"), dict)
                    or not isinstance(source.get("measures"), list) or not source["measures"]
                    or not isinstance(source.get("optional_filters", {}), dict)
                    or not all(isinstance(rule, dict) and isinstance(rule.get("values"), list)
                               for rule in list(source["selectors"].values())
                               + list(source.get("optional_filters", {}).values()))):
                raise BindError("the catalog source %r has an unexpected shape" % str(name)[:40])
            _only_identifiers(name, [source["date_field"]] + source["measures"])
            continue
        if (not isinstance(source, dict) or not isinstance(source.get("model"), str)
                or not isinstance(source.get("read_audience"), str) or not isinstance(source.get("selectors"), dict)
                or not isinstance(source.get("column_groups"), dict) or not isinstance(source.get("value_columns"), list)
                or not isinstance(source.get("year_chain"), dict) or "current" not in source["year_chain"]
                or not all(isinstance(rule, dict) and ("values" in rule or ("min" in rule and "max" in rule))
                           for rule in source["selectors"].values())):
            raise BindError("the catalog source %r has an unexpected shape" % str(name)[:40])
        _only_identifiers(name, source["value_columns"])
        if "aliases" in source:
            check_aliases(name, source, source["aliases"])
    return catalog


def normalize_alias(text):
    """The form two label texts are compared in: NFKC, every whitespace removed, case folded (so `SZ DK 냉장고` and
    `SZDK 냉장고` are one text)."""
    return "".join(unicodedata.normalize("NFKC", text).split()).casefold()


def check_aliases(source_name, source, aliases):
    """`aliases` = {selector: {value: [text, ...]}} of a LIST source. The texts are used to MATCH the labels of an Excel
    (scan_excel.py) and never reach a formula or a domain; they are checked anyway: known selector and value, at most
    ALIAS_MAX_COUNT short single-line texts per value, and no text (after normalisation) shared by two values of
    one selector (it would be a false match)."""
    where = "the catalog source %r aliases" % str(source_name)[:40]
    if not isinstance(aliases, dict):
        raise BindError("%s must be an object {selector: {value: [text]}}" % where)
    for selector, by_value in aliases.items():
        rule = source["selectors"].get(selector) if isinstance(selector, str) else None
        if not isinstance(rule, dict) or "values" not in rule or not isinstance(by_value, dict):
            raise BindError("%s name a selector the source does not have (or without a closed list): %r"
                            % (where, str(selector)[:40]))
        seen = {}
        for value, texts in by_value.items():
            if value not in rule["values"] or not isinstance(texts, list) or len(texts) > ALIAS_MAX_COUNT:
                raise BindError("%s: %s=%r is not a value of the selector (or holds more than %d texts)"
                                % (where, selector, str(value)[:40], ALIAS_MAX_COUNT))
            for text in texts:
                if (not isinstance(text, str) or not text.strip() or len(text) > ALIAS_MAX_LEN or CONTROL.search(text)
                        or not normalize_alias(text)):
                    raise BindError("%s: a text of %s=%r is not a short single-line text" % (where, selector, value))
                key = normalize_alias(text)
                if seen.setdefault(key, value) != value:
                    raise BindError("%s: the text %r belongs to two values of %s (%s and %s)"
                                    % (where, text[:40], selector, seen[key], value))


def _only_identifiers(source_name, values):
    """Column, measure and date-field names are written INTO formulas: only plain field names may get there, so even a
    hand-written catalog (`--catalog`) cannot put a quote or a function call into a formula."""
    for value in values:
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
            raise BindError("the catalog source %r holds a name that is not a plain field name: %r"
                            % (str(source_name)[:40], str(value)[:40]))


def _typed_number(content):
    """A typed number: a JSON number, or text that reads as one (the xlsx import keeps `<v>` as text)."""
    if isinstance(content, bool):
        return False
    return isinstance(content, (int, float)) or (isinstance(content, str) and NUMBER.fullmatch(content) is not None)


def _text(value, what, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or CONTROL.search(value):
        raise BindError("%s must be a short single-line text" % what)
    if value.lstrip()[0] in "=+-@":
        raise BindError("%s may not start with = + - or @ (it could become a formula)" % what)
    return value


# ---------------------------------------------------------------- cells

def _block(text, what):
    """(row1, col1, row2, col2) of 'D9' or 'D9:O9'."""
    if not isinstance(text, str):
        raise BindError("%s must be a text like D9 or D9:O9" % what)
    first, _, last = text.partition(":")
    try:
        r1, c1 = intake_xlsx.split_ref(first)
        r2, c2 = intake_xlsx.split_ref(last or first)
    except intake_xlsx.IntakeError:
        raise BindError("%s %r is not a cell or a range" % (what, text[:40])) from None
    if r2 < r1 or c2 < c1:
        raise BindError("%s %r runs backwards" % (what, text[:40]))
    return r1, c1, r2, c2


def parse_cells(text, what):
    """'D9' or 'D9:O9' -> [(row, col)] in reading order; one row or one column only."""
    r1, c1, r2, c2 = _block(text, "%s: `cells`" % what)
    if r1 != r2 and c1 != c2:
        raise BindError("%s: a binding covers one row or one column, not a block (%s)" % (what, text))
    if (r2 - r1 + 1) * (c2 - c1 + 1) > MAX_BINDING_CELLS:
        raise BindError("%s: a binding covers at most %d cells" % (what, MAX_BINDING_CELLS))
    if r1 == r2:
        return [(r1, c) for c in range(c1, c2 + 1)]
    return [(r, c1) for r in range(r1, r2 + 1)]


def keep_rects(entries):
    """The blocks (row1, col1, row2, col2) of the `keep_typed` entries (never expanded into cells)."""
    if not isinstance(entries, list) or len(entries) > MAX_KEEP_ENTRIES:
        raise BindError("`keep_typed` must be a list of at most %d cells or ranges" % MAX_KEEP_ENTRIES)
    return [_block(text, "`keep_typed` entry") for text in entries]


def _in_rects(pos, rects):
    return any(r1 <= pos[0] <= r2 and c1 <= pos[1] <= c2 for r1, c1, r2, c2 in rects)


def ref_of(row, col):
    return "%s%d" % (intake_xlsx.col_letters(col), row)


def _merge_problem(merges, row, col):
    """The merge that contains (row, col) without it being the anchor (top-left) cell, or None."""
    for merge in merges:
        first, _, last = merge.partition(":")
        (r1, c1), (r2, c2) = intake_xlsx.split_ref(first), intake_xlsx.split_ref(last)
        if r1 <= row <= r2 and c1 <= col <= c2 and (row, col) != (r1, c1):
            return merge
    return None


def _formula_rects(content):
    """(row1, col1, row2, col2) of every reference of a baseline formula (string literals ignored)."""
    text, _ = intake_xlsx._scan(content)
    rects = []
    for match in intake_xlsx.RANGE_REF.finditer(text):
        a = (intake_xlsx._row(match.group(4)), intake_xlsx.col_number(match.group(2)))
        b = (intake_xlsx._row(match.group(8)), intake_xlsx.col_number(match.group(6)))
        rects.append((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])))
    rest = intake_xlsx.RANGE_REF.sub(lambda m: " " * len(m.group(0)), text)
    for match in intake_xlsx.CELL_REF.finditer(rest):
        pos = (intake_xlsx._row(match.group(4)), intake_xlsx.col_number(match.group(2)))
        rects.append(pos + pos)
    return rects


# ---------------------------------------------------------------- binding

def _resolve_columns(binding, source, count, what):
    if "columns" in binding and "column" in binding:
        raise BindError("%s: give `columns` or `column`, not both" % what)
    columns = binding.get("columns", binding.get("column"))
    if isinstance(columns, str):
        group = source["column_groups"].get(columns)
        columns = group if group is not None else [columns]
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        raise BindError("%s: `columns` must be a column group name or a list of column names" % what)
    unknown = [c for c in columns if c not in source["value_columns"]]
    if unknown:
        raise BindError("%s: column %r is not in the source (groups: %s)"
                        % (what, unknown[0][:40], ", ".join(sorted(source["column_groups"]))))
    if len(columns) != count:
        raise BindError("%s: %d cells but %d columns" % (what, count, len(columns)))
    return list(columns)


def _resolve_where(binding, source, what):
    where = binding.get("where", {})
    if not isinstance(where, dict) or set(where) != set(source["selectors"]):
        raise BindError("%s: `where` must give exactly %s" % (what, sorted(source["selectors"]) or "nothing"))
    domain = []
    for name in sorted(source["selectors"]):
        rule, value = source["selectors"][name], where[name]
        if "values" in rule:
            ok = isinstance(value, str) and value in rule["values"]
        else:
            ok = isinstance(value, int) and not isinstance(value, bool) and rule["min"] <= value <= rule["max"]
        if not ok:
            allowed = rule["values"] if "values" in rule else "%d..%d" % (rule["min"], rule["max"])
            raise BindError("%s: %s=%r is not allowed, use %s" % (what, name, str(value)[:40], allowed))
        domain.append([name, "=", value])
    return domain


def read_formula(source, list_id, column):
    """The read of position 1. A source every default user may read tolerates a missing row (0); any other source
    uses the raw read, so an unreadable model shows an error and never a silent 0 (Plan step 3, H9)."""
    read = 'ODOO.LIST(%d,1,"%s")' % (list_id, column)
    return "IFERROR(%s+0,0)" % read if source["read_audience"] == "base.group_user" else read


def _list_def(list_id, info, catalog):
    source = catalog["sources"][info["source"]]
    suffix = "".join(" %s" % d[2] for d in info["domain"]) + (" (previous year)" if info["year"] == "previous" else "")
    return {"columns": sorted(info["columns"] | {d[0] for d in info["domain"]}), "domain": info["domain"],
            "model": source["model"], "context": {}, "orderBy": [], "id": str(list_id),
            "name": (source["title"].split(",")[0] + suffix)[:100],
            "fieldMatching": {FILTER_ID: {"chain": source["year_chain"][info["year"]], "type": "date"}}}


def _collect(bindings, catalog, merges, area_rows, area_cols, cells):
    """(lists {id: info}, targets {(row, col): (list id, column, source, zero)}) after every per-binding guard."""
    list_ids, lists, targets = {}, {}, {}
    for index, binding in enumerate(bindings, 1):
        what = "binding %d" % index
        if not isinstance(binding, dict) or set(binding) - BINDING_KEYS:
            raise BindError("%s is not an object with known keys" % what)
        if "derived" in binding:
            continue                              # a derived formula: handled after the reads are known
        source_name = binding.get("source")
        source = catalog["sources"].get(source_name) if isinstance(source_name, str) else None
        if source is None:
            raise BindError(what + ": " + NO_CATALOG % str(source_name)[:40])
        if source.get("kind") == "pivot":
            continue                              # a pivot read: handled by _collect_pivots
        misplaced = sorted(set(binding) & {"measure", "month", "filter"})
        if misplaced:                             # would be ignored silently: the author thinks it filters
            raise BindError("%s: %s only apply to a pivot source, but %s is a list source"
                            % (what, ", ".join(misplaced), source_name))
        cell_list = parse_cells(binding.get("cells"), what)
        columns = _resolve_columns(binding, source, len(cell_list), what)
        domain = _resolve_where(binding, source, what)
        year = binding.get("year", "current")
        if year not in ("current", "previous") or year not in source["year_chain"]:
            raise BindError("%s: year %r is not available for %s (chains: %s)"
                            % (what, str(year)[:20], source_name, ", ".join(sorted(source["year_chain"]))))
        zero = binding.get("zero", "number")
        if zero not in ("number", "dash"):
            raise BindError("%s: `zero` must be number or dash" % what)
        key = (source_name, json.dumps(domain), year)
        if key not in list_ids:
            list_ids[key] = len(list_ids) + 1
            lists[list_ids[key]] = {"source": source_name, "domain": domain, "year": year, "columns": set()}
        lists[list_ids[key]]["columns"].update(columns)
        for (row, col), column in zip(cell_list, columns):
            ref = ref_of(row, col)
            if row > area_rows or col > area_cols:
                raise BindError("%s: %s is outside the design area (%d rows x %d columns)"
                                % (what, ref, area_rows, area_cols))
            if (row, col) in targets:
                raise BindError("%s: %s is already bound by another binding" % (what, ref))
            merge = _merge_problem(merges, row, col)
            if merge:
                raise BindError("%s: %s is inside the merge %s but is not its top-left cell" % (what, ref, merge))
            content = cells.get(ref, {}).get("content")
            if (isinstance(content, str) and content and not content.startswith("=") and not NUMBER.fullmatch(content)
                    and binding.get("overwrite_text") is not True):
                raise BindError("%s: %s holds the text %r: refusing to overwrite a label (a shifted row?); add "
                                "\"overwrite_text\": true if it is meant" % (what, ref, content[:40]))
            targets[(row, col)] = (list_ids[key], column, source, zero)
    return lists, targets


def _check_target(what, row, col, merges, area_rows, area_cols, cells, taken, binding):
    ref = ref_of(row, col)
    if row > area_rows or col > area_cols:
        raise BindError("%s: %s is outside the design area (%d rows x %d columns)" % (what, ref, area_rows, area_cols))
    if (row, col) in taken:
        raise BindError("%s: %s is already bound by another binding" % (what, ref))
    merge = _merge_problem(merges, row, col)
    if merge:
        raise BindError("%s: %s is inside the merge %s but is not its top-left cell" % (what, ref, merge))
    content = cells.get(ref, {}).get("content")
    if (isinstance(content, str) and content and not content.startswith("=") and not NUMBER.fullmatch(content)
            and binding.get("overwrite_text") is not True):
        raise BindError("%s: %s holds the text %r: refusing to overwrite a label (a shifted row?); add "
                        "\"overwrite_text\": true if it is meant" % (what, ref, content[:40]))


def _resolve_filter(binding, source, what):
    chosen = binding.get("filter", {})
    allowed = source.get("optional_filters", {})
    if not isinstance(chosen, dict) or set(chosen) - set(allowed):
        raise BindError("%s: `filter` may only use %s" % (what, sorted(allowed) or "nothing"))
    domain = []
    for name in sorted(chosen):
        values = chosen[name]
        if (not isinstance(values, list) or not values or len(set(map(str, values))) != len(values)
                or any(not isinstance(v, str) or v not in allowed[name]["values"] for v in values)):
            raise BindError("%s: filter %s must be a non-empty list of distinct values from %s"
                            % (what, name, allowed[name]["values"]))
        domain.append([name, "in", list(values)])
    return domain


def _collect_pivots(bindings, catalog, merges, area_rows, area_cols, cells, taken):
    """(pivots {id: info}, probes {source: id}, reads {(row, col): (kind info)}) for the pivot bindings.

    Per distinct (source, where, filter) one DATA pivot (never matched on the Year filter, so the previous year can
    be read) and per source one YEAR PROBE pivot matched on the Year filter: the year of its first month column is
    the selected year (verified in the browser: an empty selection gives the date 0, year 1899, and every read
    shows 0)."""
    pivot_ids, pivots, probes, reads = {}, {}, {}, {}
    for index, binding in enumerate(bindings, 1):
        what = "binding %d" % index
        source_name = binding.get("source")
        source = catalog["sources"].get(source_name) if isinstance(source_name, str) else None
        if not isinstance(binding, dict) or "derived" in binding or source is None or source.get("kind") != "pivot":
            continue
        for key in ("columns", "column"):
            if key in binding:
                raise BindError("%s: a pivot binding has no `%s` (use `measure`)" % (what, key))
        cell_list = parse_cells(binding.get("cells"), what)
        measure = binding.get("measure", "__count")
        if measure not in source["measures"]:
            raise BindError("%s: measure %r is not in the source (use one of %s)"
                            % (what, str(measure)[:40], source["measures"]))
        month = binding.get("month")
        if month is None:
            if len(cell_list) != 12:
                raise BindError("%s: a pivot binding without `month` covers the 12 months (12 cells), got %d"
                                % (what, len(cell_list)))
            months = list(range(1, 13))
        else:
            if (not isinstance(month, int) or isinstance(month, bool) or not 1 <= month <= 12
                    or len(cell_list) != 1):
                raise BindError("%s: `month` is a number 1..12 for ONE cell" % what)
            months = [month]
        year = binding.get("year", "current")
        if year not in ("current", "previous"):
            raise BindError("%s: `year` must be current or previous" % what)
        zero = binding.get("zero", "number")
        if zero not in ("number", "dash"):
            raise BindError("%s: `zero` must be number or dash" % what)
        domain = _resolve_where(binding, source, what) + _resolve_filter(binding, source, what)
        key = (source_name, json.dumps(domain))
        if key not in pivot_ids:
            pivot_ids[key] = len(pivot_ids) + 1
            pivots[pivot_ids[key]] = {"source": source_name, "domain": domain, "measures": set()}
        pivots[pivot_ids[key]]["measures"].add(measure)
        probes.setdefault(source_name, None)
        for (row, col), month_number in zip(cell_list, months):
            _check_target(what, row, col, merges, area_rows, area_cols, cells, set(taken) | set(reads), binding)
            reads[(row, col)] = {"pivot": pivot_ids[key], "measure": measure, "month": month_number,
                                 "year": year, "source": source, "source_name": source_name, "zero": zero}
    return pivots, probes, reads


def _pivot_def(pivot_id, info, catalog):
    source = catalog["sources"][info["source"]]
    return {"colGroupBys": ["%s:month" % source["date_field"]], "rowGroupBys": [], "context": {},
            "domain": info["domain"], "id": str(pivot_id),
            "measures": [{"field": m} for m in sorted(info["measures"])], "model": source["model"],
            "name": (source["title"].split(":")[0] + " " + " ".join(str(d[2]) for d in info["domain"]))[:100],
            "sortedColumn": None, "fieldMatching": {}}


def _probe_def(pivot_id, source_name, source):
    return {"colGroupBys": ["%s:month" % source["date_field"]], "rowGroupBys": [], "context": {}, "domain": [],
            "id": str(pivot_id), "measures": [{"field": "__count"}], "model": source["model"],
            "name": ("Year of the filter: " + source_name)[:100], "sortedColumn": None,
            "fieldMatching": {FILTER_ID: {"chain": source["date_field"], "type": "date"}}}


def pivot_read_formula(source, pivot_id, measure, month, year, year_ref):
    key = '"%02d/"&%s' % (month, year_ref if year == "current" else "(%s-1)" % year_ref)
    read = 'ODOO.PIVOT(%d,"%s","%s:month",%s)+0' % (pivot_id, measure, source["date_field"], key)
    return "IFERROR(%s,0)" % read if source["read_audience"] == "base.group_user" else read


def _collect_derived(bindings, reads, dash_positions, taken, merges, area_rows, area_cols, cells):
    """{(row, col): (op, [source refs])} for the derived bindings. A derived cell reads only bound NUMBER cells."""
    derived = {}
    for index, binding in enumerate(bindings, 1):
        what = "binding %d" % index
        if not isinstance(binding, dict) or "derived" not in binding:
            continue
        spec = binding["derived"]
        if (not isinstance(spec, dict) or spec.get("op") not in DERIVED_OPS
                or set(binding) - {"cells", "derived", "overwrite_text"}):
            raise BindError("%s: `derived` is {\"op\": one of %s, ...} and the binding has only `cells`" % (what, DERIVED_OPS))
        op = spec["op"]
        cell_list = parse_cells(binding.get("cells"), what)
        wanted = {"ratio": {"op", "of", "over"}, "prev_period": {"op", "of"}, "sum": {"op", "of"}}[op]
        if set(spec) != wanted:
            raise BindError("%s: the %s operation takes exactly %s" % (what, op, sorted(wanted)))
        sources = {}
        for field in sorted(wanted - {"op"}):
            positions = parse_cells(spec[field], "%s: `%s`" % (what, field))
            missing = [ref_of(*p) for p in positions if p not in reads]
            if missing:
                raise BindError("%s: `%s` must be cells bound by a list or pivot binding, %s is not"
                                % (what, field, missing[0]))
            sources[field] = positions
        if op == "sum":
            if len(cell_list) != 1:
                raise BindError("%s: sum fills ONE cell" % what)
        elif any(len(positions) != len(cell_list) for positions in sources.values()):
            raise BindError("%s: the ranges and `cells` must have the same number of cells" % what)
        for row, col in cell_list:
            _check_target(what, row, col, merges, area_rows, area_cols, cells, set(taken) | set(derived), binding)
            derived[(row, col)] = None
        if op == "sum":
            kinds = {pos in dash_positions for pos in sources["of"]}
            if len(kinds) != 1:
                raise BindError("%s: the cells to sum mix cells that show `-` with plain ones: bind them alike" % what)
        for position, (row, col) in enumerate(cell_list):
            if op == "sum":
                derived[(row, col)] = (op, {"of": sources["of"]}, position)
            elif op == "prev_period":
                derived[(row, col)] = (op, {"of": sources["of"][position],
                                            "previous": sources["of"][position - 1] if position else None}, position)
            else:
                derived[(row, col)] = (op, {field: positions[position] for field, positions in sources.items()},
                                       position)
    return derived


def bind(baseline, spec, catalog, baseline_bytes=None):
    """(dashboard document, report) for a cropped one-sheet baseline and a spec."""
    if not isinstance(spec, dict) or set(spec) - SPEC_KEYS:
        raise BindError("the spec is not an object with known keys")
    if type(spec.get("spec_version")) is not int or spec["spec_version"] != SPEC_VERSION:
        raise BindError("spec_version must be %d" % SPEC_VERSION)
    name = _text(spec.get("name"), "`name`", 100)
    label = _text(spec.get("year_filter_label", "Year"), "`year_filter_label`", 40)
    bindings = spec.get("bindings")
    if not isinstance(bindings, list) or not bindings or len(bindings) > MAX_BINDINGS:
        raise BindError("`bindings` must be a list of 1..%d bindings" % MAX_BINDINGS)
    if spec.get("baseline_sha256") is not None and baseline_bytes is not None:
        if spec["baseline_sha256"] != hashlib.sha256(baseline_bytes).hexdigest():
            raise BindError("the baseline changed since the spec was written (sha256 differs): run the intake again "
                            "and review the spec")
    if (not isinstance(baseline, dict) or not isinstance(baseline.get("sheets"), list)
            or len(baseline["sheets"]) != 1 or not isinstance(baseline["sheets"][0], dict)):
        raise BindError("the baseline must hold exactly one sheet (run intake_xlsx.py with --sheet and --area)")
    sheet = baseline["sheets"][0]
    try:
        cells = {ref: dict(cell) for ref, cell in sheet["cells"].items()}
        merges, area_rows, area_cols = list(sheet.get("merges", [])), int(sheet["rowNumber"]), int(sheet["colNumber"])
        rows = dict(sheet.get("rows", {}))
        if not all(isinstance(merge, str) for merge in merges):
            raise TypeError("merge")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise BindError("the baseline sheet is malformed (cells / merges / rowNumber / colNumber)") from None

    labels = spec.get("labels", {})
    if not isinstance(labels, dict):
        raise BindError("`labels` must be an object {cell: text}")
    for ref in sorted(labels, key=intake_xlsx.split_ref):
        actual, expected = cells.get(ref, {}).get("content"), labels[ref]
        if (not isinstance(expected, str) or not expected.strip() or not isinstance(actual, str)
                or expected.strip() not in actual):
            raise BindError("label shift: %s should contain %r but holds %r" % (ref, str(expected)[:40], str(actual)[:40]))

    lists, targets = _collect(bindings, catalog, merges, area_rows, area_cols, cells)
    pivots, probes, pivot_reads = _collect_pivots(bindings, catalog, merges, area_rows, area_cols, cells, set(targets))
    reads_all = set(targets) | set(pivot_reads)
    dash_positions = ({pos for pos, (_, _, _, zero) in targets.items() if zero == "dash"}
                      | {pos for pos, info in pivot_reads.items() if info["zero"] == "dash"})
    derived = _collect_derived(bindings, reads_all, dash_positions, reads_all, merges, area_rows, area_cols, cells)

    keep = keep_rects(spec.get("keep_typed", []))
    leftovers = sorted((ref for ref, cell in cells.items()
                        if _typed_number(cell.get("content"))
                        and intake_xlsx.split_ref(ref) not in reads_all
                        and intake_xlsx.split_ref(ref) not in derived
                        and not _in_rects(intake_xlsx.split_ref(ref), keep)),
                       key=intake_xlsx.split_ref)
    if leftovers:
        raise BindError("typed numbers left outside the bindings: %s%s: bind them or list them in `keep_typed`"
                        % (", ".join(leftovers[:20]), " ..." if len(leftovers) > 20 else ""))

    dash_cells = set(dash_positions)
    helper_rows = {row: area_rows + 2 + offset for offset, row in enumerate(sorted({r for r, _ in dash_cells}))}
    next_helper = max([area_rows + 1] + list(helper_rows.values())) + 1
    year_rows = {}
    for source_name in sorted(probes):             # one year cell (column A) per pivot source, in a hidden row
        year_rows[source_name] = next_helper
        next_helper += 1
    dash_sorted = sorted(dash_cells | set(derived))     # a derived cell may show "N/A": nothing may read it either
    for ref in sorted(cells, key=intake_xlsx.split_ref):
        content = cells[ref].get("content")
        if (isinstance(content, str) and content.startswith("=") and intake_xlsx.split_ref(ref) not in targets
                and intake_xlsx.split_ref(ref) not in pivot_reads and intake_xlsx.split_ref(ref) not in derived):
            for r1, c1, r2, c2 in _formula_rects(content):
                hit = next((pos for pos in dash_sorted if r1 <= pos[0] <= r2 and c1 <= pos[1] <= c2), None)
                if hit:
                    raise BindError("the formula of %s reads %s, a cell that shows `-` on zero or `N/A`: bind %s with "
                                    "\"zero\": \"number\" or change that formula" % (ref, ref_of(*hit), ref_of(*hit)))
    for (row, col), (list_id, column, source, zero) in sorted(targets.items()):
        read = read_formula(source, list_id, column)
        ref = ref_of(row, col)
        if zero == "dash":
            helper = ref_of(helper_rows[row], col)
            cells[helper] = {"content": "=" + read}
            cells.setdefault(ref, {})["content"] = '=IF(%s=0,"-",%s)' % (helper, helper)
        else:
            cells.setdefault(ref, {})["content"] = "=" + read

    probe_ids = {name: len(pivots) + index + 1 for index, name in enumerate(sorted(probes))}
    for source_name, year_row in sorted(year_rows.items()):
        probe_id = probe_ids[source_name]
        source = catalog["sources"][source_name]
        cells["A%d" % year_row] = {"content": '=IFERROR(YEAR(ODOO.PIVOT.HEADER(%d,"#%s:month",1)),0)'
                                              % (probe_id, source["date_field"])}
    for (row, col), info in sorted(pivot_reads.items()):
        read = pivot_read_formula(info["source"], info["pivot"], info["measure"], info["month"], info["year"],
                                  "$A$%d" % year_rows[info["source_name"]])
        ref = ref_of(row, col)
        if info["zero"] == "dash":
            helper = ref_of(helper_rows[row], col)
            cells[helper] = {"content": "=" + read}
            cells.setdefault(ref, {})["content"] = '=IF(%s=0,"-",%s)' % (helper, helper)
        else:
            cells.setdefault(ref, {})["content"] = "=" + read

    def number_ref(position):
        """The cell a derived formula reads: the helper of a dash cell, otherwise the number cell itself."""
        if position in dash_cells:
            return ref_of(helper_rows[position[0]], position[1])
        return ref_of(*position)

    for (row, col), (op, srcs, position) in sorted(derived.items()):
        ref = ref_of(row, col)
        if op == "ratio":
            num, den = number_ref(srcs["of"]), number_ref(srcs["over"])
            cells.setdefault(ref, {})["content"] = '=IF(%s>0,%s/%s,"N/A")' % (den, num, den)
        elif op == "prev_period":
            if position == 0:
                cells.setdefault(ref, {})["content"] = "N/A"          # the first period has no previous one
            else:
                previous_ref = number_ref(srcs["previous"])
                cells.setdefault(ref, {})["content"] = '=IF(%s>0,%s/%s,"N/A")' % (
                    previous_ref, number_ref(srcs["of"]), previous_ref)
        else:
            refs = [number_ref(p) for p in srcs["of"]]
            cells.setdefault(ref, {})["content"] = "=SUM(%s:%s)" % (refs[0], refs[-1])

    out_sheet = dict(sheet)
    out_sheet["cells"] = dict(sorted(cells.items(), key=lambda kv: intake_xlsx.split_ref(kv[0])))
    for helper_row in list(helper_rows.values()) + list(year_rows.values()):
        rows[str(helper_row - 1)] = {"size": 23, "isHidden": True}
    out_sheet["rows"] = dict(sorted(rows.items(), key=lambda kv: int(kv[0])))
    out_sheet["rowNumber"] = max([area_rows] + list(helper_rows.values()) + list(year_rows.values()))
    document = {"version": baseline.get("version", 14.5), "sheets": [out_sheet]}
    document.update({k: v for k, v in baseline.items() if k not in ("version", "sheets")})
    document["pivots"] = {**{str(i): _pivot_def(i, info, catalog) for i, info in sorted(pivots.items())},
                          **{str(i): _probe_def(i, name, catalog["sources"][name])
                             for name, i in sorted(probe_ids.items(), key=lambda kv: kv[1])}}
    document["pivotNextId"] = len(document["pivots"]) + 1
    document["lists"] = {str(i): _list_def(i, info, catalog) for i, info in sorted(lists.items())}
    document["listNextId"] = len(lists) + 1
    document["globalFilters"] = [{"id": FILTER_ID, "type": "date", "label": label, "rangeType": "single_date",
                                  "singleDateType": "year"}]
    document["revisionId"] = intake_xlsx.BASELINE_REVISION
    problems = validate_formulas.unknown_function_problems(document, validate_formulas.load_frozen())
    if problems:
        raise BindError("; ".join(problems))
    report = {"name": name, "lists": len(lists), "pivots": len(pivots), "derived_cells": len(derived),
              "bound_cells": len(targets) + len(pivot_reads), "dash_cells": len(dash_cells),
              "helper_rows": len(helper_rows),
              "kept_typed": sum(1 for ref, cell in cells.items() if _in_rects(intake_xlsx.split_ref(ref), keep)
                                and _typed_number(cell.get("content")))}
    return document, report


# ---------------------------------------------------------------- command line

def build_parser():
    parser = argparse.ArgumentParser(prog="bind_kpi.py", description=__doc__.split("\n\n")[0], allow_abbrev=False)
    parser.add_argument("--baseline", required=True, help="baseline JSON written by intake_xlsx.py")
    parser.add_argument("--spec", required=True, help="the KPI spec JSON")
    parser.add_argument("--out", required=True, help="where to write the dashboard JSON")
    parser.add_argument("--project", help="project whose shipped source catalog to use (default: the only one installed)")
    parser.add_argument("--catalog", help="a source catalog file instead of a shipped one (developers only)")
    parser.add_argument("--force", action="store_true", help="replace --out when it exists")
    return parser


def run(args, out):
    baseline_bytes, baseline = _read_json(args.baseline, "the baseline")
    _, spec = _read_json(args.spec, "the spec")
    document, report = bind(baseline, spec, load_catalog(args.catalog, args.project), baseline_bytes)
    data = intake_xlsx.dump_bytes(document)
    if os.path.exists(args.out):
        for other in (args.baseline, args.spec, args.catalog):
            if other and os.path.exists(other) and os.path.samefile(args.out, other):
                raise BindError("--out is the input file (%s): refusing to overwrite it" % os.path.basename(other))
    intake_xlsx.write_baseline(args.out, data, force=args.force, source_path=args.baseline)
    report.update({"out": args.out, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    json.dump(report, out, ensure_ascii=False, indent=1)
    out.write("\n")
    return 0


def main(argv=None, out=None, err=None):
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
        return run(args, out)
    except (BindError, intake_xlsx.IntakeError, validate_formulas.ValidateError) as exc:
        print("error: %s" % exc, file=err)
        return 2
    except RecursionError:
        print("error: the input is nested too deeply", file=err)
        return 2
    except Exception as exc:  # noqa: BLE001 - last resort: one line, no traceback
        print("error: unexpected %s" % type(exc).__name__, file=err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
