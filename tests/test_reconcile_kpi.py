"""scripts/reconcile_kpi.py: the numbers an Excel had TYPED against the live numbers the bound dashboard shows.

Plan kpi-skill-improvements, steps 1-2 (AC1, AC1b, AC2, AC2b, AC2c). Every run is a real `main()` call against the
in-process fake Odoo on 127.0.0.1 (lists through `search_read`, QMS pivots through `read_group` with a time zone) and
the spec names the SHIPPED catalog sources (reconcile has no `--catalog`). The dashboard file is produced by `bind()` as
the real flow does. The expectations are written by hand from the Plan, never computed with the script under test.
Customer data is never used: sheets, rows and labels are invented.
"""
import collections
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import unittest
from unittest import mock

import reconcile_fixtures as rf
from dashboard_fixtures import StreamCase, start_fake, use_qaqc_env
from fake_odoo import FAKE_DB, FAKE_KEY, FAKE_LOGIN, FAKE_UID, UNSET, DocumentsError
from helpers import ExplodingOpener, RecordingOpener, ini_text, odoo_rpc, write_file

import bind_kpi
import intake_xlsx
import reconcile_kpi
import validate_formulas

Counts = collections.namedtuple("Counts", "replaced typed equal rounded differ error untyped text formula")
SUMMARY = re.compile(r"^replaced (\d+) cells: (\d+) typed \((\d+) equal, (\d+) equal after rounding, (\d+) differ, "
                     r"(\d+) error\), (\d+) untyped, (\d+) had text; (\d+) formula cells left to Excel "
                     r"\(not compared\)$")
VALUE = r"(?:-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|-|N/A|<text>|error:\w+)"      # a number, a dash, N/A, <text>, an error name
HEADER = re.compile(r"^dashboard sha256 ([0-9a-f]{64}), year (\d{4})(?:, time zone ([A-Za-z0-9_+\-/]+))?$")
CELL = re.compile(r"^([A-Z]{1,3}\d+)[:,]? Excel (\S+?),? live (\S+?)(?: \((?:differ|error)\))?$")
MORE = re.compile(r"^\.\.\. (\d+) more \(use --out for the full list\)$")
MONTHS = "DEFGHIJKLMNO"


def counts_of(out):
    """The summary line of a stdout as `Counts` (the Plan's wording is pinned by the regular expression)."""
    lines = [line for line in out.splitlines() if line.startswith("replaced ")]
    assert len(lines) == 1, out
    match = SUMMARY.match(lines[0])
    assert match, lines[0]
    return Counts(*(int(group) for group in match.groups()))


def diff_cells(out):
    """[(ref, live value text)] of the listed differing / failing cells, in the printed order."""
    return [(m.group(1), m.group(3)) for m in map(CELL.match, out.splitlines()) if m]


def month_counts(**by_month):
    """A list of 12 counts: month_counts(m4=2) -> zeros with 2 in April."""
    values = [0] * 12
    for key, value in by_month.items():
        values[int(key[1:]) - 1] = value
    return values


class ReconcileCase(StreamCase):
    def setUp(self):
        super().setUp()
        self.fake = start_fake(self)
        use_qaqc_env(self.fake.url)
        self.world = rf.make_world(self.fake)
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.baseline_path = os.path.join(self.work, "baseline.json")
        self.spec_path = os.path.join(self.work, "spec.json")
        self.dashboard_path = os.path.join(self.work, "dashboard.json")
        self.out_path = os.path.join(self.work, "reconcile_report.json")

    # ---- building the scene
    def put(self, path, data):
        with open(path, "wb") as handle:
            handle.write(data)

    @staticmethod
    def read(path):
        with open(path, "rb") as handle:
            return handle.read()

    def scene(self, typed, bindings, with_sha=False, produce=True, **spec_over):
        """Writes baseline.json / spec.json / dashboard.json (the bind output) into the work folder."""
        self.baseline = rf.baseline_of(typed)
        if with_sha:
            spec_over["baseline_sha256"] = hashlib.sha256(intake_xlsx.dump_bytes(self.baseline)).hexdigest()
        self.spec = rf.spec_of(bindings, **spec_over)
        if produce:
            baseline_bytes, spec_bytes, self.dashboard_bytes = rf.produce(self.baseline, self.spec)
        else:
            baseline_bytes, spec_bytes = intake_xlsx.dump_bytes(self.baseline), json.dumps(self.spec).encode("utf-8")
            self.dashboard_bytes = b"{}\n"
        self.put(self.baseline_path, baseline_bytes)
        self.put(self.spec_path, spec_bytes)
        self.put(self.dashboard_path, self.dashboard_bytes)

    def argv(self, *extra, year=2026):
        return ["--baseline", self.baseline_path, "--spec", self.spec_path, "--dashboard", self.dashboard_path,
                "--year", str(year)] + list(extra)

    def run_rec(self, *extra, year=2026, **kwargs):
        return self.collect(reconcile_kpi.main, self.argv(*extra, year=year), **kwargs)

    def reconcile(self, *extra, year=2026):
        """Runs, expects exit 0 and an empty stderr, returns stdout."""
        rc, out, err = self.run_rec(*extra, year=year)
        self.assertEqual((rc, err), (0, ""))
        return out

    def rpc_calls(self):
        return len(self.fake.requests)

    def set_tz(self, tz):
        self.world.users[FAKE_UID]["tz"] = tz

    def clear_rows(self, model):
        self.world.models[model]["rows"].clear()

    @contextlib.contextmanager
    def file_limit_after_bind(self, limit):
        """The binder's file size limit becomes `limit` once `bind()` has read its own inputs (the catalog is bigger
        than the small dashboard of these scenes, so the limit cannot be lowered from the start)."""
        real_bind, original = bind_kpi.bind, bind_kpi.MAX_FILE

        def bind(*args, **kwargs):
            result = real_bind(*args, **kwargs)
            bind_kpi.MAX_FILE = limit
            return result

        try:
            with mock.patch.object(bind_kpi, "bind", bind):
                yield
        finally:
            bind_kpi.MAX_FILE = original

    def sequence(self, model=None, method=None):
        return [call for call in self.fake.sequence() if model in (None, call[0]) and method in (None, call[1])]


# ---------------------------------------------------------------- AC2: the comparison table (lists)

class ComparisonTableTests(ReconcileCase):
    def one_row(self, typed, live, count=None, **list_extra):
        rf.add_list_row(self.world, rf.REPORT, 2026, live, "DK_REF", "shipped")
        self.scene(rf.typed_row(5, typed), [rf.list_binding(5, count=count or len(typed), **list_extra)])
        return self.reconcile()

    def test_equal_inside_the_tolerance_relative_above_one_and_absolute_below(self):
        out = self.one_row(["100", "0", "1000000", "1000000", "0.5", "0", "0"],
                           [100, 1e-10, 1000000.0000001, 1000001, 0.5000000001, 1e-9, 2e-9])
        # F: |diff| 1e-7 <= 1e-9 * 1e6 (relative); E: 1e-10 <= 1e-9 (the floor of max(1, |N|)); G differs by 1; the
        # bound itself is inside ("0" / 1e-9 is equal), one step beyond it ("0" / 2e-9) is only equal after rounding
        self.assertEqual(counts_of(out), Counts(7, 7, 5, 1, 1, 0, 0, 0, 0))
        self.assertEqual(diff_cells(out), [("G5", "1000001")])

    def test_outside_the_tolerance_but_rounded_to_the_decimals_of_the_typed_text_is_counted_apart(self):
        out = self.one_row(["1.5", "3", "1.5E-3", "2.5", "2.50", "2.5", "7", "1.5E2"],
                           [1.4999999, 2.6, 0.0015004, 2.506, 2.506, 2.5, 7.4, 154])
        # D 1.5 -> 1 decimal; E 3 -> 0 (2.6 rounds to 3); F 1.5E-3 -> 4 decimals; G 2.5 -> 1 decimal (2.506 -> 2.5);
        # H 2.50 -> 2 decimals (2.506 -> 2.51: differs); I equal; J 7 -> 0 decimals (7.4 rounds to 7);
        # K 1.5E2 is 150 with a decimals floor of 0 (154 does not round to 150: a negative count would round to tens)
        self.assertEqual(counts_of(out), Counts(8, 8, 1, 5, 2, 0, 0, 0, 0))
        self.assertEqual(diff_cells(out), [("H5", "2.506"), ("K5", "154")])

    def test_numeric_text_formats_the_binder_accepts_as_typed(self):
        out = self.one_row(["1.5E-3", "1.5e-3", "-0", "0.0", "0", "12", "-7.25"],
                           [0.0015, 0.0015, 0, 0, 0, 12, -7.25])
        self.assertEqual(counts_of(out), Counts(7, 7, 7, 0, 0, 0, 0, 0, 0))

    def test_a_json_number_in_the_baseline_is_typed_too(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [5, 2.5, 0], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["9", "9", "9"]), [rf.list_binding(5, count=3)])
        baseline = json.loads(intake_xlsx.dump_bytes(self.baseline).decode("utf-8"))
        cells = baseline["sheets"][0]["cells"]
        cells["D5"]["content"], cells["E5"]["content"], cells["F5"]["content"] = 5, 2.5, 0           # numbers, not text
        self.put(self.baseline_path, intake_xlsx.dump_bytes(baseline))
        self.put(self.dashboard_path, rf.produce(baseline, self.spec)[2])
        self.assertEqual(counts_of(self.reconcile()), Counts(3, 3, 3, 0, 0, 0, 0, 0, 0))

    def test_a_number_that_differs_is_listed_and_the_exit_code_stays_zero(self):
        out = self.one_row(["100", "100", "0"], [101, 100, 3])
        self.assertEqual(counts_of(out), Counts(3, 3, 1, 0, 2, 0, 0, 0, 0))
        self.assertEqual(diff_cells(out), [("D5", "101"), ("F5", "3")])       # typed 0 against a plain 3 differs too

    def test_typed_zero_against_the_dash_is_equal_and_any_other_typed_number_against_it_differs(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [0, 0, 7, 3], "SK_WM", "shipped")
        self.scene(rf.typed_row(6, ["0", "5", "7", "0"]), [rf.list_binding(6, "SK_WM", count=4, zero="dash")])
        out = self.reconcile()
        self.assertEqual(counts_of(out), Counts(4, 4, 2, 0, 2, 0, 0, 0, 0))     # D6 "0"/"-" and F6 equal
        self.assertEqual(diff_cells(out), [("E6", "-"), ("G6", "3")])           # typed 5 against "-", typed 0 against 3

    def test_a_dash_cell_with_no_row_at_all_still_reads_the_dash_for_a_typed_zero(self):
        self.scene(rf.typed_row(6, ["0", "0", "0"]), [rf.list_binding(6, "SK_WM", count=3, zero="dash")])
        self.assertEqual(counts_of(self.reconcile()), Counts(3, 3, 3, 0, 0, 0, 0, 0, 0))

    def test_an_unset_month_reads_zero(self):
        out = self.one_row(["0", "4"], [None, 4])
        self.assertEqual(counts_of(out), Counts(2, 2, 2, 0, 0, 0, 0, 0, 0))

    def test_a_number_cell_shows_zero_not_a_dash_so_a_typed_zero_is_equal_and_a_typed_one_differs(self):
        out = self.one_row(["0", "1"], [0, 0])
        self.assertEqual(counts_of(out), Counts(2, 2, 1, 0, 1, 0, 0, 0, 0))
        self.assertEqual(diff_cells(out), [("E5", "0")])

    def test_the_previous_year_list_is_read_through_the_next_year_column(self):
        rf.add_list_row(self.world, rf.PRODUCTION, 2026, [500, 510], "DK_REF")        # the year 2026 row
        rf.add_list_row(self.world, rf.PRODUCTION, 2025, [300, 310], "DK_REF")        # the year before: next_year 2026
        rf.add_list_row(self.world, rf.PRODUCTION, 2024, [100, 110], "DK_REF")
        typed = dict(rf.typed_row(5, ["500", "510"]), **rf.typed_row(6, ["300", "310"]))
        self.scene(typed, [rf.list_binding(5, source="production_group", count=2),
                           rf.list_binding(6, source="production_group", count=2, year="previous")])
        out = self.reconcile()
        self.assertEqual(counts_of(out), Counts(4, 4, 4, 0, 0, 0, 0, 0, 0))
        reads = [call for call in self.sequence(rf.PRODUCTION, "search_read")]
        self.assertEqual(sorted(call[2][0][-2][0] for call in reads), ["next_year", "year"])    # one read per list

    def test_the_year_filter_includes_both_ends_of_the_year_and_nothing_outside(self):
        for stored, read in (("2026-01-01", True), ("2026-12-31", True), ("2025-12-31", False), ("2027-01-01", False)):
            with self.subTest(year_value=stored):
                self.clear_rows(rf.REPORT)
                self.world.add_row(rf.REPORT, group_code="DK_REF", kind="shipped", year=stored, month_1=9.0)
                self.scene(rf.typed_row(5, ["9" if read else "0"]), [rf.list_binding(5, count=1)])
                self.assertEqual(counts_of(self.reconcile()), Counts(1, 1, 1, 0, 0, 0, 0, 0, 0))

    def test_the_year_option_picks_the_rows_of_that_year(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [26], "DK_REF", "shipped")
        rf.add_list_row(self.world, rf.REPORT, 2025, [25], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["25"]), [rf.list_binding(5, count=1)])
        self.assertEqual(counts_of(self.reconcile(year=2025)), Counts(1, 1, 1, 0, 0, 0, 0, 0, 0))
        self.assertEqual(counts_of(self.reconcile(year=2026)), Counts(1, 1, 0, 0, 1, 0, 0, 0, 0))

    def test_the_read_is_one_row_of_the_selection_with_the_year_range_on_the_chain_field(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["1"]), [rf.list_binding(5, count=1)])
        self.reconcile()
        (model, method, args, kwargs), = self.sequence(rf.REPORT)
        self.assertEqual((model, method), (rf.REPORT, "search_read"))
        self.assertEqual(args[0], [["group_code", "=", "DK_REF"], ["kind", "=", "shipped"],
                                   ["year", ">=", "2026-01-01"], ["year", "<=", "2026-12-31"]])
        self.assertEqual((kwargs["limit"], sorted(kwargs["fields"])),
                         (1, sorted(["group_code", "kind", "month_1"])))
        self.assertNotIn("context", kwargs)


# ---------------------------------------------------------------- AC2: derived cells

class DerivedCellsTests(ReconcileCase):
    """A ratio with a zero denominator, prev_period and sum are evaluated like the client does."""

    def setUp(self):
        super().setUp()
        rf.add_list_row(self.world, rf.REPORT, 2026, [10, 20, 0, 30], "DK_REF", "shipped")
        rf.add_list_row(self.world, rf.REPORT, 2026, [4, 0, 5, 3], "DK_REF", "produced")
        self.typed = {}
        for row, values in ((5, ["10", "20", "0", "30"]), (6, ["4", "0", "5", "3"]), (7, ["2.5", "7", "0", "9.99"]),
                            (8, ["3", "2", "0", "1"])):
            self.typed.update(rf.typed_row(row, values))
        self.typed.update({"P9": "60", "P10": "12"})
        self.bindings = [rf.list_binding(5, count=4), rf.list_binding(6, kind="produced", count=4, zero="dash"),
                         rf.derived_binding("D7:G7", "ratio", of="D5:G5", over="D6:G6"),
                         rf.derived_binding("D8:G8", "prev_period", of="D5:G5"),
                         rf.derived_binding("P9", "sum", of="D5:G5"), rf.derived_binding("P10", "sum", of="D6:G6")]

    def test_ratio_prev_period_and_sum_over_number_and_dash_cells(self):
        self.scene(self.typed, self.bindings)
        out = self.reconcile()
        # D7 2.5 equal, E7 N/A (zero denominator) vs typed 7, F7 0 equal, G7 10 vs typed 9.99;
        # D8 the first period is N/A vs typed 3, E8 2 equal, F8 0 equal, G8 N/A (previous period 0) vs typed 1
        self.assertEqual(counts_of(out), Counts(18, 18, 14, 0, 4, 0, 0, 0, 0))
        self.assertEqual(diff_cells(out), [("E7", "N/A"), ("G7", "10"), ("D8", "N/A"), ("G8", "N/A")])

    def test_the_ratio_reads_the_numbers_behind_the_dash_cells_never_the_dash_text(self):
        # D6 shows "-" on 0: the ratio over it would be #VALUE! if it read the display cell; the sum over the dash row
        # (P10, 4 + 0 + 5 + 3) proves the helper numbers are read
        self.scene(self.typed, self.bindings)
        out = self.reconcile()
        self.assertEqual(counts_of(out).error, 0)
        self.assertNotIn("P10", out)

    def test_a_typed_number_against_n_a_differs_and_n_a_text_in_the_baseline_is_had_text(self):
        self.typed["E7"], self.typed["D8"] = "N/A", "old text"    # text in the baseline cells of a derived binding
        self.bindings[2]["overwrite_text"] = self.bindings[3]["overwrite_text"] = True
        self.scene(self.typed, self.bindings)
        out = self.reconcile()
        self.assertEqual(counts_of(out), Counts(18, 16, 14, 0, 2, 0, 0, 2, 0))
        self.assertEqual(diff_cells(out), [("G7", "10"), ("G8", "N/A")])


# ---------------------------------------------------------------- AC1 / AC1b: which cells count

class ReplacedCellsTests(ReconcileCase):
    def test_the_shape_of_a_real_sheet_typed_blank_absent_formula_and_zero_cells(self):
        # rows 5-7: months 1-10 typed, months 11-12 NOT in the baseline at all, D7 a styled blank; row 8 and column P are
        # Excel totals (formulas) that stay; row 9: twelve typed zeros of a group with no data, shown as dashes
        for row, group, factor in ((5, "DK_REF", 100), (6, "DK_AC", 200), (7, "DK_ETC", 300)):
            rf.add_list_row(self.world, rf.REPORT, 2026, [factor * i for i in range(1, 13)], group, "shipped")
        typed = {}
        for row, factor in ((5, 100), (6, 200), (7, 300)):
            typed.update(rf.typed_row(row, [str(factor * i) for i in range(1, 11)] + [None, None]))
        typed["D7"] = ""
        typed.update(rf.typed_row(9, ["0"] * 12))
        for month in range(1, 13):
            typed[rf.ref(8, month)] = "=SUM(%s5:%s7)" % (MONTHS[month - 1], MONTHS[month - 1])
        for row in (5, 6, 7):
            typed["P%d" % row] = "=SUM(D%d:O%d)" % (row, row)
        self.scene(typed, [rf.list_binding(5, "DK_REF"), rf.list_binding(6, "DK_AC"), rf.list_binding(7, "DK_ETC"),
                           rf.list_binding(9, "SK_ETC", "produced", zero="dash")])
        out = self.reconcile()
        # replaced 12 + 12 + 12 + 12 = 48: typed 10 + 10 + 9 + 12 = 41 (all equal), untyped 2 + 2 + (1 + 2) = 7, 12 + 3 formulas
        self.assertEqual(counts_of(out), Counts(48, 41, 41, 0, 0, 0, 7, 0, 15))
        self.assertEqual(diff_cells(out), [])

    def test_helper_and_probe_rows_below_the_design_area_are_not_replaced_cells(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1, 2, 3], "DK_REF", "shipped")
        rf.add_qms(self.world, "2026-02-10 12:00:00")
        typed = dict(rf.typed_row(5, ["1", "2", "3"]), **rf.typed_row(6, ["0"] * 12))
        self.scene(typed, [rf.list_binding(5, count=3, zero="dash"), rf.qms_binding(6)])
        document = json.loads(self.dashboard_bytes.decode("utf-8"))
        below = [r for r in document["sheets"][0]["cells"] if intake_xlsx.split_ref(r)[0] > rf.AREA_ROWS]
        self.assertGreaterEqual(len(below), 4)                    # three helper cells and the year cell of the probe
        counts = counts_of(self.reconcile())
        self.assertEqual((counts.replaced, counts.untyped), (15, 0))     # 3 + 12 bound cells, no helper

    def test_the_last_row_of_the_area_is_inside_it_and_a_row_below_is_not(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1, 2, 3], "DK_REF", "shipped")
        self.scene(rf.typed_row(rf.AREA_ROWS, ["1", "2", "9"]), [rf.list_binding(rf.AREA_ROWS, count=3, zero="dash")])
        out = self.reconcile()
        self.assertEqual(counts_of(out), Counts(3, 3, 2, 0, 1, 0, 0, 0, 0))
        self.assertEqual(diff_cells(out), [("F12", "3")])

    def test_a_typed_number_kept_on_purpose_and_text_nobody_touched_are_not_counted(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1, 2, 3], "DK_REF", "shipped")
        typed = dict(rf.typed_row(5, ["1", "2", "3"]), D8="77")
        self.scene(typed, [rf.list_binding(5, count=3)], keep_typed=["D8"])
        out = self.reconcile()
        self.assertEqual(counts_of(out), Counts(3, 3, 3, 0, 0, 0, 0, 0, 0))
        self.assertNotIn(rf.LABEL, out)

    def test_a_baseline_blank_and_a_cell_missing_from_the_baseline_are_untyped_not_compared(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [11, 22, 33, 44], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["11", "", None, None]), [rf.list_binding(5, count=4)])
        out = self.reconcile()
        self.assertEqual(counts_of(out), Counts(4, 1, 1, 0, 0, 0, 3, 0, 0))
        self.assertEqual(diff_cells(out), [])

    def test_a_text_label_is_had_text_when_the_binding_may_overwrite_it(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1, 2, 3], "DK_REF", "shipped")
        self.scene(dict(rf.typed_row(5, ["1", "2"]), F5="old label"), [rf.list_binding(5, count=3, overwrite_text=True)])
        out = self.reconcile()
        self.assertEqual(counts_of(out), Counts(3, 2, 2, 0, 0, 0, 0, 1, 0))
        self.assertNotIn("old label", out)

    def test_a_baseline_formula_that_a_binding_replaces_is_untyped(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1, 2], "DK_REF", "shipped")
        self.scene(dict(rf.typed_row(5, ["1"]), E5="=1+1"), [rf.list_binding(5, count=2)])
        self.assertEqual(counts_of(self.reconcile()), Counts(2, 1, 1, 0, 0, 0, 1, 0, 0))

    def test_a_formula_the_binder_left_alone_is_counted_apart_and_never_compared(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1, 2], "DK_REF", "shipped")
        self.scene(dict(rf.typed_row(5, ["1", "2"]), P5="=SUM(D5:E5)", P6="=SUM(D5:E5)*2"), [rf.list_binding(5, count=2)])
        self.assertEqual(counts_of(self.reconcile()), Counts(2, 2, 2, 0, 0, 0, 0, 0, 2))

    def test_a_cell_the_spec_does_not_bind_is_not_counted_whatever_it_holds(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1, 2], "DK_REF", "shipped")
        self.scene(dict(rf.typed_row(5, ["1", "2"]), A7="a title", B7="text", C7="  "), [rf.list_binding(5, count=2)])
        self.assertEqual(counts_of(self.reconcile()), Counts(2, 2, 2, 0, 0, 0, 0, 0, 0))

    def test_more_than_fifty_differing_cells_are_cut_at_fifty_and_the_full_list_goes_to_out(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [2] * 12, "DK_REF", "shipped")
        typed = {}
        for row in range(5, 10):
            typed.update(rf.typed_row(row, ["1"] * 12))
        self.scene(typed, [rf.list_binding(row) for row in range(5, 10)])
        out = self.reconcile("--out", self.out_path)
        self.assertEqual(counts_of(out), Counts(60, 60, 0, 0, 60, 0, 0, 0, 0))
        self.assertEqual(len(diff_cells(out)), 50)
        self.assertEqual(diff_cells(out)[0], ("D5", "2"))
        self.assertEqual(diff_cells(out)[-1], ("E9", "2"))                  # row-major: 48 cells of rows 5-8, D9, E9
        self.assertEqual([MORE.match(line).group(1) for line in out.splitlines() if MORE.match(line)], ["10"])
        with open(self.out_path, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(len(report["cells"]), 60)
        self.assertEqual(report["cells"][0]["cell"], "D5")

    def test_exactly_fifty_differing_cells_are_all_listed_with_no_more_line(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [2] * 12, "DK_REF", "shipped")
        typed = {}
        for row in range(5, 9):
            typed.update(rf.typed_row(row, ["1"] * 12))
        typed.update(rf.typed_row(9, ["1", "1"]))
        self.scene(typed, [rf.list_binding(row) for row in range(5, 9)] + [rf.list_binding(9, count=2)])
        out = self.reconcile()
        self.assertEqual(counts_of(out).differ, 50)
        self.assertEqual(len(diff_cells(out)), 50)
        self.assertFalse([line for line in out.splitlines() if MORE.match(line)])

    def test_fifty_one_differing_cells_print_fifty_and_one_more(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [2] * 12, "DK_REF", "shipped")
        typed = {}
        for row in range(5, 9):
            typed.update(rf.typed_row(row, ["1"] * 12))
        typed.update(rf.typed_row(9, ["1", "1", "1"]))
        self.scene(typed, [rf.list_binding(row) for row in range(5, 9)] + [rf.list_binding(9, count=3)])
        out = self.reconcile()
        self.assertEqual(len(diff_cells(out)), 50)
        self.assertEqual([MORE.match(line).group(1) for line in out.splitlines() if MORE.match(line)], ["1"])

    def test_the_differing_cells_are_listed_in_reading_order(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [5, 5, 5], "DK_REF", "shipped")
        typed = dict(rf.typed_row(5, ["1", "1", "1"]), **rf.typed_row(6, ["1", "1", "1"]))
        self.scene(typed, [rf.list_binding(6, count=3), rf.list_binding(5, count=3)])
        self.assertEqual([ref for ref, _ in diff_cells(self.reconcile())], ["D5", "E5", "F5", "D6", "E6", "F6"])


# ---------------------------------------------------------------- AC2: errors are never hidden

class EvaluatorErrorTests(ReconcileCase):
    """An input the evaluator cannot read is an `error`, never `equal`; IFERROR cannot hide a forgotten input."""

    @staticmethod
    def doc(cells, **top):
        base = {"sheets": [{"cells": {r: {"content": c} for r, c in cells.items()}, "rowNumber": 10}]}
        return dict(base, **top)

    def test_every_kind_of_evaluator_failure_is_an_error_and_a_text_result_a_differ(self):
        baseline = self.doc({"A1": "5", "A2": "6", "A3": "7", "A4": "8", "A5": "9"})
        document = self.doc({"A1": "=1/0",                                                    # #DIV/0!
                             "A2": '=IFERROR(ODOO.LIST(9,1,"month_1")+0,0)',                  # list nobody fed: NotCatchable
                             "A3": '=IFERROR(ODOO.PIVOT(4,"__count","check_date:month","04/2026")+0,0)',   # pivot nobody fed
                             "A4": "=NOSUCHFUNCTION(1)",                                       # the client's #NAME?
                             "A5": '="a server text"'})                                        # text that is no `-` / `N/A`
        report = reconcile_kpi.compare(baseline, document, {}, {})
        self.assertEqual({k: report["counts"][k] for k in ("replaced", "typed", "equal", "rounded", "differ", "error")},
                         {"replaced": 5, "typed": 5, "equal": 0, "rounded": 0, "differ": 1, "error": 4})
        self.assertEqual([(c["cell"], c["class"]) for c in report["cells"]],
                         [("A1", "error"), ("A2", "error"), ("A3", "error"), ("A4", "error"), ("A5", "differ")])
        self.assertEqual(report["cells"][4]["live"], "<text>")                                # a server text is never printed
        for item in report["cells"][:4]:
            self.assertTrue(item["live"].startswith("error:"), item)

    def test_an_error_is_not_equal_even_when_a_swallowed_error_would_have_matched(self):
        baseline = self.doc({"A1": "0"})                                                      # typed 0 == what IFERROR gives
        document = self.doc({"A1": '=IFERROR(ODOO.LIST(9,1,"month_1")+0,0)'})
        counts = reconcile_kpi.compare(baseline, document, {}, {})["counts"]
        self.assertEqual((counts["error"], counts["equal"], counts["rounded"], counts["differ"]), (1, 0, 0, 0))

    def test_an_empty_read_is_a_list_with_no_row_and_reads_zero(self):
        baseline = self.doc({"A1": "0", "A2": "0"})
        document = self.doc({"A1": '=IFERROR(ODOO.LIST(9,1,"month_1")+0,0)',
                             "A2": '=IFERROR(ODOO.PIVOT(4,"__count","check_date:month","04/2026")+0,0)'})
        pivots = {4: {"measures": ["__count"], "months": {}}}
        counts = reconcile_kpi.compare(baseline, document, {9: []}, pivots)["counts"]
        self.assertEqual((counts["equal"], counts["error"]), (2, 0))

    def test_every_list_and_pivot_id_of_the_dashboard_is_fed_even_when_the_server_has_no_row(self):
        # an empty database: nothing may be omitted, or IFERROR-wrapped reads would raise instead of reading 0
        seen = []

        class Spy(validate_formulas.SheetEvaluator):
            def __init__(self, data, lists=None, translations=None, pivots=None):
                seen.append((copy.deepcopy(lists), copy.deepcopy(pivots)))
                super().__init__(data, lists=lists, translations=translations, pivots=pivots)

        typed = dict(rf.typed_row(5, ["0", "0"]), **rf.typed_row(6, ["0", "0"]), **rf.typed_row(7, ["0"] * 12))
        self.scene(typed, [rf.list_binding(5, "DK_REF", count=2), rf.list_binding(6, "DK_AC", count=2),
                           rf.qms_binding(7)])
        with mock.patch.object(validate_formulas, "SheetEvaluator", Spy):
            out = self.reconcile()
        (lists, pivots), = seen
        self.assertEqual(lists, {1: [], 2: []})
        self.assertEqual(pivots, {1: {"measures": ["__count"], "months": {}},
                                  2: {"measures": ["__count"], "months": {}}})
        self.assertEqual(counts_of(out), Counts(16, 16, 16, 0, 0, 0, 0, 0, 0))

    def test_an_evaluator_failure_on_a_cell_is_counted_as_error_listed_and_the_exit_code_stays_zero(self):
        class Failing(validate_formulas.SheetEvaluator):
            def value(self, cell_ref):
                if cell_ref == "E5":
                    raise validate_formulas.NotCatchable("an input is missing")
                return super().value(cell_ref)

        rf.add_list_row(self.world, rf.REPORT, 2026, [0, 0, 5], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["0", "0", "5"]), [rf.list_binding(5, count=3)])
        with mock.patch.object(validate_formulas, "SheetEvaluator", Failing):
            out = self.reconcile()
        self.assertEqual(counts_of(out), Counts(3, 3, 2, 0, 0, 1, 0, 0, 0))
        self.assertEqual(diff_cells(out), [("E5", "error:NotCatchable")])

    def test_each_failure_family_the_evaluator_can_raise_is_an_error_not_a_crash(self):
        for exc in (validate_formulas.SheetError("#VALUE!"), validate_formulas.NotCatchable("x"),
                    validate_formulas.UnknownFunction("f"), ZeroDivisionError(), OverflowError(), ValueError("v"),
                    RecursionError()):
            with self.subTest(exc=type(exc).__name__):
                class Failing(validate_formulas.SheetEvaluator):
                    def value(self, cell_ref, _exc=exc):
                        raise _exc

                baseline = self.doc({"A1": "1"})
                document = self.doc({"A1": "=1"})
                with mock.patch.object(validate_formulas, "SheetEvaluator", Failing):
                    counts = reconcile_kpi.compare(baseline, document, {}, {})["counts"]
                self.assertEqual((counts["error"], counts["equal"]), (1, 0))


# ---------------------------------------------------------------- AC2b: pivots, time zone

class PivotTests(ReconcileCase):
    """The QMS pivot is read with `read_group` in the user's time zone and keyed by the local month."""

    def qms_scene(self, expected, *extra, tz=None):
        """A single pivot row D5:O5 typed with the `expected` counts; returns stdout."""
        if tz is not None:
            self.set_tz(tz)
        self.scene(rf.typed_row(5, [str(v) for v in expected]), [rf.qms_binding(5)])
        return self.reconcile(*extra)

    def test_a_row_on_a_month_boundary_lands_in_the_month_the_client_shows_east_and_west_of_utc(self):
        rows = [("2026-03-31 20:00:00", "pqc"), ("2026-01-15 12:00:00", "pqc"), ("2026-04-15 12:00:00", "pqc"),
                ("2026-04-10 12:00:00", "iqc")]                                  # the last is another form: never counted
        for when, form in rows:
            rf.add_qms(self.world, when, form=form)
        seoul = month_counts(m1=1, m4=2)                                         # 20:00 UTC is 05:00 on April 1 in Seoul
        los_angeles = month_counts(m1=1, m3=1, m4=1)                             # and 13:00 on March 31 in Los Angeles
        for tz, expected in (("Asia/Seoul", seoul), ("America/Los_Angeles", los_angeles)):
            with self.subTest(user_tz=tz):
                out = self.qms_scene(expected, tz=tz)
                self.assertEqual(counts_of(out), Counts(12, 12, 12, 0, 0, 0, 0, 0, 0))
                self.assertEqual(HEADER.match(out.splitlines()[0]).group(3), tz)

    def test_the_wrong_zone_gives_the_other_months_and_the_differing_cells_are_exactly_those(self):
        for when in ("2026-03-31 20:00:00", "2026-01-15 12:00:00", "2026-04-15 12:00:00"):
            rf.add_qms(self.world, when)
        out = self.qms_scene(month_counts(m1=1, m4=2), tz="America/Los_Angeles")     # typed with Seoul's months
        self.assertEqual(counts_of(out), Counts(12, 12, 10, 0, 2, 0, 0, 0, 0))
        self.assertEqual(diff_cells(out), [("F5", "1"), ("G5", "1")])               # March 1 not 0, April 1 not 2

    def test_tz_option_wins_over_the_zone_of_the_user_and_skips_reading_it(self):
        for when in ("2026-03-31 20:00:00", "2026-01-15 12:00:00", "2026-04-15 12:00:00"):
            rf.add_qms(self.world, when)
        self.set_tz("Asia/Seoul")
        out = self.qms_scene(month_counts(m1=1, m3=1, m4=1), "--tz", "America/Los_Angeles")
        self.assertEqual(counts_of(out), Counts(12, 12, 12, 0, 0, 0, 0, 0, 0))
        self.assertEqual(HEADER.match(out.splitlines()[0]).group(3), "America/Los_Angeles")
        self.assertEqual(self.world.users_reads, 0)                                  # --tz given: the user is not read
        self.assertEqual(len(self.sequence("res.users", "fields_get")), 1)           # the selection still validates it

    def test_the_boundary_second_by_second_in_zones_east_and_west_of_utc(self):
        # (zone, last instant of March, first instant of April) in UTC: the month changes between the two
        for tz, march, april in (("UTC", "2026-03-31 23:59:59", "2026-04-01 00:00:00"),
                                 ("Asia/Seoul", "2026-03-31 14:59:59", "2026-03-31 15:00:00"),
                                 ("Asia/Ho_Chi_Minh", "2026-03-31 16:59:59", "2026-03-31 17:00:00"),
                                 ("Pacific/Kiritimati", "2026-03-31 09:59:59", "2026-03-31 10:00:00"),
                                 ("Etc/GMT+12", "2026-04-01 11:59:59", "2026-04-01 12:00:00"),
                                 ("America/Los_Angeles", "2026-04-01 06:59:59", "2026-04-01 07:00:00")):
            for when, month in ((march, 3), (april, 4)):
                with self.subTest(tz=tz, instant=when):
                    self.clear_rows(rf.QMS)
                    rf.add_qms(self.world, when)
                    expected = [0] * 12
                    expected[month - 1] = 1
                    out = self.qms_scene(expected, "--tz", tz)
                    self.assertEqual(counts_of(out), Counts(12, 12, 12, 0, 0, 0, 0, 0, 0))

    def test_months_next_to_a_daylight_saving_change_keep_their_own_row(self):
        # the month is read from the `from` the server gives, not computed from a fixed offset per zone
        for tz, when, month in (("Europe/Berlin", "2026-02-28 22:59:59", 2), ("Europe/Berlin", "2026-02-28 23:00:00", 3),
                                ("Europe/Berlin", "2026-03-31 21:59:59", 3), ("Europe/Berlin", "2026-03-31 22:00:00", 4),
                                ("Europe/Berlin", "2026-10-31 22:59:59", 10), ("Europe/Berlin", "2026-10-31 23:00:00", 11),
                                ("America/Los_Angeles", "2026-03-01 07:59:59", 2),
                                ("America/Los_Angeles", "2026-03-01 08:00:00", 3),
                                ("America/Los_Angeles", "2026-11-01 06:59:59", 10),
                                ("America/Los_Angeles", "2026-11-01 07:00:00", 11),
                                ("America/Los_Angeles", "2026-12-01 07:59:59", 11),
                                ("America/Los_Angeles", "2026-12-01 08:00:00", 12),
                                ("Asia/Seoul", "2026-12-31 14:59:59", 12)):
            with self.subTest(tz=tz, instant=when):
                self.clear_rows(rf.QMS)
                rf.add_qms(self.world, when)
                expected = [0] * 12
                expected[month - 1] = 1
                self.assertEqual(counts_of(self.qms_scene(expected, "--tz", tz)), Counts(12, 12, 12, 0, 0, 0, 0, 0, 0))

    def test_a_pivot_is_one_read_group_per_pivot_with_the_zone_the_language_and_no_lazy_grouping(self):
        rf.add_qms(self.world, "2026-04-15 12:00:00")
        self.qms_scene(month_counts(m4=1), tz="Asia/Seoul")
        calls = self.sequence(rf.QMS, "read_group")
        self.assertEqual(len(calls), 2)                                              # the data pivot and the year probe
        data, probe = calls
        for _, _, args, kwargs in calls:
            self.assertEqual(args[2], ["check_date:month"])
            self.assertEqual(kwargs, {"lazy": False, "context": {"tz": "Asia/Seoul", "lang": "en_US"}})
        self.assertEqual(data[2][0], [["form_type", "=", "pqc"]])                    # no Year filter: all years
        self.assertEqual(probe[2][0], [["check_date", ">=", "2026-01-01"], ["check_date", "<=", "2026-12-31"]])
        self.assertEqual((data[2][1], probe[2][1]), (["__count"], ["__count"]))

    def test_the_data_pivot_gets_all_months_of_both_years_and_the_probe_only_non_empty_months_of_the_selected_year(self):
        seen = []

        class Spy(validate_formulas.SheetEvaluator):
            def __init__(self, data, lists=None, translations=None, pivots=None):
                seen.append(copy.deepcopy(pivots))
                super().__init__(data, lists=lists, translations=translations, pivots=pivots)

        for when in ("2026-03-10 12:00:00", "2026-03-11 12:00:00", "2026-10-10 12:00:00", "2025-01-10 12:00:00",
                     "2025-12-10 12:00:00", "2027-02-10 12:00:00"):
            rf.add_qms(self.world, when)
        self.scene(rf.typed_row(5, ["0"] * 12), [rf.qms_binding(5)])
        with mock.patch.object(validate_formulas, "SheetEvaluator", Spy):
            self.reconcile("--tz", "UTC")
        (pivots,) = seen
        self.assertEqual(sorted(pivots[1]["months"]), ["01/2025", "02/2027", "03/2026", "10/2026", "12/2025"])
        self.assertEqual(sorted(pivots[2]["months"]), ["03/2026", "10/2026"])      # selected year, non-empty only
        self.assertEqual(pivots[1]["months"]["03/2026"], {"__count": 2.0})
        self.assertEqual(pivots[2]["measures"], ["__count"])

    def test_a_previous_year_row_reads_the_data_pivot_of_the_year_before(self):
        for when in ("2026-03-10 12:00:00", "2026-03-11 12:00:00", "2026-10-10 12:00:00", "2025-01-10 12:00:00",
                     "2025-12-10 12:00:00", "2025-12-11 12:00:00"):
            rf.add_qms(self.world, when)
        typed = dict(rf.typed_row(5, [str(v) for v in month_counts(m3=2, m10=1)]),
                     **rf.typed_row(6, [str(v) for v in month_counts(m1=1, m12=2)]))
        self.set_tz("UTC")
        self.scene(typed, [rf.qms_binding(5), rf.qms_binding(6, year="previous")])
        self.assertEqual(counts_of(self.reconcile()), Counts(24, 24, 24, 0, 0, 0, 0, 0, 0))

    def test_a_year_with_no_data_reads_zero_for_the_previous_year_too_like_the_client(self):
        # the probe has no month column in 2027, so its year cell is 1899 and "previous" reads 1898, not 2026 (the
        # documented limitation of the generated sheet; the evaluator reproduces the client)
        for when in ("2026-03-10 12:00:00", "2025-12-10 12:00:00"):
            rf.add_qms(self.world, when)
        self.set_tz("UTC")
        typed = dict(rf.typed_row(5, ["0"] * 12), **rf.typed_row(6, ["0"] * 12))
        self.scene(typed, [rf.qms_binding(5), rf.qms_binding(6, year="previous")])
        self.assertEqual(counts_of(self.reconcile(year=2027)), Counts(24, 24, 24, 0, 0, 0, 0, 0, 0))

    def test_a_false_or_none_measure_is_zero_and_a_number_is_summed(self):
        rf.add_qms(self.world, "2026-04-10 12:00:00", ng_qty=2.5)
        rf.add_qms(self.world, "2026-04-11 12:00:00", ng_qty=1.5)
        rf.add_qms(self.world, "2026-05-10 12:00:00")                                # the measure unset: the sum is NULL
        rf.add_qms(self.world, "2026-05-11 12:00:00")
        self.set_tz("UTC")
        for answer in (False, None):
            with self.subTest(null_sum_answer=answer):
                self.world.null_sum_answer = answer
                self.scene(rf.typed_row(5, [str(v) for v in [0, 0, 0, 4, 0] + [0] * 7]),
                           [rf.qms_binding(5, measure="ng_qty")])
                self.assertEqual(counts_of(self.reconcile()), Counts(12, 12, 12, 0, 0, 0, 0, 0, 0))

    def test_the_filter_keeps_only_the_listed_results_and_the_measure_is_summed(self):
        rf.add_qms(self.world, "2026-04-10 12:00:00", result="OK", qty=5.0)
        rf.add_qms(self.world, "2026-04-11 12:00:00", result="Pass", qty=6.0)
        rf.add_qms(self.world, "2026-04-12 12:00:00", result="NG", qty=7.0)
        self.set_tz("UTC")
        self.scene(rf.typed_row(5, [str(v) for v in month_counts(m4=11)]),
                   [rf.qms_binding(5, measure="qty", filter={"overall_result": ["OK", "Pass"]})])
        self.assertEqual(counts_of(self.reconcile()), Counts(12, 12, 12, 0, 0, 0, 0, 0, 0))
        domain = self.sequence(rf.QMS, "read_group")[0][2][0]
        self.assertEqual(domain, [["form_type", "=", "pqc"], ["overall_result", "in", ["OK", "Pass"]]])

    def test_a_single_month_cell_and_a_pivot_on_a_source_with_no_row_at_all(self):
        self.set_tz("UTC")
        rf.add_qms(self.world, "2026-10-10 12:00:00")
        self.scene({"D5": "1", "E5": "0"}, [{"cells": "D5", "source": "qms_history", "where": {"form_type": "pqc"},
                                             "month": 10},
                                            {"cells": "E5", "source": "qms_history", "where": {"form_type": "oqc"},
                                             "month": 10}])
        self.assertEqual(counts_of(self.reconcile()), Counts(2, 2, 2, 0, 0, 0, 0, 0, 0))

    def test_a_list_only_spec_needs_no_time_zone_and_reads_nothing_about_users(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1], "DK_REF", "shipped")
        self.set_tz(False)
        self.scene(rf.typed_row(5, ["1"]), [rf.list_binding(5, count=1)])
        out = self.reconcile()
        self.assertEqual(self.sequence("res.users"), [])
        self.assertIsNone(HEADER.match(out.splitlines()[0]).group(3))


class TimeZoneDecisionTests(ReconcileCase):
    """tz source: --tz, else the user's `tz`; both empty -> exit 1; an unknown --tz is refused; the accepted tz is printed."""

    def pivot_scene(self):
        rf.add_qms(self.world, "2026-04-10 12:00:00")
        self.scene(rf.typed_row(5, ["0"] * 12), [rf.qms_binding(5)])

    def refused(self, *extra):
        del self.fake.requests[:]
        rc, out, err = self.run_rec(*extra)
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("error: "), err)
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertEqual(self.sequence(rf.QMS, "read_group"), [], "no pivot may be read before the zone is decided")
        return err

    def test_an_empty_zone_without_the_option_exits_one_and_never_falls_back_to_utc(self):
        self.pivot_scene()
        for empty in (False, "", None):
            with self.subTest(user_tz=empty):
                self.set_tz(empty)
                err = self.refused()
                self.assertIn("time zone", err)
                self.assertIn("--tz", err)

    def test_an_empty_zone_is_fine_when_the_option_names_one(self):
        self.pivot_scene()
        self.set_tz(False)
        out = self.reconcile("--tz", "UTC")
        self.assertEqual(HEADER.match(out.splitlines()[0]).group(3), "UTC")

    def test_an_unknown_zone_is_refused_whoever_names_it(self):
        self.pivot_scene()
        for name in ("Mars/Phobos", "asia/seoul", "UTC+9", "Asia/Seoul "):
            with self.subTest(option=name):
                self.set_tz("Asia/Seoul")
                self.assertIn("time zone", self.refused("--tz", name))
        self.set_tz("Mars/Phobos")                                                   # the user's own zone is not on the list
        self.assertIn("Mars/Phobos", self.refused())

    def test_a_zone_name_that_is_not_a_plain_name_is_refused_before_any_pivot_read(self):
        self.pivot_scene()
        for name in ("../etc/passwd", "Asia/Seoul\n", "A" * 80, "Asia/Seoul;x", "Asia/Seoul'--"):
            with self.subTest(option=name[:20]):
                self.refused("--tz", name)

    def test_the_accepted_zone_is_the_one_printed_and_it_is_the_one_sent_to_the_server(self):
        self.pivot_scene()
        for given, user, expected in (("Asia/Ho_Chi_Minh", "Asia/Seoul", "Asia/Ho_Chi_Minh"), (None, "Asia/Seoul", "Asia/Seoul")):
            with self.subTest(option=given):
                self.set_tz(user)
                del self.fake.requests[:]
                out = self.reconcile(*(("--tz", given) if given else ()))
                self.assertEqual(HEADER.match(out.splitlines()[0]).group(3), expected)
                for _, _, _, kwargs in self.sequence(rf.QMS, "read_group"):
                    self.assertEqual(kwargs["context"]["tz"], expected)

    def test_the_zone_is_validated_against_the_selection_of_the_server_not_a_list_of_the_script(self):
        self.pivot_scene()
        self.world.tz_selection = [["Asia/Seoul", "Seoul"]]                          # this server offers one zone only
        self.assertEqual(HEADER.match(self.reconcile("--tz", "Asia/Seoul").splitlines()[0]).group(3), "Asia/Seoul")
        self.refused("--tz", "UTC")

    def test_the_two_reads_outside_the_catalog_are_the_users_own_tz_and_the_tz_selection(self):
        self.pivot_scene()
        self.reconcile()
        users = self.sequence("res.users")
        self.assertEqual([(m, meth) for m, meth, _, _ in users], [("res.users", "fields_get"), ("res.users", "read")])
        self.assertEqual((users[0][2], users[0][3]), ([["tz"]], {"attributes": ["selection"]}))
        self.assertEqual((users[1][2], users[1][3]), ([[FAKE_UID], ["tz"]], {}))
        models = {call[0] for call in self.fake.sequence()}
        self.assertEqual(models, {"res.users", rf.QMS})                              # nothing else is read

    def test_a_server_that_answers_no_selection_is_a_zone_error(self):
        self.pivot_scene()
        for answer in ({}, {"tz": {}}, {"tz": {"selection": "x"}}, [], {"tz": {"selection": [5]}}):
            with self.subTest(answer=str(answer)):
                self.world.fields_get_override = answer
                self.assertIn("time zone", self.refused())

    def pivots_seen(self, *extra):
        """Runs once and returns the pivots the evaluator was fed ({pivot id: {"measures", "months"}})."""
        seen = []

        class Spy(validate_formulas.SheetEvaluator):
            def __init__(self, data, lists=None, translations=None, pivots=None):
                seen.append(copy.deepcopy(pivots))
                super().__init__(data, lists=lists, translations=translations, pivots=pivots)

        with mock.patch.object(validate_formulas, "SheetEvaluator", Spy):
            out = self.reconcile(*extra)
        (pivots,) = seen
        return out, pivots

    def test_a_null_date_group_is_ignored_and_the_dated_groups_are_counted_as_before(self):
        # Odoo 17 models.py:2538-2541: a record with NO date forms a group whose `__range[group]` is False. The client's
        # pivot has a column for it that no `MM/yyyy` read touches, so it counts for nothing (it was refused before)
        rf.add_qms(self.world, "2026-04-10 12:00:00")
        rf.add_qms(self.world, "2026-04-11 12:00:00")
        self.world.add_row(rf.QMS, form_type="pqc", overall_result="OK")             # no check_date: a group with no range
        self.world.add_row(rf.QMS, form_type="pqc", overall_result="OK")
        self.set_tz("UTC")
        self.scene(rf.typed_row(5, [str(v) for v in month_counts(m4=2)]), [rf.qms_binding(5)])
        out, pivots = self.pivots_seen()
        self.assertEqual(counts_of(out), Counts(12, 12, 12, 0, 0, 0, 0, 0, 0))     # April 2, not 4; the rest 0
        self.assertEqual(sorted(pivots[1]["months"]), ["04/2026"])                   # no month key for the NULL group
        self.assertEqual(pivots[1]["months"]["04/2026"], {"__count": 2.0})
        self.assertEqual(pivots[2]["months"]["04/2026"], {"__count": 2.0})

    def test_a_null_date_group_never_adds_its_measure_to_any_month(self):
        rf.add_qms(self.world, "2026-04-10 12:00:00", ng_qty=2.0)
        self.world.add_row(rf.QMS, form_type="pqc", overall_result="OK", ng_qty=100.0)
        self.set_tz("UTC")
        self.scene(rf.typed_row(5, [str(v) for v in month_counts(m4=2)]), [rf.qms_binding(5, measure="ng_qty")])
        out, pivots = self.pivots_seen()
        self.assertEqual(counts_of(out), Counts(12, 12, 12, 0, 0, 0, 0, 0, 0))
        self.assertEqual(pivots[1]["months"], {"04/2026": {"ng_qty": 2.0}})

    def test_a_pivot_with_only_a_null_date_group_reads_zero_in_every_month_and_exits_zero(self):
        self.world.add_row(rf.QMS, form_type="pqc", overall_result="OK")
        self.set_tz("UTC")
        self.scene(rf.typed_row(5, ["0"] * 12), [rf.qms_binding(5)])
        out, pivots = self.pivots_seen()
        self.assertEqual(counts_of(out), Counts(12, 12, 12, 0, 0, 0, 0, 0, 0))
        self.assertEqual(pivots[1]["months"], {})

    def test_a_null_date_group_next_to_a_malformed_group_is_still_refused(self):
        # the skip is for `__range[group] is False` and nothing else: the malformed group still ends the run (exit 1)
        self.set_tz("UTC")
        rf.add_qms(self.world, "2026-04-10 12:00:00")
        self.scene(rf.typed_row(5, ["0"] * 12), [rf.qms_binding(5)])
        null_group = {"__count": 1, "__range": {"check_date:month": False}}
        for label, malformed in (("no range", {"__count": 1}),
                                 ("range from garbage", {"__count": 1, "__range": {
                                     "check_date:month": {"from": "garbage", "to": "x"}}})):
            for order in (0, 1):
                with self.subTest(malformed=label, null_group_first=bool(order)):
                    groups = [null_group, malformed] if order == 0 else [malformed, null_group]
                    self.world.answer_override = {(rf.QMS, "read_group"): groups}
                    rc, out, err = self.run_rec()
                    self.assertEqual((rc, out), (1, ""))
                    self.assertEqual(err, "error: the server answered an unexpected month group\n")


# ---------------------------------------------------------------- AC2c: nothing is trusted from a file

class InputGuardTests(ReconcileCase):
    """The dashboard definitions come from `bind()` over the shipped catalog; every input problem exits 2 with 0 RPC calls."""

    def base_scene(self, **kwargs):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1, 2], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["1", "2"]), [rf.list_binding(5, count=2)], **kwargs)

    def refused(self, *extra, contains=(), argv=None):
        rc, out, err = self.collect(reconcile_kpi.main, argv or self.argv(*extra))
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(self.rpc_calls(), 0, "the network was reached before the input was refused")
        self.assertTrue(err.startswith("error: ") and len(err.splitlines()) == 1, err)
        for text in ([contains] if isinstance(contains, str) else contains):
            self.assertIn(text, err)
        return err

    def test_the_unchanged_bind_output_is_accepted_and_its_hash_is_printed(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1, 2], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["1", "2"]), [rf.list_binding(5, count=2)])
        os.remove(self.dashboard_path)
        bind_out, bind_err = io.StringIO(), io.StringIO()
        self.assertEqual(bind_kpi.main(["--baseline", self.baseline_path, "--spec", self.spec_path, "--out",
                                        self.dashboard_path], bind_out, bind_err), 0)           # the real bind step
        out = self.reconcile()
        self.assertEqual(HEADER.match(out.splitlines()[0]).group(1), json.loads(bind_out.getvalue())["sha256"])
        self.assertEqual(counts_of(out), Counts(2, 2, 2, 0, 0, 0, 0, 0, 0))

    def test_a_dashboard_file_that_differs_by_one_byte_is_refused_before_any_rpc(self):
        self.base_scene()
        data = self.dashboard_bytes
        for label, changed in (("last byte dropped", data[:-1]), ("one byte appended", data + b" "),
                               ("one byte changed", data.replace(b"month_1", b"month_2", 1)),
                               ("first byte changed", b" " + data[1:]), ("empty", b"")):
            with self.subTest(change=label):
                self.assertNotEqual(changed, data)
                self.put(self.dashboard_path, changed)
                self.refused(contains="dashboard")

    def test_the_dashboard_file_of_another_spec_is_refused(self):
        self.base_scene()
        other = rf.produce(rf.baseline_of(rf.typed_row(5, ["1", "2"])), rf.spec_of([rf.list_binding(5, count=2, zero="dash")]))[2]
        self.put(self.dashboard_path, other)
        self.refused(contains="dashboard")

    def test_the_dashboard_option_is_required(self):
        self.base_scene()
        argv = ["--baseline", self.baseline_path, "--spec", self.spec_path, "--year", "2026"]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            reconcile_kpi.main(argv, io.StringIO(), io.StringIO())
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--dashboard", stderr.getvalue())
        self.assertEqual(self.rpc_calls(), 0)

    def test_a_missing_dashboard_file_is_refused(self):
        self.base_scene()
        os.remove(self.dashboard_path)
        self.refused(contains="dashboard")

    def test_a_dashboard_file_over_the_size_limit_is_refused_and_never_read_whole(self):
        self.base_scene()
        with self.file_limit_after_bind(len(self.dashboard_bytes) - 1):
            self.refused(contains=("dashboard file", "larger"))

    def test_a_dashboard_file_exactly_at_the_size_limit_is_accepted(self):
        self.base_scene()
        with self.file_limit_after_bind(len(self.dashboard_bytes)):
            self.assertEqual(counts_of(self.reconcile()), Counts(2, 2, 2, 0, 0, 0, 0, 0, 0))

    def test_the_dashboard_file_is_read_with_a_bound_never_whole(self):
        # a stand-in that serves bytes and raises on an unbounded read (never a real endless file)
        self.base_scene()
        served = []

        class Bounded:
            def read(self, size=-1):
                if size is None or size < 0:
                    raise AssertionError("an unbounded read of the dashboard file")
                served.append(size)
                return b"x" * size

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

        real_open = open

        def fake_open(path, *args, **kwargs):
            return Bounded() if path == self.dashboard_path else real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", fake_open), self.file_limit_after_bind(1000):
            self.refused(contains=("dashboard file", "larger"))
        self.assertEqual(served, [1001])

    def test_the_dashboard_file_is_read_for_its_bytes_and_never_parsed_for_definitions(self):
        self.base_scene()
        text = self.dashboard_bytes.decode("utf-8")
        seen = []
        real_loads, real_load = json.loads, json.load

        def spy_loads(data, *args, **kwargs):
            seen.append(data if isinstance(data, str) else bytes(data).decode("utf-8", "replace"))
            return real_loads(data, *args, **kwargs)

        def spy_load(handle, *args, **kwargs):
            data = handle.read()
            seen.append(data if isinstance(data, str) else bytes(data).decode("utf-8", "replace"))
            return real_loads(data, *args, **kwargs)

        with mock.patch.object(json, "loads", spy_loads), mock.patch.object(json, "load", spy_load):
            self.reconcile()
        self.assertGreaterEqual(len(seen), 3)                      # baseline, spec, catalog (and the server answers)
        self.assertNotIn(text, seen)

    def test_a_spec_that_names_a_source_outside_the_catalog_is_refused_by_the_binder_with_no_rpc(self):
        self.scene(rf.typed_row(5, ["1", "2"]), [dict(rf.list_binding(5, count=2), source="no_such_view")], produce=False)
        err = self.refused(contains="no_such_view")
        self.assertIn("not in the catalog", err)
        self.assertIn("requirement text for a developer", err)

    def test_a_hand_edited_dashboard_file_that_names_another_model_is_refused_by_the_byte_check(self):
        self.base_scene()
        document = json.loads(self.dashboard_bytes.decode("utf-8"))
        document["lists"]["1"]["model"] = "res.users"                       # a hand-edited dashboard file
        self.put(self.dashboard_path, intake_xlsx.dump_bytes(document))
        self.refused(contains="dashboard")

    def test_a_stale_baseline_hash_is_refused_with_no_rpc_and_the_current_one_is_accepted(self):
        self.base_scene(with_sha=True)
        self.assertEqual(counts_of(self.reconcile()), Counts(2, 2, 2, 0, 0, 0, 0, 0, 0))
        before = self.rpc_calls()
        self.assertGreater(before, 0)
        self.fake.requests[:] = []
        self.spec["baseline_sha256"] = "0" * 64
        self.put(self.spec_path, json.dumps(self.spec).encode("utf-8"))
        self.refused(contains="sha256")
        self.spec["baseline_sha256"] = hashlib.sha256(intake_xlsx.dump_bytes(self.baseline) + b" ").hexdigest()
        self.put(self.spec_path, json.dumps(self.spec).encode("utf-8"))
        self.refused(contains="sha256")

    def test_the_baseline_is_read_as_bytes_so_a_baseline_edited_after_the_spec_stops(self):
        self.base_scene(with_sha=True)
        baseline = json.loads(intake_xlsx.dump_bytes(self.baseline).decode("utf-8"))
        baseline["sheets"][0]["cells"]["A1"]["content"] = "Edited title"
        self.put(self.baseline_path, intake_xlsx.dump_bytes(baseline))
        self.refused(contains="sha256")

    def test_every_other_spec_and_baseline_problem_is_exit_two_with_no_rpc(self):
        self.base_scene()
        good_spec = json.dumps(self.spec).encode("utf-8")
        for label, path, data in (("spec not JSON", self.spec_path, b"{nope"),
                                  ("spec not an object", self.spec_path, b"[]"),
                                  ("spec unknown key", self.spec_path, json.dumps(dict(self.spec, extra=1)).encode()),
                                  ("baseline not JSON", self.baseline_path, b"\xff\xfe"),
                                  ("baseline wrong shape", self.baseline_path, b"{}")):
            with self.subTest(case=label):
                original = self.read(path)
                self.put(path, data)
                self.refused()
                self.put(path, original)
        self.assertEqual(self.read(self.spec_path), good_spec)

    def test_a_missing_baseline_or_spec_file_is_exit_two(self):
        self.base_scene()
        for path in (self.baseline_path, self.spec_path):
            with self.subTest(path=os.path.basename(path)):
                original = self.read(path)
                os.remove(path)
                self.refused(contains="cannot read")
                self.put(path, original)

    def test_there_is_no_catalog_option(self):
        self.base_scene()
        catalog = os.path.join(self.work, "catalog.json")
        self.put(catalog, json.dumps(bind_kpi.load_catalog(project="dksk")).encode("utf-8"))
        for extra in (["--catalog", catalog], ["--catalog=" + catalog], ["--cat", catalog]):
            with self.subTest(extra=extra[0]):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    reconcile_kpi.main(self.argv(*extra), io.StringIO(), io.StringIO())
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("unrecognized arguments", stderr.getvalue())
        self.assertEqual(self.rpc_calls(), 0)

    def test_an_abbreviated_option_is_refused(self):
        self.base_scene()
        for short, full in (("--base", "--baseline"), ("--dash", "--dashboard"), ("--sp", "--spec"), ("--ye", "--year")):
            with self.subTest(option=short):
                argv = self.argv()
                argv[argv.index(full)] = short
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    reconcile_kpi.main(argv, io.StringIO(), io.StringIO())
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.rpc_calls(), 0)

    def test_the_help_text_names_no_catalog_and_an_abbreviation_is_not_taken_for_an_option(self):
        parser = reconcile_kpi.build_parser()
        self.assertNotIn("--catalog", parser.format_help())
        self.assertFalse(any("catalog" in "".join(a.option_strings) for a in parser._actions))
        self.assertFalse(parser.allow_abbrev)

    def test_a_non_local_host_needs_the_host_typed_by_the_human_before_any_request(self):
        self.base_scene()
        os.environ["ODOO_KPI_URL"] = "https://my-instance.example.com"
        opener = ExplodingOpener()
        factory = lambda config, confirm_instance=None: odoo_rpc.Client(config, confirm_instance=confirm_instance,
                                                                         opener=opener)
        for confirm in ([], ["--confirm-instance", "my-instance"], ["--confirm-instance", "other.example.com"]):
            with self.subTest(confirm=confirm):
                rc, out, err = self.run_rec(*confirm, client_factory=factory)
                self.assertEqual((rc, out), (2, ""))
                self.assertIn("--confirm-instance", err)
        self.assertEqual(opener.calls, 0)

    def test_the_host_typed_by_the_human_reaches_the_client(self):
        self.base_scene()
        os.environ["ODOO_KPI_URL"] = "https://my-instance.example.com"
        opener = RecordingOpener(FAKE_UID, [{"id": 1, "group_code": "DK_REF", "kind": "shipped", "month_1": 1,
                                             "month_2": 2}])
        factory = lambda config, confirm_instance=None: odoo_rpc.Client(config, confirm_instance=confirm_instance,
                                                                         opener=opener)
        rc, out, err = self.run_rec("--confirm-instance", "my-instance.example.com", client_factory=factory)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(counts_of(out), Counts(2, 2, 2, 0, 0, 0, 0, 0, 0))
        self.assertEqual(len(opener.requests), 2)                                    # the login and one list read

    def test_the_config_option_names_the_config_file(self):
        self.base_scene()
        for name in odoo_rpc.ENV_NAMES.values():
            os.environ.pop(name, None)
        config = write_file(self.work, ini_text(self.fake.url), name="my-config.ini")
        self.assertEqual(counts_of(self.reconcile("--config", config)), Counts(2, 2, 2, 0, 0, 0, 0, 0, 0))
        rc, out, err = self.run_rec()                                                # no variable, no default file
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("no config", err)


# ---------------------------------------------------------------- command line

class CommandLineTests(ReconcileCase):
    def many_cells(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1, 2, 3, 4], "DK_REF", "shipped")
        rf.add_list_row(self.world, rf.REPORT, 2026, [0, 7, 0, 9], "SK_WM", "shipped")
        typed = dict(rf.typed_row(5, ["1", "9", "3", "9"]), **rf.typed_row(6, ["0", "7", "1", "9"]))
        self.scene(typed, [rf.list_binding(5, count=4), rf.list_binding(6, "SK_WM", count=4, zero="dash")])

    def test_stdout_holds_only_the_hash_the_counts_cell_references_and_numbers(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1, 2, 3, 4], "DK_REF", "shipped")
        rf.add_list_row(self.world, rf.REPORT, 2026, [0, 7, 0, 9], "SK_WM", "shipped")
        rf.add_qms(self.world, "2026-04-10 12:00:00")
        self.scene(dict(rf.typed_row(5, ["1", "9", "3", "9"]), **rf.typed_row(6, ["0", "7", "1", "9"]),
                        **rf.typed_row(8, ["0"] * 12)),
                   [rf.list_binding(5, count=4), rf.list_binding(6, "SK_WM", count=4, zero="dash"), rf.qms_binding(8)])
        out = self.reconcile()
        lines = out.splitlines()
        self.assertTrue(HEADER.match(lines[0]), lines[0])
        self.assertTrue(SUMMARY.match(lines[1]), lines[1])
        value = VALUE
        for line in lines[2:]:
            self.assertTrue(re.match(r"^[A-Z]{1,3}\d+: Excel %s, live %s \((differ|error)\)$" % (value, value), line)
                            or MORE.match(line), line)
        for private in (rf.LABEL, "DK_REF", "SK_WM", "shipped", "dksk", "group_code", "Synthetic", FAKE_LOGIN, FAKE_DB,
                        self.fake.url, "127.0.0.1"):
            self.assertNotIn(private, out)

    def test_the_summary_line_is_the_one_of_the_plan(self):
        self.many_cells()
        out = self.reconcile()
        self.assertIn("replaced 8 cells: 8 typed (5 equal, 0 equal after rounding, 3 differ, 0 error), 0 untyped, "
                      "0 had text; 0 formula cells left to Excel (not compared)", out.splitlines())

    def test_a_server_text_in_a_number_column_is_never_printed(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1], "DK_REF", "shipped")
        self.world.answer_override[(rf.REPORT, "search_read")] = [{"id": 1, "group_code": "DK_REF", "kind": "shipped",
                                                                  "month_1": "SERVER-SAYS-HELLO"}]
        self.scene(rf.typed_row(5, ["1"]), [rf.list_binding(5, count=1)])
        rc, out, err = self.run_rec()
        self.assertNotIn("SERVER-SAYS-HELLO", out + err)
        self.assertEqual(rc, 0)                                         # text + 0 is an error the IFERROR turns into 0
        self.assertEqual(counts_of(out), Counts(1, 1, 0, 0, 1, 0, 0, 0, 0))

    def test_exit_zero_whatever_the_counts_even_when_every_cell_differs(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [9, 9], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["1", "2"]), [rf.list_binding(5, count=2)])
        rc, out, err = self.run_rec()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(counts_of(out), Counts(2, 2, 0, 0, 2, 0, 0, 0, 0))

    def test_nothing_is_written_unless_out_is_given(self):
        self.many_cells()
        before = sorted(os.listdir(self.work))
        self.reconcile()
        self.assertEqual(sorted(os.listdir(self.work)), before)

    def test_out_writes_the_report_with_the_intake_helper_and_leaves_no_temporary_file(self):
        self.many_cells()
        out = self.reconcile("--out", self.out_path)
        raw = self.read(self.out_path)
        report = json.loads(raw.decode("utf-8"))
        self.assertEqual(raw, intake_xlsx.dump_bytes(report))                         # the canonical serialisation
        self.assertEqual(report["sha256"], HEADER.match(out.splitlines()[0]).group(1))
        self.assertEqual((report["year"], report["time_zone"]), (2026, None))
        self.assertEqual(report["counts"]["differ"], 3)
        self.assertEqual([c["cell"] for c in report["cells"]], [ref for ref, _ in diff_cells(out)])
        self.assertEqual(sorted(n for n in os.listdir(self.work) if n.startswith(".intake-")), [])
        self.assertNotIn(rf.LABEL, raw.decode("utf-8"))

    def test_out_does_not_replace_a_file_without_force(self):
        self.many_cells()
        self.put(self.out_path, b"previous report")
        rc, out, err = self.run_rec("--out", self.out_path)
        self.assertEqual(rc, 2)
        self.assertIn("--force", err)
        self.assertEqual(self.read(self.out_path), b"previous report")
        self.assertEqual(sorted(n for n in os.listdir(self.work) if n.startswith(".intake-")), [])
        self.reconcile("--out", self.out_path, "--force")
        self.assertEqual(json.loads(self.read(self.out_path).decode("utf-8"))["year"], 2026)

    def test_out_never_replaces_the_baseline_even_with_force(self):
        self.many_cells()
        before = self.read(self.baseline_path)
        rc, out, err = self.run_rec("--out", self.baseline_path, "--force")
        self.assertEqual(rc, 2)
        self.assertEqual(self.read(self.baseline_path), before)

    def test_out_into_a_folder_that_does_not_exist_is_exit_two(self):
        self.many_cells()
        rc, out, err = self.run_rec("--out", os.path.join(self.work, "no_such_folder", "report.json"))
        self.assertEqual(rc, 2)
        self.assertIn("folder", err)

    def test_the_year_must_be_a_year_between_1900_and_2200(self):
        self.many_cells()
        for bad in ("1899", "2201", "abc", "", "20 26", "2026.5", "-2026", "1e3"):
            with self.subTest(year=bad):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    reconcile_kpi.main(self.argv(year=bad), io.StringIO(), io.StringIO())
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.rpc_calls(), 0)
        for good in (1900, 2200, 2026):
            with self.subTest(year=good):
                self.assertEqual(self.run_rec(year=good)[0], 0)

    def test_the_year_is_required(self):
        self.many_cells()
        argv = ["--baseline", self.baseline_path, "--spec", self.spec_path, "--dashboard", self.dashboard_path]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            reconcile_kpi.main(argv, io.StringIO(), io.StringIO())
        self.assertEqual(caught.exception.code, 2)


class ErrorPathTests(ReconcileCase):
    """Exit 1 = the server or the time zone, exit 2 = the input; the API key is in no stream and no file on ANY path."""

    def setUp(self):
        super().setUp()
        rf.add_list_row(self.world, rf.REPORT, 2026, [1, 2], "DK_REF", "shipped")
        rf.add_qms(self.world, "2026-04-10 12:00:00")
        self.scene(dict(rf.typed_row(5, ["1", "2"]), **rf.typed_row(6, ["0"] * 12)),
                   [rf.list_binding(5, count=2), rf.qms_binding(6)])

    def failing(self, expected_code, *extra, contains=None):
        rc, out, err = self.run_rec("--out", self.out_path, *extra)
        self.assertEqual(rc, expected_code, err)
        self.assertEqual(out, "")                                                   # nothing printed before a failure
        self.assertTrue(err.startswith("error: ") and len(err.splitlines()) == 1, err)
        self.assertFalse(os.path.exists(self.out_path), "a failed run must not write the report")
        for secret in (FAKE_KEY,):
            self.assertNotIn(secret, err)
        self.assertEqual(sorted(n for n in os.listdir(self.work) if n.startswith(".intake-")), [])
        if contains:
            self.assertIn(contains, err)
        return err

    def test_a_wrong_login_is_exit_one(self):
        self.fake.mode = "login_false"
        self.failing(1, contains="login failed")

    def test_a_server_that_echoes_the_key_in_its_error_never_shows_it(self):
        for mode in ("error_echo", "error_echo_top", "http_500"):
            with self.subTest(mode=mode):
                self.fake.mode = mode
                self.failing(1)

    def test_other_transport_failures_are_exit_one(self):
        for mode in ("cf_1010", "cf_other", "http_403_plain", "non_json", "json_list", "cf_502", "deep_json"):
            with self.subTest(mode=mode):
                self.fake.mode = mode
                self.failing(1)

    def test_a_server_that_cannot_be_reached_is_exit_one(self):
        self.fake.stop()
        self.failing(1, contains="cannot reach")

    def test_an_odoo_error_after_the_login_never_shows_the_key(self):
        for model, method in ((rf.REPORT, "search_read"), (rf.QMS, "read_group")):
            with self.subTest(read=model):
                self.world.answer_override = {(model, method): DocumentsError("access refused for key {key}")}
                err = self.failing(1, "--tz", "UTC")
                self.assertIn("access refused", err)
                self.assertIn("***", err)                                           # the client's own masking

    def test_an_odoo_error_while_reading_the_time_zone_never_shows_the_key(self):
        self.world.users_read_override = DocumentsError("tz refused {key}")
        self.assertIn("tz refused", self.failing(1))
        self.world.users_read_override = UNSET
        self.world.fields_get_override = DocumentsError("selection refused {key}")
        self.assertIn("selection refused", self.failing(1))

    def test_every_input_problem_is_exit_two_and_never_shows_the_key(self):
        self.fake.mode = "error_echo"                                               # a key echo is armed, yet no RPC happens
        os.remove(self.dashboard_path)
        self.failing(2, contains="dashboard")
        self.put(self.dashboard_path, self.dashboard_bytes)
        self.put(self.spec_path, b"{broken")
        self.failing(2)

    def test_a_missing_config_is_exit_two_and_names_no_key(self):
        for name in odoo_rpc.ENV_NAMES.values():
            os.environ.pop(name, None)
        self.failing(2, contains="no config")

    def test_a_config_file_with_open_permissions_is_exit_two_and_the_key_stays_out_of_the_message(self):
        if os.name == "nt":
            self.skipTest("POSIX mode bits only")
        for name in odoo_rpc.ENV_NAMES.values():
            os.environ.pop(name, None)
        config = write_file(self.work, ini_text(self.fake.url), mode=0o644, name="open.ini")
        self.failing(2, "--config", config)

    def test_an_unexpected_exception_is_one_line_with_no_traceback_and_no_exception_text(self):
        with mock.patch.object(reconcile_kpi, "fetch_lists", side_effect=KeyError("SECRET-" + FAKE_KEY)):
            err = self.failing(1)
        self.assertEqual(err.strip(), "error: unexpected KeyError")

    def test_a_recursion_error_is_an_input_problem_exit_two(self):
        with mock.patch.object(reconcile_kpi, "fetch_lists", side_effect=RecursionError()):
            rc, out, err = self.run_rec()
        self.assertEqual((rc, out, err), (2, "", "error: the input is nested too deeply\n"))

    def test_an_interrupt_is_exit_130_with_one_word(self):
        with mock.patch.object(reconcile_kpi, "fetch_lists", side_effect=KeyboardInterrupt()):
            rc, out, err = self.run_rec()
        self.assertEqual((rc, out, err), (130, "", "interrupted\n"))

    def test_the_key_is_in_no_stream_and_no_file_on_a_run_that_succeeds_either(self):
        self.world.users[FAKE_UID]["tz"] = "UTC"
        out = self.reconcile("--out", self.out_path)
        self.assertNotIn(FAKE_KEY, out)
        for name in os.listdir(self.work):
            with open(os.path.join(self.work, name), "rb") as handle:
                self.assertNotIn(FAKE_KEY.encode("utf-8"), handle.read(), name)


class ServerAnswerShapeTests(ReconcileCase):
    """A malformed answer from the server is reported in one line, never as a traceback; it is a server problem, so exit 1."""

    def check(self, override, qms=False):
        self.world.users[FAKE_UID]["tz"] = "UTC"
        rf.add_list_row(self.world, rf.REPORT, 2026, [1], "DK_REF", "shipped")
        rf.add_qms(self.world, "2026-04-10 12:00:00")
        bindings = [rf.list_binding(5, count=1)] + ([rf.qms_binding(6)] if qms else [])
        self.scene(dict(rf.typed_row(5, ["1"]), **(rf.typed_row(6, ["0"] * 12) if qms else {})), bindings)
        self.world.answer_override = override
        return self.run_rec()

    CASES = (
                ("list not a list", {(rf.REPORT, "search_read"): {"rows": []}}, False),
                ("list null", {(rf.REPORT, "search_read"): None}, False),
                ("groups null", {(rf.QMS, "read_group"): None}, True),
                ("list row not a dict", {(rf.REPORT, "search_read"): [123]}, False),
                ("list number is a list", {(rf.REPORT, "search_read"): [{"month_1": [1]}]}, False),
                ("list number is text-true", {(rf.REPORT, "search_read"): [{"month_1": True}]}, False),
                ("groups not a list", {(rf.QMS, "read_group"): "x"}, True),
                ("group not a dict", {(rf.QMS, "read_group"): [5]}, True),
                ("group without a range", {(rf.QMS, "read_group"): [{"__count": 1}]}, True),
                ("range from garbage", {(rf.QMS, "read_group"): [{"__count": 1, "__range": {
                    "check_date:month": {"from": "garbage", "to": "x"}}}]}, True),
                ("a count that is text", {(rf.QMS, "read_group"): [{"__count": "many", "__range": {
                    "check_date:month": {"from": "2026-03-31 15:00:00", "to": "x"}}}]}, True),
                # the NULL-date skip is for `__range[group] is False` only: every other odd shape is still refused
                ("range is text", {(rf.QMS, "read_group"): [{"__count": 1, "__range": "x"}]}, True),
                ("range is a list", {(rf.QMS, "read_group"): [{"__count": 1, "__range": [1]}]}, True),
                ("range is null", {(rf.QMS, "read_group"): [{"__count": 1, "__range": None}]}, True),
                ("range entry is null", {(rf.QMS, "read_group"): [{"__count": 1, "__range": {
                    "check_date:month": None}}]}, True),
                ("range entry is zero", {(rf.QMS, "read_group"): [{"__count": 1, "__range": {
                    "check_date:month": 0}}]}, True),
                ("range entry is true", {(rf.QMS, "read_group"): [{"__count": 1, "__range": {
                    "check_date:month": True}}]}, True),
                ("range entry is text", {(rf.QMS, "read_group"): [{"__count": 1, "__range": {
                    "check_date:month": "2026-03-31"}}]}, True),
                ("range entry is an empty object", {(rf.QMS, "read_group"): [{"__count": 1, "__range": {
                    "check_date:month": {}}}]}, True),
                ("range entry has no from", {(rf.QMS, "read_group"): [{"__count": 1, "__range": {
                    "check_date:month": {"to": "2026-04-30 15:00:00"}}}]}, True),
                ("range names another group", {(rf.QMS, "read_group"): [{"__count": 1, "__range": {
                    "other:month": False}}]}, True),
                ("range is empty", {(rf.QMS, "read_group"): [{"__count": 1, "__range": {}}]}, True),
                # a search_read asked with limit 1 that answers more than one row
                ("list answers two rows", {(rf.REPORT, "search_read"): [
                    {"id": 1, "group_code": "DK_REF", "kind": "shipped", "month_1": 1.0},
                    {"id": 2, "group_code": "DK_REF", "kind": "shipped", "month_1": 2.0}]}, False),
                ("list answers many rows", {(rf.REPORT, "search_read"): [
                    {"id": n, "group_code": "DK_REF", "kind": "shipped", "month_1": 1.0} for n in range(1, 60)]}, False))

    def test_a_malformed_server_answer_is_refused_in_one_line_with_nothing_on_stdout(self):
        for label, override, qms in self.CASES:
            with self.subTest(answer=label):
                rc, out, err = self.check(override, qms)
                self.assertNotEqual(rc, 0)
                self.assertEqual(out, "")
                self.assertTrue(err.startswith("error: ") and len(err.splitlines()) == 1, err)
                self.assertIn("the server answered", err)             # a named refusal, not "unexpected KeyError"

    def test_a_malformed_server_answer_is_an_rpc_problem_exit_one(self):
        # Plan B1: "Exit codes: 2 = input / spec problem (BindError, stale sha, dashboard bytes differ, bad file),
        # 1 = RPC or tz error": a server that answers a wrong shape is not the user's input
        wrong = []
        for label, override, qms in self.CASES:
            self.clear_rows(rf.REPORT)
            self.clear_rows(rf.QMS)
            rc, out, err = self.check(override, qms)
            if rc != 1:
                wrong.append((label, rc, err.strip()))
        self.assertEqual(wrong, [])


class KnownBugTests(ReconcileCase):
    """Defects found by these tests in scripts/reconcile_kpi.py (reported to main; the tests stay as they are)."""

    def test_a_differing_cell_is_listed_with_the_excel_number_not_a_text_marker(self):
        # Plan B1: "the first 50 differ/error cells as `ref, Excel value, live value` - refs and numbers only"
        rf.add_list_row(self.world, rf.REPORT, 2026, [101, 2.506], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["100", "2.50"]), [rf.list_binding(5, count=2)])
        out = self.reconcile()
        shown = [(m.group(1), m.group(2), m.group(3)) for m in map(CELL.match, out.splitlines()) if m]
        self.assertEqual(shown, [("D5", "100", "101"), ("E5", "2.50", "2.506")])

    def test_a_typed_number_too_large_for_a_float_is_not_equal_to_everything(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [5, 5], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["1E400", "-1E400"]), [rf.list_binding(5, count=2)])
        counts = counts_of(self.reconcile())
        self.assertEqual((counts.equal, counts.rounded, counts.differ), (0, 0, 2))

    def test_out_never_replaces_the_spec_or_the_dashboard_file_either(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1], "DK_REF", "shipped")
        overwritten = []
        for name in ("spec", "dashboard"):
            self.scene(rf.typed_row(5, ["1"]), [rf.list_binding(5, count=1)])        # (re)write the three inputs
            path = {"spec": self.spec_path, "dashboard": self.dashboard_path}[name]
            before = self.read(path)
            rc, out, err = self.run_rec("--out", path, "--force")
            if self.read(path) != before:
                overwritten.append((name, rc))
        self.assertEqual(overwritten, [], "the report replaced an input file (bind_kpi.py refuses this)")


class CombinedReviewFixTests(ReconcileCase):
    """The fixes of the combined review (R2): a one-row answer, a bounded typed text, a huge typed integer, `--out` first."""

    def test_a_search_read_that_answers_two_rows_for_limit_one_is_one_static_line_exit_one(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["1"]), [rf.list_binding(5, count=1)])
        self.world.answer_override = {(rf.REPORT, "search_read"): [
            {"id": 1, "group_code": "DK_REF", "kind": "shipped", "month_1": 1.0},
            {"id": 2, "group_code": "SENTINEL-SECOND-ROW", "kind": "shipped", "month_1": 987654.0}]}
        rc, out, err = self.run_rec("--out", self.out_path)
        self.assertEqual((rc, out), (1, ""))
        self.assertRegex(err, r"^error: the server answered an unexpected shape for list \d+\n$")
        self.assertNotIn("SENTINEL", err)
        self.assertNotIn("987654", err)
        self.assertFalse(os.path.exists(self.out_path))

    def test_a_search_read_that_answers_exactly_one_row_or_none_is_accepted(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["1"]), [rf.list_binding(5, count=1)])
        self.assertEqual(counts_of(self.reconcile()), Counts(1, 1, 1, 0, 0, 0, 0, 0, 0))
        self.world.answer_override = {(rf.REPORT, "search_read"): []}
        self.assertEqual(counts_of(self.reconcile()), Counts(1, 1, 0, 0, 1, 0, 0, 0, 0))     # no row: reads 0, typed 1

    def put_typed(self, text, live=5, cell="D5"):
        """One typed cell holding `text` (any JSON value) against the live number `live`; returns the baseline path."""
        rf.add_list_row(self.world, rf.REPORT, 2026, [live], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["9"]), [rf.list_binding(5, count=1)])
        baseline = json.loads(intake_xlsx.dump_bytes(self.baseline).decode("utf-8"))
        baseline["sheets"][0]["cells"][cell]["content"] = text
        self.put(self.baseline_path, intake_xlsx.dump_bytes(baseline))
        self.put(self.dashboard_path, rf.produce(baseline, self.spec)[2])

    def test_the_typed_text_of_a_differing_cell_is_cut_at_forty_characters_with_an_ellipsis(self):
        digits = "٣" * 5000                              # Arabic-Indic three: `bind_kpi.NUMBER` takes any Unicode digit
        self.put_typed(digits)
        out = self.reconcile("--out", self.out_path)
        (line,) = [item for item in out.splitlines() if item.startswith("D5: ")]
        self.assertEqual(line, "D5: Excel %s, live 5 (differ)" % ("٣" * 39 + "…"))
        self.assertLess(len(out), 400)                        # nothing near the 5000 digits reached the screen
        report = json.loads(self.read(self.out_path).decode("utf-8"))
        self.assertEqual(report["cells"], [{"cell": "D5", "excel": "٣" * 39 + "…", "live": "5", "class": "differ"}])

    def test_a_typed_text_of_forty_characters_is_shown_whole_and_forty_one_is_cut(self):
        for length, expected in ((40, "7" * 40), (41, "7" * 39 + "…"), (39, "7" * 39)):
            with self.subTest(length=length):
                self.clear_rows(rf.REPORT)
                self.put_typed("7" * length)
                (line,) = [item for item in self.reconcile().splitlines() if item.startswith("D5: ")]
                self.assertEqual(line, "D5: Excel %s, live 5 (differ)" % expected)

    def test_the_cut_function_itself_keeps_forty_and_cuts_forty_one_with_an_ellipsis_as_the_fortieth(self):
        self.assertEqual(reconcile_kpi._show_typed("1" * 40), "1" * 40)
        self.assertEqual(reconcile_kpi._show_typed("1" * 41), "1" * 39 + "…")
        self.assertEqual(len(reconcile_kpi._show_typed("1" * 100000)), 40)
        self.assertEqual(reconcile_kpi._show_typed(12.5), "12.5")

    def test_an_error_cell_shows_its_typed_text_cut_too(self):
        long_number = "١" * 90
        with mock.patch.object(validate_formulas.SheetEvaluator, "value", side_effect=validate_formulas.SheetError("x")):
            self.put_typed(long_number)
            out = self.reconcile()
        shown = [item for item in out.splitlines() if item.startswith("D5: ")]
        self.assertEqual(shown, ["D5: Excel %s, live error:SheetError (error)" % ("١" * 39 + "…")])

    def test_a_typed_json_integer_above_1e308_is_a_differing_cell_never_a_crash(self):
        for text in (10 ** 400, -10 ** 400, 10 ** 309):
            with self.subTest(typed=str(text)[:12]):
                self.clear_rows(rf.REPORT)
                self.put_typed(text)
                rc, out, err = self.run_rec()
                self.assertEqual((rc, err), (0, ""))
                self.assertEqual(counts_of(out), Counts(1, 1, 0, 0, 1, 0, 0, 0, 0))
                (line,) = [item for item in out.splitlines() if item.startswith("D5: ")]
                self.assertTrue(line.endswith(", live 5 (differ)"), line)
                self.assertLess(len(line), 80)

    def test_classify_keeps_a_huge_integer_apart_from_every_live_number(self):
        for live in (0.0, 5.0, 1e308, -1e308, "-"):
            with self.subTest(live=live):
                self.assertEqual(reconcile_kpi.classify(10 ** 400, live), "differ")
                self.assertEqual(reconcile_kpi.classify(-10 ** 400, live), "differ")
        self.assertEqual(reconcile_kpi.classify(10 ** 308, 1e308), "equal")       # still a float: compared as usual
        self.assertEqual(reconcile_kpi.classify("1E400", 5.0), "differ")

    def test_an_existing_out_without_force_is_refused_before_any_rpc_and_before_anything_is_printed(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1, 9], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["1", "2"]), [rf.list_binding(5, count=2)])
        self.put(self.out_path, b"previous report")
        rc, out, err = self.run_rec("--out", self.out_path)
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(err, "error: %s exists already: pass --force to replace it\n" % self.out_path)
        self.assertEqual(self.rpc_calls(), 0)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.read(self.out_path), b"previous report")
        self.assertEqual(sorted(n for n in os.listdir(self.work) if n.startswith(".intake-")), [])

    def test_an_existing_out_without_force_is_refused_before_the_pivot_reads_too(self):
        self.set_tz("UTC")
        rf.add_qms(self.world, "2026-04-10 12:00:00")
        self.scene(rf.typed_row(5, ["0"] * 12), [rf.qms_binding(5)])
        self.put(self.out_path, b"previous report")
        rc, out, err = self.run_rec("--out", self.out_path)
        self.assertEqual((rc, out, self.rpc_calls()), (2, "", 0))
        self.assertIn("pass --force", err)

    def test_an_out_that_does_not_exist_yet_is_written_and_force_replaces_one_that_does(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["1"]), [rf.list_binding(5, count=1)])
        self.assertTrue(self.reconcile("--out", self.out_path))
        self.assertEqual(json.loads(self.read(self.out_path).decode("utf-8"))["year"], 2026)
        self.put(self.out_path, b"previous report")
        self.assertTrue(self.reconcile("--out", self.out_path, "--force"))
        self.assertEqual(json.loads(self.read(self.out_path).decode("utf-8"))["year"], 2026)

    def test_out_equal_to_an_input_file_is_refused_first_with_the_input_message_with_or_without_force(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["1"]), [rf.list_binding(5, count=1)])
        for name, path in (("baseline", self.baseline_path), ("spec", self.spec_path),
                           ("dashboard", self.dashboard_path)):
            before = self.read(path)
            for extra in ((), ("--force",)):
                with self.subTest(out=name, force=bool(extra)):
                    rc, out, err = self.run_rec("--out", path, *extra)
                    self.assertEqual((rc, out), (2, ""))
                    self.assertEqual(err, "error: --out is an input file (%s): refusing to overwrite it\n"
                                     % os.path.basename(path))
                    self.assertEqual(self.rpc_calls(), 0)
                    self.assertEqual(self.read(path), before)

    @unittest.skipIf(os.name == "nt", "symlinks need POSIX here")
    def test_out_that_is_a_link_to_an_input_file_is_refused_first_too(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1], "DK_REF", "shipped")
        self.scene(rf.typed_row(5, ["1"]), [rf.list_binding(5, count=1)])
        link = os.path.join(self.work, "link.json")
        os.symlink(self.spec_path, link)
        before = self.read(self.spec_path)
        rc, out, err = self.run_rec("--out", link)
        self.assertEqual((rc, out, self.rpc_calls()), (2, "", 0))
        self.assertIn("--out is an input file (spec.json)", err)
        self.assertNotIn("--force", err)
        self.assertEqual(self.read(self.spec_path), before)


if __name__ == "__main__":
    unittest.main()
