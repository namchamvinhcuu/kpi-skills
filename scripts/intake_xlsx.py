#!/usr/bin/env python3
"""Baseline intake: the converted spreadsheet JSON of ONE sheet / area of the QA/QC's Excel, cropped and deterministic.

Odoo converts an xlsx into spreadsheet JSON in the browser only. Three ways to hand that JSON to this script
(a route is never mixed within one sheet; the route name goes into the KPI spec):

  --document-id N     route `snapshot`: the spreadsheet document N already holds a snapshot. The user opened the
                      Excel as a spreadsheet in Documents with `?debug=1` and used File > Snapshot.
  --xlsx-document N   route `snapshot`: N is the uploaded xlsx. The script clones it, prints the URL to open, waits
                      until the user did File > Snapshot, reads it and deletes the clone (unless --keep).
  --file PATH         route `download`: the file saved by File > Download as JSON (debug mode); no connection needed.

Without --sheet and --area only the sheet inventory is printed. With them the baseline is written to --out:
the chosen sheet cropped to the cells A1:<area end>, `revisionId` fixed, byte-identical for the same input.
The crop FAILS loudly on anything it would silently cut: a merge straddling the area edge, a formula reading outside
the area or another sheet, a sheet / workbook key it does not know, an Odoo list / pivot / filter, a figure.

Never prints the API key. Standard library only.
"""
import argparse
import base64
import binascii
import copy
import json
import os
import re
import sys
import tempfile
import time
import hashlib
import math

import odoo_rpc

BASELINE_REVISION = "START_REVISION"   # the revisionId of a never-edited Download as JSON (same value, both routes)
MAX_AREA_CELLS = 1000000
KNOWN_TOP_KEYS = frozenset({"version", "sheets", "entities", "styles", "formats", "borders", "revisionId",
                            "uniqueFigureIds", "settings", "odooVersion", "globalFilters", "pivots", "pivotNextId",
                            "lists", "listNextId", "chartOdooMenusReferences"})
EMPTY_TOP_KEYS = ("entities", "lists", "pivots", "globalFilters", "chartOdooMenusReferences")
KNOWN_SHEET_KEYS = frozenset({"id", "name", "colNumber", "rowNumber", "rows", "cols", "merges", "cells",
                              "conditionalFormats", "dataValidationRules", "filterTables", "figures", "headerGroups",
                              "areGridLinesVisible", "isVisible", "zoom"})
EMPTY_SHEET_KEYS = ("conditionalFormats", "dataValidationRules", "filterTables", "figures")
KNOWN_CELL_KEYS = frozenset({"content", "style", "format", "border"})
KNOWN_SIZE_KEYS = frozenset({"size", "isHidden"})
TABLE_OF = {"style": "styles", "format": "formats", "border": "borders"}
CELL_REF = re.compile(r"(?<![A-Za-z0-9_.])(\$?)([A-Za-z]{1,3})(\$?)(\d+)(?![\d(])", re.ASCII)
RANGE_REF = re.compile(r"(?<![A-Za-z0-9_.])(\$?)([A-Za-z]{1,3})(\$?)(\d+)\s*:\s*"
                       r"(\$?)([A-Za-z]{1,3})(\$?)(\d+)(?![\d(])", re.ASCII)
# scanned left to right, with the parser's own string rule (o_spreadsheet.js tokenizeString): a quote closes the string
# unless a backslash precedes it, `""` is NOT an escape; a quote inside 'a"b'!A1 comes after the apostrophe
# an UNTERMINATED literal runs to the end of the cell (one linear pass) and is flagged as unbalanced by `_scan`
# a markdown link `[x](...)` or a web / mail / odoo link in a cell would be clickable for every viewer of the dashboard
LINK_IN_TEXT = re.compile(r"\]\(|\b(?:https?|ftp|file|odoo)://|\b(?:mailto|javascript):", re.I)
HYPERLINK_CALL = re.compile(r"(?<![\w.$])hyperlink[\s﻿]*\(", re.I)
LITERAL_OR_SHEET_MARK = re.compile(r'"(?:[^"]|(?<=\\)")*(?:(?<!\\)"|\Z)|[\'!]')
SIZE_INDEX = re.compile(r"\d{1,7}", re.ASCII)
MAX_NAMES = 10
MIN_POLL_INTERVAL, MAX_WAIT_SECONDS, POLL_FAILURES = 1.0, 3600, 2
SNAPSHOT_HELP = ("open the Excel as a spreadsheet in Documents with `?debug=1` at the end of the address, then use "
                 "the menu File > Snapshot")


class IntakeError(Exception):
    """The input cannot be turned into a baseline (message is safe to print)."""


# ---------------------------------------------------------------- cell references

def col_number(letters):
    number = 0
    for char in letters.upper():
        number = number * 26 + ord(char) - 64
    return number


def col_letters(number):
    letters = ""
    while number:
        number, rest = divmod(number - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def split_ref(ref):
    """'D9' -> (row 9, col 4), 1-based."""
    match = re.fullmatch(r"([A-Z]{1,3})(\d{1,7})", ref, re.ASCII) if isinstance(ref, str) else None
    if not match or int(match.group(2)) < 1:
        raise IntakeError("not a cell reference: %s" % _short(ref))
    return int(match.group(2)), col_number(match.group(1))


def parse_area(area):
    """'A1:Q31' -> (last_row, last_col); the area starts at A1 so every cell keeps its address."""
    first, _, last = area.partition(":") if isinstance(area, str) else ("", "", "")
    if first != "A1" or not last:
        raise IntakeError("the area must look like A1:Q31 (it starts at A1), got %s" % _short(area))
    row, col = split_ref(last)
    if row * col > MAX_AREA_CELLS:
        raise IntakeError("the area %s is too large" % area)
    return row, col


def _names(keys):
    names = sorted(_short(key, 40) for key in keys)
    return "[%s%s]" % (", ".join(names[:MAX_NAMES]), ", ..." if len(names) > MAX_NAMES else "")


def _short(value, limit=80):
    text = str(value).replace("\n", " ")
    text = "".join(ch if ch.isprintable() else "?" for ch in text)
    return text if len(text) <= limit else text[:limit] + "..."


def _scan(content):
    """(the formula with every string literal blanked, why it cannot be cropped or None)."""
    marks, unbalanced = [], []

    def blank(match):
        mark = match.group(0)
        if mark in ("'", "!"):
            marks.append(mark)
        elif not (len(mark) >= 2 and mark[-1] == '"' and mark[-2] != "\\"):
            unbalanced.append(mark)                      # the literal reached the end of the cell without closing
        return " " if mark[0] == '"' else mark

    text = LITERAL_OR_SHEET_MARK.sub(blank, content)
    if marks:
        return text, "reads another sheet"
    if unbalanced or '"' in text:
        return text, "has an unbalanced quote"
    return text, None


def formula_problem(content):
    """Why a formula cannot be cropped, or None. Every `:` must belong to a plain A1:B2 range the crop can check."""
    text, problem = _scan(content)
    if problem:
        return problem
    if ":" in RANGE_REF.sub(" ", text):
        return "uses a whole row or column range"
    return None


def _row(digits):
    """A row number from the digits of a reference; absurdly long digit strings stay "far outside" (no int() limit)."""
    return int(digits) if len(digits) <= 9 else 10 ** 9


def formula_refs(content):
    """(row, col) of every plain cell reference and of both ends of every A1:B2 range, in text order."""
    text, _ = _scan(content)
    found = []
    for match in RANGE_REF.finditer(text):
        found.append((match.start(4), (_row(match.group(4)), col_number(match.group(2)))))
        found.append((match.start(8), (_row(match.group(8)), col_number(match.group(6)))))
    rest = RANGE_REF.sub(lambda m: " " * len(m.group(0)), text)
    found += [(m.start(4), (_row(m.group(4)), col_number(m.group(2)))) for m in CELL_REF.finditer(rest)]
    return [ref for _, ref in sorted(found)]


# ---------------------------------------------------------------- snapshot parsing and crop

def _no_constant(name):
    raise ValueError(name)


def parse_snapshot(raw_bytes):
    """The converted workbook (a dict) from the bytes of a snapshot or a downloaded .osheet.json."""
    if len(raw_bytes) > odoo_rpc.MAX_RESPONSE:
        raise IntakeError("the spreadsheet JSON is larger than %d MB" % (odoo_rpc.MAX_RESPONSE // (1024 * 1024)))
    try:
        text = raw_bytes.decode("utf-8-sig")
        data = json.loads(text, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise IntakeError("the data is not spreadsheet JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("sheets"), list) or not data["sheets"]:
        raise IntakeError("the data is not spreadsheet JSON (no sheets)")
    for sheet in data["sheets"]:
        if (not isinstance(sheet, dict) or not isinstance(sheet.get("name"), str)
                or not isinstance(sheet.get("cells"), dict)):
            raise IntakeError("the data is not spreadsheet JSON (a sheet has no name or no cells)")
    return data


def inventory(raw):
    """One row per sheet: name, visible, cells with content, last cell that has content."""
    rows = []
    for sheet in raw["sheets"]:
        last_row = last_col = 0
        filled = 0
        for ref, cell in sheet["cells"].items():
            if isinstance(cell, dict) and cell.get("content") not in (None, ""):
                try:
                    row, col = split_ref(ref)
                except IntakeError:
                    continue
                filled += 1
                last_row, last_col = max(last_row, row), max(last_col, col)
        rows.append({"name": sheet["name"], "visible": sheet.get("isVisible", True) is not False,
                     "content_cells": filled, "last_cell": col_letters(last_col) + str(last_row) if filled else ""})
    return rows


def assert_supported(raw, sheet):
    """Fail loudly on anything the crop would silently cut or does not understand."""
    unknown = set(raw) - KNOWN_TOP_KEYS
    if unknown:
        raise IntakeError("unknown workbook keys %s: the converter changed, ask the plugin owner" % _names(unknown))
    for key in EMPTY_TOP_KEYS:
        if raw.get(key):
            raise IntakeError("the snapshot holds Odoo %s (use a clean copy of the Excel, without lists / pivots / "
                              "filters / chart links)" % key)
    unknown = set(sheet) - KNOWN_SHEET_KEYS
    if unknown:
        raise IntakeError("sheet %s has keys the intake does not know %s" % (_short(sheet["name"]), _names(unknown)))
    for key in EMPTY_SHEET_KEYS:
        if sheet.get(key):
            raise IntakeError("sheet %s has %s (not supported: the crop would cut them)" % (_short(sheet["name"]), key))
    groups = sheet.get("headerGroups") or {}
    if not isinstance(groups, dict) or any(groups.values()):
        raise IntakeError("sheet %s has grouped rows or columns (not supported)" % _short(sheet["name"]))
    for kind in ("rows", "cols"):
        sizes = sheet.get(kind, {})
        if not isinstance(sizes, dict):
            raise IntakeError("sheet %s: %s is not an object" % (_short(sheet["name"]), kind))
        for index, size in sizes.items():
            if not isinstance(size, dict) or set(size) - KNOWN_SIZE_KEYS or not SIZE_INDEX.fullmatch(str(index)):
                raise IntakeError("sheet %s: %s %s has properties the intake does not know"
                                  % (_short(sheet["name"]), kind, _short(index)))


def _whole(value, what):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise IntakeError("the sheet has a bad %s" % what)
    return value


def crop(raw, sheet_name, area):
    """(baseline, report): `raw` reduced to the sheet `sheet_name` and the cells A1..`area`, in a fixed order."""
    last_row, last_col = parse_area(area)
    sheets = [s for s in raw["sheets"] if s["name"] == sheet_name]
    if len(sheets) != 1:
        raise IntakeError("expected exactly one sheet named %s, found %d; the sheets are: %s"
                          % (_short(sheet_name), len(sheets), ", ".join(_short(s["name"]) for s in raw["sheets"])))
    sheet = sheets[0]
    assert_supported(raw, sheet)
    rows_total, cols_total = _whole(sheet.get("rowNumber"), "rowNumber"), _whole(sheet.get("colNumber"), "colNumber")
    if last_row > rows_total or last_col > cols_total:
        raise IntakeError("the area %s is bigger than the sheet (%d rows x %d columns)"
                          % (area, rows_total, cols_total))

    def inside(ref):
        row, col = split_ref(ref)
        return row <= last_row and col <= last_col

    cells, dropped = {}, 0
    for ref in sorted(sheet["cells"], key=split_ref):
        cell = sheet["cells"][ref]
        if not isinstance(cell, dict):
            raise IntakeError("cell %s is not an object" % _short(ref))
        if not inside(ref):
            dropped += cell.get("content") not in (None, "")
            continue
        extra = set(cell) - KNOWN_CELL_KEYS
        if extra:
            raise IntakeError("cell %s has properties the intake does not know %s" % (ref, _names(extra)))
        for key, table in TABLE_OF.items():
            if key in cell and str(cell[key]) not in raw.get(table, {}):
                raise IntakeError("cell %s points to an unknown %s id" % (ref, key))
        content = cell.get("content")
        if isinstance(content, str) and LINK_IN_TEXT.search(content):
            raise IntakeError("cell %s (%s) holds a link: links are not copied into a dashboard that every internal "
                              "user sees; remove it from the design area" % (ref, _short(content)))
        if isinstance(content, str) and content.startswith("="):
            if HYPERLINK_CALL.search(_scan(content)[0]):
                raise IntakeError("formula of %s (%s) calls HYPERLINK: links are not copied into a dashboard that "
                                  "every internal user sees" % (ref, _short(content)))
            problem = formula_problem(content)
            if problem:
                raise IntakeError("formula of %s (%s) %s" % (ref, _short(content), problem))
            for row, col in formula_refs(content):
                if row > last_row or col > last_col:
                    raise IntakeError("formula of %s (%s) reads outside the area %s" % (ref, _short(content), area))
        cells[ref] = copy.deepcopy(cell)
    merges = []
    for merge in sheet.get("merges", []):
        first, _, last = merge.partition(":") if isinstance(merge, str) else ("", "", "")
        if not last:
            raise IntakeError("malformed merge %s" % _short(merge))
        (row1, col1), (row2, col2) = split_ref(first), split_ref(last)
        if min(row1, row2) > last_row or min(col1, col2) > last_col:
            continue                                  # entirely outside: cropped away with its cells
        if max(row1, row2) > last_row or max(col1, col2) > last_col:
            raise IntakeError("merge %s straddles the edge of the area %s: widen the area" % (merge, area))
        merges.append(merge)
    out_sheet = {k: (v if k in ("cells", "rows", "cols", "merges") else copy.deepcopy(v)) for k, v in sheet.items()}
    out_sheet["cells"] = cells
    out_sheet["merges"] = merges
    out_sheet["cols"] = {k: copy.deepcopy(v) for k, v in sheet.get("cols", {}).items() if int(k) < last_col}
    out_sheet["rows"] = {k: copy.deepcopy(v) for k, v in sheet.get("rows", {}).items() if int(k) < last_row}
    out_sheet["colNumber"], out_sheet["rowNumber"] = last_col, last_row
    baseline = {k: (v if k == "sheets" else copy.deepcopy(v)) for k, v in raw.items()}
    baseline["sheets"] = [out_sheet]
    baseline["revisionId"] = BASELINE_REVISION
    report = {"sheet": sheet_name, "area": area, "cells": len(cells), "merges": len(merges),
              "content_cells": sum(1 for c in cells.values() if c.get("content") not in (None, "")),
              "dropped_content_cells": dropped}
    return baseline, report


def dump_bytes(data):
    """The one canonical serialisation: UTF-8, LF line ends on every OS, fixed indent."""
    try:
        return (json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False) + "\n").encode("utf-8")
    except UnicodeEncodeError:
        raise IntakeError("the sheet holds text that cannot be written as UTF-8") from None
    except (ValueError, RecursionError):
        raise IntakeError("the sheet holds a number or nesting that cannot be written as JSON") from None


def write_baseline(path, payload, force=False, source_path=None):
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        raise IntakeError("the folder of --out does not exist: %s" % directory)
    if os.path.exists(path):
        if source_path and os.path.samefile(path, source_path):
            raise IntakeError("--out is the input file: refusing to overwrite it")
        if not force:
            raise IntakeError("%s exists already: pass --force to replace it" % path)
    handle, temp = tempfile.mkstemp(dir=directory, prefix=".intake-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(payload)
        os.replace(temp, path)
    except BaseException as exc:
        leftover = ""
        try:
            os.unlink(temp)
        except OSError:
            leftover = " (the temporary file %s could not be removed: delete it, it holds your data)" % temp
        if isinstance(exc, OSError):
            raise IntakeError("cannot write %s: %s%s" % (path, exc.strerror or "error", leftover)) from None
        raise


# ---------------------------------------------------------------- sources

def read_file(path):
    try:
        with open(path, "rb") as fh:
            data = fh.read(odoo_rpc.MAX_RESPONSE + 1)
        if len(data) > odoo_rpc.MAX_RESPONSE:
            raise IntakeError("%s is larger than %d MB" % (path, odoo_rpc.MAX_RESPONSE // (1024 * 1024)))
        return data
    except OSError as exc:
        raise IntakeError("cannot read %s: %s" % (path, exc.strerror or "error")) from None


def read_snapshot(client, document_id):
    """Bytes of the snapshot of the spreadsheet document `document_id`, or None when it has none yet."""
    rows = client.execute("documents.document", "read", [[document_id], ["name", "handler", "spreadsheet_snapshot"]])
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise IntakeError("document %s was not found" % document_id)
    row = rows[0]
    if row.get("handler") != "spreadsheet":
        raise IntakeError("document %s is not a spreadsheet: open the Excel as a spreadsheet first" % document_id)
    packed = row.get("spreadsheet_snapshot")
    if not packed:
        return None
    try:
        return base64.b64decode(packed, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise IntakeError("the snapshot of document %s is not base64" % document_id) from None


def check_fresh_clone(client, clone_id):
    """A new clone is a spreadsheet document without a snapshot; anything else is not ours to touch or delete."""
    rows = client.execute("documents.document", "read", [[clone_id], ["handler", "spreadsheet_snapshot"]])
    row = rows[0] if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else {}
    if row.get("handler") != "spreadsheet" or row.get("spreadsheet_snapshot"):
        raise IntakeError("document %d is not a fresh clone: left untouched" % clone_id)


def clone_id_of(result, source_id):
    """The id of the clone: a positive integer that is not the uploaded xlsx itself (that one is never deleted)."""
    if not isinstance(result, int) or isinstance(result, bool) or result <= 0 or result == source_id:
        raise IntakeError("the server did not return the id of a new clone")
    return result


def wait_for_snapshot(client, document_id, wait_seconds, poll_interval, sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + wait_seconds
    failures = 0
    while True:
        try:
            data = read_snapshot(client, document_id)
        except odoo_rpc.RpcError:
            failures += 1
            if failures > POLL_FAILURES:               # one transient error must not abort the user's wait
                raise
            data = None
        else:
            failures = 0
        if data:
            return data
        if clock() >= deadline:
            raise IntakeError("no snapshot after %d seconds: %s" % (wait_seconds, SNAPSHOT_HELP))
        sleep(poll_interval)


def intake_from_xlsx_document(client, xlsx_id, keep, wait_seconds, poll_interval, log, sleep=time.sleep,
                              clock=time.monotonic):
    """Clone the uploaded xlsx, wait for the user's snapshot, return its bytes. The clone is deleted in `finally`."""
    rows = client.execute("documents.document", "read", [[xlsx_id], ["name", "handler", "mimetype"]])
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise IntakeError("document %s was not found" % xlsx_id)
    if rows[0].get("handler") == "spreadsheet" or "spreadsheetml" not in str(rows[0].get("mimetype")):
        raise IntakeError("document %s is not an uploaded xlsx file" % xlsx_id)
    try:
        clone = clone_id_of(client.execute("documents.document", "clone_xlsx_into_spreadsheet", [[xlsx_id]]), xlsx_id)
        check_fresh_clone(client, clone)
    except BaseException:
        log("note: if the server did create a clone it stays in Documents (named after the xlsx): delete it there")
        raise
    try:
        url = client.config.url.rstrip("/") + "/web?debug=1#action=action_open_spreadsheet&spreadsheet_id=%d" % clone
        log("Open this address in the browser, then use the menu File > Snapshot (no edit needed):")
        log(url)
        log("Waiting for the snapshot (up to %d seconds) ..." % wait_seconds)
        return wait_for_snapshot(client, clone, wait_seconds, poll_interval, sleep=sleep, clock=clock)
    finally:
        if keep:
            log("kept the clone: spreadsheet document %d (re-use it with --document-id %d)" % (clone, clone))
        else:
            log("deleting the clone (spreadsheet document %d) ..." % clone)
            try:
                deleted = client.execute("documents.document", "unlink", [[clone]])
            except odoo_rpc.RpcError as exc:
                deleted, reason = False, str(exc)
            else:
                reason = "the server refused"
            if deleted:
                log("deleted the clone (spreadsheet document %d)" % clone)
            else:
                log("WARNING: could not delete the clone, spreadsheet document %d (%s): delete it in Documents"
                    % (clone, reason))


# ---------------------------------------------------------------- command line

def build_parser():
    parser = argparse.ArgumentParser(prog="intake_xlsx.py", description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--document-id", type=int, help="spreadsheet document that already holds a snapshot")
    source.add_argument("--xlsx-document", type=int, help="uploaded xlsx document: clone, wait for File > Snapshot")
    source.add_argument("--file", help="file saved with File > Download as JSON")
    parser.add_argument("--sheet", help="name of the sheet (without it, only the sheet inventory is printed)")
    parser.add_argument("--area", help="A1:<last cell> of the design area, e.g. A1:Q31")
    parser.add_argument("--out", help="where to write the baseline JSON")
    parser.add_argument("--force", action="store_true", help="replace --out when it exists")
    parser.add_argument("--keep", action="store_true", help="keep the clone made by --xlsx-document")
    parser.add_argument("--wait-seconds", type=int, default=600)
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--config", help="path of the config file (default: see odoo_rpc.py config-path)")
    parser.add_argument("--confirm-instance", help="the full host name typed by the user (non-local hosts)")
    return parser


def run(args, out, err, client_factory=odoo_rpc.Client, sleep=time.sleep, clock=time.monotonic):
    def log(text):
        print(text, file=err, flush=True)

    if bool(args.sheet) != bool(args.area):
        raise IntakeError("--sheet and --area go together (or leave both out to list the sheets)")
    if args.area and not args.out:
        raise IntakeError("--out is required with --sheet / --area")
    if any(value is not None and value <= 0 for value in (args.document_id, args.xlsx_document)):
        raise IntakeError("a document id is a positive number")
    if not 1 <= args.wait_seconds <= MAX_WAIT_SECONDS:
        raise IntakeError("--wait-seconds must be positive (at most %d)" % MAX_WAIT_SECONDS)
    if not math.isfinite(args.poll_interval) or args.poll_interval < MIN_POLL_INTERVAL:
        raise IntakeError("--poll-interval must be positive (a finite number, at least %g)" % MIN_POLL_INTERVAL)
    if args.area:
        parse_area(args.area)                      # fail before any clone is made
    route = "download" if args.file else "snapshot"
    if args.file:
        payload = read_file(args.file)
    else:
        client = client_factory(odoo_rpc.load_config(path=args.config), confirm_instance=args.confirm_instance)
        client.login()
        if args.document_id is not None:
            payload = read_snapshot(client, args.document_id)
            if not payload:
                raise IntakeError("document %d has no snapshot yet: %s" % (args.document_id, SNAPSHOT_HELP))
        else:
            payload = intake_from_xlsx_document(client, args.xlsx_document, args.keep, args.wait_seconds,
                                                args.poll_interval, log, sleep=sleep, clock=clock)
    raw = parse_snapshot(payload)
    if not args.area:
        json.dump({"route": route, "sheets": inventory(raw)}, out, ensure_ascii=False, indent=1)
        out.write("\n")
        return 0
    try:
        baseline, report = crop(raw, args.sheet, args.area)
    except RecursionError:
        raise IntakeError("the sheet is nested too deeply") from None
    data = dump_bytes(baseline)
    write_baseline(args.out, data, force=args.force, source_path=args.file)
    report.update({"route": route, "out": args.out, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                   "revision_id": BASELINE_REVISION})
    json.dump(report, out, ensure_ascii=False, indent=1)
    out.write("\n")
    return 0


def main(argv=None, out=None, err=None, **kwargs):
    out, err = out or sys.stdout, err or sys.stderr
    for stream in (out, err):                      # a Windows pipe uses the ANSI code page: sheet names are not ASCII
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (ValueError, OSError):
                pass
    args = build_parser().parse_args(argv)
    try:
        return run(args, out, err, **kwargs)
    except (odoo_rpc.ConfigError, odoo_rpc.GuardError, IntakeError) as exc:
        print("error: %s" % exc, file=err)
        return 2
    except odoo_rpc.RpcError as exc:
        print("error: %s" % exc, file=err)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=err)
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort: one line, no traceback, never the exception text
        print("error: unexpected %s" % type(exc).__name__, file=err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
