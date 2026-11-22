"""scripts/publish_dashboard.py against the fake Odoo (ownership / permission class).

Every refusal of the publisher has a test here AND is meant to have a mutant that removes it (tests/README of the mutant
kit: `run_mutants.py --section publish`). The fake never enforces ACL: a script that forgets a guard is seen writing.
"""
import base64
import copy
import itertools
import json
import os
import unittest

from dashboard_fixtures import (COLLEAGUE_UID, DASHBOARD_BYTES, GROUP, Scenario, StreamCase, start_fake, use_qaqc_env,
                                write_dashboard_file)
from fake_odoo import FAKE_UID

import publish_dashboard

DASH = "spreadsheet.dashboard"
ENCODED = base64.b64encode(DASHBOARD_BYTES).decode("ascii")


class PublishCase(StreamCase):
    def setUp(self):
        super().setUp()
        self.fake = start_fake(self)
        self.s = Scenario(self.fake)
        self.world = self.s.world
        use_qaqc_env(self.fake.url)
        self.file = write_dashboard_file(self.tmp)

    def run_pub(self, *argv, **kwargs):
        return self.collect(publish_dashboard.main, argv, **kwargs)

    def argv_for(self, op, dashboard_id, apply=True):
        extra = ["--apply"] if apply else []
        if op == "archive":
            return ["archive", "--id", str(dashboard_id)] + extra
        if op == "delete-own":
            return ["delete-own", "--id", str(dashboard_id)] + extra
        return ["publish", "--dashboard", self.file, "--replace", str(dashboard_id),
                "--confirm-replace", str(dashboard_id)] + extra

    def assertNoWrites(self):
        self.assertEqual(self.fake.write_requests(), [])

    def assertOnlyDashboardWrites(self):
        self.assertEqual([call for call in self.fake.write_requests() if call[0] != DASH], [])


ID_OPS = ("archive", "delete-own", "replace")
ALL_OPS = ("list", "publish-new", "archive", "delete-own", "replace")


class ConfiguredCase(PublishCase):
    def argv_any(self, op):
        if op == "list":
            return ["list"]
        if op == "publish-new":
            return ["publish", "--dashboard", self.file, "--name", "Fresh", "--apply"]
        return self.argv_for(op, self.s.own)


# ---------------------------------------------------------------------------- ownership: what may be written

class OwnDashboardTests(PublishCase):
    def test_archive_writes_active_false_on_exactly_that_record(self):
        for target in self.s.own_ids:
            with self.subTest(target=target):
                before = self.world.snapshot()
                self.fake.requests.clear()
                code, out, err = self.run_pub(*self.argv_for("archive", target))
                self.assertEqual((code, out), (0, ""), err)
                self.assertEqual(self.fake.write_requests(), [(DASH, "write", [[target], {"active": False}], {})])
                self.assertFalse(self.world.dashboards[target]["active"])
                users, groups, dashboards = before
                self.assertEqual({k: v for k, v in self.world.dashboards.items() if k != target},
                                 {k: v for k, v in dashboards.items() if k != target})

    def test_delete_own_unlinks_exactly_that_record(self):
        for target in self.s.own_ids:
            with self.subTest(target=target):
                self.fake.requests.clear()
                others = {k: copy.deepcopy(v) for k, v in self.world.dashboards.items() if k != target}
                code, out, err = self.run_pub(*self.argv_for("delete-own", target))
                self.assertEqual((code, out), (0, ""), err)
                self.assertEqual(self.fake.write_requests(), [(DASH, "unlink", [[target]], {})])
                self.assertNotIn(target, self.world.dashboards)
                self.assertEqual(self.world.dashboards, others)

    def test_replace_rewrites_only_the_binary_of_that_record_and_prints_its_id(self):
        for target in self.s.own_ids:
            with self.subTest(target=target):
                self.fake.requests.clear()
                code, out, err = self.run_pub(*self.argv_for("replace", target))
                self.assertEqual((code, out), (0, "%d\n" % target), err)
                self.assertEqual(self.fake.write_requests(),
                                 [(DASH, "write", [[target], {"spreadsheet_binary_data": ENCODED}], {})])
                self.assertEqual(self.world.dashboards[target]["spreadsheet_binary_data"], ENCODED)
                self.assertIn("lost", err)

    def test_the_publisher_never_writes_anything_but_dashboards(self):
        for op in ID_OPS:
            self.run_pub(*self.argv_for(op, self.s.own))
            self.run_pub(*self.argv_for(op, self.s.foreign))
        self.run_pub("publish", "--dashboard", self.file, "--name", "Fresh", "--apply")
        self.assertTrue(self.fake.write_requests())
        self.assertOnlyDashboardWrites()
        self.assertEqual(self.fake.execute_calls("spreadsheet.dashboard.group", "create"), [])
        self.assertEqual(self.fake.execute_calls("spreadsheet.dashboard.group", "write"), [])
        self.assertEqual(self.fake.execute_calls("spreadsheet.dashboard.group", "unlink"), [])
        self.assertEqual(self.fake.execute_calls("res.users", "write"), [])
        # ir.model.data is unreadable for a QA/QC user: the only call allowed on it is the group reference
        touched = {(m, meth) for m, meth, _, _ in self.fake.sequence() if m == "ir.model.data"}
        self.assertLessEqual(touched, {("ir.model.data", "check_object_reference")})
        self.assertEqual(self.fake.execute_calls("res.users", "search_read"), [])


class ForeignDashboardTests(PublishCase):
    """Every operation refuses every kind of dashboard that is not the publisher's own, each for its own reason."""

    def cases(self):
        s = self.s
        return (("colleague's dashboard", s.foreign, "created by someone else"),
                ("shipped dashboard in the group", s.shipped, "external id"),
                ("shipped dashboard I created, in the group", s.shipped_mine, "external id"),
                ("orphan (creator gone)", s.orphan, "created by someone else"),
                ("my dashboard in ANOTHER group", s.elsewhere_mine, "not in the group"),
                ("shipped dashboard in another group", s.elsewhere_shipped, "not in the group"),
                ("no such dashboard", 999999, "does not exist"))

    def test_every_operation_refuses_every_foreign_dashboard_and_writes_nothing(self):
        for op, (label, target, reason) in itertools.product(ID_OPS, self.cases()):
            with self.subTest(op=op, case=label):
                self.fake.requests.clear()
                before = self.world.snapshot()
                code, out, err = self.run_pub(*self.argv_for(op, target))
                self.assertEqual(code, 2, err)
                self.assertEqual(out, "")
                self.assertIn("is not yours to change", err)
                self.assertIn(reason, err)
                self.assertNoWrites()
                self.assertEqual(self.world.snapshot(), before)

    def test_a_correct_replace_confirmation_does_not_unlock_a_foreign_dashboard(self):
        code, out, err = self.run_pub(*self.argv_for("replace", self.s.foreign))
        self.assertEqual(code, 2)
        self.assertNoWrites()

    def test_dry_run_also_refuses_a_foreign_dashboard(self):
        for op in ID_OPS:
            with self.subTest(op=op):
                code, out, err = self.run_pub(*self.argv_for(op, self.s.foreign, apply=False))
                self.assertEqual(code, 2, err)
                self.assertIn("is not yours to change", err)

    def test_each_rule_alone_is_enough_to_refuse(self):
        # one dashboard per rule, everything else true: group / xmlid / creator are three independent refusals
        world, s = self.world, self.s
        only_group = world.add_dashboard("only wrong group", s.other_group, creator=FAKE_UID)
        only_xmlid = world.add_dashboard("only shipped", s.group, creator=FAKE_UID, xmlids=["m.x"])
        only_creator = world.add_dashboard("only other creator", s.group, creator=COLLEAGUE_UID)
        for target in (only_group, only_xmlid, only_creator):
            with self.subTest(target=target):
                code, _, err = self.run_pub(*self.argv_for("archive", target))
                self.assertEqual(code, 2, err)
        self.assertNoWrites()
        good = world.add_dashboard("all rules hold", s.group, creator=FAKE_UID)
        self.assertEqual(self.run_pub(*self.argv_for("archive", good))[0], 0)

    def test_ownership_is_read_again_by_every_command(self):
        self.assertEqual(self.run_pub(*self.argv_for("archive", self.s.own))[0], 0)
        self.world.dashboards[self.s.own]["create_uid"] = COLLEAGUE_UID      # a colleague took it over meanwhile
        self.fake.requests.clear()
        code, _, err = self.run_pub(*self.argv_for("delete-own", self.s.own))
        self.assertEqual(code, 2, err)
        self.assertNoWrites()
        self.world.dashboards[self.s.own]["create_uid"] = FAKE_UID
        self.world.dashboards[self.s.own]["xmlids"] = ["m.shipped_now"]
        self.assertEqual(self.run_pub(*self.argv_for("delete-own", self.s.own))[0], 2)
        self.assertNoWrites()

    def test_a_dashboard_is_not_own_for_another_publisher_uid(self):
        # the same data, a publisher with another uid: the creator rule compares with THIS login's uid
        self.world.dashboards[self.s.own]["create_uid"] = COLLEAGUE_UID
        code, _, err = self.run_pub(*self.argv_for("archive", self.s.own))
        self.assertEqual(code, 2, err)
        self.assertNoWrites()


class CheckBeforeWriteTests(PublishCase):
    def test_the_ownership_is_read_immediately_before_every_write(self):
        for op in ID_OPS:
            with self.subTest(op=op):
                target = self.world.add_dashboard("fresh own %s" % op, self.s.group, creator=FAKE_UID)
                self.fake.requests.clear()
                code, _, err = self.run_pub(*self.argv_for(op, target))
                self.assertEqual(code, 0, err)
                seq = self.fake.sequence()
                index = [i for i, call in enumerate(seq) if call[1] in ("write", "unlink")]
                self.assertEqual(len(index), 1)
                at = index[0]
                self.assertEqual(seq[at - 1][:3], (DASH, "get_metadata", [[target]]))
                self.assertEqual(seq[at - 2][:2], (DASH, "read"))
                self.assertEqual(seq[at - 2][2][0], [target])
                self.assertEqual(seq[at - 3][:2], ("spreadsheet.dashboard.group", "search"))

    def test_nothing_is_written_when_the_check_fails_whatever_the_order_of_calls(self):
        # a foreign record must be untouched in the fake itself (a write-then-check order would have changed it)
        for op in ID_OPS:
            with self.subTest(op=op):
                target = self.s.foreign
                before = copy.deepcopy(self.world.dashboards[target])
                self.run_pub(*self.argv_for(op, target))
                self.assertEqual(self.world.dashboards.get(target), before)

    def test_creating_reads_the_group_by_name_just_before_the_create(self):
        self.run_pub("publish", "--dashboard", self.file, "--name", "Fresh", "--apply")
        seq = self.fake.sequence()
        at = [i for i, call in enumerate(seq) if call[1] == "create"][0]
        searches = [i for i, call in enumerate(seq[:at]) if (call[0], call[1]) == ("spreadsheet.dashboard.group", "search")]
        self.assertTrue(searches)
        self.assertEqual(seq[searches[0]][2], [[["name", "=", GROUP]]])
        self.assertLess(searches[0], at)


# ---------------------------------------------------------------------------- fail closed

class FailClosedTests(PublishCase):
    def metadata_variants(self, target):
        good = self.world.metadata_row(target)

        def without(key):
            row = dict(good)
            del row[key]
            return [row]

        def with_(**changes):
            return [{**good, **changes}]

        return {
            "empty list": [], "dict, not a list": good, "None": None, "text": "ok", "list of ints": [1],
            "list of None": [None], "two rows": [good, good], "row is a list": [[good]],
            "missing xmlids": without("xmlids"), "xmlids None": with_(xmlids=None), "xmlids False": with_(xmlids=False),
            "xmlids empty text": with_(xmlids=""), "xmlids a dict": with_(xmlids={}),
            "xmlids has one entry": with_(xmlids=[{"xmlid": "m.x", "noupdate": False}]),
            "xmlid set, xmlids empty": with_(xmlid="m.x", xmlids=[]),
            "missing create_uid": without("create_uid"), "create_uid False": with_(create_uid=False),
            "create_uid None": with_(create_uid=None), "create_uid empty list": with_(create_uid=[]),
            "create_uid bare int": with_(create_uid=FAKE_UID), "create_uid text id": with_(create_uid=[str(FAKE_UID), "x"]),
            "create_uid float id": with_(create_uid=[float(FAKE_UID), "x"]),
            "create_uid other user": with_(create_uid=[COLLEAGUE_UID, "Colleague"]),
            "create_uid bool": with_(create_uid=[True, "x"]),
        }

    def test_an_unreadable_or_malformed_metadata_refuses_every_operation(self):
        target = self.s.own
        for op, (label, raw) in itertools.product(ID_OPS, self.metadata_variants(target).items()):
            with self.subTest(op=op, metadata=label):
                self.fake.requests.clear()
                self.world.metadata_override[target] = raw
                code, out, err = self.run_pub(*self.argv_for(op, target))
                self.assertEqual(code, 2, err)
                self.assertEqual(out, "")
                self.assertIn("is not yours to change", err)
                self.assertNoWrites()

    def test_an_odoo_error_on_get_metadata_stops_everything_without_a_write(self):
        from fake_odoo import DocumentsError
        self.world.metadata_override[self.s.own] = DocumentsError("cannot read the metadata")
        for op in ID_OPS:
            with self.subTest(op=op):
                code, out, err = self.run_pub(*self.argv_for(op, self.s.own))
                self.assertNotEqual(code, 0)
                self.assertEqual(out, "")
                self.assertNoWrites()

    def test_an_odoo_error_on_the_read_stops_everything_without_a_write(self):
        from fake_odoo import DocumentsError
        self.world.read_override[self.s.own] = DocumentsError("cannot read the dashboard")
        for op in ID_OPS:
            with self.subTest(op=op):
                code, out, err = self.run_pub(*self.argv_for(op, self.s.own))
                self.assertNotEqual(code, 0)
                self.assertNoWrites()

    def test_a_malformed_read_answer_refuses(self):
        good = {"id": self.s.own, "dashboard_group_id": [self.s.group, GROUP], "name": "x"}
        for label, raw in (("empty", []), ("None", None), ("dict", {"id": 1}), ("two rows", [{}, {}]),
                           ("two good rows", [good, good]), ("good row then junk", [good, "junk"]),
                           ("row is text", ["x"]), ("no group key", [{"id": self.s.own, "name": "x"}]),
                           ("group False", [{"id": self.s.own, "dashboard_group_id": False}]),
                           ("group bare id", [{"id": self.s.own, "dashboard_group_id": self.s.group}]),
                           ("group other", [{"id": self.s.own, "dashboard_group_id": [self.s.other_group, "x"]}])):
            for op in ID_OPS:
                with self.subTest(op=op, read=label):
                    self.world.read_override[self.s.own] = raw
                    code, out, err = self.run_pub(*self.argv_for(op, self.s.own))
                    self.assertEqual(code, 2, err)
                    self.assertNoWrites()

    def test_a_bool_creator_id_is_not_a_uid_even_for_uid_1(self):
        # JSON `true` == 1 in Python: a publisher with uid 1 must not be fooled by [true, "x"]
        fake = start_fake(self, uid=1)
        scenario = Scenario(fake)
        scenario.world.add_user(1, "qa.user", "QA User")
        target = scenario.world.add_dashboard("bool creator", scenario.group, creator=1)
        scenario.world.metadata_override[target] = [{**scenario.world.metadata_row(target), "create_uid": [True, "x"]}]
        use_qaqc_env(fake.url)
        code, _, err = self.run_pub("archive", "--id", str(target), "--apply")
        self.assertEqual(code, 2, err)
        self.assertEqual(fake.write_requests(), [])
        scenario.world.metadata_override.clear()
        self.assertEqual(self.run_pub("archive", "--id", str(target), "--apply")[0], 0)


# ---------------------------------------------------------------------------- the group

class GroupTests(ConfiguredCase):
    def test_the_group_is_looked_up_by_exact_name_in_the_search_domain(self):
        self.run_pub("list")
        first = [c for c in self.fake.sequence() if (c[0], c[1]) == ("spreadsheet.dashboard.group", "search")][0]
        self.assertEqual(first[2], [[["name", "=", GROUP]]])

    def test_a_missing_group_refuses_every_operation_and_writes_nothing(self):
        for name in ("kpi (qa/qc)", "KPI (QA/QC) ", " KPI (QA/QC)", "KPI (QA/QC)2", "KPI", "Old KPI (QA/QC)", "KPI (QA-QC)"):
            self.world.groups[self.s.group]["name"] = name
            for op in ALL_OPS:
                with self.subTest(group=name, op=op):
                    self.fake.requests.clear()
                    code, out, err = self.run_pub(*self.argv_any(op))
                    self.assertEqual(code, 2, err)
                    self.assertEqual(out, "")
                    self.assertIn("exactly one dashboard group", err)
                    self.assertIn("found 0", err)
                    self.assertNoWrites()

    def test_a_duplicate_group_refuses_every_operation_and_writes_nothing(self):
        self.world.add_group(GROUP)
        for op in ALL_OPS:
            with self.subTest(op=op):
                self.fake.requests.clear()
                code, out, err = self.run_pub(*self.argv_any(op))
                self.assertEqual(code, 2, err)
                self.assertEqual(out, "")
                self.assertIn("found 2", err)
                self.assertNoWrites()

    def test_three_groups_are_refused_too(self):
        self.world.add_group(GROUP)
        self.world.add_group(GROUP)
        code, _, err = self.run_pub("list")
        self.assertEqual(code, 2)
        self.assertIn("found 3", err)

    def test_lookalike_groups_next_to_the_exact_one_do_not_disturb_it(self):
        lookalikes = [self.world.add_group(n) for n in ("kpi (qa/qc)", "KPI (QA/QC) archive", "KPI", "Old KPI (QA/QC)")]
        mine_there = [self.world.add_dashboard("Lookalike mine", g, creator=FAKE_UID) for g in lookalikes]
        code, out, err = self.run_pub("list")
        self.assertEqual(code, 0, err)
        listed = {int(line.split("\t")[0]) for line in out.splitlines()}
        self.assertEqual(listed, {self.s.own, self.s.archived_own})
        self.assertFalse(listed & set(mine_there))
        code, out, err = self.run_pub("publish", "--dashboard", self.file, "--name", "Fresh", "--apply")
        self.assertEqual(code, 0, err)
        created = int(out.strip())
        self.assertEqual(self.world.dashboards[created]["dashboard_group_id"], self.s.group)
        for target in mine_there:
            with self.subTest(target=target):
                self.assertEqual(self.run_pub(*self.argv_for("archive", target))[0], 2)

    def test_a_malformed_group_answer_refuses_and_writes_nothing(self):
        for label, raw in (("None", None), ("False", False), ("text", "5"), ("dict", {"a": 1}), ("bool id", [True]),
                           ("float id", [1.5]), ("text id", ["7"]), ("None id", [None]), ("empty", []), ("two ids", [3, 4]),
                           ("same id twice", [11, 11])):
            for op in ALL_OPS:
                with self.subTest(answer=label, op=op):
                    self.world.group_search_override = raw
                    self.fake.requests.clear()
                    code, out, err = self.run_pub(*self.argv_any(op))
                    self.assertEqual(code, 2, err)
                    self.assertIn("exactly one dashboard group", err)
                    self.assertNoWrites()

    # ---- round 2 (security r1, finding 4): `name` is a translated field, the lookup pins the stored language
    LOOKUP = {"context": {"active_test": False, "lang": "en_US"}}

    def dry_argv(self, op):
        if op == "list":
            return ["list"]
        if op == "publish-new":
            return ["publish", "--dashboard", self.file, "--name", "Fresh"]
        return self.argv_for(op, self.s.own, apply=False)

    def test_every_command_looks_the_group_up_with_the_stored_language_in_the_context(self):
        for op in ALL_OPS:
            with self.subTest(op=op):
                self.fake.requests.clear()
                code, out, err = self.run_pub(*self.dry_argv(op))
                self.assertEqual(code, 0, err)
                lookups = [c for c in self.fake.sequence() if c[:2] == ("spreadsheet.dashboard.group", "search")]
                self.assertEqual([c[2:] for c in lookups], [([[["name", "=", GROUP]]], self.LOOKUP)])

    def test_a_group_whose_translation_equals_the_name_is_not_the_group(self):
        # a ko_KR publisher through a client that carries the user's language (`execute_kw` does not, security r2: the
        # explicit en_US pin is defence in depth): the decoy's en_US name differs but its Korean text is "KPI (QA/QC)";
        # compared in the user's language it would make two groups ("found 2"), in the pinned en_US it is no candidate
        self.world.users[FAKE_UID]["lang"] = "ko_KR"
        self.world.context_lang_from_user = True          # a client that carries the user's language (defence in depth)
        decoy = self.world.add_group("Decoy", translations={"ko_KR": GROUP})
        for op in ALL_OPS:
            with self.subTest(op=op):
                code, out, err = self.run_pub(*self.dry_argv(op))
                self.assertEqual(code, 0, err)
        code, out, err = self.run_pub("publish", "--dashboard", self.file, "--name", "Fresh", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.world.dashboards[int(out)]["dashboard_group_id"], self.s.group)
        self.assertNotEqual(self.world.dashboards[int(out)]["dashboard_group_id"], decoy)

    def test_a_group_renamed_in_another_language_is_still_found(self):
        self.world.users[FAKE_UID]["lang"] = "ko_KR"
        self.world.context_lang_from_user = True          # a client that carries the user's language (defence in depth)
        self.world.groups[self.s.group]["translations"] = {"ko_KR": "KPI (품질)"}
        for op in ALL_OPS:
            with self.subTest(op=op):
                code, out, err = self.run_pub(*self.dry_argv(op))
                self.assertEqual(code, 0, err)

    def test_a_decoy_found_only_in_a_translation_is_never_used_when_the_real_group_is_gone(self):
        self.world.users[FAKE_UID]["lang"] = "ko_KR"
        self.world.context_lang_from_user = True          # a client that carries the user's language (defence in depth)
        self.world.add_group("Decoy", translations={"ko_KR": GROUP})
        del self.world.groups[self.s.group]
        for op in ALL_OPS:
            with self.subTest(op=op):
                code, out, err = self.run_pub(*self.dry_argv(op))
                self.assertEqual(code, 2, err)
                self.assertIn("found 0", err)
                self.assertNoWrites()


# ---------------------------------------------------------------------------- dry run

class DryRunTests(PublishCase):
    def test_no_apply_means_no_write_at_all(self):
        for op in ID_OPS:
            for target in self.s.own_ids:
                with self.subTest(op=op, target=target):
                    self.fake.requests.clear()
                    before = self.world.snapshot()
                    code, out, err = self.run_pub(*self.argv_for(op, target, apply=False))
                    self.assertEqual((code, out), (0, ""), err)
                    self.assertIn("dry run", err)
                    self.assertNoWrites()
                    self.assertEqual(self.world.snapshot(), before)

    def test_publish_new_without_apply_writes_nothing_and_says_what_it_would_do(self):
        before = self.world.snapshot()
        code, out, err = self.run_pub("publish", "--dashboard", self.file, "--name", "Quality")
        self.assertEqual((code, out), (0, ""), err)
        self.assertIn("will create the dashboard 'Quality v3'", err)
        self.assertIn("dry run", err)
        self.assertNoWrites()
        self.assertEqual(self.world.snapshot(), before)

    PLAN_SENTENCE = "(visible to EVERY internal user; the text of the Excel design area is copied as it is)"

    def test_the_plan_line_says_who_will_see_the_dashboard_and_that_the_excel_text_is_copied(self):
        # combined security review M4: the QA/QC user must be able to give informed consent before --apply
        code, out, err = self.run_pub("publish", "--dashboard", self.file, "--name", "Quality")
        self.assertEqual((code, out), (0, ""), err)
        self.assertEqual(err.splitlines(), [
            "will create the dashboard 'Quality v3' in the group %r %s" % (publish_dashboard.GROUP_NAME, self.PLAN_SENTENCE),
            "dry run: nothing written, add --apply"])

    def test_the_same_plan_line_is_printed_before_a_real_publish(self):
        code, out, err = self.run_pub("publish", "--dashboard", self.file, "--name", "Quality", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(err.splitlines(), [
            "will create the dashboard 'Quality v3' in the group %r %s" % (publish_dashboard.GROUP_NAME, self.PLAN_SENTENCE)])
        self.assertEqual(self.world.dashboards[int(out)]["name"], "Quality v3")

    def test_the_sentence_is_true_the_new_dashboard_goes_to_the_internal_user_group(self):
        # what the sentence promises is what the create call does: the group of every internal user, nothing narrower
        self.world.res_groups["base.group_user"] = 77
        self.run_pub("publish", "--dashboard", self.file, "--name", "Quality", "--apply")
        create = [call for call in self.fake.write_requests() if call[1] == "create"][0]
        self.assertEqual(create[2][0]["group_ids"], [[6, 0, [77]]])

    def test_only_publishing_a_new_dashboard_carries_the_sentence(self):
        target = self.world.add_dashboard("control", self.s.group, creator=FAKE_UID)
        for argv in (["list"], ["archive", "--id", str(target)], ["delete-own", "--id", str(target)],
                     ["publish", "--dashboard", self.file, "--replace", str(target), "--confirm-replace", str(target)]):
            with self.subTest(argv=argv[0]):
                code, out, err = self.run_pub(*argv)
                self.assertEqual(code, 0, err)
                self.assertNotIn("EVERY internal user", err + out)

    def test_apply_is_what_turns_the_same_commands_into_writes(self):
        for op in ID_OPS:
            with self.subTest(op=op):
                target = self.world.add_dashboard("control %s" % op, self.s.group, creator=FAKE_UID)
                self.fake.requests.clear()
                self.run_pub(*self.argv_for(op, target))
                self.assertEqual(len(self.fake.write_requests()), 1)

    def test_a_dry_run_with_a_failing_check_still_writes_nothing(self):
        for op in ID_OPS:
            self.run_pub(*self.argv_for(op, self.s.shipped, apply=False))
        self.assertNoWrites()


# ---------------------------------------------------------------------------- versions

class VersionTests(PublishCase):
    def publish_name(self, name):
        code, out, err = self.run_pub("publish", "--dashboard", self.file, "--name", name, "--apply")
        self.assertEqual(code, 0, err)
        return self.world.dashboards[int(out.strip())]["name"]

    def test_a_first_publish_is_version_1(self):
        self.assertEqual(self.publish_name("Brand new"), "Brand new v1")

    def test_the_next_version_follows_the_highest_own_one_archived_included(self):
        for title, active in (("Rev v1", True), ("Rev v3", True), ("Rev v5", False)):
            self.world.add_dashboard(title, self.s.group, creator=FAKE_UID, active=active)
        self.assertEqual(self.publish_name("Rev"), "Rev v6")

    def test_versions_compare_as_numbers_not_as_text(self):
        for title in ("Num v2", "Num v10", "Num v9"):
            self.world.add_dashboard(title, self.s.group, creator=FAKE_UID)
        self.assertEqual(self.publish_name("Num"), "Num v11")

    def test_a_plain_name_counts_as_version_0(self):
        self.world.add_dashboard("Plain", self.s.group, creator=FAKE_UID)
        self.assertEqual(self.publish_name("Plain"), "Plain v1")

    def test_foreign_and_shipped_versions_are_not_counted(self):
        s = self.s
        self.world.add_dashboard("Shared v7", s.group, creator=COLLEAGUE_UID)
        self.world.add_dashboard("Shared v8", s.group, creator=FAKE_UID, xmlids=["m.x"])
        self.world.add_dashboard("Shared v9", s.other_group, creator=FAKE_UID)
        self.world.add_dashboard("Shared v4", s.group, creator=FAKE_UID)
        self.assertEqual(self.publish_name("Shared"), "Shared v5")

    def test_other_names_are_not_counted(self):
        for title in ("Other Rev v7", "Rev v7 draft", "Rev2 v8", "rev v9"):
            self.world.add_dashboard(title, self.s.group, creator=FAKE_UID)
        self.assertEqual(self.publish_name("Rev"), "Rev v1")

    def test_publishing_twice_creates_two_dashboards_and_keeps_the_hand_edited_one(self):
        first = self.publish_name("Twice")
        edited_id = [k for k, v in self.world.dashboards.items() if v["name"] == first][0]
        self.world.dashboards[edited_id]["spreadsheet_binary_data"] = "HAND-EDITED"
        second = self.publish_name("Twice")
        self.assertEqual((first, second), ("Twice v1", "Twice v2"))
        self.assertEqual(self.world.dashboards[edited_id]["spreadsheet_binary_data"], "HAND-EDITED")
        self.assertEqual([c for c in self.fake.write_requests() if c[1] != "create"], [])


# ---------------------------------------------------------------------------- publish (create)

class PublishCreateTests(PublishCase):
    def test_a_new_dashboard_is_created_with_this_exact_payload_and_its_id_is_the_only_stdout(self):
        code, out, err = self.run_pub("publish", "--dashboard", self.file, "--name", "Report", "--apply")
        self.assertEqual(code, 0, err)
        created = max(self.world.dashboards)
        self.assertEqual(out, "%d\n" % created)
        self.assertEqual(self.fake.write_requests(), [(DASH, "create", [{
            "name": "Report v1", "dashboard_group_id": self.s.group, "group_ids": [[6, 0, [1]]],
            "spreadsheet_binary_data": ENCODED}], {})])
        self.assertEqual(self.world.dashboards[created]["create_uid"], FAKE_UID)
        self.assertEqual(base64.b64decode(self.world.dashboards[created]["spreadsheet_binary_data"]), DASHBOARD_BYTES)

    def test_the_new_dashboard_is_the_publishers_own_afterwards(self):
        out = self.run_pub("publish", "--dashboard", self.file, "--name", "Report", "--apply")[1]
        created = int(out)
        self.assertEqual(self.run_pub("archive", "--id", str(created), "--apply")[0], 0)
        self.assertFalse(self.world.dashboards[created]["active"])

    def test_the_visible_group_is_resolved_from_the_server_not_assumed(self):
        self.world.res_groups["base.group_user"] = 77
        self.run_pub("publish", "--dashboard", self.file, "--name", "Report", "--apply")
        create = self.fake.write_requests()[0]
        self.assertEqual(create[2][0]["group_ids"], [[6, 0, [77]]])
        refs = self.fake.execute_calls("ir.model.data", "check_object_reference")
        self.assertEqual(refs, [["base", "group_user"]])

    def test_a_bad_internal_group_answer_stops_before_the_create(self):
        for label, raw in (("None", None), ("empty", []), ("one item", ["res.groups"]), ("zero id", ["res.groups", 0]),
                           ("bool id", ["res.groups", True]), ("text id", ["res.groups", "1"]),
                           ("negative", ["res.groups", -1]), ("three items", ["res.groups", 1, 2]), ("dict", {"a": 1})):
            with self.subTest(answer=label):
                self.fake.requests.clear()
                self.world.ref_override["base.group_user"] = raw
                code, out, err = self.run_pub("publish", "--dashboard", self.file, "--name", "Report", "--apply")
                self.assertEqual(code, 2, err)
                self.assertEqual(out, "")
                self.assertIn("internal-user group", err)
                self.assertNoWrites()

    def test_a_server_answer_that_is_no_id_is_an_error_and_prints_no_id(self):
        for label, raw in (("False", False), ("zero", 0), ("True", True), ("text", "5"), ("negative", -1), ("list", [5])):
            with self.subTest(answer=label):
                self.world.create_answer = raw
                code, out, err = self.run_pub("publish", "--dashboard", self.file, "--name", "Report", "--apply")
                self.assertEqual(code, 2, err)
                self.assertEqual(out, "")
                self.assertIn("did not return the id", err)

    def test_the_name_is_checked_before_anything_is_written(self):
        for label, name in (("formula", "=SUM(1)"), ("plus", "+x"), ("minus", "-x"), ("at", "@x"), ("empty", ""),
                            ("blank", "   "), ("newline", "a\nb"), ("control", "a\x07b"), ("too long", "x" * 95),
                            ("formula after space", "  =x")):
            with self.subTest(name=label):
                code, out, err = self.run_pub("publish", "--dashboard", self.file, "--name=" + name, "--apply")
                self.assertEqual(code, 2, err)
                self.assertEqual(out, "")
                self.assertNoWrites()

    def test_the_longest_accepted_name_leaves_room_for_the_version_suffix(self):
        code, out, err = self.run_pub("publish", "--dashboard", self.file, "--name", "x" * 94, "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.world.dashboards[int(out)]["name"], "x" * 94 + " v1")
        self.assertLessEqual(len(self.world.dashboards[int(out)]["name"]), publish_dashboard.NAME_LIMIT)

    def test_a_bad_dashboard_file_is_refused_before_any_write(self):
        formula = {"sheets": [{"id": "s", "cells": {"A1": {"content": "=NOPE.FN(1)"}}}]}
        cases = {"missing": None, "not json": b"{not json", "a list": b"[]", "no sheets key": b"{}",
                 "empty sheets": b'{"sheets": []}', "sheets not a list": b'{"sheets": 5}',
                 "unknown function": json.dumps(formula).encode("utf-8"), "nan": b'{"sheets": [NaN]}'}
        for label, data in cases.items():
            with self.subTest(file=label):
                path = os.path.join(self.tmp, "absent.json") if data is None else write_dashboard_file(
                    self.tmp, data, "case.json")
                code, out, err = self.run_pub("publish", "--dashboard", path, "--name", "Report", "--apply")
                self.assertEqual(code, 2, err)
                self.assertEqual(out, "")
                self.assertNoWrites()
        self.assertIn("NOPE", self.run_pub("publish", "--dashboard", write_dashboard_file(
            self.tmp, json.dumps(formula).encode("utf-8"), "f.json"), "--name", "R", "--apply")[2])

    def test_a_bad_dashboard_file_is_refused_in_a_dry_run_too(self):
        path = write_dashboard_file(self.tmp, b'{"sheets": []}', "empty.json")
        code, _, err = self.run_pub("publish", "--dashboard", path, "--name", "Report")
        self.assertEqual(code, 2, err)

    def test_the_file_is_sent_byte_for_byte(self):
        data = json.dumps({"sheets": [{"id": "s", "cells": {"A1": {"content": "café"}}}]}, indent=3).encode("utf-8")
        path = write_dashboard_file(self.tmp, data, "bytes.json")
        out = self.run_pub("publish", "--dashboard", path, "--name", "Bytes", "--apply")[1]
        self.assertEqual(base64.b64decode(self.world.dashboards[int(out)]["spreadsheet_binary_data"]), data)

    # ---- round 2 (security r1): the bytes sent are the bytes validated; Odoo reads `json.loads(bytes.decode())`
    BOM = b"\xef\xbb\xbf"

    def bom_bodies(self):
        accented = json.dumps({"sheets": [{"id": "s", "cells": {"A1": {"content": "café"}}}]}, ensure_ascii=False)
        return {"compact": DASHBOARD_BYTES,
                "indented with CRLF": json.dumps(json.loads(DASHBOARD_BYTES), indent=3).replace("\n", "\r\n").encode("utf-8"),
                "non ascii": accented.encode("utf-8")}

    def test_a_utf8_bom_file_is_stored_without_the_bom_and_still_valid_json(self):
        for label, body in self.bom_bodies().items():
            with self.subTest(file=label):
                path = write_dashboard_file(self.tmp, self.BOM + body, "bom.json")
                code, out, err = self.run_pub("publish", "--dashboard", path, "--name", "Bom " + label, "--apply")
                self.assertEqual(code, 0, err)
                stored = base64.b64decode(self.world.dashboards[int(out)]["spreadsheet_binary_data"])
                self.assertFalse(stored.startswith(self.BOM))
                self.assertTrue(stored.startswith(b"{"))
                self.assertEqual(stored, body)
                json.loads(stored.decode())                    # how Odoo reads it: no BOM allowed

    def test_a_bom_file_sent_with_replace_is_stored_without_the_bom_too(self):
        for label, body in self.bom_bodies().items():
            with self.subTest(file=label):
                path = write_dashboard_file(self.tmp, self.BOM + body, "bom.json")
                code, out, err = self.run_pub("publish", "--dashboard", path, "--replace", str(self.s.own),
                                              "--confirm-replace", str(self.s.own), "--apply")
                self.assertEqual(code, 0, err)
                stored = base64.b64decode(self.world.dashboards[self.s.own]["spreadsheet_binary_data"])
                self.assertEqual(stored, body)
                self.assertTrue(stored.startswith(b"{"))

    def test_a_bom_file_in_a_dry_run_writes_nothing(self):
        path = write_dashboard_file(self.tmp, self.BOM + DASHBOARD_BYTES, "bom.json")
        code, out, err = self.run_pub("publish", "--dashboard", path, "--name", "Bom")
        self.assertEqual((code, out), (0, ""), err)
        self.assertNoWrites()

    def test_a_bom_does_not_make_a_bad_file_acceptable(self):
        formula = json.dumps({"sheets": [{"id": "s", "cells": {"A1": {"content": "=NOPE.FN(1)"}}}]}).encode("utf-8")
        cases = {"bom only": self.BOM, "empty": b"", "bom and invalid json": self.BOM + b"{not json",
                 "bom and a list": self.BOM + b"[]", "bom and empty sheets": self.BOM + b'{"sheets": []}',
                 "bom and unknown function": self.BOM + formula, "two boms": self.BOM + self.BOM + DASHBOARD_BYTES,
                 "bom after a space": b" " + self.BOM + DASHBOARD_BYTES}
        for label, data in cases.items():
            with self.subTest(file=label):
                path = write_dashboard_file(self.tmp, data, "bad.json")
                code, out, err = self.run_pub("publish", "--dashboard", path, "--name", "Report", "--apply")
                self.assertEqual(code, 2, err)
                self.assertEqual(out, "")
                self.assertNoWrites()


# ---------------------------------------------------------------------------- publish (replace)

class ReplaceTests(PublishCase):
    def replace_argv(self, target, confirm, apply=True):
        argv = ["publish", "--dashboard", self.file, "--replace", str(target)]
        if confirm is not None:
            argv += ["--confirm-replace", str(confirm)]
        return argv + (["--apply"] if apply else [])

    def test_replace_needs_the_matching_confirmation(self):
        target = self.s.own
        for confirm in (None, target + 1, 1, 999):
            for apply in (True, False):
                with self.subTest(confirm=confirm, apply=apply):
                    code, out, err = self.run_pub(*self.replace_argv(target, confirm, apply))
                    self.assertEqual(code, 2, err)
                    self.assertEqual(out, "")
                    self.assertIn("needs --confirm-replace %d" % target, err)
                    self.assertNoWrites()

    def test_a_matching_confirmation_is_not_enough_without_apply(self):
        code, out, err = self.run_pub(*self.replace_argv(self.s.own, self.s.own, apply=False))
        self.assertEqual((code, out), (0, ""), err)
        self.assertIn("dry run", err)
        self.assertIn("will rewrite dashboard %d in place" % self.s.own, err)
        self.assertNoWrites()

    def test_a_matching_confirmation_with_apply_rewrites_once(self):
        code, out, err = self.run_pub(*self.replace_argv(self.s.own, self.s.own))
        self.assertEqual((code, out), (0, "%d\n" % self.s.own), err)
        self.assertEqual(len(self.fake.write_requests()), 1)

    def test_name_and_replace_together_or_neither_are_refused_before_the_network(self):
        both = ["publish", "--dashboard", self.file, "--name", "x", "--replace", str(self.s.own),
                "--confirm-replace", str(self.s.own), "--apply"]
        neither = ["publish", "--dashboard", self.file, "--apply"]
        for label, argv in (("both", both), ("neither", neither)):
            with self.subTest(case=label):
                code, out, err = self.run_pub(*argv)
                self.assertEqual(code, 2, err)
                self.assertIn("--name", err)
                self.assertEqual(self.fake.requests, [])

    def test_the_replace_does_not_create_or_rename_anything(self):
        before_names = {k: v["name"] for k, v in self.world.dashboards.items()}
        self.run_pub(*self.replace_argv(self.s.own, self.s.own))
        self.assertEqual({k: v["name"] for k, v in self.world.dashboards.items()}, before_names)
        self.assertEqual(self.fake.execute_calls(DASH, "create"), [])


# ---------------------------------------------------------------------------- list / archived

class ListTests(PublishCase):
    def test_list_shows_only_the_own_dashboards_archived_ones_included(self):
        code, out, err = self.run_pub("list")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["%d\tactive\tQuality v1" % self.s.own,
                                            "%d\tarchived\tQuality v2" % self.s.archived_own])
        self.assertNoWrites()

    def test_list_looks_up_the_dashboards_with_active_test_false(self):
        self.run_pub("list")
        call = [c for c in self.fake.sequence() if (c[0], c[1]) == (DASH, "search_read")][0]
        self.assertEqual(call[3]["context"], {"active_test": False})
        self.assertEqual(call[2][0], [["dashboard_group_id", "=", self.s.group]])

    def test_every_lookup_carries_active_test_false(self):
        # Plan step 9: archived own dashboards are found, `active_test=False` in the lookups
        self.run_pub("list")
        self.run_pub(*self.argv_for("archive", self.s.archived_own))
        seen = {}
        for model, method, args, kwargs in self.fake.sequence():
            seen.setdefault((model, method), []).append(kwargs)
        for key in (("spreadsheet.dashboard.group", "search"), (DASH, "read"), (DASH, "get_metadata"),
                    (DASH, "search_read")):
            with self.subTest(call=key):
                self.assertTrue(seen[key])
                for kwargs in seen[key]:
                    # round 2: the group lookup also pins the stored language (the name is a translated field)
                    wanted = {"active_test": False, "lang": "en_US"} if key[0].endswith(".group") else {"active_test": False}
                    self.assertEqual(kwargs["context"], wanted)

    def test_list_of_a_group_without_own_dashboards_is_empty(self):
        for target in self.s.own_ids:
            del self.world.dashboards[target]
        code, out, err = self.run_pub("list")
        self.assertEqual((code, out), (0, ""), err)

    def test_list_prints_one_line_per_dashboard_whatever_the_name_holds(self):
        self.world.add_dashboard("two\nlines\x07", self.s.group, creator=FAKE_UID)
        out = self.run_pub("list")[1]
        self.assertEqual(len(out.splitlines()), 3)
        self.assertIn("two lines?", out)

    def test_a_dashboard_that_became_foreign_disappears_from_the_list(self):
        self.world.dashboards[self.s.own]["xmlids"] = ["m.now_shipped"]
        out = self.run_pub("list")[1]
        self.assertNotIn("Quality v1", out)
        self.assertIn("Quality v2", out)

    def test_an_archived_own_dashboard_can_be_archived_or_deleted_again(self):
        self.assertEqual(self.run_pub(*self.argv_for("archive", self.s.archived_own))[0], 0)
        self.assertEqual(self.run_pub(*self.argv_for("delete-own", self.s.archived_own))[0], 0)
        self.assertNotIn(self.s.archived_own, self.world.dashboards)


if __name__ == "__main__":
    unittest.main()
