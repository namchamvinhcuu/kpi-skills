"""`validate_formulas.SheetEvaluator` extended for step 8b: `pivots=` plus the functions `ODOO.PIVOT`,
`ODOO.PIVOT.HEADER` and `YEAR` (and the old behaviour unchanged). Synthetic inputs only (formula_fixtures.py).

The evaluator models what the client does for a pivot grouped by month (`<date field>:month`): a month column is read by
its `MM/yyyy` key, a month the pivot has no column for shows an empty cell, and the header of the first month column is
the date serial of the first day of that month (0 when the pivot has no column at all: measured in the browser, the
client then shows the date 0 = 1899-12-30).
"""
import datetime
import unittest

from formula_fixtures import dashboard, pivot
from helpers import REPO  # noqa: F401

import validate_formulas as vf  # noqa: E402

SERIAL_2025_12_01 = 45992                      # Excel / Odoo serial of 2025-12-01 (2026-01-01 is 46023)
SERIAL_2026_01_01 = 46023
SERIAL_2026_03_01 = 46082
SERIAL_2025_10_01 = 45931

PIVOTS = {
    1: pivot({"03/2026": {"__count": 4.0, "qty": 40.0}, "10/2026": {"__count": 7.0, "qty": 70.0},
              "12/2025": {"__count": 2.0}}, measures=("__count", "qty")),
    2: pivot({}),
    3: pivot({"10/2026": 5, "03/2026": 6, "10/2025": 9}),
}


def calc(formula, cells=None, pivots=PIVOTS, lists=None, ref="Z1"):
    all_cells = {ref: formula}
    all_cells.update(cells or {})
    return vf.SheetEvaluator(dashboard(all_cells), lists, None, pivots).value(ref)


class ExcelEpochTests(unittest.TestCase):
    def test_the_constant_is_the_spreadsheet_epoch(self):
        self.assertEqual(vf.EXCEL_EPOCH, datetime.date(1899, 12, 30))

    def test_the_serials_used_by_the_tests_are_right(self):
        for day, serial in ((datetime.date(2025, 12, 1), SERIAL_2025_12_01),
                            (datetime.date(2026, 1, 1), SERIAL_2026_01_01),
                            (datetime.date(2026, 3, 1), SERIAL_2026_03_01),
                            (datetime.date(2025, 10, 1), SERIAL_2025_10_01)):
            self.assertEqual((day - datetime.date(1899, 12, 30)).days, serial)


class OdooPivotTests(unittest.TestCase):
    def test_it_reads_the_month_column_and_the_measure(self):
        self.assertEqual(calc('=ODOO.PIVOT(1,"qty","check_date:month","03/2026")'), 40.0)
        self.assertEqual(calc('=ODOO.PIVOT(1,"__count","check_date:month","10/2026")'), 7.0)
        self.assertEqual(calc('=ODOO.PIVOT(3,"__count","day:month","10/2025")'), 9.0)

    def test_the_date_field_name_is_not_checked_only_the_month_grouping(self):
        self.assertEqual(calc('=ODOO.PIVOT(1,"qty","anything:month","03/2026")'), 40.0)
        self.assertEqual(calc('=ODOO.PIVOT(1,"qty",":month","03/2026")'), 40.0)

    def test_the_month_key_as_text_and_as_the_text_the_binder_builds_with_a_float_year(self):
        cells = {"A1": "=2026+0", "A2": "=YEAR(46082)", "A3": "=2025+1"}
        self.assertEqual(calc('=ODOO.PIVOT(1,"qty","c:month","03/"&A1)', cells), 40.0)
        self.assertEqual(calc('=ODOO.PIVOT(1,"qty","c:month","10/"&A2)', cells), 70.0)
        self.assertEqual(calc('=ODOO.PIVOT(1,"qty","c:month","12/"&(A3-1))', cells), 0.0)       # no qty in 12/2025
        self.assertEqual(calc('=ODOO.PIVOT(1,"__count","c:month","12/"&(A3-1))', cells), 2.0)

    def test_the_previous_year_key_is_the_year_minus_one(self):
        cells = {"A1": "=2026+0"}
        self.assertEqual(calc('=ODOO.PIVOT(3,"__count","c:month","10/"&(A1-1))', cells), 9.0)
        self.assertEqual(calc('=ODOO.PIVOT(3,"__count","c:month","10/"&A1)', cells), 5.0)

    def test_a_month_without_a_column_is_an_empty_string_and_adds_as_zero(self):
        self.assertEqual(calc('=ODOO.PIVOT(1,"qty","c:month","05/2026")'), "")
        self.assertEqual(calc('=ODOO.PIVOT(1,"qty","c:month","05/2026")+0'), 0.0)
        self.assertEqual(calc('=IFERROR(ODOO.PIVOT(1,"qty","c:month","05/2026")+0,9)'), 0.0)
        self.assertEqual(calc('=ODOO.PIVOT(2,"__count","c:month","03/2026")'), "")             # an empty pivot

    def test_the_key_must_match_exactly(self):
        for key in ('"3/2026"', '"03/26"', '"03-2026"', '" 03/2026"', '"03/2026 "', '"2026/03"', "3", '"March"', '""'):
            with self.subTest(key=key):
                self.assertEqual(calc('=ODOO.PIVOT(1,"qty","c:month",%s)' % key), "")

    def test_a_month_that_has_a_column_but_not_the_value_of_a_measure_is_zero(self):
        self.assertEqual(calc('=ODOO.PIVOT(1,"qty","c:month","12/2025")'), 0.0)         # "12/2025" holds __count only
        self.assertIsInstance(calc('=ODOO.PIVOT(1,"qty","c:month","12/2025")'), float)

    def test_a_measure_the_pivot_does_not_hold_is_not_catchable_and_not_hidden_by_iferror(self):
        for formula in ('=ODOO.PIVOT(1,"ng_qty","c:month","03/2026")', '=ODOO.PIVOT(1,"","c:month","03/2026")',
                        '=IFERROR(ODOO.PIVOT(1,"ng_qty","c:month","03/2026")+0,0)',
                        '=IFERROR(ODOO.PIVOT(3,"qty","c:month","03/2026"),5)'):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.NotCatchable) as raised:
                    calc(formula)
                self.assertIn("holds no measure", str(raised.exception))

    def test_a_measure_missing_is_reported_even_when_the_month_has_no_column(self):
        with self.assertRaises(vf.NotCatchable):
            calc('=ODOO.PIVOT(1,"ng_qty","c:month","05/2026")')

    def test_a_pivot_nobody_provided_is_not_catchable_and_not_hidden_by_iferror(self):
        for formula in ('=ODOO.PIVOT(9,"qty","c:month","03/2026")', '=ODOO.PIVOT(0,"qty","c:month","03/2026")',
                        '=IFERROR(ODOO.PIVOT(9,"qty","c:month","03/2026")+0,0)'):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.NotCatchable) as raised:
                    calc(formula)
                self.assertIn("is not provided", str(raised.exception))
        with self.assertRaises(vf.NotCatchable):
            calc('=ODOO.PIVOT(1,"qty","c:month","03/2026")', pivots=None)
        with self.assertRaises(vf.NotCatchable):
            calc('=ODOO.PIVOT(1,"qty","c:month","03/2026")', pivots={})

    def test_the_pivot_id_may_be_a_number_text_or_a_formula(self):
        self.assertEqual(calc('=ODOO.PIVOT("1","qty","c:month","03/2026")'), 40.0)
        self.assertEqual(calc('=ODOO.PIVOT(A1,"qty","c:month","03/2026")', {"A1": "=0+1"}), 40.0)
        self.assertEqual(calc('=ODOO.PIVOT(1.0,"qty","c:month","03/2026")'), 40.0)

    def test_only_a_month_grouping_is_modelled(self):
        for group in ('"check_date:year"', '"check_date"', '"check_date:month:day"', '"month"', '"check_date:Month"',
                      '""', "5"):
            with self.subTest(group=group):
                with self.assertRaises(vf.NotCatchable) as raised:
                    calc('=ODOO.PIVOT(1,"qty",%s,"03/2026")' % group)
                self.assertIn("grouped by month only", str(raised.exception))
        with self.assertRaises(vf.NotCatchable):
            calc('=IFERROR(ODOO.PIVOT(1,"qty","check_date:year","03/2026")+0,0)')

    def test_exactly_four_arguments(self):
        for formula in ("=ODOO.PIVOT()", "=ODOO.PIVOT(1)", '=ODOO.PIVOT(1,"qty")', '=ODOO.PIVOT(1,"qty","c:month")',
                        '=ODOO.PIVOT(1,"qty","c:month","03/2026",1)'):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.NotCatchable) as raised:
                    calc(formula)
                self.assertIn("takes 4 arguments", str(raised.exception))

    def test_an_error_inside_an_argument_propagates(self):
        with self.assertRaises(vf.SheetError):
            calc('=ODOO.PIVOT(1,"qty","c:month","03/"&"x"+1)')
        self.assertEqual(calc('=IFERROR(ODOO.PIVOT(1,"qty","c:month","03/"&("x"+1)),"e")'), "e")

    def test_the_pivots_of_the_constructor_are_copied_not_shared(self):
        pivots = {1: pivot({"01/2026": 1})}
        ev = vf.SheetEvaluator(dashboard({"A1": '=ODOO.PIVOT(1,"__count","c:month","01/2026")'}), None, None, pivots)
        pivots[2] = pivot({})
        self.assertNotIn(2, ev.pivots)
        self.assertEqual(ev.value("A1"), 1.0)

    def test_the_pivots_argument_is_keyword_compatible_and_optional(self):
        ev = vf.SheetEvaluator(dashboard({"A1": '=ODOO.PIVOT(1,"__count","c:month","01/2026")'}),
                               pivots={1: pivot({"01/2026": 3})})
        self.assertEqual(ev.value("A1"), 3.0)
        self.assertEqual(vf.SheetEvaluator(dashboard({"A1": "=1+1"})).value("A1"), 2.0)
        self.assertEqual(vf.SheetEvaluator(dashboard({"A1": "=1+1"})).pivots, {})

    def test_a_pivot_read_in_a_sum_and_in_if(self):
        cells = {"A1": '=ODOO.PIVOT(1,"qty","c:month","03/2026")', "A2": '=ODOO.PIVOT(1,"qty","c:month","10/2026")',
                 "A3": '=ODOO.PIVOT(1,"qty","c:month","05/2026")+0'}
        self.assertEqual(calc("=SUM(A1:A3)", cells), 110.0)
        self.assertEqual(calc('=IF(A3=0,"-",A3)', cells), "-")
        self.assertEqual(calc('=IF(A1=0,"-",A1)', cells), 40.0)


class OdooPivotHeaderTests(unittest.TestCase):
    def test_the_first_month_is_the_earliest_by_year_then_month(self):
        self.assertIsInstance(calc('=ODOO.PIVOT.HEADER(1,"#check_date:month",1)'), float)
        self.assertEqual(calc('=ODOO.PIVOT.HEADER(1,"#check_date:month",1)'), float(SERIAL_2025_12_01))
        self.assertEqual(calc('=ODOO.PIVOT.HEADER(3,"#check_date:month",1)'), float(SERIAL_2025_10_01))

    def test_it_is_not_the_first_key_of_the_dict_nor_the_lexical_minimum(self):
        # dict order 10/2026, 03/2026, 10/2025: the lexical minimum is 03/2026, the earliest is 10/2025
        self.assertEqual(list(PIVOTS[3]["months"]), ["10/2026", "03/2026", "10/2025"])
        self.assertEqual(calc('=ODOO.PIVOT.HEADER(3,"#c:month",1)'), float(SERIAL_2025_10_01))
        for order in (["03/2026", "10/2026", "10/2025"], ["10/2025", "03/2026", "10/2026"],
                      ["10/2026", "10/2025", "03/2026"]):
            with self.subTest(order=order):
                shuffled = {3: pivot({key: 1 for key in order})}
                self.assertEqual(calc('=ODOO.PIVOT.HEADER(3,"#c:month",1)', pivots=shuffled), float(SERIAL_2025_10_01))

    def test_one_month_pivot(self):
        self.assertEqual(calc('=ODOO.PIVOT.HEADER(1,"#c:month",1)', pivots={1: pivot({"03/2026": 1})}),
                         float(SERIAL_2026_03_01))
        self.assertEqual(calc('=ODOO.PIVOT.HEADER(1,"#c:month",1)', pivots={1: pivot({"01/2026": 1})}),
                         float(SERIAL_2026_01_01))

    def test_the_hash_is_optional(self):
        self.assertEqual(calc('=ODOO.PIVOT.HEADER(1,"c:month",1)'), float(SERIAL_2025_12_01))

    def test_a_pivot_without_a_month_column_gives_the_date_zero(self):
        value = calc('=ODOO.PIVOT.HEADER(2,"#c:month",1)')
        self.assertEqual(value, 0.0)
        self.assertIsInstance(value, float)

    def test_only_the_first_position_is_modelled(self):
        for position in ("0", "2", "3", "-1", '"1"', '"x"', "1.5"):
            with self.subTest(position=position):
                with self.assertRaises(vf.NotCatchable) as raised:
                    calc('=ODOO.PIVOT.HEADER(1,"#c:month",%s)' % position)
                self.assertIn("FIRST", str(raised.exception))
        self.assertEqual(calc('=ODOO.PIVOT.HEADER(1,"#c:month",1.0)'), float(SERIAL_2025_12_01))

    def test_a_pivot_nobody_provided_and_a_non_month_group_are_not_catchable(self):
        for formula in ('=ODOO.PIVOT.HEADER(9,"#c:month",1)', '=IFERROR(ODOO.PIVOT.HEADER(9,"#c:month",1),0)',
                        '=ODOO.PIVOT.HEADER(1,"#c:year",1)', '=ODOO.PIVOT.HEADER(1,"#c",1)',
                        '=IFERROR(YEAR(ODOO.PIVOT.HEADER(1,"#c:week",1)),0)'):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.NotCatchable):
                    calc(formula)

    def test_exactly_three_arguments(self):
        for formula in ("=ODOO.PIVOT.HEADER()", "=ODOO.PIVOT.HEADER(1)", '=ODOO.PIVOT.HEADER(1,"#c:month")',
                        '=ODOO.PIVOT.HEADER(1,"#c:month",1,1)'):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.NotCatchable) as raised:
                    calc(formula)
                self.assertIn("takes 3 arguments", str(raised.exception))

    def test_the_header_does_not_need_the_pivot_to_hold_any_measure(self):
        self.assertEqual(calc('=ODOO.PIVOT.HEADER(1,"#c:month",1)', pivots={1: {"measures": [], "months": {
            "03/2026": {}}}}), float(SERIAL_2026_03_01))


class YearTests(unittest.TestCase):
    def test_serial_zero_is_1899(self):
        value = calc("=YEAR(0)")
        self.assertEqual(value, 1899.0)
        self.assertIsInstance(value, float)

    def test_known_serials(self):
        for serial, year in ((1, 1899), (2, 1900), (SERIAL_2025_12_01, 2025), (46022, 2025), (SERIAL_2026_01_01, 2026),
                             (SERIAL_2026_03_01, 2026), (73050, 2099), (36526, 2000), (36525, 1999)):
            with self.subTest(serial=serial):
                self.assertEqual(calc("=YEAR(%d)" % serial), float(year))

    def test_negative_serials_go_before_the_epoch(self):
        self.assertEqual(calc("=YEAR(-1)"), 1899.0)                   # 1899-12-29
        self.assertEqual(calc("=YEAR(-30)"), 1899.0)                  # 1899-11-30
        self.assertEqual(calc("=YEAR(-363)"), 1899.0)                 # 1899-01-01
        self.assertEqual(calc("=YEAR(-364)"), 1898.0)                 # 1898-12-31
        self.assertEqual(calc("=YEAR(-365)"), 1898.0)                 # 1898-12-30
        self.assertEqual(calc("=YEAR(-1000)"), 1897.0)

    def test_the_serial_is_truncated_to_a_whole_day(self):
        self.assertEqual(calc("=YEAR(46022.99)"), 2025.0)
        self.assertEqual(calc("=YEAR(46023.01)"), 2026.0)
        self.assertEqual(calc("=YEAR(0.5)"), 1899.0)

    def test_an_empty_cell_and_empty_text_count_as_zero_and_other_text_is_an_error(self):
        self.assertEqual(calc("=YEAR(B1)"), 1899.0)
        self.assertEqual(calc('=YEAR("")'), 1899.0)
        with self.assertRaises(vf.SheetError):
            calc('=YEAR("abc")')
        self.assertEqual(calc('=IFERROR(YEAR("abc"),0)'), 0.0)

    def test_a_numeric_text_converts(self):
        self.assertEqual(calc('=YEAR("46023")'), 2026.0)

    def test_exactly_one_argument(self):
        for formula in ("=YEAR()", "=YEAR(1,2)"):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.NotCatchable) as raised:
                    calc(formula)
                self.assertIn("takes 1 argument", str(raised.exception))

    def test_the_year_of_the_first_month_header_is_what_the_binder_reads(self):
        formula = '=IFERROR(YEAR(ODOO.PIVOT.HEADER(1,"#c:month",1)),0)'
        self.assertEqual(calc(formula), 2025.0)
        self.assertEqual(calc(formula, pivots={1: pivot({"06/2027": 1, "01/2028": 1})}), 2027.0)
        self.assertEqual(calc(formula, pivots={1: pivot({})}), 1899.0)                 # a year with no data

    def test_a_missing_pivot_under_the_year_formula_is_not_hidden_by_iferror(self):
        with self.assertRaises(vf.NotCatchable):
            calc('=IFERROR(YEAR(ODOO.PIVOT.HEADER(9,"#c:month",1)),0)')

    def test_the_year_text_built_from_the_year_cell_matches_the_month_keys(self):
        cells = {"A1": '=IFERROR(YEAR(ODOO.PIVOT.HEADER(1,"#c:month",1)),0)'}
        self.assertEqual(calc('=ODOO.PIVOT(1,"__count","c:month","12/"&A1)', cells), 2.0)
        self.assertEqual(calc('=ODOO.PIVOT(1,"__count","c:month","03/"&A1)', cells), "")         # 03/2025 is absent


class OldBehaviourUnchangedTests(unittest.TestCase):
    LISTS = {1: [{"f": 1.5}]}

    def test_the_list_and_translation_arguments_keep_their_positions(self):
        ev = vf.SheetEvaluator(dashboard({"A1": '=ODOO.LIST(1,1,"f")', "A2": '=_t("yes")'}), self.LISTS, {"yes": "oui"})
        self.assertEqual((ev.value("A1"), ev.value("A2")), (1.5, "oui"))

    def test_lists_and_pivots_side_by_side(self):
        cells = {"A1": '=ODOO.LIST(1,1,"f")+ODOO.PIVOT(1,"__count","c:month","01/2026")'}
        self.assertEqual(vf.SheetEvaluator(dashboard(cells), self.LISTS, None, {1: pivot({"01/2026": 2})}).value("A1"),
                         3.5)

    def test_an_unprovided_list_and_unknown_functions_behave_as_before(self):
        with self.assertRaises(vf.NotCatchable):
            calc('=ODOO.LIST(7,1,"f")', lists=self.LISTS)
        for formula in ("=ODOO.PIVOT.CELL(1,1)", "=ODOO.PIVOT.POSITION(1,1)", "=YEARS(1)", "=ODOO.PIVOTS(1)",
                        "=year(1)"):
            with self.subTest(formula=formula):
                with self.assertRaises(vf.UnknownFunction):
                    calc(formula)

    def test_the_generated_function_names_are_in_the_frozen_client_list(self):
        names = vf.load_frozen()
        for name in ("ODOO.PIVOT", "ODOO.PIVOT.HEADER", "YEAR", "IFERROR", "IF", "SUM", "ODOO.LIST"):
            self.assertIn(name, names)

    def test_the_new_functions_are_not_reported_unknown_for_a_pivot_dashboard(self):
        data = dashboard({"A1": '=IFERROR(ODOO.PIVOT(1,"__count","c:month","01/"&$A$12)+0,0)',
                          "A12": '=IFERROR(YEAR(ODOO.PIVOT.HEADER(2,"#c:month",1)),0)',
                          "A2": '=IF(A1>0,A1/A1,"N/A")', "A3": "=SUM(A1:A2)"})
        self.assertEqual(vf.unknown_function_problems(data, vf.load_frozen()), [])


if __name__ == "__main__":
    unittest.main()
