"""Tests of `scripts/validate_formulas.py`: the function-name guard, the derivation of the frozen list and the sheet
evaluator. Synthetic inputs only (see `formula_fixtures.py`); the command line is in `test_validate_cli.py`.

Resource rules: no test builds a big structure on purpose. Range tests either refuse before any cell is built (the
cell-name helper is replaced by a stand-in that fails on the first call) or stay under the cap; the file-size tests use
a stand-in file that serves a bounded number of bytes and fails on an unbounded read.
"""
import hashlib
import json
import os
import re
import time
import unittest
from unittest import mock

import formula_fixtures as fx
from formula_fixtures import dashboard, make_tree, registry_js, write, write_frozen
from helpers import IsolatedTestCase

import validate_formulas as vf  # noqa: E402

REAL_TREE = os.environ.get("KPI_ODOO_ADDONS_PATH", "")     # read before IsolatedTestCase clears the environment


def calc(formula, cells=None, lists=None, translations=None, ref="Z1"):
    """The value of one formula placed in `ref`, next to `cells` {ref: content}."""
    all_cells = {ref: formula}
    all_cells.update(cells or {})
    return vf.SheetEvaluator(dashboard(all_cells), lists, translations).value(ref)


def evaluator(cells, lists=None, translations=None):
    return vf.SheetEvaluator(dashboard(cells), lists, translations)


class BoundedFile:
    """A stand-in for a file object: serves at most `size` bytes, only through a bounded `read(n)`, and fails on any
    read without a size or with a size above `limit` (an unbounded read must never reach the real disk or memory)."""

    def __init__(self, size, limit):
        self.remaining, self.limit, self.asked = size, limit, []

    def read(self, size=-1):
        self.asked.append(size)
        if size is None or size < 0 or size > self.limit:
            raise AssertionError("unbounded read: %r" % (size,))
        count = min(size, self.remaining)
        self.remaining -= count
        return b"x" * count

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


# ---------------------------------------------------------------- 1. function names of the formulas

class FormulaFunctionNamesTests(unittest.TestCase):
    def test_names_are_upper_cased_and_collect_the_cells_that_call_them(self):
        data = dashboard({"A1": "=sum(B1:B2)+Sum(1)", "A2": "=IF(1,2,3)", "A3": '=odoo.list(1,1,"f")',
                          "A4": "=_t(\"x\")", "A5": "=LOG10(5)+ODOO.LIST.HEADER(1,\"f\")"})
        self.assertEqual(vf.formula_function_names(data),
                         {"SUM": ["A1", "A1"], "IF": ["A2"], "ODOO.LIST": ["A3"], "_T": ["A4"], "LOG10": ["A5"],
                          "ODOO.LIST.HEADER": ["A5"]})

    def test_a_reference_or_a_parenthesis_is_not_a_function_call(self):
        self.assertEqual(vf.formula_function_names(dashboard({"A1": "=A1+B2", "A2": "=(1+2)*3", "A3": "=1+2"})), {})
        self.assertEqual(sorted(vf.formula_function_names(dashboard({"A1": "=IF(A1,B2,3)"}))), ["IF"])

    def test_a_space_before_the_parenthesis_is_still_a_call(self):
        self.assertEqual(vf.formula_function_names(dashboard({"A1": "=SUM (B1)"})), {"SUM": ["A1"]})

    def test_names_inside_string_literals_are_ignored(self):
        data = dashboard({"A1": '=_t("N(x) and FOO(y)")', "A2": '="x(" & SUM(1) & ")("'})
        self.assertEqual(vf.formula_function_names(data), {"_T": ["A1"], "SUM": ["A2"]})

    def test_an_escaped_quote_does_not_end_a_string_literal(self):
        # the literal is  say \"N(1)\" now  : N( sits INSIDE it
        data = dashboard({"A1": '=IF(B1="say \\"N(1)\\" now",1,2)'})
        self.assertEqual(vf.formula_function_names(data), {"IF": ["A1"]})

    def test_a_call_after_a_literal_is_found(self):
        self.assertEqual(vf.formula_function_names(dashboard({"A1": '="a"&N(1)'})), {"N": ["A1"]})
        self.assertEqual(vf.formula_function_names(dashboard({"A1": '="a\\"b"&N(1)'})), {"N": ["A1"]})

    def test_every_sheet_is_scanned_and_refs_are_not_sheet_qualified(self):
        second = {"cells": {"A1": {"content": "=FOO(1)"}, "C3": {"content": "=SUM(1)"}}}
        data = dashboard({"A1": "=SUM(1)"}, extra_sheets=[second])
        self.assertEqual(vf.formula_function_names(data), {"SUM": ["A1", "C3"], "FOO": ["A1"]})

    def test_odd_cells_are_skipped_without_an_error(self):
        sheet = {"cells": {"A1": {"content": 5}, "A2": {"content": None}, "A3": {"content": True},
                           "A4": {"content": ["=SUM(1)"]}, "A5": {"content": {"a": 1}}, "A6": "=SUM(1)",
                           "A7": None, "A8": 5, "A9": ["=SUM(1)"], "B1": {"content": "SUM(1)"},
                           "B2": {"content": " =SUM(1)"}, "B3": {"style": 1}, "B4": {}, "B5": {"content": "=IF(1,2,3)"}}}
        self.assertEqual(vf.formula_function_names({"sheets": [sheet]}), {"IF": ["B5"]})

    def test_a_sheet_without_cells_or_a_dashboard_without_sheets_has_no_names(self):
        self.assertEqual(vf.formula_function_names({"sheets": [{}, {"cells": {}}]}), {})
        self.assertEqual(vf.formula_function_names({"sheets": []}), {})
        self.assertEqual(vf.formula_function_names({}), {})


class UnknownFunctionProblemsTests(unittest.TestCase):
    KNOWN = {"SUM", "IF", "IFERROR", "ODOO.LIST", "_T"}

    def test_a_sheet_of_known_functions_gives_no_problem(self):
        data = dashboard({"A1": '=IFERROR(ODOO.LIST(1,1,"f")+0,0)', "A2": "=SUM(A1:A3)", "A3": '=_t("x")'})
        self.assertEqual(vf.unknown_function_problems(data, self.KNOWN), [])

    def test_n_is_rejected_with_its_exact_message(self):
        data = dashboard({"G22": '=IFERROR(N(ODOO.LIST(1,1,"month_3")),0)'})
        self.assertEqual(vf.unknown_function_problems(data, self.KNOWN),
                         ["function N() is not defined by the vendored client (cells: G22)"])

    def test_an_invented_function_is_rejected_even_nested_or_in_lower_case(self):
        for formula in ("=SUM(C20:C25)+FOO(1)", "=iferror(foo(C20),0)", "=IF(1,2,FOO (3))"):
            with self.subTest(formula=formula):
                problems = vf.unknown_function_problems(dashboard({"C26": formula}), self.KNOWN)
                self.assertEqual(problems, ["function FOO() is not defined by the vendored client (cells: C26)"])

    def test_matching_is_case_insensitive_on_both_sides(self):
        data = dashboard({"A1": "=sum(1)+Sum(2)+IFERROR(1,2)"})
        self.assertEqual(vf.unknown_function_problems(data, ["sum", "iferror"]), [])
        self.assertEqual(vf.unknown_function_problems(data, ["SUM"]),
                         ["function IFERROR() is not defined by the vendored client (cells: A1)"])

    def test_one_line_per_function_name_not_per_cell(self):
        data = dashboard({"A1": "=N(1)", "A2": "=N(2)+N(3)", "A3": "=FOO(1)"})
        problems = vf.unknown_function_problems(data, self.KNOWN)
        self.assertEqual(len(problems), 2)

    def test_problems_are_sorted_by_function_name(self):
        data = dashboard({"A1": "=ZED(1)+FOO(2)+N(3)"})
        self.assertEqual([p.split("(")[0] for p in vf.unknown_function_problems(data, self.KNOWN)],
                         ["function FOO", "function N", "function ZED"])

    def test_at_most_three_cells_are_listed_sorted(self):
        cells = {ref: "=FOO(1)" for ref in ("D1", "B1", "A10", "A2", "C1")}
        problems = vf.unknown_function_problems(dashboard(cells), self.KNOWN)
        self.assertEqual(problems, ["function FOO() is not defined by the vendored client (cells: A10, A2, B1)"])

    def test_exactly_three_cells_are_all_listed(self):
        cells = {ref: "=FOO(1)" for ref in ("C1", "A1", "B1")}
        self.assertEqual(vf.unknown_function_problems(dashboard(cells), self.KNOWN),
                         ["function FOO() is not defined by the vendored client (cells: A1, B1, C1)"])

    def test_two_cells_list_two(self):
        cells = {ref: "=FOO(1)" for ref in ("B1", "A1")}
        self.assertIn("(cells: A1, B1)", vf.unknown_function_problems(dashboard(cells), self.KNOWN)[0])

    def test_an_empty_known_set_reports_every_function(self):
        data = dashboard({"A1": "=SUM(1)+IF(1,2,3)"})
        self.assertEqual(len(vf.unknown_function_problems(data, set())), 2)

    def test_a_name_that_only_appears_in_a_literal_is_not_a_problem(self):
        self.assertEqual(vf.unknown_function_problems(dashboard({"B5": '=_t("N(x) and FOO(y)")'}), self.KNOWN), [])

    def test_the_guard_is_not_vacuous_on_the_real_frozen_list(self):
        known = vf.load_frozen()
        self.assertEqual(vf.unknown_function_problems(dashboard({"A1": "=SUM(A2:A3)"}), known), [])
        for formula, name in (("=N(A1)", "N"), ("=FOO(1)", "FOO"), ("=IFERROR(N(ODOO.LIST(1,1,\"x\")),0)", "N")):
            with self.subTest(formula=formula):
                problems = vf.unknown_function_problems(dashboard({"A1": formula}), known)
                self.assertEqual(len(problems), 1)
                self.assertIn("function %s()" % name, problems[0])

    def test_the_old_formula_shape_with_n_is_reported_once_for_all_its_cells(self):
        cells = {"A%d" % i: '=IFERROR(N(ODOO.LIST(1,%d,"month_%d")),0)' % (i, i) for i in range(1, 73)}
        data = dashboard(cells)
        problems = vf.unknown_function_problems(data, vf.load_frozen())
        self.assertEqual(len(problems), 1)
        self.assertEqual(len(vf.formula_function_names(data)["N"]), 72)
        self.assertIn("(cells: A1, A10, A11)", problems[0])


class RegexTimeTests(unittest.TestCase):
    """The guard runs on whatever file `check` is given: one hostile cell must not make it take minutes. These two
    are RED on purpose (quadratic regexes, see the report); they use a payload of a few KB and a generous bound."""

    def _seconds(self, content):
        start = time.perf_counter()
        vf.formula_function_names(dashboard({"A1": content}))
        return time.perf_counter() - start

    def test_an_unterminated_literal_full_of_escaped_quotes_is_scanned_in_linear_time(self):
        # 12,000 repetitions = 36 KB; the string-literal regex restarts at every quote and reads to the end
        elapsed = self._seconds('="' + 'a\\"' * 12000)
        self.assertLess(elapsed, 1.5, "36 KB of escaped quotes took %.1f s (quadratic)" % elapsed)

    def test_one_long_identifier_without_a_parenthesis_is_scanned_in_linear_time(self):
        # 20,000 letters; the function-call regex restarts at every letter and reads to the end
        elapsed = self._seconds("=" + "A" * 20000 + " ")
        self.assertLess(elapsed, 1.0, "a 20 KB identifier took %.1f s (quadratic)" % elapsed)


FEFF = "\N{ZERO WIDTH NO-BREAK SPACE}"          # a space for the client tokenizer, not for Python's `\s`
NBSP, EM_SPACE = "\N{NO-BREAK SPACE}", "\N{EM SPACE}"
CANNOT_BE_CHECKED = ("formula(s) that cannot be checked (a quote or `!` outside a string, or an unterminated "
                     "string) in: %s")


def cf_sheet(*values_lists, key="conditionalFormats", path=("rule", "values")):
    """A sheet with no cells and one conditional format / data validation per list of `values`."""
    items = []
    for values in values_lists:
        node = values
        for step in reversed(path):
            node = {step: node}
        items.append(dict(node, id=str(len(items)), ranges=["A1:A3"]))
    return {"sheets": [{"cells": {}, key: items}]}


class GuardReadsLikeTheClientTests(unittest.TestCase):
    """Combined security review M2: the guard reads a formula the way the client tokenizer does (the node comparison with
    the real client is in test_validate_client_parity.py; these run everywhere)."""
    KNOWN = {"SUM", "IF", "IFERROR", "ODOO.LIST", "_T"}
    EVIL = "function EVIL() is not defined by the vendored client (cells: A1)"

    def problems(self, formula, known=None):
        return vf.unknown_function_problems(dashboard({"A1": formula}), self.KNOWN if known is None else known)

    # ---- the five bypasses of the review
    def test_two_backslashes_then_a_quote_do_not_hide_a_call(self):
        # the client string `"A\\"B"` runs on past `\\"` (a quote after a backslash does not close it)
        self.assertEqual(self.problems('="A\\\\"B"&EVIL(1)&"C"'), [self.EVIL])

    def test_u_feff_before_the_parenthesis_is_a_space_for_the_client(self):
        self.assertEqual(self.problems("=EVIL" + FEFF + "(1)"), [self.EVIL])

    def test_a_double_quote_in_a_sheet_name_cannot_hide_a_call(self):
        # refused as unreadable (a quote outside a literal), never read as `"b'!A1+EVIL(1)+"` being a string
        self.assertEqual(self.problems("='a\"b'!A1+EVIL(1)+\"c\""), [CANNOT_BE_CHECKED % "A1"])

    def test_a_dollar_or_an_accented_letter_before_a_name_makes_another_name(self):
        self.assertEqual(self.problems("=$SUM(1)"),
                         ["function $SUM() is not defined by the vendored client (cells: A1)"])
        self.assertEqual(self.problems("=ÉSUM(1)"),
                         ["function ÉSUM() is not defined by the vendored client (cells: A1)"])

    def test_the_review_bypasses_are_reported_against_the_real_frozen_list_too(self):
        known = vf.load_frozen()
        for formula in ('="A\\\\"B"&EVIL(1)&"C"', "=EVIL" + FEFF + "(1)", "='a\"b'!A1+EVIL(1)+\"c\"", "=$SUM(1)",
                        "=ÉSUM(1)"):
            with self.subTest(formula=formula):
                self.assertTrue(self.problems(formula, known))

    def test_the_shapes_that_must_stay_clean_stay_clean(self):
        known = vf.load_frozen()
        for formula in ('="EVIL(1)"', "=SUM(1,2)", '=IF(A1>0,ODOO.LIST(1,1,"x")+0,0)', '="x\\"&FOO(1)"',
                        '="it\'s"', '="a!b"', '=""', '="a""b"', '="a\\"b"&SUM(1)', '=_t("x")&SUM(1)'):
            with self.subTest(formula=formula):
                self.assertEqual(self.problems(formula, known), [])

    # ---- what a name is
    def test_a_name_is_the_whole_symbol_run_before_the_parenthesis(self):
        for formula, names in (("=$SUM(1)", ["$SUM"]), ("=A$SUM(1)", ["A$SUM"]), ("=SUM$(1)", ["SUM$"]),
                               ("=X.SUM(1)", ["X.SUM"]), ("=A.B.C(1)", ["A.B.C"]), ("=_SUM(1)", ["_SUM"]),
                               ("=É(1)", ["É"]), ("=ésum(1)", ["ÉSUM"]),
                               ("=SUM(1)+ÉSUM(2)+SUM(3)", ["SUM", "ÉSUM"]), ("=$A(1)", ["$A"]),
                               ("=äöü(1)", ["ÄÖÜ"])):
            with self.subTest(formula=formula):
                self.assertEqual(sorted(vf.formula_function_names(dashboard({"A1": formula}))), sorted(names))

    def test_a_name_inside_a_longer_symbol_is_not_found_by_its_tail(self):
        # `SUM(` after `$` or after a letter of another alphabet is not a call of SUM
        for formula in ("=$SUM(1)", "=ÉSUM(1)", "=X$SUM(1)", "=é.SUM(1)", "=A.SUM(1)"):
            with self.subTest(formula=formula):
                self.assertNotIn("SUM", vf.formula_function_names(dashboard({"A1": formula})))

    def test_u_feff_counts_as_a_space_before_the_parenthesis_like_any_other_white_space(self):
        for gap in (FEFF, FEFF + FEFF, " " + FEFF, FEFF + "\t", NBSP, "\n", EM_SPACE):
            with self.subTest(gap=gap):
                self.assertEqual(vf.formula_function_names(dashboard({"A1": "=SUM%s(1)" % gap})), {"SUM": ["A1"]})

    # ---- readable or not
    def test_a_quote_or_a_bang_outside_a_literal_makes_the_formula_unreadable(self):
        for formula in ("=A1+'x'!B2", "='x'", "=Sheet1!A1+SUM(1)", "=!", "='", "=SUM('a')", '="a"&\'b', "=SUM(1)!"):
            with self.subTest(formula=formula):
                self.assertEqual(self.problems(formula), [CANNOT_BE_CHECKED % "A1"])

    def test_an_unterminated_literal_makes_the_formula_unreadable(self):
        for formula in ('="abc', '=SUM(1)&"abc', '="a\\"', '="\\\\"', '=EVIL(1)&"x\\"&SUM(1)', '="', '=SUM("'):
            with self.subTest(formula=formula):
                problems = self.problems(formula)
                self.assertIn(CANNOT_BE_CHECKED % "A1", problems)

    def test_a_quote_or_a_bang_inside_a_literal_is_fine(self):
        for formula in ('="it\'s"', '="a!b"', '="\'x\'!A1"', '="x"&"y!z"', '=SUM(1)&"\'"'):
            with self.subTest(formula=formula):
                self.assertEqual(self.problems(formula), [])

    def test_the_unreadable_line_is_one_line_with_at_most_three_cells_sorted(self):
        cells = {ref: '="open' for ref in ("D1", "B1", "A10", "A2", "C1")}
        self.assertEqual(vf.unknown_function_problems(dashboard(cells), self.KNOWN), [CANNOT_BE_CHECKED % "A10, A2, B1"])
        self.assertEqual(vf.unknown_function_problems(dashboard({"B1": '="a', "A1": "='"}), self.KNOWN),
                         [CANNOT_BE_CHECKED % "A1, B1"])

    def test_function_problems_come_first_and_the_unreadable_line_last(self):
        data = dashboard({"A1": "=EVIL(1)", "B1": '="open', "C1": "=ZED(1)"})
        self.assertEqual(vf.unknown_function_problems(data, self.KNOWN),
                         [self.EVIL, "function ZED() is not defined by the vendored client (cells: C1)",
                          CANNOT_BE_CHECKED % "B1"])

    def test_a_readable_sheet_has_no_unreadable_line(self):
        data = dashboard({"A1": '=SUM(1)&"a b"', "A2": "x'y", "A3": 'say "hi"', "A4": "!"})        # A2-A4 are not formulas
        self.assertEqual(vf.unknown_function_problems(data, self.KNOWN), [])

    def test_the_guard_does_not_read_the_text_of_the_unreadable_formula_into_the_message(self):
        message = vf.unknown_function_problems(dashboard({"A1": "='secret-looking text'!A1"}), self.KNOWN)[0]
        self.assertNotIn("secret-looking", message)

    # ---- the scan helper
    def test_blank_literals_gives_the_blanked_text_and_whether_it_is_unsure(self):
        self.assertEqual(vf._blank_literals('="a"&SUM(1)'), ("= &SUM(1)", False))
        self.assertEqual(vf._blank_literals('="a\\"b"&N(1)'), ("= &N(1)", False))
        self.assertEqual(vf._blank_literals('="A\\\\"B"&N(1)'), ("= &N(1)", False))
        self.assertEqual(vf._blank_literals("=N(1)"), ("=N(1)", False))
        self.assertEqual(vf._blank_literals('="open'), ("= ", True))
        self.assertEqual(vf._blank_literals("='a'!A1"), ("='a'!A1", True))
        self.assertEqual(vf._blank_literals("=A!B"), ("=A!B", True))
        self.assertEqual(vf._blank_literals('="a\\"'), ("= ", True))

    # ---- conditional formats and data validations (the client compiles their `=` values too)
    def test_a_function_in_a_conditional_format_is_reported_with_its_place(self):
        data = cf_sheet(["=SUM(1)"], ["=EVIL(1)", "3"])
        self.assertEqual(vf.unknown_function_problems(data, self.KNOWN),
                         ["function EVIL() is not defined by the vendored client (cells: conditionalFormats[1])"])
        self.assertEqual(vf.formula_function_names(data), {"SUM": ["conditionalFormats[0]"],
                                                           "EVIL": ["conditionalFormats[1]"]})

    def test_a_function_in_a_data_validation_is_reported_with_its_place(self):
        data = cf_sheet(["=EVIL(1)"], key="dataValidationRules", path=("criterion", "values"))
        self.assertEqual(vf.unknown_function_problems(data, self.KNOWN),
                         ["function EVIL() is not defined by the vendored client (cells: dataValidationRules[0])"])

    def test_every_value_of_a_rule_is_scanned_and_only_the_equals_values(self):
        data = cf_sheet(["1", "=EVIL(1)"], ["=FOO(1)", "=ZED(1)"], ["EVIL(1)", 5, None, ["=EVIL(1)"], {"a": "=EVIL(1)"}])
        self.assertEqual(sorted(vf.formula_function_names(data)), ["EVIL", "FOO", "ZED"])
        self.assertEqual(vf.formula_function_names(data)["EVIL"], ["conditionalFormats[0]"])
        self.assertEqual(vf.formula_function_names(data)["ZED"], ["conditionalFormats[1]"])

    def test_a_rule_value_with_a_quote_outside_a_string_is_unreadable_too(self):
        data = cf_sheet(["='a\"b'!A1+EVIL(1)+\"c\""], key="dataValidationRules", path=("criterion", "values"))
        self.assertEqual(vf.unknown_function_problems(data, self.KNOWN), [CANNOT_BE_CHECKED % "dataValidationRules[0]"])
        data = cf_sheet(['="open'])
        self.assertEqual(vf.unknown_function_problems(data, self.KNOWN), [CANNOT_BE_CHECKED % "conditionalFormats[0]"])

    def test_cells_and_rules_are_scanned_together_on_every_sheet(self):
        first = {"cells": {"A1": {"content": "=EVIL(1)"}}, "conditionalFormats": [{"rule": {"values": ["=EVIL(2)"]}}]}
        second = {"cells": {}, "dataValidationRules": [{"criterion": {"values": ["=EVIL(3)"]}}]}
        names = vf.formula_function_names({"sheets": [first, second]})
        self.assertEqual(names, {"EVIL": ["A1", "conditionalFormats[0]", "dataValidationRules[0]"]})

    def test_odd_rule_shapes_are_skipped_without_an_error(self):
        for sheet in ({"conditionalFormats": None}, {"conditionalFormats": []}, {"conditionalFormats": [None, 5, "x", []]},
                      {"conditionalFormats": [{}]}, {"conditionalFormats": [{"rule": None}]},
                      {"conditionalFormats": [{"rule": "=EVIL(1)"}]}, {"conditionalFormats": [{"rule": {}}]},
                      {"conditionalFormats": [{"rule": {"values": None}}]},
                      {"conditionalFormats": [{"rule": {"values": "=EVIL(1)"}}]},
                      {"conditionalFormats": [{"rule": {"values": {"a": "=EVIL(1)"}}}]},
                      {"dataValidationRules": [{"criterion": {"values": "=EVIL(1)"}}]},
                      {"dataValidationRules": [{"rule": {"values": ["=EVIL(1)"]}}]},
                      {"conditionalFormats": {"rule": {"values": ["=EVIL(1)"]}}}):
            with self.subTest(sheet=sheet):
                self.assertEqual(vf.formula_function_names({"sheets": [sheet]}), {})

    def test_a_sheet_that_is_not_an_object_is_skipped_and_the_next_ones_are_still_scanned(self):
        sheets = ["x", None, 5, [], {"cells": {"A1": {"content": "=FOO(1)"}}}]
        self.assertEqual(vf.formula_function_names({"sheets": sheets}), {"FOO": ["A1"]})
        self.assertEqual(vf.unknown_function_problems({"sheets": sheets}, self.KNOWN),
                         ["function FOO() is not defined by the vendored client (cells: A1)"])

    def test_the_other_places_of_a_rule_are_not_scanned(self):
        # only `rule.values` and `criterion.values` hold formulas: a `=` text elsewhere is plain text
        item = {"rule": {"type": "=EVIL(1)", "operator": "=EVIL(2)", "values": ["1"], "style": {"x": "=EVIL(3)"}},
                "ranges": ["=EVIL(4)"], "id": "=EVIL(5)"}
        self.assertEqual(vf.formula_function_names({"sheets": [{"conditionalFormats": [item]}]}), {})


class GuardTimeTests(unittest.TestCase):
    """Every hostile shape of the new name / literal scan stays linear (a few KB, generous bound)."""

    def test_hostile_formulas_are_checked_in_linear_time(self):
        for label, content in (("dollars", "=" + "$" * 20000), ("name then spaces", "=A" + " " * 20000),
                               ("feff run", "=A" + FEFF * 20000), ("name feff pairs", "=" + ("A" + FEFF) * 10000),
                               ("dots", "=" + "A." * 10000), ("letters", "=" + "É" * 20000),
                               ("quotes", "=" + "'" * 20000), ("bangs", "=" + "!" * 20000),
                               ("escaped pairs", '="' + 'a\\"' * 12000), ("closed pairs", "=" + '"\\"' * 12000),
                               ("parens", "=" + "A(" * 10000)):
            with self.subTest(label=label):
                start = time.perf_counter()
                vf.unknown_function_problems(dashboard({"A1": content}), {"SUM"})
                elapsed = time.perf_counter() - start
                self.assertLess(elapsed, 1.5, "%s: %.2f s" % (label, elapsed))


class BlankLiteralsParityTests(unittest.TestCase):
    """The guard and the intake blank literals with the same rule (both copy the client tokenizer): they must agree on
    every string over the alphabet of the tokenizer's special characters, so a formula the intake crops is read the
    same way by the guard that runs after the bind and before the upload."""
    ALPHABET = '"\\\'!a'
    LENGTH = 7

    def test_both_scanners_agree_on_every_string_up_to_the_length(self):
        import itertools
        import intake_xlsx
        compared = unsure_count = 0
        mismatches = []
        for size in range(self.LENGTH + 1):
            for chars in itertools.product(self.ALPHABET, repeat=size):
                content = "".join(chars)
                blanked, unsure = vf._blank_literals(content)
                other_blanked, why = intake_xlsx._scan(content)
                compared += 1
                unsure_count += unsure
                if blanked != other_blanked or unsure != (why is not None):
                    mismatches.append((content, (blanked, unsure), (other_blanked, why)))
        self.assertEqual(compared, sum(len(self.ALPHABET) ** n for n in range(self.LENGTH + 1)))
        self.assertGreater(unsure_count, 10000)                    # both outcomes are well represented
        self.assertGreater(compared - unsure_count, 5000)
        self.assertEqual(len(mismatches), 0, "%d mismatches, first: %r" % (len(mismatches), mismatches[:5]))

    def test_the_unsure_flag_follows_the_reason_of_the_intake_exactly(self):
        import intake_xlsx
        for content, reason in (("=A1", None), ('="a"', None), ("='a'!A1", "reads another sheet"),
                                ("=A!B", "reads another sheet"), ('="open', "has an unbalanced quote"),
                                ('="a\\"', "has an unbalanced quote")):
            with self.subTest(content=content):
                self.assertEqual(intake_xlsx._scan(content)[1], reason)
                self.assertEqual(vf._blank_literals(content)[1], reason is not None)


# ---------------------------------------------------------------- 2. the core o_spreadsheet.js and the Odoo additions

class CoreFunctionNamesTests(unittest.TestCase):
    def test_every_entry_of_every_category_is_collected_and_underscores_become_dots(self):
        categories = fx.default_categories(with_sum=True)
        names = vf.core_function_names(fx.core_source(categories))
        self.assertEqual(names, fx.expected_core_names(categories))
        self.assertIn("UNARY.PERCENT", names)
        self.assertIn("CEILING.MATH", names)
        self.assertIn("LOG10", names)
        self.assertIn("SUM", names)
        self.assertFalse([n for n in names if "_" in n])
        self.assertEqual(len(names), 22)                 # Cat1: 4 names, Cat2..Cat10: 2 each

    def test_a_name_registered_by_two_categories_counts_once(self):
        categories = fx.default_categories()
        categories["Cat2"] = categories["Cat2"] + ["LOG10"]
        names = vf.core_function_names(fx.core_source(categories))
        self.assertEqual(names, fx.expected_core_names(categories))

    def test_a_variable_that_no_category_lists_is_ignored(self):
        extra = "    var Unused = /*#__PURE__*/Object.freeze({\n        __proto__: null,\n        GHOST: GHOST\n    });"
        names = vf.core_function_names(fx.core_source(extra=extra))
        self.assertNotIn("GHOST", names)

    def test_only_the_categories_block_is_read(self):
        extra = "    const other = [\n        { name: \"z\", functions: Zed },\n    ];"
        self.assertEqual(vf.core_function_names(fx.core_source(extra=extra)),
                         fx.expected_core_names(fx.default_categories()))

    def test_a_windows_line_ending_source_gives_the_same_names(self):
        categories = fx.default_categories(with_sum=True)
        self.assertEqual(vf.core_function_names(fx.core_source(categories, newline="\r\n")),
                         fx.expected_core_names(categories))

    def test_exactly_ten_categories_are_enough_and_nine_are_not(self):
        self.assertTrue(vf.core_function_names(fx.core_source(fx.default_categories(10))))
        with self.assertRaises(vf.ValidateError) as raised:
            vf.core_function_names(fx.core_source(fx.default_categories(9)))
        self.assertIn("only 9 function categories", str(raised.exception))

    def test_no_categories_block_raises(self):
        for source in ("const something = [];", "", "const categories = [ no end"):
            with self.subTest(source=source):
                with self.assertRaises(vf.ValidateError) as raised:
                    vf.core_function_names(source)
                self.assertIn("`const categories = [...]` not found", str(raised.exception))

    def test_a_category_whose_frozen_object_is_missing_raises_and_names_it(self):
        source = fx.core_source().replace("var Cat7 = /*#__PURE__*/Object.freeze(", "var Cat7x = (", 1)
        with self.assertRaises(vf.ValidateError) as raised:
            vf.core_function_names(source)
        self.assertIn("var Cat7 = Object.freeze", str(raised.exception))

    def test_the_pure_annotation_is_part_of_the_layout(self):
        source = fx.core_source().replace("/*#__PURE__*/", "", 1)
        with self.assertRaises(vf.ValidateError):
            vf.core_function_names(source)

    def test_an_entry_that_is_not_name_colon_name_raises_and_never_returns_a_partial_set(self):
        bad_lines = ["SUM: SUM2", "SUM: SUM_X", "SUM: function () {}", "sum: sum", "SUM: OTHER", "...SUM", "SUM,"]
        for var in ("Cat1", "Cat10"):                  # first and last category: nothing parsed before it is returned
            for bad in bad_lines:
                with self.subTest(var=var, bad=bad):
                    categories = fx.default_categories()
                    raw = ["%s: %s" % (n, n) for n in categories[var]] + [bad]
                    with self.assertRaises(vf.ValidateError) as raised:
                        vf.core_function_names(fx.core_source(categories, entries={var: raw}))
                    self.assertIn("category %s has" % var, str(raised.exception))

    def test_blank_lines_and_the_proto_line_inside_a_category_are_fine(self):
        categories = fx.default_categories()
        raw = ["", "FUNC1A: FUNC1A", "", "FUNC1_B: FUNC1_B"]
        names = vf.core_function_names(fx.core_source(categories, entries={"Cat1": raw}))
        self.assertIn("FUNC1.B", names)

    def test_the_message_counts_entries_against_well_formed_entries(self):
        categories = fx.default_categories()
        raw = ["A1X: A1X", "bad line"]
        with self.assertRaises(vf.ValidateError) as raised:
            vf.core_function_names(fx.core_source(categories, entries={"Cat3": raw}))
        self.assertIn("category Cat3 has 2 entries but 1 are `NAME: NAME`", str(raised.exception))


class ModulePathTests(IsolatedTestCase):
    def test_the_first_folder_that_holds_the_module_wins(self):
        first, second = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
        os.makedirs(os.path.join(first, "spreadsheet"))
        os.makedirs(os.path.join(second, "spreadsheet"))
        self.assertEqual(vf.module_path([first, second], "spreadsheet"), os.path.join(first, "spreadsheet"))
        self.assertEqual(vf.module_path([second, first], "spreadsheet"), os.path.join(second, "spreadsheet"))

    def test_a_later_folder_is_used_when_the_first_lacks_the_module(self):
        first, second = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
        os.makedirs(first)
        os.makedirs(os.path.join(second, "spreadsheet"))
        self.assertEqual(vf.module_path([first, second], "spreadsheet"), os.path.join(second, "spreadsheet"))

    def test_none_when_no_folder_has_it_or_there_is_no_folder(self):
        first = os.path.join(self.tmp, "a")
        os.makedirs(first)
        self.assertIsNone(vf.module_path([first, os.path.join(self.tmp, "missing")], "spreadsheet"))
        self.assertIsNone(vf.module_path([], "spreadsheet"))

    def test_a_plain_file_with_the_module_name_is_not_a_module(self):
        first, second = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
        write(os.path.join(first, "spreadsheet"), "not a folder")
        os.makedirs(os.path.join(second, "spreadsheet"))
        self.assertEqual(vf.module_path([first, second], "spreadsheet"), os.path.join(second, "spreadsheet"))


class OdooFunctionNamesTests(IsolatedTestCase):
    def names(self, files=None, extra_files=None, addons=None, **kwargs):
        root = make_tree(os.path.join(self.tmp, "addons"), modules=files, extra_files=extra_files)
        return vf.odoo_function_names(addons or [root], **kwargs)

    def test_registry_adds_are_found_and_upper_cased(self):
        found = self.names({"spreadsheet": {"functions/a.js": registry_js("ODOO.X", "odoo.lower"),
                                            "functions/b.js": registry_js("_t")}})
        self.assertEqual(found, {"ODOO.X", "ODOO.LOWER", "_T"})

    def test_an_add_call_split_over_lines_is_found(self):
        text = 'functionRegistry\n    .add(\n        "ODOO.SPLIT",\n        {});\n'
        self.assertEqual(self.names({"spreadsheet": {"x.js": text}}), {"ODOO.SPLIT"})

    def test_an_add_in_a_file_without_the_word_function_registry_is_ignored(self):
        text = 'cellRegistry.add("NOT.A.FUNCTION", {});\n'
        self.assertEqual(self.names({"spreadsheet": {"x.js": text, "y.js": registry_js("ODOO.Y")}}), {"ODOO.Y"})

    def test_the_core_file_itself_is_skipped_wherever_it_sits(self):
        found = self.names({"spreadsheet": {"o_spreadsheet/o_spreadsheet.js": registry_js("CORE.ONLY"),
                                            "deeper/o_spreadsheet.js": registry_js("ALSO.CORE")}})
        self.assertEqual(found, set())

    def test_a_folder_called_tests_is_skipped_at_any_depth_but_similar_names_are_not(self):
        found = self.names({"spreadsheet": {
            "tests/a.js": registry_js("IN.TESTS"), "a/tests/b.js": registry_js("DEEP.TESTS"),
            "a/tests/c/d.js": registry_js("UNDER.TESTS"), "mytests/e.js": registry_js("KEPT.ONE"),
            "tests_helpers/f.js": registry_js("KEPT.TWO")}})
        self.assertEqual(found, {"KEPT.ONE", "KEPT.TWO"})

    def test_only_js_files_under_static_src_are_read(self):
        found = self.names({"spreadsheet": {"a.js": registry_js("ODOO.A"), "b.ts": registry_js("NOT.JS"),
                                            "c.js.map": registry_js("NOT.MAP"), "d.xml": registry_js("NOT.XML")}},
                           extra_files={"spreadsheet/static/lib/e.js": registry_js("OUTSIDE.SRC"),
                                        "spreadsheet/models/f.js": registry_js("OUTSIDE.TOO")})
        self.assertEqual(found, {"ODOO.A"})

    def test_all_four_client_modules_are_scanned_and_no_other(self):
        found = self.names({"spreadsheet": {"a.js": registry_js("M.CORE")},
                            "spreadsheet_dashboard": {"a.js": registry_js("M.DASH")},
                            "spreadsheet_dashboard_edition": {"a.js": registry_js("M.DASHED")},
                            "spreadsheet_edition": {"a.js": registry_js("M.EDIT")},
                            "spreadsheet_other": {"a.js": registry_js("M.OTHER")}})
        self.assertEqual(found, {"M.CORE", "M.DASH", "M.DASHED", "M.EDIT"})

    def test_a_modules_argument_replaces_the_default_list(self):
        found = self.names({"spreadsheet": {"a.js": registry_js("M.CORE")}, "mine": {"a.js": registry_js("M.MINE")}},
                           modules=("mine",))
        self.assertEqual(found, {"M.MINE"})

    def test_missing_modules_are_skipped(self):
        self.assertEqual(self.names({"spreadsheet_edition": {"a.js": registry_js("M.EDIT")}}), {"M.EDIT"})
        self.assertEqual(vf.odoo_function_names([os.path.join(self.tmp, "nowhere")]), set())

    def test_a_module_is_read_from_the_first_folder_that_holds_it_only(self):
        first = make_tree(os.path.join(self.tmp, "a"), modules={"spreadsheet_edition": {"a.js": registry_js("FROM.A")}})
        second = make_tree(os.path.join(self.tmp, "b"), modules={"spreadsheet_edition": {"a.js": registry_js("FROM.B")},
                                                                  "spreadsheet_dashboard": {"a.js": registry_js("ONLY.B")}})
        self.assertEqual(vf.odoo_function_names([first, second]), {"FROM.A", "ONLY.B"})
        self.assertEqual(vf.odoo_function_names([second, first]), {"FROM.B", "ONLY.B"})

    def test_a_non_utf8_js_file_is_a_clean_error(self):
        root = make_tree(os.path.join(self.tmp, "addons"))
        write(os.path.join(root, "spreadsheet", "static", "src", "bad.js"), data=b"functionRegistry \xff\xfe")
        with self.assertRaises(vf.ValidateError) as raised:
            vf.odoo_function_names([root])
        self.assertIn("is not UTF-8 text", str(raised.exception))

    def test_a_js_file_over_the_size_limit_is_refused_and_one_at_the_limit_is_read(self):
        root = make_tree(os.path.join(self.tmp, "addons"))
        text = registry_js("ODOO.SIZE")
        path = write(os.path.join(root, "spreadsheet", "static", "src", "big.js"), text)
        size = os.path.getsize(path)
        with mock.patch.object(vf, "MAX_FILE", size):
            self.assertEqual(vf.odoo_function_names([root]), {"ODOO.SIZE"})
        with mock.patch.object(vf, "MAX_FILE", size - 1):
            with self.assertRaises(vf.ValidateError) as raised:
                vf.odoo_function_names([root])
        self.assertIn("larger than", str(raised.exception))


class ReadTextBoundTests(IsolatedTestCase):
    def test_a_source_that_never_ends_is_read_up_to_the_limit_only(self):
        stand_in = BoundedFile(size=3 * 1000, limit=1001)
        with mock.patch.object(vf, "MAX_FILE", 1000), mock.patch.object(vf, "open", lambda *a, **k: stand_in,
                                                                         create=True):
            with self.assertRaises(vf.ValidateError) as raised:
                vf._read_text("unused")
        self.assertIn("larger than", str(raised.exception))
        self.assertEqual(stand_in.asked, [1001])

    def test_a_source_of_exactly_the_limit_is_read_whole_by_one_bounded_read(self):
        stand_in = BoundedFile(size=1000, limit=1001)
        with mock.patch.object(vf, "MAX_FILE", 1000), mock.patch.object(vf, "open", lambda *a, **k: stand_in,
                                                                         create=True):
            text = vf._read_text("unused")
        self.assertEqual(len(text), 1000)
        self.assertEqual(stand_in.asked, [1001])

    def test_the_frozen_list_is_read_with_a_bounded_read_too(self):
        stand_in = BoundedFile(size=3 * 1000, limit=1000)
        with mock.patch.object(vf, "MAX_FILE", 1000), mock.patch.object(vf, "open", lambda *a, **k: stand_in,
                                                                         create=True):
            with self.assertRaises(vf.ValidateError):
                vf.load_frozen("unused")
        self.assertEqual(stand_in.asked, [1000])

    def test_an_unreadable_or_missing_file_is_a_clean_error_without_the_path_leaking_a_traceback(self):
        with self.assertRaises(vf.ValidateError) as raised:
            vf._read_text(os.path.join(self.tmp, "missing.js"))
        self.assertIn("cannot read", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)       # `from None`: no chained OSError in a traceback
        with self.assertRaises(vf.ValidateError):
            vf._read_text(self.tmp)                    # a directory
        bad = write(os.path.join(self.tmp, "bad.js"), data=b"\xff\xfe")
        with self.assertRaises(vf.ValidateError) as raised:
            vf._read_text(bad)
        self.assertTrue(raised.exception.__suppress_context__)


# ---------------------------------------------------------------- 3. derive / dump / load / drift

class DeriveTreeTests(IsolatedTestCase):
    def tree(self, name="addons", **kwargs):
        return make_tree(os.path.join(self.tmp, name), **kwargs)

    def test_names_and_source_info_of_one_tree(self):
        categories = fx.default_categories(with_sum=True)
        source = fx.core_source(categories)
        root = self.tree(core=source, modules={"spreadsheet_edition": {"a.js": registry_js("ODOO.X", "_t")}})
        names, info = vf.derive_tree([root])
        self.assertEqual(names, fx.expected_core_names(categories) | {"ODOO.X", "_T"})
        self.assertEqual(info, {"o_spreadsheet_version": fx.CORE_VERSION,
                               "o_spreadsheet_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                               "function_count": len(names),
                               "modules": ["spreadsheet", "spreadsheet_edition"]})

    def test_the_modules_list_follows_the_client_module_order_and_looks_in_every_folder(self):
        first = self.tree("a", modules={"spreadsheet_edition": {"a.js": registry_js("ODOO.E")}})
        second = os.path.join(self.tmp, "b")
        write(os.path.join(second, "spreadsheet_dashboard", "static", "src", "a.js"), registry_js("ODOO.D"))
        names, info = vf.derive_tree([first, second])
        self.assertEqual(info["modules"], ["spreadsheet", "spreadsheet_dashboard", "spreadsheet_edition"])
        self.assertIn("ODOO.D", names)

    def test_the_core_comes_from_the_first_folder_that_holds_spreadsheet(self):
        first = self.tree("a", core=fx.core_source(version="17.0.1"))
        second = self.tree("b", core=fx.core_source(version="17.0.2"))
        self.assertEqual(vf.derive_tree([first, second])[1]["o_spreadsheet_version"], "17.0.1")
        self.assertEqual(vf.derive_tree([second, first])[1]["o_spreadsheet_version"], "17.0.2")

    def test_the_version_is_read_from_the_head_of_the_file_only(self):
        near = self.tree("near", core=fx.core_source(version_offset=10))
        far = self.tree("far", core=fx.core_source(version_offset=250))
        self.assertEqual(vf.derive_tree([near])[1]["o_spreadsheet_version"], fx.CORE_VERSION)
        self.assertEqual(vf.derive_tree([far])[1]["o_spreadsheet_version"], "unknown")

    def test_a_missing_version_tag_is_unknown(self):
        root = self.tree(core=fx.core_source(version=None))
        self.assertEqual(vf.derive_tree([root])[1]["o_spreadsheet_version"], "unknown")

    def test_a_missing_spreadsheet_module_is_a_clean_error_naming_the_folders(self):
        empty_a, empty_b = os.path.join(self.tmp, "x"), os.path.join(self.tmp, "y")
        os.makedirs(empty_a)
        os.makedirs(empty_b)
        with self.assertRaises(vf.ValidateError) as raised:
            vf.derive_tree([empty_a, empty_b])
        self.assertEqual(str(raised.exception), "the module `spreadsheet` is not in %s,%s" % (empty_a, empty_b))

    def test_a_missing_core_file_or_a_changed_layout_fails_loudly(self):
        root = os.path.join(self.tmp, "nocore")
        os.makedirs(os.path.join(root, "spreadsheet"))
        with self.assertRaises(vf.ValidateError) as raised:
            vf.derive_tree([root])
        self.assertIn("cannot read", str(raised.exception))
        garbage = self.tree("garbage", core="const nothing = 1;")
        with self.assertRaises(vf.ValidateError):
            vf.derive_tree([garbage])

    def test_a_non_utf8_core_is_a_clean_error(self):
        root = os.path.join(self.tmp, "bad")
        write(os.path.join(root, "spreadsheet", *fx.CORE_JS_PARTS), data=b"\xff\xfe\x00")
        with self.assertRaises(vf.ValidateError) as raised:
            vf.derive_tree([root])
        self.assertIn("is not UTF-8 text", str(raised.exception))


class DeriveTests(IsolatedTestCase):
    def tree(self, name, categories=None, adds=(), version=fx.CORE_VERSION):
        modules = {"spreadsheet_edition": {"a.js": registry_js(*adds)}} if adds else None
        return make_tree(os.path.join(self.tmp, name), core=fx.core_source(categories, version=version), modules=modules)

    def test_the_document_has_a_sorted_unique_function_list_and_one_source_per_tree(self):
        root = self.tree("a", adds=("ODOO.Z", "ODOO.A", "ODOO.A"))
        document = vf.derive([[root]])
        self.assertEqual(document["schema"], vf.SCHEMA)
        self.assertEqual(document["functions"], sorted(set(document["functions"])))
        self.assertIn("ODOO.A", document["functions"])
        self.assertEqual(len(document["sources"]), 1)
        self.assertEqual(document["sources"][0]["function_count"], len(document["functions"]))

    def test_no_tree_is_an_error(self):
        with self.assertRaises(vf.ValidateError):
            vf.derive([])

    def test_the_list_is_the_intersection_over_the_trees_not_the_union(self):
        categories = fx.default_categories()
        wider = dict(categories, Cat2=categories["Cat2"] + ["ONLY_IN_A"])
        narrower = dict(categories, Cat2=categories["Cat2"] + ["ONLY_IN_B"])
        a = self.tree("a", wider, adds=("ODOO.SHARED", "ODOO.ONLY_A"))
        b = self.tree("b", narrower, adds=("ODOO.SHARED", "ODOO.ONLY_B"))
        names = set(vf.derive([[a], [b]])["functions"])
        self.assertEqual(names, fx.expected_core_names(categories) | {"ODOO.SHARED"})
        self.assertFalse({"ONLY.IN.A", "ONLY.IN.B", "ODOO.ONLY_A", "ODOO.ONLY_B"} & names)

    def test_three_trees_intersect_all_three_and_sources_keep_the_tree_order(self):
        base = fx.default_categories()
        a = self.tree("a", dict(base, Cat2=base["Cat2"] + ["X_ALL", "X_AB"]), version="17.0.1")
        b = self.tree("b", dict(base, Cat2=base["Cat2"] + ["X_ALL", "X_AB", "X_B"]), version="17.0.2")
        c = self.tree("c", dict(base, Cat2=base["Cat2"] + ["X_ALL", "X_C"]), version="17.0.3")
        document = vf.derive([[a], [b], [c]])
        self.assertIn("X.ALL", document["functions"])
        self.assertFalse({"X.AB", "X.B", "X.C"} & set(document["functions"]))
        self.assertEqual([s["o_spreadsheet_version"] for s in document["sources"]], ["17.0.1", "17.0.2", "17.0.3"])

    def test_a_tree_may_be_several_addons_folders(self):
        first = self.tree("a")
        second = os.path.join(self.tmp, "b")
        write(os.path.join(second, "spreadsheet_edition", "static", "src", "x.js"), registry_js("ODOO.IN.SECOND"))
        self.assertIn("ODOO.IN.SECOND", vf.derive([[first, second]])["functions"])

    def test_a_tree_without_spreadsheet_stops_the_whole_derivation(self):
        good = self.tree("good")
        bad = os.path.join(self.tmp, "bad")
        os.makedirs(bad)
        with self.assertRaises(vf.ValidateError):
            vf.derive([[good], [bad]])


class DumpBytesTests(IsolatedTestCase):
    def document(self):
        root = make_tree(os.path.join(self.tmp, "a"), modules={"spreadsheet_edition": {"a.js": registry_js("ODOO.X")}})
        return vf.derive([[root]])

    def test_two_derivations_of_the_same_tree_dump_to_the_same_bytes(self):
        first, second = vf.dump_bytes(self.document()), vf.dump_bytes(self.document())
        self.assertEqual(first, second)
        self.assertIsInstance(first, bytes)

    def test_lf_line_ends_one_final_newline_and_an_indent_of_one(self):
        data = vf.dump_bytes(self.document())
        self.assertNotIn(b"\r", data)
        self.assertTrue(data.endswith(b"}\n"))
        self.assertFalse(data.endswith(b"\n\n"))
        self.assertTrue(data.split(b"\n")[1].startswith(b' "'))
        self.assertFalse(data.split(b"\n")[1].startswith(b'  '))

    def test_keys_are_sorted_at_every_level_whatever_the_insertion_order(self):
        data = vf.dump_bytes(self.document()).decode("utf-8")
        self.assertLess(data.index('"functions"'), data.index('"schema"'))
        self.assertLess(data.index('"schema"'), data.index('"sources"'))
        self.assertLess(data.index('"function_count"'), data.index('"modules"'))
        self.assertLess(data.index('"modules"'), data.index('"o_spreadsheet_sha256"'))
        self.assertLess(data.index('"o_spreadsheet_sha256"'), data.index('"o_spreadsheet_version"'))
        self.assertEqual(vf.dump_bytes({"b": 1, "a": 2}), vf.dump_bytes({"a": 2, "b": 1}))

    def test_text_is_utf8_not_escaped(self):
        self.assertIn("Größe".encode("utf-8"), vf.dump_bytes({"functions": ["Größe"]}))

    def test_the_dump_round_trips_through_json(self):
        document = self.document()
        self.assertEqual(json.loads(vf.dump_bytes(document).decode("utf-8")), document)

    def test_the_dump_holds_no_absolute_path_and_no_user_name(self):
        text = vf.dump_bytes(self.document()).decode("utf-8")
        self.assertNotIn(self.tmp, text)
        self.assertNotIn(os.sep + "tmp", text)
        self.assertNotIn(os.path.expanduser("~"), text)
        for name in {os.environ.get("USER", ""), os.environ.get("USERNAME", "")} - {""}:
            self.assertNotIn(name, text)
        self.assertNotIn("/", text)
        self.assertNotIn("\\", text)


class LoadFrozenTests(IsolatedTestCase):
    def path(self, text=None, data=None):
        return write(os.path.join(self.tmp, "frozen.json"), text, data)

    def test_a_good_file_gives_the_name_set(self):
        self.assertEqual(vf.load_frozen(self.path(json.dumps({"schema": 1, "functions": ["SUM", "IF", "SUM"]}))),
                         {"SUM", "IF"})

    def test_the_default_is_the_file_next_to_the_script(self):
        self.assertEqual(vf.FROZEN_PATH, os.path.join(str(fx.REPO), "scripts", "client_functions.json"))
        self.assertEqual(vf.load_frozen(), vf.load_frozen(vf.FROZEN_PATH))

    def test_a_wrong_or_missing_schema_is_refused(self):
        for document in ({"schema": 2, "functions": ["SUM"]}, {"functions": ["SUM"]}, {"schema": "1", "functions": ["SUM"]},
                         {"schema": None, "functions": ["SUM"]}, {"schema": 0, "functions": ["SUM"]}):
            with self.subTest(document=document):
                with self.assertRaises(vf.ValidateError) as raised:
                    vf.load_frozen(self.path(json.dumps(document)))
                self.assertIn("unexpected shape", str(raised.exception))

    def test_an_empty_or_missing_function_list_is_refused(self):
        for document in ({"schema": 1, "functions": []}, {"schema": 1}, {"schema": 1, "functions": None}):
            with self.subTest(document=document):
                with self.assertRaises(vf.ValidateError):
                    vf.load_frozen(self.path(json.dumps(document)))

    def test_a_function_list_that_is_not_a_list_of_strings_is_refused(self):
        for names in ("SUM", {"SUM": 1}, 5, [1, "SUM"], [None], [["SUM"]], ["SUM", 2.5], ["SUM", True]):
            with self.subTest(names=names):
                with self.assertRaises(vf.ValidateError):
                    vf.load_frozen(self.path(json.dumps({"schema": 1, "functions": names})))

    def test_a_document_that_is_not_an_object_is_refused_with_a_clean_error(self):
        for text in ("[]", '["SUM"]', "5", '"SUM"', "null", "true"):
            with self.subTest(text=text):
                with self.assertRaises(vf.ValidateError) as raised:
                    vf.load_frozen(self.path(text))
                self.assertIn("unexpected shape", str(raised.exception))

    def test_a_missing_file_a_directory_and_a_non_json_file_are_clean_errors(self):
        for target in (os.path.join(self.tmp, "missing.json"), self.tmp, self.path("not json {"),
                       self.path(data=b"\xff\xfe\x00 not utf-8"), self.path("")):
            with self.subTest(target=target):
                with self.assertRaises(vf.ValidateError) as raised:
                    vf.load_frozen(target)
                self.assertIn("cannot read the frozen function list", str(raised.exception))
                self.assertNotIn(self.tmp, str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(raised.exception.__suppress_context__)


class DriftTests(unittest.TestCase):
    def test_removed_and_added_are_reported_sorted(self):
        self.assertEqual(vf.drift({"Z", "A", "M", "KEEP"}, {"KEEP", "Y", "B"}), (["A", "M", "Z"], ["B", "Y"]))
        # a set of strings iterates in a hash order that changes with every run (PYTHONHASHSEED): with 3 names a
        # mutant that forgets `sorted` is right 1 run in 6, with 24 names essentially never
        removed = {"R%02d" % number for number in range(24)}
        added = {"A%02d" % number for number in range(24)}
        self.assertEqual(vf.drift(removed | {"KEEP"}, added | {"KEEP"}), (sorted(removed), sorted(added)))

    def test_no_difference_gives_two_empty_lists(self):
        self.assertEqual(vf.drift({"A", "B"}, {"A", "B"}), ([], []))

    def test_only_removed_and_only_added(self):
        self.assertEqual(vf.drift({"A", "B"}, {"A"}), (["B"], []))
        self.assertEqual(vf.drift({"A"}, {"A", "B"}), ([], ["B"]))

    def test_the_comparison_is_case_sensitive(self):
        self.assertEqual(vf.drift({"SUM"}, {"sum"}), (["SUM"], ["sum"]))


@unittest.skipUnless(REAL_TREE, "maintainer check, needs Odoo sources: set KPI_ODOO_ADDONS_PATH=<comma separated "
                                "addons folders of one Odoo tree> to compare the frozen list with the real client")
class DriftOnARealTreeTests(unittest.TestCase):
    """Opt-in (the plugin repo holds no Odoo sources): the frozen list still matches the client of a real tree."""

    def test_the_frozen_list_matches_the_derivation_of_this_tree(self):
        names, _ = vf.derive_tree([p for p in REAL_TREE.split(",") if p])
        self.assertEqual(vf.drift(vf.load_frozen(), names), ([], []))


# ---------------------------------------------------------------- 4. the REAL frozen file

class RealFrozenFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(vf.FROZEN_PATH, "rb") as handle:
            cls.raw = handle.read()
        cls.document = json.loads(cls.raw.decode("utf-8"))
        cls.names = vf.load_frozen()

    def test_the_list_is_large_and_holds_the_functions_the_dashboards_use(self):
        self.assertGreater(len(self.names), 200)           # a mostly empty list would make the guard vacuous
        for name in ("SUM", "IF", "IFERROR", "ODOO.LIST", "ODOO.PIVOT", "_T", "INT", "ISNUMBER", "ROUND", "TRUNC", "ADD",
                     "MINUS", "UNARY.PERCENT", "CEILING.MATH", "CONCATENATE", "AVERAGE", "AVERAGEIF", "RIGHT", "MAX",
                     "SUBSTITUTE", "ODOO.LIST.HEADER", "ODOO.FILTER.VALUE", "ODOO.CURRENCY.RATE"):
            self.assertIn(name, self.names, name)

    def test_n_and_invented_names_are_not_in_the_list(self):
        for name in ("N", "FOO", "n", "sum", "UNARY_PERCENT", ""):
            self.assertNotIn(name, self.names, name)

    def test_every_name_is_a_plausible_function_name(self):
        for name in self.names:
            self.assertRegex(name, r"^[A-Z_][A-Z0-9_.]*$", name)

    def test_every_function_the_evaluator_implements_is_a_function_of_the_client(self):
        for name in ("SUM", "IF", "IFERROR", "INT", "MAX", "AVERAGEIF", "RIGHT", "SUBSTITUTE", "_T", "ODOO.LIST"):
            self.assertIn(name, self.names, name)

    def test_the_file_is_the_canonical_dump_of_its_own_content(self):
        self.assertEqual(vf.dump_bytes(self.document), self.raw)

    def test_the_functions_are_sorted_and_unique_and_the_sources_are_recorded(self):
        functions = self.document["functions"]
        self.assertEqual(functions, sorted(set(functions)))
        self.assertGreaterEqual(len(self.document["sources"]), 1)
        for source in self.document["sources"]:
            self.assertRegex(source["o_spreadsheet_sha256"], r"^[0-9a-f]{64}$")
            self.assertGreaterEqual(source["function_count"], len(functions))
            self.assertEqual(sorted(source), ["function_count", "modules", "o_spreadsheet_sha256",
                                              "o_spreadsheet_version"])
            self.assertIn("spreadsheet", source["modules"])

    def test_the_file_holds_no_absolute_path_and_no_user_name(self):
        text = self.raw.decode("utf-8")
        self.assertNotIn("/", text)
        self.assertNotIn("\\", text)
        self.assertNotIn(os.path.expanduser("~"), text)
        self.assertNotIn(b"\r", self.raw)


# ---------------------------------------------------------------- 5. the evaluator

class ErrorClassesTests(unittest.TestCase):
    def test_the_hierarchy_is_the_one_iferror_relies_on(self):
        self.assertTrue(issubclass(vf.NotCatchable, vf.SheetError))
        self.assertTrue(issubclass(vf.UnknownFunction, vf.NotCatchable))
        self.assertTrue(issubclass(vf.SheetError, Exception))
        self.assertFalse(issubclass(vf.SheetError, vf.ValidateError))
        self.assertFalse(issubclass(vf.ValidateError, vf.SheetError))


class ArithmeticTests(unittest.TestCase):
    def test_precedence_associativity_and_unary_minus(self):
        table = [("=1+2*3", 7), ("=(1+2)*3", 9), ("=-(4-1)", -3), ("=--2", 2), ("=2-3-4", -5), ("=8/2/2", 2),
                 ("=-2*3", -6), ("=2*-3", -6), ("=10-4+1", 7), ("=2*3+4*5", 26), ("=1.5+1.5", 3), ("=12/4*3", 9),
                 ("=-2-2", -4), ("=((1))", 1), ("= 1 + 1 ", 2), ("=0/5", 0)]
        for formula, expected in table:
            with self.subTest(formula=formula):
                self.assertEqual(calc(formula), expected)
                self.assertIsInstance(calc(formula), float)

    def test_ampersand_binds_looser_than_plus_and_tighter_than_comparison(self):
        self.assertEqual(calc("=1+2&3"), "33")
        self.assertEqual(calc('="a"&1+2'), "a3")
        self.assertIs(calc("=1+1=2"), True)
        self.assertIs(calc("=2>1+1"), False)
        self.assertIs(calc('="a"&"b"="AB"'), True)
        self.assertIs(calc("=2*3=6"), True)

    def test_numbers_print_the_way_the_client_prints_them(self):
        table = [('=2026&""', "2026"), ('=0.5&""', "0.5"), ('=-1&""', "-1"), ('=1/4&""', "0.25"), ('=0&""', "0"),
                 ('=2026.0&""', "2026"), ('=1000000&""', "1000000"), ('=0.1+0.2&""', "0.30000000000000004"),
                 ('=(1=1)&""', "TRUE"), ('=(1=2)&""', "FALSE"), ('=A9&"x"', "x"), ('="a"&A9&"b"', "ab"),
                 ('=-0&""', "0"), ('=1/3&""', "0.3333333333333333")]
        for formula, expected in table:
            with self.subTest(formula=formula):
                self.assertEqual(calc(formula), expected)

    def test_an_empty_string_is_zero_in_arithmetic_but_other_text_is_value_error(self):
        for formula, expected in [('=""+1', 1), ('=1+""', 1), ('=""*5', 0), ('="5"+1', 6), ("=A9+1", 1), ("=1-A9", 1),
                                  ("=(1=1)+1", 2), ("=(1=2)+1", 1), ('=-""', 0)]:
            with self.subTest(formula=formula):
                self.assertEqual(calc(formula), expected)
        for formula in ('="x"+1', '=1+"x"', '=-"x"', '="x"*2', '=1/"x"', '="x"-1'):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.SheetError) as raised:
                    calc(formula)
                self.assertNotIsInstance(raised.exception, vf.NotCatchable)
                self.assertIn("#VALUE!", str(raised.exception))

    def test_a_text_cell_is_value_error_and_an_empty_text_cell_is_zero(self):
        with self.assertRaises(vf.SheetError):
            calc("=A1+1", {"A1": "abc"})
        self.assertEqual(calc("=A1+1", {"A1": ""}), 1)
        self.assertEqual(calc("=A1+1", {"A1": "12"}), 13)
        self.assertEqual(calc("=A1*2", {"A1": "-3.5"}), -7)

    def test_division_by_zero_is_a_catchable_error(self):
        for formula in ("=1/0", "=1/A9", "=0/0", "=1/(2-2)", '=1/""'):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.SheetError) as raised:
                    calc(formula)
                self.assertNotIsInstance(raised.exception, vf.NotCatchable)
                self.assertIn("#DIV/0!", str(raised.exception))


class ComparisonTests(unittest.TestCase):
    def test_equality_rules(self):
        table = [("=A1=0", True), ('=A1=""', True), ("=A1=A2", True), ('=A1="x"', False), ("=A1=1", False),
                 ('="abc"="ABC"', True), ('="abc"<>"ABC"', False), ('=1="1"', False), ('="1"=1', False),
                 ("=(1=1)=1", False), ("=(1=1)=(2=2)", True), ("=A1=(1=2)", True), ("=1=1.0", True),
                 ("=0.1+0.2=0.3", True), ("=1=1.000000001", False), ("=1<>2", True), ("=1<>1", False), ("=A1<>0", False),
                 ('="a"<>1', True), ('="TRUE"=(1=1)', False), ('=""=A1', True), ('="a"="a"', True), ('="a"="b"', False),
                 ("=1<2=(2<3)", True), ("=1=1=1", False), ("=2>1<>0", True)]      # comparisons chain from the left
        for formula, expected in table:
            with self.subTest(formula=formula):
                self.assertIs(calc(formula), expected)

    def test_a_cell_holding_empty_text_equals_an_empty_cell(self):
        self.assertIs(calc("=A1=A2", {"A1": ""}), True)
        self.assertIs(calc("=A1=0", {"A1": ""}), True)

    def test_ordering_of_numbers_and_empty_cells(self):
        table = [("=1<2", True), ("=2<1", False), ("=1<1", False), ("=2>1", True), ("=1>1", False), ("=1>2", False),
                 ("=2>=2", True), ("=1>=2", False), ("=1<=1", True), ("=2<=1", False), ("=A1<1", True),
                 ("=A1>0", False), ("=A1>=0", True), ("=A1<=0", True), ("=1>A1", True), ("=-1<A1", True)]
        for formula, expected in table:
            with self.subTest(formula=formula):
                self.assertIs(calc(formula), expected)

    def test_ordering_text_or_booleans_is_a_loud_defect_that_iferror_cannot_hide(self):
        for formula in ('="a"<1', '=1<"a"', '="a">="b"', "=(1=1)>0", '=A1<"x"'):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.NotCatchable):
                    calc(formula)
                with self.assertRaises(vf.NotCatchable):
                    calc("=IFERROR(%s,0)" % formula[1:])


class IfTests(unittest.TestCase):
    def test_branches_with_and_without_else(self):
        table = [("=IF(1,2,3)", 2), ("=IF(0,2,3)", 3), ("=IF(2,\"y\",\"n\")", "y"), ('=IF(-1,"y","n")', "y"),
                 ("=IF(1=1,2)", 2), ("=IF(A9,1,2)", 2), ('=IF("true",1,2)', 1), ('=IF("False",1,2)', 2),
                 ("=IF(0.5,1,2)", 1), ("=IF(1=2,1,2)", 2)]
        for formula, expected in table:
            with self.subTest(formula=formula):
                self.assertEqual(calc(formula), expected)
        self.assertIs(calc("=IF(0,2)"), False)
        self.assertIs(calc("=IF(1=2,2)"), False)

    def test_only_the_taken_branch_is_evaluated(self):
        self.assertEqual(calc("=IF(1,2,1/0)"), 2)
        self.assertEqual(calc("=IF(0,1/0,3)"), 3)
        self.assertEqual(calc("=IF(1,2,NOPE(1))"), 2)
        with self.assertRaises(vf.SheetError):
            calc("=IF(0,2,1/0)")

    def test_a_condition_that_is_not_logical_is_value_error_and_an_error_condition_propagates(self):
        with self.assertRaises(vf.SheetError) as raised:
            calc('=IF("abc",1,2)')
        self.assertIn("#VALUE!", str(raised.exception))
        with self.assertRaises(vf.SheetError):
            calc("=IF(1/0,1,2)")


class IferrorTests(unittest.TestCase):
    def test_it_catches_sheet_errors(self):
        table = [("=IFERROR(1/0,5)", 5), ('=IFERROR("x"+1,5)', 5), ("=IFERROR(1/0)", ""), ("=IFERROR(5,1/0)", 5),
                 ("=IFERROR(IFERROR(1/0,2/0),7)", 7), ('=IFERROR(IF("abc",1,2),9)', 9),
                 ('=IFERROR(RIGHT("abc",-1),"bad")', "bad"), ('=IFERROR(INT("x"),3)', 3),
                 ("=IFERROR(A1,4)", 4), ('=IFERROR(SUM(1,"x"),6)', 6)]
        for formula, expected in table:
            with self.subTest(formula=formula):
                self.assertEqual(calc(formula, {"A1": "=1/0"}), expected)

    def test_an_error_in_the_fallback_propagates(self):
        with self.assertRaises(vf.SheetError):
            calc("=IFERROR(1/0,2/0)")

    def test_the_first_argument_error_is_swallowed_but_a_good_value_is_returned_untouched(self):
        self.assertEqual(calc('=IFERROR("text",1)'), "text")
        self.assertEqual(calc("=IFERROR(0,1)"), 0)

    def test_it_never_hides_a_defect_of_the_formula_or_of_the_test_input(self):
        lists = {1: [{"f": 1}]}
        defects = {
            "unknown function": "NOPE(1)",
            "unprovided list": 'ODOO.LIST(7,1,"f")',
            "list row without the field": 'ODOO.LIST(1,1,"missing")',
            "unsupported AVERAGEIF criterion": 'AVERAGEIF(B1:B2,"abc")',
            "SUBSTITUTE with two arguments": 'SUBSTITUTE("a","b")',
            "text ordering": '"a"<1',
            "bad ODOO.LIST arity": "ODOO.LIST(1,1)",
            "AVERAGEIF with a scalar range": 'AVERAGEIF(1,">0")',
            "unknown function nested": "SUM(1,NOPE2(2))",
        }
        for label, inner in defects.items():
            with self.subTest(defect=label):
                with self.assertRaises(vf.NotCatchable) as bare:
                    calc("=" + inner, lists=lists)
                with self.assertRaises(vf.NotCatchable) as wrapped:
                    calc("=IFERROR(%s,0)" % inner, lists=lists)
                self.assertIs(type(bare.exception), type(wrapped.exception))
                with self.assertRaises(vf.NotCatchable):
                    calc("=IFERROR(%s)" % inner, lists=lists)       # no fallback: still not hidden

    def test_an_unknown_function_is_the_clients_name_error_with_its_name(self):
        with self.assertRaises(vf.UnknownFunction) as raised:
            calc("=IFERROR(NOPE(1),0)")
        self.assertEqual(str(raised.exception), "function NOPE is not supported by the test evaluator")

    def test_a_syntax_error_in_a_referenced_cell_is_not_hidden(self):
        with self.assertRaises(vf.NotCatchable):
            calc("=IFERROR(A1,0)", {"A1": "=1+"})

    def test_a_syntax_error_inside_the_cell_is_a_compile_error_before_iferror_runs(self):
        for formula in ("=IFERROR(1+,0)", "=IFERROR(1,", "=IFERROR(1,0))", "=IFERROR(#,0)"):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.NotCatchable):
                    calc(formula)

    def test_an_unprovided_list_is_not_turned_into_a_silent_zero(self):
        with self.assertRaises(vf.NotCatchable) as raised:
            calc('=IFERROR(ODOO.LIST(1,1,"qty")+0,0)')
        self.assertIn("is not provided", str(raised.exception))


class SumTests(unittest.TestCase):
    CELLS = {"B1": "1", "B2": "abc", "B4": "=1=1", "B5": "=2"}          # B3 is absent

    def test_references_and_ranges_skip_text_booleans_and_empty_cells(self):
        self.assertEqual(calc("=SUM(B1:B5)", self.CELLS), 3)
        self.assertEqual(calc("=SUM(B1,B2,B3,B4,B5)", self.CELLS), 3)
        self.assertEqual(calc("=SUM(B1:B2)+SUM(B5)", self.CELLS), 3)
        self.assertEqual(calc("=SUM(B2)", self.CELLS), 0)
        self.assertEqual(calc("=SUM(A9)"), 0)
        self.assertIsInstance(calc("=SUM(B1:B5)", self.CELLS), float)

    def test_a_formula_cell_that_yields_text_is_skipped_even_when_it_looks_numeric(self):
        self.assertEqual(calc("=SUM(D1:D2)", {"D1": '="7"', "D2": "3"}), 3)

    def test_scalars_in_the_formula_are_strict(self):
        for formula, expected in [("=SUM(1,2)", 3), ('=SUM(1,"2")', 3), ("=SUM()", 0), ("=SUM(1+1,B5)", 4),
                                  ("=SUM(1,(1=1))", 2), ("=SUM(-1)", -1)]:
            with self.subTest(formula=formula):
                self.assertEqual(calc(formula, self.CELLS), expected)
        for formula in ('=SUM(1,"abc")', '=SUM(1,"")', '=SUM("")', '=SUM("x",1)'):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.SheetError) as raised:
                    calc(formula)
                self.assertNotIsInstance(raised.exception, vf.NotCatchable)

    def test_an_error_cell_in_a_range_propagates_and_iferror_catches_it(self):
        cells = {"C1": "=1/0", "C2": "1"}
        with self.assertRaises(vf.SheetError):
            calc("=SUM(C1:C2)", cells)
        self.assertEqual(calc("=IFERROR(SUM(C1:C2),-1)", cells), -1)

    def test_a_defect_inside_a_range_is_not_catchable(self):
        with self.assertRaises(vf.NotCatchable):
            calc("=IFERROR(SUM(C1:C2),-1)", {"C1": "=NOPE(1)", "C2": "1"})

    def test_a_reversed_range_sums_nothing(self):
        self.assertEqual(calc("=SUM(B5:B1)", self.CELLS), 0)


class MaxTests(unittest.TestCase):
    def test_the_largest_number_of_the_references_and_scalars(self):
        cells = {"B1": "1", "B2": "abc", "B4": "=1=1", "B5": "=2"}
        self.assertEqual(calc("=MAX(B1:B5)", cells), 2)
        self.assertEqual(calc("=MAX(1,5,3)"), 5)
        self.assertEqual(calc("=MAX(-3)"), -3)
        self.assertEqual(calc("=MAX(-3,-1)"), -1)
        self.assertEqual(calc("=MAX(B1:B5,-10)", cells), 2)
        self.assertEqual(calc("=MAX(B1:B5,10)", cells), 10)

    def test_negative_numbers_are_not_clamped_to_zero(self):
        self.assertEqual(calc("=MAX(F1:F2)", {"F1": "-5", "F2": "-2"}), -2)

    def test_nothing_numeric_is_zero(self):
        self.assertEqual(calc("=MAX(E1:E2)", {"E1": "a", "E2": "b"}), 0)
        self.assertEqual(calc("=MAX()"), 0)
        self.assertEqual(calc("=MAX(A9)"), 0)
        self.assertEqual(calc("=MAX(E1:E2)", {"E1": "=1=1", "E2": '="7"'}), 0)
        self.assertIsInstance(calc("=MAX()"), float)

    def test_a_non_numeric_scalar_is_strict_like_sum(self):
        for formula in ('=MAX("x")', '=MAX("")', '=MAX(1,"x")'):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.SheetError):
                    calc(formula)


class AverageIfTests(unittest.TestCase):
    CELLS = {"B1": "4", "B2": "-2", "B3": "6", "B4": "x"}

    def test_numeric_criteria(self):
        table = [('">0"', 5), ('">=4"', 5), ('"=4"', 4), ('"<0"', -2), ('"<=4"', 1), ('"4"', 4), ('"> 3"', 5),
                 ('">-3"', 8 / 3), ('">0.5"', 5), ('"<10"', 8 / 3), ("4", 4), ("-2", -2), ('"=-2"', -2)]
        for criterion, expected in table:
            with self.subTest(criterion=criterion):
                self.assertAlmostEqual(calc("=AVERAGEIF(B1:B4,%s)" % criterion, self.CELLS), expected)

    def test_the_comparison_operators_differ_at_the_boundary(self):
        cells = {"B1": "5"}
        self.assertEqual(calc('=AVERAGEIF(B1:B1,">=5")', cells), 5)
        self.assertEqual(calc('=AVERAGEIF(B1:B1,"<=5")', cells), 5)
        self.assertEqual(calc('=AVERAGEIF(B1:B1,"=5")', cells), 5)
        for criterion in ('">5"', '"<5"'):
            with self.assertRaises(vf.SheetError):
                calc("=AVERAGEIF(B1:B1,%s)" % criterion, cells)

    def test_a_criterion_taken_from_a_cell_or_built_by_concatenation(self):
        self.assertEqual(calc("=AVERAGEIF(B1:B4,C1)", dict(self.CELLS, C1='=">"&0')), 5)
        self.assertEqual(calc("=AVERAGEIF(B1:B4,C1)", dict(self.CELLS, C1="4")), 4)

    def test_text_and_empty_cells_never_match(self):
        with self.assertRaises(vf.SheetError) as raised:
            calc('=AVERAGEIF(B1:B3,"=0")', {"B1": "x", "B2": ""})              # B3 absent, B1 text, B2 empty text
        self.assertIn("#DIV/0!", str(raised.exception))
        self.assertEqual(calc('=AVERAGEIF(B1:B3,"<1")', {"B1": "x", "B2": "-1"}), -1)

    def test_nothing_matched_is_a_catchable_divide_by_zero(self):
        with self.assertRaises(vf.SheetError) as raised:
            calc('=AVERAGEIF(B1:B2,">100")', {"B1": "1", "B2": "2"})
        self.assertNotIsInstance(raised.exception, vf.NotCatchable)
        self.assertIn("#DIV/0!", str(raised.exception))
        self.assertEqual(calc('=IFERROR(AVERAGEIF(B1:B2,">100"),0)', {"B1": "1", "B2": "2"}), 0)

    def test_an_average_range_gives_the_values_and_skips_its_non_numbers(self):
        cells = {"B1": "1", "B2": "2", "B3": "3", "C1": "10", "C2": "x", "C3": "30"}
        self.assertEqual(calc('=AVERAGEIF(B1:B3,">0",C1:C3)', cells), 20)      # B2 matches but C2 is text
        self.assertEqual(calc('=AVERAGEIF(B1:B3,">1",C1:C3)', cells), 30)

    def test_a_shorter_average_range_skips_the_missing_cells(self):
        cells = {"B1": "1", "B2": "2", "B3": "3", "C1": "10", "C2": "20"}
        self.assertEqual(calc('=AVERAGEIF(B1:B3,">0",C1:C2)', cells), 15)

    def test_a_single_reference_is_a_range_of_one(self):
        self.assertEqual(calc('=AVERAGEIF(B1,">0")', {"B1": "7"}), 7)

    def test_an_unsupported_criterion_is_loud_even_inside_iferror(self):
        for criterion in ('"abc"', '"<>5"', '"*"', '""', '"="', '">"', '">=a"', '"5 "', '">>5"'):
            with self.subTest(criterion=criterion):
                with self.assertRaises(vf.NotCatchable):
                    calc("=AVERAGEIF(B1:B2,%s)" % criterion, {"B1": "1", "B2": "2"})
                with self.assertRaises(vf.NotCatchable):
                    calc("=IFERROR(AVERAGEIF(B1:B2,%s),0)" % criterion, {"B1": "1", "B2": "2"})

    def test_a_wrong_argument_shape_is_loud(self):
        for formula in ("=AVERAGEIF(B1:B2)", '=AVERAGEIF(B1:B2,">0",C1:C2,D1:D2)', '=AVERAGEIF(1,">0")',
                        '=AVERAGEIF(B1:B2,">0",5)', '=AVERAGEIF(B1:B2,">0",1+1)'):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.NotCatchable):
                    calc(formula, {"B1": "1"})


class TextFunctionTests(unittest.TestCase):
    def test_right(self):
        table = [('=RIGHT("abcdef",2)', "ef"), ('=RIGHT("abc")', "c"), ('=RIGHT("abc",10)', "abc"),
                 ('=RIGHT("abc",0)', ""), ("=RIGHT(2026,2)", "26"), ('=RIGHT("abc",2.9)', "bc"), ('=RIGHT("abc",A9)', ""),
                 ("=RIGHT(A9,2)", ""), ('=RIGHT("abc",3)', "abc"), ('=RIGHT("abc",1000000000000000000000)', "abc"),
                 ('=RIGHT("ab"&"cd",3)', "bcd")]
        for formula, expected in table:
            with self.subTest(formula=formula):
                self.assertEqual(calc(formula), expected)

    def test_right_with_a_negative_count_is_a_catchable_error(self):
        with self.assertRaises(vf.SheetError) as raised:
            calc('=RIGHT("abc",-1)')
        self.assertNotIsInstance(raised.exception, vf.NotCatchable)

    def test_int_floors(self):
        table = [("=INT(3.7)", 3), ("=INT(-3.2)", -4), ("=INT(5)", 5), ("=INT(-5)", -5), ("=INT(A9)", 0), ('=INT("")', 0),
                 ('=INT("7.9")', 7), ("=INT(0.999)", 0)]
        for formula, expected in table:
            with self.subTest(formula=formula):
                self.assertEqual(calc(formula), expected)
                self.assertIsInstance(calc(formula), float)
        with self.assertRaises(vf.SheetError):
            calc('=INT("x")')

    def test_substitute_replaces_every_occurrence_with_three_arguments_only(self):
        table = [('=SUBSTITUTE("a-b-c","-","+")', "a+b+c"), ('=SUBSTITUTE("abc","","x")', "abc"),
                 ("=SUBSTITUTE(2026,\"0\",\"o\")", "2o26"), ('=SUBSTITUTE("Aa","a","x")', "Ax"),
                 ('=SUBSTITUTE("aaa","aa","b")', "ba"), ('=SUBSTITUTE("abc","b","")', "ac"),
                 ('=SUBSTITUTE(A9,"a","b")', ""), ('=SUBSTITUTE("x","x",1)', "1")]
        for formula, expected in table:
            with self.subTest(formula=formula):
                self.assertEqual(calc(formula), expected)
        for formula in ('=SUBSTITUTE("a","b")', '=SUBSTITUTE("a","b","c",1)', "=SUBSTITUTE()"):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.NotCatchable):
                    calc(formula)

    def test_t_without_translations_is_the_identity(self):
        for translations in (None, {}):
            with self.subTest(translations=translations):
                self.assertEqual(calc('=_t("Total")', translations=translations), "Total")
                self.assertEqual(calc("=_t(5)", translations=translations), "5")
                self.assertEqual(calc("=_t(A9)", translations=translations), "")

    def test_t_with_translations_looks_up_the_exact_msgid(self):
        translations = {"Total": "Gesamt", "5": "fünf", "": "leer"}
        self.assertEqual(calc('=_t("Total")', translations=translations), "Gesamt")
        self.assertEqual(calc('=_t("Other")', translations=translations), "Other")
        self.assertEqual(calc("=_t(5)", translations=translations), "fünf")
        self.assertEqual(calc('=_t("total")', translations=translations), "total")
        self.assertEqual(calc('=_t("Total")&" "&_t("Other")', translations=translations), "Gesamt Other")
        self.assertEqual(calc('=_t("")', translations=translations), "leer")

    def test_function_names_are_case_sensitive_in_the_evaluator(self):
        # the guard upper-cases names, the evaluator does not: a lower-case or upper-case `_T` call is loud, not wrong
        for formula in ('=_T("x")', "=sum(1)", "=Sum(1)", "=iferror(1,2)"):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.UnknownFunction):
                    calc(formula)


class OdooListTests(unittest.TestCase):
    LISTS = {1: [{"f": 1.5, "g": "text", "n": None}, {"f": "x", "g": 2}], 2: [], 3: [{"f": 9}]}

    def test_it_reads_the_row_at_the_position_and_the_field(self):
        self.assertEqual(calc('=ODOO.LIST(1,1,"f")', lists=self.LISTS), 1.5)
        self.assertEqual(calc('=ODOO.LIST(1,2,"f")', lists=self.LISTS), "x")
        self.assertEqual(calc('=ODOO.LIST(1,1,"g")', lists=self.LISTS), "text")
        self.assertEqual(calc('=ODOO.LIST(3,1,"f")', lists=self.LISTS), 9)

    def test_a_none_value_is_an_empty_cell(self):
        self.assertEqual(calc('=ODOO.LIST(1,1,"n")+1', lists=self.LISTS), 1)
        self.assertEqual(calc('=ODOO.LIST(1,1,"n")&"x"', lists=self.LISTS), "x")

    def test_a_position_beyond_the_rows_or_below_one_is_an_empty_string(self):
        for position in (3, 4, 100, 0, -1, -100):
            with self.subTest(position=position):
                self.assertEqual(calc('=ODOO.LIST(1,%d,"f")' % position, lists=self.LISTS), "")
        self.assertEqual(calc('=ODOO.LIST(2,1,"f")', lists=self.LISTS), "")           # a list with no rows

    def test_the_last_position_is_inside_and_an_out_of_range_position_skips_the_field_check(self):
        self.assertEqual(calc('=ODOO.LIST(1,2,"g")', lists=self.LISTS), 2)
        self.assertEqual(calc('=ODOO.LIST(1,5,"no_such_field")', lists=self.LISTS), "")

    def test_a_position_given_as_a_cell_or_as_numeric_text_or_with_decimals(self):
        self.assertEqual(calc('=ODOO.LIST(1,B1,"f")', {"B1": "2"}, lists=self.LISTS), "x")
        self.assertEqual(calc('=ODOO.LIST(1,"2","f")', lists=self.LISTS), "x")
        self.assertEqual(calc('=ODOO.LIST(1,1.9,"f")', lists=self.LISTS), 1.5)
        self.assertEqual(calc('=ODOO.LIST(1,1+1,"f")', lists=self.LISTS), "x")

    def test_an_unprovided_list_is_not_catchable(self):
        for list_id in (7, 0, -1):
            with self.subTest(list_id=list_id):
                with self.assertRaises(vf.NotCatchable) as raised:
                    calc('=ODOO.LIST(%d,1,"f")' % list_id, lists=self.LISTS)
                self.assertIn("is not provided", str(raised.exception))
        with self.assertRaises(vf.NotCatchable):
            calc('=ODOO.LIST(1,1,"f")')                                                # no lists at all

    def test_a_field_missing_from_the_row_is_not_catchable(self):
        with self.assertRaises(vf.NotCatchable) as raised:
            calc('=ODOO.LIST(1,1,"missing")', lists=self.LISTS)
        self.assertIn("have no field 'missing'", str(raised.exception))
        with self.assertRaises(vf.NotCatchable):
            calc('=ODOO.LIST(1,2,"n")', lists=self.LISTS)                              # row 2 has no `n`

    def test_exactly_three_arguments(self):
        for formula in ("=ODOO.LIST()", "=ODOO.LIST(1)", "=ODOO.LIST(1,1)", '=ODOO.LIST(1,1,"f",1)'):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.NotCatchable) as raised:
                    calc(formula, lists=self.LISTS)
                self.assertIn("takes 3 arguments", str(raised.exception))

    def test_a_list_of_the_constructor_is_copied_not_shared(self):
        lists = {1: [{"f": 1}]}
        ev = vf.SheetEvaluator(dashboard({"A1": '=ODOO.LIST(1,1,"f")'}), lists)
        lists[2] = [{"f": 2}]
        self.assertNotIn(2, ev.lists)


class CellAndCacheTests(unittest.TestCase):
    def test_cell_kinds(self):
        ev = evaluator({"A1": "12", "A2": "12.5", "A3": "-3", "A4": "abc", "A5": "", "A6": "=A1+A2", "A7": "007",
                        "A8": "1e2"})
        self.assertEqual(ev.value("A1"), 12.0)
        self.assertEqual(ev.value("A2"), 12.5)
        self.assertEqual(ev.value("A3"), -3.0)
        self.assertEqual(ev.value("A4"), "abc")
        self.assertIsNone(ev.value("A5"))
        self.assertIsNone(ev.value("A99"))
        self.assertEqual(ev.value("A6"), 24.5)
        self.assertEqual(ev.value("A7"), 7.0)
        self.assertEqual(ev.value("A8"), 100.0)

    def test_a_cell_without_content_is_empty(self):
        data = {"sheets": [{"cells": {"A1": {"style": 1}, "A2": {}, "A3": {"content": ""}}}]}
        ev = vf.SheetEvaluator(data)
        self.assertEqual(ev.evaluate_all(), {"A1": None, "A2": None, "A3": None})

    def test_absolute_references_and_two_letter_columns(self):
        self.assertEqual(calc("=$A$1+A$1+$A1+AB10", {"A1": "1", "AB10": "10"}), 13)

    def test_references_are_upper_case_only(self):
        with self.assertRaises(vf.NotCatchable):
            calc("=a1+1", {"A1": "1"})

    def test_only_the_first_sheet_is_read(self):
        second = {"cells": {"A1": {"content": "99"}, "B1": {"content": "5"}}}
        ev = vf.SheetEvaluator(dashboard({"A1": "1"}, extra_sheets=[second]))
        self.assertEqual(ev.evaluate_all(), {"A1": 1.0})
        self.assertIsNone(ev.value("B1"))

    def test_a_cell_is_evaluated_once(self):
        class CountingRows(list):
            lengths = 0

            def __len__(self):                       # ODOO.LIST asks for the length once per evaluation
                CountingRows.lengths += 1
                return super().__len__()

        rows = CountingRows([{"f": 3}])
        ev = evaluator({"A1": '=ODOO.LIST(1,1,"f")', "B1": "=A1+A1+A1", "C1": "=B1+A1"}, lists={1: rows})
        self.assertEqual(ev.value("C1"), 12)
        self.assertEqual(CountingRows.lengths, 1)
        self.assertEqual(ev.cache["A1"], 3)

    def test_an_error_is_not_cached_and_raises_again(self):
        ev = evaluator({"A1": "=1/0"})
        for _ in range(2):
            with self.assertRaises(vf.SheetError):
                ev.value("A1")
        self.assertNotIn("A1", ev.cache)
        self.assertEqual(ev.in_progress, set())

    def test_circular_references_are_sheet_errors_and_leave_the_evaluator_usable(self):
        cycles = {"self": {"A1": "=A1"}, "two": {"A1": "=B1", "B1": "=A1"}, "three": {"A1": "=B1+1", "B1": "=C1", "C1": "=A1"}}
        for label, cells in cycles.items():
            with self.subTest(cycle=label):
                ev = evaluator(dict(cells, Z1="5"))
                for _ in range(2):
                    with self.assertRaises(vf.SheetError) as raised:
                        ev.value("A1")
                    self.assertNotIsInstance(raised.exception, vf.NotCatchable)
                    self.assertIn("circular reference", str(raised.exception))
                self.assertEqual(ev.in_progress, set())
                self.assertEqual(ev.value("Z1"), 5)

    def test_a_chain_of_cells_within_the_recursion_limit_evaluates(self):
        cells = {"A1": "1"}
        cells.update({"A%d" % i: "=A%d+1" % (i - 1) for i in range(2, 51)})
        self.assertEqual(evaluator(cells).value("A50"), 50)

    def test_evaluate_all_returns_every_cell_of_the_sheet(self):
        ev = evaluator({"A1": "2", "A2": "=A1*3", "A3": '="t"', "A4": ""})
        self.assertEqual(ev.evaluate_all(), {"A1": 2.0, "A2": 6.0, "A3": "t", "A4": None})

    def test_evaluate_all_stops_at_the_first_error(self):
        ev = evaluator({"A1": "2", "A2": "=1/0"})
        with self.assertRaises(vf.SheetError):
            ev.evaluate_all()

    def test_a_range_outside_sum_max_averageif_is_an_error_value(self):
        with self.assertRaises(vf.SheetError) as raised:
            calc("=A1:B2")
        self.assertNotIsInstance(raised.exception, vf.NotCatchable)
        with self.assertRaises(vf.SheetError):
            calc("=INT(A1:B2)")

    def test_a_cell_that_is_only_an_equals_sign_is_a_compile_error(self):
        with self.assertRaises(vf.NotCatchable):
            calc("=")

    def test_parse_formula_is_cached_and_refuses_non_formulas(self):
        self.assertIs(vf.parse_formula("=1+2"), vf.parse_formula("=1+2"))
        for text in ("1+2", "x1", "abc", ""):
            with self.subTest(text=text), self.assertRaises(vf.NotCatchable):
                vf.parse_formula(text)

    def test_syntax_errors_are_compile_errors(self):
        for formula in ("=1+", "=)", "=(1", "=1 2", "=SUM(1)SUM(2)", "=SUM(1,)", "=MAX(,)", "=1..2", "=.5", "=#", "=A1:B2:C3",
                        '="a"+', "=SUM(", "=A1:5", "=AAA1", "=SUM(1 2)", "=(1 2)", "=SUM(1;2)",
                        "=SUM(1 2", "=(1 2", "=IF(1,2 3", "=(1,"):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.NotCatchable):
                    calc(formula)

    def test_wrong_arity_or_a_bad_position_never_produces_a_value(self):
        # not clean errors (a raw IndexError / ValueError / OverflowError escapes, see the report) but never a value
        for formula in ("=IF()", "=INT()", "=RIGHT()", "=_t()", "=IFERROR()", "=IF(1)", '=ODOO.LIST(1,"a","f")',
                        "=ODOO.LIST(1,A9,\"f\")", '=ODOO.LIST("x",1,"f")', '=INT("1e999")', '=RIGHT("abc","1e999")'):
            with self.subTest(formula=formula):
                with self.assertRaises(Exception):
                    calc(formula, lists={1: [{"f": 1}]})
                with self.assertRaises(Exception):
                    calc("=IFERROR(%s,0)" % formula[1:], lists={1: [{"f": 1}]})


class GeneratedShapeTests(unittest.TestCase):
    """The sheets the binder will produce: a hidden helper row, a display cell and a ratio."""

    CELLS = {"A1": '=IFERROR(ODOO.LIST(1,1,"qty")+0,0)', "A2": '=IFERROR(ODOO.LIST(1,2,"qty")+0,0)',
             "A3": '=IFERROR(ODOO.LIST(1,3,"qty")+0,0)', "B20": "=SUM(A1:A3)", "B21": "=MAX(A1:A3)",
             "C20": '=IF(B20=0,"-",B20)', "C21": '=IF(B21>0,B20/B21,"N/A")'}

    def test_helper_row_display_cell_and_ratio_with_two_rows(self):
        got = vf.SheetEvaluator(dashboard(self.CELLS), {1: [{"qty": 4}, {"qty": 6}]}).evaluate_all()
        self.assertEqual((got["A1"], got["A2"], got["A3"]), (4, 6, 0))
        self.assertEqual((got["B20"], got["B21"]), (10, 6))
        self.assertEqual(got["C20"], 10)
        self.assertAlmostEqual(got["C21"], 10 / 6)

    def test_no_rows_gives_zeros_dash_and_not_available(self):
        got = vf.SheetEvaluator(dashboard(self.CELLS), {1: []}).evaluate_all()
        self.assertEqual((got["A1"], got["A2"], got["A3"], got["B20"], got["B21"]), (0, 0, 0, 0, 0))
        self.assertEqual((got["C20"], got["C21"]), ("-", "N/A"))

    def test_a_text_value_in_a_row_becomes_zero_through_the_helper(self):
        got = vf.SheetEvaluator(dashboard(self.CELLS), {1: [{"qty": "n/a"}, {"qty": 5}]}).evaluate_all()
        self.assertEqual((got["A1"], got["A2"], got["B20"], got["C20"]), (0, 5, 5, 5))

    def test_a_none_value_in_a_row_becomes_zero_through_the_helper(self):
        got = vf.SheetEvaluator(dashboard(self.CELLS), {1: [{"qty": None}, {"qty": 5}]}).evaluate_all()
        self.assertEqual((got["A1"], got["B20"]), (0, 5))

    def test_a_row_without_the_field_is_a_defect_the_helper_does_not_hide(self):
        with self.assertRaises(vf.NotCatchable):
            vf.SheetEvaluator(dashboard(self.CELLS), {1: [{"other": 4}]}).evaluate_all()

    def test_a_list_nobody_provided_is_a_defect_the_helper_does_not_hide(self):
        with self.assertRaises(vf.NotCatchable):
            vf.SheetEvaluator(dashboard(self.CELLS)).evaluate_all()

    def test_a_year_label_with_translation_right_and_substitute(self):
        cells = {"B1": '=ODOO.LIST(1,1,"year")', "A1": '=IF(B1="","",RIGHT(B1,2)&_t("yr"))',
                 "A2": '=SUBSTITUTE(_t("Sales of YEAR"),"YEAR",B1&"")'}
        translations = {"yr": "y.", "Sales of YEAR": "Sales YEAR!"}
        got = vf.SheetEvaluator(dashboard(cells), {1: [{"year": 2026}]}, translations).evaluate_all()
        self.assertEqual((got["A1"], got["A2"]), ("26y.", "Sales 2026!"))
        empty = vf.SheetEvaluator(dashboard(cells), {1: []}, translations)
        self.assertEqual(empty.value("A1"), "")
        self.assertEqual(empty.value("A2"), "Sales !")

    def test_an_average_helper_with_a_fallback(self):
        cells = {"C1": "0", "D1": "4", "E1": "8", "F1": "0", "G1": "0", "H1": "0",
                 "A1": '=IFERROR(AVERAGEIF(C1:E1,">0"),0)', "A2": '=IFERROR(AVERAGEIF(F1:H1,">0"),0)'}
        got = vf.SheetEvaluator(dashboard(cells)).evaluate_all()
        self.assertEqual((got["A1"], got["A2"]), (6, 0))

    def test_a_percentage_label(self):
        cells = {"B1": "3", "B2": "8", "A1": '=IF(B2>0,INT(B1/B2*100)&"%","")', "A2": '=IF(B3>0,INT(B1/B3*100)&"%","")'}
        got = vf.SheetEvaluator(dashboard(cells)).evaluate_all()
        self.assertEqual((got["A1"], got["A2"]), ("37%", ""))


# ---------------------------------------------------------------- 6. range safety

class RangeSafetyTests(unittest.TestCase):
    @staticmethod
    def refs(formula):
        return vf.referenced_cells(vf.parse_formula(formula))

    @staticmethod
    def never_build():
        """A stand-in for `_col_letters` that fails on the first call: a range that is refused must not build a cell."""
        return mock.patch.object(vf, "_col_letters", side_effect=AssertionError("a refused range was expanded"))

    def test_a_range_expands_column_by_column_in_order(self):
        self.assertEqual(self.refs("=A1:B3"), [("A", 1), ("A", 2), ("A", 3), ("B", 1), ("B", 2), ("B", 3)])

    def test_absolute_anchors_and_two_letter_columns_expand(self):
        self.assertEqual(self.refs("=$Z$1:$AB$1"), [("Z", 1), ("AA", 1), ("AB", 1)])
        self.assertEqual(self.refs("=A$2:A3"), [("A", 2), ("A", 3)])

    def test_a_single_cell_range_and_single_references(self):
        self.assertEqual(self.refs("=C5:C5"), [("C", 5)])
        self.assertEqual(self.refs("=C5"), [("C", 5)])

    def test_non_reference_nodes_read_nothing(self):
        self.assertEqual(self.refs("=1+2"), [])
        self.assertEqual(self.refs('="a"'), [])
        self.assertEqual(self.refs("=SUM()"), [])

    def test_references_are_collected_through_negation_binary_operators_and_calls_left_to_right(self):
        self.assertEqual(self.refs("=-A1"), [("A", 1)])
        self.assertEqual(self.refs("=A1+B2*C3"), [("A", 1), ("B", 2), ("C", 3)])
        self.assertEqual(self.refs("=SUM(A1,B2:B3)+IF(C4,D5,1)"),
                         [("A", 1), ("B", 2), ("B", 3), ("C", 4), ("D", 5)])
        self.assertEqual(self.refs("=IF(A1=B2,C3&D4,-E5)"), [("A", 1), ("B", 2), ("C", 3), ("D", 4), ("E", 5)])

    def test_a_range_of_exactly_the_cap_is_allowed_and_one_more_is_refused(self):
        self.assertEqual(vf.MAX_RANGE_CELLS, 100000)
        allowed = self.refs("=A1:A100000")
        self.assertEqual(len(allowed), 100000)
        self.assertEqual((allowed[0], allowed[-1]), (("A", 1), ("A", 100000)))
        with self.never_build() as letters:
            with self.assertRaises(vf.NotCatchable) as raised:
                self.refs("=A1:A100001")
            self.assertEqual(letters.call_count, 0)
        self.assertIn("1 x 100001", str(raised.exception))

    def test_the_cap_is_on_the_number_of_cells_not_on_a_side(self):
        with mock.patch.object(vf, "MAX_RANGE_CELLS", 12):
            self.assertEqual(len(self.refs("=A1:C4")), 12)               # 3 x 4 = the cap
            self.assertEqual(len(self.refs("=A1:L1")), 12)               # 12 x 1
            self.assertEqual(len(self.refs("=A1:A12")), 12)              # 1 x 12
            for formula in ("=A1:D4", "=A1:C5", "=A1:M1", "=A1:A13"):    # 16, 15, 13, 13
                with self.subTest(formula=formula), self.never_build() as letters:
                    with self.assertRaises(vf.NotCatchable):
                        self.refs(formula)
                    self.assertEqual(letters.call_count, 0)

    def test_a_hostile_range_is_refused_before_any_cell_is_built(self):
        for formula in ("=A1:ZZ99999999", "=AA1:AA99999999", "=A1:ZZ1000000", "=A1:ZZ143", "=B1:B100001"):
            with self.subTest(formula=formula), self.never_build() as letters:
                with self.assertRaises(vf.NotCatchable):
                    self.refs(formula)
                self.assertEqual(letters.call_count, 0)

    def test_a_hostile_range_in_a_sheet_is_not_hidden_by_iferror_and_builds_nothing(self):
        for formula in ("=SUM(A1:ZZ99999999)", "=IFERROR(SUM(A1:ZZ99999999),0)", "=MAX(A1:ZZ99999999)",
                        '=IFERROR(AVERAGEIF(A1:ZZ99999999,">0"),0)'):
            with self.subTest(formula=formula), self.never_build() as letters:
                with self.assertRaises(vf.NotCatchable):
                    calc(formula, {"B1": "1", "B2": "2"})
                self.assertEqual(letters.call_count, 0)

    def test_the_average_range_of_averageif_is_capped_too(self):
        # the criteria range is expanded first, so use a small cap instead of the stand-in
        with mock.patch.object(vf, "MAX_RANGE_CELLS", 5):
            self.assertEqual(calc('=AVERAGEIF(B1:B2,">0",C1:C5)', {"B1": "1", "B2": "2", "C1": "4", "C2": "6"}), 5)
            with self.assertRaises(vf.NotCatchable):
                calc('=IFERROR(AVERAGEIF(B1:B2,">0",C1:C6),0)', {"B1": "1", "B2": "2"})

    def test_a_sum_over_a_range_of_exactly_the_cap_works(self):
        got = calc("=SUM(A1:A100000)", {"A1": "2", "A100000": "3", "A5000": "x"})
        self.assertEqual(got, 5)

    def test_reversed_ranges_are_empty_without_an_error_and_without_building_cells(self):
        for formula in ("=B3:A1", "=A3:A1", "=B1:A1", "=A5:B1", "=ZZ99999999:A1", "=A99999999:ZZ1"):
            with self.subTest(formula=formula), self.never_build() as letters:
                self.assertEqual(self.refs(formula), [])
                self.assertEqual(letters.call_count, 0)
        self.assertEqual(calc("=SUM(B3:A1)", {"A1": "1", "B3": "2"}), 0)
        self.assertEqual(calc("=MAX(B3:A1)", {"A1": "1", "B3": "2"}), 0)

    def test_a_deeply_nested_formula_is_a_documented_recursion_error_not_a_hang_or_a_value(self):
        # FINDING (documented, not asserted as a defect): the recursive-descent parser raises RecursionError, not a
        # SheetError, for nesting that is thousands deep; it is raised at once and nothing is cached.
        formula = "=" + "(" * 5000 + "1" + ")" * 5000
        start = time.perf_counter()
        with self.assertRaises((vf.SheetError, RecursionError)):
            vf.parse_formula(formula)
        self.assertLess(time.perf_counter() - start, 5)
        ev = evaluator({"A1": formula, "B1": "7"})
        with self.assertRaises((vf.SheetError, RecursionError)):
            ev.value("A1")
        self.assertEqual(ev.in_progress, set())
        self.assertEqual(ev.value("B1"), 7)

    def test_moderate_nesting_still_parses(self):
        self.assertEqual(calc("=" + "(" * 100 + "1" + ")" * 100), 1)
        self.assertEqual(calc("=" + "-" * 100 + "1"), 1)
        self.assertEqual(calc("=" + "IF(1," * 30 + "1" + ",0)" * 30), 1)

    def test_a_long_formula_of_garbage_is_a_clean_compile_error(self):
        text = "=" + "#" * 100000
        start = time.perf_counter()
        with self.assertRaises(vf.NotCatchable):
            vf.parse_formula(text)
        self.assertLess(time.perf_counter() - start, 5)

    def test_a_long_string_literal_formula_is_evaluated(self):
        value = calc('="%s"' % ("x" * 100000))
        self.assertEqual(len(value), 100000)

    def test_a_long_flat_sum_chain_parses_but_is_too_deep_to_evaluate(self):
        # 25,000 terms = 50 KB: the parser builds a left-leaning tree (fine), the evaluator recurses on it
        formula = "=" + "1+" * 25000 + "1"
        tree = vf.parse_formula(formula)
        self.assertEqual(tree[0], "bin")
        with self.assertRaises((vf.SheetError, RecursionError)):
            calc(formula)


class OddDocumentShapeTests(unittest.TestCase):
    """A document of an unexpected shape never ends in a raw exception of the guard (the client refuses it anyway)."""

    def test_odd_shapes_give_a_list_not_a_traceback(self):
        for document in ({"sheets": None}, {"sheets": 5}, {"sheets": [None, 3, "x"]},
                         {"sheets": [{"conditionalFormats": 5}]}, {"sheets": [{"conditionalFormats": True}]},
                         {"sheets": [{"dataValidationRules": "x"}]}, {"sheets": [{"conditionalFormats": [None, 7]}]},
                         {"sheets": [{"cells": None}]}, {"sheets": [{"cells": {"A1": None}}]}):
            with self.subTest(document=document):
                self.assertEqual(vf.unknown_function_problems(document, set()), [])

    def test_a_name_may_start_with_a_digit_or_a_dot_like_in_the_client(self):
        for formula in ("=1SUM(1)", "=.SUM(1)", "=1_EVIL(1)"):
            with self.subTest(formula=formula):
                document = {"sheets": [{"cells": {"A1": {"content": formula}}}]}
                self.assertEqual(len(vf.unknown_function_problems(document, {"SUM"})), 1)


if __name__ == "__main__":
    unittest.main()
