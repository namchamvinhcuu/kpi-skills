"""`bind()` and the helpers of scripts/bind_kpi.py: spec validation, cells, catalog resolution, the output structure
and the guards on the baseline. The AC2 oracle / determinism / timing tests are in test_bind_oracle.py, the command
line in test_bind_cli.py. Every input is built in Python (bind_fixtures.py): invented English text, no customer data.
"""
import copy
import hashlib
import json
import unittest
from unittest import mock

import bind_fixtures as bf
from helpers import REPO  # noqa: F401

import bind_kpi
import intake_xlsx

AC7 = "write a requirement text for a developer and stop"


class BindCase(unittest.TestCase):
    def refuse(self, spec=bf.DEFAULT, baseline=bf.DEFAULT, catalog=bf.DEFAULT, baseline_bytes=None, contains=(),
               exc=bind_kpi.BindError):
        """bind() must raise `exc`; the message must contain every text of `contains`; returns the message."""
        with self.assertRaises(exc) as ctx:
            bf.bind(spec, baseline, catalog, baseline_bytes)
        message = str(ctx.exception)
        for text in ([contains] if isinstance(contains, str) else contains):
            self.assertIn(text, message)
        return message

    def real(self, binding, cells=None, **spec_over):
        return bf.bind(bf.make_spec([binding], **spec_over), bf.make_baseline(cells=cells or {}), bf.real_catalog())

    def refuse_real(self, binding, contains=()):
        with self.assertRaises(bind_kpi.BindError) as ctx:
            self.real(binding)
        for text in ([contains] if isinstance(contains, str) else contains):
            self.assertIn(text, str(ctx.exception))
        return str(ctx.exception)


# ---------------------------------------------------------------- 1. spec validation

class SpecShapeTests(BindCase):
    def test_the_default_spec_binds(self):
        document, report = bf.bind()
        self.assertEqual(report["bound_cells"], 6)
        self.assertEqual(len(document["sheets"]), 1)

    def test_a_spec_that_is_not_an_object_is_refused(self):
        for bad in ([], "spec", None, 5, [make_one()]):
            with self.subTest(bad=bad):
                self.refuse(bad, contains="known keys")

    def test_an_unknown_top_level_key_is_refused(self):
        for key in ("extra", "Name", "binding", "keep_typed ", "spec-version"):
            with self.subTest(key=key):
                self.refuse(bf.make_spec(**{key: 1}), contains="known keys")

    def test_every_documented_key_is_accepted_and_the_intake_record_is_informational(self):
        spec = bf.make_spec(year_filter_label="Year", labels={"A5": "Row label"}, keep_typed=["D8"],
                            baseline_sha256=None, intake={"route": "download", "sheet": "synthetic"})
        document, _ = bf.bind(spec)
        self.assertNotIn("download", json.dumps(document))
        self.assertNotIn("synthetic", json.dumps(document))

    def test_spec_version_must_be_1(self):
        for bad in (2, 0, "1", None, [1], 1.5, -1):
            with self.subTest(version=bad):
                spec = bf.make_spec()
                spec["spec_version"] = bad
                self.refuse(spec, contains="spec_version must be 1")
        spec = bf.make_spec()
        del spec["spec_version"]
        self.refuse(spec, contains="spec_version must be 1")


class TextRuleTests(BindCase):
    """`name` (100) and `year_filter_label` (40): short single-line text that cannot start a formula."""

    BAD_TEXTS = ["", " ", " ", "a\nb", "a\rb", "a\tb", "a\x00b", "a\x1fb", "a\x7fb", "a\x85b", "=SUM(A1)", "+1",
                 "-1", "@x", " =x", "  =x", " =x", "=", "- x"]

    def check(self, field, limit):
        message = "`%s`" % field
        for bad in self.BAD_TEXTS + [None, 5, ["x"], {"x": 1}, True, "x" * (limit + 1), "y" * 1000]:
            with self.subTest(field=field, bad=bad):
                self.refuse(bf.make_spec(**{field: bad}), contains=message)

    def test_name_rules(self):
        self.check("name", 100)

    def test_year_filter_label_rules(self):
        self.check("year_filter_label", 40)

    def test_a_missing_name_is_refused(self):
        spec = bf.make_spec()
        del spec["name"]
        self.refuse(spec, contains="`name`")

    def test_the_limits_are_inclusive_and_text_is_kept_as_given(self):
        for text in ("x" * 100, "Báo cáo sản lượng 한국어", "a=b", "a-b+c", "1+1", "Q1 = 2026", "ok - fine",
                     "  padded name  "):
            with self.subTest(name=text):
                _, report = bf.bind(bf.make_spec(name=text))
                self.assertEqual(report["name"], text)
        for text in ("L" * 40, "Năm", "Year (2026)", " padded "):
            with self.subTest(label=text):
                document, _ = bf.bind(bf.make_spec(year_filter_label=text))
                self.assertEqual(document["globalFilters"][0]["label"], text)

    def test_the_default_filter_label_is_Year(self):
        document, _ = bf.bind()
        self.assertEqual(document["globalFilters"][0]["label"], "Year")

    def test_an_explicit_null_label_is_not_the_default(self):
        self.refuse(bf.make_spec(year_filter_label=None), contains="`year_filter_label`")

    def test_name_and_label_never_reach_a_formula_or_a_list(self):
        document, _ = bf.bind(bf.make_spec(name="ZXQ-NAME token", year_filter_label="ZXQ-LABEL token"))
        text = json.dumps(document)
        self.assertNotIn("ZXQ-NAME", text)                    # the name is the publisher's: not in the document at all
        self.assertEqual(text.count("ZXQ-LABEL"), 1)
        self.assertEqual(document["globalFilters"][0]["label"], "ZXQ-LABEL token")


def make_one():
    return bf.plain("D5:F5")


class BindingsListTests(BindCase):
    def test_bindings_must_be_a_list_of_1_to_500(self):
        for bad in ([], {}, "x", None, 5, {"cells": "D5"}, True):
            with self.subTest(bad=bad):
                spec = bf.make_spec()
                spec["bindings"] = bad
                self.refuse(spec, contains="1..500")
        spec = bf.make_spec()
        del spec["bindings"]
        self.refuse(spec, contains="1..500")

    def test_501_bindings_are_refused_before_the_baseline_is_looked_at(self):
        spec = bf.make_spec([{"cells": "A1", "source": "plain"}] * 501)
        self.refuse(spec, baseline={"sheets": []}, contains="1..500")

    def test_500_bindings_are_accepted(self):
        baseline = bf.make_baseline(cells={}, merges=[], row_number=500, col_number=1)
        bindings = [{"cells": "A%d" % i, "source": "secret", "column": "m1"} for i in range(1, 501)]
        document, report = bf.bind(bf.make_spec(bindings), baseline)
        self.assertEqual(report["bound_cells"], 500)
        self.assertEqual(report["lists"], 1)
        self.assertEqual(len(document["sheets"][0]["cells"]), 500)

    def test_a_binding_that_is_not_an_object_with_known_keys_is_refused(self):
        good = bf.plain("D5:F5")
        for bad in (5, "x", [], None, dict(good, extra=1), dict(good, Source="plain"), dict(good, **{"overwrite-text": 1})):
            with self.subTest(bad=bad):
                self.refuse(bf.make_spec([good, bad]), contains="binding 2 is not an object with known keys")

    def test_every_binding_key_is_known(self):
        binding = {"cells": "D5", "source": "plain", "where": {"code": "A"}, "year": "current", "column": "m1",
                   "zero": "number", "overwrite_text": False}
        bf.bind(bf.make_spec([binding]), bf.make_baseline(cells={}))
        several = {k: v for k, v in binding.items() if k != "column"}
        several.update(columns="months", cells="D5:F5")
        bf.bind(bf.make_spec([several]), bf.make_baseline(cells={}))


class BaselineHashTests(BindCase):
    def test_a_matching_hash_is_accepted(self):
        data = b"baseline bytes"
        bf.bind(bf.make_spec(baseline_sha256=hashlib.sha256(data).hexdigest()), baseline_bytes=data)

    def test_a_stale_hash_is_refused(self):
        message = self.refuse(bf.make_spec(baseline_sha256=hashlib.sha256(b"old").hexdigest()),
                              baseline_bytes=b"new", contains="sha256 differs")
        self.assertIn("run the intake again", message)

    def test_other_values_are_a_stale_hash(self):
        for bad in ("", "abc", 5, False, [], hashlib.sha256(b"x").hexdigest().upper()):
            with self.subTest(bad=bad):
                self.refuse(bf.make_spec(baseline_sha256=bad), baseline_bytes=b"x", contains="sha256 differs")

    def test_no_hash_in_the_spec_or_no_bytes_means_no_check(self):
        bf.bind(bf.make_spec(), baseline_bytes=b"anything")
        bf.bind(bf.make_spec(baseline_sha256="0" * 64), baseline_bytes=None)

    def test_the_hash_is_checked_before_the_baseline_shape(self):
        self.refuse(bf.make_spec(baseline_sha256="0" * 64), baseline={"sheets": []}, baseline_bytes=b"x",
                    contains="sha256 differs")


class LabelTests(BindCase):
    def test_a_label_only_has_to_be_contained(self):
        bf.bind(bf.make_spec(labels={"A5": "Row label one"}))
        bf.bind(bf.make_spec(labels={"A5": "label"}))
        bf.bind(bf.make_spec(labels={"A5": "Row"}))
        bf.bind(bf.make_spec(labels={"A5": "  one  "}))             # the expected text is stripped
        bf.bind(bf.make_spec(labels={"A5": "Row label one", "A6": "two", "A1": "title"}))

    def test_a_label_of_another_row_is_a_label_shift(self):
        message = self.refuse(bf.make_spec(labels={"A5": "Row label two"}), contains="label shift")
        self.assertIn("A5", message)
        self.assertIn("Row label two", message)
        self.assertIn("Row label one", message)

    def test_the_first_shifted_label_in_natural_order_is_reported(self):
        message = self.refuse(bf.make_spec(labels={"A6": "nope", "A5": "nope", "A1": "Report title"}),
                              contains="label shift")
        self.assertIn("A5", message)
        baseline = bf.edit(bf.small_baseline(), {"A10": {"content": "Ten"}, "B2": {"content": "Two"}})
        message = self.refuse(bf.make_spec(labels={"A10": "nope", "B2": "nope"}), baseline, contains="label shift")
        self.assertIn("B2", message)                                   # row 2 comes before row 10
        self.assertNotIn("A10", message)

    def test_a_label_with_more_text_than_the_cell_is_a_shift(self):
        self.refuse(bf.make_spec(labels={"A5": "Row label one and more"}), contains="label shift")

    def test_labels_are_case_sensitive(self):
        self.refuse(bf.make_spec(labels={"A5": "row label"}), contains="label shift")

    def test_multi_line_labels(self):
        baseline = bf.edit(bf.small_baseline(), {"A5": {"content": "Row\nlabel one"}})
        bf.bind(bf.make_spec(labels={"A5": "label one"}), baseline)
        bf.bind(bf.make_spec(labels={"A5": "Row\nlabel"}), baseline)
        self.refuse(bf.make_spec(labels={"A5": "Row label"}), baseline, contains="label shift")

    def test_a_non_string_or_blank_expected_text_is_refused(self):
        for bad in (5, None, ["x"], {"x": 1}, True, "", "   ", "\n"):
            with self.subTest(bad=bad):
                self.refuse(bf.make_spec(labels={"A5": bad}), contains="label shift")

    def test_a_missing_cell_or_a_cell_without_text_is_refused(self):
        self.refuse(bf.make_spec(labels={"H9": "x"}), contains="label shift")           # no such cell
        self.refuse(bf.make_spec(labels={"B1": "x"}), contains="label shift")           # style-only cell
        baseline = bf.edit(bf.small_baseline(), {"A5": {"content": 12}})
        self.refuse(bf.make_spec(labels={"A5": "12"}), baseline, contains="label shift")

    def test_labels_must_be_an_object(self):
        for bad in ([], "A5", None, 5, [["A5", "x"]]):
            with self.subTest(bad=bad):
                self.refuse(bf.make_spec(labels=bad), contains="`labels` must be an object")

    def test_a_malformed_cell_address_is_a_clean_error(self):
        for bad in ("a5", "5A", "A", "A0", "", "A5:B5"):
            with self.subTest(ref=bad):
                with self.assertRaises((bind_kpi.BindError, intake_xlsx.IntakeError)):
                    bf.bind(bf.make_spec(labels={bad: "x"}))

    def test_the_label_check_runs_before_the_binding_checks(self):
        spec = bf.make_spec([{"cells": "D5:F5", "source": "nope"}], labels={"A5": "nope"})
        self.refuse(spec, contains="label shift")


class KeepTypedTests(BindCase):
    def baseline(self):
        return bf.edit(bf.small_baseline(), {"H8": {"content": "7"}, "I8": {"content": "8"}, "J9": {"content": "9"}})

    def test_a_cell_keeps_its_number(self):
        document, report = bf.bind(bf.make_spec(keep_typed=["H8", "I8", "J9"]), self.baseline())
        self.assertEqual(document["sheets"][0]["cells"]["H8"], {"content": "7"})
        self.assertEqual(report["kept_typed"], 3)

    def test_a_block_keeps_its_numbers(self):
        _, report = bf.bind(bf.make_spec(keep_typed=["H8:J9"]), self.baseline())
        self.assertEqual(report["kept_typed"], 3)
        _, report = bf.bind(bf.make_spec(keep_typed=["H8:I8", "J9"]), self.baseline())
        self.assertEqual(report["kept_typed"], 3)

    def test_a_block_does_not_cover_a_neighbour(self):
        message = self.refuse(bf.make_spec(keep_typed=["H8:I8"]), self.baseline(), contains="J9")
        self.assertNotIn("H8", message)

    def test_a_rectangle_is_two_dimensional(self):
        self.refuse(bf.make_spec(keep_typed=["H9:J9"]), self.baseline(), contains="H8")
        self.refuse(bf.make_spec(keep_typed=["H8:H9", "I9:J9"]), self.baseline(), contains="I8")

    def test_a_huge_block_is_never_expanded(self):
        _, report = bf.bind(bf.make_spec(keep_typed=["A1:ZZZ9999999"]), self.baseline())
        self.assertEqual(report["kept_typed"], 3)

    def test_200_entries_are_accepted_and_201_refused(self):
        entries = ["H8", "I8", "J9"] + ["Z%d" % i for i in range(1, 198)]
        self.assertEqual(len(entries), 200)
        bf.bind(bf.make_spec(keep_typed=entries), self.baseline())
        self.refuse(bf.make_spec(keep_typed=entries + ["Z200"]), self.baseline(), contains="at most 200")

    def test_malformed_entries_are_refused(self):
        for bad in ("D", "8", "d8", "D8:", ":D8", "D8:E", "H8:J9:K9", "", " H8", "H8 ", 5, None, ["H8"], {"a": 1},
                    "D0", "AAAA1"):
            with self.subTest(bad=bad):
                if bad in ("D8:",):
                    continue                                  # "D8:" reads as the cell D8 (see the report)
                with self.assertRaises((bind_kpi.BindError, intake_xlsx.IntakeError)):
                    bf.bind(bf.make_spec(keep_typed=[bad]), self.baseline())

    def test_a_backwards_block_is_refused(self):
        self.refuse(bf.make_spec(keep_typed=["J9:H8"]), self.baseline(), contains="runs backwards")
        self.refuse(bf.make_spec(keep_typed=["I8:H8"]), self.baseline(), contains="runs backwards")
        self.refuse(bf.make_spec(keep_typed=["H9:H8"]), self.baseline(), contains="runs backwards")

    def test_keep_typed_must_be_a_list(self):
        for bad in ("H8", {"H8": 1}, None, 5):
            with self.subTest(bad=bad):
                self.refuse(bf.make_spec(keep_typed=bad), self.baseline(), contains="`keep_typed` must be a list")

    def test_a_bound_cell_inside_a_kept_block_is_still_replaced(self):
        baseline = bf.small_baseline()
        document, report = bf.bind(bf.make_spec(keep_typed=["D5:F6"]), baseline)
        self.assertTrue(document["sheets"][0]["cells"]["D6"]["content"].startswith("=IFERROR("))
        self.assertEqual(report["kept_typed"], 0)


# ---------------------------------------------------------------- 2. cells

class ParseCellsTests(BindCase):
    def test_a_cell_a_row_range_and_a_column_range(self):
        self.assertEqual(bind_kpi.parse_cells("D9", "b"), [(9, 4)])
        self.assertEqual(bind_kpi.parse_cells("D9:D9", "b"), [(9, 4)])
        self.assertEqual(bind_kpi.parse_cells("D9:O9", "b"), [(9, c) for c in range(4, 16)])
        self.assertEqual(bind_kpi.parse_cells("D9:D12", "b"), [(r, 4) for r in (9, 10, 11, 12)])
        self.assertEqual(bind_kpi.parse_cells("AA3:AC3", "b"), [(3, 27), (3, 28), (3, 29)])

    def test_a_block_is_refused(self):
        for text in ("D9:E10", "D9:O10", "A1:B2", "D9:E9000"):
            with self.subTest(text=text):
                with self.assertRaises(bind_kpi.BindError) as ctx:
                    bind_kpi.parse_cells(text, "binding 3")
                self.assertIn("not a block", str(ctx.exception))
                self.assertIn("binding 3", str(ctx.exception))

    def test_backwards_is_refused(self):
        for text in ("O9:D9", "D12:D9", "E10:D9", "D10:E9"):
            with self.subTest(text=text):
                with self.assertRaises(bind_kpi.BindError) as ctx:
                    bind_kpi.parse_cells(text, "binding 3")
                self.assertIn("runs backwards", str(ctx.exception))

    def test_malformed_text_is_refused(self):
        for bad in ("d9", "D", "9", "", "D0", "D9:O9:P9", "D 9", " D9", "D9 ", "$D$9", "Sheet!D9", None, 5, ["D9"],
                    {"D9": 1}, True, "AAAA1", "D99999999"):
            with self.subTest(bad=bad):
                with self.assertRaises(bind_kpi.BindError) as ctx:
                    bind_kpi.parse_cells(bad, "binding 1")
                self.assertIn("`cells`", str(ctx.exception))

    def test_the_cell_count_limit_is_1000_and_is_checked_before_any_list_is_built(self):
        self.assertEqual(len(bind_kpi.parse_cells("A1:A1000", "b")), 1000)
        last_ok, last_bad = intake_xlsx.col_letters(1000), intake_xlsx.col_letters(1001)
        self.assertEqual(len(bind_kpi.parse_cells("A1:%s1" % last_ok, "b")), 1000)
        for text in ("A1:A1001", "A1:%s1" % last_bad, "A1:A100000"):
            with self.subTest(text=text):
                with self.assertRaises(bind_kpi.BindError) as ctx:
                    bind_kpi.parse_cells(text, "binding 1")
                self.assertIn("at most 1000 cells", str(ctx.exception))

    def test_no_list_of_more_than_1000_cells_is_ever_built(self):
        """The cap is checked on the corner arithmetic: `range` is never asked for a refused size (a stand-in that
        raises, so a mutant that builds first dies at once instead of allocating)."""
        built = []

        def guarded_range(*args):
            span = len(range(*args))
            built.append(span)
            if span > 1000:
                raise AssertionError("a list of %d cells is being built before the cap check" % span)
            return range(*args)

        with mock.patch.object(bind_kpi, "range", guarded_range, create=True):
            for text in ("A1:A1001", "A1:A100000", "A1:%s1" % intake_xlsx.col_letters(1001)):
                with self.subTest(text=text), self.assertRaises(bind_kpi.BindError):
                    bind_kpi.parse_cells(text, "binding 1")
            self.assertEqual(len(bind_kpi.parse_cells("A1:A1000", "b")), 1000)
        self.assertEqual(built, [1000])

    def test_a_binding_of_1001_cells_is_refused_before_its_columns_are_looked_at(self):
        message = self.refuse(bf.make_spec([{"cells": "A1:A1001", "source": "plain", "columns": ["nope"]}]),
                              bf.make_baseline(cells={}, merges=[], row_number=1001, col_number=1), contains="at most 1000 cells")
        self.assertNotIn("nope", message)

    def test_a_range_given_to_a_one_column_spec_reads_top_to_bottom(self):
        document, _ = bf.bind(bf.make_spec([bf.plain("D5:D7", columns=["m3", "m1", "m2"])]),
                              bf.make_baseline(cells={}))
        cells = document["sheets"][0]["cells"]
        self.assertEqual([cells[r]["content"] for r in ("D5", "D6", "D7")],
                         ['=IFERROR(ODOO.LIST(1,1,"m3")+0,0)', '=IFERROR(ODOO.LIST(1,1,"m1")+0,0)',
                          '=IFERROR(ODOO.LIST(1,1,"m2")+0,0)'])

    def test_ref_of(self):
        self.assertEqual(bind_kpi.ref_of(9, 4), "D9")
        self.assertEqual(bind_kpi.ref_of(1, 27), "AA1")
        self.assertEqual(bind_kpi.ref_of(100, 703), "AAA100")


class TargetCellTests(BindCase):
    def single(self, ref, **kwargs):
        binding = {"cells": ref, "source": "plain", "where": {"code": "A"}, "column": "m1"}
        binding.update(kwargs)
        return bf.make_spec([binding])

    def test_outside_the_design_area_is_refused(self):
        baseline = bf.make_baseline(cells={})
        for ref in ("D11", "Q5", "D200", "ZZ5", "Q11", "A11"):
            with self.subTest(ref=ref):
                message = self.refuse(self.single(ref), baseline, contains="outside the design area")
                self.assertIn("10 rows x 16 columns", message)
                self.assertIn(ref, message)
        bf.bind(self.single("P10"), baseline)                         # the last cell of the area is inside
        bf.bind(self.single("A1"), baseline)

    def test_a_range_reaching_outside_is_refused_at_the_first_outside_cell(self):
        baseline = bf.make_baseline(cells={})
        message = self.refuse(bf.make_spec([bf.plain("N5:R5", columns=["m1", "m2", "m3", "m1", "m2"])]), baseline,
                              contains="outside the design area")
        self.assertIn("Q5", message)
        self.refuse(bf.make_spec([bf.plain("D9:D12", columns=["m1", "m2", "m3", "m1"])]), baseline,
                    contains="outside the design area")

    def test_overlap_between_two_bindings_is_refused(self):
        baseline = bf.make_baseline(cells={})
        message = self.refuse(bf.make_spec([bf.plain("D5:F5"), bf.plain("F5:H5", "B")]), baseline,
                              contains="already bound by another binding")
        self.assertIn("F5", message)
        self.assertIn("binding 2", message)
        self.refuse(bf.make_spec([bf.plain("D5:F5"), bf.plain("E5:E7", "B", columns=["m1", "m2", "m3"])]), baseline,
                    contains="E5")
        self.refuse(bf.make_spec([bf.plain("D5:F5"), bf.plain("D5:F5")]), baseline, contains="D5")
        self.refuse(bf.make_spec([bf.plain("D5", columns=["m1"]), bf.plain("D5", columns=["m2"])]), baseline,
                    contains="D5 is already bound")

    def test_neighbours_do_not_overlap(self):
        baseline = bf.make_baseline(cells={})
        bf.bind(bf.make_spec([bf.plain("D5:F5"), bf.plain("G5:I5", "B"), bf.plain("D6:F6"),
                              bf.plain("A5:A7", columns=["m1", "m2", "m3"])]), baseline)

    def test_a_target_inside_a_merge_is_refused_and_the_anchor_is_accepted(self):
        baseline = bf.small_baseline()
        for ref in ("B1", "C1", "P9"):
            with self.subTest(ref=ref):
                message = self.refuse(self.single(ref), baseline,
                                      contains="is inside the merge")
                self.assertIn("top-left", message)
                self.assertIn(ref, message)
        self.assertIn("A1:C1", self.refuse(self.single("B1"), baseline))
        self.assertIn("P8:P9", self.refuse(self.single("P9"), baseline))
        document, _ = bf.bind(self.single("P8"), bf.edit(baseline, {"D5": None, "E5": None, "F5": None, "D6": None,
                                                                   "E6": None, "F6": None}))
        self.assertTrue(document["sheets"][0]["cells"]["P8"]["content"].startswith("=IFERROR("))
        self.assertEqual(document["sheets"][0]["cells"]["P8"]["style"], 1)

    def test_the_anchor_of_a_merge_that_holds_text_needs_overwrite_text(self):
        baseline = bf.edit(bf.small_baseline(), {c: None for c in ("D5", "E5", "F5", "D6", "E6", "F6")})
        self.refuse(self.single("A1"), baseline, contains="holds the text")
        document, _ = bf.bind(self.single("A1", overwrite_text=True), baseline)
        self.assertTrue(document["sheets"][0]["cells"]["A1"]["content"].startswith("=IFERROR("))

    def test_a_range_running_through_a_merge_is_refused_on_the_inner_cell(self):
        baseline = bf.make_baseline(cells={}, merges=["E5:G5"])
        message = self.refuse(bf.make_spec([bf.plain("D5:H5", columns=["m1", "m2", "m3", "m1", "m2"])]), baseline,
                              contains="inside the merge E5:G5")
        self.assertIn("F5", message)

    def test_a_text_label_is_refused_and_accepted_with_overwrite_text(self):
        baseline = bf.edit(bf.small_baseline(), {"D5": {"content": "n/a"}, "D6": {"content": "1,5", "style": 2}})
        message = self.refuse(bf.make_spec([bf.plain("D5:F5")]), baseline, contains="holds the text")
        self.assertIn("'n/a'", message)
        self.assertIn("overwrite_text", message)
        self.refuse(bf.make_spec([bf.plain("D6:F6", "B", overwrite_text=False)]), baseline, contains="D6")
        document, _ = bf.bind(bf.make_spec([bf.plain("D5:F5", overwrite_text=True), bf.plain("D6:F6", "B",
                                                                                           overwrite_text=True)]),
                              baseline)
        cells = document["sheets"][0]["cells"]
        self.assertTrue(cells["D5"]["content"].startswith("=IFERROR("))
        self.assertEqual(cells["D6"]["style"], 2)                      # the style survives the new content

    def test_overwrite_text_must_be_the_boolean_true(self):
        # RED on purpose (finding F4): `binding.get("overwrite_text")` is a truthiness test, so the JSON text "false"
        # (or "no", 1 ...) switches the label guard off
        baseline = bf.edit(bf.small_baseline(), {"D5": {"content": "n/a"}})
        for value in ("false", "no", "yes", "true", 1, [1], {"a": 1}):
            with self.subTest(value=value):
                self.refuse(bf.make_spec([bf.plain("D5:F5", overwrite_text=value), bf.plain("D6:F6", "B")]), baseline,
                            contains="holds the text")

    def test_overwrite_text_belongs_to_its_own_binding(self):
        baseline = bf.edit(bf.small_baseline(), {"D5": {"content": "n/a"}, "D6": {"content": "n/b"}})
        self.refuse(bf.make_spec([bf.plain("D5:F5", overwrite_text=True), bf.plain("D6:F6", "B")]), baseline,
                    contains="D6")

    def test_formulas_typed_numbers_empty_and_missing_cells_are_replaced(self):
        baseline = bf.edit(bf.make_baseline(cells={}), {"D5": {"content": "=A1+1", "format": 1}, "E5": {"content": "-3"},
                                                        "F5": {"content": ""}, "G5": {"style": 1}})
        document, _ = bf.bind(bf.make_spec([bf.plain("D5:H5", columns=["m1", "m2", "m3", "m1", "m2"])]), baseline)
        cells = document["sheets"][0]["cells"]
        for ref, column in zip("DEFGH", ("m1", "m2", "m3", "m1", "m2")):
            self.assertEqual(cells[ref + "5"]["content"], '=IFERROR(ODOO.LIST(1,1,"%s")+0,0)' % column)
        self.assertEqual(cells["D5"]["format"], 1)
        self.assertEqual(cells["G5"]["style"], 1)
        self.assertEqual(cells["H5"], {"content": '=IFERROR(ODOO.LIST(1,1,"m2")+0,0)'})

    def test_a_target_whose_content_is_not_text_is_replaced(self):
        for content in (12, 12.5, True, None, ["x"]):
            with self.subTest(content=content):
                baseline = bf.edit(bf.make_baseline(cells={}), {"D5": {"content": content, "style": 1}})
                document, _ = bf.bind(self.single("D5"), baseline)
                self.assertEqual(document["sheets"][0]["cells"]["D5"],
                                 {"content": '=IFERROR(ODOO.LIST(1,1,"m1")+0,0)', "style": 1})

    def test_numeric_text_with_a_sign_or_decimals_is_replaceable_without_overwrite_text(self):
        for content in ("0", "1.5", "-3", "007", "-0.25"):
            with self.subTest(content=content):
                baseline = bf.edit(bf.make_baseline(cells={}), {"D5": {"content": content}})
                bf.bind(self.single("D5"), baseline)

    def test_text_that_only_looks_like_a_number_is_a_label(self):
        for content in ("1,5", "12abc", "+3", " 5", "5 ", "N/A", "0x10", "--1"):
            with self.subTest(content=content):
                baseline = bf.edit(bf.make_baseline(cells={}), {"D5": {"content": content}})
                self.refuse(self.single("D5"), baseline, contains="holds the text")


# ---------------------------------------------------------------- 3. catalog resolution

class SourceTests(BindCase):
    def test_an_unknown_source_carries_the_requirement_text_sentence(self):
        for name in ("not_there", "Production_Group", "", " production_group", "production_group ", "note", "schema"):
            with self.subTest(name=name):
                message = self.refuse_real({"cells": "D5", "source": name, "column": "month_1"}, contains=AC7)
                self.assertIn("binding 1", message)
                self.assertIn("not in the catalog", message)

    def test_a_missing_or_non_text_source_is_the_same_error(self):
        for bad in (None, 5, ["production_group"], {"a": 1}, True):
            with self.subTest(bad=bad):
                self.refuse_real({"cells": "D5", "source": bad, "column": "month_1"}, contains=AC7)
        self.refuse_real({"cells": "D5", "column": "month_1"}, contains=AC7)

    def test_the_source_name_is_cut_and_never_prints_a_line_break(self):
        message = self.refuse_real({"cells": "D5", "source": "a\nb" + "x" * 100, "column": "month_1"}, contains=AC7)
        self.assertNotIn("\n", message)
        self.assertNotIn("x" * 50, message)

    def test_the_source_is_checked_before_everything_else_of_the_binding(self):
        message = self.refuse_real({"cells": "ZZZ", "source": "nope", "column": ["x"], "where": 5, "year": "x"},
                                   contains=AC7)
        self.assertNotIn("`cells`", message)

    def test_every_catalog_source_and_column_group_binds(self):
        catalog = bf.real_catalog()
        self.assertGreaterEqual(len(catalog["sources"]), 5)
        pivot_sources = 0
        for name, source in catalog["sources"].items():
            where = {}
            for selector, rule in source["selectors"].items():
                where[selector] = rule["values"][0] if "values" in rule else rule["min"]
            if source.get("kind") == "pivot":                  # one binding per measure and per selector value
                pivot_sources += 1
                for measure in source["measures"]:
                    for selector, rule in source["selectors"].items():
                        for value in rule["values"]:
                            with self.subTest(source=name, measure=measure, selector=selector, value=value):
                                binding = {"cells": "A1:L1", "source": name, "measure": measure,
                                           "where": {**where, selector: value}}
                                document, report = bf.bind(bf.make_spec([binding]),
                                                           bf.make_baseline(cells={}, merges=[]), catalog)
                                self.assertEqual((report["pivots"], report["lists"], report["bound_cells"]),
                                                 (1, 0, 12))
                                self.assertEqual(sorted(document["pivots"]), ["1", "2"])      # data pivot + probe
                                data = document["pivots"]["1"]
                                self.assertEqual(data["model"], source["model"])
                                self.assertEqual(data["measures"], [{"field": measure}])
                                self.assertEqual(data["colGroupBys"], [source["date_field"] + ":month"])
                                self.assertEqual(data["domain"], [[s, "=", {**where, selector: value}[s]]
                                                                  for s in sorted(source["selectors"])])
                                self.assertEqual(document["pivots"]["2"]["fieldMatching"],
                                                 {"kpi-year-filter": {"chain": source["date_field"], "type": "date"}})
                continue
            for group, columns in source["column_groups"].items():
                for year in source["year_chain"]:
                    with self.subTest(source=name, group=group, year=year):
                        binding = {"cells": "A1:%s1" % intake_xlsx.col_letters(len(columns)), "source": name,
                                   "columns": group, "year": year}
                        if where:
                            binding["where"] = where
                        document, _ = bf.bind(bf.make_spec([binding]), bf.make_baseline(cells={}, merges=[]), catalog)
                        listing = document["lists"]["1"]
                        self.assertEqual(listing["model"], source["model"])
                        self.assertEqual(listing["fieldMatching"]["kpi-year-filter"]["chain"],
                                         source["year_chain"][year])
                        self.assertEqual(listing["columns"], sorted(set(columns) | set(source["selectors"])))
        self.assertGreaterEqual(pivot_sources, 1)


class WhereTests(BindCase):
    PROCESS = {"cells": "D5:O5", "source": "process_month", "columns": "months"}

    def process(self, where="<absent>"):
        binding = dict(self.PROCESS)
        if where != "<absent>":
            binding["where"] = where
        return binding

    def test_a_valid_where_gives_the_sorted_domain(self):
        document, _ = self.real(self.process({"row_no": 3, "metric": "per_day"}))
        self.assertEqual(document["lists"]["1"]["domain"], [["metric", "=", "per_day"], ["row_no", "=", 3]])

    def test_the_domain_shape_for_one_selector(self):
        document, _ = self.real({"cells": "D5:O5", "source": "production_group", "where": {"group_code": "DK_REF"},
                                 "columns": "months"})
        self.assertEqual(document["lists"]["1"]["domain"], [["group_code", "=", "DK_REF"]])

    def test_the_int_selector_range_is_inclusive(self):
        for value in (1, 2, 41):
            with self.subTest(value=value):
                self.real(self.process({"metric": "per_hour", "row_no": value}))

    def test_a_value_that_is_not_allowed_is_refused(self):
        bad_numbers = [0, 42, -1, 10 ** 30, True, False, "3", 3.0, 3.5, None, [3], {"a": 1}, "", "1"]
        for value in bad_numbers:
            with self.subTest(row_no=value):
                message = self.refuse_real(self.process({"metric": "per_day", "row_no": value}),
                                           contains=("row_no", "is not allowed"))
                self.assertIn("1..41", message)
        for value in ("per_year", "PER_DAY", " per_day", "per_day ", 5, None, True, ["per_day"], "", {"a": 1}):
            with self.subTest(metric=value):
                message = self.refuse_real(self.process({"metric": value, "row_no": 3}),
                                           contains=("metric", "is not allowed"))
                self.assertIn("per_hour", message)

    def test_the_refused_value_is_cut_and_never_prints_a_line_break(self):
        message = self.refuse_real(self.process({"metric": "a\nb" + "z" * 100, "row_no": 3}), contains="metric")
        self.assertNotIn("\n", message)
        self.assertNotIn("z" * 50, message)

    def test_where_must_give_exactly_the_selectors(self):
        for where in ("<absent>", {}, {"metric": "per_day"}, {"row_no": 3}, {"metric": "per_day", "row_no": 3, "x": 1},
                      {"metric": "per_day", "row_no": 3, "metric ": 1}, [], "metric", None, 5,
                      {"Metric": "per_day", "row_no": 3}):
            with self.subTest(where=where):
                message = self.refuse_real(self.process(where), contains="`where` must give exactly")
                self.assertIn("metric", message)
                self.assertIn("row_no", message)

    def test_a_source_without_selectors_takes_no_where(self):
        base = {"cells": "D5:O5", "source": "production_year", "columns": "headcount"}
        self.real(base)
        self.real(dict(base, where={}))
        for where in ({"metric": "x"}, {"a": 1}, [1], "x", None):
            with self.subTest(where=where):
                self.refuse_real(dict(base, where=where), contains="`where` must give exactly nothing")

    def test_two_selectors_sort_by_name_whatever_the_key_order(self):
        for where in ({"group_code": "DK_AC", "kind": "shipped"}, {"kind": "shipped", "group_code": "DK_AC"}):
            document, _ = self.real({"cells": "D5:O5", "source": "report_group", "where": where, "columns": "months"})
            self.assertEqual(document["lists"]["1"]["domain"], [["group_code", "=", "DK_AC"], ["kind", "=", "shipped"]])
            self.assertEqual(document["lists"]["1"]["name"], "Shipped and produced quantity by customer / product "
                                                              "group DK_AC shipped")

    def test_a_selector_from_another_source_is_not_allowed(self):
        self.refuse_real({"cells": "D5:O5", "source": "production_group", "where": {"group_code": "SZDK_WM"},
                          "columns": "months"}, contains="group_code")
        self.real({"cells": "D5:O5", "source": "report_group", "where": {"group_code": "SZDK_WM", "kind": "produced"},
                   "columns": "months"})


class ColumnTests(BindCase):
    def bind_columns(self, cells="D5:F5", **kwargs):
        binding = {"cells": cells, "source": "plain", "where": {"code": "A"}}
        binding.update(kwargs)
        return bf.bind(bf.make_spec([binding]), bf.make_baseline(cells={}))

    def contents(self, document, refs):
        return [document["sheets"][0]["cells"][r]["content"] for r in refs]

    def test_a_group_name(self):
        document, _ = self.bind_columns(columns="months")
        self.assertEqual(self.contents(document, ("D5", "E5", "F5")),
                         ['=IFERROR(ODOO.LIST(1,1,"m%d")+0,0)' % i for i in (1, 2, 3)])

    def test_an_explicit_list_keeps_its_order(self):
        document, _ = self.bind_columns(columns=["m3", "m1", "extra"])
        self.assertEqual(self.contents(document, ("D5", "E5", "F5")),
                         ['=IFERROR(ODOO.LIST(1,1,"%s")+0,0)' % c for c in ("m3", "m1", "extra")])

    def test_column_gives_one_cell(self):
        document, _ = self.bind_columns("D5", column="m2")
        self.assertEqual(self.contents(document, ["D5"]), ['=IFERROR(ODOO.LIST(1,1,"m2")+0,0)'])
        document, _ = self.bind_columns("D5", columns="m2")           # a text that is not a group is one column
        self.assertEqual(self.contents(document, ["D5"]), ['=IFERROR(ODOO.LIST(1,1,"m2")+0,0)'])

    def test_both_columns_and_column_is_refused(self):
        with self.assertRaises(bind_kpi.BindError) as ctx:
            self.bind_columns("D5", columns=["m1"], column="m1")
        self.assertIn("not both", str(ctx.exception))
        with self.assertRaises(bind_kpi.BindError) as ctx:
            self.bind_columns("D5", columns=None, column=None)
        self.assertIn("not both", str(ctx.exception))

    def test_a_length_mismatch_is_refused(self):
        for cells, columns, text in (("D5:F5", ["m1", "m2"], "3 cells but 2 columns"),
                                     ("D5:E5", "months", "2 cells but 3 columns"),
                                     ("D5", [], "1 cells but 0 columns"),
                                     ("D5:F5", ["m1", "m2", "m3", "m1"], "3 cells but 4 columns")):
            with self.subTest(cells=cells, columns=columns):
                with self.assertRaises(bind_kpi.BindError) as ctx:
                    self.bind_columns(cells, columns=columns)
                self.assertIn(text, str(ctx.exception))

    def test_an_unknown_column_is_refused_and_the_groups_are_listed(self):
        for columns in (["m1", "m4", "m3"], "weeks", ["M1", "m2", "m3"], ["m1", "m2", "m3 "]):
            with self.subTest(columns=columns):
                with self.assertRaises(bind_kpi.BindError) as ctx:
                    self.bind_columns("D5:F5" if isinstance(columns, list) else "D5", columns=columns)
                self.assertIn("is not in the source", str(ctx.exception))
                self.assertIn("groups: months", str(ctx.exception))

    def test_a_column_of_another_source_is_unknown(self):
        with self.assertRaises(bind_kpi.BindError):
            self.bind_columns("D5", column="c000")

    def test_the_wrong_type_is_refused(self):
        for columns in (None, 5, [1, 2, 3], ["m1", None, "m3"], {"a": "m1"}, True, [["m1"], "m2", "m3"]):
            with self.subTest(columns=columns):
                with self.assertRaises(bind_kpi.BindError) as ctx:
                    self.bind_columns("D5:F5", columns=columns)
                self.assertIn("column group name or a list", str(ctx.exception))
        with self.assertRaises(bind_kpi.BindError) as ctx:
            bf.bind(bf.make_spec([{"cells": "D5", "source": "plain", "where": {"code": "A"}}]),
                    bf.make_baseline(cells={}))
        self.assertIn("column group name or a list", str(ctx.exception))

    def test_the_same_column_twice_is_allowed(self):
        document, _ = self.bind_columns("D5:E5", columns=["m1", "m1"])
        self.assertEqual(document["lists"]["1"]["columns"], ["code", "m1"])


class YearAndZeroTests(BindCase):
    def test_previous_needs_a_chain(self):
        for source, where in (("month_metric", {"metric": "ng_qty"}),
                              ("report_group", {"group_code": "DK_REF", "kind": "shipped"})):
            with self.subTest(source=source):
                message = self.refuse_real({"cells": "D5:O5", "source": source, "where": where, "columns": "months",
                                            "year": "previous"}, contains="year 'previous' is not available")
                self.assertIn(source, message)
                self.assertIn("chains: current", message)

    def test_previous_is_accepted_with_the_next_year_chain(self):
        cases = (("production_group", {"group_code": "DK_REF"}, "months"), ("process_month", {"metric": "per_day",
                                                                                              "row_no": 1}, "months"),
                 ("production_year", {}, "headcount"))
        for source, where, group in cases:
            with self.subTest(source=source):
                document, _ = self.real({"cells": "D5:O5", "source": source, "where": where, "columns": group,
                                         "year": "previous"})
                self.assertEqual(document["lists"]["1"]["fieldMatching"]["kpi-year-filter"],
                                 {"chain": "next_year", "type": "date"})
                self.assertTrue(document["lists"]["1"]["name"].endswith(" (previous year)"))

    def test_current_chains_come_from_the_catalog(self):
        for source, where, group, chain in (("production_group", {"group_code": "DK_REF"}, "months", "year"),
                                            ("production_year", {}, "headcount", "year_date"),
                                            ("month_metric", {"metric": "ng_qty"}, "months", "year")):
            with self.subTest(source=source):
                for binding in ({"cells": "D5:O5", "source": source, "where": where, "columns": group},
                                {"cells": "D5:O5", "source": source, "where": where, "columns": group,
                                 "year": "current"}):
                    document, _ = self.real(binding)
                    self.assertEqual(document["lists"]["1"]["fieldMatching"]["kpi-year-filter"]["chain"], chain)

    def test_other_years_are_refused(self):
        for year in ("last", "Current", "next_year", "year", 2025, None, True, ["current"], "", "previous "):
            with self.subTest(year=year):
                self.refuse_real({"cells": "D5:O5", "source": "production_group", "where": {"group_code": "DK_REF"},
                                  "columns": "months", "year": year}, contains="is not available")

    def test_zero_must_be_number_or_dash(self):
        for zero in ("Dash", "dash ", "NUMBER", "", True, False, None, 0, ["dash"], "none"):
            with self.subTest(zero=zero):
                with self.assertRaises(bind_kpi.BindError) as ctx:
                    bf.bind(bf.make_spec([bf.plain("D5:F5", zero=zero)]), bf.make_baseline(cells={}))
                self.assertIn("`zero` must be number or dash", str(ctx.exception))

    def test_zero_defaults_to_number(self):
        document, report = bf.bind(bf.make_spec([bf.plain("D5:F5")]), bf.make_baseline(cells={}))
        self.assertEqual(report["dash_cells"], 0)
        self.assertEqual(document["sheets"][0]["rowNumber"], 10)


# ---------------------------------------------------------------- 4. output structure

def structure_spec():
    return bf.make_spec([
        {"cells": "D5", "source": "plain", "where": {"code": "A"}, "column": "m3"},                      # list 1
        {"cells": "D6:E6", "source": "plain", "where": {"code": "A"}, "columns": ["m2", "m1"]},           # list 1
        {"cells": "D7", "source": "plain", "where": {"code": "B"}, "column": "m1"},                      # list 2
        {"cells": "D8", "source": "plain", "where": {"code": "A"}, "column": "m1", "year": "previous"},   # list 3
        {"cells": "D9", "source": "plain", "where": {"code": "B"}, "column": "extra"},                   # list 2
        {"cells": "D10", "source": "secret", "column": "m1"},                                            # list 4
        {"cells": "D2", "source": "grid", "where": {"n": 2, "kind": "x"}, "column": "m1"},               # list 5
        {"cells": "D3", "source": "grid", "where": {"kind": "x", "n": 2}, "column": "m2"},               # list 5
        {"cells": "D4", "source": "grid", "where": {"n": 3, "kind": "x"}, "column": "m1"},               # list 6
    ])


class ListStructureTests(BindCase):
    def setUp(self):
        self.document, self.report = bf.bind(structure_spec(), bf.make_baseline(cells={}))

    def test_one_list_per_distinct_source_where_and_year_in_first_use_order(self):
        lists = self.document["lists"]
        self.assertEqual(list(lists), ["1", "2", "3", "4", "5", "6"])
        self.assertEqual([lists[i]["id"] for i in lists], ["1", "2", "3", "4", "5", "6"])
        self.assertEqual([(l["model"], l["domain"]) for l in lists.values()], [
            ("t.plain", [["code", "=", "A"]]), ("t.plain", [["code", "=", "B"]]), ("t.plain", [["code", "=", "A"]]),
            ("t.secret", []), ("t.grid", [["kind", "=", "x"], ["n", "=", 2]]),
            ("t.grid", [["kind", "=", "x"], ["n", "=", 3]])])
        self.assertEqual(self.document["listNextId"], 7)
        self.assertEqual(self.report["lists"], 6)

    def test_columns_are_the_sorted_union_plus_the_selector_fields(self):
        lists = self.document["lists"]
        self.assertEqual(lists["1"]["columns"], ["code", "m1", "m2", "m3"])
        self.assertEqual(lists["2"]["columns"], ["code", "extra", "m1"])
        self.assertEqual(lists["3"]["columns"], ["code", "m1"])
        self.assertEqual(lists["4"]["columns"], ["m1"])
        self.assertEqual(lists["5"]["columns"], ["kind", "m1", "m2", "n"])
        self.assertEqual(lists["6"]["columns"], ["kind", "m1", "n"])

    def test_the_previous_year_list_is_matched_through_the_next_year_chain(self):
        chains = {i: l["fieldMatching"] for i, l in self.document["lists"].items()}
        self.assertEqual(chains["1"], {"kpi-year-filter": {"chain": "year", "type": "date"}})
        self.assertEqual(chains["2"], {"kpi-year-filter": {"chain": "year", "type": "date"}})
        self.assertEqual(chains["3"], {"kpi-year-filter": {"chain": "next_year", "type": "date"}})
        self.assertEqual(chains["4"], {"kpi-year-filter": {"chain": "year_date", "type": "date"}})
        self.assertEqual(chains["5"], {"kpi-year-filter": {"chain": "year", "type": "date"}})

    def test_each_list_has_the_full_shape(self):
        self.assertEqual(self.document["lists"]["1"], {
            "columns": ["code", "m1", "m2", "m3"], "domain": [["code", "=", "A"]], "model": "t.plain", "context": {},
            "orderBy": [], "id": "1", "name": "Plain figures A",
            "fieldMatching": {"kpi-year-filter": {"chain": "year", "type": "date"}}})
        for listing in self.document["lists"].values():
            self.assertEqual(set(listing), {"columns", "domain", "model", "context", "orderBy", "id", "name",
                                            "fieldMatching"})
            self.assertEqual(listing["context"], {})
            self.assertEqual(listing["orderBy"], [])

    def test_list_names(self):
        names = [l["name"] for l in self.document["lists"].values()]
        self.assertEqual(names, ["Plain figures A", "Plain figures B", "Plain figures A (previous year)",
                                 "Secret figures", "Grid x 2", "Grid x 3"])

    def test_a_long_title_is_cut_to_100_characters(self):
        document, _ = bf.bind(bf.make_spec([{"cells": "D5", "source": "long", "where": {"code": "A"}, "column": "m1",
                                             "year": "current"}]), bf.make_baseline(cells={}))
        self.assertEqual(document["lists"]["1"]["name"], "T" * 100)
        title = "U" * 90
        catalog = bf.small_catalog()
        catalog["sources"]["long"]["title"] = title
        catalog["sources"]["long"]["year_chain"]["previous"] = "next_year"
        document, _ = bf.bind(bf.make_spec([{"cells": "D5", "source": "long", "where": {"code": "A"}, "column": "m1",
                                             "year": "previous"}]), bf.make_baseline(cells={}), catalog)
        self.assertEqual(len(document["lists"]["1"]["name"]), 100)
        self.assertEqual(document["lists"]["1"]["name"], (title + " A (previous year)")[:100])

    def test_the_first_part_of_the_title_before_a_comma_names_the_list(self):
        catalog = bf.small_catalog()
        catalog["sources"]["plain"]["title"] = "One, two, three"
        document, _ = bf.bind(bf.make_spec([bf.plain("D5:F5")]), bf.make_baseline(cells={}), catalog)
        self.assertEqual(document["lists"]["1"]["name"], "One A")

    def test_ids_follow_the_order_of_first_use(self):
        document, _ = bf.bind(bf.make_spec([bf.plain("D6:F6", "B"), bf.plain("D5:F5", "A")]), bf.make_baseline(cells={}))
        self.assertEqual(document["lists"]["1"]["domain"], [["code", "=", "B"]])
        self.assertEqual(document["lists"]["2"]["domain"], [["code", "=", "A"]])

    def test_two_sources_without_selectors_never_share_a_list(self):
        document, report = bf.bind(bf.make_spec([{"cells": "D5", "source": "secret", "column": "m1"},
                                                 {"cells": "D6", "source": "wide", "column": "c001"}]),
                                   bf.make_baseline(cells={}))
        self.assertEqual(report["lists"], 2)
        self.assertEqual([l["model"] for l in document["lists"].values()], ["t.secret", "t.wide"])
        self.assertEqual(document["lists"]["2"]["columns"], ["c001"])

    def test_one_list_is_shared_by_bindings_of_the_same_key_even_when_not_adjacent(self):
        document, report = bf.bind(bf.make_spec([bf.plain("D5:F5", "A"), bf.plain("D6:F6", "B"),
                                                 bf.plain("D7:F7", "A"), bf.plain("D8:F8", "B")]),
                                   bf.make_baseline(cells={}))
        self.assertEqual(report["lists"], 2)
        cells = document["sheets"][0]["cells"]
        self.assertIn("ODOO.LIST(1,", cells["D7"]["content"])
        self.assertIn("ODOO.LIST(2,", cells["D8"]["content"])

    def test_the_same_source_with_a_different_year_is_a_different_list(self):
        _, report = bf.bind(bf.make_spec([bf.plain("D5:F5"), bf.plain("D6:F6", year="previous")]),
                            bf.make_baseline(cells={}))
        self.assertEqual(report["lists"], 2)

    def test_the_read_points_at_the_list_of_its_binding(self):
        cells = self.document["sheets"][0]["cells"]
        self.assertEqual(cells["D5"]["content"], '=IFERROR(ODOO.LIST(1,1,"m3")+0,0)')
        self.assertEqual(cells["D6"]["content"], '=IFERROR(ODOO.LIST(1,1,"m2")+0,0)')
        self.assertEqual(cells["D7"]["content"], '=IFERROR(ODOO.LIST(2,1,"m1")+0,0)')
        self.assertEqual(cells["D8"]["content"], '=IFERROR(ODOO.LIST(3,1,"m1")+0,0)')
        self.assertEqual(cells["D9"]["content"], '=IFERROR(ODOO.LIST(2,1,"extra")+0,0)')
        self.assertEqual(cells["D2"]["content"], '=IFERROR(ODOO.LIST(5,1,"m1")+0,0)')
        self.assertEqual(cells["D3"]["content"], '=IFERROR(ODOO.LIST(5,1,"m2")+0,0)')
        self.assertEqual(cells["D4"]["content"], '=IFERROR(ODOO.LIST(6,1,"m1")+0,0)')

    def test_a_source_other_users_than_base_group_user_reads_raw(self):
        cells = self.document["sheets"][0]["cells"]
        self.assertEqual(cells["D10"]["content"], '=ODOO.LIST(4,1,"m1")')


class ReadFormulaTests(BindCase):
    def test_the_audience_rule(self):
        self.assertEqual(bind_kpi.read_formula({"read_audience": "base.group_user"}, 3, "c"),
                         'IFERROR(ODOO.LIST(3,1,"c")+0,0)')
        for audience in ("base.group_system", "base.group_portal", "base.group_user ", "BASE.GROUP_USER", "", None,
                         "account.group_account_user"):
            with self.subTest(audience=audience):
                self.assertEqual(bind_kpi.read_formula({"read_audience": audience}, 12, "col_a"),
                                 'ODOO.LIST(12,1,"col_a")')

    def test_a_raw_source_binds_the_raw_read_for_every_zero_mode(self):
        for zero, ref, helper in (("number", "D5", None), ("dash", "D5", "D12")):
            with self.subTest(zero=zero):
                document, _ = bf.bind(bf.make_spec([{"cells": "D5", "source": "secret", "column": "m1",
                                                     "zero": zero}]), bf.make_baseline(cells={}))
                cells = document["sheets"][0]["cells"]
                if helper:
                    self.assertEqual(cells[helper]["content"], '=ODOO.LIST(1,1,"m1")')
                    self.assertEqual(cells[ref]["content"], '=IF(D12=0,"-",D12)')
                else:
                    self.assertEqual(cells[ref]["content"], '=ODOO.LIST(1,1,"m1")')

    def test_a_catalog_source_with_another_audience_changes_the_formula_not_the_list(self):
        catalog = bf.small_catalog()
        for audience, expected in (("base.group_user", 'IFERROR(ODOO.LIST(1,1,"m1")+0,0)'),
                                   ("base.group_erp_manager", 'ODOO.LIST(1,1,"m1")')):
            catalog["sources"]["plain"]["read_audience"] = audience
            document, _ = bf.bind(bf.make_spec([{"cells": "D5", "source": "plain", "where": {"code": "A"},
                                                 "column": "m1"}]), bf.make_baseline(cells={}), catalog)
            self.assertEqual(document["sheets"][0]["cells"]["D5"]["content"], "=" + expected)


class DocumentTests(BindCase):
    def test_exactly_one_global_filter(self):
        document, _ = bf.bind(bf.make_spec(year_filter_label="Fiscal year"))
        self.assertEqual(document["globalFilters"], [{"id": "kpi-year-filter", "type": "date", "label": "Fiscal year",
                                                      "rangeType": "single_date", "singleDateType": "year"}])
        self.assertEqual(bind_kpi.FILTER_ID, "kpi-year-filter")

    def test_a_filter_of_the_baseline_is_replaced_not_added(self):
        baseline = bf.small_baseline()
        baseline["globalFilters"] = [{"id": "old", "type": "text", "label": "Old"}]
        baseline["lists"] = {"9": {"id": "9"}}
        baseline["listNextId"] = 40
        document, _ = bf.bind(bf.make_spec(), baseline)
        self.assertEqual([f["id"] for f in document["globalFilters"]], ["kpi-year-filter"])
        self.assertEqual(sorted(document["lists"]), ["1", "2"])
        self.assertEqual(document["listNextId"], 3)

    def test_the_revision_is_the_baseline_revision(self):
        baseline = bf.small_baseline()
        baseline["revisionId"] = "3f2b8a4e-other"
        document, _ = bf.bind(bf.make_spec(), baseline)
        self.assertEqual(document["revisionId"], "START_REVISION")
        self.assertEqual(document["revisionId"], intake_xlsx.BASELINE_REVISION)
        del baseline["revisionId"]
        document, _ = bf.bind(bf.make_spec(), baseline)
        self.assertEqual(document["revisionId"], "START_REVISION")

    def test_the_version_and_the_other_workbook_keys_come_from_the_baseline(self):
        baseline = bf.small_baseline()
        document, _ = bf.bind(bf.make_spec(), baseline)
        replaced = {"sheets", "lists", "listNextId", "globalFilters", "revisionId"}
        self.assertEqual(document["version"], baseline["version"])
        for key in baseline:
            if key not in replaced:
                self.assertEqual(document[key], baseline[key], key)
        self.assertEqual(set(document), set(baseline) | {"lists", "listNextId", "globalFilters", "revisionId"})
        baseline["version"] = 99
        self.assertEqual(bf.bind(bf.make_spec(), baseline)[0]["version"], 99)
        del baseline["version"]
        self.assertEqual(bf.bind(bf.make_spec(), baseline)[0]["version"], 14.5)

    def test_the_input_is_not_modified(self):
        baseline, spec, catalog = bf.small_baseline(), bf.make_spec(), bf.small_catalog()
        before = copy.deepcopy((baseline, spec, catalog))
        bf.bind(spec, baseline, catalog)
        self.assertEqual((baseline, spec, catalog), before)

    def test_every_cell_that_is_not_a_target_is_untouched_and_a_target_keeps_its_look(self):
        baseline = bf.small_baseline()
        document, _ = bf.bind(bf.make_spec(), baseline)
        out, base = document["sheets"][0], baseline["sheets"][0]
        targets = {"%s%d" % (c, r) for c in "DEF" for r in (5, 6)}
        helpers = {"D12", "E12", "F12"}
        self.assertEqual(set(out["cells"]), set(base["cells"]) | helpers)
        for ref, cell in base["cells"].items():
            if ref in targets:
                self.assertEqual({k: v for k, v in out["cells"][ref].items() if k != "content"},
                                 {k: v for k, v in cell.items() if k != "content"}, ref)
                self.assertNotEqual(out["cells"][ref]["content"], cell["content"])
            else:
                self.assertEqual(out["cells"][ref], cell, ref)
        for key in base:
            if key not in ("cells", "rows", "rowNumber"):
                self.assertEqual(out[key], base[key], key)
        for index, size in base["rows"].items():
            self.assertEqual(out["rows"][index], size)
        self.assertEqual(out["colNumber"], base["colNumber"])
        self.assertEqual(out["merges"], ["A1:C1", "P8:P9"])
        self.assertEqual(document["styles"], baseline["styles"])
        self.assertEqual(document["formats"], baseline["formats"])
        self.assertEqual(document["borders"], baseline["borders"])

    def test_cells_are_written_in_natural_order(self):
        baseline = bf.small_baseline()
        sheet = baseline["sheets"][0]
        sheet["cells"] = dict(reversed(list(sheet["cells"].items())))
        document, _ = bf.bind(bf.make_spec(), baseline)
        refs = list(document["sheets"][0]["cells"])
        self.assertEqual(refs, sorted(refs, key=bf.natural_key))
        self.assertEqual(refs[:3], ["A1", "B1", "C1"])
        self.assertEqual(refs[-3:], ["D12", "E12", "F12"])
        self.assertLess(refs.index("A5"), refs.index("D5"))                 # column order inside a row
        self.assertLess(refs.index("F5"), refs.index("A6"))                 # row 5 before row 6
        self.assertLess(refs.index("P7"), refs.index("P8"))

    def test_natural_order_is_by_row_then_column_not_by_text(self):
        baseline = bf.make_baseline(cells={"B2": {"content": "x"}, "A10": {"content": "y"}, "AA1": {"content": "z"},
                                           "B1": {"content": "w"}}, col_number=30)
        document, _ = bf.bind(bf.make_spec([{"cells": "C9", "source": "secret", "column": "m1"}]), baseline)
        self.assertEqual(list(document["sheets"][0]["cells"]), ["B1", "Q1", "AA1", "B2", "C9", "A10"])


class DashStructureTests(BindCase):
    def test_a_dash_cell_reads_a_helper_in_a_hidden_row_below_the_area(self):
        document, report = bf.bind()
        sheet = document["sheets"][0]
        self.assertEqual(sheet["rowNumber"], 12)
        self.assertEqual(sheet["rows"]["11"], {"size": 23, "isHidden": True})
        for col, column in zip("DEF", ("m1", "m2", "m3")):
            self.assertEqual(sheet["cells"][col + "12"], {"content": '=IFERROR(ODOO.LIST(1,1,"%s")+0,0)' % column})
            self.assertEqual(sheet["cells"][col + "5"]["content"], '=IF(%s12=0,"-",%s12)' % (col, col))
            self.assertEqual(sheet["cells"][col + "6"]["content"],
                             '=IFERROR(ODOO.LIST(2,1,"%s")+0,0)' % column)           # number: no helper
        self.assertNotIn("D13", sheet["cells"])
        self.assertEqual((report["dash_cells"], report["helper_rows"], report["bound_cells"]), (3, 1, 6))

    def test_without_a_dash_there_is_no_helper_row(self):
        baseline = bf.small_baseline()
        document, report = bf.bind(bf.make_spec([bf.plain("D5:F5"), bf.plain("D6:F6", "B")]), baseline)
        sheet = document["sheets"][0]
        self.assertEqual(sheet["rowNumber"], 10)
        self.assertEqual(sheet["rows"], baseline["sheets"][0]["rows"])
        self.assertEqual(len(sheet["cells"]), len(baseline["sheets"][0]["cells"]))
        self.assertEqual((report["dash_cells"], report["helper_rows"]), (0, 0))

    def test_helper_rows_are_allocated_once_per_target_row_in_ascending_order(self):
        baseline = bf.make_baseline(cells={})
        bindings = [bf.plain("D8:F8", "B", zero="dash"), bf.plain("D5:F5", "A", zero="dash"),
                    bf.plain("G5:I5", "B", zero="dash"), bf.plain("D3:F3", "A", zero="number"),
                    bf.plain("D7", "A", columns=["m1"], zero="dash")]
        document, report = bf.bind(bf.make_spec(bindings), baseline)
        sheet = document["sheets"][0]
        # target rows with a dash: 5, 7, 8 -> helper rows 12, 13, 14; row 3 is a number: none
        for col in "DEFGHI":
            self.assertEqual(sheet["cells"][col + "5"]["content"], '=IF(%s12=0,"-",%s12)' % (col, col))
        self.assertEqual(sheet["cells"]["D7"]["content"], '=IF(D13=0,"-",D13)')
        for col in "DEF":
            self.assertEqual(sheet["cells"][col + "8"]["content"], '=IF(%s14=0,"-",%s14)' % (col, col))
        self.assertNotIn("D3", [r for r in sheet["cells"] if r.endswith(("15", "16"))])
        self.assertEqual(sheet["cells"]["D3"]["content"].startswith("=IFERROR("), True)
        self.assertEqual(sheet["rowNumber"], 14)
        self.assertEqual({k: v for k, v in sheet["rows"].items() if int(k) >= 10},
                         {"11": {"size": 23, "isHidden": True}, "12": {"size": 23, "isHidden": True},
                          "13": {"size": 23, "isHidden": True}})
        self.assertEqual(report["helper_rows"], 3)
        self.assertEqual(report["dash_cells"], 3 + 3 + 3 + 1)
        self.assertEqual([r for r in sheet["cells"] if r.endswith("12") and len(r) == 3], ["D12", "E12", "F12", "G12",
                                                                                           "H12", "I12"])

    def test_the_helper_sits_in_the_column_of_its_display_cell_and_reads_the_same_column(self):
        document, _ = bf.bind(bf.make_spec([bf.plain("G6:I6", columns=["m3", "m2", "m1"], zero="dash")]),
                              bf.make_baseline(cells={}))
        cells = document["sheets"][0]["cells"]
        self.assertEqual(cells["G12"]["content"], '=IFERROR(ODOO.LIST(1,1,"m3")+0,0)')
        self.assertEqual(cells["I12"]["content"], '=IFERROR(ODOO.LIST(1,1,"m1")+0,0)')
        self.assertEqual(cells["H6"]["content"], '=IF(H12=0,"-",H12)')

    def test_rows_stay_in_numeric_order(self):
        document, _ = bf.bind()
        self.assertEqual(list(document["sheets"][0]["rows"]), ["0", "4", "9", "11"])
        baseline = bf.small_baseline()
        baseline["sheets"][0]["rows"] = {"9": {"size": 22}, "4": {"size": 25}, "0": {"size": 30}}
        document, _ = bf.bind(bf.make_spec(), baseline)
        self.assertEqual(list(document["sheets"][0]["rows"]), ["0", "4", "9", "11"])
        self.assertEqual(document["sheets"][0]["rows"]["9"], {"size": 22})

    def test_a_column_binding_with_dash_uses_one_helper_row_per_target_row(self):
        document, report = bf.bind(bf.make_spec([bf.plain("D5:D7", columns=["m1", "m2", "m3"], zero="dash")]),
                                   bf.make_baseline(cells={}))
        cells = document["sheets"][0]["cells"]
        self.assertEqual([cells["D%d" % r]["content"] for r in (5, 6, 7)],
                         ['=IF(D12=0,"-",D12)', '=IF(D13=0,"-",D13)', '=IF(D14=0,"-",D14)'])
        self.assertEqual(report["helper_rows"], 3)
        self.assertEqual(document["sheets"][0]["rowNumber"], 14)

    def test_the_hidden_row_follows_the_area_size(self):
        baseline = bf.make_baseline(cells={}, row_number=31, col_number=17)
        document, _ = bf.bind(bf.make_spec([bf.plain("D5:F5", zero="dash")]), baseline)
        sheet = document["sheets"][0]
        self.assertEqual(sheet["rowNumber"], 33)
        self.assertEqual(sheet["rows"]["32"], {"size": 23, "isHidden": True})
        self.assertIn("D33", sheet["cells"])


# ---------------------------------------------------------------- 5. guards on the baseline

class TypedLeftoverTests(BindCase):
    def test_a_typed_number_outside_the_bindings_fails(self):
        baseline = bf.edit(bf.small_baseline(), {"H8": {"content": "7"}})
        message = self.refuse(bf.make_spec(), baseline, contains=("typed numbers left", "H8", "keep_typed"))
        self.assertIn("bind them or list them", message)

    def test_it_passes_when_listed(self):
        baseline = bf.edit(bf.small_baseline(), {"H8": {"content": "7"}})
        bf.bind(bf.make_spec(keep_typed=["H8"]), baseline)
        bf.bind(bf.make_spec(keep_typed=["G8:I8"]), baseline)

    def test_numeric_looking_text_counts_as_a_typed_number(self):
        for content in ("1.5", "-3", "0", "007", "-0.25", "123456789012345678901234567890"):
            with self.subTest(content=content):
                baseline = bf.edit(bf.small_baseline(), {"H8": {"content": content}})
                self.refuse(bf.make_spec(), baseline, contains="H8")

    def test_text_that_is_not_a_number_does_not(self):
        for content in ("1,5", "12abc", "+3", " 5", "5 ", "N/A", "", "x", "=5", "=-3", "--1"):
            with self.subTest(content=content):
                bf.bind(bf.make_spec(), bf.edit(bf.small_baseline(), {"H8": {"content": content}}))

    def test_only_string_contents_are_typed_numbers(self):
        for content in (None, [], {}):
            with self.subTest(content=content):
                bf.bind(bf.make_spec(), bf.edit(bf.small_baseline(), {"H8": {"content": content}}))

    def test_a_typed_number_in_a_non_text_cell_with_a_style_only_is_not_a_leftover(self):
        bf.bind(bf.make_spec(), bf.edit(bf.small_baseline(), {"H8": {"style": 1, "format": 1}}))

    def test_at_most_20_refs_are_listed_in_natural_order(self):
        cells = {"%s9" % intake_xlsx.col_letters(c): {"content": str(c)} for c in range(2, 27)}     # B9 .. Z9 (25)
        message = self.refuse(bf.make_spec(), bf.make_baseline(cells=cells, col_number=30), contains="B9, C9")
        listed = message.split(": ")[1].split(": bind")[0]
        refs = [part.strip() for part in listed.replace(" ...", "").split(",")]
        self.assertEqual(refs, ["%s9" % intake_xlsx.col_letters(c) for c in range(2, 22)][:20])
        self.assertTrue(message.split(": bind")[0].endswith(" ..."))
        self.assertNotIn("V9", message)
        self.assertNotIn("Z9", message)

    def test_exactly_20_leftovers_have_no_ellipsis_and_21_have(self):
        for count, ellipsis in ((20, False), (21, True)):
            with self.subTest(count=count):
                cells = {"%s9" % intake_xlsx.col_letters(c): {"content": "1"} for c in range(2, 2 + count)}
                message = self.refuse(bf.make_spec(), bf.make_baseline(cells=cells, col_number=30),
                                      contains="typed numbers left")
                self.assertEqual(" ..." in message, ellipsis)
                self.assertIn("U9", message)
                self.assertEqual("V9" in message, False)

    def test_the_order_is_row_then_column(self):
        baseline = bf.make_baseline(cells={"B2": {"content": "1"}, "A10": {"content": "2"}, "C1": {"content": "3"}})
        message = self.refuse(bf.make_spec([{"cells": "D5", "source": "secret", "column": "m1"}]), baseline,
                              contains="typed numbers left")
        self.assertIn("C1, B2, A10", message)

    def test_a_target_is_not_a_leftover(self):
        bf.bind(bf.make_spec([bf.plain("D5:F5"), bf.plain("D6:F6", "B")]), bf.small_baseline())

    def test_every_leftover_check_sees_only_the_baseline_cells(self):
        baseline = bf.make_baseline(cells={"H8": {"content": "7"}})
        self.refuse(bf.make_spec([{"cells": "D5", "source": "secret", "column": "m1"}]), baseline, contains="H8")


class DashReaderTests(BindCase):
    """A baseline formula that is not bound must never read a cell that shows `-` on zero."""

    def spec(self, zero):
        return bf.make_spec([{"cells": "D5", "source": "plain", "where": {"code": "A"}, "column": "m1", "zero": zero}])

    def baseline(self, formula, at="F9"):
        return bf.make_baseline(cells={"A1": {"content": "t"}, "D5": {"content": "0"}, at: {"content": formula}})

    HITS = ["=D5", "=$D$5", "=D5+1", "=2*D5", "=d5*2", "=$d$5", "=SUM(D5:D5)", "=SUM(C4:E6)", "=SUM(D5:E9)",
            "=SUM(A1:D5)", "=SUM(O6:D5)", "=SUM(E9:D5)", "=SUM(D$5:E$9)", "=IF(1,D5,0)", "=1+SUM(D4:D6)", "=D4+D5",
            "=SUM(D1:D10)", "=SUM(A5:P5)", "=SUM(A1:P10)", "=SUM(D5 : D6)", "=IF(1,\"x\",D5)"]
    MISSES = ["=D4", "=D6", "=C5", "=E5", "=SUM(E5:F9)", "=SUM(A1:C5)", "=SUM(D6:E9)", "=SUM(A1:D4)", "=SUM(E6:F9)",
              "=SUM(A1:C9)", "=SUM(E1:P10)", "=SUM(A6:P10)", "=SUM(A1:P4)", "=SUM(A5:C5)", "=SUM(E5:P5)", '="D5"',
              '=IF(1,"see D5",2)', '=IF(1,"D5:D6",2)', "=A1", "=SUM(1,2)", "=$E$5", "=SUM(E4:F6)"]

    def test_a_formula_that_reads_the_dash_cell_fails_and_names_both_cells(self):
        for formula in self.HITS:
            with self.subTest(formula=formula):
                message = self.refuse(self.spec("dash"), self.baseline(formula),
                                      contains=("the formula of F9 reads D5", "shows `-` on zero"))
                self.assertIn('"zero": "number"', message)

    def test_the_same_formula_passes_when_the_cell_is_a_number(self):
        for formula in self.HITS:
            with self.subTest(formula=formula):
                bf.bind(self.spec("number"), self.baseline(formula))

    def test_a_formula_that_reads_near_but_not_on_the_dash_cell_passes(self):
        for formula in self.MISSES:
            with self.subTest(formula=formula):
                bf.bind(self.spec("dash"), self.baseline(formula))

    def test_a_reference_only_inside_a_string_literal_does_not_count(self):
        for formula in ('="D5"', '=IF(1,"see D5",2)', '="A1:P10"', '="a"&"D5"', '=IF(1,"say ""D5",2)'):
            with self.subTest(formula=formula):
                try:
                    bf.bind(self.spec("dash"), self.baseline(formula))
                except bind_kpi.BindError as exc:
                    self.assertNotIn("reads D5", str(exc))

    def test_the_first_dash_cell_in_reading_order_is_named(self):
        spec = bf.make_spec([bf.plain("D5:F5", zero="dash")])
        message = self.refuse(spec, bf.edit(bf.small_baseline(typed_rows=(5,)), {"H9": {"content": "=SUM(E5:F5)"}}),
                              contains="the formula of H9 reads E5")
        self.assertNotIn("F5", message)

    def test_the_first_dash_cell_is_named_whatever_the_hash_order_of_the_set(self):
        # five dash cells D5:H5: a Python set of their (row, col) pairs iterates E5 first, the reading order starts at D5
        spec = bf.make_spec([{"cells": "D5:H5", "source": "wide", "columns": ["c000", "c001", "c002", "c003", "c004"],
                              "zero": "dash"}])
        baseline = bf.make_baseline(cells={"A9": {"content": "=SUM(D5:H5)"}}, merges=[])
        self.refuse(spec, baseline, contains="the formula of A9 reads D5")
        baseline = bf.make_baseline(cells={"A9": {"content": "=SUM(E5:H5)"}}, merges=[])
        self.refuse(spec, baseline, contains="the formula of A9 reads E5")

    def test_the_first_formula_in_reading_order_is_named(self):
        baseline = bf.edit(bf.small_baseline(typed_rows=(5,)), {"H9": {"content": "=D5"}, "G8": {"content": "=D5"},
                                                                "B10": {"content": "=D5"}})
        spec = bf.make_spec([bf.plain("D5:F5", zero="dash")])
        # D7:F7 read row 6 only, so the first reader is G8 (row 8 before row 9 and 10)
        self.refuse(spec, baseline, contains="the formula of G8 reads D5")

    def test_formulas_across_rows_and_columns(self):
        baseline = bf.edit(bf.small_baseline(typed_rows=(5,)), {"P1": {"content": "=SUM(D1:F10)"}})
        self.refuse(bf.make_spec([bf.plain("D5:F5", zero="dash")]), baseline, contains="the formula of P1 reads D5")
        bf.bind(bf.make_spec([bf.plain("D5:F5", zero="number")]), baseline)

    def test_a_bound_cell_may_read_a_dash_cell_because_it_is_replaced(self):
        baseline = bf.edit(bf.small_baseline(), {"D6": {"content": "=D5"}})
        bf.bind(bf.make_spec(), baseline)

    def test_the_baseline_formulas_of_the_default_design_read_only_number_cells(self):
        bf.bind(bf.make_spec([bf.plain("D5:F5", zero="dash"), bf.plain("D6:F6", "B", zero="number")]))
        self.refuse(bf.make_spec([bf.plain("D5:F5", zero="dash"), bf.plain("D6:F6", "B", zero="dash")]),
                    contains="the formula of D7 reads D6")

    def test_a_formula_reading_a_helper_row_cell_is_not_a_dash_reader(self):
        baseline = bf.edit(bf.small_baseline(), {"P2": {"content": "=D6"}})
        bf.bind(bf.make_spec([bf.plain("D5:F5", zero="dash"), bf.plain("D6:F6", "B")]), baseline)

    def test_a_dash_cell_far_down_is_found_by_a_range_that_just_reaches_it(self):
        baseline = bf.make_baseline(cells={"A1": {"content": "t"}, "P10": {"content": "=SUM(A1:O9)"},
                                           "O1": {"content": "=SUM(A1:O9)"}}, merges=[])
        spec = bf.make_spec([{"cells": "O9", "source": "plain", "where": {"code": "A"}, "column": "m1",
                              "zero": "dash"}])
        self.refuse(spec, baseline, contains="reads O9")
        spec = bf.make_spec([{"cells": "P9", "source": "plain", "where": {"code": "A"}, "column": "m1",
                              "zero": "dash"}])
        bf.bind(spec, baseline)
        spec = bf.make_spec([{"cells": "A10", "source": "plain", "where": {"code": "A"}, "column": "m1",
                              "zero": "dash"}])
        bf.bind(spec, baseline)


class UnknownFunctionTests(BindCase):
    def baseline(self, formula, at="F9"):
        return bf.edit(bf.small_baseline(), {at: {"content": formula}})

    def test_an_unregistered_function_outside_the_bindings_fails_and_is_named(self):
        for formula, name in (("=N(A1)", "N"), ("=n(A1)", "N"), ("=FOO(1)", "FOO"), ("=1+FOO.BAR(2)", "FOO.BAR"),
                              ("=SUM(1)+N (A1)", "N")):
            with self.subTest(formula=formula):
                message = self.refuse(bf.make_spec(), self.baseline(formula),
                                      contains="function %s() is not defined by the vendored client" % name)
                self.assertIn("F9", message)

    def test_registered_functions_pass(self):
        for formula in ("=SUM(1,2)", "=IF(1,2,3)", "=MAX(1,2)", "=IFERROR(1,2)", "=sum(1,2)", "=_t(\"x\")",
                        "=SUM(D6:F6)"):
            with self.subTest(formula=formula):
                bf.bind(bf.make_spec(), self.baseline(formula))

    def test_a_function_name_inside_a_string_literal_does_not_count(self):
        bf.bind(bf.make_spec(), self.baseline('="N(1)"'))
        bf.bind(bf.make_spec(), self.baseline('=IF(1,"FOO(2)",3)'))

    def test_a_bound_cell_is_replaced_before_the_check(self):
        baseline = bf.edit(bf.small_baseline(), {"D5": {"content": "=N(A1)"}})
        bf.bind(bf.make_spec(), baseline)

    def test_a_text_cell_is_not_a_formula(self):
        bf.bind(bf.make_spec(), self.baseline("N(A1) is text"))


class OneSheetTests(BindCase):
    def test_exactly_one_sheet(self):
        good = bf.small_baseline()
        sheet = good["sheets"][0]
        for baseline in ({"sheets": []}, {"sheets": [sheet, copy.deepcopy(sheet)]}, {"sheets": "x"}, {"sheets": None},
                         {"sheets": {"a": sheet}}, {}, [], None, "baseline", 5, {"sheets": [None]}, {"sheets": [5]},
                         {"sheets": [[]]}):
            with self.subTest(baseline=str(baseline)[:30]):
                self.refuse(bf.make_spec(), baseline, contains="exactly one sheet")


class MalformedSheetTests(BindCase):
    def broken(self, **changes):
        baseline = bf.small_baseline()
        sheet = baseline["sheets"][0]
        for key, value in changes.items():
            if value is KeyError:
                del sheet[key]
            else:
                sheet[key] = value
        return baseline

    def test_a_malformed_sheet_is_a_clean_bind_error(self):
        cases = [dict(cells=KeyError), dict(rowNumber=KeyError), dict(colNumber=KeyError), dict(rowNumber="ten"),
                 dict(colNumber="sixteen"), dict(rowNumber=None), dict(colNumber=None), dict(rowNumber=[10]),
                 dict(colNumber={}), dict(cells=[]), dict(cells="A1"), dict(cells=None), dict(cells=5),
                 dict(cells={"A1": "text"}), dict(cells={"A1": 5}), dict(cells={"A1": None}),
                 dict(cells={"A1": [1]}), dict(merges=None), dict(merges=5), dict(rowNumber=""),
                 dict(rowNumber="1e3")]
        for changes in cases:
            with self.subTest(changes=str({k: v if v is not KeyError else "<missing>" for k, v in changes.items()})):
                self.refuse(bf.make_spec(), self.broken(**changes), contains="the baseline sheet is malformed")

    def test_rows_that_is_not_an_object_is_a_clean_bind_error(self):
        # RED on purpose (finding F2): `dict(sheet.get("rows", {}))` runs outside the try block of the sheet shape
        # check, so these give a raw TypeError (through the command line: "error: unexpected TypeError", exit 1)
        for rows in (5, None, 1.5, True, [1, 2]):
            with self.subTest(rows=rows):
                self.refuse(bf.make_spec(), self.broken(rows=rows), contains="malformed")

    def test_a_malformed_merge_is_a_clean_error_of_either_class(self):
        for merges in (["A1"], ["A1:"], [5], ["A1:B"], "A1:B1"):
            with self.subTest(merges=merges):
                with self.assertRaises((bind_kpi.BindError, intake_xlsx.IntakeError)):
                    bf.bind(bf.make_spec(), self.broken(merges=merges))


class JsonNumberContentTests(BindCase):
    def test_a_typed_number_stored_as_a_json_number_left_outside_the_bindings_fails(self):
        # RED on purpose (finding F3b): only a STRING that looks like a number counts today. Odoo's xlsx import keeps
        # `<v>` text, so real baselines hold strings, but a hand-edited or other-route file with `"content": 7` would
        # leave a typed number behind without a word; failing loudly costs nothing (MAIN-VERIFY on real data)
        for content in (7, 12.5, -3, 0):
            with self.subTest(content=content):
                baseline = bf.edit(bf.small_baseline(), {"H8": {"content": content}})
                self.refuse(bf.make_spec(), baseline, contains=("typed numbers left", "H8"))

    def test_a_json_boolean_or_null_is_not_a_typed_number(self):
        for content in (True, False, None):
            with self.subTest(content=content):
                bf.bind(bf.make_spec(), bf.edit(bf.small_baseline(), {"H8": {"content": content}}))


class ScientificNotationTests(BindCase):
    """Excel writes small and large numbers in `<v>` as 1.5E-3 / 1E+20, and the client's own `isNumber` reads
    `1.5e-3` and `1e3` as numbers: they are typed numbers too (Spec invariant: no typed number left in a bound set)."""

    def test_a_typed_number_in_scientific_notation_left_outside_the_bindings_fails(self):
        # RED on purpose (finding F3): NUMBER = -?\d+(\.\d+)? does not match 1.5E-3
        for content in ("1.5E-3", "1E+20", "-2.5E-05", "1e3", "1.0000000000000001E-2"):
            with self.subTest(content=content):
                baseline = bf.edit(bf.small_baseline(), {"H8": {"content": content}})
                self.refuse(bf.make_spec(), baseline, contains=("typed numbers left", "H8"))


class BoundDoesNotCreateFreeText(BindCase):
    def test_a_binding_cannot_smuggle_text_into_a_formula(self):
        hostile = ['m1"),1)+SUM(A1', "m1\n", "m1)", "=m1", "m1 ", "M1", "../m1", "m1'"]
        for column in hostile:
            with self.subTest(column=column):
                with self.assertRaises(bind_kpi.BindError):
                    bf.bind(bf.make_spec([{"cells": "D5", "source": "plain", "where": {"code": "A"}, "column": column}]),
                            bf.make_baseline(cells={}))
        for code in ('A"]', "A\n", "a", "=A", "A,B", ["A"]):
            with self.subTest(code=code):
                with self.assertRaises(bind_kpi.BindError):
                    bf.bind(bf.make_spec([{"cells": "D5", "source": "plain", "where": {"code": code},
                                           "column": "m1"}]), bf.make_baseline(cells={}))

    def test_the_output_formulas_and_domains_hold_only_catalog_text(self):
        document, _ = bf.bind(structure_spec(), bf.make_baseline(cells={}))
        catalog = bf.small_catalog()["sources"]
        allowed_columns = {c for s in catalog.values() for c in s["value_columns"]} | {"code", "kind", "n"}
        for ref, cell in document["sheets"][0]["cells"].items():
            content = cell["content"]
            self.assertRegex(content, r'^=(IFERROR\()?ODOO\.LIST\([1-6],1,"([a-z0-9]+)"\)(\+0,0\))?$')
            self.assertIn(content.split('"')[1], allowed_columns)
        for listing in document["lists"].values():
            self.assertTrue(set(listing["columns"]) <= allowed_columns)
            for field, op, value in listing["domain"]:
                self.assertEqual(op, "=")
                self.assertIn(field, ("code", "kind", "n"))
                self.assertIn(value, ("A", "B", "x", 2, 3))


if __name__ == "__main__":
    unittest.main()
