"""Command line `main(argv, out, err, ...)` of scripts/intake_xlsx.py against the fake Odoo server.

Every run goes through `run_intake`, which also checks, at the end of EVERY test, that the API key never reached
stdout / stderr and that no traceback was printed. Sleep and clock are injected: nothing here really waits.
"""
import contextlib
import copy
import hashlib
import io
import json
import os
import unittest
from unittest import mock

import intake_fixtures as fx
from fake_odoo import FAKE_KEY, FakeOdoo
from helpers import ExplodingOpener, IsolatedTestCase, odoo_rpc

import intake_xlsx

POSIX_ONLY = unittest.skipIf(os.name == "nt", "symlinks / hard links need POSIX here")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
URL_SUFFIX = "/web?debug=1#action=action_open_spreadsheet&spreadsheet_id=%d"
FRESH_FIELDS = ["handler", "spreadsheet_snapshot"]
POLL_FIELDS = ["name", "handler", "spreadsheet_snapshot"]
NOTE = "note: if the server did create a clone it stays in Documents"


class FakeTime:
    """Injected `sleep` / `clock`: sleeping advances the clock instantly."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class IntakeCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeOdoo()
        self.fake.start()
        self.addCleanup(self.fake.stop)
        self.use_url(self.fake.url)
        self.time = FakeTime()
        self.streams = []
        self.out_path = os.path.join(self.tmp, "baseline.json")
        self.addCleanup(self.check_streams)

    def check_streams(self):
        for out, err in self.streams:
            self.assertNotIn(FAKE_KEY, out)
            self.assertNotIn(FAKE_KEY, err)
            self.assertNotIn("Traceback", err)
            self.assertNotIn("Traceback", out)

    def use_url(self, url):
        os.environ.update({"ODOO_KPI_URL": url, "ODOO_KPI_DB": "fake_db", "ODOO_KPI_LOGIN": "qa.user",
                           "ODOO_KPI_API_KEY": FAKE_KEY})

    def run_intake(self, *argv, sleep=None, clock=None, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        try:
            code = intake_xlsx.main(list(argv), out, err, sleep=sleep or self.time.sleep,
                                    clock=clock or self.time.clock, **kwargs)
        except KeyboardInterrupt:        # an escaped Ctrl-C would abort the whole test run instead of failing one test
            self.fail("KeyboardInterrupt escaped main(); stderr so far: %s" % err.getvalue())
        self.streams.append((out.getvalue(), err.getvalue()))
        return code, out.getvalue(), err.getvalue()

    def write_input(self, data=None, name="input.json", raw=None, **kwargs):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as handle:
            handle.write(raw if raw is not None else fx.as_bytes(fx.make_snapshot() if data is None else data,
                                                                 **kwargs))
        return path

    def crop_args(self, *more):
        return ["--sheet", fx.SHEET, "--area", fx.AREA, "--out", self.out_path, *more]

    def stage_snapshot(self, doc_id=5, data=None, **kwargs):
        self.fake.add_document(doc_id, name="book", snapshot=fx.as_bytes(fx.make_snapshot() if data is None else data,
                                                                         **kwargs))

    def stage_xlsx(self, xlsx_id=7, data=None, **kwargs):
        self.fake.add_document(xlsx_id, name="book.xlsx", handler=False, mimetype=XLSX_MIME)
        self.fake.clone_snapshot = fx.as_bytes(fx.make_snapshot() if data is None else data, **kwargs)

    def xlsx_args(self, *more, xlsx_id=7):
        return ["--xlsx-document", str(xlsx_id), "--wait-seconds", "10", "--poll-interval", "2", *more]

    def expected_baseline_bytes(self, data=None, area=fx.AREA):
        return intake_xlsx.dump_bytes(fx.crop_it(fx.make_snapshot() if data is None else data, area=area)[0])

    def read_out(self):
        with open(self.out_path, "rb") as handle:
            return handle.read()

    def leftovers(self, directory=None):
        directory = directory or self.tmp
        return [name for name in os.listdir(directory) if name.startswith(".intake-")]

    def assertFailed(self, result, code, fragment):
        got, out, err = result
        self.assertEqual(got, code, err)
        lines = [line for line in err.splitlines() if line.startswith("error: ")]
        self.assertEqual(len(lines), 1, err)
        self.assertIn(fragment, lines[0])
        self.assertEqual(out, "")

    def clone_calls(self):
        return self.fake.execute_calls("documents.document", "clone_xlsx_into_spreadsheet")

    def unlink_calls(self):
        return self.fake.execute_calls("documents.document", "unlink")

    def document_calls(self):
        """[(method, positional args)] of every `documents.document` request, in the order the server got them."""
        return [(body["params"]["args"][4], body["params"]["args"][5])
                for body in self.fake.calls("object", "execute_kw") if body["params"]["args"][3] == "documents.document"]

    def reads_of(self, doc_id):
        return [args for method, args in self.document_calls() if method == "read" and args[0] == [doc_id]]

    def fresh_reads_of(self, doc_id):
        """The reads that only ask `handler` and `spreadsheet_snapshot`: the freshness check of a clone."""
        return [args for args in self.reads_of(doc_id) if args[1] == FRESH_FIELDS]

    def polls_of(self, doc_id):
        """The reads that also ask `name`: the polls for the user's snapshot (and the --document-id read)."""
        return [args for args in self.reads_of(doc_id) if args[1] == POLL_FIELDS]

    def assertNoCloneOrUnlink(self):
        self.assertEqual((self.clone_calls(), self.unlink_calls(), self.fake.cloned, self.fake.unlinked),
                         ([], [], [], []))


# ---------------------------------------------------------------- 1. happy paths per source

class FileRouteTests(IntakeCase):
    def test_inventory_lists_the_sheets_and_touches_no_server(self):
        path = self.write_input()
        code, out, err = self.run_intake("--file", path)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"route": "download", "sheets": [
            {"name": fx.SHEET, "visible": True, "content_cells": 8, "last_cell": "G30"},
            {"name": fx.OTHER_SHEET, "visible": True, "content_cells": 1, "last_cell": "A1"}]})
        self.assertEqual(self.fake.requests, [])
        self.assertFalse(os.path.exists(self.out_path))

    def test_inventory_does_not_need_a_config(self):
        os.environ.pop("ODOO_KPI_URL")
        code, out, err = self.run_intake("--file", self.write_input())
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["route"], "download")

    def test_crop_writes_the_baseline_and_reports(self):
        path = self.write_input()
        code, out, err = self.run_intake("--file", path, *self.crop_args())
        self.assertEqual((code, err), (0, ""))
        written = self.read_out()
        self.assertEqual(written, self.expected_baseline_bytes())
        report = json.loads(out)
        self.assertEqual(report, {"sheet": fx.SHEET, "area": fx.AREA, "cells": 6, "merges": 2, "content_cells": 5,
                                  "dropped_content_cells": 3, "route": "download", "out": self.out_path,
                                  "bytes": len(written), "sha256": hashlib.sha256(written).hexdigest(),
                                  "revision_id": "START_REVISION"})
        self.assertEqual(self.fake.requests, [])

    def test_the_written_file_has_the_cropped_content_in_natural_order(self):
        self.run_intake("--file", self.write_input(), *self.crop_args())
        data = json.loads(self.read_out().decode("utf-8"))
        sheet = data["sheets"][0]
        self.assertEqual(list(sheet["cells"]), ["A1", "C1", "A2", "B2", "D3", "E6"])
        self.assertEqual(sheet["merges"], ["A1:B1", "D5:E6"])
        self.assertEqual((sheet["rowNumber"], sheet["colNumber"]), (6, 5))
        self.assertEqual(list(sheet["rows"]), ["0", "5"])
        self.assertEqual(list(sheet["cols"]), ["0", "4"])
        self.assertEqual(data["revisionId"], "START_REVISION")
        raw = fx.make_snapshot()
        for key in raw:
            if key not in ("sheets", "revisionId"):
                self.assertEqual(data[key], raw[key], key)

    def test_a_file_with_a_bom_and_indentation_works(self):
        path = self.write_input(indent=2, bom=True)
        code, _, err = self.run_intake("--file", path, *self.crop_args())
        self.assertEqual((code, self.read_out()), (0, self.expected_baseline_bytes()), err)

    def test_a_missing_or_unreadable_file_is_a_clean_error(self):
        self.assertFailed(self.run_intake("--file", os.path.join(self.tmp, "nope.json")), 2, "cannot read")
        self.assertFailed(self.run_intake("--file", self.tmp), 2, "cannot read")


class SnapshotRouteTests(IntakeCase):
    def test_inventory_from_a_document_snapshot(self):
        self.stage_snapshot(5)
        code, out, err = self.run_intake("--document-id", "5")
        self.assertEqual((code, err), (0, ""))
        report = json.loads(out)
        self.assertEqual(report["route"], "snapshot")
        self.assertEqual([s["name"] for s in report["sheets"]], [fx.SHEET, fx.OTHER_SHEET])

    def test_crop_from_a_document_snapshot_and_the_requests_sent(self):
        self.stage_snapshot(5)
        code, out, err = self.run_intake("--document-id", "5", *self.crop_args())
        self.assertEqual((code, err), (0, ""))
        written = self.read_out()
        self.assertEqual(written, self.expected_baseline_bytes())
        report = json.loads(out)
        self.assertEqual((report["route"], report["sha256"], report["bytes"]),
                         ("snapshot", hashlib.sha256(written).hexdigest(), len(written)))
        self.assertEqual(self.fake.execute_calls("documents.document", "read"),
                         [[[5], ["name", "handler", "spreadsheet_snapshot"]]])
        self.assertNoCloneOrUnlink()

    def test_document_and_file_routes_never_clone_or_unlink_even_with_keep(self):
        self.stage_snapshot(5)
        for argv in (["--document-id", "5", "--keep"], ["--document-id", "5"],
                     ["--file", self.write_input(), "--keep"]):
            with self.subTest(argv=argv):
                self.run_intake(*argv, *self.crop_args("--force"))
        self.assertNoCloneOrUnlink()


class XlsxRouteTests(IntakeCase):
    def setUp(self):
        super().setUp()
        self.stage_xlsx(7)

    def test_inventory_through_a_clone_that_is_deleted_afterwards(self):
        code, out, err = self.run_intake(*self.xlsx_args())
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual((report["route"], [s["name"] for s in report["sheets"]]),
                         ("snapshot", [fx.SHEET, fx.OTHER_SHEET]))
        self.assertEqual(self.fake.unlinked, [[900]])

    def test_crop_through_a_clone_gives_the_same_bytes_and_the_expected_report(self):
        code, out, err = self.run_intake(*self.xlsx_args(*self.crop_args()))
        self.assertEqual(code, 0, err)
        written = self.read_out()
        self.assertEqual(written, self.expected_baseline_bytes())
        report = json.loads(out)
        self.assertEqual((report["route"], report["sha256"], report["cells"], report["dropped_content_cells"]),
                         ("snapshot", hashlib.sha256(written).hexdigest(), 6, 3))
        self.assertEqual(self.fake.cloned, [(7, 900)])

    def test_requests_sent_to_the_server(self):
        self.run_intake(*self.xlsx_args(*self.crop_args()))
        self.assertEqual(self.document_calls(), [
            ("read", [[7], ["name", "handler", "mimetype"]]),
            ("clone_xlsx_into_spreadsheet", [[7]]),
            ("read", [[900], ["handler", "spreadsheet_snapshot"]]),      # the freshness check of the clone
            ("read", [[900], ["name", "handler", "spreadsheet_snapshot"]]),   # the one poll
            ("unlink", [[900]])])
        self.assertEqual(len(self.fake.calls("common", "login")), 1)

    def test_the_url_for_the_user_and_the_progress_go_to_stderr_only(self):
        code, out, err = self.run_intake(*self.xlsx_args(*self.crop_args()))
        self.assertEqual(code, 0)
        self.assertIn(self.fake.url + URL_SUFFIX % 900, err.splitlines())
        self.assertIn("File > Snapshot", err)
        self.assertIn("deleted the clone (spreadsheet document 900)", err)
        self.assertNotIn("/web?", out)
        self.assertEqual(json.loads(out)["route"], "snapshot")

    def test_the_url_has_no_double_slash_when_the_config_url_ends_with_a_slash(self):
        self.use_url(self.fake.url + "/")
        _, _, err = self.run_intake(*self.xlsx_args())
        self.assertIn(self.fake.url + URL_SUFFIX % 900, err.splitlines())

    def test_the_url_carries_no_key_and_no_login(self):
        _, _, err = self.run_intake(*self.xlsx_args())
        url = [line for line in err.splitlines() if "/web?" in line]
        self.assertEqual(len(url), 1)
        self.assertNotIn(FAKE_KEY, url[0])
        self.assertNotIn("qa.user", url[0])
        self.assertNotIn("api_key", url[0])

    def test_the_snapshot_that_appears_after_some_polls_is_waited_for(self):
        self.fake.snapshot_after_polls = 2
        code, _, err = self.run_intake(*self.xlsx_args(*self.crop_args()))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.time.sleeps, [2.0, 2.0])
        self.assertEqual((len(self.fresh_reads_of(900)), len(self.polls_of(900))), (1, 3))
        self.assertEqual(self.read_out(), self.expected_baseline_bytes())
        self.assertEqual(self.fake.unlinked, [[900]])

    def test_a_plain_positive_integer_answer_is_the_clone_id(self):
        code, _, err = self.run_intake(*self.xlsx_args())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.unlinked, [[self.fake.cloned[-1][1]]])
        self.assertNotIn("note:", err)

    def test_any_other_clone_answer_is_refused_nothing_is_deleted_and_the_user_is_told(self):
        for mode in ("dict", "dict_id", "garbage", "zero", "bool", "str_int", "negative", "source", "none", "list"):
            with self.subTest(mode=mode):
                self.fake.clone_mode = mode
                result = self.run_intake(*self.xlsx_args(*self.crop_args()))
                self.assertFailed(result, 2, "did not return the id of a new clone")
                self.assertIn("note: if the server did create a clone it stays in Documents", result[2])
        self.assertEqual(self.fake.unlinked, [])
        self.assertEqual(self.unlink_calls(), [])
        self.assertFalse(os.path.exists(self.out_path))

    def test_a_server_error_on_clone_is_exit_1_and_nothing_is_deleted(self):
        self.fake.clone_mode = "error"
        code, out, err = self.run_intake(*self.xlsx_args())
        self.assertEqual((code, out), (1, ""))
        self.assertIn("error: ", err)
        self.assertEqual(self.fake.unlinked, [])


# ---------------------------------------------------------------- 2. AC3: determinism

class DeterminismTests(IntakeCase):
    def test_two_runs_write_byte_identical_files(self):
        path = self.write_input()
        second = os.path.join(self.tmp, "second.json")
        self.run_intake("--file", path, *self.crop_args())
        self.run_intake("--file", path, "--sheet", fx.SHEET, "--area", fx.AREA, "--out", second)
        with open(self.out_path, "rb") as one, open(second, "rb") as two:
            self.assertEqual(one.read(), two.read())

    def test_snapshot_route_and_download_route_give_the_same_bytes_for_a_pair_differing_in_revision_id(self):
        download = fx.make_snapshot(revision_id=fx.FILE_REVISION)
        snapshot = fx.make_snapshot(revision_id="0b6c2c1e-9d0f-4c43-8a41-5f3d2a9e7b10")
        self.assertNotEqual(download["revisionId"], snapshot["revisionId"])
        path = self.write_input(download)
        self.stage_snapshot(5, snapshot)
        self.stage_xlsx(7, snapshot)
        by_route = os.path.join(self.tmp, "by-snapshot.json")
        by_clone = os.path.join(self.tmp, "by-clone.json")
        self.assertEqual(self.run_intake("--file", path, *self.crop_args())[0], 0)
        self.assertEqual(self.run_intake("--document-id", "5", "--sheet", fx.SHEET, "--area", fx.AREA,
                                         "--out", by_route)[0], 0)
        self.assertEqual(self.run_intake(*self.xlsx_args("--sheet", fx.SHEET, "--area", fx.AREA,
                                                         "--out", by_clone))[0], 0)
        contents = []
        for name in (self.out_path, by_route, by_clone):
            with open(name, "rb") as handle:
                contents.append(handle.read())
        self.assertEqual(contents[0], contents[1])
        self.assertEqual(contents[1], contents[2])

    def test_the_sha256_of_the_two_routes_agree(self):
        self.stage_snapshot(5, fx.make_snapshot(revision_id="another-id"))
        first = json.loads(self.run_intake("--file", self.write_input(), *self.crop_args())[1])
        second = json.loads(self.run_intake("--document-id", "5", *self.crop_args("--force"))[1])
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual((first["route"], second["route"]), ("download", "snapshot"))

    def test_the_file_is_lf_only_valid_utf8_with_text_kept_unescaped(self):
        self.run_intake("--file", self.write_input(), *self.crop_args())
        written = self.read_out()
        self.assertNotIn(b"\r", written)
        text = written.decode("utf-8")
        self.assertIn(fx.NON_ASCII, text)
        self.assertNotIn("\\u", text)
        self.assertTrue(written.endswith(b"\n") and not written.endswith(b"\n\n"))

    def test_the_bytes_are_the_same_when_os_linesep_is_crlf(self):
        path = self.write_input()
        self.run_intake("--file", path, *self.crop_args())
        plain = self.read_out()
        crlf_out = os.path.join(self.tmp, "crlf.json")
        with mock.patch.object(os, "linesep", "\r\n"):
            code, _, err = self.run_intake("--file", path, "--sheet", fx.SHEET, "--area", fx.AREA, "--out", crlf_out)
        self.assertEqual(code, 0, err)
        with open(crlf_out, "rb") as handle:
            self.assertEqual(handle.read(), plain)
        self.assertNotIn(b"\r", plain)


# ---------------------------------------------------------------- 3. the guards, through the command line

class GuardThroughCliTests(IntakeCase):
    def assertRefused(self, data, fragment, **kwargs):
        path = self.write_input(data)
        result = self.run_intake("--file", path, *self.crop_args(), **kwargs)
        self.assertFailed(result, 2, fragment)
        self.assertFalse(os.path.exists(self.out_path))
        self.assertEqual(self.leftovers(), [])

    def test_each_guard_fails_with_exit_2_one_error_line_and_no_file(self):
        cases = {
            "straddling merge": (fx.make_snapshot(sheets=[fx.make_sheet(merges=["D5:F6"])]), "straddles"),
            "formula outside": (fx.with_formula("=Z99"), "reads outside"),
            "formula other sheet": (fx.with_formula("=Other!A1"), "another sheet"),
            "formula whole column": (fx.with_formula("=SUM(A:A)"), "whole row or column"),
            "unknown top key": (fx.make_snapshot(mystery=1), "mystery"),
            "unknown sheet key": (fx.with_sheet_extra(mystery=1), "mystery"),
            "unknown cell key": (fx.with_cell("D4", {"content": "x", "note": "n"}), "does not know"),
            "pivots": (fx.make_snapshot(pivots={"1": {}}), "pivots"),
            "lists": (fx.make_snapshot(lists={"1": {}}), "lists"),
            "global filters": (fx.make_snapshot(globalFilters=[{"id": "f"}]), "globalFilters"),
            "entities": (fx.make_snapshot(entities={"x": 1}), "entities"),
            "figures": (fx.with_sheet_extra(figures=[{"id": "f"}]), "figures"),
            "conditional formats": (fx.with_sheet_extra(conditionalFormats=[{"id": "c"}]), "conditionalFormats"),
            "validation rules": (fx.with_sheet_extra(dataValidationRules=[{"id": "d"}]), "dataValidationRules"),
            "filter tables": (fx.with_sheet_extra(filterTables=[{"id": "t"}]), "filterTables"),
            "header groups": (fx.with_sheet_extra(headerGroups={"ROW": [{"start": 1, "end": 2}], "COL": []}),
                              "grouped"),
            "unknown row property": (fx.make_snapshot(sheets=[fx.make_sheet(rows={"0": {"size": 1, "x": 1}})]), "rows"),
            "dangling style": (fx.with_cell("D4", {"style": 99}), "style"),
            "dangling format": (fx.with_cell("D4", {"format": 99}), "format"),
            "dangling border": (fx.with_cell("D4", {"border": 99}), "border"),
            "no sheets": ({"sheets": []}, "no sheets"),
        }
        for label, (data, fragment) in cases.items():
            with self.subTest(label):
                self.assertRefused(data, fragment)

    def test_the_sheet_and_area_guards(self):
        path = self.write_input()
        out = ["--out", self.out_path]
        self.assertFailed(self.run_intake("--file", path, "--sheet", "Nope", "--area", fx.AREA, *out), 2, "found 0")
        self.assertFailed(self.run_intake("--file", path, "--sheet", fx.SHEET, "--area", "A1:E1001", *out), 2,
                          "bigger than the sheet")
        for area in ("B1:E6", "A1", "A1:", "E6", "a1:e6", "A1:E6:F7", "A1:A0"):
            with self.subTest(area=area):
                self.assertFailed(self.run_intake("--file", path, "--sheet", fx.SHEET, "--area", area, *out), 2, "")
        data = fx.make_snapshot(sheets=[fx.make_sheet(), fx.make_sheet()])
        self.assertFailed(self.run_intake("--file", self.write_input(data, name="dup.json"), *self.crop_args()), 2,
                          "found 2")
        self.assertFalse(os.path.exists(self.out_path))

    def test_passing_neighbours_of_the_guards_still_produce_a_file(self):
        data = fx.make_snapshot(sheets=[fx.make_sheet(merges=["A1:B2", "F1:G2"])])
        sheet = data["sheets"][0]
        sheet["cells"]["D4"] = {"content": '=IF(A1="x!",$A$1+LOG10(B2),"Z99")+ATAN2(A1,B2)+ABCD5'}
        code, _, err = self.run_intake("--file", self.write_input(data), *self.crop_args())
        self.assertEqual(code, 0, err)
        written = json.loads(self.read_out())
        self.assertEqual(written["sheets"][0]["merges"], ["A1:B2"])


# ---------------------------------------------------------------- 4. cleanup of the clone (the heart of the AC)

class CloneCleanupTests(IntakeCase):
    def setUp(self):
        super().setUp()
        self.stage_xlsx(7)

    def test_success_unlinks_exactly_the_clone_and_never_the_source(self):
        self.fake.next_document_id = 4242
        code, _, err = self.run_intake(*self.xlsx_args(*self.crop_args()))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.cloned, [(7, 4242)])
        self.assertEqual(self.unlink_calls(), [[[4242]]])
        self.assertEqual(self.fake.unlinked, [[4242]])
        self.assertIn(7, self.fake.documents)
        self.assertNotIn(4242, self.fake.documents)

    def test_a_dict_answer_never_leads_to_an_unlink_of_its_id(self):
        self.fake.next_document_id = 5151
        self.fake.clone_mode = "dict"
        self.assertFailed(self.run_intake(*self.xlsx_args()), 2, "new clone")
        self.assertEqual((self.fake.unlinked, self.unlink_calls()), ([], []))

    def test_an_answer_that_is_the_source_id_is_refused_and_the_source_is_never_unlinked(self):
        self.fake.clone_mode = "source"
        self.assertFailed(self.run_intake(*self.xlsx_args()), 2, "new clone")
        self.assertEqual((self.fake.unlinked, self.unlink_calls()), ([], []))
        self.assertIn(7, self.fake.documents)

    def test_timeout_is_exit_2_with_the_snapshot_hint_and_the_clone_is_still_unlinked(self):
        self.fake.snapshot_after_polls = 999
        result = self.run_intake("--xlsx-document", "7", "--wait-seconds", "5", "--poll-interval", "2",
                                 *self.crop_args())
        self.assertFailed(result, 2, "no snapshot after 5 seconds")
        self.assertIn("File > Snapshot", result[2])
        self.assertEqual(self.time.sleeps, [2.0, 2.0, 2.0])
        self.assertEqual(self.fake.unlinked, [[900]])
        self.assertFalse(os.path.exists(self.out_path))

    def test_the_last_poll_before_the_deadline_is_accepted_and_one_more_is_a_timeout(self):
        argv = ["--xlsx-document", "7", "--wait-seconds", "4", "--poll-interval", "2"]
        self.fake.snapshot_after_polls = 2
        self.assertEqual(self.run_intake(*argv)[0], 0)
        self.assertEqual(self.fake.unlinked, [[900]])
        self.fake.snapshot_after_polls = 3
        self.fake.unlinked.clear()
        self.assertFailed(self.run_intake(*argv), 2, "no snapshot after 4 seconds")
        self.assertEqual(self.fake.unlinked, [[901]])

    def test_a_crop_failure_after_the_snapshot_arrived_still_unlinks(self):
        self.fake.clone_snapshot = fx.as_bytes(fx.make_snapshot(sheets=[fx.make_sheet(merges=["D5:F6"])]))
        self.assertFailed(self.run_intake(*self.xlsx_args(*self.crop_args())), 2, "straddles")
        self.assertEqual(self.fake.unlinked, [[900]])
        self.assertFalse(os.path.exists(self.out_path))

    def test_a_snapshot_that_is_not_json_still_unlinks(self):
        self.fake.clone_snapshot = b"this is not json"
        self.assertFailed(self.run_intake(*self.xlsx_args()), 2, "not spreadsheet JSON")
        self.assertEqual(self.fake.unlinked, [[900]])

    def test_a_refused_overwrite_after_the_snapshot_arrived_still_unlinks_and_keeps_the_file(self):
        with open(self.out_path, "wb") as handle:
            handle.write(b"precious")
        self.assertFailed(self.run_intake(*self.xlsx_args(*self.crop_args())), 2, "exists already")
        self.assertEqual(self.fake.unlinked, [[900]])
        self.assertEqual(self.read_out(), b"precious")

    def test_a_server_error_while_polling_is_exit_1_and_the_clone_is_still_unlinked(self):
        self.fake.read_error_on_polls = set(range(20))          # the freshness read works, every poll fails
        code, out, err = self.run_intake(*self.xlsx_args())
        self.assertEqual((code, out), (1, ""))
        self.assertIn("error: ", err)
        self.assertNotIn(NOTE, err)
        self.assertEqual(self.fake.unlinked, [[900]])

    def test_keyboard_interrupt_during_the_wait_is_exit_130_and_the_clone_is_unlinked(self):
        self.fake.snapshot_after_polls = 5

        def interrupted(_seconds):
            raise KeyboardInterrupt

        code, out, err = self.run_intake(*self.xlsx_args(*self.crop_args()), sleep=interrupted)
        self.assertEqual((code, out), (130, ""))
        self.assertIn("interrupted", err)
        self.assertEqual(self.fake.unlinked, [[900]])
        self.assertFalse(os.path.exists(self.out_path))

    def test_keep_does_not_unlink_and_reports_the_id_on_stderr(self):
        code, out, err = self.run_intake(*self.xlsx_args("--keep", *self.crop_args()))
        self.assertEqual(code, 0, err)
        self.assertEqual((self.fake.unlinked, self.unlink_calls()), ([], []))
        self.assertIn("kept the clone: spreadsheet document 900", err)
        self.assertIn("--document-id 900", err)
        self.assertNotIn("deleted", err)
        self.assertIn(900, self.fake.documents)

    def test_keep_also_holds_on_a_timeout(self):
        self.fake.snapshot_after_polls = 999
        result = self.run_intake("--xlsx-document", "7", "--keep", "--wait-seconds", "3", "--poll-interval", "2")
        self.assertFailed(result, 2, "no snapshot")
        self.assertIn("kept the clone: spreadsheet document 900", result[2])
        self.assertEqual(self.fake.unlinked, [])

    def test_unlink_answering_false_is_a_warning_naming_the_clone_and_exit_0(self):
        self.fake.unlink_mode = "false"
        code, out, err = self.run_intake(*self.xlsx_args(*self.crop_args()))
        self.assertEqual(code, 0, err)
        warning = [line for line in err.splitlines() if line.startswith("WARNING:")]
        self.assertEqual(len(warning), 1)
        self.assertIn("900", warning[0])
        self.assertIn("the server refused", warning[0])
        self.assertNotIn("deleted the clone", err)
        self.assertEqual(self.read_out(), self.expected_baseline_bytes())
        self.assertEqual(json.loads(out)["route"], "snapshot")

    def test_unlink_raising_an_odoo_error_is_a_warning_naming_the_clone_and_exit_0(self):
        self.fake.unlink_mode = "error"
        code, _, err = self.run_intake(*self.xlsx_args(*self.crop_args()))
        self.assertEqual(code, 0, err)
        warning = [line for line in err.splitlines() if line.startswith("WARNING:")]
        self.assertEqual(len(warning), 1)
        self.assertIn("900", warning[0])
        self.assertIn("you cannot delete this document", warning[0])
        self.assertEqual(self.read_out(), self.expected_baseline_bytes())

    def test_unlink_error_that_echoes_the_key_is_redacted(self):
        self.fake.unlink_mode = "error_echo"
        code, _, err = self.run_intake(*self.xlsx_args())
        self.assertEqual(code, 0, err)
        self.assertNotIn(FAKE_KEY, err)
        self.assertIn("WARNING:", err)

    def test_a_failed_unlink_does_not_hide_a_crop_failure(self):
        self.fake.unlink_mode = "false"
        self.fake.clone_snapshot = b"[]"
        self.assertFailed(self.run_intake(*self.xlsx_args()), 2, "not spreadsheet JSON")

    def test_a_bad_area_fails_before_any_request_is_sent(self):
        for area in ("B1:E6", "A1", "nonsense", "A1:A0"):
            with self.subTest(area=area):
                self.assertFailed(self.run_intake(*self.xlsx_args("--sheet", fx.SHEET, "--area", area,
                                                                  "--out", self.out_path)), 2, "")
        self.assertEqual(self.fake.requests, [])

    def test_bad_arguments_fail_before_any_request_is_sent(self):
        out = ["--out", self.out_path]
        bad = [
            (["--sheet", fx.SHEET], "go together"),
            (["--area", fx.AREA, *out], "go together"),
            (["--sheet", fx.SHEET, "--area", fx.AREA], "--out is required"),
        ]
        for extra, fragment in bad:
            with self.subTest(extra=extra):
                self.assertFailed(self.run_intake("--xlsx-document", "7", *extra), 2, fragment)
        for number in ("0", "-5"):
            with self.subTest(number=number):
                self.assertFailed(self.run_intake("--xlsx-document", number), 2, "positive number")
        for flag, value in (("--wait-seconds", "0"), ("--wait-seconds", "-1"), ("--poll-interval", "0"),
                            ("--poll-interval", "-1")):
            with self.subTest(flag=flag, value=value):
                self.assertFailed(self.run_intake("--xlsx-document", "7", flag, value), 2, "must be positive")
        self.assertEqual(self.fake.requests, [])

    def test_a_source_that_is_already_a_spreadsheet_is_refused_and_not_cloned(self):
        self.fake.add_document(8, name="sheet", handler="spreadsheet", mimetype=XLSX_MIME)
        self.fake.add_document(9, name="odd", handler="spreadsheet", mimetype="application/o-spreadsheet")
        for doc_id in (8, 9):
            with self.subTest(doc_id=doc_id):
                self.assertFailed(self.run_intake("--xlsx-document", str(doc_id)), 2, "not an uploaded xlsx")
        self.assertNoCloneOrUnlink()

    def test_a_source_with_another_mimetype_is_refused_and_not_cloned(self):
        for mimetype in ("application/pdf", False, "text/csv"):
            self.fake.add_document(10, name="x", handler=False, mimetype=mimetype)
            with self.subTest(mimetype=mimetype):
                self.assertFailed(self.run_intake("--xlsx-document", "10"), 2, "not an uploaded xlsx")
        self.assertNoCloneOrUnlink()

    def test_a_source_that_does_not_exist_is_refused_and_not_cloned(self):
        self.assertFailed(self.run_intake("--xlsx-document", "123456"), 2, "was not found")
        self.assertNoCloneOrUnlink()

    def test_with_keep_an_interrupted_wait_keeps_the_clone_too(self):
        self.fake.snapshot_after_polls = 5

        def interrupted(_seconds):
            raise KeyboardInterrupt

        code, _, err = self.run_intake(*self.xlsx_args("--keep"), sleep=interrupted)
        self.assertEqual(code, 130)
        self.assertEqual(self.fake.unlinked, [])
        self.assertIn("kept the clone", err)


# ---------------------------------------------------------------- 5. --document-id and bad data

class DocumentIdProblemTests(IntakeCase):
    def test_a_document_without_a_snapshot_gives_the_file_snapshot_hint(self):
        self.fake.add_document(5, snapshot=None)
        result = self.run_intake("--document-id", "5", *self.crop_args())
        self.assertFailed(result, 2, "has no snapshot yet")
        self.assertIn("File > Snapshot", result[2])
        self.assertIn("?debug=1", result[2])
        self.assertNoCloneOrUnlink()

    def test_a_document_that_is_not_a_spreadsheet_is_refused(self):
        for handler in (False, "other"):
            self.fake.add_document(5, handler=handler, mimetype=XLSX_MIME, snapshot=b"{}")
            with self.subTest(handler=handler):
                self.assertFailed(self.run_intake("--document-id", "5"), 2, "not a spreadsheet")

    def test_a_missing_document_is_refused(self):
        self.assertFailed(self.run_intake("--document-id", "55"), 2, "was not found")

    def test_snapshots_that_are_not_a_spreadsheet_give_a_clean_one_line_message(self):
        bad = {
            "not base64": ("not base64 !!!", "not base64"),
            "base64 with a stray character": ("aGVsbG8!gd29ybGQ=", "not base64"),
            "not json": (b"hello", "not spreadsheet JSON"),
            "json list": (b"[]", "not spreadsheet JSON"),
            "no sheets": (b'{"version": 1}', "no sheets"),
            "sheet without cells": (b'{"sheets": [{"name": "A"}]}', "no name or no cells"),
            "deeply nested": (b"[" * 100000, "not spreadsheet JSON"),
        }
        for label, (snapshot, fragment) in bad.items():
            self.fake.add_document(5, snapshot=snapshot)
            with self.subTest(label):
                self.assertFailed(self.run_intake("--document-id", "5"), 2, fragment)

    def test_deeply_nested_json_from_a_file_is_a_clean_error(self):
        path = self.write_input(raw=b"[" * 100000)
        self.assertFailed(self.run_intake("--file", path), 2, "not spreadsheet JSON")

    def test_a_file_that_is_not_json_or_has_the_wrong_shape_is_a_clean_error(self):
        for raw in (b"hello", b"[]", b'{"sheets": []}', b"\xff\xfe\x00", b""):
            with self.subTest(raw=raw):
                self.assertFailed(self.run_intake("--file", self.write_input(raw=raw)), 2, "spreadsheet JSON")

    def test_a_file_over_the_size_limit_is_refused_and_one_exactly_at_the_limit_is_read(self):
        path = self.write_input()
        size = os.path.getsize(path)
        with mock.patch.object(odoo_rpc, "MAX_RESPONSE", size):
            self.assertEqual(self.run_intake("--file", path)[0], 0)
        with mock.patch.object(odoo_rpc, "MAX_RESPONSE", size - 1):
            self.assertFailed(self.run_intake("--file", path), 2, "larger than")

    def test_a_snapshot_over_the_rpc_size_limit_is_a_clean_error(self):
        self.stage_snapshot(5)
        limit = len(fx.as_base64(fx.make_snapshot())) // 2      # bigger than a login answer, smaller than the snapshot
        self.assertGreater(limit, 500)
        with mock.patch.object(odoo_rpc, "MAX_RESPONSE", limit):
            code, out, err = self.run_intake("--document-id", "5")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(len([line for line in err.splitlines() if line.startswith("error: ")]), 1)
        self.assertIn("too large", err)


# ---------------------------------------------------------------- 6. output file handling

class OutputFileTests(IntakeCase):
    def setUp(self):
        super().setUp()
        self.path = self.write_input()

    def crop(self, *more, source=None):
        return self.run_intake("--file", source or self.path, *self.crop_args(*more))

    def test_an_existing_out_is_refused_without_force_and_left_untouched(self):
        with open(self.out_path, "wb") as handle:
            handle.write(b"old content")
        self.assertFailed(self.crop(), 2, "--force")
        self.assertEqual(self.read_out(), b"old content")
        self.assertEqual(self.leftovers(), [])

    def test_force_replaces_the_existing_out(self):
        with open(self.out_path, "wb") as handle:
            handle.write(b"old content")
        code, _, err = self.crop("--force")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read_out(), self.expected_baseline_bytes())

    def test_a_missing_out_folder_is_refused(self):
        argv = ["--file", self.path, "--sheet", fx.SHEET, "--area", fx.AREA, "--out",
                os.path.join(self.tmp, "no-such-folder", "baseline.json")]
        self.assertFailed(self.run_intake(*argv), 2, "folder of --out does not exist")

    def test_out_equal_to_the_input_file_is_refused_with_or_without_force(self):
        before = self.read_input()
        for extra in ((), ("--force",)):
            with self.subTest(extra=extra):
                result = self.run_intake("--file", self.path, "--sheet", fx.SHEET, "--area", fx.AREA, "--out",
                                         self.path, *extra)
                self.assertFailed(result, 2, "input file")
                self.assertEqual(self.read_input(), before)
        self.assertEqual(self.leftovers(), [])

    def read_input(self):
        with open(self.path, "rb") as handle:
            return handle.read()

    def test_out_that_is_the_same_path_spelled_differently_is_refused(self):
        os.mkdir(os.path.join(self.tmp, "sub"))
        before = self.read_input()
        for spelled in (os.path.join(self.tmp, "sub", "..", "input.json"), os.path.join(self.tmp, ".", "input.json")):
            with self.subTest(spelled=spelled):
                result = self.run_intake("--file", self.path, "--sheet", fx.SHEET, "--area", fx.AREA, "--out",
                                         spelled, "--force")
                self.assertFailed(result, 2, "input file")
                self.assertEqual(self.read_input(), before)

    @POSIX_ONLY
    def test_out_that_is_a_symlink_or_hard_link_to_the_input_is_refused(self):
        before = self.read_input()
        link, hard = os.path.join(self.tmp, "link.json"), os.path.join(self.tmp, "hard.json")
        os.symlink(self.path, link)
        os.link(self.path, hard)
        for target in (link, hard):
            for extra in ((), ("--force",)):
                with self.subTest(target=target, extra=extra):
                    result = self.run_intake("--file", self.path, "--sheet", fx.SHEET, "--area", fx.AREA, "--out",
                                             target, *extra)
                    self.assertFailed(result, 2, "input file")
                    self.assertEqual(self.read_input(), before)
        self.assertTrue(os.path.islink(link))

    def test_no_temporary_file_is_left_after_success_or_failure(self):
        self.assertEqual(self.crop()[0], 0)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.crop()[0], 2)
        self.assertEqual(self.leftovers(), [])
        bad = self.write_input(fx.with_formula("=Z99"), name="bad.json")
        self.assertEqual(self.crop("--force", source=bad)[0], 2)
        self.assertEqual(self.leftovers(), [])

    def test_a_failed_write_cleans_the_temporary_file_and_keeps_the_old_content(self):
        with open(self.out_path, "wb") as handle:
            handle.write(b"old content")
        seen = []

        def failing_replace(source, target):
            seen.append((os.path.basename(source), os.path.dirname(source), target))
            raise PermissionError(13, "Permission denied")

        with mock.patch.object(os, "replace", failing_replace):
            result = self.crop("--force")
        self.assertFailed(result, 2, "cannot write")
        self.assertEqual(self.read_out(), b"old content")
        self.assertEqual(self.leftovers(), [])
        name, directory, target = seen[0]
        self.assertTrue(name.startswith(".intake-") and name.endswith(".tmp"), name)
        self.assertEqual((directory, target), (self.tmp, self.out_path))

    def test_out_that_is_a_folder_is_refused_cleanly_even_with_force(self):
        os.mkdir(self.out_path)
        self.assertFailed(self.crop(), 2, "--force")
        self.assertFailed(self.crop("--force"), 2, "cannot write")
        self.assertEqual(self.leftovers(), [])

    def test_a_lone_surrogate_in_the_sheet_is_a_clean_message_and_writes_nothing(self):
        text = json.dumps(fx.make_snapshot(sheets=[fx.make_sheet(cells={"A1": {"content": "\ud800"}}, merges=[])]))
        self.assertIn("\\ud800", text)
        source = self.write_input(raw=text.encode("ascii"), name="surrogate.json")
        result = self.crop(source=source)
        self.assertFailed(result, 2, "cannot be written as UTF-8")
        self.assertNotIn("unexpected", result[2])
        self.assertFalse(os.path.exists(self.out_path))
        self.assertEqual(self.leftovers(), [])

    def test_a_lone_surrogate_outside_the_area_is_dropped_and_does_not_fail(self):
        cells = {"A1": {"content": "ok"}, "Z90": {"content": "\ud800"}}
        text = json.dumps(fx.make_snapshot(sheets=[fx.make_sheet(cells=cells, merges=[])]))
        source = self.write_input(raw=text.encode("ascii"), name="surrogate.json")
        code, _, err = self.crop(source=source)
        self.assertEqual(code, 0, err)

    def test_sheet_without_area_and_area_without_sheet_or_out_are_refused(self):
        self.assertFailed(self.run_intake("--file", self.path, "--sheet", fx.SHEET), 2, "go together")
        self.assertFailed(self.run_intake("--file", self.path, "--area", fx.AREA, "--out", self.out_path), 2,
                          "go together")
        self.assertFailed(self.run_intake("--file", self.path, "--sheet", fx.SHEET, "--area", fx.AREA), 2,
                          "--out is required")
        self.assertFalse(os.path.exists(self.out_path))

    def test_ids_and_waits_that_are_not_positive_are_refused(self):
        self.stage_snapshot(5)
        for argv in (["--document-id", "0"], ["--document-id", "-3"], ["--xlsx-document", "0"],
                     ["--file", self.path, "--wait-seconds", "0"], ["--file", self.path, "--poll-interval", "0"],
                     ["--file", self.path, "--poll-interval", "-2"], ["--file", self.path, "--wait-seconds", "-1"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_intake(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(len([line for line in err.splitlines() if line.startswith("error: ")]), 1)
        self.assertEqual(self.fake.requests, [])

    def test_argument_parser_refuses_missing_or_conflicting_sources(self):
        for argv in ([], ["--document-id", "1", "--file", self.path], ["--document-id", "x"],
                     ["--xlsx-document", "1", "--document-id", "2"], ["--file", self.path, "--wait-seconds", "1.5"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    intake_xlsx.main(argv, io.StringIO(), io.StringIO())
                self.assertEqual(caught.exception.code, 2)


# ---------------------------------------------------------------- 7. secrets and the instance guard

class SecretsAndGuardTests(IntakeCase):
    def test_a_login_error_that_echoes_the_key_is_redacted(self):
        for mode in ("error_echo", "error_echo_top", "http_500"):
            self.fake.mode = mode
            with self.subTest(mode=mode):
                code, out, err = self.run_intake("--document-id", "5")
                self.assertEqual((code, out), (1, ""))
                self.assertIn("error: ", err)

    def test_a_server_error_while_reading_the_snapshot_is_redacted(self):
        self.stage_snapshot(5)
        self.fake.mode = "http_500"
        code, _, err = self.run_intake("--document-id", "5")
        self.assertEqual(code, 1)
        self.assertNotIn(FAKE_KEY, err)

    def test_a_wrong_key_in_the_config_is_an_error_without_the_key(self):
        os.environ["ODOO_KPI_API_KEY"] = "WRONG-" + FAKE_KEY
        self.stage_snapshot(5)
        code, out, err = self.run_intake("--document-id", "5")
        self.assertEqual((code, out), (1, ""))
        self.assertNotIn("WRONG-" + FAKE_KEY, err)

    def test_the_success_streams_and_the_baseline_hold_no_key(self):
        self.stage_snapshot(5)
        code, out, err = self.run_intake("--document-id", "5", *self.crop_args())
        self.assertEqual(code, 0, err)
        self.assertNotIn(FAKE_KEY, out + err + self.read_out().decode("utf-8"))

    def test_an_unexpected_exception_prints_its_type_only_never_its_text(self):
        def boom(config, confirm_instance=None):
            raise RuntimeError("secret detail " + FAKE_KEY)

        code, out, err = self.run_intake("--document-id", "5", client_factory=boom)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.strip(), "error: unexpected RuntimeError")

    def test_every_request_carries_the_odoo_rpc_user_agent(self):
        self.fake.add_document(7, handler=False, mimetype=XLSX_MIME)
        self.fake.clone_snapshot = fx.as_bytes(fx.make_snapshot())
        self.run_intake(*self.xlsx_args())
        self.assertGreaterEqual(len(self.fake.requests), 4)
        for request in self.fake.requests:
            self.assertEqual(request["headers"]["user-agent"], odoo_rpc.USER_AGENT)
        self.assertEqual(odoo_rpc.USER_AGENT, "odoo-kpi-skills/" + odoo_rpc.VERSION)

    def test_a_non_local_host_without_confirm_is_refused_before_any_clone_request(self):
        self.use_url("https://uat-box.example.net")
        opener = ExplodingOpener()

        def factory(config, confirm_instance=None):
            return odoo_rpc.Client(config, confirm_instance=confirm_instance, opener=opener)

        # round 3: the first label alone ("uat-box") no longer counts as the confirmation, only the whole host does
        for extra in ((), ("--confirm-instance", "other-box"), ("--confirm-instance", "uat-box")):
            with self.subTest(extra=extra):
                self.assertFailed(self.run_intake(*self.xlsx_args(*extra), client_factory=factory), 2,
                                  "not a local dev host")
        self.assertEqual(opener.calls, 0)

    def test_the_default_client_refuses_a_non_local_host_before_touching_the_network(self):
        self.use_url("https://uat-box.example.net")
        self.assertFailed(self.run_intake("--document-id", "5"), 2, "--confirm-instance")
        self.assertFailed(self.run_intake("--document-id", "5", "--confirm-instance", "uat-box"), 2,
                          "type the full host name")
        self.use_url("http://uat-box.example.net")
        self.assertFailed(self.run_intake("--document-id", "5", "--confirm-instance", "uat-box.example.net"), 2, "https")

    def test_confirm_instance_is_forwarded_to_the_client_factory(self):
        seen = []
        snapshot_b64 = fx.as_base64(fx.make_snapshot())

        class StubClient:
            config = odoo_rpc.Config("https://uat-box.example.net", "db", "login", FAKE_KEY)

            def login(self):
                return 1

            def execute(self, model, method, args=None, kwargs=None):
                return [{"id": 5, "name": "book", "handler": "spreadsheet", "spreadsheet_snapshot": snapshot_b64}]

        def factory(config, confirm_instance=None):
            seen.append((config.url, confirm_instance))
            return StubClient()

        self.use_url("https://uat-box.example.net")
        code, out, err = self.run_intake("--document-id", "5", "--confirm-instance", "uat-box.example.net",
                                         client_factory=factory)
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, [("https://uat-box.example.net", "uat-box.example.net")])
        self.assertEqual(json.loads(out)["route"], "snapshot")

    def test_a_missing_config_is_a_clean_exit_2(self):
        for name in ("ODOO_KPI_URL", "ODOO_KPI_DB", "ODOO_KPI_LOGIN", "ODOO_KPI_API_KEY"):
            os.environ.pop(name)
        self.assertFailed(self.run_intake("--document-id", "5"), 2, "no config")
        self.assertFailed(self.run_intake(*self.xlsx_args()), 2, "no config")
        self.assertEqual(self.fake.requests, [])


# ---------------------------------------------------------------- 9. clone call failures, polling retries, logs

class StubClient:
    """A client whose `execute` serves an xlsx document read and raises `on_clone` at the clone request."""

    config = odoo_rpc.Config("http://127.0.0.1:1", "db", "login", FAKE_KEY)

    def __init__(self, on_clone):
        self.on_clone = on_clone
        self.methods = []

    def login(self):
        return 1

    def execute(self, model, method, args=None, kwargs=None):
        self.methods.append(method)
        if method == "read":
            return [{"id": 7, "name": "book.xlsx", "handler": False, "mimetype": XLSX_MIME}]
        raise self.on_clone


class CloneRequestFailureTests(IntakeCase):
    def setUp(self):
        super().setUp()
        self.stage_xlsx(7)

    def test_a_server_error_on_the_clone_request_tells_the_user_where_a_clone_may_remain(self):
        self.fake.clone_mode = "error"
        code, out, err = self.run_intake(*self.xlsx_args())
        self.assertEqual((code, out), (1, ""))
        self.assertIn("note: if the server did create a clone it stays in Documents", err)
        self.assertEqual(self.fake.unlinked, [])

    def test_an_interrupt_during_the_clone_request_is_exit_130_with_the_note_and_no_unlink(self):
        stub = StubClient(KeyboardInterrupt())
        code, out, err = self.run_intake(*self.xlsx_args(), client_factory=lambda config, confirm_instance=None: stub)
        self.assertEqual((code, out), (130, ""))
        self.assertIn("interrupted", err)
        self.assertIn("note: if the server did create a clone it stays in Documents", err)
        self.assertNotIn("unlink", stub.methods)

    def test_no_note_on_success_and_none_when_no_clone_was_attempted(self):
        self.assertNotIn("note:", self.run_intake(*self.xlsx_args())[2])
        self.fake.add_document(8, handler="spreadsheet")
        for xlsx_id in (8, 999):
            self.assertNotIn("note:", self.run_intake("--xlsx-document", str(xlsx_id))[2])

    def test_the_deleting_line_comes_before_the_deleted_line_and_is_absent_with_keep(self):
        _, _, err = self.run_intake(*self.xlsx_args())
        lines = err.splitlines()
        self.assertLess(lines.index("deleting the clone (spreadsheet document 900) ..."),
                        lines.index("deleted the clone (spreadsheet document 900)"))
        _, _, err = self.run_intake(*self.xlsx_args("--keep"))
        self.assertNotIn("deleting the clone", err)

    def test_the_deleting_line_is_also_logged_when_the_unlink_fails(self):
        self.fake.unlink_mode = "false"
        _, _, err = self.run_intake(*self.xlsx_args())
        self.assertIn("deleting the clone (spreadsheet document 900) ...", err.splitlines())


class PollRetryTests(IntakeCase):
    def setUp(self):
        super().setUp()
        self.stage_xlsx(7)
        self.argv = ["--xlsx-document", "7", "--wait-seconds", "60", "--poll-interval", "2"]

    def reads_of_clone(self):
        """The polls only: the freshness read of the clone is counted apart (`fresh_reads_of`)."""
        return self.polls_of(900)

    def test_two_consecutive_poll_errors_are_retried(self):
        self.fake.read_error_on_polls = {0, 1}
        code, _, err = self.run_intake(*self.argv, *self.crop_args())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.time.sleeps, [2.0, 2.0])
        self.assertEqual(len(self.reads_of_clone()), 3)
        self.assertEqual(len(self.fresh_reads_of(900)), 1)
        self.assertEqual(self.read_out(), self.expected_baseline_bytes())
        self.assertEqual(self.fake.unlinked, [[900]])

    def test_a_third_consecutive_poll_error_is_exit_1_and_the_clone_is_still_unlinked(self):
        self.fake.read_error_on_polls = {0, 1, 2, 3, 4}
        code, out, err = self.run_intake(*self.argv)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(len([line for line in err.splitlines() if line.startswith("error: ")]), 1)
        self.assertEqual(len(self.reads_of_clone()), 3)
        self.assertEqual(self.time.sleeps, [2.0, 2.0])
        self.assertEqual(self.fake.unlinked, [[900]])

    def test_a_successful_poll_resets_the_failure_counter(self):
        self.fake.read_error_on_polls = {0, 1, 3, 4}
        self.fake.snapshot_after_polls = 5
        code, _, err = self.run_intake(*self.argv)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.time.sleeps, [2.0] * 5)
        self.assertEqual(self.fake.unlinked, [[900]])

    def test_a_retried_poll_still_respects_the_deadline(self):
        self.fake.read_error_on_polls = {0, 1}
        self.fake.snapshot_after_polls = 99
        result = self.run_intake("--xlsx-document", "7", "--wait-seconds", "3", "--poll-interval", "2")
        self.assertFailed(result, 2, "no snapshot after 3 seconds")
        self.assertEqual(self.fake.unlinked, [[900]])

    def test_a_document_that_is_not_a_spreadsheet_is_not_retried(self):
        self.fake.clone_poll_handler = False          # fresh at the first read, no longer a spreadsheet at the polls
        self.assertFailed(self.run_intake(*self.argv), 2, "not a spreadsheet")
        self.assertEqual(len(self.reads_of_clone()), 1)
        self.assertEqual(self.time.sleeps, [])
        self.assertEqual(self.fake.unlinked, [[900]])

    def test_the_document_id_route_does_not_retry(self):
        self.fake.add_document(5, snapshot=fx.as_bytes(fx.make_snapshot()))
        self.fake.read_error_ids = {5}
        code, out, err = self.run_intake("--document-id", "5")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(len(self.fake.execute_calls("documents.document", "read")), 1)
        self.assertEqual(self.time.sleeps, [])


# ---------------------------------------------------------------- 10. argument limits

class ArgumentLimitTests(IntakeCase):
    def test_poll_interval_must_be_finite_and_at_least_one_second(self):
        self.fake.add_document(7, handler=False, mimetype=XLSX_MIME)

        def no_wait(_seconds):                       # a poll loop that starts here would never end with nan / inf
            raise AssertionError("the poll loop must not start with a bad --poll-interval")

        for value in ("nan", "inf", "-inf", "0.5", "0.99", "0", "-1", "-0.0"):
            with self.subTest(value=value):
                self.assertFailed(self.run_intake("--xlsx-document", "7", "--poll-interval=" + value, sleep=no_wait),
                                  2, "--poll-interval must be positive")
        self.assertEqual(self.fake.requests, [])

    def test_poll_interval_of_one_or_more_is_accepted(self):
        path = self.write_input()
        for value in ("1", "1.0", "2.5", "3600"):
            with self.subTest(value=value):
                self.assertEqual(self.run_intake("--file", path, "--poll-interval", value)[0], 0)

    def test_wait_seconds_must_be_between_1_and_3600(self):
        self.fake.add_document(7, handler=False, mimetype=XLSX_MIME)
        # no larger value than 3601 on purpose: a mutant that drops the upper bound would then poll 3601 times and fail
        # the assertion; with 999999 it polled a million times and was only ever stopped by the wall-clock timeout
        for value in ("0", "-1", "3601"):
            with self.subTest(value=value):
                self.assertFailed(self.run_intake("--xlsx-document", "7", "--wait-seconds", value), 2,
                                  "--wait-seconds must be positive")
        self.assertEqual(self.fake.requests, [])
        path = self.write_input()
        for value in ("1", "3600"):
            with self.subTest(value=value):
                self.assertEqual(self.run_intake("--file", path, "--wait-seconds", value)[0], 0)

    def test_the_limits_are_checked_before_any_request_on_every_server_route(self):
        self.stage_snapshot(5)
        for argv in (["--document-id", "5", "--poll-interval", "nan"], ["--document-id", "5", "--wait-seconds", "3601"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_intake(*argv)[0], 2)
        self.assertEqual(self.fake.requests, [])


# ---------------------------------------------------------------- 11. output hardening

class OutputHardeningTests(IntakeCase):
    def setUp(self):
        super().setUp()
        self.path = self.write_input()
        with open(self.out_path, "wb") as handle:
            handle.write(b"old content")

    def crop(self, *more, source=None):
        return self.run_intake("--file", source or self.path, *self.crop_args("--force", *more))

    def test_an_interrupt_inside_the_replace_removes_the_temp_file_and_keeps_the_old_content(self):
        with mock.patch.object(os, "replace", side_effect=KeyboardInterrupt):
            code, out, err = self.crop()
        self.assertEqual((code, out), (130, ""))
        self.assertIn("interrupted", err)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.read_out(), b"old content")

    def test_an_unexpected_error_inside_the_replace_is_reraised_and_cleans_the_temp_file(self):
        with mock.patch.object(os, "replace", side_effect=RuntimeError("boom " + FAKE_KEY)):
            code, out, err = self.crop()
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err.strip(), "error: unexpected RuntimeError")
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.read_out(), b"old content")

    def test_when_the_temp_file_cannot_be_removed_the_error_names_it(self):
        with mock.patch.object(os, "replace", side_effect=PermissionError(13, "Permission denied")), \
                mock.patch.object(os, "unlink", side_effect=OSError(13, "Permission denied")):
            result = self.crop()
        self.assertFailed(result, 2, "could not be removed")
        left = self.leftovers()
        self.assertEqual(len(left), 1)
        self.assertIn(os.path.join(self.tmp, left[0]), result[2])
        os.remove(os.path.join(self.tmp, left[0]))

    def test_a_failed_replace_that_can_be_cleaned_does_not_mention_a_leftover(self):
        with mock.patch.object(os, "replace", side_effect=PermissionError(13, "Permission denied")):
            result = self.crop()
        self.assertFailed(result, 2, "cannot write")
        self.assertNotIn("could not be removed", result[2])

    def test_a_source_that_never_ends_is_read_up_to_the_limit_only(self):
        # bounded by construction (no /dev/zero: an unbounded read of it would eat the RAM of the machine): the stand-in
        # file refuses a read without a size or with a size above MAX_RESPONSE + 1 and holds only 3 * MAX bytes
        limit = 1000
        asked = []

        class EndlessFile:
            remaining = 3 * limit

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def read(self, size=-1):
                asked.append(size)
                if size is None or size < 0 or size > limit + 1:
                    raise AssertionError("unbounded read: size=%r" % (size,))
                size = min(size, self.remaining)
                self.remaining -= size
                return b"\0" * size

        opened = []

        def fake_open(path, mode="r", *args, **kwargs):
            opened.append((path, mode))
            return EndlessFile()

        with mock.patch.object(odoo_rpc, "MAX_RESPONSE", limit), \
                mock.patch.object(intake_xlsx, "open", fake_open, create=True):
            self.assertFailed(self.run_intake("--file", "endless.bin"), 2, "larger than")
        self.assertEqual(opened, [("endless.bin", "rb")])
        self.assertEqual(asked, [limit + 1])

    def test_a_source_of_exactly_the_limit_is_read_whole_by_one_bounded_read(self):
        limit = 1000
        data = b"x" * limit
        asked = []

        class OneFile:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def read(self, size=-1):
                asked.append(size)
                return data[:size] if size >= 0 else data

        with mock.patch.object(odoo_rpc, "MAX_RESPONSE", limit), \
                mock.patch.object(intake_xlsx, "open", lambda *a, **k: OneFile(), create=True):
            self.assertEqual(intake_xlsx.read_file("x"), data)
        self.assertEqual(asked, [limit + 1])

    def test_nan_and_infinity_constants_are_not_spreadsheet_json(self):
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            raw = b'{"sheets": [{"name": "A", "cells": {}}], "x": ' + constant + b"}"
            with self.subTest(constant=constant):
                self.assertFailed(self.run_intake("--file", self.write_input(raw=raw)), 2, "not spreadsheet JSON")
        raw = b'{"sheets": [{"name": "A", "cells": {"A1": {"content": NaN}}}]}'
        self.assertFailed(self.run_intake("--file", self.write_input(raw=raw)), 2, "not spreadsheet JSON")

    def test_a_number_that_overflows_to_infinity_is_a_clean_error_not_invalid_json(self):
        for number in ("1e999", "-1e999"):
            cells = '{"A1": {"content": %s}}' % number
            raw = ('{"sheets": [{"name": "S", "cells": %s, "colNumber": 5, "rowNumber": 5}]}' % cells).encode()
            with self.subTest(number=number):
                result = self.run_intake("--file", self.write_input(raw=raw, name="big.json"), "--sheet", "S",
                                         "--area", "A1:B2", "--out", self.out_path, "--force")
                self.assertFailed(result, 2, "cannot be written as JSON")
                self.assertEqual(self.read_out(), b"old content")
                self.assertEqual(self.leftovers(), [])

    def test_deeply_nested_content_is_a_clean_exit_2(self):
        for place in ("cell", "settings"):
            for depth in (900, 100000):
                nested = "[" * depth + "]" * depth
                cells = '{"A1": {"content": %s}}' % (nested if place == "cell" else '"x"')
                extra = ', "settings": %s' % nested if place == "settings" else ""
                raw = ('{"sheets": [{"name": "S", "cells": %s, "colNumber": 5, "rowNumber": 5}]%s}'
                       % (cells, extra)).encode()
                with self.subTest(place=place, depth=depth):
                    result = self.run_intake("--file", self.write_input(raw=raw, name="deep.json"), "--sheet", "S",
                                             "--area", "A1:B2", "--out", self.out_path, "--force")
                    self.assertFailed(result, 2, "")
                    self.assertNotIn("unexpected", result[2])
                    self.assertEqual(self.read_out(), b"old content")
                    self.assertEqual(self.leftovers(), [])

    def test_a_quote_inside_a_sheet_name_cannot_hide_a_foreign_reference(self):
        data = fx.with_formula("='a\"b'!A1&\"c\"")
        self.assertFailed(self.run_intake("--file", self.write_input(data, name="q.json"), *self.crop_args("--force")),
                          2, "another sheet")

    def test_a_huge_unknown_key_name_and_many_unknown_keys_give_a_short_error_line(self):
        data = fx.make_snapshot(**{("k%02d" % n): 1 for n in range(50)}, **{"x" * 3000000: 1})
        result = self.run_intake("--file", self.write_input(data, name="keys.json"), *self.crop_args("--force"))
        self.assertFailed(result, 2, "unknown workbook keys")
        self.assertLess(len(result[2]), 1500)


# ---------------------------------------------------------------- 8. Windows-relevant output encoding

class RecordingStream(io.StringIO):
    def __init__(self, fail=None):
        super().__init__()
        self.calls, self.fail = [], fail

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise self.fail


class StdoutEncodingTests(IntakeCase):
    """A script run by an agent has its stdout piped: on Windows that is the ANSI code page (cp1252 here), not UTF-8."""

    NAME = "Sheet 测试"

    def run_with_ansi_streams(self, argv):
        out_raw, err_raw = io.BytesIO(), io.BytesIO()
        out = io.TextIOWrapper(out_raw, encoding="cp1252", errors="strict", newline="")
        err = io.TextIOWrapper(err_raw, encoding="cp1252", errors="strict", newline="")
        code = intake_xlsx.main(argv, out, err)
        out.flush()
        err.flush()
        err_text = err_raw.getvalue().decode("utf-8")
        self.streams.append(("", err_text))
        return code, out_raw.getvalue().decode("utf-8"), err_text

    def named_input(self):
        return self.write_input(fx.make_snapshot(sheets=[fx.make_sheet(name=self.NAME)]))

    def test_the_inventory_is_utf8_whatever_the_console_code_page(self):
        code, out, err = self.run_with_ansi_streams(["--file", self.named_input()])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["sheets"][0]["name"], self.NAME)

    def test_the_crop_report_is_utf8_whatever_the_console_code_page(self):
        code, out, err = self.run_with_ansi_streams(["--file", self.named_input(), "--sheet", self.NAME,
                                                     "--area", fx.AREA, "--out", self.out_path])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["sheet"], self.NAME)
        self.assertEqual(self.leftovers(), [])

    def test_an_error_message_with_a_character_the_code_page_cannot_show_is_written_to_stderr(self):
        code, out, err = self.run_with_ansi_streams(["--file", self.named_input(), "--sheet", "Nope " + self.NAME,
                                                     "--area", fx.AREA, "--out", self.out_path])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Nope " + self.NAME, err)
        self.assertTrue(err.startswith("error: "))

    def test_both_streams_are_reconfigured_to_utf8_with_backslashreplace(self):
        out, err = RecordingStream(), RecordingStream()
        self.assertEqual(intake_xlsx.main(["--file", self.write_input()], out, err), 0)
        expected = [{"encoding": "utf-8", "errors": "backslashreplace"}]
        self.assertEqual((out.calls, err.calls), (expected, expected))

    def test_a_stream_that_refuses_to_be_reconfigured_does_not_stop_the_run(self):
        for failure in (ValueError("no"), OSError("no")):
            out, err = RecordingStream(fail=failure), RecordingStream(fail=failure)
            with self.subTest(failure=failure):
                self.assertEqual(intake_xlsx.main(["--file", self.write_input()], out, err), 0)
                self.assertEqual(json.loads(out.getvalue())["route"], "download")

    def test_a_stream_without_reconfigure_is_used_as_it_is(self):
        out, err = io.StringIO(), io.StringIO()
        self.assertFalse(hasattr(out, "reconfigure"))
        self.assertEqual(intake_xlsx.main(["--file", self.write_input()], out, err), 0)


# ---------------------------------------------------------------- 12. round 3: fresh clone, formula strings, Ctrl-C

class FreshCloneTests(IntakeCase):
    """The clone call is trusted only up to a point: its answer must be a new spreadsheet document without a snapshot.
    Anything else is somebody else's document: refuse, say so, and above all never unlink it."""

    def setUp(self):
        super().setUp()
        self.stage_xlsx(7)

    def refuse_existing(self, existing_id, expect_code=2, fragment="is not a fresh clone: left untouched"):
        self.fake.clone_mode = "existing"
        self.fake.existing_clone_id = existing_id
        before = copy.deepcopy(self.fake.documents)
        del self.fake.requests[:]                                     # counts below are per run (subTests share a fake)
        result = self.run_intake(*self.xlsx_args(*self.crop_args()))
        self.assertFailed(result, expect_code, fragment)
        err = result[2]
        self.assertIn(NOTE, err)
        self.assertEqual((self.fake.unlinked, self.unlink_calls()), ([], []))
        self.assertEqual(self.fake.documents, before)                 # the other document is still there, unchanged
        self.assertNotIn("deleting the clone", err)
        self.assertNotIn("deleted the clone", err)
        self.assertNotIn("kept the clone", err)
        self.assertNotIn("Open this address", err)                    # nothing was offered to the user
        self.assertFalse(os.path.exists(self.out_path))
        self.assertEqual(self.polls_of(existing_id), [])              # never waited on it
        self.assertEqual(len(self.fresh_reads_of(existing_id)), 1)
        return err

    def test_a_document_that_is_not_a_spreadsheet_is_not_a_clone_and_is_never_unlinked(self):
        for handler in (False, "other", "", None):
            self.fake.add_document(33, name="somebody else's", handler=handler, mimetype=XLSX_MIME)
            with self.subTest(handler=handler):
                err = self.refuse_existing(33)
                self.assertIn("document 33 is not a fresh clone", err)

    def test_a_spreadsheet_that_already_has_a_snapshot_is_not_a_clone_and_is_never_unlinked(self):
        self.fake.add_document(34, name="somebody's work", handler="spreadsheet", snapshot=fx.as_bytes(fx.make_snapshot()))
        err = self.refuse_existing(34)
        self.assertIn("document 34 is not a fresh clone", err)

    def test_the_same_document_with_keep_is_not_reported_as_kept(self):
        self.fake.add_document(34, name="somebody's work", handler="spreadsheet", snapshot=b"{}")
        self.fake.clone_mode, self.fake.existing_clone_id = "existing", 34
        result = self.run_intake(*self.xlsx_args("--keep"))
        self.assertFailed(result, 2, "not a fresh clone")
        self.assertNotIn("kept the clone", result[2])
        self.assertEqual(self.fake.unlinked, [])

    def test_a_server_error_on_the_freshness_read_is_exit_1_nothing_is_unlinked_and_the_note_is_shown(self):
        self.fake.next_document_id = 950
        self.fake.read_error_ids = {950}
        code, out, err = self.run_intake(*self.xlsx_args(*self.crop_args()))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(len([line for line in err.splitlines() if line.startswith("error: ")]), 1)
        self.assertIn(NOTE, err)
        self.assertEqual((self.fake.unlinked, self.unlink_calls()), ([], []))
        self.assertIn(950, self.fake.documents)                       # the clone stays: the user is told to delete it
        self.assertEqual((len(self.fresh_reads_of(950)), self.polls_of(950)), (1, []))
        self.assertNotIn("Open this address", err)
        self.assertFalse(os.path.exists(self.out_path))

    def test_a_server_error_on_the_freshness_read_of_another_document_is_exit_1_too(self):
        self.fake.add_document(35, handler="spreadsheet")
        self.fake.clone_mode, self.fake.existing_clone_id = "existing", 35
        self.fake.read_error_ids = {35}
        code, out, err = self.run_intake(*self.xlsx_args())
        self.assertEqual((code, out), (1, ""))
        self.assertIn(NOTE, err)
        self.assertEqual(self.fake.unlinked, [])
        self.assertIn(35, self.fake.documents)

    def test_a_missing_clone_row_is_refused_and_not_unlinked(self):
        self.fake.clone_mode, self.fake.existing_clone_id = "existing", 4040      # no such document
        self.assertFailed(self.run_intake(*self.xlsx_args()), 2, "document 4040 is not a fresh clone")
        self.assertEqual(self.fake.unlinked, [])

    def test_a_fresh_clone_is_read_once_before_the_url_is_shown_and_the_polls_start(self):
        self.fake.snapshot_after_polls = 1
        code, _, err = self.run_intake(*self.xlsx_args(*self.crop_args()))
        self.assertEqual(code, 0, err)
        methods = [(method, args[1] if method == "read" else None) for method, args in self.document_calls()]
        self.assertEqual(methods, [("read", ["name", "handler", "mimetype"]), ("clone_xlsx_into_spreadsheet", None),
                                   ("read", FRESH_FIELDS), ("read", POLL_FIELDS), ("read", POLL_FIELDS),
                                   ("unlink", None)])
        self.assertNotIn(NOTE, err)

    def test_a_snapshot_that_appears_by_itself_before_the_first_poll_is_still_a_fresh_clone(self):
        self.fake.snapshot_after_polls = 0
        code, _, err = self.run_intake(*self.xlsx_args())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.unlinked, [[900]])

    def test_the_note_is_logged_for_a_refused_clone_and_no_unlink_is_sent_even_when_the_user_interrupts(self):
        self.fake.add_document(33, handler=False)
        self.fake.clone_mode, self.fake.existing_clone_id = "existing", 33

        def interrupted(_seconds):
            raise KeyboardInterrupt

        code, out, err = self.run_intake(*self.xlsx_args(), sleep=interrupted)
        self.assertEqual(code, 2)
        self.assertIn(NOTE, err)
        self.assertEqual(self.fake.unlinked, [])


class FreshStubClient:
    """A client with a scripted answer for the read of the clone; records every method it is asked."""

    config = odoo_rpc.Config("http://127.0.0.1:1", "db", "login", FAKE_KEY)

    def __init__(self, clone_read, clone_answer=900):
        self.clone_read, self.clone_answer, self.methods = clone_read, clone_answer, []

    def login(self):
        return 1

    def execute(self, model, method, args=None, kwargs=None):
        self.methods.append(method)
        if method == "read" and args[0] == [7]:
            return [{"id": 7, "name": "book.xlsx", "handler": False, "mimetype": XLSX_MIME}]
        if method == "clone_xlsx_into_spreadsheet":
            return self.clone_answer
        if method == "read":
            return self.clone_read
        return True


class FreshCloneShapeTests(IntakeCase):
    def run_stub(self, clone_read, **kwargs):
        stub = FreshStubClient(clone_read, **kwargs)
        code, out, err = self.run_intake("--xlsx-document", "7", client_factory=lambda config, confirm_instance=None: stub)
        return stub, code, out, err

    def test_every_answer_that_is_not_a_fresh_spreadsheet_row_is_refused_without_an_unlink(self):
        answers = {
            "no rows": [], "none": None, "a string": "x", "a dict": {"handler": "spreadsheet"},
            "two rows": [{"handler": "spreadsheet"}, {"handler": "spreadsheet"}], "a row that is a string": ["x"],
            "an empty row": [{}], "no handler": [{"spreadsheet_snapshot": False}],
            "another handler": [{"handler": "Spreadsheet", "spreadsheet_snapshot": False}],
            "handler false": [{"handler": False, "spreadsheet_snapshot": False}],
            "snapshot present": [{"handler": "spreadsheet", "spreadsheet_snapshot": "e30="}],
            "snapshot text": [{"handler": "spreadsheet", "spreadsheet_snapshot": "x"}],
        }
        for label, answer in answers.items():
            with self.subTest(label):
                stub, code, out, err = self.run_stub(answer)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("document 900 is not a fresh clone: left untouched", err)
                self.assertIn(NOTE, err)
                self.assertNotIn("unlink", stub.methods)
                self.assertEqual(stub.methods.count("read"), 2)           # the xlsx read and the freshness read

    def test_a_row_of_a_fresh_spreadsheet_is_accepted_by_check_fresh_clone(self):
        for row in ({"handler": "spreadsheet", "spreadsheet_snapshot": False},
                    {"handler": "spreadsheet", "spreadsheet_snapshot": None},
                    {"handler": "spreadsheet", "spreadsheet_snapshot": ""}, {"handler": "spreadsheet"}):
            with self.subTest(row=row):
                stub = FreshStubClient([row])
                self.assertIsNone(intake_xlsx.check_fresh_clone(stub, 900))
                self.assertEqual(stub.methods, ["read"])

    def test_check_fresh_clone_asks_for_the_clone_and_two_fields_only(self):
        asked = []

        class Recording(FreshStubClient):
            def execute(self, model, method, args=None, kwargs=None):
                asked.append((model, method, args))
                return super().execute(model, method, args, kwargs)

        intake_xlsx.check_fresh_clone(Recording([{"handler": "spreadsheet", "spreadsheet_snapshot": False}]), 912)
        self.assertEqual(asked, [("documents.document", "read", [[912], ["handler", "spreadsheet_snapshot"]])])

    def test_the_refusal_names_the_document_with_its_id(self):
        with self.assertRaises(intake_xlsx.IntakeError) as caught:
            intake_xlsx.check_fresh_clone(FreshStubClient([]), 912)
        self.assertEqual(str(caught.exception), "document 912 is not a fresh clone: left untouched")


class CloneIdOfTests(unittest.TestCase):
    def test_only_a_positive_int_that_is_not_the_source_is_a_clone_id(self):
        for good in (1, 900, 2 ** 40):
            self.assertEqual(intake_xlsx.clone_id_of(good, 7), good)
        for bad in (True, False, 0, -1, 7, 900.0, 1.5, "900", None, [900], {"id": 900}, {"res_id": 900}, (900,), b"9"):
            with self.subTest(bad=bad), self.assertRaises(intake_xlsx.IntakeError) as caught:
                intake_xlsx.clone_id_of(bad, 7)
            self.assertIn("did not return the id of a new clone", str(caught.exception))


class InterruptInsideUnlinkTests(IntakeCase):
    """Ctrl-C while the clone is being deleted: the user must still find the clone id in the log, exit is 130."""

    def interrupting_factory(self, method="unlink"):
        class Interrupting(odoo_rpc.Client):
            def execute(self, model, name, args=None, kwargs=None):
                if name == method:
                    raise KeyboardInterrupt
                return super().execute(model, name, args, kwargs)

        return lambda config, confirm_instance=None: Interrupting(config, confirm_instance=confirm_instance)

    def setUp(self):
        super().setUp()
        self.stage_xlsx(7)

    def test_an_interrupt_inside_the_unlink_names_the_clone_in_the_earlier_log_lines(self):
        code, out, err = self.run_intake(*self.xlsx_args(*self.crop_args()), client_factory=self.interrupting_factory())
        self.assertEqual((code, out), (130, ""))
        self.assertIn("interrupted", err)
        lines = err.splitlines()
        self.assertIn(self.fake.url + URL_SUFFIX % 900, lines)
        self.assertIn("deleting the clone (spreadsheet document 900) ...", lines)
        self.assertNotIn("deleted the clone (spreadsheet document 900)", lines)
        self.assertFalse(os.path.exists(self.out_path))
        self.assertEqual(self.leftovers(), [])

    def test_the_clone_is_still_on_the_server_after_the_interrupted_unlink(self):
        self.run_intake(*self.xlsx_args(), client_factory=self.interrupting_factory())
        self.assertIn(900, self.fake.documents)
        self.assertEqual(self.fake.unlinked, [])

    def test_an_interrupt_during_the_freshness_read_is_exit_130_with_the_note_and_no_unlink(self):
        class InterruptingRead(odoo_rpc.Client):
            def execute(self, model, name, args=None, kwargs=None):
                if name == "read" and args[0] == [900]:
                    raise KeyboardInterrupt
                return super().execute(model, name, args, kwargs)

        code, out, err = self.run_intake(*self.xlsx_args(),
                                         client_factory=lambda config, confirm_instance=None: InterruptingRead(
                                             config, confirm_instance=confirm_instance))
        self.assertEqual((code, out), (130, ""))
        self.assertIn(NOTE, err)
        self.assertEqual(self.fake.unlinked, [])


class FirstPollRetryTests(IntakeCase):
    def test_one_transient_error_on_the_very_first_poll_is_retried_and_the_run_succeeds(self):
        self.stage_xlsx(7)
        self.fake.read_error_on_polls = {0}
        code, _, err = self.run_intake(*self.xlsx_args(*self.crop_args()))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.time.sleeps, [2.0])
        self.assertEqual((len(self.fresh_reads_of(900)), len(self.polls_of(900))), (1, 2))
        self.assertEqual(self.read_out(), self.expected_baseline_bytes())
        self.assertEqual(self.fake.unlinked, [[900]])
        self.assertNotIn("WARNING", err)


class FormulaStringsThroughCliTests(IntakeCase):
    """The string rule of the formula guard, end to end (`--file`: no server needed)."""

    def crop_formula(self, formula):
        path = self.write_input(fx.with_formula(formula), name="f.json")
        return self.run_intake("--file", path, *self.crop_args("--force"))

    def test_the_backslash_quote_family_is_refused_with_exit_2_and_no_file(self):
        cases = ((HIDDEN_READ, "has an unbalanced quote"), (HIDDEN_RANGE, "has an unbalanced quote"),
                 (HIDDEN_SHEET, "reads another sheet"), ('=A1&"\\"', "has an unbalanced quote"))
        for formula, fragment in cases:
            with self.subTest(formula=formula):
                self.assertFailed(self.crop_formula(formula), 2, fragment)
                self.assertFalse(os.path.exists(self.out_path))
                self.assertEqual(self.leftovers(), [])

    def test_the_quote_bang_and_colon_forms_that_stay_inside_the_area_are_written(self):
        for formula in ('="it\'s"', '="a b"', '="x:y"', '=A1&"!"', '=IF(A1="x!y",1,2)', '="a""b"'):
            with self.subTest(formula=formula):
                code, _, err = self.crop_formula(formula)
                self.assertEqual(code, 0, err)
                cells = json.loads(self.read_out())["sheets"][0]["cells"]
                self.assertEqual(cells["D4"]["content"], formula)

    def test_a_reference_of_more_than_nine_digits_is_refused_as_reading_outside(self):
        for formula in ("=A12345678", "=SUM(A1:B12345678)", "=A" + "9" * 5000):
            with self.subTest(formula=formula[:20]):
                self.assertFailed(self.crop_formula(formula), 2, "reads outside the area")

    def test_the_cell_key_of_the_sheet_with_thousands_of_digits_is_a_clean_error(self):
        data = fx.with_cell("A" + "9" * 5000, {"content": "x"})
        self.assertFailed(self.run_intake("--file", self.write_input(data, name="k.json"), *self.crop_args("--force")),
                          2, "not a cell reference")


HIDDEN_READ = '="\\""&Z99&"'
HIDDEN_RANGE = '="\\""&A1:C&"'
HIDDEN_SHEET = '="\\""&\'S\'!A1&"'


class Utf8StreamTests(IntakeCase):
    VIETNAMESE = "Doanh thu Việt"

    def run_with(self, encoding, argv, err_stream=None):
        out_raw, err_raw = io.BytesIO(), io.BytesIO()
        out = io.TextIOWrapper(out_raw, encoding=encoding, errors="strict", newline="")
        err = err_stream or io.TextIOWrapper(err_raw, encoding=encoding, errors="strict", newline="")
        code = intake_xlsx.main(argv, out, err)
        out.flush()
        if err_stream is None:
            err.flush()
        err_text = err_raw.getvalue().decode("utf-8")
        self.streams.append(("", err_text))
        return code, out_raw.getvalue().decode("utf-8"), err_text

    def named(self):
        return self.write_input(fx.make_snapshot(sheets=[fx.make_sheet(name=self.VIETNAMESE)]))

    def test_a_korean_code_page_cp949_still_gets_valid_utf8_json_for_a_vietnamese_sheet_name(self):
        with self.assertRaises(UnicodeEncodeError):
            self.VIETNAMESE.encode("cp949")                             # the premise: cp949 cannot show it
        code, out, err = self.run_with("cp949", ["--file", self.named()])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["sheets"][0]["name"], self.VIETNAMESE)

    def test_a_korean_code_page_cp949_gets_the_error_text_as_utf8_on_stderr(self):
        code, out, err = self.run_with("cp949", ["--file", self.named(), "--sheet", "Việt " + self.VIETNAMESE,
                                                 "--area", fx.AREA, "--out", self.out_path])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("Việt " + self.VIETNAMESE, err)

    def test_a_closed_stream_as_err_does_not_break_the_run_and_the_exit_code_is_kept(self):
        err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        err.close()
        out = io.StringIO()
        self.assertEqual(intake_xlsx.main(["--file", self.write_input()], out, err), 0)
        self.assertEqual(json.loads(out.getvalue())["route"], "download")

    def test_a_closed_stream_as_out_does_not_break_the_reconfigure_step(self):
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        out.close()
        err = io.StringIO()
        # nothing can be printed on a closed out: the point is that main still returns a code, not a traceback
        code = intake_xlsx.main(["--file", self.write_input()], out, err)
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue().strip(), "error: unexpected ValueError")


if __name__ == "__main__":
    unittest.main()
