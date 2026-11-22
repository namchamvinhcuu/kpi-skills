"""AC2 list part (the generated dashboard evaluated by `validate_formulas.SheetEvaluator`), AC3 determinism of the
bytes, and the time bounds of the binder. Inputs are built in Python (bind_fixtures.py): invented numbers and text.

The oracle gets its lists from a tiny data table keyed by (model, domain): the row a list returns is the record of the
filter year (or of the year before it for a list matched through `next_year`), carrying exactly the list's `columns`.
A column the binder forgot to put into a list therefore fails with `NotCatchable`, like the real client's empty field.
"""
import copy
import json
import re
import time
import unittest

import bind_fixtures as bf
from helpers import REPO  # noqa: F401

import bind_kpi
import intake_xlsx
import validate_formulas


def lists_for(document, data, year):
    """{list id: rows} for the filter year `year`; `data[(model, json domain)] = {record year: {column: value}}`."""
    lists = {}
    for list_id, listing in document["lists"].items():
        chain = listing["fieldMatching"]["kpi-year-filter"]["chain"]
        wanted = year - 1 if chain == "next_year" else year
        record = data.get((listing["model"], json.dumps(listing["domain"])), {}).get(wanted)
        if record is None:
            lists[int(list_id)] = []
            continue
        selectors = {field: value for field, _, value in listing["domain"]}
        lists[int(list_id)] = [{column: selectors[column] if column in selectors else record.get(column, 0)
                                for column in listing["columns"]}]
    return lists


def evaluate(document, data, year=2026):
    return validate_formulas.SheetEvaluator(document, lists=lists_for(document, data, year)).evaluate_all()


def key(model, **where):
    return (model, json.dumps([[field, "=", where[field]] for field in sorted(where)]))


SMALL_DATA = {
    key("t.plain", code="A"): {2026: {"m1": 100, "m2": 0, "m3": 5.5}},
    key("t.plain", code="B"): {2026: {"m1": 7, "m2": 8, "m3": 9}, 2025: {"m1": 70, "m2": 80, "m3": 90}},
}


def small_oracle_spec():
    return bf.make_spec([bf.plain("D5:F5", "A", zero="dash"),                        # list 1
                         bf.plain("D6:F6", "B", year="previous"),                    # list 2
                         bf.plain("D8:F8", "B", zero="dash"),                        # list 3
                         bf.plain("D9:F9", "A", year="previous", zero="dash"),       # list 4 (no 2025 record of A)
                         bf.plain("D10:F10", "A", year="previous")])                 # list 4


def small_oracle_baseline():
    return bf.make_baseline(cells=bf.default_cells(3, (5, 6, 8, 9, 10)))


class SmallOracleTests(unittest.TestCase):
    def setUp(self):
        self.document, _ = bf.bind(small_oracle_spec(), small_oracle_baseline())
        self.values = evaluate(self.document, SMALL_DATA, 2026)

    def row(self, number, columns="DEF"):
        return [self.values["%s%d" % (c, number)] for c in columns]

    def test_a_normal_month_shows_the_number_and_a_zero_shows_a_dash(self):
        self.assertEqual(self.row(5), [100, "-", 5.5])

    def test_the_previous_year_list_returns_the_previous_years_row(self):
        self.assertEqual(self.row(6), [70, 80, 90])

    def test_a_formula_of_the_baseline_reads_the_bound_number_cells(self):
        self.assertEqual(self.row(7), [140, 160, 180])                 # D7 = D6*2 of the baseline
        self.assertEqual(self.values["P7"], 240)                       # P7 = SUM(D6:F6)

    def test_a_dash_row_with_values(self):
        self.assertEqual(self.row(8), [7, 8, 9])

    def test_a_missing_row_shows_a_dash_or_a_zero_and_never_an_error(self):
        self.assertEqual(self.row(9), ["-", "-", "-"])                 # dash cell over an empty list
        self.assertEqual(self.row(10), [0, 0, 0])                      # number cell over an empty list
        for value in self.row(10):
            self.assertIsInstance(value, float)

    def test_the_helper_rows_hold_the_numbers_and_sit_below_the_area(self):
        self.assertEqual(self.row(12), [100, 0, 5.5])                  # dash rows 5, 8, 9 -> helper rows 12, 13, 14
        self.assertEqual(self.row(13), [7, 8, 9])
        self.assertEqual(self.row(14), [0, 0, 0])
        self.assertEqual(self.document["sheets"][0]["rowNumber"], 14)

    def test_the_labels_and_the_headers_are_untouched(self):
        self.assertEqual(self.values["A5"], "Row label one")
        self.assertEqual(self.values["D4"], "M1")
        self.assertEqual(self.values["A1"], "Report title")

    def test_the_other_filter_year_moves_every_list(self):
        values = evaluate(self.document, SMALL_DATA, 2027)
        self.assertEqual([values[c + "6"] for c in "DEF"], [7, 8, 9])         # previous of 2027 is 2026
        self.assertEqual([values[c + "5"] for c in "DEF"], ["-", "-", "-"])   # no 2027 row of A
        self.assertEqual([values[c + "9"] for c in "DEF"], [100, "-", 5.5])   # previous of 2027 is 2026

    def test_every_cell_evaluates_without_an_error(self):
        self.assertEqual(set(self.values), set(self.document["sheets"][0]["cells"]))

    def test_each_list_exposes_exactly_the_columns_its_cells_read(self):
        for list_id, listing in self.document["lists"].items():
            read = set()
            for cell in self.document["sheets"][0]["cells"].values():
                for found in re.finditer(r'ODOO\.LIST\((\d+),1,"([a-z0-9_]+)"\)', cell.get("content", "")):
                    if found.group(1) == list_id:
                        read.add(found.group(2))
            self.assertEqual(set(listing["columns"]), read | {"code"})


class IndependentScanTests(unittest.TestCase):
    """The reference scanner of the fixtures is checked on its own, so the "no display cell is read" tests are not
    vacuous."""

    def test_scanner(self):
        scan = bf.reference_scan
        self.assertEqual(scan("=D5"), {"D5"})
        self.assertEqual(scan("=$d$5+e6"), {"D5", "E6"})
        self.assertEqual(scan("=SUM(D5:E6)"), {"D5", "E5", "D6", "E6"})
        self.assertEqual(scan("=SUM(E6:D5)"), {"D5", "E5", "D6", "E6"})
        self.assertEqual(scan('=IF(1,"D5","x D6")'), set())
        self.assertEqual(scan('=ODOO.LIST(1,1,"m1")'), set())
        self.assertEqual(scan("=LOG10(100)+A1"), {"A1"})
        self.assertEqual(scan("=IFERROR(ODOO.LIST(12,1,\"month_3\")+0,0)"), set())
        self.assertEqual(scan('=IF(D12=0,"-",D12)'), {"D12"})


class NoDisplayCellReadTests(unittest.TestCase):
    """Spec invariants: no formula reads a display cell, no typed number is left in a bound set, user text only
    through validated fields."""

    def check(self, document, display, bound):
        cells = document["sheets"][0]["cells"]
        for ref, cell in cells.items():
            content = cell.get("content")
            if isinstance(content, str) and content.startswith("="):
                self.assertFalse(bf.reference_scan(content) & display, "%s reads a display cell: %s" % (ref, content))
        for ref in display:
            self.assertEqual(len(bf.reference_scan(cells[ref]["content"])), 1)
            (helper,) = bf.reference_scan(cells[ref]["content"])
            self.assertNotIn(helper, display)
            self.assertTrue(cells[helper]["content"].startswith("=IFERROR(ODOO.LIST("))
        for ref in bound:
            self.assertTrue(cells[ref]["content"].startswith("="), ref)
        for ref, cell in cells.items():
            self.assertIsNone(re.fullmatch(r"-?[0-9]+(\.[0-9]+)?", str(cell.get("content", ""))), ref)

    def test_the_small_dashboard(self):
        document, _ = bf.bind(small_oracle_spec(), small_oracle_baseline())
        display = {"%s%d" % (c, r) for c in "DEF" for r in (5, 8, 9)}
        bound = display | {"%s%d" % (c, r) for c in "DEF" for r in (6, 10)}
        self.check(document, display, bound)

    def test_twelve_months_over_the_real_catalog(self):
        spec = bf.make_spec([{"cells": "D5:O5", "source": "production_group", "where": {"group_code": "DK_REF"},
                              "columns": "months", "zero": "dash"},
                             {"cells": "D6:O6", "source": "production_group", "where": {"group_code": "DK_REF"},
                              "columns": "months", "year": "previous"}])
        document, report = bf.bind(spec, bf.make_baseline(), bf.real_catalog())
        self.assertEqual((report["bound_cells"], report["dash_cells"], report["lists"]), (24, 12, 2))
        display = {"%s5" % c for c in bf.MONTH_LETTERS}
        self.check(document, display, display | {"%s6" % c for c in bf.MONTH_LETTERS})

    def test_a_baseline_formula_that_would_read_a_display_cell_never_reaches_the_output(self):
        spec = bf.make_spec([bf.plain("D5:F5", "A", zero="dash"), bf.plain("D6:F6", "B", zero="dash")])
        with self.assertRaises(bind_kpi.BindError):
            bf.bind(spec)

    def test_user_text_never_enters_a_formula_or_a_domain(self):
        spec = bf.make_spec(name="ZXQ-NAME", year_filter_label="ZXQ-LABEL", labels={"A5": "Row label"},
                            keep_typed=["H8"], intake={"route": "ZXQ-ROUTE"})
        baseline = bf.edit(bf.small_baseline(), {"H8": {"content": "7"}})
        document, _ = bf.bind(spec, baseline)
        formulas = " ".join(c["content"] for c in document["sheets"][0]["cells"].values()
                            if str(c.get("content", "")).startswith("="))
        domains = json.dumps([listing["domain"] for listing in document["lists"].values()])
        for token in ("ZXQ", "Row label", "H8"):
            self.assertNotIn(token, formulas)
            self.assertNotIn(token, domains)
        self.assertEqual(json.dumps(document).count("ZXQ"), 1)                # the filter label only


class RealCatalogOracleTests(unittest.TestCase):
    def test_a_year_of_months_with_a_zero_month_and_a_second_source(self):
        spec = bf.make_spec([
            {"cells": "D5:O5", "source": "production_group", "where": {"group_code": "DK_REF"}, "columns": "months",
             "zero": "dash"},
            {"cells": "D6:O6", "source": "production_group", "where": {"group_code": "DK_REF"}, "columns": "months",
             "year": "previous"}])
        document, _ = bf.bind(spec, bf.make_baseline(), bf.real_catalog())
        model = "dksk.kpi.production_group"
        current = {"month_%d" % m: (0 if m == 3 else m * 10) for m in range(1, 13)}
        previous = {"month_%d" % m: m * 100 for m in range(1, 13)}
        data = {key(model, group_code="DK_REF"): {2026: current, 2025: previous}}
        values = evaluate(document, data, 2026)
        row5 = [values["%s5" % c] for c in bf.MONTH_LETTERS]
        self.assertEqual(row5, [10, 20, "-", 40, 50, 60, 70, 80, 90, 100, 110, 120])
        self.assertEqual([values["%s6" % c] for c in bf.MONTH_LETTERS], [m * 100 for m in range(1, 13)])
        self.assertEqual([values["%s7" % c] for c in bf.MONTH_LETTERS], [m * 200 for m in range(1, 13)])
        self.assertEqual(values["P7"], sum(m * 100 for m in range(1, 13)))
        self.assertEqual([values["%s12" % c] for c in bf.MONTH_LETTERS], [0 if m == 3 else m * 10 for m in range(1, 13)])

    def test_two_selector_source_and_a_manual_input_source(self):
        spec = bf.make_spec([
            {"cells": "D5:O5", "source": "report_group", "where": {"kind": "shipped", "group_code": "SK_WM"},
             "columns": "months"},
            {"cells": "D6:O6", "source": "production_year", "columns": "productivity", "year": "previous"}])
        document, _ = bf.bind(spec, bf.make_baseline(), bf.real_catalog())
        data = {key("dksk.kpi.report_group", group_code="SK_WM", kind="shipped"):
                {2026: {"month_%d" % m: m for m in range(1, 13)}},
                key("dksk.kpi.production_year"): {2025: {"productivity_%d" % m: m / 2 for m in range(1, 13)}}}
        values = evaluate(document, data, 2026)
        self.assertEqual([values["%s5" % c] for c in bf.MONTH_LETTERS], list(range(1, 13)))
        self.assertEqual([values["%s6" % c] for c in bf.MONTH_LETTERS], [m / 2 for m in range(1, 13)])


class AudienceOracleTests(unittest.TestCase):
    """H9: a source every default user may read tolerates a missing row; any other source never shows a silent 0."""

    def spec(self, source, zero="number"):
        binding = {"cells": "D5", "source": source, "column": "m1", "zero": zero}
        if source == "plain":
            binding["where"] = {"code": "A"}
        return bf.make_spec([binding])

    def values(self, source, data, zero="number"):
        document, _ = bf.bind(self.spec(source, zero), bf.make_baseline(cells={}))
        return evaluate(document, data, 2026)

    def test_a_default_user_source_reads_a_missing_row_as_zero(self):
        values = self.values("plain", {})
        self.assertEqual(values["D5"], 0)
        self.assertIsInstance(values["D5"], float)
        self.assertEqual(self.values("plain", {}, "dash")["D5"], "-")

    def test_a_restricted_source_shows_the_raw_empty_read_never_a_zero(self):
        values = self.values("secret", {})
        self.assertEqual(values["D5"], "")
        self.assertNotEqual(values["D5"], 0)
        dash = self.values("secret", {}, "dash")
        self.assertEqual(dash["D12"], "")
        self.assertEqual(dash["D5"], "")                                    # not "-": the gap is not hidden

    def test_the_client_returns_an_empty_text_for_a_float_zero(self):
        """`record[field] || ""` in the client's getListCellValue turns a real 0 into "" (skill note on fixed rows):
        a default-user source still shows 0 / the dash, never an error."""
        data = {key("t.plain", code="A"): {2026: {"m1": ""}}}
        self.assertEqual(self.values("plain", data)["D5"], 0)
        self.assertEqual(self.values("plain", data, "dash")["D5"], "-")
        self.assertEqual(self.values("plain", data, "dash")["D12"], 0)

    def test_both_read_the_value_when_the_row_exists(self):
        self.assertEqual(self.values("plain", {key("t.plain", code="A"): {2026: {"m1": 12.5}}})["D5"], 12.5)
        self.assertEqual(self.values("secret", {key("t.secret"): {2026: {"m1": 12.5}}})["D5"], 12.5)
        self.assertEqual(self.values("secret", {key("t.secret"): {2026: {"m1": 0}}}, "dash")["D5"], "-")


# ---------------------------------------------------------------- AC3 determinism

class DeterminismTests(unittest.TestCase):
    def build(self, spec=bf.DEFAULT, baseline=bf.DEFAULT, catalog=bf.DEFAULT):
        document, _ = bf.bind(spec, baseline, catalog)
        return bf.dumps(document)

    def test_two_runs_are_byte_identical(self):
        first = self.build(bf.make_spec(), bf.small_baseline())
        second = self.build(bf.make_spec(), bf.small_baseline())
        self.assertEqual(first, second)
        self.assertEqual(self.build(structure_spec(), bf.make_baseline(cells={})),
                         self.build(structure_spec(), bf.make_baseline(cells={})))

    def test_the_key_order_inside_binding_objects_does_not_matter(self):
        spec = structure_spec()
        shuffled = copy.deepcopy(spec)
        shuffled["bindings"] = [dict(reversed(list(b.items()))) for b in shuffled["bindings"]]
        for binding in shuffled["bindings"]:
            if "where" in binding:
                binding["where"] = dict(reversed(list(binding["where"].items())))
        shuffled = dict(reversed(list(shuffled.items())))
        self.assertNotEqual(list(spec["bindings"][4]), list(shuffled["bindings"][4]))
        self.assertNotEqual(list(spec["bindings"][4]["where"]), list(shuffled["bindings"][4]["where"]))
        self.assertEqual(self.build(spec, bf.make_baseline(cells={})), self.build(shuffled, bf.make_baseline(cells={})))

    def test_the_order_of_the_baseline_cells_does_not_matter(self):
        baseline = bf.small_baseline()
        scrambled = copy.deepcopy(baseline)
        sheet = scrambled["sheets"][0]
        sheet["cells"] = dict(sorted(sheet["cells"].items(), reverse=True))
        self.assertNotEqual(list(baseline["sheets"][0]["cells"]), list(sheet["cells"]))
        self.assertEqual(self.build(bf.make_spec(), baseline), self.build(bf.make_spec(), scrambled))

    def test_the_order_of_the_baseline_rows_does_not_matter(self):
        baseline = bf.small_baseline()
        scrambled = copy.deepcopy(baseline)
        scrambled["sheets"][0]["rows"] = dict(reversed(list(baseline["sheets"][0]["rows"].items())))
        self.assertNotEqual(list(baseline["sheets"][0]["rows"]), list(scrambled["sheets"][0]["rows"]))
        self.assertEqual(self.build(bf.make_spec(), baseline), self.build(bf.make_spec(), scrambled))

    def test_the_catalog_key_order_does_not_matter(self):
        catalog = bf.small_catalog()
        reordered = {"sources": {k: dict(reversed(list(v.items()))) for k, v in reversed(list(
            catalog["sources"].items()))}, "schema": 1}
        self.assertEqual(self.build(bf.make_spec(), bf.small_baseline(), catalog),
                         self.build(bf.make_spec(), bf.small_baseline(), reordered))

    def test_the_bytes_are_utf8_lf_and_end_with_one_newline(self):
        label = "연도 Năm €"
        document, _ = bf.bind(bf.make_spec(year_filter_label=label,
                                           labels={"A1": "title"}), bf.small_baseline())
        data = bf.dumps(document)
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.endswith(b"}\n"))
        self.assertFalse(data.endswith(b"\n\n"))
        text = data.decode("utf-8")
        self.assertIn(label, text)                                         # not escaped as \uXXXX
        self.assertNotIn("\\u", text)
        self.assertEqual(json.loads(text), document)
        self.assertEqual(data, intake_xlsx.dump_bytes(document))
        self.assertTrue(text.startswith('{\n "version"'))                   # indent 1

    def test_the_helper_cells_are_deterministic_too(self):
        first = bf.bind()[0]["sheets"][0]
        second = bf.bind()[0]["sheets"][0]
        self.assertEqual(list(first["cells"]), list(second["cells"]))
        self.assertEqual(list(first["rows"]), list(second["rows"]))


def structure_spec():
    return bf.make_spec([
        {"cells": "D5", "source": "plain", "where": {"code": "A"}, "column": "m3", "zero": "dash"},
        {"cells": "D6:E6", "source": "plain", "where": {"code": "A"}, "columns": ["m2", "m1"]},
        {"cells": "D7", "source": "plain", "where": {"code": "B"}, "column": "m1", "year": "previous"},
        {"cells": "D8", "source": "secret", "column": "m1"},
        {"cells": "D2", "source": "grid", "where": {"n": 2, "kind": "x"}, "column": "m1", "zero": "dash"},
        {"cells": "D3", "source": "grid", "where": {"kind": "x", "n": 2}, "column": "m2"},
    ])


# ---------------------------------------------------------------- time bounds

class TimingTests(unittest.TestCase):
    """Two earlier regex bugs of this project were quadratic: every case here must bind in well under 2 seconds."""

    LIMIT = 2.0

    def timed(self, baseline, spec=bf.DEFAULT, catalog=bf.DEFAULT, limit=None):
        start = time.perf_counter()
        result = bf.bind(spec, baseline, catalog)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, limit or self.LIMIT, "bind took %.2f s" % elapsed)
        return result

    def test_a_20_kb_identifier_in_a_baseline_formula(self):
        baseline = bf.edit(bf.small_baseline(), {"H8": {"content": "=" + "A" * 20000}})
        self.timed(baseline)
        baseline = bf.edit(bf.small_baseline(), {"H8": {"content": "=" + "ABC1" * 5000}})
        self.timed(baseline)

    # since the combined security review (M2) the final function-name guard FAILS CLOSED: a formula it cannot read with
    # certainty (a quote or `!` outside a string, an unterminated string) is refused with this exact text, so the
    # hostile-formula cases below assert that refusal AND the time limit (the scan must stay linear)
    UNREADABLE = ("formula(s) that cannot be checked (a quote or `!` outside a string, or an unterminated string) "
                  "in: H8")

    def refused_in_time(self, baseline, limit=None):
        """`bind` answers BindError(UNREADABLE) for `baseline` within the time limit."""
        start = time.perf_counter()
        with self.assertRaises(bind_kpi.BindError) as raised:
            bf.bind(bf.DEFAULT, baseline, bf.DEFAULT)
        elapsed = time.perf_counter() - start
        self.assertEqual(str(raised.exception), self.UNREADABLE)
        self.assertLess(elapsed, limit or self.LIMIT, "bind took %.2f s" % elapsed)

    def test_36_kb_of_escaped_quote_pairs_in_a_baseline_formula(self):
        # `"\"` repeated: a quote after a backslash does not close a string, so the run ends inside a literal that never
        # closes: the formula is refused as unreadable (and quickly)
        baseline = bf.edit(bf.small_baseline(), {"H8": {"content": "=" + '"\\"' * 12000}})
        self.refused_in_time(baseline)

    def test_an_unterminated_literal_full_of_escaped_quotes_is_scanned_in_linear_time(self):
        # was RED on purpose (finding F1): `intake_xlsx.LITERAL_OR_SHEET_MARK` restarted at every quote of an
        # unterminated literal and read to the end each time: 36 KB took about 7 s, 24 KB (used here) about 3 s, and
        # the time grew with the square of the size. Now the formula is also refused (fail closed), still in < 1 s
        baseline = bf.edit(bf.small_baseline(), {"H8": {"content": '="' + 'a\\"' * 8000}})
        self.refused_in_time(baseline, limit=1.0)

    def test_a_long_run_of_sheet_marks_and_quotes(self):
        refused = ("='" * 8000, "=!" * 12000, "=" + "'" * 20000)             # a quote / `!` outside a literal
        for content in refused:
            with self.subTest(content=content[:6]):
                self.refused_in_time(bf.edit(bf.small_baseline(), {"H8": {"content": content}}))
        # 12,000 closed pairs `""`: every literal closes, nothing outside them: readable, bound as before
        content = '="' * 12000
        with self.subTest(content=content[:6]):
            self.timed(bf.edit(bf.small_baseline(), {"H8": {"content": content}}))

    def test_a_5000_cell_baseline(self):
        cells = {}
        for row in range(1, 51):
            for col in range(1, 101):
                cells["%s%d" % (intake_xlsx.col_letters(col), row)] = {"content": "0" if col > 1 else "label %d" % row,
                                                                      "style": 1}
        baseline = bf.make_baseline(cells=cells, merges=[], row_number=50, col_number=100)
        self.assertEqual(len(baseline["sheets"][0]["cells"]), 5000)
        catalog = bf.small_catalog()
        columns = catalog["sources"]["wide"]["column_groups"]["all"]
        bindings = [{"cells": "B%d:CV%d" % (row, row), "source": "wide", "columns": columns[:99]} for row in range(1, 51)]
        # 99 cells per row: B..CV is 99 columns
        document, report = self.timed(baseline, bf.make_spec(bindings), catalog)
        self.assertEqual(report["bound_cells"], 50 * 99)
        self.assertEqual(report["lists"], 1)

    def test_a_5000_cell_baseline_with_dash_cells_and_many_formulas(self):
        cells = {"%s%d" % (intake_xlsx.col_letters(col), row): {"content": "0"} for row in range(1, 41)
                 for col in range(1, 51)}
        for row in range(41, 51):
            for col in range(1, 51):
                cells["%s%d" % (intake_xlsx.col_letters(col), row)] = {"content": "=A49+B49"}
        baseline = bf.make_baseline(cells=cells, merges=[], row_number=50, col_number=50)
        catalog = bf.small_catalog()
        columns = catalog["sources"]["wide"]["column_groups"]["all"]
        bindings = [{"cells": "A%d:AX%d" % (row, row), "source": "wide", "columns": columns[:50], "zero": "dash"}
                    for row in range(1, 41)]
        document, report = self.timed(baseline, bf.make_spec(bindings), catalog)
        self.assertEqual((report["bound_cells"], report["dash_cells"], report["helper_rows"]), (2000, 2000, 40))

    def test_a_formula_with_thousands_of_references_next_to_thousands_of_dash_cells(self):
        cells = {"%s%d" % (intake_xlsx.col_letters(col), row): {"content": "0"} for row in range(1, 21)
                 for col in range(1, 51)}
        cells["AZ1"] = {"content": "=" + "+".join("%s%d" % (intake_xlsx.col_letters(1 + i % 50), 30 + i // 50)
                                                  for i in range(1500))}
        baseline = bf.make_baseline(cells=cells, merges=[], row_number=80, col_number=52)
        catalog = bf.small_catalog()
        columns = catalog["sources"]["wide"]["column_groups"]["all"]
        bindings = [{"cells": "A%d:AX%d" % (row, row), "source": "wide", "columns": columns[:50], "zero": "dash"}
                    for row in range(1, 21)]
        self.timed(baseline, bf.make_spec(bindings), catalog)

    def test_500_bindings_bind_fast(self):
        baseline = bf.make_baseline(cells={}, merges=[], row_number=500, col_number=1)
        bindings = [{"cells": "A%d" % i, "source": "secret", "column": "m1", "zero": "dash"} for i in range(1, 501)]
        self.timed(baseline, bf.make_spec(bindings))


if __name__ == "__main__":
    unittest.main()
