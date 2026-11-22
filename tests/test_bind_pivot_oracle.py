"""AC2 (pivot and ratio part), AC3 and the time bound of step 8b: the dashboard that `bind()` generates is EVALUATED by
`validate_formulas.SheetEvaluator(doc, pivots=...)` over a small table of invented records, and every number must equal
the expectation computed here by plain Python from the same records (never from the formulas).

The pivot engine below models the client for what the binder generates: a pivot with `colGroupBys ["<date>:month"]` and
no row group, domain terms `=` and `in`; a pivot that carries a `fieldMatching` for the Year filter (the PROBE) sees only
the records of the filter year, every other pivot (a DATA pivot) sees all years. It never looks at the sheet formulas.
"""
import copy
import datetime
import itertools
import json
import re
import time
import unittest

import bind_fixtures as bf
from bind_fixtures import derived, hist, other
from helpers import REPO  # noqa: F401

import bind_kpi
import intake_xlsx
import validate_formulas


def H(year, month, form="a", zone="N", result="OK", grade="X", qty=10, ng_qty=0, day=3):
    return {"when": datetime.date(year, month, day), "form": form, "zone": zone, "result": result, "grade": grade,
            "qty": qty, "ng_qty": ng_qty}


def many(count, year, month, **kwargs):
    return [H(year, month, **dict(kwargs, qty=kwargs.get("qty", 10) + i, day=1 + i % 28)) for i in range(count)]


HIST = (
    many(2, 2026, 1, qty=10, ng_qty=1)                                   # a/N January: 2 records, qty 10 + 11
    + many(3, 2026, 3, qty=20, ng_qty=2, result="NG")                    # March: 3
    + many(5, 2026, 10)                                                   # October: 5
    + many(1, 2026, 12, qty=7)                                            # December: 1
    + many(1, 2026, 1, zone="S")                                          # another zone: never counted for a/N
    + many(2, 2026, 3, form="c")                                          # another form
    + many(2, 2026, 10, form="b", zone="S", result="OK", ng_qty=2)        # b/S October: OK x2
    + many(1, 2026, 10, form="b", zone="S", result="Pass", ng_qty=1)
    + many(4, 2026, 10, form="b", zone="S", result="NG", ng_qty=5)
    + many(3, 2026, 11, form="b", zone="S", result="Pass", ng_qty=4)
    + many(1, 2025, 1) + many(4, 2025, 2) + many(3, 2025, 12)             # a/N 2025
    + many(6, 2025, 10, form="b", zone="S", result="OK"))
OTHER = ([{"day": datetime.date(2025, 1, 5), "amount": 5}, {"day": datetime.date(2025, 1, 9), "amount": 7},
          {"day": datetime.date(2025, 6, 1), "amount": 1}])
VAULT = [{"stamp": datetime.date(2026, 3, 2)}, {"stamp": datetime.date(2026, 3, 9)},
         {"stamp": datetime.date(2026, 10, 1)}]
DATA = {"t.hist": HIST, "t.other": OTHER, "t.vault": VAULT}
DATE_FIELDS = {"t.hist": "when", "t.other": "day", "t.vault": "stamp"}


def pivots_for(document, data, year):
    """{pivot id: evaluator pivot} for the Year filter set to `year`."""
    pivots = {}
    for pivot_id, definition in document["pivots"].items():
        field = DATE_FIELDS[definition["model"]]
        assert definition["colGroupBys"] == [field + ":month"], definition["colGroupBys"]
        assert definition["rowGroupBys"] == []
        matched = definition["fieldMatching"]
        assert matched == {} or matched == {"kpi-year-filter": {"chain": field, "type": "date"}}, matched
        months = {}
        for record in data[definition["model"]]:
            if matched and record[field].year != year:
                continue
            if not all((record[name] == value) if op == "=" else (record[name] in value)
                       for name, op, value in definition["domain"]):
                continue
            column = months.setdefault("%02d/%04d" % (record[field].month, record[field].year), [])
            column.append(record)
        measures = [m["field"] for m in definition["measures"]]
        pivots[int(pivot_id)] = {"measures": measures, "months": {
            key: {m: float(len(rows)) if m == "__count" else float(sum(r.get(m, 0) for r in rows)) for m in measures}
            for key, rows in months.items()}}
    return pivots


def evaluate(document, year, data=DATA):
    return validate_formulas.SheetEvaluator(document, pivots=pivots_for(document, data, year)).evaluate_all()


def monthly(model, year, measure="__count", **match):
    """The twelve monthly totals of the records of `model` in `year`, computed straight from the records."""
    field = DATE_FIELDS[model]
    out = []
    for month in range(1, 13):
        rows = [r for r in DATA[model] if r[field].year == year and r[field].month == month
                and all(r[k] in v if isinstance(v, tuple) else r[k] == v for k, v in match.items())]
        out.append(float(len(rows)) if measure == "__count" else float(sum(r[measure] for r in rows)))
    return out


def dash(values):
    return ["-" if v == 0 else v for v in values]


def ratios(nums, dens):
    return [n / d if d > 0 else "N/A" for n, d in zip(nums, dens)]


MONTHS = "DEFGHIJKLMNO"
AREA_ROWS = 14


def oracle_spec():
    return [
        hist("D5:O5", zero="dash"),                                                       # a/N count
        hist("D6:O6", zero="dash", year="previous"),                                      # a/N count, year before
        hist("D7:O7", measure="qty"),                                                     # a/N qty, number cells
        hist("D8:O8", form="b", zone="S", measure="ng_qty", filter={"result": ["OK", "Pass"]}),
        other("D9:O9", measure="amount", zero="dash"),
        {"cells": "D10:O10", "source": "vault"},
        derived("D11:O11", "ratio", of="D7:O7", over="D5:O5"),                            # qty per form (den = helper)
        derived("D12:O12", "prev_period", of="D5:O5"),
        derived("D13:O13", "ratio", of="D5:O5", over="D6:O6"),                            # this year / year before
        derived("P5", "sum", of="D5:O5"), derived("P6", "sum", of="D6:O6"), derived("P7", "sum", of="D7:O7"),
        derived("P8", "sum", of="D8:O8"), derived("P9", "sum", of="D9:O9"), derived("P10", "sum", of="D10:O10"),
        hist("P11", month=10, zero="dash"), hist("P12", month=12, year="previous"),
        hist("P13", month=1, filter={"result": ["NG"]}),
    ]


def oracle_baseline():
    return bf.empty_baseline(row_number=AREA_ROWS)


class OracleBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document, cls.report = bf.bind(bf.make_spec(oracle_spec()), oracle_baseline(), bf.pivot_catalog())
        cls.values = {year: evaluate(cls.document, year) for year in (2026, 2025, 2027, 2024)}

    def row(self, number, year=2026, columns=MONTHS):
        return [self.values[year]["%s%d" % (c, number)] for c in columns]

    def year_cells(self, year=2026):
        """{pivot source model: the value of its hidden year cell}."""
        cells = self.document["sheets"][0]["cells"]
        found = {}
        for ref, cell in cells.items():
            match = re.match(r"=IFERROR\(YEAR\(ODOO\.PIVOT\.HEADER\((\d+),", cell.get("content", ""))
            if ref.startswith("A") and match:
                found[self.document["pivots"][match.group(1)]["model"]] = self.values[year][ref]
        return found


class PivotOracleTests(OracleBase):
    def test_every_cell_evaluates_without_an_error_for_every_filter_year(self):
        for year, values in self.values.items():
            self.assertEqual(set(values), set(self.document["sheets"][0]["cells"]), year)

    def test_months_with_data_and_months_without_show_the_number_and_a_dash(self):
        expected = dash(monthly("t.hist", 2026, form="a", zone="N"))
        self.assertEqual(self.row(5), expected)
        self.assertEqual(self.row(5), [2.0, "-", 3.0, "-", "-", "-", "-", "-", "-", 5.0, "-", 1.0])   # by hand too

    def test_the_previous_year_row_reads_the_year_before(self):
        self.assertEqual(self.row(6), dash(monthly("t.hist", 2025, form="a", zone="N")))
        self.assertEqual(self.row(6), [1.0, 4.0, "-", "-", "-", "-", "-", "-", "-", "-", "-", 3.0])

    def test_a_number_cell_shows_zero_and_never_a_dash_and_sums_the_measure(self):
        expected = monthly("t.hist", 2026, "qty", form="a", zone="N")
        self.assertEqual(self.row(7), expected)
        self.assertEqual(self.row(7)[0], 21.0)                                        # 10 + 11
        self.assertEqual(self.row(7)[1], 0.0)
        self.assertIsInstance(self.row(7)[1], float)

    def test_the_filter_keeps_only_the_listed_results(self):
        expected = monthly("t.hist", 2026, "ng_qty", form="b", zone="S", result=("OK", "Pass"))
        self.assertEqual(self.row(8), expected)
        self.assertEqual(self.row(8)[9], 5.0)                          # October: OK 2 x 2 + Pass 1; the 4 NG rows are out
        self.assertEqual(self.row(8)[10], 12.0)                        # November: 3 Pass records x 4

    def test_a_filter_that_matches_nothing_reads_zero(self):
        self.assertEqual(self.row(13, columns="P"), [0.0])      # NG records of a/N exist only in March: January is 0

    def test_a_single_month_cell_and_a_previous_year_single_month_cell(self):
        self.assertEqual(self.row(11, columns="P"), [5.0])             # P11: October of a/N, a dash cell
        self.assertEqual(self.row(11, columns="P"), [monthly("t.hist", 2026, form="a", zone="N")[9]])
        self.assertEqual(self.row(12, columns="P"), [3.0])             # P12: December of the year before
        self.assertEqual(self.row(12, columns="P"), [monthly("t.hist", 2025, form="a", zone="N")[11]])
        self.assertEqual(self.row(11, 2027, columns="P"), ["-"])

    def test_a_pivot_source_reads_its_own_year_cell(self):
        self.assertEqual(self.year_cells(2026), {"t.hist": 2026.0, "t.other": 1899.0, "t.vault": 2026.0})
        self.assertEqual(self.year_cells(2025), {"t.hist": 2025.0, "t.other": 2025.0, "t.vault": 1899.0})

    def test_a_source_with_no_data_in_the_filter_year_reads_zero_while_the_others_keep_reading(self):
        self.assertEqual(self.row(9), ["-"] * 12)                                      # `other` has records of 2025 only
        self.assertEqual(self.row(5)[0], 2.0)
        self.assertEqual(self.row(9, 2025), dash(monthly("t.other", 2025, "amount")))
        self.assertEqual(self.row(9, 2025)[0], 12.0)                                   # 5 + 7
        self.assertEqual(self.row(9, 2025)[5], 1.0)

    def test_a_restricted_source_reads_its_numbers(self):
        self.assertEqual(self.row(10), monthly("t.vault", 2026))
        self.assertEqual(self.row(10)[2], 2.0)
        self.assertEqual(self.row(10, 2025), [0.0] * 12)                               # no year, no data: 0 (raw `""` + 0)

    def test_the_other_filter_years_move_every_pivot_read(self):
        self.assertEqual(self.row(5, 2025), dash(monthly("t.hist", 2025, form="a", zone="N")))
        self.assertEqual(self.row(6, 2025), dash(monthly("t.hist", 2024, form="a", zone="N")))     # nothing in 2024
        self.assertEqual(self.row(6, 2025), ["-"] * 12)

    def test_a_year_with_no_data_reads_zero_everywhere_even_the_previous_year_row(self):
        # the documented limitation: the probe has no month column, so the year cell is 1899 and the previous-year
        # row reads 1898 although the year before (2026 for the filter 2027) HAS data
        for year in (2027, 2024):
            with self.subTest(year=year):
                self.assertEqual(self.year_cells(year)["t.hist"], 1899.0)
                self.assertEqual(self.row(5, year), ["-"] * 12)
                self.assertEqual(self.row(6, year), ["-"] * 12)
                self.assertEqual(self.row(7, year), [0.0] * 12)
                self.assertEqual(self.row(8, year), [0.0] * 12)
        self.assertEqual(monthly("t.hist", 2026, form="a", zone="N")[0], 2.0)             # 2026 does have data

    def test_the_filter_year_never_changes_the_data_pivots_only_the_probe(self):
        for year in (2026, 2025, 2027):
            pivots = pivots_for(self.document, DATA, year)
            data_pivot = pivots[1]["months"]
            self.assertIn("01/2026", data_pivot)
            self.assertIn("01/2025", data_pivot)                                          # all years, whatever the filter


class DerivedOracleTests(OracleBase):
    def test_ratio_with_a_zero_denominator_is_n_a_and_a_zero_numerator_is_zero(self):
        counts = monthly("t.hist", 2026, form="a", zone="N")
        qty = monthly("t.hist", 2026, "qty", form="a", zone="N")
        expected = ratios(qty, counts)
        self.assertEqual(self.row(11), expected)
        self.assertEqual(self.row(11)[1], "N/A")                                        # February: no form
        self.assertAlmostEqual(self.row(11)[0], 21.0 / 2)
        self.assertAlmostEqual(self.row(11)[2], (20 + 21 + 22) / 3.0)

    def test_a_ratio_over_dash_cells_reads_the_numbers_not_the_dash_text(self):
        # D5 shows "-" in February but the ratio D13 (this year / year before) reads the helper cells: 0 / 4 = 0
        self.assertEqual(self.row(5)[1], "-")
        self.assertEqual(self.row(6)[1], 4.0)
        self.assertEqual(self.row(13)[1], 0.0)
        self.assertEqual(self.row(13), ratios(monthly("t.hist", 2026, form="a", zone="N"),
                                              monthly("t.hist", 2025, form="a", zone="N")))
        self.assertEqual(self.row(13)[0], 2.0)                                          # 2 forms / 1 form
        self.assertEqual(self.row(13)[2], "N/A")                                        # no form the year before

    def test_prev_period_across_the_year_boundary_and_zero_months(self):
        counts = monthly("t.hist", 2026, form="a", zone="N")
        expected = ["N/A"] + [counts[i] / counts[i - 1] if counts[i - 1] > 0 else "N/A" for i in range(1, 12)]
        self.assertEqual(self.row(12), expected)
        self.assertEqual(self.row(12)[0], "N/A")                                        # January has no previous month,
        self.assertEqual(monthly("t.hist", 2025, form="a", zone="N")[11], 3.0)           # although December 2025 has 3
        self.assertEqual(self.row(12)[1], 0.0)                                          # February 0 forms / January 2
        self.assertEqual(self.row(12)[2], "N/A")                                        # March / February (0)
        self.assertEqual(self.row(12)[3], 0.0)                                          # April 0 / March 3
        self.assertEqual(self.row(12)[10], 0.0)                                         # November 0 / October 5
        self.assertEqual(self.row(12)[9], "N/A")                                        # October 5 / September (0)
        self.assertEqual(self.row(12)[11], "N/A")                                       # December 1 / November (0)

    def test_sum_adds_the_numbers_of_the_series_including_the_dash_cells(self):
        self.assertEqual(self.row(5, columns="P"), [sum(monthly("t.hist", 2026, form="a", zone="N"))])
        self.assertEqual(self.row(5, columns="P"), [11.0])
        self.assertEqual(self.row(6, columns="P"), [8.0])                               # 1 + 4 + 3
        self.assertEqual(self.row(7, columns="P"), [sum(monthly("t.hist", 2026, "qty", form="a", zone="N"))])
        self.assertEqual(self.row(8, columns="P"), [sum(monthly("t.hist", 2026, "ng_qty", form="b", zone="S",
                                                                result=("OK", "Pass")))])
        self.assertEqual(self.row(9, columns="P"), [0.0])                                # `other`: no 2026 data
        self.assertEqual(self.row(10, columns="P"), [3.0])
        self.assertEqual(self.row(9, 2025, columns="P"), [13.0])                         # 5 + 7 + 1

    def test_the_derived_cells_of_a_year_with_no_data(self):
        for year in (2027, 2024):
            with self.subTest(year=year):
                self.assertEqual(self.row(11, year), ["N/A"] * 12)
                self.assertEqual(self.row(12, year), ["N/A"] * 12)
                self.assertEqual(self.row(13, year), ["N/A"] * 12)
                self.assertEqual(self.row(5, year, columns="P"), [0.0])
                self.assertEqual(self.row(8, year, columns="P"), [0.0])

    def test_the_other_filter_year_moves_the_derived_cells_too(self):
        counts = monthly("t.hist", 2025, form="a", zone="N")
        self.assertEqual(self.row(12, 2025), ["N/A"] + [counts[i] / counts[i - 1] if counts[i - 1] > 0 else "N/A"
                                                         for i in range(1, 12)])
        self.assertEqual(self.row(5, 2025, columns="P"), [8.0])

    def test_no_derived_formula_reads_a_display_cell(self):
        cells = self.document["sheets"][0]["cells"]
        display = {ref for ref, cell in cells.items() if re.fullmatch(r'=IF\([A-Z]+\d+=0,"-",[A-Z]+\d+\)',
                                                                      cell.get("content", ""))}
        derived_refs = {"%s%d" % (c, r) for c in MONTHS for r in (11, 12, 13)} | {"P5", "P6", "P7", "P8", "P9", "P10"}
        self.assertEqual(len(display), 12 * 3 + 1)                       # dash cells: rows 5, 6, 9 and P11
        self.assertTrue(derived_refs <= set(cells))
        for ref, cell in cells.items():
            if not cell.get("content", "").startswith("="):
                continue
            read = bf.reference_scan(cell["content"])
            self.assertFalse(read & display, (ref, read & display))
            self.assertFalse(read & derived_refs, (ref, read & derived_refs))

class BrowserScenarioReplayTests(unittest.TestCase):
    """The shape of the real-browser check on the development database (PQC October 53 and November 10, previous
    year all dashes, sum 63, a second filtered series October 2 and November 9, a ratio showing N/A, the previous-period
    ratio of November 0.1887), replayed over invented records and the synthetic catalog."""

    DATA = {
        "t.hist": ([H(2026, 10, day=1 + i % 28) for i in range(53)] + [H(2026, 11, day=1 + i % 28) for i in range(10)]
                   + [H(2026, 10, form="b", zone="S", result=result, day=1 + i)
                      for i, result in enumerate(("OK", "Pass", "NG", "NG"))]
                   + [H(2026, 11, form="b", zone="S", result="OK", day=1 + i) for i in range(5)]
                   + [H(2026, 11, form="b", zone="S", result="Pass", day=10 + i) for i in range(4)]),
        "t.other": [], "t.vault": []}

    @classmethod
    def setUpClass(cls):
        spec = bf.make_spec([
            hist("D5:O5", zero="dash"),
            hist("D6:O6", zero="dash", year="previous"),
            derived("P5", "sum", of="D5:O5"),
            hist("D8", month=10, form="b", zone="S", filter={"result": ["OK", "Pass"]}),
            hist("E8", month=11, form="b", zone="S", filter={"result": ["OK", "Pass"]}),
            hist("D10", month=1),                                                        # a/N January: no record
            derived("D9", "ratio", of="D8", over="D10"),
            derived("D11:O11", "prev_period", of="D5:O5")])
        document, _ = bf.bind(spec, bf.empty_baseline(row_number=12), bf.pivot_catalog())
        cls.values = evaluate(document, 2026, cls.DATA)

    def test_counts_dashes_and_the_sum(self):
        row5 = [self.values["%s5" % c] for c in MONTHS]
        self.assertEqual(row5, ["-"] * 9 + [53.0, 10.0, "-"])
        self.assertEqual([self.values["%s6" % c] for c in MONTHS], ["-"] * 12)
        self.assertEqual(self.values["P5"], 63.0)

    def test_the_filtered_series_the_ratio_and_the_previous_period(self):
        self.assertEqual((self.values["D8"], self.values["E8"]), (2.0, 9.0))
        self.assertEqual(self.values["D9"], "N/A")                                       # over January: no record
        self.assertEqual(self.values["D10"], 0.0)
        self.assertAlmostEqual(self.values["N11"], 10 / 53, places=6)                   # November over October
        self.assertAlmostEqual(self.values["N11"], 0.1887, places=4)
        self.assertEqual(self.values["M11"], "N/A")                                      # October over an empty September
        self.assertEqual(self.values["D11"], "N/A")                                      # January: no previous period


class TwoSourcesTests(unittest.TestCase):
    def test_two_pivot_sources_with_their_own_year_cells_and_probes(self):
        spec = bf.make_spec([hist("D5:O5"), other("D6:O6", measure="amount"), other("D7:O7")])
        document, _ = bf.bind(spec, bf.empty_baseline(), bf.pivot_catalog())
        expected = {2026: (2.0, 0.0, 0.0),        # `other` has no record of 2026: its probe is empty, it reads 0
                    2025: (1.0, 12.0, 2.0)}       # January 2025: one a/N form, amounts 5 + 7, two events
        for year, (hist_jan, amount_jan, count_jan) in expected.items():
            with self.subTest(year=year):
                values = evaluate(document, year)
                self.assertEqual((values["D5"], values["D6"], values["D7"]), (hist_jan, amount_jan, count_jan))
                self.assertEqual(sum(values["%s7" % c] for c in MONTHS), 3.0 if year == 2025 else 0.0)


# ---------------------------------------------------------------- AC3: determinism

class PivotDeterminismTests(unittest.TestCase):
    def spec(self):
        return bf.make_spec(oracle_spec())

    def bytes_of(self, spec=None, baseline=None):
        document, _ = bf.bind(spec or self.spec(), baseline or oracle_baseline(), bf.pivot_catalog())
        return bf.dumps(document)

    def test_two_runs_are_byte_identical(self):
        self.assertEqual(self.bytes_of(), self.bytes_of())

    def test_the_key_order_of_binding_objects_where_filter_and_derived_does_not_change_the_bytes(self):
        reference = self.bytes_of()
        spec = self.spec()
        for binding in spec["bindings"]:
            for key in ("where", "filter", "derived"):
                if key in binding:
                    binding[key] = dict(reversed(list(binding[key].items())))
        spec["bindings"] = [dict(reversed(list(b.items()))) for b in spec["bindings"]]
        spec = dict(reversed(list(spec.items())))
        self.assertEqual(self.bytes_of(spec), reference)

    def test_the_order_of_the_baseline_cells_and_the_catalog_does_not_change_the_bytes(self):
        reference = self.bytes_of()
        baseline = oracle_baseline()
        cells = baseline["sheets"][0]["cells"]
        baseline["sheets"][0]["cells"] = dict(reversed(list(cells.items())))
        self.assertEqual(self.bytes_of(baseline=baseline), reference)
        catalog = bf.pivot_catalog()
        catalog["sources"] = dict(reversed(list(catalog["sources"].items())))
        document, _ = bf.bind(self.spec(), oracle_baseline(), catalog)
        self.assertEqual(bf.dumps(document), reference)

    def test_the_bytes_are_utf8_lf_with_one_final_newline(self):
        data = self.bytes_of()
        self.assertTrue(data.endswith(b"}\n") and not data.endswith(b"\n\n"))
        self.assertNotIn(b"\r", data)
        json.loads(data.decode("utf-8"))

    def test_the_document_survives_a_json_round_trip(self):
        document, _ = bf.bind(self.spec(), oracle_baseline(), bf.pivot_catalog())
        self.assertEqual(bf.json_roundtrip(document), document)

    def test_binding_order_changes_the_ids_but_not_the_figures(self):
        bindings = oracle_spec()
        forward, _ = bf.bind(bf.make_spec(bindings), oracle_baseline(), bf.pivot_catalog())
        backward, _ = bf.bind(bf.make_spec(list(reversed(bindings))), oracle_baseline(), bf.pivot_catalog())
        self.assertNotEqual(bf.dumps(forward), bf.dumps(backward))                          # ids follow the first use
        a, b = evaluate(forward, 2026), evaluate(backward, 2026)
        for ref in ("D5", "O5", "D6", "D7", "D8", "D11", "E12", "D13", "P5", "P8", "P11"):
            self.assertEqual(a[ref], b[ref], ref)


# ---------------------------------------------------------------- time

class PivotTimingTests(unittest.TestCase):
    LIMIT = 2.0

    def test_500_mixed_bindings_against_a_5000_cell_baseline(self):
        cells = {}
        for row in range(1, 51):
            for col in range(1, 101):
                cells["%s%d" % (intake_xlsx.col_letters(col), row)] = {"content": "0", "style": 1}
        baseline = bf.make_baseline(cells=cells, merges=[], row_number=50, col_number=100)
        self.assertEqual(len(baseline["sheets"][0]["cells"]), 5000)
        catalog = bf.pivot_catalog()
        wide = catalog["sources"]["wide"]["column_groups"]["all"]
        bindings = []
        for row in range(1, 11):                                                          # 10 list bindings of 99 cells
            bindings.append({"cells": "B%d:CV%d" % (row, row), "source": "wide", "columns": wide[:99],
                             "zero": "dash" if row % 2 else "number"})
        forms = [(f, z) for f in "abc" for z in "NS"]
        for row in range(11, 31):                                                         # 8 x 20 pivot bindings
            for index in range(8):
                form, zone = forms[(row + index) % 6]
                first = intake_xlsx.col_letters(2 + index * 12)
                last = intake_xlsx.col_letters(13 + index * 12)
                bindings.append(hist("%s%d:%s%d" % (first, row, last, row), form=form, zone=zone,
                                     measure=("__count", "qty", "ng_qty")[index % 3],
                                     zero="dash" if index % 2 else "number",
                                     year="previous" if row % 3 == 0 else "current"))
        for row in range(31, 51):                                                         # 8 x 20 derived bindings
            for index in range(8):
                first = intake_xlsx.col_letters(2 + index * 12)
                last = intake_xlsx.col_letters(13 + index * 12)
                source_row = row - 20
                op = ("ratio", "prev_period", "ratio", "prev_period")[index % 4]
                fields = {"of": "%s%d:%s%d" % (first, source_row, last, source_row)}
                if op == "ratio":
                    fields["over"] = "%s%d:%s%d" % (first, source_row + 1 if source_row < 30 else 11, last,
                                                    source_row + 1 if source_row < 30 else 11)
                bindings.append(derived("%s%d:%s%d" % (first, row, last, row), op, **fields))
        free = (["A%d" % row for row in range(1, 51)]                                    # single cells to reach 500
                + ["%s%d" % (intake_xlsx.col_letters(col), row) for col in (98, 99, 100) for row in range(11, 51)])
        for index in range(500 - len(bindings)):
            bindings.append(derived(free[index], "sum", of="B%d:M%d" % (11 + index % 20, 11 + index % 20)))
        self.assertEqual(len(bindings), 500)
        spec = bf.make_spec(bindings, keep_typed=["A1:CV50"])
        start = time.perf_counter()
        document, report = bf.bind(spec, baseline, catalog)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, self.LIMIT, "bind took %.2f s" % elapsed)
        self.assertEqual(report["lists"], 1)
        self.assertGreater(report["pivots"], 3)
        self.assertGreater(report["derived_cells"], 1500)
        self.assertGreater(report["helper_rows"], 10)
        self.assertEqual(report["bound_cells"], 10 * 99 + 160 * 12)


if __name__ == "__main__":
    unittest.main()
