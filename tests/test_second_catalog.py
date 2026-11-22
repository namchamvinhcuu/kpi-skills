"""Two projects side by side: a synthetic second catalog (`acme`) next to the shipped one.

Plan kpi-multi-project step 2b (AC3, R5): bind, scan (with aliases) and reconcile work for BOTH catalogs in the same
plugin, and nothing leaks from one to the other. The temporary `catalogs` folder holds a byte copy of the shipped
`dksk.json` and the synthetic `acme.json` of `second_catalog_fixtures` (invented names, no customer data), selected with
`mock.patch.object(kpi_catalogs, "CATALOG_DIR", <tmp>)` (D11); every command runs `main(argv, out, err)` IN PROCESS.
The offline commands (bind, scan) run with `socket.socket` patched to raise; reconcile talks to the in-process fake Odoo.
The expectations (models, counts, bindings) are written by hand, not computed with the code under test.
"""
import json
import os
import unittest
from unittest import mock

import reconcile_fixtures as rf
import scan_fixtures as sf
import second_catalog_fixtures as sc
from dashboard_fixtures import StreamCase, start_fake, use_qaqc_env
from fake_odoo import FAKE_UID
from helpers import REPO
from test_reconcile_kpi import HEADER, Counts, counts_of, diff_cells, month_counts

import bind_kpi
import intake_xlsx
import kpi_catalogs
import reconcile_kpi
import scan_excel

SHIPPED_DIR = os.path.join(str(REPO), "catalogs")
DKSK_SHARED_MODEL = "dksk.kpi.month_metric"
DKSK_LIST_MODEL, DKSK_PIVOT_MODEL = "dksk.kpi.production_group", "mes.qc_form.history"
ALL_DATA_MODELS = set(sc.ACME_MODELS) | {rf.REPORT, rf.PRODUCTION, rf.QMS, DKSK_SHARED_MODEL}


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


def dksk_bytes():
    return read(os.path.join(SHIPPED_DIR, "dksk.json"))


def models_of(document, key):
    return {definition["model"] for definition in document[key].values()}


def missing_source_text(name):
    return "binding 1: source %r is not in the catalog" % name


class TwoProjects(StreamCase):
    """A fresh folder with the installed catalogs (`INSTALLED`: `dksk` = the real file, `acme` = the synthetic one)."""

    INSTALLED = ("acme", "dksk")

    def setUp(self):
        super().setUp()
        self.catalogs = os.path.join(self.tmp, "catalogs")
        os.makedirs(self.catalogs)
        for name in self.INSTALLED:
            self.put(os.path.join(self.catalogs, name + ".json"), {"acme": sc.acme_bytes, "dksk": dksk_bytes}[name]())
        patcher = mock.patch.object(kpi_catalogs, "CATALOG_DIR", self.catalogs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)

    def path(self, name):
        return os.path.join(self.work, name)

    @staticmethod
    def put(path, data):
        with open(path, "wb") as handle:
            handle.write(data)

    def files(self):
        return sorted(os.listdir(self.work))

    def cli(self, main, argv, network=False, **kwargs):
        """(rc, stdout, stderr) of `main(argv)`; the offline commands cannot open a socket."""
        if network:
            return self.collect(main, argv, **kwargs)
        with mock.patch("socket.socket", side_effect=AssertionError("the network was reached")):
            return self.collect(main, argv, **kwargs)


# ---------------------------------------------------------------- bind

class BindTests(TwoProjects):
    def setUp(self):
        super().setUp()
        typed = rf.typed_row(5, ["1", "2"])
        self.put(self.path("acme_baseline.json"), intake_xlsx.dump_bytes(rf.baseline_of(
            dict(typed, **rf.typed_row(6, ["3", "4"]), **rf.typed_row(8, ["0"] * 12)))))
        self.put(self.path("acme_spec.json"), json.dumps(rf.spec_of(
            [sc.line_binding(5, count=2), sc.group_binding(6, count=2), sc.qc_binding(8)])).encode("utf-8"))
        self.put(self.path("dksk_baseline.json"), intake_xlsx.dump_bytes(rf.baseline_of(
            dict(typed, **rf.typed_row(8, ["0"] * 12)))))
        self.put(self.path("dksk_spec.json"), json.dumps(rf.spec_of(
            [rf.list_binding(5, source="production_group", count=2), rf.qms_binding(8)])).encode("utf-8"))
        self.before = self.files()

    def argv(self, side, out, *extra):
        return ["--baseline", self.path(side + "_baseline.json"), "--spec", self.path(side + "_spec.json"),
                "--out", self.path(out)] + list(extra)

    def bind_one(self, source_binding, baseline_cells=None):
        """Writes `one_baseline.json` / `one_spec.json` holding ONE binding and returns the argv prefix pieces."""
        cells = rf.typed_row(5, ["1"] * 12) if baseline_cells is None else baseline_cells
        self.put(self.path("one_baseline.json"), intake_xlsx.dump_bytes(rf.baseline_of(cells)))
        self.put(self.path("one_spec.json"), json.dumps(rf.spec_of([source_binding])).encode("utf-8"))

    def one_argv(self, out, *extra):
        return self.argv("one", out, *extra)

    def test_the_acme_sources_bind_under_acme_and_the_dashboard_holds_the_acme_models_only(self):
        rc, out, err = self.cli(bind_kpi.main, self.argv("acme", "acme_dashboard.json", "--project", "acme"))
        self.assertEqual((rc, err), (0, ""))
        report = json.loads(out)
        self.assertEqual((report["lists"], report["pivots"], report["bound_cells"]), (2, 1, 2 + 2 + 12))
        raw = read(self.path("acme_dashboard.json"))
        document = json.loads(raw.decode("utf-8"))
        self.assertEqual(models_of(document, "lists"), {sc.MODEL_LINE_MONTH, sc.MODEL_LINE_GROUP})
        self.assertEqual(models_of(document, "pivots"), {sc.MODEL_QC})
        text = raw.decode("utf-8").lower()
        for foreign in ("dksk", "mes.qc_form", "production_group", "report_group", "qms_history", "kpi_1"):
            self.assertNotIn(foreign, text)

    def test_the_dksk_sources_bind_under_dksk_and_the_dashboard_holds_the_dksk_models_only(self):
        rc, out, err = self.cli(bind_kpi.main, self.argv("dksk", "dksk_dashboard.json", "--project", "dksk"))
        self.assertEqual((rc, err), (0, ""))
        raw = read(self.path("dksk_dashboard.json"))
        document = json.loads(raw.decode("utf-8"))
        self.assertEqual(models_of(document, "lists"), {DKSK_LIST_MODEL})
        self.assertEqual(models_of(document, "pivots"), {DKSK_PIVOT_MODEL})
        text = raw.decode("utf-8").lower()
        for foreign in ("acme", "line_month", "line_group", "qc_log", "checked_at"):
            self.assertNotIn(foreign, text)

    def test_an_acme_only_source_is_refused_under_dksk_and_the_message_names_it(self):
        cases = (("line_month", sc.line_binding(5)), ("line_group", sc.group_binding(5)), ("qc_log", sc.qc_binding(5)))
        for name, binding in cases:
            with self.subTest(source=name):
                self.bind_one(binding)
                before = self.files()
                rc, out, err = self.cli(bind_kpi.main, self.one_argv("one_out.json", "--project", "dksk"))
                self.assertEqual((rc, out), (2, ""))
                self.assertEqual(len(err.splitlines()), 1)
                self.assertTrue(err.startswith("error: " + missing_source_text(name)), err)
                self.assertEqual(self.files(), before)                     # nothing written
                rc, out, err = self.cli(bind_kpi.main, self.one_argv("one_out.json", "--project", "acme"))
                self.assertEqual((rc, err), (0, ""))                      # the same files bind under the owner
                os.remove(self.path("one_out.json"))

    def test_a_dksk_only_source_is_refused_under_acme_and_the_message_names_it(self):
        cases = (("production_group", rf.list_binding(5, source="production_group")),
                 ("report_group", rf.list_binding(5)), ("qms_history", rf.qms_binding(5)))
        for name, binding in cases:
            with self.subTest(source=name):
                self.bind_one(binding)
                before = self.files()
                rc, out, err = self.cli(bind_kpi.main, self.one_argv("one_out.json", "--project", "acme"))
                self.assertEqual((rc, out), (2, ""))
                self.assertEqual(len(err.splitlines()), 1)
                self.assertTrue(err.startswith("error: " + missing_source_text(name)), err)
                self.assertEqual(self.files(), before)
                rc, out, err = self.cli(bind_kpi.main, self.one_argv("one_out.json", "--project", "dksk"))
                self.assertEqual((rc, err), (0, ""))
                os.remove(self.path("one_out.json"))

    def test_each_output_is_byte_deterministic(self):
        for side in ("acme", "dksk"):
            with self.subTest(project=side):
                rc1, out1, err1 = self.cli(bind_kpi.main, self.argv(side, side + "_1.json", "--project", side))
                rc2, out2, err2 = self.cli(bind_kpi.main, self.argv(side, side + "_2.json", "--project", side))
                self.assertEqual((rc1, rc2, err1, err2), (0, 0, "", ""))
                self.assertEqual(read(self.path(side + "_1.json")), read(self.path(side + "_2.json")))
                self.assertEqual(json.loads(out1)["sha256"], json.loads(out2)["sha256"])

    def test_a_name_that_exists_in_both_catalogs_binds_in_both_to_two_different_dashboards(self):
        self.bind_one(sc.shared_binding(5, count=2), rf.typed_row(5, ["1", "2"]))
        for project, model in (("acme", sc.MODEL_SHARED), ("dksk", DKSK_SHARED_MODEL)):
            rc, out, err = self.cli(bind_kpi.main, self.one_argv(project + "_shared.json", "--project", project))
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(models_of(json.loads(read(self.path(project + "_shared.json")).decode("utf-8")), "lists"),
                             {model})
        self.assertNotEqual(read(self.path("acme_shared.json")), read(self.path("dksk_shared.json")))

    def test_two_catalogs_and_no_project_is_refused_listing_both_names(self):
        rc, out, err = self.cli(bind_kpi.main, self.argv("acme", "refused.json"))
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("installed: acme, dksk", err)
        self.assertEqual(self.files(), self.before)


# ---------------------------------------------------------------- scan

class ScanTests(TwoProjects):
    def setUp(self):
        super().setUp()
        self.put(self.path("acme_baseline.json"), intake_xlsx.dump_bytes(sc.acme_sheet()))
        self.put(self.path("delta_baseline.json"), intake_xlsx.dump_bytes(sc.acme_sheet([sc.EXTRA_LABEL])))
        self.put(self.path("dksk_baseline.json"), intake_xlsx.dump_bytes(sf.twin()))
        self.before = self.files()

    def argv(self, side, *extra):
        return ["--baseline", self.path(side + "_baseline.json"), "--out", self.path("draft.json")] + list(extra)

    def draft(self):
        return json.loads(read(self.path("draft.json")).decode("utf-8"))

    def overlay(self, content):
        self.put(self.path("my_labels.json"), json.dumps(content).encode("utf-8"))
        return ["--aliases", self.path("my_labels.json")]

    def test_labels_known_only_to_the_acme_aliases_give_bindings_under_acme(self):
        rc, out, err = self.cli(scan_excel.main, self.argv("acme", "--project", "acme"))
        self.assertEqual((rc, err), (0, ""))
        draft = self.draft()
        self.assertEqual(draft["bindings"], sc.expected_acme_bindings())
        self.assertEqual(draft["labels"], {"B4": "Alpha Line", "B5": "Beta Line", "B6": "Gamma Line"})
        self.assertIn("bind dry run: ok (36 cells would be bound)", out)

    def test_the_same_labels_give_no_draft_under_dksk(self):
        rc, out, err = self.cli(scan_excel.main, self.argv("acme", "--project", "dksk"))
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("no row could be proposed", out)
        self.assertEqual(out.count("no catalog source has an alias for it"), 3)
        self.assertEqual(self.files(), self.before)

    def test_the_dksk_aliases_give_the_fourteen_bindings_under_dksk_and_none_under_acme(self):
        rc, out, err = self.cli(scan_excel.main, self.argv("dksk", "--project", "dksk"))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.draft()["bindings"], sf.expected_twin_bindings())
        os.remove(self.path("draft.json"))
        rc, out, err = self.cli(scan_excel.main, self.argv("dksk", "--project", "acme"))
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("no row could be proposed", out)
        self.assertEqual(out.count("no catalog source has an alias for it"), 14)
        self.assertEqual(self.files(), self.before)

    def test_a_draft_binds_under_the_project_that_scanned_it_and_only_there(self):
        self.cli(scan_excel.main, self.argv("acme", "--project", "acme"))
        bind_argv = ["--baseline", self.path("acme_baseline.json"), "--spec", self.path("draft.json")]
        rc, out, err = self.cli(bind_kpi.main, bind_argv + ["--out", self.path("a.json"), "--project", "acme"])
        self.assertEqual((rc, err), (0, ""))
        rc, out, err = self.cli(bind_kpi.main, bind_argv + ["--out", self.path("d.json"), "--project", "dksk"])
        self.assertEqual((rc, out), (2, ""))
        self.assertTrue(err.startswith("error: " + missing_source_text("line_month")), err)
        self.assertFalse(os.path.exists(self.path("d.json")))

    def test_the_user_overlay_adds_a_label_to_the_chosen_projects_catalog(self):
        rc, out, err = self.cli(scan_excel.main, self.argv("delta", "--project", "acme"))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.draft()["bindings"], sc.expected_acme_bindings())          # Delta Line: no alias yet
        self.assertIn('row 7 ("Delta Line") has typed numbers but no catalog source has an alias for it', out)
        rc, out, err = self.cli(scan_excel.main, self.argv("delta", "--project", "acme", "--force")
                                + self.overlay({"line_month": {"line_code": {"L_D": ["Delta Line"]}}}))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.draft()["bindings"], sc.expected_acme_bindings(["L_A", "L_B", "L_C", "L_D"]))

    def test_an_overlay_for_an_acme_source_is_refused_under_dksk(self):
        overlay = self.overlay({"line_month": {"line_code": {"L_D": ["Delta Line"]}}})
        rc, out, err = self.cli(scan_excel.main, self.argv("delta", "--project", "dksk") + overlay)
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("the --aliases file names a source that is not a list source of the catalog", err)
        self.assertEqual(self.files(), sorted(self.before + ["my_labels.json"]))

    def test_an_overlay_for_a_dksk_source_is_refused_under_acme_and_accepted_under_dksk(self):
        overlay = self.overlay({"report_group": {"group_code": {"DK_REF": ["Zed Line"]}}})
        rc, out, err = self.cli(scan_excel.main, self.argv("acme", "--project", "acme") + overlay)
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("the --aliases file names a source that is not a list source of the catalog", err)
        self.assertFalse(os.path.exists(self.path("draft.json")))
        rc, out, err = self.cli(scan_excel.main, self.argv("dksk", "--project", "dksk") + overlay)
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(len(self.draft()["bindings"]), 14)

    def test_an_overlay_value_the_chosen_catalog_does_not_list_is_refused(self):
        overlay = self.overlay({"line_month": {"line_code": {"L_Z": ["Zed Line"]}}})
        rc, out, err = self.cli(scan_excel.main, self.argv("delta", "--project", "acme") + overlay)
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("the --aliases file is refused", err)
        self.assertFalse(os.path.exists(self.path("draft.json")))


# ---------------------------------------------------------------- reconcile

class ReconcileBase(TwoProjects):
    """The in-process fake Odoo with the users, the shipped project's models and the generic ones."""

    def setUp(self):
        super().setUp()
        self.fake = start_fake(self)
        use_qaqc_env(self.fake.url)
        self.world = sc.make_world(self.fake)
        self.world.add_model(DKSK_SHARED_MODEL, dict(sc.SHARED_SCHEMA))

    def scene(self, project, typed, bindings):
        """baseline.json / spec.json / dashboard.json of the real flow with the catalog of `project`."""
        baseline_bytes, spec_bytes, self.dashboard_bytes = sc.produce(rf.baseline_of(typed), rf.spec_of(bindings), project)
        self.put(self.path("baseline.json"), baseline_bytes)
        self.put(self.path("spec.json"), spec_bytes)
        self.put(self.path("dashboard.json"), self.dashboard_bytes)

    def reconcile(self, *extra, year=2026):
        argv = ["--baseline", self.path("baseline.json"), "--spec", self.path("spec.json"),
                "--dashboard", self.path("dashboard.json"), "--year", str(year)] + list(extra)
        return self.cli(reconcile_kpi.main, argv, network=True)

    def data_calls(self):
        """{(model, method)} of the reads that reached a KPI data model."""
        return {(call[0], call[1]) for call in self.fake.sequence() if call[0] in ALL_DATA_MODELS}

    def acme_scene(self):
        """Typed numbers that agree with the fake rows except G5; decoy rows that a wrong selection would read."""
        world = self.world
        sc.add_row(world, sc.MODEL_LINE_MONTH, 2026, [100, 200, 300, 999], line_code="L_A", kind="actual")
        sc.add_row(world, sc.MODEL_LINE_MONTH, 2026, [1, 1, 1, 1], line_code="L_B", kind="actual")           # decoys
        sc.add_row(world, sc.MODEL_LINE_MONTH, 2026, [7, 7, 7, 7], line_code="L_A", kind="planned")
        sc.add_row(world, sc.MODEL_LINE_MONTH, 2025, [5, 5, 5, 5], line_code="L_A", kind="actual")
        sc.add_row(world, sc.MODEL_LINE_GROUP, 2026, [5, 6, 7], group_no=2)
        sc.add_row(world, sc.MODEL_LINE_GROUP, 2026, [9, 9, 9], group_no=3)
        for when in ("2026-03-31 20:00:00", "2026-01-15 12:00:00", "2026-04-15 12:00:00"):    # st1: Seoul m1=1, m4=2
            sc.add_check(world, when)
        sc.add_check(world, "2026-04-10 12:00:00", station="st2")                             # another station
        typed = dict(rf.typed_row(5, [100, 200, 300, 400]), **rf.typed_row(6, [5, 6, 7]),
                     **rf.typed_row(8, month_counts(m1=1, m4=2)))
        self.scene("acme", typed, [sc.line_binding(5, count=4), sc.group_binding(6, count=3), sc.qc_binding(8)])


class ReconcileTests(ReconcileBase):
    def test_the_acme_list_and_pivot_are_read_through_the_fake_and_counted(self):
        self.acme_scene()
        rc, out, err = self.reconcile("--project", "acme")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(counts_of(out), Counts(19, 19, 18, 0, 1, 0, 0, 0, 0))      # 4 + 3 + 12 typed numbers, G5 differs
        self.assertEqual(diff_cells(out), [("G5", "999")])
        self.assertEqual(HEADER.match(out.splitlines()[0]).group(3), "Asia/Seoul")
        self.assertEqual(self.data_calls(), {(sc.MODEL_LINE_MONTH, "search_read"), (sc.MODEL_LINE_GROUP, "search_read"),
                                             (sc.MODEL_QC, "read_group")})        # nothing of the other project is read

    def test_the_month_boundary_row_lands_in_the_month_of_the_users_zone_on_the_generic_model(self):
        for when in ("2026-03-31 20:00:00", "2026-01-15 12:00:00", "2026-04-15 12:00:00"):
            sc.add_check(self.world, when)
        # 20:00 UTC on March 31 is April 1 05:00 in Seoul and March 31 13:00 in Los Angeles
        for tz, expected in (("Asia/Seoul", month_counts(m1=1, m4=2)), ("America/Los_Angeles", month_counts(m1=1, m3=1, m4=1))):
            with self.subTest(user_tz=tz):
                self.world.users[FAKE_UID]["tz"] = tz
                self.scene("acme", rf.typed_row(8, expected), [sc.qc_binding(8)])
                rc, out, err = self.reconcile("--project", "acme")
                self.assertEqual((rc, err), (0, ""))
                self.assertEqual(counts_of(out), Counts(12, 12, 12, 0, 0, 0, 0, 0, 0))
        self.world.users[FAKE_UID]["tz"] = "Asia/Seoul"                              # typed with the UTC months: 2 differ
        self.scene("acme", rf.typed_row(8, month_counts(m1=1, m3=1, m4=1)), [sc.qc_binding(8)])
        rc, out, err = self.reconcile("--project", "acme")
        self.assertEqual(counts_of(out), Counts(12, 12, 10, 0, 2, 0, 0, 0, 0))
        self.assertEqual(diff_cells(out), [("F8", "0"), ("G8", "2")])
        read_group = [call for call in self.fake.sequence() if call[:2] == (sc.MODEL_QC, "read_group")][-1]
        self.assertEqual(read_group[3]["context"]["tz"], "Asia/Seoul")

    def test_the_acme_dashboard_is_refused_under_dksk_before_any_rpc(self):
        # `month_metric` exists in both catalogs (two models, two titles): the dashboard of one is not that of the other
        self.scene("acme", rf.typed_row(5, ["0", "0"]), [sc.shared_binding(5, count=2)])
        rc, out, err = self.reconcile("--project", "dksk")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("the dashboard file is not what the spec produces", err)
        self.assertEqual(len(self.fake.requests), 0)
        rc, out, err = self.reconcile("--project", "acme")                         # control: the owner reads it
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(counts_of(out), Counts(2, 2, 2, 0, 0, 0, 0, 0, 0))
        self.assertEqual(self.data_calls(), {(sc.MODEL_SHARED, "search_read")})

    def test_the_dksk_dashboard_is_refused_under_acme_before_any_rpc(self):
        self.scene("dksk", rf.typed_row(5, ["0", "0"]), [sc.shared_binding(5, count=2)])
        rc, out, err = self.reconcile("--project", "acme")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("the dashboard file is not what the spec produces", err)
        self.assertEqual(len(self.fake.requests), 0)
        rc, out, err = self.reconcile("--project", "dksk")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.data_calls(), {(DKSK_SHARED_MODEL, "search_read")})

    def test_a_spec_with_a_source_of_the_other_project_is_refused_naming_it_before_any_rpc(self):
        self.scene("acme", rf.typed_row(5, ["0"]), [sc.line_binding(5, count=1)])
        rc, out, err = self.reconcile("--project", "dksk")
        self.assertEqual((rc, out), (2, ""))
        self.assertTrue(err.startswith("error: " + missing_source_text("line_month")), err)
        self.scene("dksk", rf.typed_row(5, ["0"]), [rf.list_binding(5, source="production_group", count=1)])
        rc, out, err = self.reconcile("--project", "acme")
        self.assertEqual((rc, out), (2, ""))
        self.assertTrue(err.startswith("error: " + missing_source_text("production_group")), err)
        self.assertEqual(len(self.fake.requests), 0)

    def test_a_successful_acme_reconcile_never_prints_the_project_name(self):
        self.acme_scene()
        rc, out, err = self.reconcile("--project", "acme")
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(out)
        for text in (out, err):
            for name in ("acme", "dksk", "line_month", "qc_log", "kpi.production"):
                self.assertNotIn(name, text.lower())

    def test_a_successful_dksk_reconcile_never_prints_the_project_name(self):
        rf.add_list_row(self.world, rf.REPORT, 2026, [1, 2, 3], "DK_REF", "shipped")
        rf.add_qms(self.world, "2026-04-10 12:00:00")
        typed = dict(rf.typed_row(5, ["1", "2", "9"]), **rf.typed_row(8, month_counts(m4=1)))
        self.scene("dksk", typed, [rf.list_binding(5, count=3), rf.qms_binding(8)])
        rc, out, err = self.reconcile("--project", "dksk")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(counts_of(out), Counts(15, 15, 14, 0, 1, 0, 0, 0, 0))
        for text in (out, err):
            for name in ("acme", "dksk", "report_group", "qc_form"):
                self.assertNotIn(name, text.lower())

    def test_two_catalogs_and_no_project_is_refused_before_any_rpc(self):
        self.acme_scene()
        rc, out, err = self.reconcile()
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("installed: acme, dksk", err)
        self.assertEqual(len(self.fake.requests), 0)


# ---------------------------------------------------------------- the only catalog is the default

class SoleDefaultOfflineTests(TwoProjects):
    INSTALLED = ("acme",)

    def test_bind_without_a_project_uses_the_only_catalog(self):
        self.put(self.path("baseline.json"), intake_xlsx.dump_bytes(rf.baseline_of(rf.typed_row(5, ["1", "2"]))))
        self.put(self.path("spec.json"), json.dumps(rf.spec_of([sc.line_binding(5, count=2)])).encode("utf-8"))
        argv = ["--baseline", self.path("baseline.json"), "--spec", self.path("spec.json")]
        rc, out, err = self.cli(bind_kpi.main, argv + ["--out", self.path("plain.json")])
        self.assertEqual((rc, err), (0, ""))
        rc, out, err = self.cli(bind_kpi.main, argv + ["--out", self.path("named.json"), "--project", "acme"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(read(self.path("plain.json")), read(self.path("named.json")))
        self.assertEqual(models_of(json.loads(read(self.path("plain.json")).decode("utf-8")), "lists"),
                         {sc.MODEL_LINE_MONTH})

    def test_scan_without_a_project_uses_the_only_catalog(self):
        self.put(self.path("baseline.json"), intake_xlsx.dump_bytes(sc.acme_sheet()))
        rc, out, err = self.cli(scan_excel.main, ["--baseline", self.path("baseline.json"), "--out", self.path("draft.json")])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(read(self.path("draft.json")).decode("utf-8"))["bindings"],
                         sc.expected_acme_bindings())

    def test_the_shipped_project_is_unknown_when_only_acme_is_installed(self):
        self.put(self.path("baseline.json"), intake_xlsx.dump_bytes(sc.acme_sheet()))
        rc, out, err = self.cli(scan_excel.main, ["--baseline", self.path("baseline.json"), "--out", self.path("draft.json"),
                                                  "--project", "dksk"])
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("installed: acme", err)
        self.assertEqual(self.files(), ["baseline.json"])


class SoleDefaultReconcileTests(ReconcileBase):
    INSTALLED = ("acme",)

    def test_reconcile_without_a_project_uses_the_only_catalog(self):
        self.acme_scene()
        rc, out, err = self.reconcile()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(counts_of(out), Counts(19, 19, 18, 0, 1, 0, 0, 0, 0))
        self.assertEqual(diff_cells(out), [("G5", "999")])
        self.assertEqual(self.data_calls(), {(sc.MODEL_LINE_MONTH, "search_read"), (sc.MODEL_LINE_GROUP, "search_read"),
                                             (sc.MODEL_QC, "read_group")})


if __name__ == "__main__":
    unittest.main()
