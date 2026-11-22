"""Catalog aliases: `bind_kpi.normalize_alias`, `bind_kpi.check_aliases` and their call from `load_catalog`.

Plan kpi-skill-improvements step 3 (B2, AC4): aliases are label texts used to MATCH the rows of an Excel (scan_excel.py);
they never reach a formula or a domain, but they are checked anyway: a known selector and value, at most 20 short
single-line strings per value, and no text (after normalisation) shared by two values of one selector.
"""
import copy
import json
import os
import re
import unicodedata
import unittest

from helpers import IsolatedTestCase

import bind_kpi
import kpi_catalogs

HANGUL = re.compile(r"[가-힣]")
ENGLISH_NAME = re.compile(r"[A-Za-z0-9 ]+")
DECK_NAMES = {"report_group": {"group_code": {"DK_REF": "DK 냉장고", "DK_AC": "DK 에어컨", "DK_ETC": "DK 기타",
                                              "SK_WM": "SK 세탁기", "SK_ETC": "SK 기타", "SZDK_REF": "SZ DK 냉장고",
                                              "SZDK_WM": "SZ DK 세탁기"},
                               "kind": {"shipped": "출하 수량", "produced": "생산 수량"}},
              "production_group": {"group_code": {"DK_REF": "DK 냉장고", "DK_AC": "DK 에어컨", "DK_ETC": "DK 기타",
                                                  "SK_WM": "SK 세탁기", "SK_ETC": "SK 기타",
                                                  "SZDK_REF": "SZ DK 냉장고"}}}


def small_source():
    return {"selectors": {"code": {"values": ["A", "B", "C"]}, "kind": {"values": ["x", "y"]},
                          "n": {"min": 1, "max": 3}}}


def shipped():
    return bind_kpi.load_catalog(project="dksk")


class NormalizeAliasTests(unittest.TestCase):
    def test_texts_that_must_be_one_text(self):
        for left, right in (("SZ DK 냉장고", "SZDK 냉장고"), ("ＳＺＤＫ", "szdk"), ("SZ DK", "SZDK"),
                            ("SZ　DK", "SZDK"), ("SZ\tDK\n", "SZDK"), ("SZ DK REF", "szdkref"),
                            ("DK ＲＥＦ", "dk ref"), ("Straße", "STRASSE"), ("①", "1"), ("ﬁsh", "FISH"),
                            ("Ⅳ", "iv"), ("ｶﾞ", "ガ"), (unicodedata.normalize("NFD", "냉장고"), "냉장고"),
                            (unicodedata.normalize("NFD", "Tháng"), "tháng"), ("  a  b  ", "ab")):
            with self.subTest(left=left, right=right):
                self.assertEqual(bind_kpi.normalize_alias(left), bind_kpi.normalize_alias(right))

    def test_the_result_is_the_nfkc_whitespace_free_casefolded_text(self):
        self.assertEqual(bind_kpi.normalize_alias("ＳＺ DK 냉장고"), "szdk냉장고")
        self.assertEqual(bind_kpi.normalize_alias("Straße"), "strasse")
        self.assertEqual(bind_kpi.normalize_alias("①"), "1")
        self.assertEqual(bind_kpi.normalize_alias(" \t\n 　 "), "")
        self.assertEqual(bind_kpi.normalize_alias(""), "")

    def test_texts_that_must_stay_different(self):
        for left, right in (("DK 냉장고", "DK 에어컨"), ("SK WM", "SK ETC"), ("DK REF", "SK REF"), ("a1", "al"),
                            ("냉장고", "냉 장 고 기타")):
            with self.subTest(left=left, right=right):
                self.assertNotEqual(bind_kpi.normalize_alias(left), bind_kpi.normalize_alias(right))

    def test_normalising_twice_changes_nothing(self):
        for text in ("ＳＺ DK 냉장고", "Straße", "①②", unicodedata.normalize("NFD", "세탁기"), "A B"):
            once = bind_kpi.normalize_alias(text)
            self.assertEqual(bind_kpi.normalize_alias(once), once)


class CheckAliasesTests(unittest.TestCase):
    def check(self, aliases, source=None):
        return bind_kpi.check_aliases("small", source or small_source(), aliases)

    def refused(self, aliases, fragment="", source=None):
        with self.assertRaises(bind_kpi.BindError) as raised:
            self.check(aliases, source)
        self.assertIn(fragment, str(raised.exception))
        self.assertIn("small", str(raised.exception))
        return str(raised.exception)

    def test_valid_aliases_pass(self):
        for aliases in ({}, {"code": {}}, {"code": {"A": []}}, {"code": {"A": ["a text"]}},
                        {"code": {"A": ["one"], "B": ["two", "three"]}, "kind": {"x": ["one"]}},
                        {"code": {"A": ["한글 이름", "Tên tiếng Việt", "English name"]}}):
            with self.subTest(aliases=aliases):
                self.assertIsNone(self.check(aliases))

    def test_the_same_text_may_be_used_under_two_selectors_or_in_two_sources(self):
        self.assertIsNone(self.check({"code": {"A": ["shared"]}, "kind": {"x": ["shared"]}}))
        self.assertIsNone(bind_kpi.check_aliases("other", small_source(), {"code": {"B": ["shared"]}}))

    def test_aliases_that_are_not_an_object_are_refused(self):
        for aliases in ([], "a", None, 5, [["code"]]):
            with self.subTest(aliases=aliases):
                self.refused(aliases, "must be an object")

    def test_an_unknown_selector_or_one_without_a_closed_list_is_refused(self):
        self.refused({"nope": {"A": ["a"]}}, "selector the source does not have")
        self.refused({"n": {"1": ["a"]}}, "selector the source does not have")           # min / max, no `values`
        self.refused({5: {"A": ["a"]}}, "selector the source does not have")
        self.refused({"code": ["A"]}, "selector the source does not have")                # not {value: [text]}
        self.refused({"code": "A"}, "selector the source does not have")

    def test_an_unknown_value_is_refused(self):
        self.refused({"code": {"Z": ["a"]}}, "is not a value of the selector")
        self.refused({"code": {"a": ["a"]}}, "is not a value of the selector")            # case matters: values are codes
        self.refused({"kind": {"A": ["a"]}}, "is not a value of the selector")

    def test_texts_that_are_not_a_list_are_refused(self):
        for texts in ("a text", "abc", None, 5, {"a": "b"}, ("a", "b")):
            with self.subTest(texts=texts):
                self.refused({"code": {"A": texts}}, "is not a value of the selector (or holds more than 20 texts)")

    def test_twenty_texts_pass_and_twenty_one_do_not(self):
        twenty = ["text %d" % number for number in range(20)]
        self.assertIsNone(self.check({"code": {"A": twenty}}))
        self.refused({"code": {"A": twenty + ["text 20"]}}, "holds more than 20 texts")

    def test_sixty_characters_pass_and_sixty_one_do_not(self):
        self.assertIsNone(self.check({"code": {"A": ["x" * 60]}}))
        self.assertIsNone(self.check({"code": {"A": ["냉" * 60]}}))
        self.refused({"code": {"A": ["x" * 61]}}, "not a short single-line text")
        self.refused({"code": {"A": ["냉" * 61]}}, "not a short single-line text")

    def test_the_length_is_the_raw_length_not_the_normalised_one(self):
        self.refused({"code": {"A": ["x " * 31]}}, "not a short single-line text")        # 62 raw, 31 normalised

    def test_a_text_that_is_not_a_plain_string_is_refused(self):
        for text in (5, 1.5, None, True, ["a"], {"a": 1}, b"bytes"):
            with self.subTest(text=text):
                self.refused({"code": {"A": [text]}}, "not a short single-line text")

    def test_blank_texts_are_refused(self):
        for text in ("", " ", "  \t ", "　", "  "):
            with self.subTest(text=text):
                self.refused({"code": {"A": [text]}}, "not a short single-line text")

    def test_control_characters_are_refused_a_newline_and_a_tab_included(self):
        for text in ("a\x00b", "a\x07b", "a\nb", "a\rb", "a\tb", "a\x1bb", "\x1b[31mred", "a\x7fb", "a\x85b",
                     "a\x9fb", "tail\n"):
            with self.subTest(text=text):
                self.refused({"code": {"A": [text]}}, "not a short single-line text")

    def test_a_text_shared_by_two_values_of_one_selector_is_refused_after_normalisation(self):
        for label, aliases in (("exact", {"code": {"A": ["same"], "B": ["same"]}}),
                               ("case", {"code": {"A": ["Same"], "B": ["sAME"]}}),
                               ("spacing", {"code": {"A": ["SZ DK"], "B": ["SZDK"]}}),
                               ("nbsp and ideographic space", {"code": {"A": ["a b"], "B": ["a　b"]}}),
                               ("width", {"code": {"A": ["ＤＫ"], "B": ["dk"]}}),
                               ("nfd and nfc", {"code": {"A": ["냉장고"],
                                                          "B": [unicodedata.normalize("NFD", "냉장고")]}}),
                               ("late in a long list", {"code": {"A": ["t%d" % n for n in range(10)] + ["dup"],
                                                                 "B": ["u%d" % n for n in range(10)] + ["DUP"]}}),
                               ("three values", {"code": {"A": ["x1"], "B": ["x2"], "C": ["X 1"]}}),
                               ("kind", {"kind": {"x": ["shared"], "y": ["SHARED"]}})):
            with self.subTest(case=label):
                self.refused(aliases, "belongs to two values")

    def test_the_duplicate_message_names_both_values_and_cuts_a_long_text(self):
        message = self.refused({"code": {"A": ["z" * 60], "B": ["Z" * 60]}}, "belongs to two values of code (A and B)")
        self.assertNotIn("z" * 41, message)
        self.assertNotIn("Z" * 41, message)

    def test_the_error_text_cuts_attacker_text_and_never_holds_a_raw_control_character(self):
        for aliases, fragment in (({"s" * 100: {"A": ["a"]}}, "selector"), ({"\x1b[31mevil": {"A": ["a"]}}, "selector"),
                                  ({"code": {"v" * 100: ["a"]}}, "value"), ({"code": {"\x1b[0mv": ["a"]}}, "value"),
                                  ({"code": {"A": ["a"], "B": ["\x1b"]}}, "single-line")):
            with self.subTest(aliases=list(aliases)):
                message = self.refused(aliases, fragment)
                self.assertNotIn("s" * 41, message)
                self.assertNotIn("v" * 41, message)
                self.assertEqual([c for c in message if ord(c) < 32 or 0x7F <= ord(c) <= 0x9F], [])

    def test_a_long_source_name_is_cut_in_the_message(self):
        with self.assertRaises(bind_kpi.BindError) as raised:
            bind_kpi.check_aliases("n" * 100, small_source(), {"nope": {}})
        self.assertNotIn("n" * 41, str(raised.exception))


class LoadCatalogAliasesTests(IsolatedTestCase):
    def catalog_path(self, mutate=None):
        with open(kpi_catalogs.catalog_path("dksk"), "rb") as handle:
            catalog = json.loads(handle.read().decode("utf-8"))
        if mutate:
            mutate(catalog)
        path = os.path.join(self.tmp, "catalog.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(catalog, handle, ensure_ascii=False)
        return path

    def load(self, mutate=None):
        return bind_kpi.load_catalog(self.catalog_path(mutate))

    def refused(self, mutate, fragment=""):
        with self.assertRaises(bind_kpi.BindError) as raised:
            self.load(mutate)
        self.assertIn("aliases", str(raised.exception))
        self.assertIn(fragment, str(raised.exception))
        return str(raised.exception)

    def group(self, catalog, source="report_group", selector="group_code"):
        return catalog["sources"][source]["aliases"][selector]

    def test_an_unchanged_copy_of_the_shipped_catalog_loads(self):
        self.assertEqual(self.load()["sources"]["report_group"]["aliases"], shipped()["sources"]["report_group"]["aliases"])

    def test_a_catalog_without_aliases_still_loads(self):
        def drop(catalog):
            for source in catalog["sources"].values():
                source.pop("aliases", None)
        self.assertNotIn("aliases", self.load(drop)["sources"]["report_group"])

    def test_each_refusal_class_is_raised_by_load_catalog(self):
        def put(texts, value="DK_REF", source="report_group", selector="group_code"):
            def mutate(catalog):
                catalog["sources"][source]["aliases"][selector][value] = texts
            return mutate

        def selector_named(name):
            def mutate(catalog):
                catalog["sources"]["report_group"]["aliases"][name] = {"DK_REF": ["a"]}
            return mutate

        def aliases_are(value):
            def mutate(catalog):
                catalog["sources"]["report_group"]["aliases"] = value
            return mutate

        def on_a_range_selector(catalog):
            catalog["sources"]["process_month"]["aliases"] = {"row_no": {"1": ["first row"]}}

        def duplicate(catalog):
            self.group(catalog)["DK_AC"].append(" dk  ref ")                # DK_REF already has "DK REF"
        cases = (("unknown selector", selector_named("nope"), "selector the source does not have"),
                 ("a selector with min / max", on_a_range_selector, "selector the source does not have"),
                 ("unknown value", put(["a"], "NOT_A_GROUP"), "is not a value of the selector"),
                 ("value of another selector", put(["a"], "shipped"), "is not a value of the selector"),
                 ("texts not a list", put("a text"), "is not a value of the selector"),
                 ("21 texts", put(["t%d" % n for n in range(21)]), "holds more than 20 texts"),
                 ("61 characters", put(["x" * 61]), "not a short single-line text"),
                 ("control character", put(["a\u0007b"]), "not a short single-line text"),
                 ("newline", put(["a\nb"]), "not a short single-line text"),
                 ("not a string", put([5]), "not a short single-line text"),
                 ("blank", put(["   "]), "not a short single-line text"),
                 ("duplicate under two values", duplicate, "belongs to two values"),
                 ("aliases not an object", aliases_are(["x"]), "must be an object"),
                 ("aliases on the production source too", put(["x" * 61], "DK_REF", "production_group"),
                  "not a short single-line text"))
        for label, mutate, fragment in cases:
            with self.subTest(case=label):
                message = self.refused(mutate, fragment)
                self.assertIn("the catalog source", message)

    def test_the_limits_themselves_pass_through_load_catalog(self):
        def mutate(catalog):
            values = self.group(catalog)
            values["DK_REF"] = ["Exactly%d" % n for n in range(19)] + ["B" * 60]
        catalog = self.load(mutate)
        self.assertEqual(len(self.group(catalog)["DK_REF"]), 20)

    def test_the_error_is_a_bind_error_with_the_source_name_and_no_traceback_material(self):
        def duplicate(catalog):
            self.group(catalog, "production_group")["SK_WM"].append("SK 기타")
        message = self.refused(duplicate, "belongs to two values")
        self.assertIn("'production_group'", message)
        self.assertTrue("(SK_ETC and SK_WM)" in message or "(SK_WM and SK_ETC)" in message, message)


class ShippedAliasesTests(unittest.TestCase):
    def aliased(self):
        return {name: source for name, source in shipped()["sources"].items() if "aliases" in source}

    def test_the_two_group_sources_carry_aliases(self):
        self.assertTrue({"report_group", "production_group"} <= set(self.aliased()))

    def test_the_shipped_aliases_pass_the_checker_one_source_at_a_time(self):
        for name, source in self.aliased().items():
            with self.subTest(source=name):
                self.assertIsNone(bind_kpi.check_aliases(name, source, copy.deepcopy(source["aliases"])))

    def test_every_shipped_alias_normalises_uniquely_per_source_and_selector(self):
        for name, source in self.aliased().items():
            for selector, by_value in source["aliases"].items():
                with self.subTest(source=name, selector=selector):
                    keys = [bind_kpi.normalize_alias(text) for texts in by_value.values() for text in texts]
                    self.assertEqual(len(keys), len(set(keys)), sorted(keys))
                    for texts in by_value.values():
                        self.assertLessEqual(len(texts), bind_kpi.ALIAS_MAX_COUNT)
                        for text in texts:
                            self.assertIsInstance(text, str)
                            self.assertLessEqual(len(text), bind_kpi.ALIAS_MAX_LEN)
                            self.assertIsNone(bind_kpi.CONTROL.search(text))

    def test_every_selector_of_an_aliased_source_is_aliased_for_every_value(self):
        # a scan candidate needs EVERY selector of its source resolved, so a missing value would make a group unreachable
        for name in ("report_group", "production_group"):
            source = shipped()["sources"][name]
            self.assertEqual(set(source["aliases"]), set(source["selectors"]))
            for selector, rule in source["selectors"].items():
                with self.subTest(source=name, selector=selector):
                    self.assertEqual(set(source["aliases"][selector]), set(rule["values"]))
                    for value in rule["values"]:
                        self.assertTrue(source["aliases"][selector][value], value)

    def test_every_value_has_the_korean_name_of_the_design_deck_and_an_english_name(self):
        for name in ("report_group", "production_group"):
            source = shipped()["sources"][name]
            for selector, by_value in source["aliases"].items():
                for value, texts in by_value.items():
                    with self.subTest(source=name, selector=selector, value=value):
                        self.assertTrue(any(HANGUL.search(text) for text in texts), texts)
                        self.assertTrue(any(ENGLISH_NAME.fullmatch(text) and re.search(r"[a-z]{3}", text)
                                            for text in texts), texts)

    def test_the_korean_names_of_the_design_deck_are_aliases_of_their_values(self):
        for name, selectors in DECK_NAMES.items():
            source = shipped()["sources"][name]
            for selector, by_value in selectors.items():
                for value, deck_name in by_value.items():
                    with self.subTest(source=name, selector=selector, value=value):
                        known = {bind_kpi.normalize_alias(text) for text in source["aliases"][selector][value]}
                        self.assertIn(bind_kpi.normalize_alias(deck_name), known)
                        self.assertIn(bind_kpi.normalize_alias(unicodedata.normalize("NFD", deck_name)), known)

    def test_the_deck_names_cover_every_value_of_both_group_sources(self):
        for name, selectors in DECK_NAMES.items():
            source = shipped()["sources"][name]
            for selector, by_value in selectors.items():
                self.assertEqual(set(by_value), set(source["selectors"][selector]["values"]))
            self.assertEqual(set(selectors), set(source["selectors"]))

    def test_a_text_may_be_shared_by_the_two_group_sources_but_not_by_two_values(self):
        report = shipped()["sources"]["report_group"]["aliases"]["group_code"]
        production = shipped()["sources"]["production_group"]["aliases"]["group_code"]
        for value in production:
            self.assertEqual({bind_kpi.normalize_alias(t) for t in production[value]},
                             {bind_kpi.normalize_alias(t) for t in report[value]})


if __name__ == "__main__":
    unittest.main()
