"""Pure functions of scripts/intake_xlsx.py: cell references, formula checks, parsing, crop, serialisation.

Fixtures are synthetic (tests/intake_fixtures.py); nothing here talks to a server or touches the disk.
"""
import copy
import json
import os
import time
import unittest
from unittest import mock

import intake_fixtures as fx
from helpers import odoo_rpc

import intake_xlsx
from intake_xlsx import IntakeError


FEFF, NBSP, ZWSP = "\N{ZERO WIDTH NO-BREAK SPACE}", "\N{NO-BREAK SPACE}", "\N{ZERO WIDTH SPACE}"


def crop_error(data, sheet=fx.SHEET, area=fx.AREA):
    """The message of the IntakeError that `crop` raises (the test fails when it does not raise)."""
    try:
        intake_xlsx.crop(copy.deepcopy(data), sheet, area)
    except IntakeError as exc:
        return str(exc)
    raise AssertionError("crop did not raise IntakeError")


class ColumnAndReferenceTests(unittest.TestCase):
    def test_col_number_and_letters_are_inverse_and_bijective_base_26(self):
        self.assertEqual([intake_xlsx.col_number(c) for c in ("A", "Z", "AA", "AZ", "BA", "ZZ", "AAA", "ZZZ")],
                         [1, 26, 27, 52, 53, 702, 703, 18278])
        for number in list(range(1, 800)) + [18278]:
            self.assertEqual(intake_xlsx.col_number(intake_xlsx.col_letters(number)), number)
        self.assertEqual(intake_xlsx.col_number("ab"), 28)

    def test_col_letters_of_the_boundaries(self):
        self.assertEqual([intake_xlsx.col_letters(n) for n in (1, 26, 27, 52, 53, 702, 703)],
                         ["A", "Z", "AA", "AZ", "BA", "ZZ", "AAA"])
        self.assertEqual(intake_xlsx.col_letters(0), "")

    def test_split_ref_gives_row_then_column(self):
        self.assertEqual(intake_xlsx.split_ref("D9"), (9, 4))
        self.assertEqual(intake_xlsx.split_ref("A1"), (1, 1))
        self.assertEqual(intake_xlsx.split_ref("ZZZ1048576"), (1048576, 18278))

    def test_split_ref_refuses_anything_that_is_not_a_canonical_address(self):
        for bad in ("d9", "9D", "A0", "A", "", "A-1", "AAAA1", "A1 ", " A1", "A1:B2", "$A$1", None, 5, ["A1"]):
            with self.subTest(bad=bad), self.assertRaises(IntakeError):
                intake_xlsx.split_ref(bad)

    def test_parse_area_returns_last_row_and_last_column(self):
        self.assertEqual(intake_xlsx.parse_area("A1:Q31"), (31, 17))
        self.assertEqual(intake_xlsx.parse_area("A1:A1"), (1, 1))

    def test_parse_area_refuses_bad_text(self):
        for bad in ("B1:C3", "A1", "A1:", "", "foo", "a1:c3", "A1:B2:C3", "A1:A0", "A1:", ":B2", "A2:B3", None, 7):
            with self.subTest(bad=bad), self.assertRaises(IntakeError):
                intake_xlsx.parse_area(bad)

    def test_parse_area_size_limit_is_exactly_max_area_cells(self):
        self.assertEqual(intake_xlsx.MAX_AREA_CELLS, 1000000)
        self.assertEqual(intake_xlsx.parse_area("A1:B500000"), (500000, 2))
        with self.assertRaises(IntakeError):
            intake_xlsx.parse_area("A1:B500001")
        with self.assertRaises(IntakeError):
            intake_xlsx.parse_area("A1:ZZZ1000000")


class FormulaCheckTests(unittest.TestCase):
    def test_formula_problem_is_none_for_plain_formulas_and_text_in_quotes(self):
        for formula in ("=A1+B2", "=SUM(A1:B2)", '="say what!"', '="Z99"', '=IF(A1="a!b",1,2)',
                        '="she said ""hi!"" to Z99"', '="A:A"', '="1:1"', "=LOG10(100)", "=A1*1.5", "=$A$1+$E$6"):
            with self.subTest(formula=formula):
                self.assertIsNone(intake_xlsx.formula_problem(formula))

    def test_formula_problem_flags_another_sheet(self):
        for formula in ("=Other!A1", "='Sheet Two'!A1", "=SUM(Other!A1:B2)", "=A1+'x y'!B2", '="ok"&Other!A1'):
            with self.subTest(formula=formula):
                self.assertEqual(intake_xlsx.formula_problem(formula), "reads another sheet")

    def test_formula_problem_flags_whole_row_and_column_ranges(self):
        for formula in ("=SUM(A:A)", "=SUM($A:$B)", "=SUM(A:C)", "=SUM(1:1)", "=SUM($2:$4)", "=COUNTA(B:B)+1"):
            with self.subTest(formula=formula):
                self.assertEqual(intake_xlsx.formula_problem(formula), "uses a whole row or column range")

    def test_formula_refs_are_cell_references_outside_string_literals(self):
        self.assertEqual(intake_xlsx.formula_refs("=A1+Z99"), [(1, 1), (99, 26)])
        self.assertEqual(intake_xlsx.formula_refs("=$A$1+$B2+C$3"), [(1, 1), (2, 2), (3, 3)])
        self.assertEqual(intake_xlsx.formula_refs("=SUM(A1:B2)"), [(1, 1), (2, 2)])
        self.assertEqual(intake_xlsx.formula_refs('="Z99"&A1'), [(1, 1)])
        self.assertEqual(intake_xlsx.formula_refs("=a1"), [(1, 1)])
        self.assertEqual(intake_xlsx.formula_refs("=ZZZ9"), [(9, 18278)])

    def test_function_names_with_digits_are_not_references(self):
        self.assertEqual(intake_xlsx.formula_refs("=LOG10(A1)"), [(1, 1)])
        self.assertEqual(intake_xlsx.formula_refs("=ATAN2(A1,B1)"), [(1, 1), (1, 2)])
        self.assertEqual(intake_xlsx.formula_refs("=LOG10(100)"), [])

    def test_a_reference_inside_a_longer_name_is_not_a_reference(self):
        for formula in ("=ABCD5", "=Name_A1", "=FOO.A1", "=1E5"):
            with self.subTest(formula=formula):
                self.assertEqual(intake_xlsx.formula_refs(formula), [])
        self.assertEqual(intake_xlsx.formula_refs("=1E5+A2"), [(2, 1)])


class ParseSnapshotTests(unittest.TestCase):
    def test_a_valid_workbook_is_returned_as_a_dict(self):
        data = fx.make_snapshot()
        self.assertEqual(intake_xlsx.parse_snapshot(fx.as_bytes(data)), data)

    def test_a_utf8_bom_is_accepted(self):
        data = fx.make_snapshot()
        self.assertEqual(intake_xlsx.parse_snapshot(fx.as_bytes(data, bom=True)), data)

    def test_data_that_is_not_a_spreadsheet_gives_a_clean_error(self):
        bad = {
            "not json": b"hello",
            "invalid utf-8": b"\xff\xfe{",
            "empty": b"",
            "json list": b"[]",
            "json string": b'"x"',
            "no sheets key": b'{"version": 1}',
            "empty sheets": b'{"sheets": []}',
            "sheets not a list": b'{"sheets": {"a": 1}}',
            "sheet not an object": b'{"sheets": [1]}',
            "sheet without cells": b'{"sheets": [{"name": "A"}]}',
            "cells is a list": b'{"sheets": [{"name": "A", "cells": []}]}',
            "sheet without name": b'{"sheets": [{"cells": {}}]}',
            "sheet name not text": b'{"sheets": [{"name": 5, "cells": {}}]}',
            "second sheet broken": b'{"sheets": [{"name": "A", "cells": {}}, {"name": "B"}]}',
            "deeply nested": b"[" * 100000,
            "deeply nested object": b'{"a":' * 100000,
        }
        for label, raw in bad.items():
            with self.subTest(label), self.assertRaises(IntakeError) as caught:
                intake_xlsx.parse_snapshot(raw)
            self.assertEqual(str(caught.exception).count("\n"), 0)
            self.assertIsNone(caught.exception.__cause__)

    def test_size_limit_is_odoo_rpc_max_response_and_the_limit_itself_is_allowed(self):
        raw = fx.as_bytes(fx.make_snapshot())
        with mock.patch.object(odoo_rpc, "MAX_RESPONSE", len(raw)):
            self.assertEqual(intake_xlsx.parse_snapshot(raw)["version"], 14.5)
        with mock.patch.object(odoo_rpc, "MAX_RESPONSE", len(raw) - 1), self.assertRaises(IntakeError) as caught:
            intake_xlsx.parse_snapshot(raw)
        self.assertIn("larger than", str(caught.exception))


class InventoryTests(unittest.TestCase):
    def test_one_row_per_sheet_with_content_cells_and_last_cell(self):
        rows = intake_xlsx.inventory(fx.make_snapshot())
        self.assertEqual(rows, [
            {"name": fx.SHEET, "visible": True, "content_cells": 8, "last_cell": "G30"},
            {"name": fx.OTHER_SHEET, "visible": True, "content_cells": 1, "last_cell": "A1"}])

    def test_style_only_empty_and_null_cells_do_not_count_and_zero_does(self):
        cells = {"A1": {"style": 1}, "B2": {"content": ""}, "C3": {"content": None}, "D4": {"content": 0},
                 "E5": {"content": "x"}, "bad": {"content": "y"}, "F6": "not a dict"}
        rows = intake_xlsx.inventory(fx.make_snapshot(sheets=[fx.make_sheet(cells=cells)]))
        self.assertEqual(rows[0]["content_cells"], 2)
        self.assertEqual(rows[0]["last_cell"], "E5")

    def test_a_sheet_without_content_has_an_empty_last_cell_and_hidden_is_reported(self):
        sheet = fx.make_sheet(cells={"A1": {"style": 1}}, isVisible=False)
        rows = intake_xlsx.inventory(fx.make_snapshot(sheets=[sheet]))
        self.assertEqual(rows, [{"name": fx.SHEET, "visible": False, "content_cells": 0, "last_cell": ""}])

    def test_a_sheet_without_isvisible_counts_as_visible(self):
        sheet = fx.make_sheet()
        del sheet["isVisible"]
        self.assertTrue(intake_xlsx.inventory(fx.make_snapshot(sheets=[sheet]))[0]["visible"])


class CropResultTests(unittest.TestCase):
    def setUp(self):
        self.raw = fx.make_snapshot()
        self.baseline, self.report = fx.crop_it(self.raw)
        self.sheet = self.baseline["sheets"][0]

    def test_report_counts(self):
        self.assertEqual(self.report, {"sheet": fx.SHEET, "area": fx.AREA, "cells": 6, "merges": 2,
                                       "content_cells": 5, "dropped_content_cells": 3})

    def test_only_the_chosen_sheet_remains_and_the_cells_are_in_natural_row_major_order(self):
        self.assertEqual(len(self.baseline["sheets"]), 1)
        self.assertEqual(self.sheet["name"], fx.SHEET)
        self.assertEqual(list(self.sheet["cells"]), ["A1", "C1", "A2", "B2", "D3", "E6"])

    def test_cell_contents_are_kept_exactly(self):
        original = fx.design_cells()
        self.assertEqual(self.sheet["cells"], {ref: original[ref] for ref in self.sheet["cells"]})
        self.assertEqual(self.sheet["cells"]["C1"]["content"], fx.NON_ASCII)

    def test_merges_rows_cols_and_dimensions_are_cropped(self):
        self.assertEqual(self.sheet["merges"], ["A1:B1", "D5:E6"])
        self.assertEqual(list(self.sheet["rows"]), ["0", "5"])
        self.assertEqual(list(self.sheet["cols"]), ["0", "4"])
        self.assertEqual((self.sheet["rowNumber"], self.sheet["colNumber"]), (6, 5))

    def test_every_other_sheet_key_and_every_other_top_level_key_is_kept(self):
        for key, value in self.raw["sheets"][0].items():
            if key not in ("cells", "merges", "rows", "cols", "rowNumber", "colNumber"):
                self.assertEqual(self.sheet[key], value, key)
        for key, value in self.raw.items():
            if key not in ("sheets", "revisionId"):
                self.assertEqual(self.baseline[key], value, key)
        self.assertEqual(set(self.baseline), set(self.raw))

    def test_revision_id_is_the_fixed_baseline_value(self):
        self.assertEqual(self.baseline["revisionId"], "START_REVISION")
        self.assertEqual(intake_xlsx.BASELINE_REVISION, "START_REVISION")
        self.assertNotEqual(self.raw["revisionId"], "START_REVISION")

    def test_the_input_is_not_modified_by_the_crop(self):
        raw = fx.make_snapshot()
        frozen = copy.deepcopy(raw)
        intake_xlsx.crop(raw, fx.SHEET, fx.AREA)
        self.assertEqual(raw, frozen)

    def test_the_output_cells_and_tables_share_nothing_with_the_input(self):
        raw = fx.make_snapshot()
        frozen = copy.deepcopy(raw)
        baseline, _ = intake_xlsx.crop(raw, fx.SHEET, fx.AREA)
        baseline["sheets"][0]["cells"]["A1"]["content"] = "changed"
        baseline["styles"]["1"]["bold"] = False
        baseline["settings"]["locale"]["code"] = "xx"
        self.assertEqual(raw, frozen)

    def test_the_output_rows_and_cols_share_nothing_with_the_input(self):
        raw = fx.make_snapshot()
        frozen = copy.deepcopy(raw)
        baseline, _ = intake_xlsx.crop(raw, fx.SHEET, fx.AREA)
        baseline["sheets"][0]["rows"]["0"]["size"] = 1
        baseline["sheets"][0]["cols"]["0"]["size"] = 1
        self.assertEqual(raw, frozen)

    def test_cell_order_does_not_depend_on_the_input_order(self):
        reversed_cells = dict(reversed(list({**fx.design_cells(), **fx.padding_cells()}.items())))
        other, _ = fx.crop_it(fx.make_snapshot(sheets=[fx.make_sheet(cells=reversed_cells)]))
        self.assertEqual(list(other["sheets"][0]["cells"]), ["A1", "C1", "A2", "B2", "D3", "E6"])
        self.assertEqual(json.dumps(other), json.dumps(self.baseline))

    def test_row_numbers_sort_numerically_and_columns_by_length_then_letters(self):
        cells = {"A10": {"content": "a"}, "A2": {"content": "b"}, "B1": {"content": "c"}, "A1": {"content": "d"},
                 "AA1": {"content": "e"}, "Z1": {"content": "f"}, "E12": {"content": "g"}}
        base, _ = fx.crop_it(fx.make_snapshot(sheets=[fx.make_sheet(cells=cells, merges=[])]), area="A1:AB12")
        self.assertEqual(list(base["sheets"][0]["cells"]), ["A1", "B1", "Z1", "AA1", "A2", "A10", "E12"])

    def test_the_corner_cell_is_kept_and_its_neighbours_outside_are_dropped(self):
        cells = {"E6": {"content": "in"}, "F6": {"content": "out"}, "E7": {"content": "out"}, "A1": {"content": "x"}}
        base, report = fx.crop_it(fx.make_snapshot(sheets=[fx.make_sheet(cells=cells, merges=[])]))
        self.assertEqual(list(base["sheets"][0]["cells"]), ["A1", "E6"])
        self.assertEqual(report["dropped_content_cells"], 2)

    def test_a_one_cell_area_works(self):
        base, report = fx.crop_it(fx.make_snapshot(sheets=[fx.make_sheet(merges=[])]), area="A1:A1")
        self.assertEqual(list(base["sheets"][0]["cells"]), ["A1"])
        self.assertEqual((report["cells"], report["merges"]), (1, 0))
        self.assertEqual(base["sheets"][0]["merges"], [])
        self.assertEqual(list(base["sheets"][0]["rows"]), ["0"])
        self.assertEqual(list(base["sheets"][0]["cols"]), ["0"])

    def test_the_whole_sheet_can_be_the_area(self):
        base, _ = fx.crop_it(self.raw, area="A1:AD1000")
        self.assertEqual((base["sheets"][0]["rowNumber"], base["sheets"][0]["colNumber"]), (1000, 30))
        self.assertEqual(len(base["sheets"][0]["cells"]), 6 + 4 + 3)

    def test_dropped_content_counts_content_only_and_zero_is_content(self):
        cells = {"A1": {"content": "x"}, "H1": {"style": 1}, "H2": {"content": ""}, "H3": {"content": None},
                 "H4": {"content": 0}, "H5": {"content": "y"}}
        _, report = fx.crop_it(fx.make_snapshot(sheets=[fx.make_sheet(cells=cells, merges=[])]))
        self.assertEqual(report["dropped_content_cells"], 2)
        self.assertEqual(report["content_cells"], 1)

    def test_the_second_sheet_can_be_chosen_and_the_first_is_dropped(self):
        base, report = fx.crop_it(self.raw, sheet=fx.OTHER_SHEET, area="A1:B2")
        self.assertEqual([s["name"] for s in base["sheets"]], [fx.OTHER_SHEET])
        self.assertEqual(report["cells"], 1)

    def test_a_sheet_without_rows_cols_or_merges_keys_is_cropped(self):
        sheet = fx.make_sheet()
        for key in ("rows", "cols", "merges"):
            del sheet[key]
        base, _ = fx.crop_it(fx.make_snapshot(sheets=[sheet]))
        self.assertEqual((base["sheets"][0]["rows"], base["sheets"][0]["cols"], base["sheets"][0]["merges"]),
                         ({}, {}, []))

    def test_problems_in_another_sheet_do_not_block_the_chosen_one(self):
        other = fx.make_sheet(fx.OTHER_SHEET, cells={"A1": {"content": "o"}}, figures=[{"id": "f"}], mystery=1)
        base, _ = fx.crop_it(fx.make_snapshot(sheets=[fx.make_sheet(), other]))
        self.assertEqual(base["sheets"][0]["name"], fx.SHEET)


class DeterminismTests(unittest.TestCase):
    def test_the_same_input_gives_the_same_bytes(self):
        raw = fx.make_snapshot()
        first = intake_xlsx.dump_bytes(fx.crop_it(raw)[0])
        second = intake_xlsx.dump_bytes(fx.crop_it(raw)[0])
        self.assertEqual(first, second)

    def test_two_workbooks_that_differ_only_in_revision_id_give_the_same_bytes(self):
        one = intake_xlsx.dump_bytes(fx.crop_it(fx.make_snapshot(revision_id="uuid-one"))[0])
        two = intake_xlsx.dump_bytes(fx.crop_it(fx.make_snapshot(revision_id="START_REVISION"))[0])
        self.assertEqual(one, two)

    def test_workbooks_that_differ_in_the_insertion_order_of_cells_give_the_same_bytes(self):
        cells = {**fx.design_cells(), **fx.padding_cells()}
        shuffled = dict(sorted(cells.items(), reverse=True))
        one = intake_xlsx.dump_bytes(fx.crop_it(fx.make_snapshot(sheets=[fx.make_sheet(cells=cells)]))[0])
        two = intake_xlsx.dump_bytes(fx.crop_it(fx.make_snapshot(sheets=[fx.make_sheet(cells=shuffled)]))[0])
        self.assertEqual(one, two)


class DumpBytesTests(unittest.TestCase):
    def test_bytes_are_utf8_lf_only_with_one_final_newline_and_unescaped_text(self):
        data = intake_xlsx.dump_bytes({"text": fx.NON_ASCII, "multi": "a\nb", "list": [1, 2]})
        self.assertIsInstance(data, bytes)
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.endswith(b"}\n"))
        self.assertFalse(data.endswith(b"\n\n"))
        self.assertIn(fx.NON_ASCII.encode("utf-8"), data)
        self.assertNotIn(b"\\u", data)
        self.assertIn(b"a\\nb", data)
        self.assertEqual(json.loads(data.decode("utf-8")), {"text": fx.NON_ASCII, "multi": "a\nb", "list": [1, 2]})

    def test_the_layout_is_indent_one(self):
        self.assertEqual(intake_xlsx.dump_bytes({"a": {"b": 1}}), b'{\n "a": {\n  "b": 1\n }\n}\n')

    def test_bytes_do_not_depend_on_os_linesep(self):
        plain = intake_xlsx.dump_bytes({"a": [1, 2], "b": "x"})
        with mock.patch.object(os, "linesep", "\r\n"):
            self.assertEqual(intake_xlsx.dump_bytes({"a": [1, 2], "b": "x"}), plain)

    def test_a_lone_surrogate_is_a_clean_intake_error(self):
        with self.assertRaises(IntakeError) as caught:
            intake_xlsx.dump_bytes({"text": "\ud800"})
        self.assertIn("UTF-8", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)


class MergeGuardTests(unittest.TestCase):
    def merges(self, *merges):
        return fx.make_snapshot(sheets=[fx.make_sheet(merges=list(merges))])

    def test_a_merge_fully_inside_is_kept(self):
        base, _ = fx.crop_it(self.merges("A5:E6", "A1:A1", "B2:C3"))
        self.assertEqual(base["sheets"][0]["merges"], ["A5:E6", "A1:A1", "B2:C3"])

    def test_a_merge_fully_outside_is_dropped(self):
        base, report = fx.crop_it(self.merges("F5:G6", "A7:B8", "H1:I2"))
        self.assertEqual((base["sheets"][0]["merges"], report["merges"]), ([], 0))

    def test_a_merge_straddling_the_right_or_bottom_edge_fails(self):
        for merge in ("D5:F6", "E6:F7", "A5:A8", "A1:F1", "E1:E7", "E6:F6", "A6:A7"):
            with self.subTest(merge=merge):
                self.assertIn("straddles", crop_error(self.merges(merge)))

    def test_a_merge_starting_outside_and_ending_inside_fails(self):
        self.assertIn("straddles", crop_error(self.merges("F1:E2")))

    def test_the_straddling_merge_is_named_in_the_message(self):
        self.assertIn("D5:F6", crop_error(self.merges("A1:B1", "D5:F6")))

    def test_malformed_merges_fail(self):
        for merge in ("A1", "", None, 5, "A1:B2:C3", "A1:", ["A1:B2"], "a1:b2"):
            with self.subTest(merge=merge), self.assertRaises(IntakeError):
                intake_xlsx.crop(self.merges(merge), fx.SHEET, fx.AREA)

    def test_a_merge_without_a_colon_is_reported_as_malformed(self):
        for merge in ("A1", "", None, 5, ["A1:B2"], "A1:"):
            with self.subTest(merge=merge):
                self.assertIn("malformed merge", crop_error(self.merges(merge)))


class FormulaGuardTests(unittest.TestCase):
    def test_formulas_reading_inside_the_area_pass(self):
        for formula in ("=SUM(B2:C2)", "=A1+E6", "=$A$1+$E$6", "=SUM($A$1:$E$6)", "=LOG10(A1)", "=ATAN2(A1,B1)",
                        "=ABCD5", '="Z99"', '="sheet!"&A1', '=IF(A1="x!",B2,"Z99")', "=1E5+A1", "=A1*1.5",
                        '="she said ""Z99!"""', "=NOW()", "=5"):
            with self.subTest(formula=formula):
                base, _ = fx.crop_it(fx.with_formula(formula))
                self.assertEqual(base["sheets"][0]["cells"]["D4"]["content"], formula)

    def test_formulas_reading_outside_the_area_fail(self):
        for formula in ("=F1", "=A7", "=E7", "=F6", "=Z99", "=$F$1", "=SUM(A1:F6)", "=SUM(A1:E7)", "=A1+AD1000",
                        "=B2*2+Z99", '="x"&F1', "=IF(A1>0,F2,1)"):
            with self.subTest(formula=formula):
                self.assertIn("reads outside", crop_error(fx.with_formula(formula)))

    def test_formulas_reading_another_sheet_fail(self):
        for formula in ("=Other!A1", "='Sheet Two'!A1", "=SUM(Other!A1:B2)", "=A1+Other!A1"):
            with self.subTest(formula=formula):
                self.assertIn("another sheet", crop_error(fx.with_formula(formula)))

    def test_formulas_with_whole_row_or_column_ranges_fail(self):
        for formula in ("=SUM(A:A)", "=SUM($A:$B)", "=SUM(1:1)", "=SUM($2:$4)"):
            with self.subTest(formula=formula):
                self.assertIn("whole row or column", crop_error(fx.with_formula(formula)))

    def test_the_failing_formula_cell_is_named(self):
        self.assertIn("D4", crop_error(fx.with_formula("=F1")))

    def test_a_formula_outside_the_area_is_not_checked(self):
        data = fx.with_cell("H9", {"content": "=SUM(Other!A:A)+ZZ99"})
        base, _ = fx.crop_it(data)
        self.assertNotIn("H9", base["sheets"][0]["cells"])

    def test_text_cells_that_are_not_formulas_are_not_checked(self):
        for content in ("see Other!A1", "Z99", "x =F1", 99):
            with self.subTest(content=content):
                base, _ = fx.crop_it(fx.with_cell("D4", {"content": content}))
                self.assertEqual(base["sheets"][0]["cells"]["D4"]["content"], content)


class UnsupportedContentGuardTests(unittest.TestCase):
    def test_unknown_top_level_key_fails_and_is_named(self):
        self.assertIn("mystery", crop_error(fx.make_snapshot(mystery=1)))

    def test_non_empty_odoo_collections_fail(self):
        values = {"entities": {"x": {"a": 1}}, "lists": {"1": {"id": "1"}}, "pivots": {"1": {"id": "1"}},
                  "globalFilters": [{"id": "f"}], "chartOdooMenusReferences": {"c": "menu"}}
        for key, value in values.items():
            with self.subTest(key=key):
                self.assertIn(key, crop_error(fx.make_snapshot(**{key: value})))

    def test_empty_odoo_collections_pass(self):
        fx.crop_it(fx.make_snapshot(entities={}, lists={}, pivots={}, globalFilters=[], chartOdooMenusReferences={}))

    def test_unknown_sheet_key_fails_and_is_named(self):
        self.assertIn("mystery", crop_error(fx.with_sheet_extra(mystery=True)))

    def test_non_empty_sheet_features_fail(self):
        values = {"figures": [{"id": "f"}], "conditionalFormats": [{"id": "c"}], "dataValidationRules": [{"id": "d"}],
                  "filterTables": [{"id": "t"}]}
        for key, value in values.items():
            with self.subTest(key=key):
                self.assertIn(key, crop_error(fx.with_sheet_extra(**{key: value})))

    def test_grouped_rows_or_columns_fail(self):
        for groups in ({"ROW": [{"start": 1, "end": 2}], "COL": []}, {"ROW": [], "COL": [{"start": 0, "end": 1}]}):
            with self.subTest(groups=groups):
                self.assertIn("grouped", crop_error(fx.with_sheet_extra(headerGroups=groups)))
        self.assertIn("grouped", crop_error(fx.with_sheet_extra(headerGroups=[1])))

    def test_unknown_cell_property_inside_the_area_fails(self):
        for cell in ({"content": "x", "note": "n"}, {"link": "u"}, {"style": 1, "rich": []}):
            with self.subTest(cell=cell):
                message = crop_error(fx.with_cell("D4", cell))
                self.assertIn("D4", message)
                self.assertIn("does not know", message)

    def test_cells_that_are_not_objects_fail(self):
        for value in ("text", 5, [], None):
            with self.subTest(value=value):
                self.assertIn("not an object", crop_error(fx.with_cell("D4", value)))

    def test_a_cell_that_is_not_an_object_fails_even_outside_the_area(self):
        self.assertIn("not an object", crop_error(fx.with_cell("Z90", "text")))

    def test_cell_addresses_that_are_not_canonical_fail_cleanly(self):
        for ref in ("a1", "A0", "1A", "A-1", ""):
            with self.subTest(ref=ref):
                self.assertIn("not a cell reference", crop_error(fx.with_cell(ref, {"content": "x"})))


class LinkGuardTests(unittest.TestCase):
    """Combined security review M4: the dashboard is shown to EVERY internal user, so a clickable link (a markdown link,
    a web / mail / odoo link, a HYPERLINK formula) in the design area is refused loudly, never copied."""

    LINK_MESSAGE = ("cell D4 (%s) holds a link: links are not copied into a dashboard that every internal user sees; "
                    "remove it from the design area")
    CALL_MESSAGE = "formula of D4 (%s) calls HYPERLINK: links are not copied into a dashboard that every internal user sees"

    # one family per alternative of the pattern, each with NO other alternative inside (so a dropped one stays visible)
    LINKS = {
        "markdown": ("[open](#top)", "[x](y)", "a](b", "[a b](c d)", "[x](o-spreadsheet://abc)", "see [x](y) now",
                     "[x]()", "x]("),
        "http": ("see http://x.example", "http://x", "HTTP://X.EXAMPLE", "Http://x", "go: http://x.example/a?b=c now"),
        "https": ("https://x.example", "see https://x.example/a", "HTTPS://X.EXAMPLE", "HttpS://x", "(https://x)"),
        "ftp": ("ftp://x.example/file", "FTP://X", "get ftp://x"),
        "file": ("file:///etc/hosts", "FILE://x", "open file://x"),
        "odoo": ("odoo://view/{}", "ODOO://x", "see odoo://x"),
        "mailto": ("mailto:a@b.example", "MAILTO:a@b.example", "write mailto:x", "Mailto:x"),
        "javascript": ("javascript:alert(1)", "JAVASCRIPT:x", "JavaScript:void(0)", "do javascript:x"),
    }
    # the same words as plain labels, a colon or parentheses or brackets: not links
    LABELS = ("Total", "Total: 10", "Rate (%)", "(%)", "1:2", "Ratio 1:2", "Time 10:30", "A:B", "Note: see (a)",
              "Phone: 555-1234", "Re: plan", "e-mail", "[1] (note)", "x] (y", "tỷ lệ (%)", "Tỷ lệ (%)",
              "합계 (원)", "합계: 10", "http", "https:", "file:", "odoo:", "www.x.example",
              "ftp", "mailto", "java script:", "javascript", "$", "=", "=A1+B2", '="a (b)"', "a ( b )", "[", "]", "(", ")",
              "mail to: x", "an email: a at b", "Size: 12 (cm)", "1) first (a)", "ODOO", "Odoo / Gestión", "file", "FTP")
    # words that only END in a scheme name are no link (the pattern needs a word boundary before it)
    GLUED = ("emailto: a", "nonhttps://x", "subfile://x", "profile://x", "xhttp://x", "anodoo://x", "myftp://x",
             "ejavascript:x")

    def test_every_family_of_link_is_refused_with_the_cell_and_the_text(self):
        for family, texts in self.LINKS.items():
            for text in texts:
                with self.subTest(family=family, text=text):
                    self.assertEqual(crop_error(fx.with_cell("D4", {"content": text})), self.LINK_MESSAGE % text)

    def test_a_link_is_refused_in_a_formula_text_too(self):
        # the pattern runs on the whole content, `=` formulas included (a literal of a formula is shown to everyone)
        for formula in ('="see https://x.example"', '=A1&"[x](y)"', '="mailto:a@b.example"', '="javascript:x"&B2',
                        '=IF(A1,"odoo://view/{}","")', '="ftp://x"'):
            with self.subTest(formula=formula):
                self.assertEqual(crop_error(fx.with_formula(formula)), self.LINK_MESSAGE % formula)

    def test_a_link_cell_of_any_row_or_column_in_the_area_is_found_and_the_first_one_is_named(self):
        sheet = fx.make_sheet(cells={"E6": {"content": "https://x.example"}, "B2": {"content": "[x](y)"},
                                     "A1": {"content": "Title"}}, merges=[])
        message = crop_error(fx.make_snapshot(sheets=[sheet]))
        self.assertEqual(message, "cell B2 ([x](y)) holds a link: links are not copied into a dashboard that every "
                                  "internal user sees; remove it from the design area")
        for ref in ("A1", "E6", "C3"):
            with self.subTest(ref=ref):
                self.assertIn("cell %s (" % ref, crop_error(fx.with_cell(ref, {"content": "https://x.example"})))

    def test_a_link_outside_the_area_is_cropped_away_and_not_an_error(self):
        for ref, text in (("F1", "https://x.example"), ("A7", "[x](y)"), ("Z50", "=HYPERLINK(A1)")):
            with self.subTest(ref=ref):
                base, report = fx.crop_it(fx.with_cell(ref, {"content": text}))
                self.assertNotIn(ref, base["sheets"][0]["cells"])
                self.assertNotIn(text, json.dumps(base))
        base, report = fx.crop_it(fx.with_cell("F9", {"content": "https://x.example"}))
        self.assertEqual(report["dropped_content_cells"], 4)          # the fixture's own three + this one

    def test_a_long_link_is_cut_in_the_message(self):
        message = crop_error(fx.with_cell("D4", {"content": "https://x.example/" + "a" * 500}))
        self.assertIn("(https://x.example/" + "a" * 62 + "...)", message)           # 80 characters, then `...`
        self.assertLess(len(message), 300)
        # characters that cannot be printed are shown as `?`, never sent to the terminal
        self.assertIn("cell D4 (?](x) holds a link", crop_error(fx.with_cell("D4", {"content": ZWSP + "](x"})))

    def test_a_number_a_missing_content_or_a_text_without_a_link_does_not_trip_the_guard(self):
        for cell in ({"content": 12.5}, {"content": 0}, {"content": True}, {"style": 1}, {"content": ""},
                     {"content": None}):
            with self.subTest(cell=cell):
                fx.crop_it(fx.with_cell("D4", cell))

    def test_normal_labels_are_not_links(self):
        for text in self.LABELS:
            with self.subTest(text=text):
                base, _ = fx.crop_it(fx.with_cell("D4", {"content": text}))
                self.assertEqual(base["sheets"][0]["cells"]["D4"]["content"], text)

    def test_a_word_that_only_ends_in_a_scheme_name_is_not_a_link(self):
        for text in self.GLUED:
            with self.subTest(text=text):
                fx.crop_it(fx.with_cell("D4", {"content": text}))

    def test_the_fixture_baselines_still_crop_and_keep_their_text(self):
        base, report = fx.crop_it(fx.make_snapshot())
        self.assertEqual(report["content_cells"], 5)
        self.assertEqual(base["sheets"][0]["cells"]["A1"]["content"], "Title")
        self.assertEqual(base["sheets"][0]["cells"]["C1"]["content"], fx.NON_ASCII)

    # ---- HYPERLINK in a formula
    def test_a_formula_that_calls_hyperlink_is_refused_in_every_spelling(self):
        for formula in ("=HYPERLINK(A1)", "=HYPERLINK(A1,B1)", "=hyperlink(A1)", "=Hyperlink(A1,B1)", "=HYPERLINK (A1)",
                        "=hyperlink (A1)", "=HYPERLINK" + FEFF + "(A1)", "=HYPERLINK" + FEFF + FEFF + " (A1)",
                        "=HYPERLINK\t(A1)", "=HYPERLINK\n(A1)", "=HYPERLINK" + NBSP + "(A1)", "=1+HYPERLINK(A1)",
                        "=SUM(1)&HYPERLINK(A1,B1)", '=IF(A1,HYPERLINK(B1),"")', '="x"&HYPERLINK(A1)',
                        "=(HYPERLINK(A1))", "=-HYPERLINK(A1)", "=A1,HYPERLINK(B1)", "=HYPERLINK(HYPERLINK(A1))",
                        "=" + FEFF + "HYPERLINK(A1)"):
            with self.subTest(formula=formula):
                message = crop_error(fx.with_formula(formula))
                if formula.isprintable():
                    self.assertEqual(message, self.CALL_MESSAGE % formula)
                else:                                          # tab, newline, U+FEFF, U+00A0 are shown as `?` or a space
                    self.assertTrue(message.startswith("formula of D4 (=") and message.endswith(
                        ") calls HYPERLINK: links are not copied into a dashboard that every internal user sees"), message)

    def test_the_hyperlink_message_comes_from_the_formula_check_not_from_the_link_check(self):
        message = crop_error(fx.with_formula("=HYPERLINK(A1)"))
        self.assertNotIn("holds a link", message)
        self.assertIn("calls HYPERLINK", message)

    def test_a_formula_that_only_names_hyperlink_in_text_or_in_another_name_is_not_refused(self):
        for formula in ('="HYPERLINK(A1)"', '="x"&"HYPERLINK("', "=MYHYPERLINK(A1)", "=a.HYPERLINK(A1)",
                        "=$HYPERLINK(A1)", "=X_HYPERLINK(A1)", "=HYPERLINKS(A1)", "=A1+HYPERLINK", "=HYPERLINK",
                        "=XHYPERLINK (A1)", "=1HYPERLINK(A1)", "=ÉHYPERLINK(A1)", '="a\\"HYPERLINK(A1)"',
                        '=IF(A1="HYPERLINK(",1,2)', "=A1.HYPERLINK(B1)", "=_HYPERLINK(A1)"):
            with self.subTest(formula=formula):
                base, _ = fx.crop_it(fx.with_formula(formula))
                self.assertEqual(base["sheets"][0]["cells"]["D4"]["content"], formula)

    def test_hyperlink_in_text_that_is_not_a_formula_is_only_text(self):
        for text in ("HYPERLINK(A1)", " =HYPERLINK(A1)", "use =HYPERLINK(x)", "'=HYPERLINK(A1)"):
            with self.subTest(text=text):
                base, _ = fx.crop_it(fx.with_cell("D4", {"content": text}))
                self.assertEqual(base["sheets"][0]["cells"]["D4"]["content"], text)

    def test_a_hyperlink_formula_is_refused_before_the_formula_is_checked_for_other_problems(self):
        # the HYPERLINK refusal does not depend on the formula being croppable otherwise
        self.assertIn("calls HYPERLINK", crop_error(fx.with_formula("=HYPERLINK(Z99)")))
        self.assertIn("calls HYPERLINK", crop_error(fx.with_formula("=HYPERLINK(A1:A)")))


class TableIdGuardTests(unittest.TestCase):
    def test_ids_present_in_the_tables_pass_as_int_or_text(self):
        for value in (1, "1"):
            base, _ = fx.crop_it(fx.with_cell("D4", {"style": value, "format": value, "border": value}))
            self.assertEqual(base["sheets"][0]["cells"]["D4"]["style"], value)

    def test_a_missing_id_fails_for_each_table(self):
        for key in ("style", "format", "border"):
            for value in (99, "99", 0):
                with self.subTest(key=key, value=value):
                    message = crop_error(fx.with_cell("D4", {key: value}))
                    self.assertIn("D4", message)
                    self.assertIn(key, message)

    def test_a_missing_table_fails_when_a_cell_points_to_it(self):
        for key, table in (("style", "styles"), ("format", "formats"), ("border", "borders")):
            data = fx.with_cell("D4", {key: 1})
            del data[table]
            with self.subTest(key=key):
                self.assertIn(key, crop_error(data))

    def test_a_dangling_id_outside_the_area_is_not_checked(self):
        base, _ = fx.crop_it(fx.with_cell("Z90", {"style": 99}))
        self.assertNotIn("Z90", base["sheets"][0]["cells"])


class AreaAndSheetGuardTests(unittest.TestCase):
    def test_area_bigger_than_the_sheet_fails(self):
        for area in ("A1:E1001", "A1:AE6", "A1:AE1001"):
            with self.subTest(area=area):
                self.assertIn("bigger than the sheet", crop_error(fx.make_snapshot(), area=area))

    def test_area_exactly_the_sheet_size_passes(self):
        sheet = fx.make_sheet(col_number=5, row_number=6, cells={"A1": {"content": "x"}}, merges=[])
        base, _ = fx.crop_it(fx.make_snapshot(sheets=[sheet]))
        self.assertEqual(base["sheets"][0]["rowNumber"], 6)

    def test_bad_area_text_fails_before_the_sheet_is_looked_up(self):
        self.assertIn("A1:Q31", crop_error(fx.make_snapshot(), sheet="No Such Sheet", area="B2:C3"))

    def test_sheet_that_does_not_exist_fails_and_lists_the_sheets(self):
        message = crop_error(fx.make_snapshot(), sheet="Nope")
        self.assertIn("found 0", message)
        self.assertIn(fx.SHEET, message)
        self.assertIn(fx.OTHER_SHEET, message)

    def test_sheet_names_are_matched_exactly(self):
        for name in ("sheet one", "Sheet One ", " Sheet One", "Sheet", ""):
            with self.subTest(name=name):
                self.assertIn("found 0", crop_error(fx.make_snapshot(), sheet=name))

    def test_two_sheets_with_the_same_name_fail(self):
        data = fx.make_snapshot(sheets=[fx.make_sheet(), fx.make_sheet()])
        self.assertIn("found 2", crop_error(data))

    def test_sheet_size_numbers_must_be_whole_non_negative_numbers(self):
        for key in ("rowNumber", "colNumber"):
            for value in (-1, "30", None, True, 1.5):
                with self.subTest(key=key, value=value):
                    sheet = fx.make_sheet()
                    sheet[key] = value
                    self.assertIn("bad", crop_error(fx.make_snapshot(sheets=[sheet])))
            sheet = fx.make_sheet()
            del sheet[key]
            with self.subTest(key=key, value="missing"):
                self.assertIn("bad", crop_error(fx.make_snapshot(sheets=[sheet])))


class RowColumnPropertyGuardTests(unittest.TestCase):
    def test_hidden_flag_is_allowed_and_kept(self):
        rows = {"0": {"size": 24, "isHidden": True}, "1": {"isHidden": True}, "2": {}}
        cols = {"0": {"isHidden": False, "size": 10}}
        base, _ = fx.crop_it(fx.make_snapshot(sheets=[fx.make_sheet(rows=rows, cols=cols)]))
        self.assertEqual(base["sheets"][0]["rows"], rows)
        self.assertEqual(base["sheets"][0]["cols"], cols)

    def test_any_other_property_fails(self):
        for kind in ("rows", "cols"):
            for size in ({"size": 24, "color": "red"}, {"height": 3}, {"isHidden": True, "x": 1}):
                with self.subTest(kind=kind, size=size):
                    message = crop_error(fx.make_snapshot(sheets=[fx.make_sheet(**{kind: {"0": size}})]))
                    self.assertIn(kind, message)

    def test_an_unknown_property_beyond_the_area_still_fails(self):
        self.assertIn("rows", crop_error(fx.make_snapshot(sheets=[fx.make_sheet(rows={"500": {"size": 1, "x": 1}})])))

    def test_malformed_size_tables_fail(self):
        for kind in ("rows", "cols"):
            for value in ({"0": 24}, {"x": {"size": 1}}, {"-1": {"size": 1}}, {"0": [1]}, [], "text"):
                with self.subTest(kind=kind, value=value):
                    self.assertIn(kind, crop_error(fx.make_snapshot(sheets=[fx.make_sheet(**{kind: value})])))


# ---------------------------------------------------------------- round 2: scanner, merges, digits, caps, constants

class FormulaScannerTests(unittest.TestCase):
    def test_these_formulas_pass_the_scanner(self):
        for formula in ("=A1:B2", "=a1:b2", "=$A$1:$B$2", "=LOG10(A1)", '=IF(A1="x!y",1,2)', "=ROW()", "=A1 : B2",
                        "=B2 :C3", "=SUM(A1:B2)+SUM(C1:D2)", '="a:b"', '=IF(A1="it\'s",1,2)', '=A1&""',
                        "=LOG10(A1:B2)", '="she said ""x:y!"""', "=SUM(A1:B2, C3)", "=SUM(\n A1:B2)"):
            with self.subTest(formula=formula):
                self.assertIsNone(intake_xlsx.formula_problem(formula))

    def test_any_quote_or_bang_outside_a_string_literal_is_another_sheet(self):
        for formula in ("='a\"b'!A1&\"c\"", "=Other!A1", "='x y'!A1", "=A1+'", "=A1!", '=A1&"x"&Other!B2', "=A1'",
                        "='A'", "=!A1", '="x"&\'s\'!A1'):
            with self.subTest(formula=formula):
                self.assertEqual(intake_xlsx.formula_problem(formula), "reads another sheet")

    def test_an_unterminated_string_literal_is_an_unbalanced_quote(self):
        for formula in ('="abc', '=A1&"', '="a""', '=IF(A1="x,1,2)', '="a"&"b', '="""'):
            with self.subTest(formula=formula):
                self.assertEqual(intake_xlsx.formula_problem(formula), "has an unbalanced quote")

    def test_a_colon_that_is_not_part_of_a_full_range_is_a_whole_row_or_column_range(self):
        for formula in ("=SUM(A:A)", "=SUM(A1:C)", "=SUM(A1:A)", "=SUM(A:C5)", "=SUM(A1:5)", "=SUM(5:C3)",
                        "=SUM(A : A)", "=SUM(1 : 3)", "=SUM(A1 :C)", "=SUM($A:$B)", "=SUM(1:1)", "=SUM($2:$4)",
                        "=A1:B2:C3", "=SUM(A1:B2):C", "=1:30", "=SUM(A1:)", "=SUM(:A1)"):
            with self.subTest(formula=formula):
                self.assertEqual(intake_xlsx.formula_problem(formula), "uses a whole row or column range")

    def test_another_sheet_is_reported_before_a_bad_colon(self):
        self.assertEqual(intake_xlsx.formula_problem("=SUM(Other!A:A)"), "reads another sheet")

    def test_a_colon_inside_a_literal_does_not_count(self):
        self.assertIsNone(intake_xlsx.formula_problem('=IF(A1="A:A","1:3",B2)'))

    def test_refs_are_returned_in_text_order_with_both_ends_of_a_range(self):
        self.assertEqual(intake_xlsx.formula_refs("=B2+A1"), [(2, 2), (1, 1)])
        self.assertEqual(intake_xlsx.formula_refs("=SUM(C3:A1)+B2"), [(3, 3), (1, 1), (2, 2)])
        self.assertEqual(intake_xlsx.formula_refs("=A1 : B2"), [(1, 1), (2, 2)])
        self.assertEqual(intake_xlsx.formula_refs("=$A$1:$B$2+C3"), [(1, 1), (2, 2), (3, 3)])
        self.assertEqual(intake_xlsx.formula_refs("=a1:b2"), [(1, 1), (2, 2)])
        self.assertEqual(intake_xlsx.formula_refs("=LOG10(A1:B2)"), [(1, 1), (2, 2)])
        self.assertEqual(intake_xlsx.formula_refs('=C3+SUM(A1:B2)&"D4:E5"+F6'), [(3, 3), (1, 1), (2, 2), (6, 6)])

    def test_non_ascii_digits_are_not_references(self):
        for formula in ("=A٣", "=A²", "=A１", "=SUM(A1:B٣)", "=SUM(A٣:B2)"):
            with self.subTest(formula=formula):
                self.assertNotIn((3, 1), intake_xlsx.formula_refs(formula))
        self.assertEqual(intake_xlsx.formula_refs("=A٣"), [])
        self.assertEqual(intake_xlsx.formula_refs("=A²"), [])
        self.assertEqual(intake_xlsx.formula_refs("=A１"), [])

    def test_overlong_digit_strings_are_references_far_outside_the_area(self):
        # `_row` maps more than 9 digits to 10**9 (no int() limit): a long number is read as "far outside"
        self.assertEqual(intake_xlsx.formula_refs("=A" + "9" * 5000), [(10 ** 9, 1)])
        self.assertEqual(intake_xlsx.formula_refs("=A1234567890"), [(10 ** 9, 1)])
        self.assertEqual(intake_xlsx.formula_refs("=A123456789"), [(123456789, 1)])      # 9 digits: the real number
        self.assertEqual(intake_xlsx.formula_refs("=SUM(A1:B" + "9" * 5000 + ")"), [(1, 1), (10 ** 9, 2)])
        self.assertEqual(intake_xlsx.formula_refs("=SUM(A" + "9" * 5000 + ":B2)"), [(10 ** 9, 1), (2, 2)])

    def test_the_crop_bounds_check_both_ends_of_a_spaced_range(self):
        for formula in ("=SUM(A1 : F6)", "=SUM(F6 : A1)", "=SUM(A1:E7)", "=SUM(A7 : B8)"):
            with self.subTest(formula=formula):
                self.assertIn("reads outside", crop_error(fx.with_formula(formula)))
        for formula in ("=SUM(A1 : E6)", "=SUM(E6 : A1)", "=SUM(A1 : A1)"):
            with self.subTest(formula=formula):
                fx.crop_it(fx.with_formula(formula))

    def test_a_reference_with_more_than_seven_digits_is_read_as_outside_the_area(self):
        for formula in ("=A12345678", "=A1234567890", "=A" + "9" * 5000, "=1+Z" + "1" * 5000):
            with self.subTest(formula=formula[:20]):
                self.assertIsNone(intake_xlsx.formula_problem(formula))     # a plain reference, not an odd form
                self.assertIn("reads outside", crop_error(fx.with_formula(formula)))

    def test_a_range_ending_in_more_than_seven_digits_is_read_as_outside_the_area(self):
        for formula in ("=SUM(A1:B12345678)", "=SUM(A1:B" + "9" * 5000 + ")", "=SUM(A12345678:B2)",
                        "=SUM(A1 : B" + "9" * 5000 + ")"):
            with self.subTest(formula=formula[:20]):
                self.assertIsNone(intake_xlsx.formula_problem(formula))
                self.assertIn("reads outside", crop_error(fx.with_formula(formula)))

    def test_an_overlong_reference_is_outside_even_the_tallest_area(self):
        # the largest area the intake accepts has 1,000,000 rows: "far outside" must stay above it
        sheet = fx.make_sheet(cells={"A1": {"content": "=A1234567890"}}, merges=[], row_number=1000000)
        self.assertIn("reads outside", crop_error(fx.make_snapshot(sheets=[sheet]), area="A1:A1000000"))
        for formula in ("=A1000000", "=SUM(A1:A1000000)"):                    # the last row itself is inside
            sheet = fx.make_sheet(cells={"A1": {"content": formula}}, merges=[], row_number=1000000)
            base, _ = fx.crop_it(fx.make_snapshot(sheets=[sheet]), area="A1:A1000000")
            self.assertEqual(base["sheets"][0]["cells"]["A1"]["content"], formula)
        self.assertGreater(10 ** 9, intake_xlsx.MAX_AREA_CELLS)

    def test_a_long_number_that_is_not_a_reference_is_still_not_one(self):
        for formula in ("=ABCD" + "9" * 20, "=1" + "9" * 5000, "=LOG10(" + "9" * 5000 + ")", "=Name_A" + "9" * 20):
            with self.subTest(formula=formula[:20]):
                self.assertEqual(intake_xlsx.formula_refs(formula), [])
                fx.crop_it(fx.with_formula(formula))

    def test_the_longest_digit_string_that_is_a_number_and_the_shortest_that_is_not(self):
        # 9 digits: int(); 10 digits: 10**9. Both lie outside every area (at most 1,000,000 cells), both are refused
        self.assertEqual(intake_xlsx.formula_refs("=A999999999"), [(999999999, 1)])
        self.assertEqual(intake_xlsx.formula_refs("=A1000000000"), [(10 ** 9, 1)])
        for formula in ("=A999999999", "=A1000000000"):
            with self.subTest(formula=formula):
                self.assertIn("reads outside", crop_error(fx.with_formula(formula)))

    def test_a_formula_with_a_quote_hidden_foreign_sheet_fails_through_crop(self):
        self.assertIn("another sheet", crop_error(fx.with_formula("='a\"b'!A1&\"c\"")))
        self.assertIn("unbalanced quote", crop_error(fx.with_formula('="abc')))


class BackslashQuoteTests(unittest.TestCase):
    """The string rule of the guard copies the parser's (o_spreadsheet.js tokenizeString): a quote closes the string
    unless a backslash precedes it, `""` is NOT an escape. The security review found `="\\""&Z99&"` slipping through
    when the guard read `""` as an escape (the parser read `Z99` as a reference)."""

    HIDDEN_READ = '="\\""&Z99&"'                  # the characters: = " \ " " & Z 9 9 & "
    HIDDEN_RANGE = '="\\""&A1:C&"'
    HIDDEN_SHEET = '="\\""&\'S\'!A1&"'

    def test_the_hidden_reference_is_refused_by_formula_problem_and_crop(self):
        self.assertEqual(self.HIDDEN_READ, '="' + "\\" + '""&Z99&"')
        for formula, problem in ((self.HIDDEN_READ, "has an unbalanced quote"),
                                 (self.HIDDEN_RANGE, "has an unbalanced quote"),
                                 (self.HIDDEN_SHEET, "reads another sheet")):
            with self.subTest(formula=formula):
                self.assertEqual(intake_xlsx.formula_problem(formula), problem)
                self.assertIn(problem, crop_error(fx.with_formula(formula)))

    def test_a_backslash_quote_inside_a_literal_does_not_close_it(self):
        for formula in ('="\\"A"', '="a\\"b"', '=A1&"\\"x"', '="\\"Z99"'):
            with self.subTest(formula=formula):
                self.assertIsNone(intake_xlsx.formula_problem(formula))
                self.assertEqual(intake_xlsx.formula_refs(formula), [(1, 1)] if formula.startswith("=A1") else [])

    def test_a_closing_quote_after_a_backslash_leaves_the_literal_open(self):
        # valid Excel (backslash is no escape there), but the parser reads it as an unterminated string: refused loudly
        for formula in ('=A1&"\\"', '="a\\\\"', '="\\"', '="x"&"\\"'):
            with self.subTest(formula=formula):
                self.assertEqual(intake_xlsx.formula_problem(formula), "has an unbalanced quote")

    def test_a_doubled_quote_is_two_literals_not_an_escape(self):
        for formula in ('="a""b"', '="she said ""hi"""', '=A1&""""', '=""&""'):
            with self.subTest(formula=formula):
                self.assertIsNone(intake_xlsx.formula_problem(formula))
        self.assertEqual(intake_xlsx.formula_problem('="a"""'), None)
        self.assertEqual(intake_xlsx.formula_problem('="a""'), "has an unbalanced quote")
        self.assertEqual(intake_xlsx.formula_problem('="""'), "has an unbalanced quote")

    def test_an_unterminated_literal_full_of_escaped_quotes_is_scanned_in_linear_time(self):
        # an unterminated literal runs to the end of the cell in ONE pass; when the pattern restarted at every quote
        # of the literal and read to the end each time, 24 KB took about 3 s and 36 KB about 7 s (quadratic)
        formula = '="' + 'a\\"' * 12000                      # 36 KB, never closed
        start = time.perf_counter()
        problem = intake_xlsx.formula_problem(formula)
        refs = intake_xlsx.formula_refs(formula)
        elapsed = time.perf_counter() - start
        self.assertEqual((problem, refs), ("has an unbalanced quote", []))
        self.assertLess(elapsed, 1.0, "the scan took %.2f s" % elapsed)
        start = time.perf_counter()
        self.assertIn("unbalanced quote", crop_error(fx.with_formula('="' + 'a\\"' * 6000)))
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_a_reference_between_two_literals_is_checked(self):
        # `"a"` + Z99 + `"b"`: with "" read as an escape the guard would have hidden Z99 inside one literal
        formula = '="a"Z99"b"'
        self.assertEqual(intake_xlsx.formula_refs(formula), [(99, 26)])
        self.assertIn("reads outside", crop_error(fx.with_formula(formula)))

    def test_the_cases_that_must_keep_passing_inside_the_area(self):
        for formula in ('="it\'s"', '="a b"', '="x:y"', '=A1&"!"', '=IF(A1="x!y",1,2)', '="a""b"'):
            with self.subTest(formula=formula):
                self.assertIsNone(intake_xlsx.formula_problem(formula))
                base, _ = fx.crop_it(fx.with_formula(formula))
                self.assertEqual(base["sheets"][0]["cells"]["D4"]["content"], formula)


# one row per formula: (formula, verdict) with the verdict one of
#   ok       cropped as it is            sheet    "reads another sheet"        quote    "has an unbalanced quote"
#   range    "uses a whole row or column range"                                outside  "reads outside the area"
# the area is A1:E6 (fx.AREA); the formula sits in D4
CORPUS = [
    # --- plain and quoted text that must pass
    ("=A1+B2", "ok"), ("=SUM(A1:B2, C3)", "ok"), ('="it\'s"', "ok"), ('="a b"', "ok"), ('="x:y"', "ok"),
    ('=A1&"!"', "ok"), ('=IF(A1="x!y",1,2)', "ok"), ('=IF(A1="A:A","1:3",B2)', "ok"), ('="a""b"', "ok"),
    ('="she said ""hi"""', "ok"), ('=A1&""', "ok"), ('="\\"A"', "ok"), ('="Z99"', "ok"),
    # --- case, `$`, function names with digits, spaced full ranges (both ends are checked)
    ("=sum(a1:b2)", "ok"), ("=$A$1:$B$2", "ok"), ("=SUM($A$1:B$2)", "ok"), ("=LOG10(A1)", "ok"),
    ("=ATAN2(A1,B1)", "ok"), ("=ABCD5", "ok"), ("=1E5+A1", "ok"), ("=A1 : B2", "ok"), ("=SUM(A1\t:\tB2)", "ok"),
    ("=SUM(A1\n:\nB2)", "ok"),
    # --- reading outside the area, also through long digit strings and spaced ranges
    ("=Z99", "outside"), ("=LOG10(Z99)", "outside"), ("=$F$1", "outside"), ('="x"&F1', "outside"),
    ("=SUM(A1 : F6)", "outside"), ("=SUM(A1:b7)", "outside"), ("=SUM(A1\t:\tF6)", "outside"),
    ("=SUM(A1:B12345678)", "outside"), ("=A12345678", "outside"), ("=A" + "9" * 5000, "outside"),
    # --- another sheet: any apostrophe or bang outside a string literal
    ("=Other!A1", "sheet"), ("='Sheet Two'!A1", "sheet"), ("='a\"b'!A1&\"c\"", "sheet"), ("=A1+'", "sheet"),
    ('="x"&\'s\'!A1', "sheet"), ('="\\""&\'S\'!A1&"', "sheet"), ("=A1!", "sheet"), ("=SUM(Other!A:A)", "sheet"),
    # --- unterminated string literals, including the backslash-quote family
    ('="abc', "quote"), ('=A1&"', "quote"), ('=IF(A1="x,1,2)', "quote"), ('="""', "quote"), ('="a""', "quote"),
    ('="\\""&Z99&"', "quote"), ('="\\""&A1:C&"', "quote"), ('=A1&"\\"', "quote"), ('="a\\\\"', "quote"),
    # --- a colon that is not part of a full A1:B2 range
    ("=SUM(A:A)", "range"), ("=SUM(a:a)", "range"), ("=SUM($A:$B)", "range"), ("=SUM(A1:C)", "range"),
    ("=SUM(A1:A)", "range"), ("=SUM(A:C5)", "range"), ("=SUM(A1:5)", "range"), ("=SUM(5:C3)", "range"),
    ("=SUM(A : A)", "range"), ("=SUM(1 : 3)", "range"), ("=SUM(A1 :C)", "range"), ("=1:30", "range"),
    ("=A1:B2:C3", "range"), ("=SUM(A1:B2):C", "range"),
    ("=SUM(A1\u00a0:\u00a0B2)", "range"), ("=SUM(A1\u2003:B2)", "range"), ("=SUM(A1\u2028:B2)", "range"),
    ("=SUM(A1:\u00a0B2)", "range"),
]
VERDICT_TEXT = {"sheet": "reads another sheet", "quote": "has an unbalanced quote",
                "range": "uses a whole row or column range", "outside": "reads outside the area"}


class FormulaCorpusTests(unittest.TestCase):
    def test_every_formula_of_the_corpus_has_its_verdict_in_the_pure_functions(self):
        last_row, last_col = intake_xlsx.parse_area(fx.AREA)
        for formula, expected in CORPUS:
            with self.subTest(formula=formula[:40]):
                problem = intake_xlsx.formula_problem(formula)
                if expected in ("sheet", "quote", "range"):
                    self.assertEqual(problem, VERDICT_TEXT[expected])
                    continue
                self.assertIsNone(problem)
                outside = any(row > last_row or col > last_col for row, col in intake_xlsx.formula_refs(formula))
                self.assertEqual("outside" if outside else "ok", expected)

    def test_every_formula_of_the_corpus_has_its_verdict_through_crop(self):
        for formula, expected in CORPUS:
            with self.subTest(formula=formula[:40]):
                data = fx.with_formula(formula)
                if expected == "ok":
                    base, _ = fx.crop_it(data)
                    self.assertEqual(base["sheets"][0]["cells"]["D4"]["content"], formula)
                else:
                    message = crop_error(data)
                    self.assertIn(VERDICT_TEXT[expected], message)
                    self.assertIn("D4", message)

    def test_the_corpus_is_big_enough_and_has_every_verdict(self):
        self.assertGreaterEqual(len(CORPUS), 40)
        self.assertEqual({verdict for _, verdict in CORPUS}, {"ok", "outside", "sheet", "quote", "range"})
        self.assertEqual(len({formula for formula, _ in CORPUS}), len(CORPUS), "a formula is listed twice")


class MergeRectangleTests(unittest.TestCase):
    def merged(self, merges, area):
        base, report = fx.crop_it(fx.make_snapshot(sheets=[fx.make_sheet(merges=merges)]), area=area)
        return base["sheets"][0]["merges"], report["merges"]

    def test_a_merge_entirely_outside_is_dropped_whatever_its_direction(self):
        self.assertEqual(self.merged(["D1:E2", "A11:C12", "F7:G8", "H9:F7", "E12:D11", "D1:D11"], "A1:C10"), ([], 0))

    def test_a_merge_fully_inside_is_kept_in_either_direction(self):
        self.assertEqual(self.merged(["A1:C10", "C10:A1", "B2:B2", "C3:A1"], "A1:C10")[0],
                         ["A1:C10", "C10:A1", "B2:B2", "C3:A1"])

    def test_a_reversed_merge_that_leaves_the_area_fails(self):
        self.assertIn("straddles", crop_error(fx.make_snapshot(sheets=[fx.make_sheet(merges=["D1:A11"])]),
                                              area="A1:C10"))

    def test_the_same_reversed_merge_is_kept_when_the_area_covers_it(self):
        self.assertEqual(self.merged(["D1:A11"], "A1:D11")[0], ["D1:A11"])

    def test_a_merge_that_intersects_without_being_inside_fails(self):
        for merge in ("A1:D1", "C10:C11", "B9:D12", "A11:A1", "C1:D2"):
            with self.subTest(merge=merge):
                self.assertIn("straddles", crop_error(fx.make_snapshot(sheets=[fx.make_sheet(merges=[merge])]),
                                                      area="A1:C10"))


class DigitAndIndexTests(unittest.TestCase):
    def test_split_ref_takes_up_to_seven_ascii_digits_only(self):
        self.assertEqual(intake_xlsx.split_ref("A9999999"), (9999999, 1))
        for bad in ("A12345678", "A٣", "A²", "A" + "9" * 5000, "Ａ" "1", "A１"):
            with self.subTest(bad=bad[:12]), self.assertRaises(IntakeError):
                intake_xlsx.split_ref(bad)

    def test_a_cell_address_that_is_not_ascii_or_is_huge_is_a_clean_crop_error(self):
        for ref in ("A٣", "A" + "1" * 5000, "A12345678"):
            with self.subTest(ref=ref[:12]):
                self.assertIn("not a cell reference", crop_error(fx.with_cell(ref, {"content": "x"})))

    def test_a_size_index_is_seven_ascii_digits_at_most(self):
        sheet = fx.make_sheet(rows={"0000001": {"size": 1}})
        base, _ = fx.crop_it(fx.make_snapshot(sheets=[sheet]))
        self.assertEqual(base["sheets"][0]["rows"], {"0000001": {"size": 1}})
        for index in ("²", "٣", "00000001", "1" * 5000, "-1", "1.5", " 1"):
            for kind in ("rows", "cols"):
                with self.subTest(kind=kind, index=index[:12]):
                    message = crop_error(fx.make_snapshot(sheets=[fx.make_sheet(**{kind: {index: {"size": 1}}})]))
                    self.assertIn(kind, message)


class UnknownKeyMessageTests(unittest.TestCase):
    def test_at_most_ten_names_are_listed_then_an_ellipsis(self):
        keys = {"k%02d" % n: 1 for n in range(50)}
        for message in (crop_error(fx.make_snapshot(**keys)), crop_error(fx.with_sheet_extra(**keys))):
            self.assertIn("k00", message)
            self.assertIn("k09", message)
            self.assertNotIn("k10", message)
            self.assertIn("...", message)
        message = crop_error(fx.with_cell("D4", {"content": "x", **keys}))
        self.assertIn("k09", message)
        self.assertNotIn("k10", message)
        self.assertIn("...", message)

    def test_exactly_ten_unknown_keys_have_no_ellipsis_and_one_is_listed_whole(self):
        ten = {"k%02d" % n: 1 for n in range(10)}
        message = crop_error(fx.make_snapshot(**ten))
        self.assertIn("k09", message)
        self.assertNotIn("...", message)
        self.assertIn("mystery", crop_error(fx.make_snapshot(mystery=1)))

    def test_a_huge_key_name_is_cut_in_every_message(self):
        big = "x" * 3000000
        for data in (fx.make_snapshot(**{big: 1}), fx.with_sheet_extra(**{big: 1}),
                     fx.with_cell("D4", {"content": "x", big: 1})):
            message = crop_error(data)
            self.assertLess(len(message), 400)
            self.assertIn("...", message)

    def test_control_characters_in_a_key_name_are_replaced(self):
        message = crop_error(fx.make_snapshot(**{"bad\nname\x1b[31m": 1}))
        self.assertNotIn("\n", message)
        self.assertNotIn("\x1b", message)

    def test_the_names_are_listed_in_sorted_order(self):
        message = crop_error(fx.make_snapshot(zeta=1, alpha=1, mid=1))
        self.assertLess(message.index("alpha"), message.index("mid"))
        self.assertLess(message.index("mid"), message.index("zeta"))


class NonFiniteNumberTests(unittest.TestCase):
    def test_nan_and_infinity_constants_are_refused_by_parse_snapshot(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            for place in ('"x": %s', '"cells": {"A1": {"content": %s}}'):
                body = place % constant
                raw = ('{"sheets": [{"name": "A", "cells": {}}], %s}' % body if place.startswith('"x"')
                       else '{"sheets": [{"name": "A", %s}]}' % body).encode()
                with self.subTest(constant=constant, place=place), self.assertRaises(IntakeError):
                    intake_xlsx.parse_snapshot(raw)

    def test_a_number_that_overflows_to_infinity_is_parsed_but_cannot_be_dumped(self):
        raw = b'{"sheets": [{"name": "A", "cells": {"A1": {"content": 1e999}}}]}'
        data = intake_xlsx.parse_snapshot(raw)
        self.assertEqual(data["sheets"][0]["cells"]["A1"]["content"], float("inf"))
        with self.assertRaises(IntakeError):
            intake_xlsx.dump_bytes(data)

    def test_dump_bytes_refuses_nan_and_infinity_with_a_clean_message(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(IntakeError) as caught:
                intake_xlsx.dump_bytes({"a": [value]})
            self.assertIn("cannot be written as JSON", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_dump_bytes_refuses_nesting_it_cannot_serialise(self):
        deep = []
        for _ in range(5000):
            deep = [deep]
        with self.assertRaises(IntakeError):
            intake_xlsx.dump_bytes({"a": deep})

    def test_ordinary_floats_are_still_dumped(self):
        self.assertEqual(intake_xlsx.dump_bytes({"a": 12.5, "b": 1e300}), b'{\n "a": 12.5,\n "b": 1e+300\n}\n')


class CropSharingTests(unittest.TestCase):
    def test_nothing_mutable_of_the_output_is_shared_with_the_input(self):
        raw = fx.make_snapshot(sheets=[fx.make_sheet(headerGroups={"ROW": [], "COL": []})])
        frozen = copy.deepcopy(raw)
        baseline, _ = intake_xlsx.crop(raw, fx.SHEET, fx.AREA)
        sheet = baseline["sheets"][0]
        self.assertIsNot(baseline["sheets"], raw["sheets"])
        self.assertIsNot(sheet, raw["sheets"][0])
        for key in ("cells", "merges", "rows", "cols", "headerGroups"):
            self.assertIsNot(sheet[key], raw["sheets"][0][key], key)
        sheet["headerGroups"]["ROW"].append(1)
        sheet["merges"].append("X1:Y2")
        sheet["cells"]["NEW1"] = {}
        sheet["cells"]["B2"]["border"] = 9
        sheet["rows"]["0"]["size"] = 1
        sheet["cols"]["4"]["size"] = 1
        sheet["zoom"] = 5
        baseline["globalFilters"].append(1)
        baseline["formats"]["1"] = "x"
        baseline["borders"]["1"]["top"]["style"] = "thin"
        self.assertEqual(raw, frozen)

    def test_values_not_kept_by_the_crop_are_not_touched_either(self):
        raw = fx.make_snapshot()
        frozen = copy.deepcopy(raw)
        baseline, _ = intake_xlsx.crop(raw, fx.SHEET, fx.AREA)
        del baseline["sheets"][0]["cells"]["A1"]
        baseline["sheets"][0]["rows"].clear()
        self.assertEqual(raw, frozen)


if __name__ == "__main__":
    unittest.main()
