#!/usr/bin/env python3
"""Scan: read the baseline of the QA/QC's Excel and PROPOSE which rows can be bound to which catalog source.

    python scripts/scan_excel.py --baseline baseline.json [--out kpi_spec.draft.json] [--force] [--aliases my_aliases.json]
                                 [--project NAME]

Offline (no network). It looks for the blocks of a sheet that carry typed numbers month by month:
  1. a header row: 12 consecutive cells that read as the months 1..12 in order (`26년 1월`, `1월`, `Tháng 1`,
     `Month 1`, `Jan` ...);
  2. the block rows under it (until the next header row or two empty rows): a row with a text label to the left of
     the first month column (never a cell of a vertical merge: that is a year label) and at least one typed number
     in the 12 month cells;
  3. the block title: the first non-empty row above the header row, when it holds ONE text cell (`출하 수량`).
A row is proposed only when the `aliases` of ONE catalog source resolve EVERY selector of that source from the
row's label (any line of it) and the block title (an alias is compared after NFKC, whitespace removed, case folded).
The source that resolves more selectors wins; a tie or two values for one selector refuses the row. The draft spec has
the bindings (`cells` = the 12 month cells, `columns: "months"`), the label cell of every row in `labels`, the
baseline's `baseline_sha256`, no `keep_typed`. Then scan runs the binder on its own draft and prints the result.
It never guesses: what it cannot resolve is listed so that the user decides (or a requirement text is written,
hard rule 1 of the skill). Warnings go to the screen only, never into the spec.

`--aliases` is an optional JSON {source: {selector: {value: [text, ...]}}} in the user's folder, merged into the
catalog's aliases and checked the same way (it survives a plugin update). Printed labels are cleaned of control
characters and cut at 40 characters. Standard library only.
"""
import argparse
import hashlib
import os
import re
import sys

import bind_kpi
import intake_xlsx

LABEL_LIMIT = 40
MONTH_ABBREVIATIONS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
MONTH_FORMS = (re.compile(r"(?:(\d{2,4})년)?(\d{1,2})월"),            # whitespace is removed before matching
               re.compile(r"()tháng(\d{1,2})"),
               re.compile(r"()month(\d{1,2})"))
ENGLISH_MONTH = re.compile(r"(%s)[a-z]*(\d{2,4})?" % "|".join(MONTH_ABBREVIATIONS))
YEAR_LABEL = re.compile(r"(\d{2,4})년")
DEFAULT_NAME = "KPI draft"
MAX_BLANK_ROWS = 2


class ScanError(Exception):
    """A problem with the input (message is safe to print)."""


def _clean(text, limit=LABEL_LIMIT):
    """Printable text only: control characters, line / paragraph separators (U+2028, U+2029), bidi overrides and
    zero-width characters of an untrusted label become spaces (the same rule as `intake_xlsx._short`), then the cut."""
    text = "".join(ch if ch.isprintable() else " " for ch in bind_kpi.CONTROL.sub(" ", str(text))).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _year(digits):
    if not digits:
        return None
    value = int(digits)
    return 2000 + value if value < 100 else value


def parse_month(text):
    """(year or None, month) of a month header text, else None."""
    if not isinstance(text, str):
        return None
    norm = bind_kpi.normalize_alias(text)
    for form in MONTH_FORMS:
        match = form.fullmatch(norm)
        if match and 1 <= int(match.group(2)) <= 12:
            return _year(match.group(1)), int(match.group(2))
    match = ENGLISH_MONTH.fullmatch(norm)
    if match:
        return _year(match.group(2)), MONTH_ABBREVIATIONS.index(match.group(1)) + 1
    return None


# ---------------------------------------------------------------- the sheet

class Sheet:
    """The cells (row, col) -> content of the baseline's one sheet and its merges."""

    def __init__(self, baseline):
        try:
            if not isinstance(baseline["sheets"], list) or len(baseline["sheets"]) != 1:
                raise ValueError("sheets")
            sheet = baseline["sheets"][0]
            self.cells = {intake_xlsx.split_ref(ref): cell.get("content") for ref, cell in sheet["cells"].items()
                          if isinstance(cell, dict) and cell.get("content") not in (None, "")}
            self.merges = [bind_kpi._block(text, "a merge") for text in sheet.get("merges", [])]
            self.rows, self.cols = int(sheet["rowNumber"]), int(sheet["colNumber"])
        except (KeyError, TypeError, ValueError, AttributeError, intake_xlsx.IntakeError, bind_kpi.BindError):
            raise ScanError("the baseline must hold exactly one well-formed sheet (run intake_xlsx.py with --sheet "
                            "and --area)") from None
        if self.rows < 0 or self.cols < 0 or self.rows * self.cols > intake_xlsx.MAX_AREA_CELLS:
            raise ScanError("the baseline area is larger than %d cells (the intake never writes such a file)"
                            % intake_xlsx.MAX_AREA_CELLS)

    def text(self, row, col):
        """The text of a cell (a label, not a number, not a formula) or None."""
        value = self.cells.get((row, col))
        if (isinstance(value, str) and value.strip() and not value.startswith("=")
                and not bind_kpi._typed_number(value)):
            return value
        return None

    def merge_at(self, row, col):
        return next((m for m in self.merges if m[0] <= row <= m[2] and m[1] <= col <= m[3]), None)

    def in_vertical_merge(self, row, col):
        merge = self.merge_at(row, col)
        return merge is not None and merge[2] > merge[0]

    def typed(self, row, col):
        return bind_kpi._typed_number(self.cells.get((row, col)))

    def row_is_empty(self, row, last_col):
        return not any((row, col) in self.cells for col in range(1, last_col + 1))


def find_headers(sheet):
    """[(row, first month column, header year or None)] for every row that holds the 12 months in order."""
    headers = []
    for row in range(1, sheet.rows + 1):
        months = {col: parse_month(sheet.cells.get((row, col))) for col in range(1, sheet.cols + 1)}
        for start in range(1, sheet.cols - 10):
            run = [months.get(start + offset) for offset in range(12)]
            if all(item is not None and item[1] == offset + 1 for offset, item in enumerate(run)):
                headers.append((row, start, run[0][0]))
                break
    return headers


def block_title(sheet, header_row):
    """The text of the first non-empty row above the header row when it holds ONE text cell, else None."""
    for row in range(header_row - 1, 0, -1):
        filled = [(r, c) for (r, c) in sheet.cells if r == row]
        if filled:
            return sheet.text(*filled[0]) if len(filled) == 1 else None
    return None


def row_label(sheet, row, first_month_col):
    """(column, text) of the nearest text cell left of the month columns that is not part of a vertical merge."""
    for col in range(first_month_col - 1, 0, -1):
        text = sheet.text(row, col)
        if text is not None and not sheet.in_vertical_merge(row, col):
            return col, text
    return None


def block_year_label(sheet, row, label_col):
    """The year written left of the label column for the block that starts at `row` (the top-left cell of the merge
    that covers it, else the cell itself), as a year number or None."""
    col = label_col - 1
    if col < 1:
        return None
    merge = sheet.merge_at(row, col)
    text = sheet.text(*(merge[:2] if merge else (row, col)))
    match = YEAR_LABEL.search(bind_kpi.normalize_alias(text)) if text else None
    return _year(match.group(1)) if match else None


# ---------------------------------------------------------------- aliases

def merged_catalog(catalog, overlay):
    """The catalog with the user's alias overlay merged in (and the merged result checked like the shipped one)."""
    if overlay is None:
        return catalog
    if not isinstance(overlay, dict):
        raise ScanError("the --aliases file must be an object {source: {selector: {value: [text]}}}")
    for name, aliases in overlay.items():
        source = catalog["sources"].get(name) if isinstance(name, str) else None
        if not isinstance(source, dict) or source.get("kind") == "pivot":
            raise ScanError("the --aliases file names a source that is not a list source of the catalog: %r"
                            % str(name)[:40])
        merged = {selector: {value: list(texts) for value, texts in by_value.items()}
                  for selector, by_value in source.get("aliases", {}).items()}
        if not isinstance(aliases, dict) or not all(isinstance(v, dict) for v in aliases.values()):
            raise ScanError("the --aliases file must be an object {source: {selector: {value: [text]}}}")
        for selector, by_value in aliases.items():
            for value, texts in by_value.items():
                merged.setdefault(selector, {}).setdefault(value, [])
                merged[selector][value] = merged[selector][value] + (list(texts) if isinstance(texts, list) else [texts])
        try:
            bind_kpi.check_aliases(name, source, merged)
        except bind_kpi.BindError as exc:
            raise ScanError("the --aliases file is refused: %s" % exc) from None
        source["aliases"] = merged
    return catalog


def resolve(lines, title, catalog):
    """(source name, where) or (None, reason) for a row with the label `lines` under the block `title`. The label
    resolves every selector; the block title resolves ONLY the selector `kind` (a title that names a group must not
    turn an unrelated label into a match)."""
    label_texts = [bind_kpi.normalize_alias(text) for text in lines]
    title_texts = [bind_kpi.normalize_alias(title)] if title else []
    candidates = []
    for name, source in sorted(catalog["sources"].items()):
        aliases = source.get("aliases")
        if (source.get("kind") == "pivot" or not aliases or not source["selectors"]
                or "months" not in source.get("column_groups", {})):
            continue
        where, complete = {}, True
        for selector in sorted(source["selectors"]):
            texts = label_texts + (title_texts if selector == "kind" else [])
            matched = {value for value, items in aliases.get(selector, {}).items()
                       if any(bind_kpi.normalize_alias(item) in texts for item in items)}
            if len(matched) > 1:
                return None, "its label matches two values of %s.%s" % (name, selector)
            if not matched:
                complete = False
                break
            where[selector] = next(iter(matched))
        if complete:
            candidates.append((len(where), name, where))
    if not candidates:
        return None, "no catalog source has an alias for it"
    best = max(count for count, _, _ in candidates)
    top = [item for item in candidates if item[0] == best]
    if len(top) > 1:
        return None, "two sources fit it equally (%s)" % ", ".join(name for _, name, _ in top)
    return (top[0][1], top[0][2]), ""


# ---------------------------------------------------------------- the scan

def scan(baseline, catalog):
    """(draft spec or None, report lines, warnings). Pure: no I/O."""
    sheet = Sheet(baseline)
    headers = find_headers(sheet)
    lines, warnings, bindings, labels, block_years = [], [], [], {}, []
    if not headers:
        lines.append("no header row with the 12 months in order was found: nothing can be proposed")
        return None, lines, warnings
    for index, (header_row, first_col, header_year) in enumerate(headers):
        last_col = first_col + 11
        end_row = headers[index + 1][0] - 1 if index + 1 < len(headers) else sheet.rows
        title = block_title(sheet, header_row)
        lines.append("header row %d: months 1..12 in columns %s..%s; block title: %s"
                     % (header_row, intake_xlsx.col_letters(first_col), intake_xlsx.col_letters(last_col),
                        '"%s"' % _clean(title) if title else "none"))
        blank, block_rows, title_warned = 0, [], False
        for row in range(header_row + 1, end_row + 1):
            if sheet.row_is_empty(row, last_col):
                blank += 1
                if blank >= MAX_BLANK_ROWS:
                    break
                continue
            blank = 0
            if not any(sheet.typed(row, col) for col in range(first_col, last_col + 1)):
                continue                                   # a totals / formula / text row: nothing typed to replace
            found = row_label(sheet, row, first_col)
            if found is None:
                lines.append("row %d has typed numbers but no label: ask the user" % row)
                continue
            label_col, label = found
            where_text = "row %d (%s)" % (row, '"%s"' % _clean(label))
            parts = [part.strip() for part in re.split(r"[\r\n]+", label) if part.strip()]
            if any(bind_kpi.CONTROL.search(part) for part in parts):
                lines.append("%s holds control characters: refused, ask the user" % where_text)
                continue
            resolved, reason = resolve(parts, title, catalog)
            if resolved is None:
                lines.append("%s has typed numbers but %s: ask the user, or write a requirement text (hard rule 1)"
                             % (where_text, reason))
                continue
            source_name, where = resolved
            if title and "kind" not in catalog["sources"][source_name]["selectors"] and not title_warned:
                title_warned = True
                warnings.append('WARNING row %d: the block title "%s" was not used (the source %s has no `kind`): the '
                                "source was chosen from the row label alone, check that this block really holds that "
                                "source's data" % (row, _clean(title), source_name))
            text_cells = [intake_xlsx.col_letters(col) + str(row) for col in range(first_col, last_col + 1)
                          if sheet.text(row, col) is not None]
            if text_cells:
                lines.append("%s: text cell %s inside the row: the binder would refuse it, ask the user"
                             % (where_text, text_cells[0]))
                continue
            matched_line = next((line for line in parts if _line_matches(line, source_name, where, catalog)), None)
            if matched_line is None:                       # cannot happen once resolve() only uses the label
                lines.append("%s has typed numbers but no line of its label matches an alias: ask the user"
                             % where_text)
                continue
            labels[bind_kpi.ref_of(row, label_col)] = matched_line
            bindings.append({"cells": "%s:%s" % (bind_kpi.ref_of(row, first_col), bind_kpi.ref_of(row, last_col)),
                             "source": source_name, "where": where, "columns": "months"})
            block_rows.append((row, label_col))
            lines.append("%s -> %s %s" % (where_text, source_name,
                                          "{%s}" % ", ".join("%s: %s" % item for item in sorted(where.items()))))
        if block_rows:
            year = block_year_label(sheet, block_rows[0][0], block_rows[0][1])
            reference = header_year or (block_years[0] if block_years else None)
            if year and reference and year != reference:
                warnings.append("WARNING rows %d-%d: the year label says %d but %s say %d: confirm which year this "
                                "block shows (a list source reads the Year filter's year, and only some sources have "
                                "a previous-year chain)"
                                % (block_rows[0][0], block_rows[-1][0], year,
                                   "the month headers" if header_year else "the first block's year label", reference))
            if year:
                block_years.append(year)
    if not bindings:
        lines.append("no row could be proposed")
        return None, lines, warnings
    return {"spec_version": bind_kpi.SPEC_VERSION, "name": DEFAULT_NAME, "labels": labels,
            "bindings": bindings}, lines, warnings


def _line_matches(line, source_name, where, catalog):
    aliases = catalog["sources"][source_name]["aliases"]
    norm = bind_kpi.normalize_alias(line)
    return any(norm == bind_kpi.normalize_alias(item) for selector, value in where.items()
               for item in aliases.get(selector, {}).get(value, []))


# ---------------------------------------------------------------- command line

def build_parser():
    parser = argparse.ArgumentParser(prog="scan_excel.py", description=__doc__.split("\n\n")[0], allow_abbrev=False)
    parser.add_argument("--baseline", required=True, help="baseline JSON written by intake_xlsx.py")
    parser.add_argument("--out", help="where to write the draft spec (default: kpi_spec.draft.json next to the baseline)")
    parser.add_argument("--force", action="store_true", help="replace --out when it exists")
    parser.add_argument("--aliases", help="a JSON file of your own label aliases {source: {selector: {value: [text]}}}")
    parser.add_argument("--project", help="project whose shipped source catalog to use (default: the only one installed)")
    return parser


def run(args, out):
    baseline_bytes, baseline = bind_kpi._read_json(args.baseline, "the baseline")
    overlay = bind_kpi._read_json(args.aliases, "the --aliases file")[1] if args.aliases else None
    path = args.out or os.path.join(os.path.dirname(os.path.abspath(args.baseline)), "kpi_spec.draft.json")
    if path.lower().endswith(".spec.json"):            # a spec is the user's work: never a place for a draft
        raise ScanError("--out must not be a .spec.json file (that is the name of a spec): use the default draft name")
    if os.path.exists(path):                           # before the scan prints anything
        for other in (args.baseline, args.aliases):
            if other and os.path.exists(other) and os.path.samefile(path, other):
                raise ScanError("--out is the input file (%s): refusing to overwrite it" % os.path.basename(other))
        if not args.force:
            raise ScanError("%s exists already: pass --force to replace it" % path)
    catalog = merged_catalog(bind_kpi.load_catalog(project=args.project), overlay)
    draft, lines, warnings = scan(baseline, catalog)
    for line in lines + warnings:
        print(line, file=out)
    if draft is None:
        return 0
    draft["baseline_sha256"] = hashlib.sha256(baseline_bytes).hexdigest()
    try:
        _, report = bind_kpi.bind(baseline, draft, catalog, baseline_bytes)
        print("bind dry run: ok (%d cells would be bound)" % report["bound_cells"], file=out)
    except bind_kpi.BindError as exc:
        print("bind dry run: refused: %s" % _clean(exc, 300), file=out)
    intake_xlsx.write_baseline(path, intake_xlsx.dump_bytes(draft), force=args.force, source_path=args.baseline)
    print("draft spec written: %s (a proposal: the user confirms it, then bind_kpi.py)" % path, file=out)
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
    except (ScanError, bind_kpi.BindError, intake_xlsx.IntakeError) as exc:
        print("error: %s" % _clean(exc, 300), file=err)
        return 2
    except RecursionError:
        print("error: the input is nested too deeply", file=err)
        return 2
    except Exception as exc:  # noqa: BLE001 - last resort: one line, no traceback, never the exception text
        print("error: unexpected %s" % type(exc).__name__, file=err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
