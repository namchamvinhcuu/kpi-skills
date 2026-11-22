"""Step 8b of the binder (`scripts/bind_kpi.py`): pivot bindings (`ODOO.PIVOT` reads of one month of a year) and derived
formulas (`ratio`, `prev_period`, `sum`). The checks that EVALUATE the generated dashboard are in
test_bind_pivot_oracle.py; the evaluator itself in test_validate_pivot.py.

Design under test (measured in a real browser, see the Plan note): a pivot read is
`ODOO.PIVOT(<data pivot>,"<measure>","<date field>:month","MM/"&$A$<year row>)` where the hidden helper cell
`A<year row>` holds the YEAR of the first month column of a PROBE pivot that is matched on the Year filter; the data
pivots are never matched on the filter. Inputs are invented (bind_fixtures.py): English text, no customer data.
"""
import io
import json
import os
import unittest

import bind_fixtures as bf
from bind_fixtures import derived, hist, other, plain
from helpers import REPO, IsolatedTestCase  # noqa: F401

import bind_kpi
import intake_xlsx

DROP = object()
MONTHS = "DEFGHIJKLMNO"


def probe_formula(probe_id, field):
    return '=IFERROR(YEAR(ODOO.PIVOT.HEADER(%d,"#%s:month",1)),0)' % (probe_id, field)


def user_read(pivot_id, measure, field, month, year_ref, previous=False):
    key = '"%02d/"&%s' % (month, "(%s-1)" % year_ref if previous else year_ref)
    return 'ODOO.PIVOT(%d,"%s","%s:month",%s)+0' % (pivot_id, measure, field, key)


# ---------------------------------------------------------------- 1. the catalog shape

class CatalogShapeTests(IsolatedTestCase):
    def load(self, catalog):
        path = os.path.join(self.tmp, "catalog.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(catalog, handle)
        return bind_kpi.load_catalog(path)

    @staticmethod
    def with_pivot(name="piv", **changes):
        src = bf.pivot_source("t.piv", "when", {"form": {"values": ["a", "b"]}}, ["__count", "qty"],
                              {"result": {"values": ["OK", "NG"]}})
        for key, value in changes.items():
            if value is DROP:
                src.pop(key)
            else:
                src[key] = value
        return {"schema": 1, "sources": {"plain": bf.small_catalog()["sources"]["plain"], name: src}}

    def refuse(self, catalog, name="piv"):
        with self.assertRaises(bind_kpi.BindError) as ctx:
            self.load(catalog)
        self.assertEqual(str(ctx.exception), "the catalog source %r has an unexpected shape" % name[:40])

    def test_a_well_formed_pivot_source_loads_next_to_a_list_source(self):
        catalog = self.with_pivot()
        self.assertEqual(self.load(catalog), catalog)

    def test_a_pivot_source_needs_none_of_the_list_fields(self):
        source = self.with_pivot()["sources"]["piv"]
        for field in ("column_groups", "value_columns", "year_chain"):
            self.assertNotIn(field, source)
        self.load(self.with_pivot())

    def test_optional_filters_may_be_absent_and_selectors_may_be_empty(self):
        self.load(self.with_pivot(optional_filters=DROP))
        self.load(self.with_pivot(selectors={}, optional_filters={}))

    def test_every_malformed_pivot_field_is_refused(self):
        bad = {"model": (DROP, None, 5, ["t"]), "date_field": (DROP, None, 5, ["when"]),
               "read_audience": (DROP, None, 5), "selectors": (DROP, None, [], "x", 5),
               "measures": (DROP, None, [], "__count", {}, 5), "optional_filters": (None, [], "x", 5)}
        for field, values in bad.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.refuse(self.with_pivot(**{field: value}))

    def test_every_malformed_selector_or_filter_rule_is_refused(self):
        rules = ("x", None, 5, [], {}, {"values": "ab"}, {"values": None}, {"values": {"a": 1}},
                 {"min": 1, "max": 3})                      # a pivot selector is a closed list of values only
        for field, rule in (("selectors", "form"), ("optional_filters", "result")):
            for bad in rules:
                with self.subTest(field=field, rule=bad):
                    good = self.with_pivot()["sources"]["piv"][field]
                    good[rule] = bad
                    self.refuse(self.with_pivot(**{field: good}))

    def test_one_malformed_rule_among_good_ones_is_refused(self):
        selectors = {"form": {"values": ["a"]}, "zone": {"values": ["N"]}, "bad": {"values": "N"}}
        self.refuse(self.with_pivot(selectors=selectors))
        self.refuse(self.with_pivot(optional_filters={"r": {"values": ["a"]}, "bad": 1}))

    def test_a_pivot_source_that_is_not_an_object_is_refused_by_the_list_rules(self):
        catalog = self.with_pivot()
        catalog["sources"]["piv"] = "pivot"
        self.refuse(catalog)

    def test_another_kind_is_checked_as_a_list_source(self):
        for kind in (DROP, None, "table", "Pivot", "pivots", ["pivot"]):
            with self.subTest(kind=kind):
                catalog = self.with_pivot()
                source = catalog["sources"]["piv"]
                if kind is DROP:
                    source.pop("kind")
                else:
                    source["kind"] = kind
                self.refuse(catalog)                         # no column_groups / value_columns / year_chain

    def test_the_source_name_is_cut_at_40_characters_in_the_message(self):
        name = "n" * 100
        catalog = self.with_pivot(name=name, measures=[])
        with self.assertRaises(bind_kpi.BindError) as ctx:
            self.load(catalog)
        self.assertIn("n" * 40, str(ctx.exception))
        self.assertNotIn("n" * 41, str(ctx.exception))

    def test_the_list_sources_are_still_checked_as_before(self):
        real = bf.real_catalog()
        list_names = [name for name, source in real["sources"].items() if source.get("kind") != "pivot"]
        self.assertGreaterEqual(len(list_names), 5)
        drops = ("model", "read_audience", "selectors", "column_groups", "value_columns", "year_chain")
        for name in list_names:
            for field in drops:
                with self.subTest(source=name, drop=field):
                    catalog = json.loads(json.dumps(real))
                    catalog["sources"][name].pop(field)
                    self.refuse(catalog, name)
            with self.subTest(source=name, drop="current chain"):
                catalog = json.loads(json.dumps(real))
                catalog["sources"][name]["year_chain"].pop("current")
                self.refuse(catalog, name)
            for selector in real["sources"][name]["selectors"]:
                with self.subTest(source=name, selector=selector):
                    catalog = json.loads(json.dumps(real))
                    catalog["sources"][name]["selectors"][selector] = {"min": 1}
                    self.refuse(catalog, name)

    def test_the_real_catalog_loads_with_its_five_list_sources_and_the_pivot_source(self):
        catalog = bf.real_catalog()
        lists = sorted(name for name, source in catalog["sources"].items() if source.get("kind") != "pivot")
        self.assertEqual(len(lists), 5)
        self.assertEqual(sorted(name for name in catalog["sources"] if name not in lists), ["qms_history"])
        for name in lists:
            source = catalog["sources"][name]
            self.assertIn("column_groups", source)
            self.assertIn("current", source["year_chain"])

    def test_the_real_pivot_source_is_what_the_binder_and_the_skill_rely_on(self):
        source = bf.real_catalog()["sources"]["qms_history"]
        self.assertEqual((source["kind"], source["model"], source["date_field"], source["read_audience"]),
                         ("pivot", "mes.qc_form.history", "check_date", "base.group_user"))
        self.assertEqual(source["measures"][0], "__count")
        self.assertEqual(set(source["measures"]),
                         {"__count", "qty", "insp_qty_sampling", "ng_qty", "ok_lot_qty", "ng_lot_qty"})
        self.assertEqual(sorted(source["selectors"]), ["form_type"])
        self.assertEqual(source["selectors"]["form_type"]["values"],
                         ["iqc", "pqc", "oqc", "iqc_outsourcing", "item_qc", "re_use"])
        self.assertEqual(source["optional_filters"], {"overall_result": {"values": ["OK", "Pass", "NG", "Fail"]}})
        self.assertTrue(source["caveats"] and all(isinstance(c, str) and c for c in source["caveats"]))
        self.assertTrue(source["title"].strip())


# ---------------------------------------------------------------- 2. pivot binding validation

class PivotWhereTests(bf.PivotCase):
    def test_every_selector_must_be_given_and_nothing_else(self):
        for where in ({}, {"form": "a"}, {"zone": "N"}, {"form": "a", "zone": "N", "extra": "x"}, None, [], "form"):
            with self.subTest(where=where):
                self.refuse([{"cells": "D5:O5", "source": "hist", "where": where}],
                            contains="`where` must give exactly ['form', 'zone']")

    def test_a_missing_where_is_refused_when_the_source_has_selectors(self):
        self.refuse([{"cells": "D5:O5", "source": "hist"}], contains="`where` must give exactly")

    def test_a_source_without_selectors_takes_no_where_or_an_empty_one(self):
        bf.pbind([bf.other("D5:O5")])
        bf.pbind([bf.other("D5:O5", where={})])
        self.refuse([bf.other("D5:O5", where={"x": "y"})], contains="exactly nothing")

    def test_a_selector_value_must_be_one_of_the_allowed_texts(self):
        for value in ("d", "A", "a ", "", None, 1, True, ["a"], {"a": 1}, 1.5):
            with self.subTest(value=value):
                self.refuse([hist("D5:O5", form=value)], contains="not allowed, use ['a', 'b', 'c']")

    def test_the_value_in_the_message_is_cut(self):
        message = self.refuse([hist("D5:O5", form="x" * 100)])
        self.assertIn("x" * 40, message)
        self.assertNotIn("x" * 41, message)

    def test_every_allowed_value_binds(self):
        for form in "abc":
            for zone in "NS":
                with self.subTest(form=form, zone=zone):
                    document, _ = bf.pbind([hist("D5:O5", form=form, zone=zone)])
                    self.assertEqual(document["pivots"]["1"]["domain"], [["form", "=", form], ["zone", "=", zone]])


class PivotFilterTests(bf.PivotCase):
    def test_a_filter_is_optional_and_an_empty_object_is_the_same_as_none(self):
        document, _ = bf.pbind([hist("D5:O5", filter={})])
        self.assertEqual(document["pivots"]["1"]["domain"], [["form", "=", "a"], ["zone", "=", "N"]])

    def test_only_the_filters_of_the_source_are_allowed(self):
        for chosen in ({"nope": ["OK"]}, {"result": ["OK"], "nope": ["x"]}, {"form": ["a"]}, {"RESULT": ["OK"]}):
            with self.subTest(chosen=chosen):
                self.refuse([hist("D5:O5", filter=chosen)], contains="`filter` may only use ['grade', 'result']")

    def test_a_filter_that_is_not_an_object_is_refused(self):
        for chosen in (None, [], ["result"], "result", 5, True):
            with self.subTest(chosen=chosen):
                self.refuse([hist("D5:O5", filter=chosen)], contains="`filter` may only use")

    def test_a_source_without_filters_refuses_any_filter(self):
        self.refuse([bf.other("D5:O5", filter={"x": ["y"]})], contains="may only use nothing")

    def test_the_values_must_be_a_non_empty_list_of_distinct_allowed_texts(self):
        bad = ("OK", [], None, 1, {"OK": 1}, ["ok"], ["OK", "OK"], ["X"], [1], [True], [None], ["OK", 1], [["OK"]],
               ["OK", "Pass", "OK"], [1, "1"])
        for values in bad:
            with self.subTest(values=values):
                self.refuse([hist("D5:O5", filter={"result": values})],
                            contains="filter result must be a non-empty list of distinct values from ['OK', 'NG', 'Pass']")

    def test_a_bare_text_is_not_a_list_even_when_it_is_made_of_allowed_values(self):
        # iterating the text "X" would give ["X"], an allowed grade
        for chosen in ("X", "Y", "XY", "XX"):
            with self.subTest(chosen=chosen):
                self.refuse([hist("D5:O5", filter={"grade": chosen})], contains="filter grade must be a non-empty list")

    def test_the_second_filter_is_checked_as_well(self):
        self.refuse([hist("D5:O5", filter={"result": ["OK"], "grade": ["Z"]})], contains="filter grade must be")

    def test_the_in_terms_follow_the_selector_terms_sorted_by_name_and_keep_the_value_order(self):
        document, _ = bf.pbind([hist("D5:O5", form="b", zone="S", filter={"result": ["Pass", "OK"], "grade": ["Y"]})])
        self.assertEqual(document["pivots"]["1"]["domain"],
                         [["form", "=", "b"], ["zone", "=", "S"], ["grade", "in", ["Y"]],
                          ["result", "in", ["Pass", "OK"]]])

    def test_the_binding_is_not_changed_and_the_domain_does_not_alias_it(self):
        chosen = ["OK", "Pass"]
        document, _ = bf.pbind([hist("D5:O5", filter={"result": chosen})])
        document["pivots"]["1"]["domain"][2][2].append("NG")
        self.assertEqual(chosen, ["OK", "Pass"])


class PivotMeasureTests(bf.PivotCase):
    def test_the_default_measure_is_the_count(self):
        document, _ = bf.pbind([hist("D5:O5")])
        self.assertEqual(document["pivots"]["1"]["measures"], [{"field": "__count"}])
        self.assertIn('"__count"', self.cell(document, "D5"))

    def test_every_measure_of_the_source_binds(self):
        for measure in ("__count", "qty", "ng_qty"):
            with self.subTest(measure=measure):
                document, _ = bf.pbind([hist("D5:O5", measure=measure)])
                self.assertEqual(document["pivots"]["1"]["measures"], [{"field": measure}])
                self.assertIn('"%s"' % measure, self.cell(document, "E5"))

    def test_a_measure_outside_the_source_is_refused(self):
        for measure in ("amount", "QTY", "", None, 5, ["qty"], True, "qty "):
            with self.subTest(measure=measure):
                self.refuse([hist("D5:O5", measure=measure)],
                            contains="is not in the source (use one of ['__count', 'qty', 'ng_qty'])")

    def test_the_measure_in_the_message_is_cut(self):
        message = self.refuse([hist("D5:O5", measure="m" * 100)])
        self.assertIn("m" * 40, message)
        self.assertNotIn("m" * 41, message)

    def test_columns_and_column_are_refused_on_a_pivot_binding(self):
        for key, value in (("columns", "months"), ("column", "qty"), ("columns", ["qty"]), ("columns", None)):
            with self.subTest(key=key):
                self.refuse([hist("D5:O5", **{key: value})], contains="a pivot binding has no `%s`" % key)

    def test_a_pivot_binding_still_refuses_unknown_keys(self):
        self.refuse([hist("D5:O5", colour="red")], contains="not an object with known keys")


class PivotMonthTests(bf.PivotCase):
    def test_twelve_cells_without_month_read_the_months_in_order(self):
        document, _ = bf.pbind([hist("D5:O5")])
        for number, col in enumerate(MONTHS, 1):
            self.assertIn('"%02d/"&$A$12' % number, self.cell(document, col + "5"))
        self.assertEqual(self.cell(document, "D5"), "=IFERROR(%s,0)" % user_read(1, "__count", "when", 1, "$A$12"))
        self.assertEqual(self.cell(document, "O5"), "=IFERROR(%s,0)" % user_read(1, "__count", "when", 12, "$A$12"))

    def test_twelve_cells_in_a_column_read_the_months_top_to_bottom(self):
        document, _ = bf.pbind([hist("D4:D15")], baseline=bf.empty_baseline(row_number=20))
        for number in range(1, 13):
            self.assertIn('"%02d/"&' % number, self.cell(document, "D%d" % (number + 3)))

    def test_a_month_for_one_cell(self):
        for month in range(1, 13):
            with self.subTest(month=month):
                document, _ = bf.pbind([hist("D5", month=month)])
                self.assertEqual(self.cell(document, "D5"),
                                 "=IFERROR(%s,0)" % user_read(1, "__count", "when", month, "$A$12"))

    def test_a_month_outside_1_to_12_or_not_an_integer_is_refused(self):
        for month in (0, 13, -1, 100, True, False, "3", "03", 1.0, 2.5, [3], {"m": 3}):
            with self.subTest(month=month):
                self.refuse([hist("D5", month=month)], contains="`month` is a number 1..12 for ONE cell")

    def test_a_null_month_is_the_same_as_none_given(self):
        self.refuse([hist("D5", month=None)], contains="covers the 12 months (12 cells), got 1")
        document, _ = bf.pbind([hist("D5:O5", month=None)])
        self.assertIn('"12/"', self.cell(document, "O5"))

    def test_a_month_needs_exactly_one_cell(self):
        self.refuse([hist("D5:E5", month=3)], contains="`month` is a number 1..12 for ONE cell")
        self.refuse([hist("D5:O5", month=3)], contains="`month` is a number 1..12 for ONE cell")

    def test_without_a_month_exactly_twelve_cells_are_needed(self):
        for cells in ("D5", "D5:N5", "D5:E5", "D5:P5"):
            with self.subTest(cells=cells):
                self.refuse([hist(cells)], contains="covers the 12 months (12 cells), got %d" % (
                    {"D5": 1, "D5:N5": 11, "D5:E5": 2, "D5:P5": 13}[cells]))

    def test_a_year_is_current_or_previous_and_defaults_to_current(self):
        document, _ = bf.pbind([hist("D5", month=1), hist("D6", month=1, year="previous")])
        self.assertIn('"01/"&$A$12)', self.cell(document, "D5"))
        self.assertIn('"01/"&($A$12-1))', self.cell(document, "D6"))
        for year in ("next", "Current", "", None, True, 2026, ["current"], "previous "):
            with self.subTest(year=year):
                self.refuse([hist("D5", month=1, year=year)], contains="`year` must be current or previous")

    def test_zero_is_number_or_dash(self):
        for zero in ("Dash", "", None, True, 0, ["dash"], "dash "):
            with self.subTest(zero=zero):
                self.refuse([hist("D5", month=1, zero=zero)], contains="`zero` must be number or dash")
        for zero in ("number", "dash"):
            bf.pbind([hist("D5", month=1, zero=zero)])


class PivotCellTests(bf.PivotCase):
    def test_cells_must_be_a_cell_or_a_one_line_range(self):
        for cells in (None, 5, ["D5"], "D5:O6", "D5:E6", "ZZ", "D0", ":D5", "", "O5:D5", "D6:D5"):
            with self.subTest(cells=cells):
                self.refuse([{"cells": cells, "source": "hist", "where": {"form": "a", "zone": "N"}, "month": 1}])

    def test_a_pivot_binding_is_checked_for_the_area_in_both_directions(self):
        self.refuse([hist("D11", month=1)], contains="D11 is outside the design area (10 rows x 16 columns)")
        self.refuse([hist("Q5", month=1)], contains="Q5 is outside the design area")
        bf.pbind([hist("P10", month=1)])                       # the last cell of the area
        bf.pbind([hist("E5:P5")])                              # twelve cells ending in the last column
        self.refuse([hist("F5:Q5")], contains="Q5 is outside")

    def test_overlap_with_a_list_binding_in_both_orders(self):
        for bindings in ([plain("D5:F5"), hist("D5", month=1)],
                         [hist("D5", month=1), plain("D5:F5")],
                         [plain("D5:F5"), hist("F5:Q5")]):
            with self.subTest(bindings=[b["cells"] for b in bindings]):
                self.refuse(bindings, contains="already bound by another binding")

    def test_overlap_with_another_pivot_binding(self):
        self.refuse([hist("D5:O5"), hist("O5", month=1, form="b")], contains="O5 is already bound by another binding")
        self.refuse([hist("O5", month=1, form="b"), hist("D5:O5")], contains="O5 is already bound by another binding")
        self.refuse([hist("D5:O5"), bf.other("D5", month=1)], contains="already bound")

    def test_overlap_with_a_derived_binding_in_both_orders(self):
        for bindings in ([hist("D5:O5"), derived("D5", "sum", of="E5:F5")],
                         [derived("D5", "sum", of="E5:F5"), hist("D5:O5")]):
            with self.subTest(first=list(bindings[0])):
                self.refuse(bindings, contains="D5 is already bound by another binding")

    def test_a_target_inside_a_merge_must_be_its_anchor(self):
        baseline = bf.make_baseline(cells={})                 # merges A1:C1 and P8:P9
        self.refuse([hist("B1", month=1)], baseline, contains="B1 is inside the merge A1:C1 but is not its top-left")
        self.refuse([hist("P9", month=1)], baseline, contains="P9 is inside the merge P8:P9")
        bf.pbind([hist("P8", month=1)], baseline)
        bf.pbind([hist("A1", month=1)], baseline)

    def test_a_text_label_is_refused_unless_overwrite_text_is_true(self):
        baseline = bf.make_baseline()                          # A5 "Row label one", typed zeros D5:O6, formulas row 7
        message = self.refuse([hist("A5", month=1)], baseline, contains="A5 holds the text 'Row label one'")
        self.assertIn('"overwrite_text": true', message)
        document, _ = bf.pbind([hist("A5", month=1, overwrite_text=True), hist("D5:O5", form="b"),
                                hist("D6:O6", form="c")], baseline)
        self.assertTrue(self.cell(document, "A5").startswith("=IFERROR(ODOO.PIVOT("))
        for value in ("true", "yes", 1, [True], "false", 0):
            with self.subTest(overwrite_text=value):
                self.refuse([hist("A5", month=1, overwrite_text=value)], baseline, contains="holds the text")

    def test_a_typed_number_a_formula_and_an_empty_cell_are_replaced(self):
        baseline = bf.make_baseline()
        document, _ = bf.pbind([hist("D5:O5"), hist("D6:O6", form="b")], baseline)
        self.assertTrue(self.cell(document, "D5").startswith("=IFERROR(ODOO.PIVOT("))
        baseline = bf.edit(bf.empty_baseline(), {"D5": {"content": "=1+1"}, "E5": {"content": "12.5", "style": 1}})
        document, _ = bf.pbind([hist("D5", month=1), hist("E5", month=2), hist("F5", month=3)], baseline)
        for ref in "DEF":
            self.assertTrue(self.cell(document, ref + "5").startswith("=IFERROR(ODOO.PIVOT("))
        self.assertEqual(document["sheets"][0]["cells"]["E5"]["style"], 1)       # the look is kept

    def test_a_typed_number_next_to_the_bindings_is_still_a_leftover(self):
        baseline = bf.edit(bf.empty_baseline(), {"D5": {"content": "0"}, "P5": {"content": "7"}})
        self.refuse([hist("D5", month=1)], baseline, contains="typed numbers left outside the bindings: P5")
        document, _ = bf.pbind([hist("D5", month=1)], baseline, keep_typed=["P5"])
        self.assertEqual(self.cell(document, "P5"), "7")

    def test_a_pivot_cell_is_not_a_leftover_and_a_derived_cell_neither(self):
        baseline = bf.edit(bf.empty_baseline(), {"D5": {"content": "0"}, "D6": {"content": "5"}})
        bf.pbind([hist("D5", month=1), derived("D6", "sum", of="D5")], baseline)


class PivotKeysOnListBindingTests(bf.PivotCase):
    def test_a_list_binding_refuses_the_pivot_only_keys(self):
        # Before step 8b the key check refused them; BINDING_KEYS now holds them for every binding, and a list
        # binding silently IGNORES `measure`, `month` and `filter` (a `filter` that filters nothing = wrong figures).
        for key, value in (("measure", "qty"), ("month", 3), ("filter", {"code": ["A"]})):
            with self.subTest(key=key):
                with self.assertRaises(bind_kpi.BindError):
                    bf.pbind([plain("D5:F5", **{key: value})])


# ---------------------------------------------------------------- 3. output structure

class PivotDefinitionTests(bf.PivotCase):
    def test_the_data_pivot_and_the_probe_have_the_documented_shape(self):
        document, _ = bf.pbind([hist("D5:O5")])
        self.assertEqual(document["pivots"]["1"], {
            "colGroupBys": ["when:month"], "rowGroupBys": [], "context": {},
            "domain": [["form", "=", "a"], ["zone", "=", "N"]], "id": "1", "measures": [{"field": "__count"}],
            "model": "t.hist", "name": "History forms a N", "sortedColumn": None, "fieldMatching": {}})
        self.assertEqual(document["pivots"]["2"], {
            "colGroupBys": ["when:month"], "rowGroupBys": [], "context": {}, "domain": [], "id": "2",
            "measures": [{"field": "__count"}], "model": "t.hist", "name": "Year of the filter: hist",
            "sortedColumn": None, "fieldMatching": {"kpi-year-filter": {"chain": "when", "type": "date"}}})
        self.assertEqual(list(document["pivots"]), ["1", "2"])

    def test_a_data_pivot_is_never_matched_on_the_year_filter_and_only_the_probe_is(self):
        document, _ = bf.pbind([hist("D5:O5"), hist("D6:O6", form="b"), bf.other("D7:O7")])
        matched = [pid for pid, pivot in document["pivots"].items() if pivot["fieldMatching"]]
        self.assertEqual(matched, ["4", "5"])
        for pid in ("1", "2", "3"):
            self.assertEqual(document["pivots"][pid]["fieldMatching"], {})

    def test_the_probe_has_no_domain_and_only_the_count_whatever_the_bindings_ask(self):
        document, _ = bf.pbind([hist("D5:O5", measure="qty", filter={"result": ["OK"]})])
        probe = document["pivots"]["2"]
        self.assertEqual((probe["domain"], probe["measures"]), ([], [{"field": "__count"}]))
        self.assertEqual(document["pivots"]["1"]["measures"], [{"field": "qty"}])

    def test_data_pivot_ids_follow_the_first_use_and_the_probes_come_after_in_source_order(self):
        document, _ = bf.pbind([bf.other("D5:O5"), bf.other("D6:O6", measure="amount"), hist("D7:O7", form="b"),
                                hist("D8:O8", form="a"), bf.other("D9:O9", zero="dash"), hist("D10:O10", form="b")])
        models = [(pid, p["model"], p["domain"]) for pid, p in document["pivots"].items()]
        self.assertEqual(models, [("1", "t.other", []), ("2", "t.hist", [["form", "=", "b"], ["zone", "=", "N"]]),
                                  ("3", "t.hist", [["form", "=", "a"], ["zone", "=", "N"]]),
                                  ("4", "t.hist", []), ("5", "t.other", [])])
        self.assertEqual([p["id"] for p in document["pivots"].values()], ["1", "2", "3", "4", "5"])
        self.assertEqual(document["pivotNextId"], 6)

    def test_one_data_pivot_per_distinct_source_where_and_filter_only(self):
        document, report = bf.pbind([
            hist("D5:O5"),
            hist("D6:O6", year="previous"),                                  # same pivot, other year
            hist("D7:O7", measure="qty", zero="dash"),                       # same pivot, other measure
            hist("D8", month=3),                                             # same pivot, one month
            hist("D9:O9"),                                                   # same pivot again
            hist("D4:O4", filter={"result": ["OK"]}),                        # other filter
            hist("D3", month=1, form="b"),                                   # other where
            hist("D2:O2", filter={"result": ["OK"], "grade": ["X"]})])       # two filters
        self.assertEqual(report["pivots"], 4)
        self.assertEqual([p["domain"] for pid, p in document["pivots"].items() if p["domain"]], [
            [["form", "=", "a"], ["zone", "=", "N"]],
            [["form", "=", "a"], ["zone", "=", "N"], ["result", "in", ["OK"]]],
            [["form", "=", "b"], ["zone", "=", "N"]],
            [["form", "=", "a"], ["zone", "=", "N"], ["grade", "in", ["X"]], ["result", "in", ["OK"]]]])

    def test_the_key_order_of_the_where_and_of_the_filter_does_not_matter(self):
        first = hist("D5:O5", filter={"result": ["OK"], "grade": ["X"]})
        second = {"cells": "D6:O6", "source": "hist", "where": {"zone": "N", "form": "a"},
                  "filter": {"grade": ["X"], "result": ["OK"]}}
        document, report = bf.pbind([first, second])
        self.assertEqual(report["pivots"], 1)

    def test_the_measures_of_one_pivot_are_the_sorted_union(self):
        document, _ = bf.pbind([hist("D5:O5", measure="qty"), hist("D6:O6"), hist("D7:O7", measure="ng_qty"),
                                hist("D8:O8", measure="qty")])
        self.assertEqual(document["pivots"]["1"]["measures"],
                         [{"field": "__count"}, {"field": "ng_qty"}, {"field": "qty"}])

    def test_the_pivot_names_and_their_cut(self):
        document, _ = bf.pbind([hist("D5:O5"), bf.other("D6:O6"), {"cells": "D7:O7", "source": "longp"}])
        self.assertEqual(document["pivots"]["1"]["name"], "History forms a N")
        self.assertEqual(document["pivots"]["2"]["name"], "Other events ")
        self.assertEqual(document["pivots"]["3"]["name"], "P" * 100)
        self.assertEqual(len(document["pivots"]["3"]["name"]), 100)
        filtered, _ = bf.pbind([hist("D5:O5", filter={"result": ["OK", "Pass"]})])
        self.assertTrue(filtered["pivots"]["1"]["name"].startswith("History forms a N "))
        self.assertIn("OK", filtered["pivots"]["1"]["name"])
        self.assertIn("Pass", filtered["pivots"]["1"]["name"])

    def test_the_probe_name_is_cut_at_100_characters(self):
        catalog = bf.pivot_catalog()
        long_name = "s" * 90
        catalog["sources"][long_name] = bf.pivot_source("t.s", "when", {}, ["__count"], {})
        document, _ = bf.pbind([{"cells": "D5:O5", "source": long_name}], catalog=catalog)
        name = document["pivots"]["2"]["name"]
        self.assertEqual(name, ("Year of the filter: " + long_name)[:100])
        self.assertEqual(len(name), 100)
        short, _ = bf.pbind([hist("D5:O5")])
        self.assertEqual(short["pivots"]["2"]["name"], "Year of the filter: hist")

    def test_a_pivot_never_holds_free_text_of_the_spec(self):
        document, _ = bf.pbind([hist("D5:O5")], name="Zebra dashboard")
        self.assertNotIn("Zebra", json.dumps(document))

    def test_no_pivot_binding_gives_no_pivot_and_no_probe(self):
        document, report = bf.bind()
        self.assertEqual((document["pivots"], document["pivotNextId"], report["pivots"]), ({}, 1, 0))
        self.assertNotIn("when", json.dumps(document["sheets"]))

    def test_the_pivots_of_the_baseline_are_replaced(self):
        baseline = bf.empty_baseline()
        baseline["pivots"] = {"9": {"id": "9", "model": "old"}}
        baseline["pivotNextId"] = 10
        document, _ = bf.pbind([hist("D5:O5")], baseline)
        self.assertEqual(sorted(document["pivots"]), ["1", "2"])
        self.assertEqual(document["pivotNextId"], 3)
        document, _ = bf.pbind([plain("D5:F5")], baseline)
        self.assertEqual((document["pivots"], document["pivotNextId"]), ({}, 1))

    def test_pivot_next_id_counts_data_pivots_and_probes(self):
        document, _ = bf.pbind([hist("D5:O5"), hist("D6:O6", form="b"), bf.other("D7:O7")])
        self.assertEqual(len(document["pivots"]), 5)
        self.assertEqual(document["pivotNextId"], 6)

    def test_the_input_is_not_mutated(self):
        baseline = bf.empty_baseline()
        spec = bf.make_spec([hist("D5:O5", filter={"result": ["OK"]}), derived("D8", "sum", of="D5:O5")])
        before = (json.dumps(baseline, sort_keys=True), json.dumps(spec, sort_keys=True))
        bf.bind(spec, baseline, bf.pivot_catalog())
        self.assertEqual((json.dumps(baseline, sort_keys=True), json.dumps(spec, sort_keys=True)), before)


class PivotYearCellTests(bf.PivotCase):
    def test_the_year_cell_sits_in_a_hidden_row_below_the_area_and_reads_the_probe(self):
        document, _ = bf.pbind([hist("D5:O5")])
        sheet = document["sheets"][0]
        self.assertEqual(sheet["cells"]["A12"]["content"], probe_formula(2, "when"))
        self.assertEqual(sheet["rowNumber"], 12)
        self.assertEqual(sheet["rows"]["11"], {"size": 23, "isHidden": True})
        self.assertEqual(sorted(sheet["rows"], key=int), ["0", "4", "9", "11"])      # row 40 is outside the area

    def test_the_year_cell_sits_after_the_dash_helper_rows(self):
        document, report = bf.pbind([hist("D5:O5", zero="dash"), hist("D6:O6", form="b", zero="dash"),
                                     hist("D7:O7", form="c")])
        sheet = document["sheets"][0]
        self.assertEqual(report["helper_rows"], 2)
        self.assertEqual(sheet["cells"]["A14"]["content"], probe_formula(4, "when"))        # rows 12, 13 = helpers
        self.assertEqual(sheet["rowNumber"], 14)
        for hidden in ("11", "12", "13"):
            self.assertEqual(sheet["rows"][hidden], {"size": 23, "isHidden": True})
        self.assertNotIn("14", sheet["rows"])
        self.assertIn("$A$14", sheet["cells"]["D7"]["content"])
        self.assertIn("$A$14", sheet["cells"]["D12"]["content"])

    def test_one_year_cell_per_pivot_source_in_source_name_order_even_with_several_bindings(self):
        document, _ = bf.pbind([bf.other("D5:O5"), {"cells": "D6:O6", "source": "vault"}, hist("D7:O7"),
                                hist("D8:O8", form="b"), bf.other("D9", month=1, measure="amount")])
        sheet = document["sheets"][0]
        self.assertEqual([sheet["cells"]["A%d" % r]["content"] for r in (12, 13, 14)],
                         [probe_formula(5, "when"), probe_formula(6, "day"), probe_formula(7, "stamp")])
        self.assertEqual(sheet["rowNumber"], 14)
        self.assertEqual([r for r in sheet["rows"] if sheet["rows"][r].get("isHidden")], ["11", "12", "13"])
        self.assertIn("$A$12", sheet["cells"]["D7"]["content"])          # hist -> first
        self.assertIn("$A$13", sheet["cells"]["D5"]["content"])          # other -> second
        self.assertIn("$A$14", sheet["cells"]["D6"]["content"])          # vault -> third
        self.assertEqual(sum(1 for ref in sheet["cells"] if ref.startswith("A") and ref[1:].isdigit()
                             and int(ref[1:]) > 10), 3)

    def test_a_list_only_spec_gets_no_year_cell_and_the_old_row_layout(self):
        document, _ = bf.bind()
        sheet = document["sheets"][0]
        self.assertEqual(sheet["rowNumber"], 12)                          # dash row 5 -> helper row 12
        self.assertNotIn("A12", sheet["cells"])
        self.assertEqual([r for r in sheet["rows"] if sheet["rows"][r].get("isHidden")], ["11"])

    def test_the_year_cell_does_not_touch_the_baseline_cells_and_a_dash_helper_in_column_a(self):
        baseline = bf.empty_baseline()
        document, _ = bf.pbind([hist("A5", month=1, zero="dash"), hist("D6", month=1, form="b")], baseline)
        sheet = document["sheets"][0]
        self.assertEqual(sheet["cells"]["A12"]["content"],
                         "=IFERROR(%s,0)" % user_read(1, "__count", "when", 1, "$A$13"))      # the dash helper of A5
        self.assertEqual(sheet["cells"]["A13"]["content"], probe_formula(3, "when"))
        self.assertEqual(sheet["cells"]["A5"]["content"], '=IF(A12=0,"-",A12)')


class PivotReadFormulaTests(bf.PivotCase):
    def test_current_and_previous_year_reads(self):
        document, _ = bf.pbind([hist("D5:O5", measure="qty"), hist("D6:O6", measure="qty", year="previous")])
        self.assertEqual(self.cell(document, "F5"),
                         '=IFERROR(ODOO.PIVOT(1,"qty","when:month","03/"&$A$12)+0,0)')
        self.assertEqual(self.cell(document, "F6"),
                         '=IFERROR(ODOO.PIVOT(1,"qty","when:month","03/"&($A$12-1))+0,0)')
        self.assertEqual(self.cell(document, "O6"),
                         '=IFERROR(ODOO.PIVOT(1,"qty","when:month","12/"&($A$12-1))+0,0)')

    def test_the_read_formula_function_directly(self):
        user, system = bf.pivot_catalog()["sources"]["hist"], bf.pivot_catalog()["sources"]["vault"]
        self.assertEqual(bind_kpi.pivot_read_formula(user, 3, "qty", 7, "current", "$A$12"),
                         'IFERROR(ODOO.PIVOT(3,"qty","when:month","07/"&$A$12)+0,0)')
        self.assertEqual(bind_kpi.pivot_read_formula(user, 3, "qty", 7, "previous", "$A$12"),
                         'IFERROR(ODOO.PIVOT(3,"qty","when:month","07/"&($A$12-1))+0,0)')
        self.assertEqual(bind_kpi.pivot_read_formula(system, 3, "qty", 12, "current", "$A$5"),
                         'ODOO.PIVOT(3,"qty","stamp:month","12/"&$A$5)+0')
        self.assertEqual(bind_kpi.pivot_read_formula(user, 10, "__count", 1, "current", "$A$12"),
                         'IFERROR(ODOO.PIVOT(10,"__count","when:month","01/"&$A$12)+0,0)')

    def test_the_audience_rule_only_the_default_user_group_tolerates_a_missing_read(self):
        for audience, wrapped in (("base.group_user", True), ("base.group_system", False), ("", False),
                                  ("base.group_user ", False), ("BASE.GROUP_USER", False), ("sales.group", False)):
            with self.subTest(audience=audience):
                source = dict(bf.pivot_catalog()["sources"]["hist"], read_audience=audience)
                formula = bind_kpi.pivot_read_formula(source, 1, "__count", 1, "current", "$A$12")
                self.assertEqual(formula.startswith("IFERROR("), wrapped)
                self.assertTrue(formula.endswith(",0)" if wrapped else ")+0"))

    def test_a_restricted_source_reads_raw_through_bind_and_the_year_cell_is_always_protected(self):
        document, _ = bf.pbind([{"cells": "D5:O5", "source": "vault"}])
        sheet = document["sheets"][0]
        self.assertEqual(sheet["cells"]["D5"]["content"], '=ODOO.PIVOT(1,"__count","stamp:month","01/"&$A$12)+0')
        self.assertNotIn("IFERROR", sheet["cells"]["D5"]["content"])
        self.assertTrue(sheet["cells"]["A12"]["content"].startswith("=IFERROR(YEAR("))

    def test_a_dash_cell_has_a_helper_and_a_display_cell(self):
        document, report = bf.pbind([hist("D5:O5", zero="dash")])
        sheet = document["sheets"][0]
        self.assertEqual(sheet["cells"]["D5"]["content"], '=IF(D12=0,"-",D12)')
        self.assertEqual(sheet["cells"]["D12"]["content"],
                         '=IFERROR(ODOO.PIVOT(1,"__count","when:month","01/"&$A$13)+0,0)')
        self.assertEqual(sheet["cells"]["O12"]["content"],
                         '=IFERROR(ODOO.PIVOT(1,"__count","when:month","12/"&$A$13)+0,0)')
        self.assertEqual(sheet["cells"]["A13"]["content"], probe_formula(2, "when"))
        self.assertEqual((report["dash_cells"], report["helper_rows"]), (12, 1))

    def test_a_number_cell_has_no_helper(self):
        document, report = bf.pbind([hist("D5:O5")])
        self.assertEqual((report["dash_cells"], report["helper_rows"]), (0, 0))
        self.assertNotIn("D12", document["sheets"][0]["cells"])

    def test_a_dash_cell_takes_the_look_of_the_baseline_cell(self):
        baseline = bf.edit(bf.empty_baseline(), {"D5": {"content": "0", "style": 2, "format": 1, "border": 1}})
        document, _ = bf.pbind([hist("D5", month=1, zero="dash")], baseline)
        cell = document["sheets"][0]["cells"]["D5"]
        self.assertEqual((cell["style"], cell["format"], cell["border"]), (2, 1, 1))
        self.assertNotIn("style", document["sheets"][0]["cells"]["D12"])


class MixedListAndPivotTests(bf.PivotCase):
    def test_the_list_part_is_the_same_with_or_without_pivot_bindings(self):
        lists_only, _ = bf.pbind([plain("D5:F5", "A", zero="dash"), plain("D6:F6", "B")])
        mixed, _ = bf.pbind([plain("D5:F5", "A", zero="dash"), hist("D8:O8"), plain("D6:F6", "B"),
                             hist("D9:O9", form="b")])
        self.assertEqual(mixed["lists"], lists_only["lists"])
        self.assertEqual(mixed["listNextId"], lists_only["listNextId"])
        for ref in ("D5", "E5", "F5", "D6", "F6", "D12", "F12"):
            self.assertEqual(mixed["sheets"][0]["cells"][ref], lists_only["sheets"][0]["cells"][ref])

    def test_helper_rows_are_shared_in_row_order_between_lists_and_pivots(self):
        document, report = bf.pbind([plain("D6:F6", "A", zero="dash"), hist("D5:O5", zero="dash")])
        cells = document["sheets"][0]["cells"]
        self.assertEqual(report["helper_rows"], 2)
        self.assertEqual(cells["D5"]["content"], '=IF(D12=0,"-",D12)')          # row 5 -> first helper row
        self.assertEqual(cells["D6"]["content"], '=IF(D13=0,"-",D13)')
        self.assertTrue(cells["D12"]["content"].startswith("=IFERROR(ODOO.PIVOT("))
        self.assertTrue(cells["D13"]["content"].startswith("=IFERROR(ODOO.LIST("))
        self.assertEqual(cells["A14"]["content"], probe_formula(2, "when"))

    def test_a_list_and_a_pivot_dash_cell_in_one_row_share_the_helper_row(self):
        document, report = bf.pbind([plain("D5:F5", "A", zero="dash"), hist("G5", month=1, zero="dash")])
        cells = document["sheets"][0]["cells"]
        self.assertEqual(report["helper_rows"], 1)
        self.assertTrue(cells["D12"]["content"].startswith("=IFERROR(ODOO.LIST("))
        self.assertTrue(cells["G12"]["content"].startswith("=IFERROR(ODOO.PIVOT("))

    def test_exactly_one_year_filter_whatever_the_number_of_lists_and_pivots(self):
        document, _ = bf.pbind([plain("D5:F5", "A"), plain("D6:F6", "B", year="previous"), hist("D7:O7"),
                                bf.other("D8:O8"), {"cells": "D9:O9", "source": "vault"}])
        self.assertEqual(document["globalFilters"], [{"id": "kpi-year-filter", "type": "date", "label": "Year",
                                                     "rangeType": "single_date", "singleDateType": "year"}])
        keys = {key for pivot in document["pivots"].values() for key in pivot["fieldMatching"]}
        keys |= {key for listing in document["lists"].values() for key in listing["fieldMatching"]}
        self.assertEqual(keys, {"kpi-year-filter"})

    def test_the_report_counts_pivot_reads_pivots_and_derived_cells(self):
        _, report = bf.pbind([plain("D5:F5", "A", zero="dash"), hist("D6:O6"), hist("D7", month=2, zero="dash"),
                              derived("D8:F8", "ratio", of="D5:F5", over="D6:F6"),
                              derived("D9", "sum", of="D6:O6")])
        self.assertEqual((report["lists"], report["pivots"], report["bound_cells"], report["dash_cells"],
                          report["helper_rows"], report["derived_cells"]), (1, 1, 3 + 12 + 1, 4, 2, 4))
        self.assertEqual(set(report), {"name", "lists", "pivots", "derived_cells", "bound_cells", "dash_cells",
                                       "helper_rows", "kept_typed"})

    def test_the_pivot_part_is_deterministic_in_binding_order_of_independent_cells(self):
        spec_a = [hist("D5:O5"), hist("D6:O6", form="b", zero="dash")]
        first, _ = bf.pbind(spec_a)
        again, _ = bf.pbind(spec_a)
        self.assertEqual(bf.dumps(first), bf.dumps(again))


# ---------------------------------------------------------------- 4. derived formulas

class DerivedFormulaTests(bf.PivotCase):
    BASE = [hist("D5:O5"), hist("D6:O6", form="b")]

    def test_ratio_over_number_cells(self):
        document, report = bf.pbind(self.BASE + [derived("D8:O8", "ratio", of="D5:O5", over="D6:O6")])
        for col in MONTHS:
            self.assertEqual(self.cell(document, col + "8"), '=IF(%s6>0,%s5/%s6,"N/A")' % (col, col, col))
        self.assertEqual(report["derived_cells"], 12)

    def test_ratio_reads_the_helper_cells_of_dash_sources(self):
        document, _ = bf.pbind([hist("D5:O5", zero="dash"), hist("D6:O6", form="b", zero="dash"),
                                derived("D8:O8", "ratio", of="D5:O5", over="D6:O6")])
        self.assertEqual(self.cell(document, "D8"), '=IF(D13>0,D12/D13,"N/A")')
        self.assertEqual(self.cell(document, "O8"), '=IF(O13>0,O12/O13,"N/A")')

    def test_ratio_with_one_dash_source_and_one_number_source(self):
        document, _ = bf.pbind([hist("D5:O5", zero="dash"), hist("D6:O6", form="b"),
                                derived("D8:O8", "ratio", of="D5:O5", over="D6:O6"),
                                derived("D9:O9", "ratio", of="D6:O6", over="D5:O5")])
        self.assertEqual(self.cell(document, "E8"), '=IF(E6>0,E12/E6,"N/A")')
        self.assertEqual(self.cell(document, "E9"), '=IF(E12>0,E6/E12,"N/A")')

    def test_ratio_in_columns_and_a_single_cell(self):
        baseline = bf.empty_baseline(row_number=20)
        document, _ = bf.pbind([hist("D5:D16"), hist("E5:E16", form="b"),
                                derived("F5:F16", "ratio", of="D5:D16", over="E5:E16"),
                                derived("G5", "ratio", of="D5", over="E5")], baseline)
        self.assertEqual(self.cell(document, "F5"), '=IF(E5>0,D5/E5,"N/A")')
        self.assertEqual(self.cell(document, "F16"), '=IF(E16>0,D16/E16,"N/A")')
        self.assertEqual(self.cell(document, "G5"), '=IF(E5>0,D5/E5,"N/A")')

    def test_a_ratio_may_pair_a_row_with_a_column_of_the_same_length(self):
        baseline = bf.empty_baseline(row_number=20)
        document, _ = bf.pbind([hist("D5:O5"), hist("B6:B17", form="b"),
                                derived("D8:O8", "ratio", of="D5:O5", over="B6:B17")], baseline)
        self.assertEqual(self.cell(document, "E8"), '=IF(B7>0,E5/B7,"N/A")')
        self.assertEqual(self.cell(document, "O8"), '=IF(B17>0,O5/B17,"N/A")')

    def test_prev_period_first_cell_is_the_text_n_a_and_the_others_divide_by_the_previous_cell(self):
        document, report = bf.pbind(self.BASE + [derived("D8:O8", "prev_period", of="D5:O5")])
        self.assertEqual(document["sheets"][0]["cells"]["D8"], {"content": "N/A"})
        self.assertEqual(self.cell(document, "E8"), '=IF(D5>0,E5/D5,"N/A")')
        self.assertEqual(self.cell(document, "O8"), '=IF(N5>0,O5/N5,"N/A")')
        self.assertEqual(report["derived_cells"], 12)

    def test_prev_period_on_dash_cells_reads_the_helpers(self):
        document, _ = bf.pbind([hist("D5:O5", zero="dash"), derived("D8:O8", "prev_period", of="D5:O5")])
        self.assertEqual(self.cell(document, "D8"), "N/A")
        self.assertEqual(self.cell(document, "E8"), '=IF(D12>0,E12/D12,"N/A")')

    def test_prev_period_goes_by_the_position_inside_the_source_range_not_by_the_target_cell(self):
        document, _ = bf.pbind([hist("D5:O5"), derived("E8:G8", "prev_period", of="K5:M5")])
        self.assertEqual(self.cell(document, "E8"), "N/A")
        self.assertEqual(self.cell(document, "F8"), '=IF(K5>0,L5/K5,"N/A")')
        self.assertEqual(self.cell(document, "G8"), '=IF(L5>0,M5/L5,"N/A")')

    def test_prev_period_over_a_column_range(self):
        baseline = bf.empty_baseline(row_number=20)
        document, _ = bf.pbind([hist("D5:D16"), derived("F5:F16", "prev_period", of="D5:D16")], baseline)
        self.assertEqual(self.cell(document, "F5"), "N/A")
        self.assertEqual(self.cell(document, "F6"), '=IF(D5>0,D6/D5,"N/A")')

    def test_a_one_cell_prev_period_is_just_the_text(self):
        document, _ = bf.pbind(self.BASE + [derived("D8", "prev_period", of="D5")])
        self.assertEqual(document["sheets"][0]["cells"]["D8"], {"content": "N/A"})

    def test_sum_of_number_cells(self):
        document, report = bf.pbind(self.BASE + [derived("P5", "sum", of="D5:O5")])
        self.assertEqual(self.cell(document, "P5"), "=SUM(D5:O5)")
        self.assertEqual(report["derived_cells"], 1)

    def test_sum_of_dash_cells_is_the_helper_range(self):
        document, _ = bf.pbind([hist("D5:O5", zero="dash"), derived("P5", "sum", of="D5:O5")])
        self.assertEqual(self.cell(document, "P5"), "=SUM(D12:O12)")

    def test_sum_of_a_column_of_dash_cells_is_a_contiguous_helper_range_even_with_other_dash_rows(self):
        baseline = bf.empty_baseline(row_number=20)
        spec = [hist("E3", month=1, zero="dash"), hist("E4", month=1, zero="dash"),
                hist("D5", month=1, zero="dash"), hist("D6", month=1, zero="dash"), hist("D7", month=1, zero="dash"),
                hist("D8", month=1, zero="dash"), hist("E9", month=1, zero="dash"),
                derived("P5", "sum", of="D5:D8")]
        document, _ = bf.pbind(spec, baseline)
        # dash rows 3, 4, 5, 6, 7, 8, 9 -> helper rows 22 .. 28 (area rows 20): D5:D8 -> D24:D27
        self.assertEqual(self.cell(document, "P5"), "=SUM(D24:D27)")

    def test_sum_mixing_dash_and_plain_cells_is_refused(self):
        for first, second in (("dash", "number"), ("number", "dash")):
            with self.subTest(first=first):
                self.refuse([hist("D5", month=1, zero=first), hist("E5", month=2, zero=second),
                             derived("P5", "sum", of="D5:E5")],
                            contains="mix cells that show `-` with plain ones")

    def test_sum_over_cells_bound_by_a_list_and_by_a_pivot(self):
        document, _ = bf.pbind([plain("D5:F5", "A"), hist("G5", month=1), derived("P5", "sum", of="D5:G5")])
        self.assertEqual(self.cell(document, "P5"), "=SUM(D5:G5)")

    def test_derived_over_a_list_bound_range(self):
        document, _ = bf.pbind([plain("D5:F5", "A"), plain("D6:F6", "B", zero="dash"),
                                derived("D8:F8", "ratio", of="D5:F5", over="D6:F6"),
                                derived("D9:F9", "prev_period", of="D6:F6"),
                                derived("G5", "sum", of="D6:F6")])
        self.assertEqual(self.cell(document, "D8"), '=IF(D12>0,D5/D12,"N/A")')
        self.assertEqual(self.cell(document, "E9"), '=IF(D12>0,E12/D12,"N/A")')
        self.assertEqual(self.cell(document, "G5"), "=SUM(D12:F12)")

    def test_sum_takes_exactly_one_cell(self):
        self.refuse(self.BASE + [derived("P5:P6", "sum", of="D5:O5")], contains="sum fills ONE cell")
        self.refuse(self.BASE + [derived("D8:E8", "sum", of="D5:O5")], contains="sum fills ONE cell")

    def test_ratio_and_prev_period_need_ranges_of_the_length_of_cells(self):
        for op, fields in (("ratio", {"of": "D5:O5", "over": "D6:N6"}), ("ratio", {"of": "D5:N5", "over": "D6:O6"}),
                           ("ratio", {"of": "D5", "over": "D6"}), ("prev_period", {"of": "D5:N5"})):
            with self.subTest(op=op, fields=fields):
                self.refuse(self.BASE + [derived("D8:O8", op, **fields)],
                            contains="the ranges and `cells` must have the same number of cells")
        extra = hist("P5", month=1, form="c")                         # 13 bound cells D5:P5: one more than `cells`
        self.refuse(self.BASE + [extra, derived("D8:O8", "prev_period", of="D5:P5")],
                    contains="the ranges and `cells` must have the same number of cells")
        self.refuse(self.BASE + [extra, derived("D8:O8", "ratio", of="D5:P5", over="D6:O6")],
                    contains="the ranges and `cells` must have the same number of cells")

    def test_every_cell_of_a_range_must_be_bound_by_a_list_or_pivot_binding(self):
        typed = bf.edit(bf.empty_baseline(), {"F5": {"content": "0"}})
        cases = [("empty cell", self.BASE, "D5:P5", bf.empty_baseline(), "P5"),
                 ("typed cell", [hist("D5", month=1), hist("E5", month=2), hist("G5", month=3)], "D5:G5", typed,
                  "F5"),
                 ("a cell of the other row", self.BASE, "D4:O4", bf.empty_baseline(), "D4")]
        for label, bindings, rng, baseline, missing in cases:
            with self.subTest(case=label):
                message = self.refuse(bindings + [derived("D9", "sum", of=rng)], baseline,
                                      contains="must be cells bound by a list or pivot binding")
                self.assertIn("%s is not" % missing, message)
                self.assertIn("`of`", message)

    def test_the_first_unbound_cell_in_reading_order_is_named(self):
        message = self.refuse([hist("D5", month=1), hist("H5", month=2), derived("D9", "sum", of="D5:H5")],
                              contains="E5 is not")
        self.assertNotIn("F5", message)

    def test_a_missing_cell_of_over_is_named_with_its_field(self):
        message = self.refuse(self.BASE + [derived("D8:O8", "ratio", of="D5:O5", over="D7:O7")],
                              contains="`over` must be cells bound")
        self.assertIn("D7 is not", message)

    def test_when_both_ranges_are_unbound_the_first_field_in_name_order_is_reported(self):
        message = self.refuse(self.BASE + [derived("D8:O8", "ratio", of="D4:O4", over="D3:O3")],
                              contains="`of` must be cells bound by a list or pivot binding, D4 is not")
        self.assertNotIn("`over`", message)

    def test_a_derived_cell_is_not_a_source_of_another_derived_cell(self):
        for rest in (derived("D9", "sum", of="D8:E8"), derived("D9:E9", "prev_period", of="D8:E8"),
                     derived("D9:E9", "ratio", of="D8:E8", over="D5:E5")):
            with self.subTest(rest=rest["derived"]["op"]):
                self.refuse(self.BASE + [derived("D8:E8", "ratio", of="D5:E5", over="D6:E6"), rest],
                            contains="must be cells bound by a list or pivot binding")
        self.refuse(self.BASE + [derived("D9", "sum", of="D8:E8"), derived("D8:E8", "ratio", of="D5:E5", over="D6:E6")],
                    contains="must be cells bound by a list or pivot binding")

    def test_a_derived_cell_cannot_read_itself(self):
        self.refuse(self.BASE + [derived("D5", "sum", of="D5:E5")], contains="already bound")


class DerivedShapeTests(bf.PivotCase):
    BASE = [hist("D5:O5"), hist("D6:O6", form="b")]

    def test_the_op_must_be_one_of_the_three(self):
        for op in ("avg", "", "Ratio", "RATIO", None, 5, ["ratio"], True, "ratio "):
            with self.subTest(op=op):
                self.refuse(self.BASE + [{"cells": "D8", "derived": {"op": op, "of": "D5", "over": "D6"}}],
                            contains="`derived` is")

    def test_a_derived_that_is_not_an_object_or_has_no_op_is_refused(self):
        for value in (None, [], "ratio", 5, True, {}, {"of": "D5"}):
            with self.subTest(value=value):
                self.refuse(self.BASE + [{"cells": "D8", "derived": value}], contains="`derived` is")

    def test_the_keys_of_each_operation_are_exactly_these(self):
        cases = [("ratio", {"of": "D5"}), ("ratio", {"over": "D6"}), ("ratio", {}),
                 ("ratio", {"of": "D5", "over": "D6", "extra": "D7"}),
                 ("prev_period", {}), ("prev_period", {"of": "D5", "over": "D6"}),
                 ("prev_period", {"of": "D5", "x": 1}), ("sum", {}), ("sum", {"of": "D5", "over": "D6"}),
                 ("sum", {"of": "D5", "previous": "D6"})]
        for op, fields in cases:
            with self.subTest(op=op, fields=fields):
                self.refuse(self.BASE + [derived("D8", op, **fields)], contains="the %s operation takes exactly" % op)

    def test_a_derived_binding_has_only_cells_derived_and_overwrite_text(self):
        for key, value in (("source", "hist"), ("where", {}), ("year", "current"), ("zero", "dash"),
                           ("columns", "months"), ("measure", "qty"), ("month", 1), ("filter", {}),
                           ("column", "m1"), ("colour", "red")):
            with self.subTest(key=key):
                binding = derived("D8", "sum", of="D5:O5")
                binding[key] = value
                self.refuse(self.BASE + [binding], contains="known keys" if key == "colour" else "has only `cells`")

    def test_the_cells_of_a_derived_binding_follow_the_cell_rules(self):
        for cells in (None, 5, "D8:E9", "ZZ", "O8:D8"):
            with self.subTest(cells=cells):
                self.refuse(self.BASE + [{"cells": cells, "derived": {"op": "sum", "of": "D5:O5"}}])

    def test_the_source_ranges_follow_the_cell_rules(self):
        for rng in (None, 5, "D5:E6", "ZZ", "O5:D5", ["D5"]):
            with self.subTest(rng=rng):
                self.refuse(self.BASE + [{"cells": "D8", "derived": {"op": "sum", "of": rng}}])

    def test_a_derived_target_is_checked_for_area_overlap_merge_and_label(self):
        self.refuse(self.BASE + [derived("D11", "sum", of="D5:O5")], contains="D11 is outside the design area")
        self.refuse(self.BASE + [derived("Q5", "sum", of="D5:O5")], contains="Q5 is outside the design area")
        self.refuse(self.BASE + [derived("D8", "sum", of="D5:O5"), derived("D8", "sum", of="D6:O6")],
                    contains="D8 is already bound by another binding")
        self.refuse(self.BASE + [derived("D8:F8", "prev_period", of="D5:F5"), derived("F8", "sum", of="D6:O6")],
                    contains="F8 is already bound")
        self.refuse(self.BASE + [derived("D5", "sum", of="D6:O6")], contains="D5 is already bound")
        for bindings in ([bf.plain("D5:F5", "A"), derived("E5", "sum", of="D5:F5")],
                         [derived("E5", "sum", of="D5:F5"), bf.plain("D5:F5", "A")]):
            with self.subTest(first=list(bindings[0])[:2]):
                self.refuse(bindings, contains="E5 is already bound by another binding")
        baseline = bf.make_baseline(cells={"A5": {"content": "Row label one"}})
        self.refuse(self.BASE + [derived("B1", "sum", of="D5:O5")], baseline, contains="inside the merge A1:C1")
        self.refuse(self.BASE + [derived("P9", "sum", of="D5:O5")], baseline, contains="inside the merge P8:P9")
        message = self.refuse(self.BASE + [derived("A5", "sum", of="D5:O5")], baseline,
                              contains="A5 holds the text 'Row label one'")
        self.assertIn('"overwrite_text": true', message)

    def test_overwrite_text_works_on_a_derived_binding_and_only_when_true(self):
        baseline = bf.make_baseline(cells={"A5": {"content": "Row label one"}}, merges=[])
        binding = derived("A5", "sum", of="D5:O5")
        binding["overwrite_text"] = True
        document, _ = bf.pbind(self.BASE + [binding], baseline)
        self.assertEqual(self.cell(document, "A5"), "=SUM(D5:O5)")
        for value in ("true", 1, "yes"):
            binding["overwrite_text"] = value
            with self.subTest(overwrite_text=value):
                self.refuse(self.BASE + [binding], baseline, contains="holds the text")

    def test_a_first_prev_period_cell_on_a_text_label_follows_the_label_rule(self):
        baseline = bf.make_baseline(cells={"D8": {"content": "Heading"}}, merges=[])
        self.refuse(self.BASE + [derived("D8:F8", "prev_period", of="D5:F5")], baseline, contains="D8 holds the text")
        binding = derived("D8:F8", "prev_period", of="D5:F5")
        binding["overwrite_text"] = True
        document, _ = bf.pbind(self.BASE + [binding], baseline)
        self.assertEqual(self.cell(document, "D8"), "N/A")

    def test_derived_binding_count_is_part_of_the_500_binding_cap(self):
        bindings = [hist("D5:O5")] + [derived("D%d" % (6 + i % 4), "sum", of="D5:O5") for i in range(500)]
        self.refuse(bindings, contains="1..500 bindings")


class DerivedGuardTests(bf.PivotCase):
    def test_a_baseline_formula_may_not_read_a_derived_cell(self):
        for formula in ("=D8*2", "=SUM(D8:F8)", "=E8+1", "=SUM(A8:P8)", "=$D$8"):
            with self.subTest(formula=formula):
                baseline = bf.edit(bf.empty_baseline(), {"H9": {"content": formula}})
                message = self.refuse([hist("D5:O5"), hist("D6:O6", form="b"),
                                       derived("D8:F8", "ratio", of="D5:F5", over="D6:F6")], baseline,
                                      contains="a cell that shows `-` on zero or `N/A`")
                self.assertIn("the formula of H9 reads", message)

    def test_the_message_names_the_formula_cell_and_the_derived_cell(self):
        baseline = bf.edit(bf.empty_baseline(), {"H9": {"content": "=F8+1"}})
        message = self.refuse([hist("D5:O5"), derived("D8:F8", "prev_period", of="D5:F5")], baseline)
        self.assertEqual(message, "the formula of H9 reads F8, a cell that shows `-` on zero or `N/A`: bind F8 with "
                                  "\"zero\": \"number\" or change that formula")

    def test_the_first_prev_period_cell_with_its_text_is_guarded_too(self):
        baseline = bf.edit(bf.empty_baseline(), {"H9": {"content": "=D8+1"}})
        self.refuse([hist("D5:O5"), derived("D8:F8", "prev_period", of="D5:F5")], baseline,
                    contains="the formula of H9 reads D8")

    def test_a_formula_next_to_the_derived_cells_is_fine(self):
        baseline = bf.edit(bf.empty_baseline(), {"H9": {"content": "=D5+SUM(D6:F6)"}, "H10": {"content": "=C8+G8"}})
        bf.pbind([hist("D5:O5"), hist("D6:O6", form="b"), derived("D8:F8", "ratio", of="D5:F5", over="D6:F6")], baseline)

    def test_a_derived_target_that_held_a_formula_reading_a_derived_cell_is_replaced_without_a_complaint(self):
        baseline = bf.edit(bf.empty_baseline(), {"D8": {"content": "=D9+1"}})
        bf.pbind([hist("D5:O5"), derived("D8", "sum", of="D5:O5"), derived("D9", "sum", of="D5:O5")], baseline)

    def test_a_pivot_target_that_held_a_formula_reading_a_dash_cell_is_replaced_without_a_complaint(self):
        baseline = bf.edit(bf.empty_baseline(), {"D8": {"content": "=D5+1"}})
        document, _ = bf.pbind([hist("D5:O5", zero="dash"), hist("D8", month=1, form="b")], baseline)
        self.assertTrue(self.cell(document, "D8").startswith("=IFERROR(ODOO.PIVOT("))
        baseline = bf.edit(bf.empty_baseline(), {"D8": {"content": "=D5+1"}, "E8": {"content": "=D5+1"}})
        self.refuse([hist("D5:O5", zero="dash"), hist("D8", month=1, form="b")], baseline, contains="reads D5")

    def test_a_baseline_formula_may_not_read_a_pivot_dash_cell_but_may_read_a_number_cell(self):
        baseline = bf.edit(bf.empty_baseline(), {"H9": {"content": "=D5+1"}})
        self.refuse([hist("D5:O5", zero="dash")], baseline, contains="the formula of H9 reads D5")
        document, _ = bf.pbind([hist("D5:O5")], baseline)
        self.assertEqual(self.cell(document, "H9"), "=D5+1")

    def test_the_default_baseline_row_7_reads_row_6(self):
        baseline = bf.make_baseline()                      # D7 = D6*2, P7 = SUM(D6:O6)
        bf.pbind([hist("D5:O5"), hist("D6:O6", form="b")], baseline)
        self.refuse([hist("D5:O5"), hist("D6:O6", form="b", zero="dash")], baseline, contains="reads D6")

    def test_the_guard_checks_formulas_with_a_range_through_a_derived_cell(self):
        baseline = bf.edit(bf.empty_baseline(), {"H9": {"content": "=SUM(C8:E8)"}})
        self.refuse([hist("D5:O5"), derived("E8", "sum", of="D5:O5")], baseline, contains="reads E8")

    def test_a_formula_inside_a_string_literal_is_not_a_read(self):
        baseline = bf.edit(bf.empty_baseline(), {"H9": {"content": '="D8"&"E8"'}})
        bf.pbind([hist("D5:O5"), derived("D8:E8", "prev_period", of="D5:E5")], baseline)


class RealCatalogPivotTests(unittest.TestCase):
    """The dashboard shape of the real-browser check, over the REAL catalog (`qms_history`)."""

    @classmethod
    def setUpClass(cls):
        spec = bf.make_spec([
            {"cells": "D5:O5", "source": "qms_history", "where": {"form_type": "pqc"}, "zero": "dash"},
            {"cells": "D6:O6", "source": "qms_history", "where": {"form_type": "pqc"}, "year": "previous",
             "zero": "dash"},
            {"cells": "D7", "source": "qms_history", "where": {"form_type": "oqc"}, "measure": "ng_qty", "month": 10,
             "filter": {"overall_result": ["OK", "Pass"]}},
            derived("D8", "sum", of="D5:O5"),
            derived("D9:O9", "prev_period", of="D5:O5"),
            derived("D10:O10", "ratio", of="D5:O5", over="D6:O6")])
        cls.document, cls.report = bf.bind(spec, bf.empty_baseline(), bf.real_catalog())

    def cell(self, ref):
        return self.document["sheets"][0]["cells"][ref]["content"]

    def test_the_report(self):
        self.assertEqual((self.report["pivots"], self.report["lists"], self.report["bound_cells"],
                          self.report["dash_cells"], self.report["helper_rows"], self.report["derived_cells"]),
                         (2, 0, 25, 24, 2, 25))

    def test_the_pivots(self):
        pivots = self.document["pivots"]
        self.assertEqual(list(pivots), ["1", "2", "3"])
        self.assertEqual(pivots["1"]["domain"], [["form_type", "=", "pqc"]])
        self.assertEqual(pivots["2"]["domain"], [["form_type", "=", "oqc"], ["overall_result", "in", ["OK", "Pass"]]])
        self.assertEqual(pivots["2"]["measures"], [{"field": "ng_qty"}])
        self.assertEqual(pivots["3"]["domain"], [])
        self.assertEqual(pivots["3"]["fieldMatching"], {"kpi-year-filter": {"chain": "check_date", "type": "date"}})
        for pivot in pivots.values():
            self.assertEqual((pivot["model"], pivot["colGroupBys"], pivot["rowGroupBys"]),
                             ("mes.qc_form.history", ["check_date:month"], []))

    def test_the_cells(self):
        self.assertEqual(self.cell("A14"), '=IFERROR(YEAR(ODOO.PIVOT.HEADER(3,"#check_date:month",1)),0)')
        self.assertEqual(self.cell("D5"), '=IF(D12=0,"-",D12)')
        self.assertEqual(self.cell("D12"), '=IFERROR(ODOO.PIVOT(1,"__count","check_date:month","01/"&$A$14)+0,0)')
        self.assertEqual(self.cell("D13"), '=IFERROR(ODOO.PIVOT(1,"__count","check_date:month","01/"&($A$14-1))+0,0)')
        self.assertEqual(self.cell("D7"), '=IFERROR(ODOO.PIVOT(2,"ng_qty","check_date:month","10/"&$A$14)+0,0)')
        self.assertEqual(self.cell("D8"), "=SUM(D12:O12)")
        self.assertEqual(self.cell("D9"), "N/A")
        self.assertEqual(self.cell("E9"), '=IF(D12>0,E12/D12,"N/A")')
        self.assertEqual(self.cell("D10"), '=IF(D13>0,D12/D13,"N/A")')
        self.assertEqual(self.document["sheets"][0]["rowNumber"], 14)


class PivotCliTests(IsolatedTestCase):
    def test_the_command_line_binds_a_pivot_spec_with_the_default_catalog(self):
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        baseline_bytes = intake_xlsx.dump_bytes(bf.empty_baseline())
        spec = bf.make_spec([{"cells": "D5:O5", "source": "qms_history", "where": {"form_type": "iqc"}, "zero": "dash"},
                             derived("P5", "sum", of="D5:O5")])
        paths = {name: os.path.join(work, name) for name in ("baseline.json", "spec.json", "out.json")}
        with open(paths["baseline.json"], "wb") as handle:
            handle.write(baseline_bytes)
        with open(paths["spec.json"], "w", encoding="utf-8") as handle:
            json.dump(spec, handle)
        out, err = io.StringIO(), io.StringIO()
        rc = bind_kpi.main(["--baseline", paths["baseline.json"], "--spec", paths["spec.json"], "--out",
                            paths["out.json"]], out, err)
        self.assertEqual((rc, err.getvalue()), (0, ""))
        report = json.loads(out.getvalue())
        self.assertEqual((report["pivots"], report["derived_cells"], report["bound_cells"], report["dash_cells"]),
                         (1, 1, 12, 12))
        with open(paths["out.json"], "rb") as handle:
            written = json.loads(handle.read().decode("utf-8"))
        self.assertEqual(sorted(written["pivots"]), ["1", "2"])
        self.assertEqual(written["pivots"]["1"]["domain"], [["form_type", "=", "iqc"]])
        self.assertEqual(written["sheets"][0]["cells"]["P5"]["content"], "=SUM(D12:O12)")

    def test_a_bad_pivot_binding_is_a_clean_error_line_and_writes_nothing(self):
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        with open(os.path.join(work, "baseline.json"), "wb") as handle:
            handle.write(intake_xlsx.dump_bytes(bf.empty_baseline()))
        spec = bf.make_spec([{"cells": "D5:O5", "source": "qms_history", "where": {"form_type": "nope"}}])
        with open(os.path.join(work, "spec.json"), "w", encoding="utf-8") as handle:
            json.dump(spec, handle)
        out, err = io.StringIO(), io.StringIO()
        rc = bind_kpi.main(["--baseline", os.path.join(work, "baseline.json"), "--spec", os.path.join(work, "spec.json"),
                            "--out", os.path.join(work, "out.json")], out, err)
        self.assertEqual((rc, out.getvalue()), (2, ""))
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("form_type='nope' is not allowed", err.getvalue())
        self.assertFalse(os.path.exists(os.path.join(work, "out.json")))


class MixedSpecDocumentTests(bf.PivotCase):
    def test_every_cell_of_the_output_is_a_formula_or_a_baseline_label(self):
        document, _ = bf.pbind([hist("D5:O5", zero="dash"), hist("D6:O6", form="b"),
                                derived("D8:O8", "ratio", of="D5:O5", over="D6:O6"),
                                derived("D9:O9", "prev_period", of="D6:O6"), derived("P5", "sum", of="D5:O5")])
        for ref, cell in document["sheets"][0]["cells"].items():
            content = cell.get("content")
            if content is not None:
                self.assertTrue(content.startswith("=") or content == "N/A" or not content[:1].isdigit(), (ref, content))

    def test_a_pivot_dashboard_has_no_function_the_client_does_not_register(self):
        document, _ = bf.pbind([hist("D5:O5"), derived("D8:O8", "prev_period", of="D5:O5"),
                                derived("P5", "sum", of="D5:O5")])
        self.assertEqual(document["version"], 14.5)

    def test_the_hidden_row_marks_cover_every_helper_and_year_row_and_no_other(self):
        document, _ = bf.pbind([hist("D5:O5", zero="dash"), hist("D6:O6", form="b", zero="dash"), bf.other("D7:O7")])
        hidden = sorted(int(r) for r, v in document["sheets"][0]["rows"].items() if v.get("isHidden"))
        self.assertEqual(hidden, [11, 12, 13, 14])                 # helper rows 12, 13 + year rows 14, 15
        self.assertEqual(document["sheets"][0]["rowNumber"], 15)


if __name__ == "__main__":
    unittest.main()
