"""scripts/scan_excel.py: the scan proposes which rows of the QA/QC's Excel bind to which catalog source.

Plan kpi-skill-improvements step 3 (B2): AC3 (a synthetic twin of the real design sheet gives the 14 expected bindings,
the year-label WARNING and an accepted dry run) and AC4 (every refusal). Every sheet is built in code (`scan_fixtures`).
Every CLI run goes through `ScanCase.cli`, which fails the test when a traceback, a control / escape character or a
network call reaches it.
"""
import contextlib
import copy
import fnmatch
import io
import json
import os
import socket
import unittest
from unittest import mock

import scan_fixtures as sf
from helpers import REPO, IsolatedTestCase
from test_repo_hygiene import WORKFLOW_OUTPUT_PATTERNS, gitignore_patterns, workflow_outputs

import bind_kpi
import intake_xlsx
import kpi_catalogs
import scan_excel

POSIX_ONLY = unittest.skipIf(os.name == "nt", "modes / symlinks need POSIX here")
DRAFT_KEYS = {"spec_version", "name", "labels", "bindings", "baseline_sha256"}
LONG_ALIAS = ("LongName " * 6).strip()                     # 53 characters
# S1: characters that are not control characters of the old pattern yet break or hide a printed line (name, character)
SEPARATORS = ([("U+2028 line separator", " "), ("U+2029 paragraph separator", " ")]
              + [("U+%04X bidi embedding / override" % code, chr(code)) for code in range(0x202A, 0x202F)]
              + [("U+%04X bidi isolate" % code, chr(code)) for code in range(0x2066, 0x206A)]
              + [("U+%04X zero width / direction mark" % code, chr(code)) for code in range(0x200B, 0x2010)]
              + [("U+FEFF byte order mark", "﻿")])


def control_characters(text):
    return [c for c in text if c != "\n" and (ord(c) < 32 or 0x7F <= ord(c) <= 0x9F)]


def row_lines(out, row):
    return [line for line in out.splitlines() if line.startswith("row %d " % row)]


def simple_grid(labels, title=sf.SHIPPED_TITLE, form="ko", year_text=None, header_year=26):
    """A title row (row 1), the header (row 2), `labels` in rows 3.., a totals row; month columns C..N."""
    grid = sf.Grid(len(labels) + 6, 15)
    sf.block(grid, 1, title, year_text, form, labels=list(labels), header_year=header_year)
    return grid


class ScanCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.baseline_path = os.path.join(self.work, "baseline.json")
        self.out_path = os.path.join(self.work, "draft.json")
        self.streams = []
        self.addCleanup(self.check_streams)

    def check_streams(self):
        for out, err in self.streams:
            for text in (out, err):
                self.assertNotIn("Traceback", text)
                self.assertEqual(control_characters(text), [])

    # ---- helpers
    def write(self, baseline, path=None):
        data = intake_xlsx.dump_bytes(baseline)
        with open(path or self.baseline_path, "wb") as handle:
            handle.write(data)
        return data

    def write_text(self, name, text):
        path = os.path.join(self.work, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def cli(self, argv, out=None, err=None):
        out, err = out or io.StringIO(), err or io.StringIO()
        with mock.patch.object(socket, "socket", side_effect=AssertionError("the scan reached the network")):
            rc = scan_excel.main(argv, out, err)
        self.streams.append((out.getvalue(), err.getvalue()))
        return rc, out.getvalue(), err.getvalue()

    def scan(self, source, *extra, default_out=False):
        baseline = source.baseline() if isinstance(source, sf.Grid) else source
        self.write(baseline)
        if not default_out and os.path.exists(self.out_path):        # no stale draft of an earlier sub-case
            os.remove(self.out_path)
        argv = ["--baseline", self.baseline_path] + ([] if default_out else ["--out", self.out_path]) + list(extra)
        return self.cli(argv)

    def draft(self, path=None):
        with open(path or self.out_path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))

    def files(self):
        return sorted(os.listdir(self.work))

    def bound(self, out):
        """{row: (source, where text)} of the matched lines."""
        return {int(line.split()[1]): line.split(" -> ", 1)[1] for line in out.splitlines()
                if line.startswith("row ") and " -> " in line}

    def where(self, grid_labels, title=sf.SHIPPED_TITLE, **kwargs):
        rc, out, err = self.scan(simple_grid(grid_labels, title=title, **kwargs))
        self.assertEqual((rc, err), (0, ""))
        return out


# ---------------------------------------------------------------- AC3: the synthetic twin of the real sheet

class TwinTests(ScanCase):
    def run_twin(self, **kwargs):
        baseline = sf.twin(**kwargs)
        data = self.write(baseline)
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
        return baseline, data, rc, out, err

    def test_the_draft_bindings_equal_the_fourteen_expected_ones(self):
        _, _, rc, out, err = self.run_twin()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.draft()["bindings"], sf.expected_twin_bindings())
        self.assertEqual(len(self.draft()["bindings"]), 14)

    def test_the_labels_map_holds_the_label_cell_of_each_row_unchanged(self):
        baseline, _, _, _, _ = self.run_twin()
        labels = self.draft()["labels"]
        self.assertEqual(labels, sf.expected_twin_labels())
        cells = baseline["sheets"][0]["cells"]
        for ref, text in labels.items():
            self.assertIn(text, cells[ref]["content"])

    def test_the_draft_has_exactly_the_closed_keys_and_a_constant_name(self):
        _, data, _, _, _ = self.run_twin()
        draft = self.draft()
        self.assertEqual(set(draft), DRAFT_KEYS)
        self.assertEqual(draft["name"], "KPI draft")
        self.assertEqual(draft["spec_version"], bind_kpi.SPEC_VERSION)
        self.assertNotIn("keep_typed", draft)
        self.assertNotIn("intake", draft)

    def test_the_name_is_never_text_from_the_excel(self):
        grid = simple_grid(["DK 냉장고"], title="Quarterly Board Evil Title")      # a real single-cell title, no alias
        rc, out, err = self.scan(grid)
        self.assertIn('block title: "Quarterly Board Evil Title"', out)
        self.assertEqual(self.draft()["name"], "KPI draft")
        self.assertNotIn("Evil", json.dumps(self.draft()))

    def test_the_sha256_is_the_hash_of_the_baseline_file_bytes(self):
        import hashlib
        _, data, _, _, _ = self.run_twin()
        self.assertEqual(self.draft()["baseline_sha256"], hashlib.sha256(data).hexdigest())

    def test_the_year_label_warning_names_the_25_block_and_not_the_26_block(self):
        _, _, _, out, _ = self.run_twin()
        warnings = [line for line in out.splitlines() if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("rows 23-29", warnings[0])
        self.assertIn("2025", warnings[0])
        self.assertIn("2026", warnings[0])

    def test_the_warning_is_on_the_screen_only_never_in_the_spec(self):
        self.run_twin()
        with open(self.out_path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("WARNING", text)
        self.assertNotIn("year label", text)
        self.assertEqual(set(self.draft()), DRAFT_KEYS)

    def test_the_dry_run_binds_168_cells(self):
        _, _, _, out, _ = self.run_twin()
        self.assertIn("bind dry run: ok (168 cells would be bound)", out.splitlines())

    def test_the_report_has_two_headers_two_titles_and_fourteen_matches_with_no_coverage_line(self):
        _, _, _, out, _ = self.run_twin()
        lines = out.splitlines()
        self.assertIn('header row 3: months 1..12 in columns C..N; block title: "출하 수량"', lines)
        self.assertIn('header row 22: months 1..12 in columns C..N; block title: "생산 수량"', lines)
        self.assertEqual(len(self.bound(out)), 14)
        self.assertEqual(sorted(self.bound(out)), list(range(4, 11)) + list(range(23, 30)))
        self.assertEqual([line for line in lines if "ask the user" in line], [])
        self.assertEqual([line for line in lines if line.startswith("row 11") or line.startswith("row 30")], [])

    def test_every_value_of_the_draft_comes_from_the_catalog_closed_lists(self):
        self.run_twin()
        rules = bind_kpi.load_catalog(project="dksk")["sources"]["report_group"]["selectors"]
        for binding in self.draft()["bindings"]:
            self.assertEqual(binding["source"], "report_group")
            self.assertEqual(binding["columns"], "months")
            for selector, value in binding["where"].items():
                self.assertIn(value, rules[selector]["values"])

    def test_the_draft_is_accepted_unchanged_by_the_binder_library_and_cli(self):
        baseline, data, _, _, _ = self.run_twin()
        _, report = bind_kpi.bind(baseline, self.draft(), bind_kpi.load_catalog(project="dksk"), data)
        self.assertEqual(report["bound_cells"], 168)
        dashboard = os.path.join(self.work, "dashboard.json")
        out = io.StringIO()
        rc = bind_kpi.main(["--baseline", self.baseline_path, "--spec", self.out_path, "--out", dashboard], out,
                           io.StringIO())
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["bound_cells"], 168)

    def test_the_written_bytes_are_the_canonical_serialisation_of_the_draft(self):
        self.run_twin()
        with open(self.out_path, "rb") as handle:
            raw = handle.read()
        self.assertEqual(raw, intake_xlsx.dump_bytes(json.loads(raw.decode("utf-8"))))
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r", raw)

    def test_the_twin_gives_the_same_bindings_in_every_month_header_form(self):
        # the twin's block year labels are 26 and 25: a header WITH a year differs from the 25 block, and without a
        # header year the 25 block differs from the first block (Plan B2 7 "differs between blocks or from the header
        # year"), so every header form warns once
        expected_warning = {form: True for form in sf.MONTH_FORMS}
        for form in sf.MONTH_FORMS:
            with self.subTest(form=form):
                _, _, rc, out, err = self.run_twin(forms=form)
                self.assertEqual((rc, err), (0, ""))
                self.assertEqual(self.draft()["bindings"], sf.expected_twin_bindings())
                self.assertEqual(any(line.startswith("WARNING") for line in out.splitlines()), expected_warning[form])
                self.assertIn("bind dry run: ok (168 cells would be bound)", out)
                os.remove(self.out_path)

    def test_the_two_blocks_may_use_two_different_header_forms(self):
        grid = sf.Grid(31, 15)
        sf.block(grid, 2, sf.SHIPPED_TITLE, "26년", "vi")
        sf.block(grid, 21, sf.PRODUCED_TITLE, "26년", "en_short")
        rc, out, err = self.scan(grid)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.draft()["bindings"], sf.expected_twin_bindings())
        self.assertEqual([line for line in out.splitlines() if line.startswith("WARNING")], [])


# ---------------------------------------------------------------- header rows

class HeaderTests(ScanCase):
    def grid_with_header(self, texts, first_col=3):
        grid = sf.Grid(8, 16)
        grid.put(1, 1, sf.SHIPPED_TITLE).header(2, texts, first_col)
        grid.put(3, 2, "DK 냉장고").numbers(3, first_col=first_col)
        return grid

    def assertRefused(self, grid):
        rc, out, err = self.scan(grid)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("no header row with the 12 months in order was found: nothing can be proposed", out)
        self.assertEqual(self.bound(out), {})
        self.assertEqual(self.files(), ["baseline.json"])
        self.assertNotIn("bind dry run", out)

    def test_eleven_months_are_no_header(self):
        self.assertRefused(self.grid_with_header(sf.month_texts("ko")[:11]))

    def test_months_out_of_order_are_no_header(self):
        texts = sf.month_texts("ko")
        texts[4], texts[5] = texts[5], texts[4]
        self.assertRefused(self.grid_with_header(texts))

    def test_a_duplicate_month_is_no_header(self):
        texts = sf.month_texts("ko")
        texts[2] = texts[1]
        self.assertRefused(self.grid_with_header(texts))

    def test_a_rotated_or_reversed_year_is_no_header(self):
        texts = sf.month_texts("ko")
        for label, variant in (("rotated", texts[1:] + texts[:1]), ("reversed", texts[::-1])):
            with self.subTest(order=label):
                self.assertRefused(self.grid_with_header(variant))

    def test_a_gap_inside_the_twelve_cells_is_no_header(self):
        grid = self.grid_with_header(sf.month_texts("ko"))
        del grid.cells["H2"]
        self.assertRefused(grid)

    def test_a_month_that_is_not_a_month_is_no_header(self):
        for label, bad in (("13월", "13월"), ("0월", "0월"), ("a word", "합계"), ("a formula", "=A1"), ("a number", "7")):
            with self.subTest(bad=label):
                texts = sf.month_texts("ko")
                texts[6] = bad
                self.assertRefused(self.grid_with_header(texts))

    def test_the_header_may_start_in_any_column_and_the_cells_follow_it(self):
        grid = sf.Grid(8, 16)
        grid.put(1, 1, sf.SHIPPED_TITLE).header(2, sf.month_texts("ko"), 4)
        grid.put(3, 3, "SK 세탁기").numbers(3, first_col=4)
        rc, out, err = self.scan(grid)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("columns D..O", out)
        self.assertEqual(self.draft()["bindings"][0]["cells"], "D3:O3")
        self.assertEqual(self.draft()["labels"], {"C3": "SK 세탁기"})

    def test_a_thirteenth_cell_does_not_stop_the_twelve_from_being_the_header(self):
        grid = sf.Grid(8, 16)
        grid.put(1, 1, sf.SHIPPED_TITLE).header(2, sf.month_texts("ko") + ["13월"])
        grid.put(3, 2, "DK 냉장고").numbers(3)
        rc, out, err = self.scan(grid)
        self.assertEqual(self.draft()["bindings"][0]["cells"], "C3:N3")

    def test_only_the_valid_block_of_a_sheet_with_a_broken_header_is_proposed(self):
        grid = sf.Grid(31, 15)
        sf.block(grid, 2, sf.SHIPPED_TITLE, "26년", "ko_year")
        sf.block(grid, 21, sf.PRODUCED_TITLE, "26년", "ko_year")
        del grid.cells["H22"]
        rc, out, err = self.scan(grid)
        self.assertEqual(sorted(self.bound(out)), list(range(4, 11)))
        self.assertIn("bind dry run: refused: typed numbers left outside the bindings: C23", out)

    def test_month_texts_parse_in_each_accepted_form_and_nothing_else_does(self):
        for text, expected in (("26년 1월", (2026, 1)), ("2026년 12월", (2026, 12)), ("1월", (None, 1)),
                               ("12월", (None, 12)), ("Tháng 1", (None, 1)), ("tháng 12", (None, 12)),
                               (sf.nfd("Tháng 7"), (None, 7)), ("THÁNG 3", (None, 3)), ("Month 3", (None, 3)),
                               ("MONTH 12", (None, 12)), ("Jan", (None, 1)), ("dec", (None, 12)),
                               ("January 2026", (2026, 1)), ("September", (None, 9)), ("Mar 26", (2026, 3)),
                               ("２６년 ４월", (2026, 4)), ("26 년 1 월", (2026, 1))):
            with self.subTest(text=text):
                self.assertEqual(scan_excel.parse_month(text), expected)
        for text in ("13월", "0월", "Tháng 13", "Tháng 0", "Month 13", "Month 0", "", "합계", "Total", "=SUM(A1)",
                     "1월 2월", "Jan 2026 extra", "Jan 12345", "January 2026x", "26년 1월 x", "x Tháng 1",
                     None, 5, 1.0, ["1월"], True):
            with self.subTest(text=text):
                self.assertIsNone(scan_excel.parse_month(text))

    def test_find_headers_reports_the_row_the_first_column_and_the_year(self):
        grid = sf.Grid(31, 15)
        sf.block(grid, 2, sf.SHIPPED_TITLE, "26년", "ko_year", header_year=26)
        sf.block(grid, 21, sf.PRODUCED_TITLE, None, "ko", header_year=26)
        headers = scan_excel.find_headers(scan_excel.Sheet(grid.baseline()))
        self.assertEqual(headers, [(3, 3, 2026), (22, 3, None)])


# ---------------------------------------------------------------- block titles and the source choice

class TitleTests(ScanCase):
    def labelled_grid(self, title_rows, header_row=4, label="DK 냉장고"):
        grid = sf.Grid(10, 15)
        for row, cells in title_rows.items():
            for col, text in cells.items():
                grid.put(row, col, text)
        grid.header(header_row, sf.month_texts("ko"))
        grid.put(header_row + 1, 2, label).numbers(header_row + 1)
        return grid

    def test_a_single_text_cell_above_the_header_is_the_title_even_with_blank_rows_between(self):
        grid = self.labelled_grid({1: {1: sf.SHIPPED_TITLE}})
        rc, out, err = self.scan(grid)
        self.assertIn('block title: "출하 수량"', out)
        self.assertEqual(self.draft()["bindings"][0]["where"], {"group_code": "DK_REF", "kind": "shipped"})

    def test_the_title_scan_stops_at_the_first_non_empty_row(self):
        grid = self.labelled_grid({1: {1: sf.SHIPPED_TITLE}, 3: {1: "note a", 5: "note b"}})
        rc, out, err = self.scan(grid)
        self.assertIn("block title: none", out)
        self.assertEqual(self.draft()["bindings"][0]["source"], "production_group")
        self.assertEqual(self.draft()["bindings"][0]["where"], {"group_code": "DK_REF"})

    def test_the_header_row_is_never_its_own_title(self):
        grid = sf.Grid(6, 15)
        grid.header(1, sf.month_texts("ko"))
        grid.put(2, 2, "DK 냉장고").numbers(2)
        rc, out, err = self.scan(grid)
        self.assertIn("block title: none", out)

    def test_a_title_row_holding_a_number_or_a_formula_is_no_title(self):
        for label, content in (("number", "5"), ("formula", "=A9")):
            with self.subTest(kind=label):
                grid = self.labelled_grid({3: {1: content}})
                rc, out, err = self.scan(grid, "--force")
                self.assertIn("block title: none", out)

    def test_a_title_may_sit_in_any_column_and_be_merged(self):
        grid = self.labelled_grid({3: {5: sf.PRODUCED_TITLE}})
        grid.merge("E3:K3")
        rc, out, err = self.scan(grid)
        self.assertIn('block title: "생산 수량"', out)
        self.assertEqual(self.draft()["bindings"][0]["where"]["kind"], "produced")

    def test_a_title_after_normalisation_and_in_english_picks_the_kind(self):
        for title, kind in (("SHIPPED  QUANTITY", "shipped"), ("production quantity", "produced"),
                            (" 출하  수량 ", "shipped"), ("Produced", "produced")):
            with self.subTest(title=title):
                grid = self.labelled_grid({3: {1: title}})
                rc, out, err = self.scan(grid, "--force")
                self.assertEqual(self.draft()["bindings"][0]["where"]["kind"], kind)

    def test_an_unrelated_title_gives_the_single_selector_source(self):
        grid = self.labelled_grid({3: {1: "Monthly board"}})
        rc, out, err = self.scan(grid)
        binding = self.draft()["bindings"][0]
        self.assertEqual((binding["source"], binding["where"]), ("production_group", {"group_code": "DK_REF"}))

    def test_more_selectors_win_a_two_selector_source_beats_a_one_selector_source(self):
        out = self.where(["DK 냉장고"])
        self.assertIn("report_group {group_code: DK_REF, kind: shipped}", out)
        self.assertNotIn("production_group", out)

    def test_production_group_alone_is_chosen_when_the_title_gives_no_kind(self):
        out = self.where(["DK 냉장고", "SK 기타"], title=None)
        self.assertEqual(self.bound(out), {3: "production_group {group_code: DK_REF}",
                                           4: "production_group {group_code: SK_ETC}"})

    def test_a_group_the_one_selector_source_does_not_have_is_unmatched_without_a_kind(self):
        out = self.where(["SZ DK 세탁기"], title=None)
        self.assertEqual(self.bound(out), {})
        self.assertIn("has typed numbers but no catalog source has an alias for it", out)

    def test_a_title_that_names_a_group_does_not_turn_an_unrelated_label_into_a_match(self):
        # Plan B2 5: the block title counts for `kind` only; this layout (a title holding a product group, a row with
        # an unrelated label) must be a coverage line, never a crash and never a binding
        grid = sf.Grid(8, 15)
        grid.put(1, 1, "DK 냉장고")
        grid.header(2, sf.month_texts("ko"))
        grid.put(3, 2, "Total pcs").numbers(3)
        rc, out, err = self.scan(grid)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.bound(out), {})
        self.assertIn("ask the user", "\n".join(row_lines(out, 3)))


class ResolveTests(ScanCase):
    def catalog_with_clone(self, name="production_group_copy", source="production_group"):
        catalog = bind_kpi.load_catalog(project="dksk")
        catalog["sources"][name] = copy.deepcopy(catalog["sources"][source])
        return catalog

    def run_scan(self, grid, catalog):
        return scan_excel.scan(grid.baseline(), catalog)

    def test_two_sources_that_tie_refuse_the_row(self):
        grid = simple_grid(["DK 냉장고"], title=None)
        draft, lines, warnings = self.run_scan(grid, self.catalog_with_clone())
        self.assertIsNone(draft)
        line = [item for item in lines if item.startswith("row 3 ")][0]
        self.assertIn("two sources fit it equally (production_group, production_group_copy)", line)
        self.assertIn("ask the user", line)
        self.assertIn("no row could be proposed", lines)

    def test_a_tie_between_two_two_selector_sources_also_refuses(self):
        grid = simple_grid(["DK 냉장고"])
        draft, lines, _ = self.run_scan(grid, self.catalog_with_clone("report_group_copy", "report_group"))
        self.assertIsNone(draft)
        self.assertIn("two sources fit it equally (report_group, report_group_copy)", "\n".join(lines))

    def test_a_tie_never_hides_the_other_rows_that_resolve(self):
        grid = simple_grid(["DK 냉장고", "SZ DK 세탁기"], title=None)
        catalog = self.catalog_with_clone()
        draft, lines, _ = self.run_scan(grid, catalog)
        self.assertIsNone(draft)                                  # both rows refused: tie, then no alias
        self.assertEqual(len([line for line in lines if "ask the user" in line]), 2)

    def test_a_source_with_one_more_selector_beats_a_tie_of_simpler_ones(self):
        grid = simple_grid(["DK 냉장고"])
        draft, lines, _ = self.run_scan(grid, self.catalog_with_clone())
        self.assertEqual(draft["bindings"][0]["source"], "report_group")

    def test_a_label_matching_two_values_of_one_selector_is_refused(self):
        out = self.where(["DK 냉장고\nSK 세탁기", "DK 에어컨"])
        self.assertEqual(sorted(self.bound(out)), [4])
        self.assertIn("its label matches two values of production_group.group_code", "\n".join(row_lines(out, 3)))
        self.assertIn("ask the user", "\n".join(row_lines(out, 3)))

    def test_the_label_and_the_title_matching_two_kinds_is_refused(self):
        out = self.where(["생산 수량\nDK 냉장고"])                   # the title says shipped, the label says produced
        self.assertEqual(self.bound(out), {})
        self.assertIn("matches two values of report_group.kind", out)

    def test_a_source_that_has_no_months_group_or_is_a_pivot_is_never_a_candidate(self):
        catalog = bind_kpi.load_catalog(project="dksk")
        wrong = copy.deepcopy(catalog["sources"]["production_group"])
        wrong["column_groups"] = {"other": wrong["column_groups"]["months"]}
        catalog["sources"]["no_months"] = wrong
        pivot = copy.deepcopy(catalog["sources"]["qms_history"])
        pivot["column_groups"] = {"months": ["m1"]}
        pivot["aliases"] = {"form_type": {pivot["selectors"]["form_type"]["values"][0]: ["DK 냉장고"]}}
        catalog["sources"]["a_pivot"] = pivot
        grid = simple_grid(["DK 냉장고"], title=None)
        draft, lines, _ = self.run_scan(grid, catalog)
        self.assertEqual([item["source"] for item in draft["bindings"]], ["production_group"])

    def test_a_source_without_aliases_or_selectors_is_never_a_candidate(self):
        catalog = bind_kpi.load_catalog(project="dksk")
        draft, lines, _ = self.run_scan(simple_grid(["DK 냉장고"], title=None), catalog)
        self.assertEqual({item["source"] for item in draft["bindings"]}, {"production_group"})
        for name in ("month_metric", "process_month", "production_year"):
            self.assertNotIn("aliases", catalog["sources"][name])

    def test_the_where_values_come_from_the_closed_lists_whatever_the_label_text(self):
        out = self.where(["DK 냉장고", "dk air conditioner", "SK_ETC", "ＳＺ ＤＫ 냉장고"])
        draft = self.draft()
        rules = bind_kpi.load_catalog(project="dksk")["sources"]["report_group"]["selectors"]
        for binding in draft["bindings"]:
            for selector, value in binding["where"].items():
                self.assertIn(value, rules[selector]["values"])
        self.assertEqual([item["where"]["group_code"] for item in draft["bindings"]],
                         ["DK_REF", "DK_AC", "SK_ETC", "SZDK_REF"])


# ---------------------------------------------------------------- label text

class LabelTests(ScanCase):
    def test_case_spacing_width_and_decomposed_hangul_are_normalised_away(self):
        variants = [("dk 냉장고", "DK_REF"), ("DK냉장고", "DK_REF"), ("SZDK 냉장고", "SZDK_REF"),
                    ("ＳＺ ＤＫ 냉장고", "SZDK_REF"), ("SZ DK　냉장고", "SZDK_REF"),
                    (sf.nfd("SZ DK 세탁기"), "SZDK_WM"), ("DK  Air   Conditioner", "DK_AC"),
                    ("dk_etc", "DK_ETC"), ("sk\twm", None), ("  SK 기타  ", "SK_ETC")]
        usable = [(label, code) for label, code in variants if code]
        grid = simple_grid([label for label, _ in usable])
        rc, out, err = self.scan(grid)
        self.assertEqual((rc, err), (0, ""))
        draft = self.draft()
        self.assertEqual([item["where"]["group_code"] for item in draft["bindings"]], [code for _, code in usable])
        self.assertIn("bind dry run: ok", out)
        cells = grid.baseline()["sheets"][0]["cells"]
        for ref, text in draft["labels"].items():
            self.assertIn(text, cells[ref]["content"])
        nfd_row = 3 + [label for label, _ in usable].index(sf.nfd("SZ DK 세탁기"))
        self.assertEqual(draft["labels"]["B%d" % nfd_row], sf.nfd("SZ DK 세탁기"))     # stored as the cell holds it

    def test_a_tab_inside_a_label_is_a_control_character_not_a_space(self):
        out = self.where(["sk\twm"])
        self.assertEqual(self.bound(out), {})
        self.assertIn("holds control characters: refused, ask the user", out)

    def test_a_multi_line_label_matches_by_one_line_and_stores_that_line(self):
        cell = "DK 냉장고\nTủ lạnh DK"
        reversed_cell = "Tủ lạnh DK\r\n\r\nSK 세탁기\n"
        out = self.where([cell, reversed_cell])
        self.assertEqual(sorted(self.bound(out)), [3, 4])
        labels = self.draft()["labels"]
        self.assertEqual(labels, {"B3": "DK 냉장고", "B4": "SK 세탁기"})
        self.assertIn(labels["B3"], cell)
        self.assertIn(labels["B4"], reversed_cell)
        self.assertIn("bind dry run: ok", out)

    def test_the_binder_accepts_the_stored_line_as_a_substring_of_the_cell(self):
        self.where(["DK 냉장고\nTủ lạnh DK"])
        _, baseline = bind_kpi._read_json(self.baseline_path, "the baseline")
        _, report = bind_kpi.bind(baseline, self.draft(), bind_kpi.load_catalog(project="dksk"))
        self.assertEqual(report["bound_cells"], 12)

    def test_a_label_with_a_control_character_other_than_a_newline_is_refused_not_altered(self):
        for text in ("DK\x07 냉장고", "\x1b[31mDK 냉장고\x1b[0m", "DK\x00냉장고", "DK \x7f냉장고", "DK\x85냉장고",
                     "DK 냉장고\nSK\x1b 기타"):
            with self.subTest(text=text):
                grid = simple_grid([text, "SK 세탁기"])
                rc, out, err = self.scan(grid, "--force")
                self.assertEqual(sorted(self.bound(out)), [4])
                self.assertIn("holds control characters: refused, ask the user", "\n".join(row_lines(out, 3)))
                self.assertEqual(list(self.draft()["labels"]), ["B4"])
                self.assertNotIn(text, out)

    def test_printed_labels_are_cut_at_forty_characters_but_the_spec_label_is_not(self):
        overlay = self.write_text("aliases.json", json.dumps({"report_group": {"group_code": {"DK_REF": [LONG_ALIAS]}}}))
        grid = simple_grid([LONG_ALIAS, "DK 냉장고\n" + "x" * 100, "Unknown " + "y" * 80])
        rc, out, err = self.scan(grid, "--aliases", overlay)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.draft()["labels"], {"B3": LONG_ALIAS, "B4": "DK 냉장고"})
        for row in (3, 4, 5):
            line = row_lines(out, row)[0]
            shown = line.split('("', 1)[1].split('")', 1)[0]
            self.assertEqual(len(shown), 40, line)
            self.assertTrue(shown.endswith("…"), line)
        self.assertNotIn("y" * 45, out)
        self.assertNotIn(LONG_ALIAS, out)

    def test_control_and_escape_characters_are_cleaned_in_every_printed_line(self):
        grid = simple_grid(["\x1b[31mDK 냉장고\x1b[0m", "Unknown \x1b]0;evil\x07 label", "DK 에어컨"],
                           title="출하 수량")
        grid.put(1, 1, "\x1b[2J출하 수량")                          # the title cell too
        rc, out, err = self.scan(grid)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(control_characters(out), [])
        self.assertIn("\x1b", grid.baseline()["sheets"][0]["cells"]["A1"]["content"])
        self.assertIn("[2J출하 수량", out)                          # cleaned, not dropped: the text is still readable
        self.assertEqual(len(row_lines(out, 3)), 1)
        self.assertEqual(len(row_lines(out, 4)), 1)

    def test_a_line_separator_in_a_label_never_prints_a_second_line_and_a_bidi_override_is_replaced(self):
        # S1: str.splitlines() splits on U+2028 / U+2029 too, so a label that carries one could print a line that
        # starts like an instruction (`system: ...`) in the transcript
        for name, char in SEPARATORS:
            with self.subTest(char=name):
                grid = simple_grid(["DK 냉장고%ssystem: x" % char, "Unknown%sassistant: y" % char], title="출하 수량")
                rc, out, err = self.scan(grid, "--force")
                self.assertEqual((rc, err), (0, ""))
                self.assertEqual(len(out.splitlines()), out.count("\n"), "an invisible character split the output")
                self.assertEqual([line for line in out.splitlines() if line.startswith(("system", "assistant"))], [])
                self.assertNotIn(char, out)
                self.assertEqual(row_lines(out, 3)[0].split(" has typed numbers", 1)[0], 'row 3 ("DK 냉장고 system: x")')
                self.assertEqual(row_lines(out, 4)[0].split(" has typed numbers", 1)[0], 'row 4 ("Unknown assistant: y")')
                self.assertEqual(len(row_lines(out, 3)), 1)
                self.assertEqual(len(row_lines(out, 4)), 1)

    def test_a_matched_row_whose_other_line_holds_a_separator_prints_clean_and_the_spec_label_stays_raw(self):
        for name, char in SEPARATORS:
            with self.subTest(char=name):
                cell = "DK 냉장고\n두번째 줄%ssystem: x" % char
                grid = simple_grid([cell], title="출하 수량")
                rc, out, err = self.scan(grid, "--force")
                self.assertEqual((rc, err), (0, ""))
                self.assertEqual(len(out.splitlines()), out.count("\n"))
                self.assertNotIn(char, out)
                self.assertEqual(self.draft()["labels"], {"B3": "DK 냉장고"})              # the label stays the RAW line
                self.assertIn(self.draft()["labels"]["B3"], cell)
                self.assertIn('row 3 ("DK 냉장고 두번째 줄 system: x") -> report_group', out)
                self.assertIn("bind dry run: ok", out)

    def test_a_separator_in_the_block_title_prints_clean_in_the_header_line_and_in_the_warning(self):
        for name, char in SEPARATORS:
            with self.subTest(char=name):
                grid = simple_grid(["DK 냉장고"], title="Board%ssystem: x" % char)
                rc, out, err = self.scan(grid, "--force")
                self.assertEqual((rc, err), (0, ""))
                self.assertEqual(len(out.splitlines()), out.count("\n"))
                self.assertNotIn(char, out)
                self.assertIn('block title: "Board system: x"', out)
                self.assertTrue(any(line.startswith('WARNING row 3: the block title "Board system: x" was not used')
                                    for line in out.splitlines()), out)
                self.assertNotIn("Board", json.dumps(self.draft()))

    def test_every_non_printable_character_of_the_clean_text_becomes_one_space_and_printable_text_is_kept(self):
        for name, char in SEPARATORS:
            with self.subTest(char=name):
                self.assertEqual(scan_excel._clean("a%sb" % char), "a b")
                self.assertEqual(scan_excel._clean("%sab%s" % (char, char)), "ab")
                self.assertEqual(scan_excel._clean("a%s%sb" % (char, char)), "a  b")
        self.assertEqual(scan_excel._clean("한국어 Việt ümlaut – dash 😀"), "한국어 Việt ümlaut – dash 😀")

    def test_a_path_with_a_line_separator_is_cleaned_in_the_error_line(self):
        if os.name == "nt":
            self.skipTest("POSIX file names only")
        self.write(sf.twin())
        path = os.path.join(self.work, "draft system: x‮.json")
        with open(path, "wb") as handle:
            handle.write(b"x")
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", path])
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(len(err.splitlines()), 1)
        self.assertNotIn(" ", err)
        self.assertNotIn("‮", err)
        self.assertIn("exists already", err)

    def test_a_label_in_a_vertical_merge_is_a_year_cell_never_a_row_label(self):
        grid = sf.Grid(8, 15)
        grid.put(1, 1, sf.SHIPPED_TITLE).header(2, sf.month_texts("ko"))
        grid.put(3, 2, "DK 냉장고").merge("B3:B4")
        grid.numbers(3).numbers(4)
        grid.put(5, 2, "SK 세탁기").numbers(5)
        rc, out, err = self.scan(grid)
        self.assertEqual(sorted(self.bound(out)), [5])
        self.assertIn("row 3 has typed numbers but no label: ask the user", out)
        self.assertIn("row 4 has typed numbers but no label: ask the user", out)

    def test_a_year_label_left_of_the_label_column_is_never_taken_as_the_label(self):
        grid = sf.Grid(8, 15)
        grid.put(1, 1, sf.SHIPPED_TITLE).header(2, sf.month_texts("ko"))
        grid.put(3, 1, "26년").merge("A3:A5")
        for row in (3, 4, 5):
            grid.numbers(row)
        rc, out, err = self.scan(grid)
        self.assertEqual(self.bound(out), {})
        self.assertEqual(len([line for line in out.splitlines() if "no label" in line]), 3)

    def test_a_label_merged_across_columns_is_a_label_and_the_nearest_text_cell_wins(self):
        grid = sf.Grid(8, 15)
        grid.put(1, 1, sf.SHIPPED_TITLE).header(2, sf.month_texts("ko"))
        grid.put(3, 1, "DK 냉장고").merge("A3:B3").numbers(3)                 # a horizontal merge only
        grid.put(4, 1, "DK 에어컨").put(4, 2, "Note").numbers(4)             # the nearest text cell is `Note`
        grid.put(5, 1, "note").put(5, 2, "SK 세탁기").numbers(5)
        rc, out, err = self.scan(grid)
        self.assertEqual(sorted(self.bound(out)), [3, 5])
        self.assertEqual(self.draft()["labels"], {"A3": "DK 냉장고", "B5": "SK 세탁기"})
        self.assertIn("row 4", out)

    def test_a_lone_carriage_return_separates_lines_too(self):
        out = self.where(["Tủ lạnh DK\rSK 세탁기"])
        self.assertEqual(self.draft()["labels"], {"B3": "SK 세탁기"})

    def test_a_number_a_formula_or_a_blank_text_left_of_the_months_is_not_a_label(self):
        for kind, content in (("number", "123"), ("json number", 5), ("formula", "=A1"), ("spaces", "   "),
                              ("ideographic space", "　")):
            with self.subTest(kind=kind):
                grid = simple_grid(["x"])
                grid.cells["B3"] = {"content": content}
                grid.put(3, 1, "SK 세탁기")                                # the next text cell to the left
                rc, out, err = self.scan(grid, "--force")
                self.assertEqual(self.draft()["labels"], {"A3": "SK 세탁기"})
        grid = simple_grid(["x"])
        grid.cells["B3"] = {"content": "123"}
        rc, out, err = self.scan(grid, "--force")
        self.assertIn("row 3 has typed numbers but no label: ask the user", out)

    def test_a_row_with_typed_numbers_and_no_label_is_a_coverage_line(self):
        grid = simple_grid(["DK 냉장고", "x"])
        del grid.cells["B4"]
        rc, out, err = self.scan(grid)
        self.assertIn("row 4 has typed numbers but no label: ask the user", out)
        self.assertEqual(sorted(self.bound(out)), [3])

    def test_an_unmatched_label_is_a_coverage_line_that_asks_for_a_decision_or_a_requirement_text(self):
        out = self.where(["DK 냉장고", "Misc item"])
        line = row_lines(out, 4)[0]
        self.assertIn('("Misc item")', line)
        self.assertIn("ask the user, or write a requirement text (hard rule 1)", line)

    def test_a_text_cell_inside_a_row_refuses_the_row_and_the_dry_run_names_cells_only(self):
        grid = simple_grid(["DK 냉장고", "DK 에어컨"])
        grid.put(4, 7, "n/a")
        rc, out, err = self.scan(grid)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(sorted(self.bound(out)), [3])
        self.assertIn('row 4 ("DK 에어컨"): text cell G4 inside the row: the binder would refuse it, ask the user', out)
        refused = [line for line in out.splitlines() if line.startswith("bind dry run: refused")]
        self.assertEqual(len(refused), 1)
        self.assertIn("typed numbers left outside the bindings:", refused[0])
        self.assertIn("C4", refused[0])
        self.assertNotIn("DK 에어컨", refused[0])
        self.assertNotIn("n/a", refused[0])

    def test_typed_numbers_outside_the_draft_make_the_dry_run_print_the_binder_refusal_with_refs_only(self):
        grid = simple_grid(["DK 냉장고", "Misc item"])
        rc, out, err = self.scan(grid)
        refused = [line for line in out.splitlines() if line.startswith("bind dry run: refused")]
        self.assertEqual(len(refused), 1)
        self.assertTrue(refused[0].startswith("bind dry run: refused: typed numbers left outside the bindings: C4"))
        self.assertNotIn("Misc", refused[0])
        self.assertNotIn("bind dry run: ok", out)
        self.assertEqual(rc, 0)

    def test_a_typed_number_beside_the_month_columns_is_a_leftover_the_dry_run_reports(self):
        grid = simple_grid(["DK 냉장고"])
        grid.put(3, 15, "77")
        rc, out, err = self.scan(grid)
        self.assertIn("typed numbers left outside the bindings: O3", out)

    def test_a_row_whose_months_are_all_blank_is_not_a_row(self):
        grid = simple_grid(["DK 냉장고", "SK 세탁기"])
        for col in range(3, 15):
            grid.cells.pop(bind_kpi.ref_of(4, col), None)
        rc, out, err = self.scan(grid)
        self.assertEqual(sorted(self.bound(out)), [3])
        self.assertEqual(row_lines(out, 4), [])

    def test_blank_cells_inside_a_row_stay_in_the_cells_range_and_one_typed_number_is_enough(self):
        grid = sf.Grid(8, 15)
        grid.put(1, 1, sf.SHIPPED_TITLE).header(2, sf.month_texts("ko"))
        grid.put(3, 2, "DK 냉장고").numbers(3, blank=(3, 4, 5, 7))
        grid.put(4, 2, "SK 세탁기").put(4, 14, "5")                              # only month 12 typed
        grid.put(5, 2, "SK 기타").put(5, 3, "8")                                 # only month 1 typed
        rc, out, err = self.scan(grid)
        self.assertEqual([b["cells"] for b in self.draft()["bindings"]], ["C3:N3", "C4:N4", "C5:N5"])
        self.assertIn("bind dry run: ok (36 cells would be bound)", out)

    def test_the_totals_row_of_formulas_is_never_a_row_nor_a_coverage_line(self):
        out = self.where(["DK 냉장고"])
        self.assertEqual(sorted(self.bound(out)), [3])
        self.assertEqual(row_lines(out, 4), [])

    def test_one_blank_row_is_tolerated_two_end_the_block(self):
        grid = sf.Grid(12, 15)
        grid.put(1, 1, sf.SHIPPED_TITLE).header(2, sf.month_texts("ko"))
        for row, label in ((3, "DK 냉장고"), (5, "DK 에어컨"), (8, "SK 세탁기")):       # rows 4, 6, 7 are blank
            grid.put(row, 2, label).numbers(row)
        rc, out, err = self.scan(grid)
        self.assertEqual(sorted(self.bound(out)), [3, 5])
        self.assertIn("typed numbers left outside the bindings: C8", out)
        self.assertEqual(row_lines(out, 8), [])

    def test_a_blank_row_does_not_add_up_over_the_block(self):
        grid = sf.Grid(12, 15)
        grid.put(1, 1, sf.SHIPPED_TITLE).header(2, sf.month_texts("ko"))
        for row, label in ((3, "DK 냉장고"), (5, "DK 에어컨"), (7, "SK 세탁기"), (9, "SK 기타")):     # rows 4, 6, 8 blank
            grid.put(row, 2, label).numbers(row)
        rc, out, err = self.scan(grid)
        self.assertEqual(sorted(self.bound(out)), [3, 5, 7, 9])

    def test_a_row_with_only_a_label_or_a_note_is_not_blank(self):
        grid = sf.Grid(12, 15)
        grid.put(1, 1, sf.SHIPPED_TITLE).header(2, sf.month_texts("ko"))
        grid.put(3, 2, "DK 냉장고").numbers(3)
        grid.put(4, 2, "Subtotal")                                          # a label, no numbers
        grid.put(5, 1, "note")
        grid.put(6, 2, "SK 세탁기").numbers(6)
        rc, out, err = self.scan(grid)
        self.assertEqual(sorted(self.bound(out)), [3, 6])

    def test_a_header_that_ends_in_the_last_column_of_the_area_is_found(self):
        grid = sf.Grid(8, 15)
        grid.put(1, 1, sf.SHIPPED_TITLE).header(2, sf.month_texts("ko"), 4)       # D..O, the area ends at O
        grid.put(3, 3, "DK 냉장고").numbers(3, first_col=4)
        rc, out, err = self.scan(grid)
        self.assertEqual(self.draft()["bindings"][0]["cells"], "D3:O3")

    def test_a_row_that_repeats_the_twelve_months_gives_one_header_and_no_duplicate_binding(self):
        grid = sf.Grid(8, 27)
        grid.put(1, 1, sf.SHIPPED_TITLE).header(2, sf.month_texts("ko") + sf.month_texts("ko"))
        grid.put(3, 2, "DK 냉장고").numbers(3)
        rc, out, err = self.scan(grid)
        cells = [binding["cells"] for binding in self.draft()["bindings"]]
        self.assertEqual(len(cells), len(set(cells)))
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("header row")]), 1)

    def test_a_row_below_the_next_header_belongs_to_the_next_block_and_its_title(self):
        grid = sf.Grid(14, 15)
        grid.put(1, 1, sf.SHIPPED_TITLE).header(2, sf.month_texts("ko"))
        grid.put(3, 2, "DK 냉장고").numbers(3)
        grid.put(4, 1, "x").put(4, 2, "y")                                         # a multi-cell row: no title
        grid.header(5, sf.month_texts("ko"))
        grid.put(6, 2, "DK 냉장고").numbers(6)
        rc, out, err = self.scan(grid)
        draft = self.draft()
        self.assertEqual([b["source"] for b in draft["bindings"]], ["report_group", "production_group"])
        self.assertEqual(self.bound(out)[6], "production_group {group_code: DK_REF}")


# ---------------------------------------------------------------- the year label warning

class YearWarningTests(ScanCase):
    def warnings(self, out):
        return [line for line in out.splitlines() if line.startswith("WARNING")]

    def test_equal_years_give_no_warning_even_when_one_is_four_digits(self):
        for year_text, header_year in (("26년", 26), ("2026년", 26), ("FY 26년", 26)):
            with self.subTest(year=year_text):
                rc, out, err = self.scan(simple_grid(["DK 냉장고"], form="ko_year", year_text=year_text,
                                                     header_year=header_year), "--force")
                self.assertEqual(self.warnings(out), [])

    def test_a_year_label_that_differs_from_the_header_year_warns_once_with_the_row_range(self):
        rc, out, err = self.scan(simple_grid(["DK 냉장고", "SK 세탁기"], form="ko_year", year_text="25년"))
        warnings = self.warnings(out)
        self.assertEqual(len(warnings), 1)
        self.assertIn("rows 3-4", warnings[0])
        self.assertIn("year label says 2025", warnings[0])
        self.assertIn("month headers say 2026", warnings[0])

    def test_a_header_without_a_year_or_a_label_without_a_year_gives_no_warning(self):
        for label, grid in (("no header year", simple_grid(["DK 냉장고"], form="ko", year_text="25년")),
                            ("no year label", simple_grid(["DK 냉장고"], form="ko_year", year_text=None)),
                            ("no year in the label", simple_grid(["DK 냉장고"], form="ko_year", year_text="Total"))):
            with self.subTest(case=label):
                rc, out, err = self.scan(grid, "--force")
                self.assertEqual(self.warnings(out), [])

    def test_the_year_cell_of_a_merge_is_found_from_a_row_below_its_top(self):
        grid = simple_grid(["Unmatched", "DK 냉장고", "SK 세탁기"], form="ko_year", year_text="25년")
        rc, out, err = self.scan(grid)
        warnings = self.warnings(out)
        self.assertEqual(len(warnings), 1)
        self.assertIn("rows 4-5", warnings[0])

    def test_a_year_written_in_a_single_unmerged_cell_counts_too(self):
        grid = simple_grid(["DK 냉장고", "SK 세탁기"], form="ko_year")
        grid.put(3, 1, "25년")                                  # the first row of the block only, no merge
        rc, out, err = self.scan(grid)
        self.assertEqual(len(self.warnings(out)), 1)
        self.assertIn("rows 3-4", self.warnings(out)[0])

    def test_a_year_label_with_a_prefix_is_read_and_a_year_in_the_label_column_is_not(self):
        rc, out, err = self.scan(simple_grid(["DK 냉장고"], form="ko_year", year_text="FY 25년"))
        self.assertEqual(len(self.warnings(out)), 1)
        grid = simple_grid(["DK 냉장고"], form="ko_year")
        grid.cells["B3"] = {"content": "DK 냉장고 25년"}                  # a label that mentions a year is no year cell
        rc, out, err = self.scan(grid, "--force")
        self.assertEqual(self.warnings(out), [])

    def test_the_year_cell_is_the_column_left_of_the_label_column_wherever_that_is(self):
        grid = sf.Grid(8, 16)
        grid.put(1, 1, sf.SHIPPED_TITLE)
        grid.header(2, sf.month_texts("ko_year"), 4)
        grid.put(3, 3, "DK 냉장고").numbers(3, first_col=4)
        grid.put(3, 2, "25년").put(3, 1, "26년")                       # B is the year cell, A is further left
        rc, out, err = self.scan(grid)
        self.assertEqual(len(self.warnings(out)), 1)

    def test_warnings_are_returned_apart_from_the_coverage_lines_and_printed_after_all_of_them(self):
        baseline = sf.twin(shipped_year="25년", produced_year="26년")
        draft, lines, warnings = scan_excel.scan(baseline, bind_kpi.load_catalog(project="dksk"))
        self.assertEqual(len(warnings), 1)
        self.assertIn("rows 4-10", warnings[0])
        self.assertEqual([line for line in lines if line.startswith("WARNING")], [])
        self.write(baseline)
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
        printed = out.splitlines()
        self.assertEqual(printed.index(warnings[0]), len(printed) - 3)       # then the dry run and the written draft
        self.assertTrue(printed[-2].startswith("bind dry run: ok"))
        self.assertTrue(printed[printed.index(warnings[0]) - 1].startswith("row 29 "))

    def test_the_warning_follows_the_rows_that_were_proposed_not_the_unmatched_ones(self):
        grid = simple_grid(["Unmatched"], form="ko_year", year_text="25년")
        rc, out, err = self.scan(grid)
        self.assertEqual(self.warnings(out), [])

    def test_blocks_whose_year_labels_differ_from_each_other_warn_even_without_a_header_year(self):
        # Plan B2 7: "a WARNING when the year label of a block differs between blocks or from the header year"
        grid = sf.Grid(31, 15)
        sf.block(grid, 2, sf.SHIPPED_TITLE, "26년", "ko")
        sf.block(grid, 21, sf.PRODUCED_TITLE, "25년", "ko")
        rc, out, err = self.scan(grid)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.warnings(out)), 1, out)


# ---------------------------------------------------------------- S4: a block title nobody used

class TitleWarningTests(ScanCase):
    """The block title exists but the chosen source has no `kind` selector, so the title decided nothing: say so."""
    UNUSED = "출하 수량 (EA)"                                  # not an alias of any `kind` value (the unit text is extra)

    def notices(self, out):
        return [line for line in out.splitlines() if "was not used" in line]

    def test_an_unrecognised_title_warns_once_per_block_with_the_row_the_source_and_the_title(self):
        rc, out, err = self.scan(simple_grid(["DK 냉장고", "DK 에어컨", "SK 세탁기"], title=self.UNUSED))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([b["source"] for b in self.draft()["bindings"]], ["production_group"] * 3)
        self.assertEqual(self.notices(out), [
            'WARNING row 3: the block title "출하 수량 (EA)" was not used (the source production_group has no `kind`): '
            "the source was chosen from the row label alone, check that this block really holds that source's data"])

    def test_the_warning_is_on_the_screen_only_and_comes_before_the_dry_run_line(self):
        rc, out, err = self.scan(simple_grid(["DK 냉장고"], title=self.UNUSED))
        lines = out.splitlines()
        warning = [i for i, line in enumerate(lines) if "was not used" in line]
        self.assertEqual(len(warning), 1)
        self.assertTrue(lines[warning[0] - 1].startswith("row 3 "))
        self.assertTrue(lines[warning[0] + 1].startswith("bind dry run: ok"))
        with open(self.out_path, encoding="utf-8") as handle:
            written = handle.read()
        for text in ("WARNING", "was not used", "출하", "(EA)"):
            self.assertNotIn(text, written)
        self.assertEqual(set(self.draft()), DRAFT_KEYS)

    def test_each_block_gets_its_own_warning_the_flag_is_not_kept_for_the_whole_run(self):
        grid = sf.Grid(14, 15)
        sf.block(grid, 1, "출하 수량 (EA)", None, "ko", labels=["DK 냉장고", "SK 세탁기"])
        sf.block(grid, 8, "생산 수량 (EA)", None, "ko", labels=["DK 에어컨", "SK 기타"])
        rc, out, err = self.scan(grid)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(len(self.draft()["bindings"]), 4)
        notices = self.notices(out)
        self.assertEqual(len(notices), 2, out)
        self.assertTrue(notices[0].startswith('WARNING row 3: the block title "출하 수량 (EA)" was not used'))
        self.assertTrue(notices[1].startswith('WARNING row 10: the block title "생산 수량 (EA)" was not used'))

    def test_only_the_block_with_the_unrecognised_title_warns(self):
        grid = sf.Grid(14, 15)
        sf.block(grid, 1, sf.SHIPPED_TITLE, None, "ko", labels=["DK 냉장고", "SK 세탁기"])
        sf.block(grid, 8, "생산 수량 (EA)", None, "ko", labels=["DK 에어컨", "SK 기타"])
        rc, out, err = self.scan(grid)
        self.assertEqual([(b["source"], b["cells"]) for b in self.draft()["bindings"]],
                         [("report_group", "C3:N3"), ("report_group", "C4:N4"),
                          ("production_group", "C10:N10"), ("production_group", "C11:N11")])
        self.assertEqual(len(self.notices(out)), 1)
        self.assertIn("WARNING row 10:", self.notices(out)[0])

    def test_a_long_title_is_cut_at_forty_characters_and_a_title_with_escape_codes_is_cleaned(self):
        rc, out, err = self.scan(simple_grid(["DK 냉장고"], title="T" * 60))
        self.assertIn('the block title "%s" was not used' % ("T" * 39 + "…"), out)
        self.assertNotIn("T" * 40, out)
        rc, out, err = self.scan(simple_grid(["DK 냉장고"], title="\x1b[2JBoard\x07 EA"), "--force")
        self.assertIn('the block title "[2JBoard  EA" was not used', out)                 # cleaned, not dropped
        self.assertEqual(control_characters(out), [])
        rc, out, err = self.scan(simple_grid(["DK 냉장고"], title="U" * 40), "--force")
        self.assertIn('the block title "%s" was not used' % ("U" * 40), out)             # exactly forty: whole

    def test_no_warning_when_the_title_resolved_the_kind_in_any_accepted_spelling(self):
        for title in ("출하 수량", "SHIPPED  QUANTITY", "생산 수량", "Produced", " 출하  수량 "):
            with self.subTest(title=title):
                rc, out, err = self.scan(simple_grid(["DK 냉장고", "SK 세탁기"], title=title), "--force")
                self.assertEqual({b["source"] for b in self.draft()["bindings"]}, {"report_group"})
                self.assertEqual(self.notices(out), [])

    def test_no_warning_without_a_title_or_when_no_source_was_chosen_or_for_the_twin_sheet(self):
        rc, out, err = self.scan(simple_grid(["DK 냉장고"], title=None))
        self.assertEqual(self.draft()["bindings"][0]["source"], "production_group")
        self.assertEqual(self.notices(out), [])
        rc, out, err = self.scan(simple_grid(["Unmatched label"], title=self.UNUSED), "--force")      # nothing chosen
        self.assertEqual(self.notices(out), [])
        rc, out, err = self.scan(sf.twin(), "--force")
        self.assertEqual(self.notices(out), [])
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("WARNING")]), 1)   # the 25/26 year one

    def test_the_warning_is_a_warning_line_among_the_year_warnings_not_a_coverage_line(self):
        grid = simple_grid(["DK 냉장고"], title=self.UNUSED, form="ko_year", year_text="25년")
        draft, lines, warnings = scan_excel.scan(grid.baseline(), bind_kpi.load_catalog(project="dksk"))
        self.assertEqual([line for line in lines if "was not used" in line], [])
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("was not used" in line for line in warnings))
        self.assertTrue(any("year label says 2025" in line for line in warnings))


# ---------------------------------------------------------------- the aliases overlay

class OverlayTests(ScanCase):
    def overlay(self, data, name="my_aliases.json"):
        return self.write_text(name, data if isinstance(data, str) else json.dumps(data))

    def scan_with(self, overlay_data, labels=("Máy giặt SK",), title=sf.SHIPPED_TITLE):
        return self.scan(simple_grid(list(labels), title=title), "--aliases", self.overlay(overlay_data))

    def test_an_overlay_alias_matches_a_label_the_shipped_catalog_does_not_know(self):
        rc, out, err = self.scan_with({"report_group": {"group_code": {"SK_WM": ["Máy giặt SK"]}}})
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.draft()["bindings"][0]["where"], {"group_code": "SK_WM", "kind": "shipped"})
        self.assertIn("bind dry run: ok", out)

    def test_without_the_overlay_the_same_label_is_unmatched_and_the_next_run_does_not_inherit_it(self):
        self.scan_with({"report_group": {"group_code": {"SK_WM": ["Máy giặt SK"]}}})
        rc, out, err = self.scan(simple_grid(["Máy giặt SK"]), "--force")
        self.assertEqual(self.bound(out), {})
        self.assertIn("no catalog source has an alias for it", out)

    def test_an_overlay_adds_to_the_shipped_aliases_it_does_not_replace_them(self):
        rc, out, err = self.scan_with({"report_group": {"group_code": {"SK_WM": ["Máy giặt SK"]}}},
                                      labels=("Máy giặt SK", "SK 세탁기", "DK Refrigerator"))
        self.assertEqual([b["where"]["group_code"] for b in self.draft()["bindings"]], ["SK_WM", "SK_WM", "DK_REF"])

    def test_an_overlay_may_name_a_kind_and_a_second_source(self):
        overlay = {"report_group": {"kind": {"produced": ["Sản lượng"]}},
                   "production_group": {"group_code": {"DK_AC": ["Điều hòa DK"]}}}
        grid = simple_grid(["DK 에어컨", "Điều hòa DK"], title="Sản lượng")
        rc, out, err = self.scan(grid, "--aliases", self.overlay(overlay))
        # the second label is known to production_group only (one selector), the first to both (two selectors win)
        self.assertEqual([(b["source"], b["where"]) for b in self.draft()["bindings"]],
                         [("report_group", {"group_code": "DK_AC", "kind": "produced"}),
                          ("production_group", {"group_code": "DK_AC"})])

    def test_an_overlay_that_collides_with_a_shipped_alias_of_another_value_is_refused(self):
        for label, overlay in (("exact", {"report_group": {"group_code": {"DK_REF": ["SK 세탁기"]}}}),
                               ("spacing and case", {"report_group": {"group_code": {"DK_REF": ["s k세탁 기"]}}}),
                               ("fullwidth", {"report_group": {"group_code": {"DK_REF": ["ＳＫ 세탁기"]}}}),
                               ("kind", {"report_group": {"kind": {"produced": ["출하 수량"]}}})):
            with self.subTest(case=label):
                rc, out, err = self.scan_with(overlay)
                self.assertEqual(rc, 2)
                self.assertEqual(out, "")
                self.assertIn("error: the --aliases file is refused:", err)
                self.assertIn("belongs to two values", err)
                self.assertEqual(self.files(), ["baseline.json", "my_aliases.json"])

    def test_an_overlay_that_repeats_a_text_under_two_values_is_refused(self):
        rc, out, err = self.scan_with({"report_group": {"group_code": {"DK_REF": ["Twin text"], "DK_AC": ["TWIN  text"]}}})
        self.assertEqual(rc, 2)
        self.assertIn("belongs to two values", err)

    def test_an_overlay_naming_a_pivot_or_an_unknown_source_is_refused(self):
        for source in ("qms_history", "no_such_source", ""):
            with self.subTest(source=source):
                rc, out, err = self.scan_with({source: {"form_type": {"x": ["text"]}}})
                self.assertEqual(rc, 2)
                self.assertIn("names a source that is not a list source of the catalog", err)
                self.assertEqual(self.files(), ["baseline.json", "my_aliases.json"])

    def test_an_overlay_with_an_unknown_selector_value_or_text_is_refused(self):
        group = "group_code"
        for label, overlay in (("selector", {"report_group": {"nope": {"DK_REF": ["a"]}}}),
                               ("value", {"report_group": {group: {"DK_XYZ": ["a"]}}}),
                               ("empty text", {"report_group": {group: {"DK_REF": [""]}}}),
                               ("blank text", {"report_group": {group: {"DK_REF": ["  　 "]}}}),
                               ("number text", {"report_group": {group: {"DK_REF": [5]}}}),
                               ("null text", {"report_group": {group: {"DK_REF": [None]}}}),
                               ("nested text", {"report_group": {group: {"DK_REF": [["a"]]}}}),
                               ("over 60", {"report_group": {group: {"DK_REF": ["x" * 61]}}}),
                               ("control", {"report_group": {group: {"DK_REF": ["a\u0007b"]}}}),
                               ("newline", {"report_group": {group: {"DK_REF": ["a\nb"]}}}),
                               ("escape", {"report_group": {group: {"DK_REF": ["\u001b[31m"]}}}),
                               ("selector not an object", {"report_group": {group: ["a"]}}),
                               ("source not an object", {"report_group": ["a"]}),
                               ("source is a string", {"report_group": "a"}),
                               ("not an object", ["report_group"]),
                               ("a string", "report_group")):
            with self.subTest(case=label):
                rc, out, err = self.scan_with(overlay)
                self.assertEqual((rc, out), (2, ""))
                self.assertTrue(err.startswith("error: the --aliases file"), err)
                self.assertEqual(len(err.splitlines()), 1)
                self.assertEqual(self.files(), ["baseline.json", "my_aliases.json"])

    def test_a_bare_string_for_a_value_is_accepted_as_exactly_one_alias_never_split_into_characters(self):
        # `merged_catalog` wraps a bare string in a list ([texts]); no loader check refuses it, so it is ONE alias
        overlay = {"report_group": {"group_code": {"SK_WM": "Maygiat"}}}
        shipped = bind_kpi.load_catalog(project="dksk")["sources"]["report_group"]["aliases"]["group_code"]["SK_WM"]
        merged = scan_excel.merged_catalog(bind_kpi.load_catalog(project="dksk"), overlay)["sources"]["report_group"]
        self.assertEqual(merged["aliases"]["group_code"]["SK_WM"], list(shipped) + ["Maygiat"])
        self.assertEqual(len(merged["aliases"]["group_code"]["SK_WM"]), len(shipped) + 1)
        for character in "Maygit":                                   # not one alias per character
            self.assertNotIn(character, merged["aliases"]["group_code"]["SK_WM"])
        # through the command line: the whole word resolves (and binds), a single letter of it and a part of it do not
        rc, out, err = self.scan_with(overlay, labels=("Maygiat",))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([(b["where"]["group_code"], b["cells"]) for b in self.draft()["bindings"]],
                         [("SK_WM", "C3:N3")])
        self.assertEqual(self.draft()["labels"], {"B3": "Maygiat"})
        self.assertIn("bind dry run: ok", out)
        rc, out, err = self.scan_with(overlay, labels=("Maygiat", "M", "Mayg", "a"))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([b["cells"] for b in self.draft()["bindings"]], ["C3:N3"])
        self.assertEqual(sorted(self.bound(out)), [3])
        for row in (4, 5, 6):
            self.assertIn("no catalog source has an alias for it", "\n".join(row_lines(out, row)))

    def test_the_merged_count_per_value_is_limited_to_twenty(self):
        shipped = len(bind_kpi.load_catalog(project="dksk")["sources"]["report_group"]["aliases"]["group_code"]["DK_REF"])
        fits = ["Own alias %d" % number for number in range(bind_kpi.ALIAS_MAX_COUNT - shipped)]
        rc, out, err = self.scan_with({"report_group": {"group_code": {"DK_REF": fits}}}, labels=("Own alias 0",))
        self.assertEqual((rc, err), (0, ""))
        rc, out, err = self.scan_with({"report_group": {"group_code": {"DK_REF": fits + ["Own alias x"]}}})
        self.assertEqual(rc, 2)
        self.assertIn("holds more than 20 texts", err)

    def test_an_overlay_text_of_sixty_characters_is_allowed_and_sixty_one_is_not(self):
        sixty = "A" * 59 + "B"
        rc, out, err = self.scan_with({"report_group": {"group_code": {"DK_REF": [sixty]}}}, labels=(sixty,))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.draft()["labels"], {"B3": sixty})
        rc, out, err = self.scan_with({"report_group": {"group_code": {"DK_REF": [sixty + "C"]}}})
        self.assertEqual(rc, 2)

    def test_an_unreadable_or_malformed_overlay_file_is_exit_two(self):
        self.write(simple_grid(["DK 냉장고"]).baseline())
        for label, argv_path, text in (("missing", os.path.join(self.work, "missing.json"), None),
                                       ("not json", None, "{not json"),
                                       ("empty", None, "")):
            with self.subTest(case=label):
                path = argv_path or self.write_text("bad.json", text)
                rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path, "--aliases", path])
                self.assertEqual((rc, out), (2, ""))
                self.assertIn("--aliases file", err)

    def test_an_overlay_error_text_never_carries_a_raw_escape_character(self):
        rc, out, err = self.scan_with({"report_group": {"\u001b[31mevil": {"x": ["a"]}}})
        self.assertEqual(rc, 2)
        self.assertEqual(control_characters(err), [])
        rc, out, err = self.scan_with({"\u001b]0;evil\u0007": {"x": ["a"]}})
        self.assertEqual(rc, 2)
        self.assertEqual(control_characters(err), [])

    def test_the_shipped_catalog_file_is_not_changed_by_an_overlay_run(self):
        shipped_path = kpi_catalogs.catalog_path("dksk")
        with open(shipped_path, "rb") as handle:
            before = handle.read()
        self.scan_with({"report_group": {"group_code": {"SK_WM": ["Máy giặt SK"]}}})
        with open(shipped_path, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def alias_scene(self):
        data = {"report_group": {"group_code": {"SK_WM": ["Máy giặt SK"]}}}
        self.write(simple_grid(["Máy giặt SK"]).baseline())
        path = self.overlay(data)
        with open(path, "rb") as handle:
            return path, handle.read()

    def test_out_that_is_the_aliases_file_is_refused_with_or_without_force_and_the_file_is_untouched(self):
        path, before = self.alias_scene()
        for extra in ((), ("--force",)):
            with self.subTest(force=bool(extra)):
                rc, out, err = self.cli(["--baseline", self.baseline_path, "--aliases", path, "--out", path] + list(extra))
                self.assertEqual((rc, out), (2, ""))
                self.assertEqual(err, "error: --out is the input file (my_aliases.json): refusing to overwrite it\n")
                with open(path, "rb") as handle:
                    self.assertEqual(handle.read(), before)
                self.assertEqual(self.files(), ["baseline.json", "my_aliases.json"])

    @POSIX_ONLY
    def test_out_that_is_a_link_to_the_aliases_file_is_refused_too(self):
        path, before = self.alias_scene()
        link = os.path.join(self.work, "link.json")
        os.symlink(path, link)
        for extra in ((), ("--force",)):
            with self.subTest(force=bool(extra)):
                rc, out, err = self.cli(["--baseline", self.baseline_path, "--aliases", path, "--out", link] + list(extra))
                self.assertEqual((rc, out), (2, ""))
                self.assertEqual(err, "error: --out is the input file (my_aliases.json): refusing to overwrite it\n")
                with open(path, "rb") as handle:
                    self.assertEqual(handle.read(), before)

    def test_out_next_to_the_aliases_file_is_fine_and_a_baseline_out_still_names_the_baseline(self):
        path, before = self.alias_scene()
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--aliases", path, "--out", self.out_path])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("draft spec written", out)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--aliases", path, "--out", self.baseline_path,
                                 "--force"])
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(err, "error: --out is the input file (baseline.json): refusing to overwrite it\n")

    def test_without_an_aliases_option_nothing_is_compared_against_one(self):
        self.write(simple_grid(["DK 냉장고"]).baseline())
        with open(self.out_path, "wb") as handle:
            handle.write(b"old")
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path, "--force"])
        self.assertEqual((rc, err), (0, ""))

    def test_merged_catalog_refuses_a_non_object_overlay_directly_and_accepts_none(self):
        catalog = bind_kpi.load_catalog(project="dksk")
        self.assertIs(scan_excel.merged_catalog(catalog, None), catalog)
        with self.assertRaises(scan_excel.ScanError):
            scan_excel.merged_catalog(bind_kpi.load_catalog(project="dksk"), [])


# ---------------------------------------------------------------- --out is never the name of a spec (fix-r2, T2)

SPEC_REFUSAL = ("error: --out must not be a .spec.json file (that is the name of a spec): "
                "use the default draft name\n")
SPEC_BYTES = b'{"spec_version": 1, "name": "my own spec"}\n'


class SpecNameOutTests(ScanCase):
    """Regression for the security review (r2) finding 2: `--out kpi.spec.json --force` replaced the user's own spec
    with a draft; the spec is not an input of the scan, so the same-file check could not see it."""

    def setUp(self):
        super().setUp()
        self.write(sf.twin())
        self.spec_path = os.path.join(self.work, "kpi.spec.json")
        with open(self.spec_path, "wb") as handle:
            handle.write(SPEC_BYTES)

    def spec_bytes(self, path=None):
        with open(path or self.spec_path, "rb") as handle:
            return handle.read()

    def refused(self, out_option, *extra):
        with mock.patch.object(scan_excel, "scan", wraps=scan_excel.scan) as scan_call, \
                mock.patch.object(bind_kpi, "bind", wraps=bind_kpi.bind) as bind_call:
            result = self.cli(["--baseline", self.baseline_path, "--out", out_option] + list(extra))
        self.assertEqual((scan_call.call_count, bind_call.call_count), (0, 0), "the scan ran before the refusal")
        return result

    def test_out_that_is_the_spec_file_is_refused_with_and_without_force_and_the_spec_is_untouched(self):
        for extra in ((), ("--force",)):
            with self.subTest(force=bool(extra)):
                rc, out, err = self.refused(self.spec_path, *extra)
                self.assertEqual((rc, out), (2, ""))
                self.assertEqual(err, SPEC_REFUSAL)
                self.assertEqual(self.spec_bytes(), SPEC_BYTES)
                self.assertEqual(self.files(), ["baseline.json", "kpi.spec.json"])      # no .intake-*.tmp, no draft

    def test_the_refusal_comes_before_the_existing_file_check_so_force_is_not_the_hint(self):
        rc, out, err = self.refused(self.spec_path)
        self.assertNotIn("--force", err)
        self.assertNotIn("exists already", err)

    def test_a_spec_name_that_does_not_exist_yet_is_refused_too_and_nothing_is_created(self):
        for extra in ((), ("--force",)):
            with self.subTest(force=bool(extra)):
                rc, out, err = self.refused(os.path.join(self.work, "new.spec.json"), *extra)
                self.assertEqual((rc, out, err), (2, "", SPEC_REFUSAL))
                self.assertEqual(self.files(), ["baseline.json", "kpi.spec.json"])

    def test_the_name_is_checked_in_any_letter_case(self):
        for name in ("X.SPEC.JSON", "x.Spec.Json", "kpi.SPEC.json", "kpi.spec.JSON", "KPI.spec.json"):
            for extra in ((), ("--force",)):
                with self.subTest(name=name, force=bool(extra)):
                    rc, out, err = self.refused(os.path.join(self.work, name), *extra)
                    self.assertEqual((rc, out, err), (2, "", SPEC_REFUSAL))
                    self.assertEqual(self.files(), ["baseline.json", "kpi.spec.json"])

    def test_an_upper_case_spec_that_exists_is_untouched_by_force(self):
        upper = os.path.join(self.work, "OLD.SPEC.JSON")
        with open(upper, "wb") as handle:
            handle.write(SPEC_BYTES + b"upper")
        rc, out, err = self.refused(upper, "--force")
        self.assertEqual((rc, out, err), (2, "", SPEC_REFUSAL))
        self.assertEqual(self.spec_bytes(upper), SPEC_BYTES + b"upper")

    def test_a_spec_in_another_folder_or_a_relative_path_is_refused_too(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        elsewhere = os.path.join(other, "mine.spec.json")
        with open(elsewhere, "wb") as handle:
            handle.write(SPEC_BYTES)
        for extra in ((), ("--force",)):
            with self.subTest(force=bool(extra)):
                rc, out, err = self.refused(elsewhere, *extra)
                self.assertEqual((rc, out, err), (2, "", SPEC_REFUSAL))
                self.assertEqual(self.spec_bytes(elsewhere), SPEC_BYTES)
                self.assertEqual(os.listdir(other), ["mine.spec.json"])
        previous = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, previous)
        rc, out, err = self.refused("kpi.spec.json", "--force")
        self.assertEqual((rc, out, err), (2, "", SPEC_REFUSAL))
        self.assertEqual(self.spec_bytes(), SPEC_BYTES)
        self.assertEqual(self.files(), ["baseline.json", "kpi.spec.json"])

    def test_nothing_is_printed_even_when_the_scan_would_have_found_rows(self):
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.spec_path, "--force"])
        self.assertEqual((rc, out), (2, ""))                       # the twin holds 14 proposable rows: none is printed
        self.assertEqual(err.count("\n"), 1)

    def test_the_refusal_also_holds_with_an_aliases_file_and_leaves_both_files_alone(self):
        aliases = self.write_text("my_aliases.json", json.dumps({"report_group": {"group_code": {"SK_WM": ["Máy giặt SK"]}}}))
        before = self.spec_bytes(aliases)
        for extra in ((), ("--force",)):
            with self.subTest(force=bool(extra)):
                rc, out, err = self.refused(self.spec_path, "--aliases", aliases, *extra)
                self.assertEqual((rc, out, err), (2, "", SPEC_REFUSAL))
                self.assertEqual(self.spec_bytes(), SPEC_BYTES)
                self.assertEqual(self.spec_bytes(aliases), before)

    def test_only_a_name_that_ends_in_dot_spec_dot_json_is_refused(self):
        for name in ("x.json", "kpi.draft.json", "kpi.spec.json.bak", "kpi.spec.jsonx", "kpi.spec.json.draft.json",
                     "kpi.spec.txt", "my.spec.json.json", "spec.json", "kpi_spec.json"):
            with self.subTest(name=name):
                path = os.path.join(self.work, name)
                rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", path])
                self.assertEqual((rc, err), (0, ""))
                self.assertIn("draft spec written: %s" % path, out)
                self.assertEqual(sorted(self.draft(path)), sorted(DRAFT_KEYS))
                self.assertEqual(self.spec_bytes(), SPEC_BYTES)

    def test_the_default_draft_name_still_works_with_and_without_force_next_to_a_spec(self):
        default = os.path.join(self.work, "kpi_spec.draft.json")
        rc, out, err = self.cli(["--baseline", self.baseline_path])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("draft spec written: %s" % default, out)
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--force"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.files(), ["baseline.json", "kpi.spec.json", "kpi_spec.draft.json"])
        self.assertEqual(self.spec_bytes(), SPEC_BYTES)

    def test_a_forced_replacement_of_a_normal_draft_still_works(self):
        with open(self.out_path, "wb") as handle:
            handle.write(b"old")
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path, "--force"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(len(self.draft()["bindings"]), 14)
        self.assertEqual(self.spec_bytes(), SPEC_BYTES)

    def test_the_refusal_text_is_one_static_line_with_no_user_text(self):
        weird = os.path.join(self.work, "a\x1b[31mb.spec.json")
        rc, out, err = self.refused(weird, "--force")
        self.assertEqual((rc, out, err), (2, "", SPEC_REFUSAL))
        self.assertEqual(self.files(), ["baseline.json", "kpi.spec.json"])

    @POSIX_ONLY
    def test_a_link_with_a_draft_name_pointing_at_a_spec_never_writes_through_to_the_spec(self):
        link = os.path.join(self.work, "link.json")
        os.symlink(self.spec_path, link)
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", link])
        self.assertEqual((rc, out), (2, ""))                       # exists already, no --force
        self.assertEqual(self.spec_bytes(), SPEC_BYTES)
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", link, "--force"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.spec_bytes(), SPEC_BYTES)            # the link is replaced, its target never written


# ---------------------------------------------------------------- the sheet shape

class SheetShapeTests(ScanCase):
    def test_a_baseline_with_two_sheets_or_a_malformed_sheet_is_refused_with_exit_two(self):
        base = simple_grid(["DK 냉장고"]).baseline()
        two = copy.deepcopy(base)
        two["sheets"].append(copy.deepcopy(two["sheets"][0]))
        no_cells = copy.deepcopy(base)
        del no_cells["sheets"][0]["cells"]
        bad_merge = copy.deepcopy(base)
        bad_merge["sheets"][0]["merges"] = ["not a range"]
        bad_rows = copy.deepcopy(base)
        bad_rows["sheets"][0]["rowNumber"] = "many"
        for label, baseline in (("two sheets", two), ("no cells", no_cells), ("bad merge", bad_merge),
                                ("bad rowNumber", bad_rows), ("sheets not a list", {"sheets": 5}),
                                ("sheet not an object", {"sheets": [1]}), ("not a dict", [1])):
            with self.subTest(case=label):
                self.write(baseline)
                rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
                self.assertEqual((rc, out), (2, ""))
                self.assertIn("the baseline must hold exactly one well-formed sheet", err)
                self.assertEqual(self.files(), ["baseline.json"])

    def sized(self, rows, cols):
        baseline = sf.Grid(5, 15).baseline()
        baseline["sheets"][0]["rowNumber"], baseline["sheets"][0]["colNumber"] = rows, cols
        return baseline

    def test_a_baseline_area_above_the_intake_cap_is_refused_without_scanning_it(self):
        cap = intake_xlsx.MAX_AREA_CELLS
        self.assertEqual(cap, 1000000)
        for label, rows, cols in (("one more row", cap + 1, 1), ("one more column", 1, cap + 1),
                                  ("a square just above", 1001, 1000), ("half and a bit", cap // 2 + 1, 2),
                                  ("huge", 10 ** 9, 10 ** 9), ("huge rows", 10 ** 12, 1)):
            with self.subTest(case=label):
                self.write(self.sized(rows, cols))
                with mock.patch.object(scan_excel, "find_headers", side_effect=AssertionError("looped over the area")):
                    rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
                self.assertEqual((rc, out), (2, ""))
                self.assertEqual(err, "error: the baseline area is larger than 1000000 cells (the intake never writes "
                                      "such a file)\n")
                self.assertEqual(self.files(), ["baseline.json"])

    def test_a_negative_row_or_column_count_is_refused_whatever_the_product(self):
        for rows, cols in ((-1, 1), (1, -1), (-1, -1), (-5, 2), (2, -5), (-10 ** 9, 1), (-1, 10 ** 9), (0, -1)):
            with self.subTest(rows=rows, cols=cols):
                self.write(self.sized(rows, cols))
                with mock.patch.object(scan_excel, "find_headers", side_effect=AssertionError("looped over the area")):
                    rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
                self.assertEqual((rc, out), (2, ""))
                self.assertIn("larger than 1000000 cells", err)

    def test_a_baseline_area_exactly_at_the_cap_is_accepted_and_one_cell_more_is_not(self):
        cap = intake_xlsx.MAX_AREA_CELLS
        for rows, cols in ((1000, 1000), (cap, 1), (1, cap), (cap // 2, 2), (0, 5), (5, 0), (0, 0)):
            with self.subTest(rows=rows, cols=cols):
                sheet = scan_excel.Sheet(self.sized(rows, cols))                     # builds, never loops over the area
                self.assertEqual((sheet.rows, sheet.cols), (rows, cols))
        for rows, cols in ((1000, 1001), (cap + 1, 1), (1, cap + 1)):
            with self.subTest(refused_rows=rows, cols=cols):
                with self.assertRaises(scan_excel.ScanError) as raised:
                    scan_excel.Sheet(self.sized(rows, cols))
                self.assertIn(str(cap), str(raised.exception))

    def test_the_scan_of_a_sheet_exactly_at_the_cap_runs_to_the_end(self):
        self.write(self.sized(intake_xlsx.MAX_AREA_CELLS, 1))
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("no header row", out)

    def test_a_baseline_with_an_empty_sheet_list_is_a_scan_error_not_an_unexpected_one(self):
        self.write({"sheets": []})
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("the baseline must hold exactly one well-formed sheet", err)

    def test_a_sheet_with_no_cells_at_all_has_no_header_and_is_not_an_error(self):
        rc, out, err = self.scan(sf.Grid(5, 15))
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("no header row", out)

    def test_a_cell_without_content_or_with_a_non_object_value_is_ignored(self):
        baseline = simple_grid(["DK 냉장고"]).baseline()
        cells = baseline["sheets"][0]["cells"]                  # in the title row, so a stray cell would hide the title
        cells["J1"], cells["K1"], cells["L1"] = {"style": 1}, "oops", {"content": ""}
        rc, out, err = self.scan(baseline)
        self.assertEqual((rc, err), (0, ""))
        self.assertIn('block title: "출하 수량"', out)
        self.assertEqual(len(self.draft()["bindings"]), 1)

    def test_typed_numbers_given_as_json_numbers_count_as_typed_too(self):
        grid = simple_grid(["DK 냉장고"])
        grid.put(3, 3, 12.5).put(3, 4, 7)
        rc, out, err = self.scan(grid)
        self.assertEqual(sorted(self.bound(out)), [3])


# ---------------------------------------------------------------- the command line

class CommandLineTests(ScanCase):
    def twin_file(self):
        return self.write(sf.twin())

    def test_the_default_out_is_next_to_the_baseline_not_in_the_working_folder(self):
        self.twin_file()
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        previous = os.getcwd()
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, previous)
        rc, out, err = self.cli(["--baseline", self.baseline_path])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.files(), ["baseline.json", "kpi_spec.draft.json"])
        self.assertEqual(os.listdir(elsewhere), [])
        self.assertIn(os.path.join(self.work, "kpi_spec.draft.json"), out)

    def test_a_relative_baseline_path_puts_the_default_out_in_its_folder(self):
        self.twin_file()
        previous = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, previous)
        rc, out, err = self.cli(["--baseline", os.path.join("work", "baseline.json")])
        self.assertEqual(rc, 0)
        self.assertEqual(self.files(), ["baseline.json", "kpi_spec.draft.json"])

    def test_an_existing_out_is_not_replaced_without_force(self):
        self.twin_file()
        with open(self.out_path, "wb") as handle:
            handle.write(b"keep me")
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
        self.assertEqual(rc, 2)
        self.assertIn("exists already: pass --force to replace it", err)
        self.assertEqual(len(err.splitlines()), 1)
        with open(self.out_path, "rb") as handle:
            self.assertEqual(handle.read(), b"keep me")
        self.assertEqual(self.files(), ["baseline.json", "draft.json"])

    def test_an_existing_out_without_force_is_refused_before_the_scan_prints_or_even_runs(self):
        # S2: the old order printed the whole report and only then said that the draft could not be written
        self.twin_file()
        with open(self.out_path, "wb") as handle:
            handle.write(b"keep me")
        with mock.patch.object(scan_excel, "scan", wraps=scan_excel.scan) as scan_call, \
                mock.patch.object(bind_kpi, "bind", wraps=bind_kpi.bind) as bind_call:
            rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
        self.assertEqual((rc, out), (2, ""))
        self.assertEqual(err, "error: %s exists already: pass --force to replace it\n" % self.out_path)
        self.assertEqual((scan_call.call_count, bind_call.call_count), (0, 0))
        with open(self.out_path, "rb") as handle:
            self.assertEqual(handle.read(), b"keep me")

    def test_the_default_out_that_exists_is_refused_before_anything_is_printed_too(self):
        self.twin_file()
        default = os.path.join(self.work, "kpi_spec.draft.json")
        with open(default, "wb") as handle:
            handle.write(b"keep me")
        rc, out, err = self.cli(["--baseline", self.baseline_path])
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("exists already: pass --force to replace it", err)
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--force"])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("draft spec written", out)

    def test_a_scan_that_finds_nothing_with_an_existing_out_is_still_refused_not_a_quiet_zero(self):
        self.write(sf.Grid(5, 15).baseline())
        with open(self.out_path, "wb") as handle:
            handle.write(b"keep me")
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("pass --force", err)

    @POSIX_ONLY
    def test_an_error_text_that_holds_the_out_path_is_cleaned_of_escape_characters(self):
        self.twin_file()
        path = os.path.join(self.work, "draft\x1b[31m.json")
        with open(path, "wb") as handle:
            handle.write(b"x")
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", path])
        self.assertEqual(rc, 2)
        self.assertIn("exists already", err)
        self.assertEqual(control_characters(err), [])

    def test_force_replaces_an_existing_out(self):
        self.twin_file()
        with open(self.out_path, "wb") as handle:
            handle.write(b"old")
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path, "--force"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.draft()["bindings"]), 14)
        self.assertEqual(self.files(), ["baseline.json", "draft.json"])

    def test_out_never_replaces_the_baseline_even_with_force(self):
        data = self.twin_file()
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.baseline_path, "--force"])
        self.assertEqual(rc, 2)
        self.assertIn("--out is the input file", err)
        with open(self.baseline_path, "rb") as handle:
            self.assertEqual(handle.read(), data)
        self.assertEqual(self.files(), ["baseline.json"])

    @POSIX_ONLY
    def test_out_that_is_a_link_to_the_baseline_is_refused_too(self):
        data = self.twin_file()
        link = os.path.join(self.work, "link.json")
        os.symlink(self.baseline_path, link)
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", link, "--force"])
        self.assertEqual(rc, 2)
        with open(self.baseline_path, "rb") as handle:
            self.assertEqual(handle.read(), data)

    def test_the_draft_is_written_through_the_intake_helper_with_the_force_flag_and_the_baseline_path(self):
        self.twin_file()
        with mock.patch.object(intake_xlsx, "write_baseline", wraps=intake_xlsx.write_baseline) as helper:
            rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
            self.assertEqual(rc, 0)
            rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path, "--force"])
        self.assertEqual(helper.call_count, 2)
        first, second = helper.call_args_list
        self.assertEqual(first[0][0], self.out_path)
        self.assertEqual(first[1], {"force": False, "source_path": self.baseline_path})
        self.assertEqual(second[1], {"force": True, "source_path": self.baseline_path})
        self.assertEqual(first[0][1], intake_xlsx.dump_bytes(self.draft()))

    @POSIX_ONLY
    def test_the_draft_file_is_private_and_no_intake_temp_file_is_left(self):
        self.twin_file()
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
        self.assertEqual(rc, 0)
        self.assertEqual(os.stat(self.out_path).st_mode & 0o777, 0o600)
        self.assertEqual([name for name in os.listdir(self.work) if name.startswith(".")], [])
        self.assertEqual(self.files(), ["baseline.json", "draft.json"])

    def test_a_write_failure_is_exit_two_with_one_line_and_no_temp_file(self):
        self.twin_file()
        with mock.patch("os.replace", side_effect=OSError(28, "No space left on device")):
            rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
        self.assertEqual(rc, 2)
        self.assertIn("cannot write", err)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertEqual(os.listdir(self.work), ["baseline.json"])

    def test_the_folder_of_out_must_exist(self):
        self.twin_file()
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", os.path.join(self.work, "no", "x.json")])
        self.assertEqual(rc, 2)
        self.assertIn("the folder of --out does not exist", err)

    def test_nothing_to_propose_exits_zero_and_writes_no_file(self):
        for label, grid in (("no header", sf.Grid(5, 15)), ("no row matches", simple_grid(["Misc one", "Misc two"]))):
            with self.subTest(case=label):
                rc, out, err = self.scan(grid, default_out=True)
                self.assertEqual((rc, err), (0, ""))
                self.assertEqual(self.files(), ["baseline.json"])
                self.assertNotIn("draft spec written", out)
                self.assertNotIn("bind dry run", out)

    def test_the_last_lines_name_the_dry_run_then_the_written_draft(self):
        self.twin_file()
        rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
        lines = out.splitlines()
        self.assertTrue(lines[-2].startswith("bind dry run: ok"))
        self.assertTrue(lines[-1].startswith("draft spec written: %s" % self.out_path))
        self.assertIn("a proposal: the user confirms it", lines[-1])

    def test_missing_malformed_or_oversized_baseline_inputs_are_exit_two_with_one_line(self):
        missing = os.path.join(self.work, "missing.json")
        cases = [("missing file", missing, None), ("not json", None, b"{nope"), ("empty", None, b""),
                 ("a bom and not json", None, b"\xef\xbb\xbfnope"), ("not utf-8", None, b"\xff\xfe\x00")]
        for label, path, data in cases:
            with self.subTest(case=label):
                path = path or self.baseline_path
                if data is not None:
                    with open(path, "wb") as handle:
                        handle.write(data)
                rc, out, err = self.cli(["--baseline", path, "--out", self.out_path])
                self.assertEqual((rc, out), (2, ""))
                self.assertTrue(err.startswith("error: "), err)
                self.assertEqual(len(err.splitlines()), 1)

    def test_an_unexpected_exception_is_exit_one_with_one_line_and_never_its_text(self):
        self.twin_file()
        with mock.patch.object(scan_excel, "scan", side_effect=RuntimeError("secret-token-123 /home/user")):
            rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
        self.assertEqual((rc, out, err), (1, "", "error: unexpected RuntimeError\n"))

    def test_a_too_deeply_nested_input_is_exit_two(self):
        self.twin_file()
        with mock.patch.object(scan_excel, "scan", side_effect=RecursionError()):
            rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
        self.assertEqual((rc, out, err), (2, "", "error: the input is nested too deeply\n"))

    def test_a_scan_error_a_bind_error_and_an_intake_error_are_all_exit_two(self):
        self.twin_file()
        for exc in (scan_excel.ScanError("scan problem"), bind_kpi.BindError("bind problem"),
                    intake_xlsx.IntakeError("intake problem")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(scan_excel, "scan", side_effect=exc):
                    rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
                self.assertEqual((rc, out, err), (2, "", "error: %s\n" % exc))

    def test_there_is_no_catalog_option_and_no_option_can_be_abbreviated(self):
        parser = scan_excel.build_parser()
        options = {opt for action in parser._actions for opt in action.option_strings}
        self.assertEqual(options, {"-h", "--help", "--baseline", "--out", "--force", "--aliases", "--project"})
        self.twin_file()
        for argv in (["--catalog", "x.json"], ["--base", self.baseline_path], ["--ou", self.out_path],
                     ["--forc"], ["--alias", "x"], ["--catalog=x"], ["--proj", "dksk"]):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                    scan_excel.main(["--baseline", self.baseline_path, "--out", self.out_path] + argv
                                    if argv[0] != "--base" else argv)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("unrecognized arguments" if argv[0] != "--base" else "required", err.getvalue())
        self.assertEqual(self.files(), ["baseline.json"])

    def test_the_baseline_option_is_required(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            scan_excel.main([])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--baseline", err.getvalue())

    def test_the_scan_is_offline_even_when_everything_network_raises(self):
        self.twin_file()
        import urllib.request
        with mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("network")), \
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("network")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("network")):
            rc, out, err = self.cli(["--baseline", self.baseline_path, "--out", self.out_path])
        self.assertEqual((rc, err), (0, ""))

    def test_korean_text_is_written_to_a_stream_that_cannot_encode_it(self):
        self.twin_file()
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        rc = scan_excel.main(["--baseline", self.baseline_path, "--out", self.out_path], out, io.StringIO())
        out.flush()
        self.assertEqual(rc, 0)
        self.assertIn("DK 냉장고".encode("utf-8"), raw.getvalue())

    def test_the_main_streams_default_to_the_process_streams(self):
        self.twin_file()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = scan_excel.main(["--baseline", self.baseline_path, "--out", self.out_path])
        self.assertEqual(rc, 0)
        self.assertIn("draft spec written", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_the_draft_name_matches_the_ignore_and_hygiene_patterns_of_the_public_repo(self):
        self.twin_file()
        rc, out, err = self.cli(["--baseline", self.baseline_path])
        self.assertEqual(rc, 0)
        name = "kpi_spec.draft.json"
        self.assertTrue(fnmatch.fnmatch(name, "*.draft.json"))
        self.assertIn(name, workflow_outputs(self.work))
        self.assertIn("*.draft.json", WORKFLOW_OUTPUT_PATTERNS)
        with open(os.path.join(str(REPO), ".gitignore"), encoding="utf-8") as handle:
            patterns = gitignore_patterns(handle.read())
        self.assertTrue(any(fnmatch.fnmatch(name, pattern) for pattern in patterns))
        self.assertIn("*.draft.json", patterns)


# ---------------------------------------------------------------- the module itself

class ModuleTests(unittest.TestCase):
    def test_the_documented_constants(self):
        self.assertEqual(scan_excel.DEFAULT_NAME, "KPI draft")
        self.assertEqual(scan_excel.LABEL_LIMIT, 40)
        self.assertEqual(scan_excel.MAX_BLANK_ROWS, 2)

    def test_clean_replaces_control_characters_strips_and_cuts(self):
        self.assertEqual(scan_excel._clean("a\x1b[0mb"), "a [0mb")
        self.assertEqual(scan_excel._clean("  a\tb \n"), "a b")
        self.assertEqual(scan_excel._clean("x" * 40), "x" * 40)
        self.assertEqual(scan_excel._clean("x" * 41), "x" * 39 + "…")
        self.assertEqual(scan_excel._clean("y" * 301, 300), "y" * 299 + "…")
        self.assertEqual(scan_excel._clean(ValueError("a\x07b")), "a b")


if __name__ == "__main__":
    unittest.main()
