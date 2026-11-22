"""scripts/setup_instance.py against the fake Odoo (administrator tool: permission / ACL class).

The admin credential is a placeholder (`dashboard_fixtures.ADMIN_SECRET`); the fake never enforces ACL, so a script that
forgets a refusal is seen writing. Every test run is checked at cleanup: neither secret may appear in any stream.
"""
import io
import itertools
import os
import unittest
from unittest import mock

from dashboard_fixtures import (ADMIN_LOGIN, ADMIN_SECRET, ADMIN_UID, GROUP, MANAGER, ODOOBOT_LOGIN, ODOOBOT_UID,
                                PORTAL_UID, SYSTEM, TEMPLATE_LOGIN, TEMPLATE_UID, Scenario, StreamCase, start_fake,
                                use_admin_env)
from fake_odoo import FAKE_DB, FAKE_KEY, FAKE_UID, UNSET, DocumentsError

import setup_instance

DASH, DGROUP, USERS = "spreadsheet.dashboard", "spreadsheet.dashboard.group", "res.users"
MANAGER_ID = 2


class SetupCase(StreamCase):
    SECRETS = (ADMIN_SECRET, FAKE_KEY)

    def setUp(self):
        super().setUp()
        self.fake = start_fake(self, login=ADMIN_LOGIN, key=ADMIN_SECRET, uid=ADMIN_UID)
        self.s = Scenario(self.fake)
        self.world = self.s.world
        use_admin_env(self.fake.url)
        patcher = mock.patch("sys.stdin", io.StringIO())      # never a terminal: a missing value must not prompt
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_setup(self, *argv, **kwargs):
        return self.collect(setup_instance.main, argv, **kwargs)

    def assertNoWrites(self):
        self.assertEqual(self.fake.write_requests(), [])

    def changes(self, out):
        return [line for line in out.splitlines() if line.startswith("CHANGE:")]

    def groups_of(self, uid):
        return set(self.world.users[uid]["groups"])

    def named_groups(self):
        return sorted(g for g, rec in self.world.groups.items() if rec["name"] == GROUP)

    def new_user(self, uid, login, **kwargs):
        return self.world.add_user(uid, login, **kwargs)


ALL_COMMANDS = (["status"], ["init", "--apply"], ["add-user", "plain.user", "--apply"],
                ["remove-user", "colleague.qa", "--apply"], ["list-orphans"], ["remove-group", "--apply"])


# ---------------------------------------------------------------------------- the administrator gate

class AdminGateTests(SetupCase):
    def test_a_system_administrator_who_is_a_dashboard_manager_is_accepted(self):
        for argv in (["status"], ["list-orphans"]):
            with self.subTest(command=argv[0]):
                self.assertEqual(self.run_setup(*argv)[0], 0)

    def test_the_two_rights_are_asked_with_the_real_has_group_shape_before_anything_else(self):
        # round 2 (security r1, orange finding): `res.users.has_group` is @api.model, `args == [xmlid]` and the server
        # evaluates it for the AUTHENTICATED uid; the round-1 test pinned the shape the real server rejects ([[uid], xmlid])
        self.run_setup("status")
        first = self.fake.sequence()[:2]
        self.assertEqual(first, [(USERS, "has_group", [SYSTEM], {}), (USERS, "has_group", [MANAGER], {})])

    def test_the_rights_checked_are_those_of_the_authenticated_login_not_of_someone_else(self):
        # the only user that can answer is the authenticated uid: a QA/QC-shaped login that is a manager but not an
        # administrator is refused although ANOTHER user (the admin) holds both rights
        fake = start_fake(self, login="qa.user", key=FAKE_KEY, uid=FAKE_UID)
        Scenario(fake)
        use_admin_env(fake.url, login="qa.user", secret=FAKE_KEY)
        code, out, err = self.run_setup("status")
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("Odoo administrator", err)
        self.assertEqual([(m, meth, a) for m, meth, a, _ in fake.sequence()], [(USERS, "has_group", [SYSTEM])])

    def test_the_old_has_group_shape_with_ids_is_not_what_the_script_sends(self):
        self.run_setup("status")
        for model, method, args, kwargs in self.fake.sequence():
            if (model, method) == (USERS, "has_group"):
                self.assertEqual(len(args), 1)
                self.assertIsInstance(args[0], str)

    def test_a_login_missing_either_right_is_refused_by_every_command_and_nothing_is_written(self):
        combos = {"not system": (SYSTEM, "Odoo administrator"), "not manager": (MANAGER, "dashboard manager")}
        for (label, (group, words)), argv in itertools.product(combos.items(), ALL_COMMANDS):
            with self.subTest(case=label, command=argv[0]):
                self.fake.requests.clear()
                self.world.users[ADMIN_UID]["groups"] = {self.world.res_groups[g] for g in (SYSTEM, MANAGER) if g != group}
                code, out, err = self.run_setup(*argv)
                self.assertEqual(code, 2, err)
                self.assertEqual(out, "")
                self.assertIn("this tool is for the Odoo administrator only", err)
                self.assertIn(words, err)
                self.assertNoWrites()
                self.assertEqual({(m, meth) for m, meth, _, _ in self.fake.sequence()}, {(USERS, "has_group")})

    def test_a_login_with_neither_right_is_refused(self):
        self.world.users[ADMIN_UID]["groups"] = set()
        for argv in ALL_COMMANDS:
            with self.subTest(command=argv[0]):
                code, out, err = self.run_setup(*argv)
                self.assertEqual((code, out), (2, ""), err)
        self.assertNoWrites()

    def test_only_the_boolean_true_passes_the_gate(self):
        for xmlid in (SYSTEM, MANAGER):
            for label, raw in (("1", 1), ("text true", "true"), ("text yes", "yes"), ("None", None), ("empty list", []),
                               ("dict", {"a": 1}), ("2", 2), ("text 1", "1"), ("float", 1.0), ("list True", [True])):
                for argv in (["status"], ["init", "--apply"]):
                    with self.subTest(xmlid=xmlid, answer=label, command=argv[0]):
                        self.fake.requests.clear()
                        self.world.has_group_override = {xmlid: raw}
                        code, out, err = self.run_setup(*argv)
                        self.assertEqual(code, 2, err)
                        self.assertNoWrites()
            self.world.has_group_override = {}

    def test_an_odoo_error_on_the_role_check_stops_everything(self):
        for xmlid in (SYSTEM, MANAGER):
            with self.subTest(xmlid=xmlid):
                self.fake.requests.clear()
                self.world.has_group_override = {xmlid: DocumentsError("cannot ask")}
                code, out, err = self.run_setup("init", "--apply")
                self.assertEqual((code, out), (1, ""), err)
                self.assertNoWrites()

    def test_a_qaqc_credential_is_refused_even_though_it_is_a_dashboard_manager(self):
        fake = start_fake(self, login="qa.user", key=FAKE_KEY, uid=FAKE_UID)
        scenario = Scenario(fake)
        self.assertIn(MANAGER_ID, scenario.world.users[FAKE_UID]["groups"])
        use_admin_env(fake.url, login="qa.user", secret=FAKE_KEY)
        for argv in ALL_COMMANDS:
            with self.subTest(command=argv[0]):
                code, out, err = self.run_setup(*argv)
                self.assertEqual((code, out), (2, ""), err)
                self.assertIn("Odoo administrator", err)
        self.assertEqual(fake.write_requests(), [])
        self.assertEqual({(m, meth) for m, meth, _, _ in fake.sequence()}, {(USERS, "has_group")})

    def test_a_failed_login_is_exit_1_and_nothing_else_is_asked(self):
        use_admin_env(self.fake.url, secret="WRONG-SECRET-not-the-real-one")  # secret-allow: test placeholder
        code, out, err = self.run_setup("init", "--apply")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("login failed", err)
        self.assertEqual(self.fake.sequence(), [])


# ---------------------------------------------------------------------------- status

class StatusTests(SetupCase):
    def test_status_reports_the_group_and_the_manager_count_and_writes_nothing(self):
        code, out, err = self.run_setup("status")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["group 'KPI (QA/QC)': id %d" % self.s.group,
                                            "users in the dashboard manager group: 3"])
        self.assertNoWrites()

    def test_status_of_an_instance_without_the_group(self):
        del self.world.groups[self.s.group]
        out = self.run_setup("status")[1]
        self.assertIn("group 'KPI (QA/QC)': missing", out)

    def test_status_refuses_an_ambiguous_group(self):
        self.world.add_group(GROUP)
        code, out, err = self.run_setup("status")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("2 dashboard groups are named", err)

    def test_the_manager_count_is_asked_for_exactly_the_manager_group(self):
        self.run_setup("status")
        call = [c for c in self.fake.sequence() if (c[0], c[1]) == (USERS, "search_count")][0]
        self.assertEqual(call[2], [[["groups_id", "in", [MANAGER_ID]]]])


# ---------------------------------------------------------------------------- init

class InitTests(SetupCase):
    def setUp(self):
        super().setUp()
        del self.world.groups[self.s.group]            # an instance where nobody ran init yet
        self.world.dashboards = {k: v for k, v in self.world.dashboards.items() if v["dashboard_group_id"] != self.s.group}

    def test_init_without_apply_creates_nothing(self):
        code, out, err = self.run_setup("init")
        self.assertEqual(code, 0, err)
        self.assertIn("dry run", out)
        self.assertEqual(self.changes(out), [])
        self.assertNoWrites()
        self.assertEqual(self.named_groups(), [])

    def test_init_with_apply_creates_the_one_group_and_prints_one_change_line(self):
        code, out, err = self.run_setup("init", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.write_requests(), [(DGROUP, "create", [{"name": GROUP}], {})])
        created = self.named_groups()
        self.assertEqual(len(created), 1)
        self.assertEqual(out.splitlines(), ["CHANGE: created dashboard group 'KPI (QA/QC)' id %d" % created[0]])

    def test_init_is_idempotent_a_second_run_creates_nothing_and_prints_no_change(self):
        self.run_setup("init", "--apply")
        self.fake.requests.clear()
        for _ in range(2):
            code, out, err = self.run_setup("init", "--apply")
            self.assertEqual(code, 0, err)
            self.assertEqual(self.changes(out), [])
            self.assertIn("already exists", out)
            self.assertIn("nothing to do", out)
            self.assertNoWrites()
        self.assertEqual(len(self.named_groups()), 1)

    def test_init_does_not_create_next_to_an_existing_group(self):
        existing = self.world.add_group(GROUP)
        code, out, err = self.run_setup("init", "--apply")
        self.assertEqual(code, 0, err)
        self.assertIn("id %d" % existing, out)
        self.assertNoWrites()

    def test_init_refuses_duplicates_and_creates_nothing(self):
        self.world.add_group(GROUP)
        self.world.add_group(GROUP)
        code, out, err = self.run_setup("init", "--apply")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("2 dashboard groups are named", err)
        self.assertNoWrites()

    def test_init_creates_the_exact_group_next_to_lookalikes(self):
        for name in ("kpi (qa/qc)", "KPI (QA/QC) ", "KPI"):
            self.world.add_group(name)
        before = self.world.snapshot()
        code, out, err = self.run_setup("init", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.named_groups()), 1)
        self.assertEqual(len(self.world.groups), len(before[1]) + 1)
        self.assertEqual(self.fake.write_requests(), [(DGROUP, "create", [{"name": GROUP}], {})])

    def test_init_looks_the_group_up_by_exact_name_with_archived_ones_included(self):
        self.run_setup("init")
        call = [c for c in self.fake.sequence() if (c[0], c[1]) == (DGROUP, "search")][0]
        self.assertEqual(call[2], [[["name", "=", GROUP]]])
        # round 2: the group name is a translated field, so the lookup pins the stored language (security r1, finding 4)
        self.assertEqual(call[3], {"context": {"active_test": False, "lang": "en_US"}})

    def test_a_malformed_group_lookup_refuses_and_creates_nothing(self):
        for label, raw in (("None", None), ("text", "5"), ("dict", {"a": 1}), ("bool id", [True]), ("text id", ["7"]),
                           ("float id", [1.5]), ("None id", [None])):
            with self.subTest(answer=label):
                self.world.group_search_override = raw
                code, out, err = self.run_setup("init", "--apply")
                self.assertEqual(code, 2, err)
                self.assertIn("unexpected shape", err)
                self.assertNoWrites()

    def test_init_touches_nothing_but_the_group_model(self):
        self.run_setup("init", "--apply")
        self.assertEqual([c for c in self.fake.write_requests() if c[0] != DGROUP], [])

    def test_a_create_answer_that_is_no_positive_id_is_refused_and_prints_no_change_line(self):
        # round 2 (security r1): the id of the new group is printed in the CHANGE line the revert relies on
        for label, raw in (("None", None), ("False", False), ("True", True), ("zero", 0), ("negative", -4),
                           ("text", "12"), ("float", 12.0), ("empty list", []), ("dict", {"id": 12}), ("empty text", "")):
            with self.subTest(answer=label):
                for gid in self.named_groups():                  # the group the previous subtest made is gone again
                    del self.world.groups[gid]
                self.fake.requests.clear()
                self.world.group_create_answer = raw
                code, out, err = self.run_setup("init", "--apply")
                self.assertEqual((code, out), (2, ""), err)
                self.assertIn("did not return the id", err)
                self.assertEqual(len(self.fake.write_requests()), 1)             # the create WAS sent: the refusal is ours
                self.assertEqual(self.changes(out), [])

    def test_a_positive_integer_create_answer_is_printed_as_the_id(self):
        self.world.group_create_answer = 4242
        code, out, err = self.run_setup("init", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["CHANGE: created dashboard group 'KPI (QA/QC)' id 4242"])


# ---------------------------------------------------------------------------- add-user / remove-user

class MembershipTests(SetupCase):
    def setUp(self):
        super().setUp()
        self.new_user(12, "newbie.qa", groups=("base.group_user",))
        self.new_user(13, "second.qa", groups=("base.group_user",))

    def snapshot_others(self, skip):
        return {uid: set(rec["groups"]) for uid, rec in self.world.users.items() if uid not in skip}

    def test_add_user_writes_exactly_the_manager_membership_of_the_named_login(self):
        before = self.snapshot_others({12})
        code, out, err = self.run_setup("add-user", "newbie.qa", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.write_requests(), [(USERS, "write", [[12], {"groups_id": [[4, MANAGER_ID]]}], {})])
        self.assertEqual(self.groups_of(12), {1, MANAGER_ID})
        self.assertEqual(self.snapshot_others({12}), before)
        self.assertEqual(out.splitlines(), ["CHANGE: added newbie.qa (id 12) to the dashboard manager group (id 2)"])

    def test_remove_user_writes_exactly_the_manager_removal_of_the_named_login(self):
        before = self.snapshot_others({FAKE_UID})
        code, out, err = self.run_setup("remove-user", "qa.user", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.write_requests(),
                         [(USERS, "write", [[FAKE_UID], {"groups_id": [[3, MANAGER_ID]]}], {})])
        self.assertEqual(self.groups_of(FAKE_UID), {1})
        self.assertEqual(self.snapshot_others({FAKE_UID}), before)
        self.assertEqual(out.splitlines(),
                         ["CHANGE: removed qa.user (id %d) from the dashboard manager group (id 2)" % FAKE_UID])

    def test_several_logins_give_one_write_and_one_change_line_each_in_login_order(self):
        code, out, err = self.run_setup("add-user", "second.qa", "newbie.qa", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual([c[2][0] for c in self.fake.write_requests()], [[12], [13]])
        lines = self.changes(out)
        self.assertEqual(len(lines), 2)
        self.assertIn("newbie.qa (id 12)", lines[0])
        self.assertIn("second.qa (id 13)", lines[1])
        self.assertEqual(len(out.splitlines()), 2)

    def test_without_apply_nothing_is_written_and_no_change_line_is_printed(self):
        before = self.world.snapshot()
        for argv, verb in ((["add-user", "newbie.qa"], "would add newbie.qa to"),
                           (["remove-user", "qa.user"], "would remove qa.user from")):
            with self.subTest(command=argv[0]):
                code, out, err = self.run_setup(*argv)
                self.assertEqual(code, 0, err)
                self.assertIn("dry run: " + verb, out)
                self.assertEqual(self.changes(out), [])
                self.assertNoWrites()
                self.assertEqual(self.world.snapshot(), before)

    def test_the_manager_group_is_resolved_from_the_server_not_assumed(self):
        self.world.res_groups[MANAGER] = 55
        self.world.users[ADMIN_UID]["groups"] = {55, 3}
        self.run_setup("add-user", "newbie.qa", "--apply")
        self.assertEqual(self.fake.write_requests()[0][2][1], {"groups_id": [[4, 55]]})
        self.assertEqual(self.fake.execute_calls("ir.model.data", "check_object_reference")[-1],
                         ["spreadsheet_dashboard", "group_dashboard_manager"])

    def test_a_bad_manager_group_answer_stops_before_any_write(self):
        for label, raw in (("None", None), ("empty", []), ("one item", ["res.groups"]), ("zero id", ["res.groups", 0]),
                           ("bool id", ["res.groups", True]), ("text id", ["res.groups", "2"]),
                           ("negative", ["res.groups", -2]), ("three items", ["res.groups", 2, 3]), ("dict", {"a": 1})):
            for argv in (["add-user", "newbie.qa", "--apply"], ["remove-user", "qa.user", "--apply"], ["status"]):
                with self.subTest(answer=label, command=argv[0]):
                    self.world.ref_override[MANAGER] = raw
                    code, out, err = self.run_setup(*argv)
                    self.assertEqual(code, 2, err)
                    self.assertIn("cannot resolve the dashboard manager group", err)
                    self.assertNoWrites()

    def test_an_unknown_login_refuses_and_writes_nothing_even_for_the_valid_ones(self):
        # round 3 (archived users): the message of add-user says `active user` (it looks only at ACTIVE users), the one
        # of remove-user does not (it also finds archived ones)
        for argv, words in ((["add-user", "nobody.here", "--apply"], "no single active user with the login"),
                            (["add-user", "newbie.qa", "nobody.here", "--apply"], "no single active user with the login"),
                            (["remove-user", "nobody.here", "qa.user", "--apply"], "no single user with the login"),
                            (["add-user", "", "--apply"], "no single active user with the login")):
            with self.subTest(argv=argv):
                code, out, err = self.run_setup(*argv)
                self.assertEqual((code, out), (2, ""), err)
                self.assertIn(words, err)
                self.assertEqual("active user" in err, argv[0] == "add-user")
                self.assertNoWrites()

    def test_a_malformed_user_lookup_refuses_and_writes_nothing(self):
        row = {"id": 12, "login": "newbie.qa", "share": False}
        for label, raw in (("None", None), ("text", "x"), ("dict", {}), ("empty", []), ("list of int", [1]),
                           ("two rows", [row, dict(row, id=13)]), ("row is text", ["x"])):
            for argv, words in ((["add-user", "newbie.qa", "--apply"], "no single active user with the login"),
                                (["remove-user", "newbie.qa", "--apply"], "no single user with the login")):
                with self.subTest(answer=label, command=argv[0]):
                    self.world.users_search_override = raw
                    code, out, err = self.run_setup(*argv)
                    self.assertEqual((code, out), (2, ""), err)
                    self.assertIn(words, err)
                    self.assertEqual("active user" in err, argv[0] == "add-user")
                    self.assertNoWrites()

    def test_the_login_must_match_exactly(self):
        self.new_user(14, "alice")
        for typed in ("ALICE", "Alice", "alic", "alice ", "ali%"):
            with self.subTest(typed=typed):
                code, out, err = self.run_setup("add-user", typed, "--apply")
                self.assertEqual(code, 2, err)
                self.assertNoWrites()
        self.assertEqual(self.run_setup("add-user", "alice", "--apply")[0], 0)
        self.assertEqual(self.groups_of(14), {MANAGER_ID})

    def test_the_login_lookup_is_an_equality_on_login_and_only_remove_user_includes_archived_users(self):
        # round 3 (security r2): add-user looks users up WITHOUT `active_test: False` (an empty / absent context), only
        # remove-user keeps archived users findable (revoking a leaver's right)
        for argv, expected_kwargs in ((["add-user", "newbie.qa"], {}),
                                      (["remove-user", "newbie.qa"], {"context": {"active_test": False}})):
            with self.subTest(command=argv[0]):
                self.fake.requests.clear()
                self.run_setup(*argv)
                calls = [c for c in self.fake.sequence() if (c[0], c[1]) == (USERS, "search_read")]
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][2], [[["login", "=", "newbie.qa"]], ["login", "share"]])
                self.assertEqual(calls[0][3], expected_kwargs)

    def test_every_login_of_one_command_is_looked_up_with_the_context_of_that_command(self):
        for argv, expected_kwargs in ((["add-user", "newbie.qa", "second.qa", "plain.user"], {}),
                                      (["remove-user", "newbie.qa", "second.qa", "plain.user"],
                                       {"context": {"active_test": False}})):
            with self.subTest(command=argv[0]):
                self.fake.requests.clear()
                self.run_setup(*argv)
                calls = [c for c in self.fake.sequence() if (c[0], c[1]) == (USERS, "search_read")]
                self.assertEqual([c[2][0][0][2] for c in calls], ["newbie.qa", "second.qa", "plain.user"])
                self.assertEqual([c[3] for c in calls], [expected_kwargs] * 3)

    def test_two_users_with_the_same_login_are_ambiguous_and_refused(self):
        self.new_user(14, "twin.qa")
        self.new_user(15, "twin.qa")
        code, out, err = self.run_setup("add-user", "twin.qa", "--apply")
        self.assertEqual(code, 2, err)
        self.assertNoWrites()

    def test_a_portal_user_is_refused_and_nothing_is_written(self):
        for argv in (["add-user", "portal.user", "--apply"], ["add-user", "newbie.qa", "portal.user", "--apply"],
                     ["remove-user", "portal.user", "--apply"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_setup(*argv)
                self.assertEqual((code, out), (2, ""), err)
                self.assertIn("portal / external user", err)
                self.assertNoWrites()
        self.assertNotIn(MANAGER_ID, self.groups_of(PORTAL_UID))

    def test_duplicate_logins_in_one_command_are_refused(self):
        code, out, err = self.run_setup("add-user", "newbie.qa", "newbie.qa", "--apply")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("between 1 and 50 different logins", err)
        self.assertNoWrites()

    def test_at_most_fifty_logins_per_command(self):
        logins = []
        for index in range(51):
            self.new_user(100 + index, "bulk%02d.qa" % index, groups=("base.group_user",))
            logins.append("bulk%02d.qa" % index)
        code, out, err = self.run_setup("add-user", *logins, "--apply")
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("between 1 and 50 different logins", err)
        self.assertNoWrites()
        code, out, err = self.run_setup("add-user", *logins[:50], "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.changes(out)), 50)
        self.assertEqual(len(self.fake.write_requests()), 50)

    def test_membership_commands_write_only_to_res_users(self):
        self.run_setup("add-user", "newbie.qa", "--apply")
        self.run_setup("remove-user", "newbie.qa", "--apply")
        self.assertEqual([c for c in self.fake.write_requests() if c[0] != USERS], [])
        self.assertEqual(self.groups_of(12), {1})

    def test_adding_twice_is_harmless_for_the_membership(self):
        self.run_setup("add-user", "newbie.qa", "--apply")
        self.run_setup("add-user", "newbie.qa", "--apply")
        self.assertEqual(self.groups_of(12), {1, MANAGER_ID})


class ArchivedUserTests(SetupCase):
    """Round 3 (security r2, finding 1): `add-user` never grants the right to an ARCHIVED user. Odoo's template user
    `default` is archived + internal (share False): a group linked on it is copied to EVERY active internal user and to
    every user created later. `remove-user` still finds archived users (revoking a leaver's right is legitimate)."""

    LEAVER_UID = 20

    def setUp(self):
        super().setUp()
        self.new_user(12, "newbie.qa", groups=("base.group_user",))
        self.new_user(self.LEAVER_UID, "leaver.qa", active=False, groups=("base.group_user", MANAGER))
        self.new_user(21, "dormant.qa", active=False, groups=("base.group_user",))

    def admin_client(self):
        rpc = setup_instance.odoo_rpc
        return rpc.Client(rpc.Config(self.fake.url, FAKE_DB, ADMIN_LOGIN, ADMIN_SECRET, "admin"))

    def test_users_by_login_does_not_find_archived_users_unless_the_caller_says_so(self):
        # the safe default: a future caller that forgets the argument cannot grant to an archived user
        client = self.admin_client()
        with self.assertRaises(setup_instance.SetupError) as caught:
            setup_instance.users_by_login(client, [TEMPLATE_LOGIN])
        self.assertIn("no single active user with the login 'default'", str(caught.exception))
        with self.assertRaises(setup_instance.SetupError):
            setup_instance.users_by_login(client, [TEMPLATE_LOGIN], allow_archived=False)
        self.assertEqual(setup_instance.users_by_login(client, [TEMPLATE_LOGIN], allow_archived=True),
                         {TEMPLATE_LOGIN: TEMPLATE_UID})
        self.assertEqual(setup_instance.users_by_login(client, ["newbie.qa"]), {"newbie.qa": 12})
        self.assertEqual(setup_instance.users_by_login(client, ["newbie.qa"], allow_archived=True), {"newbie.qa": 12})

    def test_the_scenario_models_the_template_user_as_odoo_has_it(self):
        template = self.world.users[TEMPLATE_UID]
        self.assertEqual((template["login"], template["active"], template["share"], template["template"]),
                         (TEMPLATE_LOGIN, False, False, True))
        self.assertIn(1, template["groups"])                               # base.group_user: an INTERNAL user
        self.assertEqual(self.world.users[ODOOBOT_UID]["login"], ODOOBOT_LOGIN)
        self.assertFalse(self.world.users[ODOOBOT_UID]["active"])

    def test_a_write_on_the_template_user_reaches_every_active_internal_user_in_the_fake(self):
        # fidelity pin of the fake: this is the harm the refusal prevents (without it the tests below could not tell)
        others = [uid for uid, rec in self.world.users.items()
                  if uid != TEMPLATE_UID and rec["active"] and not rec["share"]]
        self.assertGreater(len(others), 3)
        self.admin_client().execute(USERS, "write", [[TEMPLATE_UID], {"groups_id": [[4, MANAGER_ID]]}])
        for uid in others:
            self.assertIn(MANAGER_ID, self.groups_of(uid), uid)
        self.assertNotIn(MANAGER_ID, self.groups_of(PORTAL_UID))              # a portal user is not internal
        self.assertNotIn(MANAGER_ID, self.groups_of(21))                      # an archived one is not touched

    def test_add_user_refuses_the_template_user_and_every_other_archived_user_and_writes_nothing(self):
        for login in (TEMPLATE_LOGIN, ODOOBOT_LOGIN, "dormant.qa", "leaver.qa"):
            for apply in (["--apply"], []):
                with self.subTest(login=login, apply=apply):
                    before = self.world.snapshot()
                    code, out, err = self.run_setup("add-user", login, *apply)
                    self.assertEqual((code, out), (2, ""), err)
                    self.assertIn("no single active user with the login %r" % login, err)
                    self.assertNoWrites()
                    self.assertEqual(self.world.snapshot(), before)          # nobody gained the manager right

    def test_the_template_user_in_a_list_stops_the_whole_command_before_any_write(self):
        for argv in (["add-user", "newbie.qa", TEMPLATE_LOGIN, "--apply"],
                     ["add-user", TEMPLATE_LOGIN, "newbie.qa", "--apply"],
                     ["add-user", "newbie.qa", "plain.user", ODOOBOT_LOGIN, "--apply"]):
            with self.subTest(argv=argv):
                before = self.world.snapshot()
                code, out, err = self.run_setup(*argv)
                self.assertEqual((code, out), (2, ""), err)
                self.assertIn("no single active user with the login", err)
                self.assertNoWrites()
                self.assertEqual(self.world.snapshot(), before)

    def test_remove_user_finds_an_archived_leaver_and_revokes_the_right(self):
        code, out, err = self.run_setup("remove-user", "leaver.qa", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.write_requests(),
                         [(USERS, "write", [[self.LEAVER_UID], {"groups_id": [[3, MANAGER_ID]]}], {})])
        self.assertEqual(out.splitlines(),
                         ["CHANGE: removed leaver.qa (id %d) from the dashboard manager group (id 2)" % self.LEAVER_UID])
        self.assertNotIn(MANAGER_ID, self.groups_of(self.LEAVER_UID))

    def test_remove_user_dry_run_names_the_archived_leaver(self):
        code, out, err = self.run_setup("remove-user", "leaver.qa")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["dry run: would remove leaver.qa from the dashboard manager group"])
        self.assertNoWrites()

    def test_remove_user_on_archived_users_without_the_right_is_a_no_op_not_an_error(self):
        for login in (TEMPLATE_LOGIN, "dormant.qa"):
            with self.subTest(login=login):
                code, out, err = self.run_setup("remove-user", login, "--apply")
                self.assertEqual(code, 0, err)
                self.assertEqual(out.splitlines(),
                                 ["%s is already outside the dashboard manager group: no change" % login])
                self.assertNoWrites()

    def test_remove_user_mixes_archived_and_active_users_in_login_order(self):
        code, out, err = self.run_setup("remove-user", "qa.user", "leaver.qa", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual([c[2][0] for c in self.fake.write_requests()], [[self.LEAVER_UID], [FAKE_UID]])
        self.assertEqual(out.splitlines(), [
            "CHANGE: removed leaver.qa (id %d) from the dashboard manager group (id 2)" % self.LEAVER_UID,
            "CHANGE: removed qa.user (id %d) from the dashboard manager group (id 2)" % FAKE_UID])

    def test_an_archived_user_among_active_ones_refuses_the_whole_command_and_the_active_one_is_not_written(self):
        code, out, err = self.run_setup("add-user", "leaver.qa", "newbie.qa", "--apply")
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("no single active user with the login 'leaver.qa'", err)
        self.assertNoWrites()
        self.assertEqual(self.groups_of(12), {1})


class UserRowShapeTests(SetupCase):
    """`users_by_login` fails closed on what the server says about a user (security r1, finding: fail-open defaults)."""

    def setUp(self):
        super().setUp()
        self.new_user(12, "newbie.qa", groups=("base.group_user",))

    def run_both(self, row):
        results = []
        for argv in (["add-user", "newbie.qa", "--apply"], ["remove-user", "newbie.qa", "--apply"]):
            self.world.users_search_override = [row]
            results.append((argv[0],) + self.run_setup(*argv))
        return results

    def test_only_a_user_whose_share_is_exactly_false_is_internal(self):
        base = {"id": 12, "login": "newbie.qa"}
        cases = (("missing", base), ("None", dict(base, share=None)), ("zero", dict(base, share=0)),
                 ("empty text", dict(base, share="")), ("text false", dict(base, share="false")),
                 ("True", dict(base, share=True)), ("one", dict(base, share=1)), ("empty list", dict(base, share=[])))
        for label, row in cases:
            with self.subTest(share=label):
                for command, code, out, err in self.run_both(row):
                    self.assertEqual((code, out), (2, ""), command + ": " + err)
                    self.assertIn("portal / external user", err)
                self.assertNoWrites()

    def test_a_row_with_share_false_is_accepted(self):
        self.world.users_search_override = [{"id": 12, "login": "newbie.qa", "share": False}]
        code, out, err = self.run_setup("add-user", "newbie.qa", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.write_requests(), [(USERS, "write", [[12], {"groups_id": [[4, MANAGER_ID]]}], {})])

    def test_the_user_id_must_be_a_positive_integer(self):
        good = {"login": "newbie.qa", "share": False}
        cases = (("missing", good), ("None", dict(good, id=None)), ("True", dict(good, id=True)),
                 ("False", dict(good, id=False)), ("zero", dict(good, id=0)), ("negative", dict(good, id=-3)),
                 ("text", dict(good, id="12")), ("float", dict(good, id=12.0)), ("list", dict(good, id=[12])),
                 ("dict", dict(good, id={"id": 12})))
        for label, row in cases:
            with self.subTest(id=label):
                for command, code, out, err in self.run_both(row):
                    self.assertEqual((code, out), (2, ""), command + ": " + err)
                    self.assertIn("unexpected shape", err)
                self.assertNoWrites()


class MembershipStateTests(SetupCase):
    """add-user / remove-user read every named user's groups first, skip a no-op, report side effects of the write."""

    def setUp(self):
        super().setUp()
        self.new_user(12, "newbie.qa", groups=("base.group_user",))
        self.new_user(13, "second.qa", groups=("base.group_user",))

    def pairs(self):
        return [(m, meth) for m, meth, _, _ in self.fake.sequence()]

    def test_the_groups_of_every_named_user_are_read_before_the_first_write_and_again_after_each_write(self):
        code, out, err = self.run_setup("add-user", "second.qa", "newbie.qa", "--apply")
        self.assertEqual(code, 0, err)
        sequence = self.fake.sequence()
        first_write = self.pairs().index((USERS, "write"))
        pre = [c for c in sequence[:first_write] if (c[0], c[1]) == (USERS, "read")]
        self.assertEqual(sorted(c[2][0][0] for c in pre), [12, 13])
        for call in pre:
            self.assertEqual((call[2][1], call[3]), (["groups_id"], {"context": {"active_test": False}}))
        tail = [(meth, args[0]) for _, meth, args, _ in sequence[first_write:]]
        self.assertEqual(tail, [("write", [12]), ("read", [12]), ("write", [13]), ("read", [13])])

    def test_a_malformed_groups_read_refuses_and_writes_nothing_even_for_the_other_users(self):
        good = [{"id": 13, "groups_id": [1]}]
        cases = (("None", None), ("empty list", []), ("text", "x"), ("dict", {}), ("row is text", ["x"]),
                 ("no groups_id key", [{"id": 13}]), ("groups None", [{"id": 13, "groups_id": None}]),
                 ("groups text", [{"id": 13, "groups_id": "2"}]), ("groups hold a bool", [{"id": 13, "groups_id": [True]}]),
                 ("groups hold text", [{"id": 13, "groups_id": ["2"]}]), ("groups hold a float", [{"id": 13, "groups_id": [1.5]}]),
                 ("groups hold None", [{"id": 13, "groups_id": [None]}]), ("two rows", good + [{"id": 14, "groups_id": [1]}]))
        for label, raw in cases:
            for command in ("add-user", "remove-user"):
                with self.subTest(answer=label, command=command):
                    # the malformed answer is for the SECOND user (13): the first one (12) must not be written either
                    self.world.users_read_override = lambda n, ids, fields, raw=raw: raw if ids == [13] else UNSET
                    code, out, err = self.run_setup(command, "newbie.qa", "second.qa", "--apply")
                    self.assertEqual((code, out), (2, ""), err)
                    self.assertIn("cannot read the groups of user id 13", err)
                    self.assertNoWrites()
        self.assertEqual((self.groups_of(12), self.groups_of(13)), ({1}, {1}))

    def test_an_odoo_error_on_the_groups_read_writes_nothing(self):
        self.world.users_read_override = DocumentsError("cannot read the groups")
        code, out, err = self.run_setup("add-user", "newbie.qa", "--apply")
        self.assertEqual((code, out), (1, ""), err)
        self.assertNoWrites()

    def test_a_user_already_in_the_target_state_gets_no_write_and_no_change_line(self):
        for command, login, word in (("add-user", "qa.user", "in"), ("remove-user", "newbie.qa", "outside")):
            for apply in (["--apply"], []):
                with self.subTest(command=command, apply=apply):
                    code, out, err = self.run_setup(command, login, *apply)
                    self.assertEqual(code, 0, err)
                    self.assertEqual(out.splitlines(),
                                     ["%s is already %s the dashboard manager group: no change" % (login, word)])
                    self.assertNoWrites()

    def test_only_the_users_that_change_are_written_and_the_others_are_named_in_login_order(self):
        code, out, err = self.run_setup("add-user", "second.qa", "qa.user", "newbie.qa", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual([c[2][0] for c in self.fake.write_requests()], [[12], [13]])
        self.assertEqual(out.splitlines(), ["CHANGE: added newbie.qa (id 12) to the dashboard manager group (id 2)",
                                            "qa.user is already in the dashboard manager group: no change",
                                            "CHANGE: added second.qa (id 13) to the dashboard manager group (id 2)"])
        self.fake.requests.clear()
        code, out, err = self.run_setup("remove-user", "qa.user", "plain.user", "colleague.qa", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual([c[2][0] for c in self.fake.write_requests()], [[8], [FAKE_UID]])
        self.assertEqual(out.splitlines(), [
            "CHANGE: removed colleague.qa (id 8) from the dashboard manager group (id 2)",
            "plain.user is already outside the dashboard manager group: no change",
            "CHANGE: removed qa.user (id %d) from the dashboard manager group (id 2)" % FAKE_UID])

    def test_a_dry_run_names_a_no_op_as_no_change_not_as_a_planned_write(self):
        code, out, err = self.run_setup("add-user", "qa.user", "newbie.qa")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["dry run: would add newbie.qa to the dashboard manager group",
                                            "qa.user is already in the dashboard manager group: no change"])
        self.assertNoWrites()

    def test_a_side_effect_of_the_write_on_other_groups_is_printed_one_line_per_group(self):
        self.world.write_side_effect_groups = {40: True, 41: True}
        code, out, err = self.run_setup("add-user", "newbie.qa", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), [
            "CHANGE: added newbie.qa (id 12) to the dashboard manager group (id 2)",
            "CHANGE: side effect on newbie.qa (id 12): group id 40 added",
            "CHANGE: side effect on newbie.qa (id 12): group id 41 added"])
        self.assertEqual(self.groups_of(12), {1, MANAGER_ID, 40, 41})

    def test_the_side_effect_lines_are_in_ascending_group_id_order(self):
        # ids chosen so that a plain `set` iterates them out of order (16 hashes before 9 in a small table)
        self.world.write_side_effect_groups = {16: True, 9: True, 33: True}
        code, out, err = self.run_setup("add-user", "newbie.qa", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual([line.rsplit(" ", 2)[-2] for line in out.splitlines()[1:]], ["9", "16", "33"])

    def test_a_removed_group_is_reported_as_removed_and_an_unchanged_one_is_not_reported(self):
        self.world.users[FAKE_UID]["groups"] |= {41, 42}
        self.world.write_side_effect_groups = {41: False, 42: True, 43: True}      # 42 already held: no change
        code, out, err = self.run_setup("remove-user", "qa.user", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), [
            "CHANGE: removed qa.user (id %d) from the dashboard manager group (id 2)" % FAKE_UID,
            "CHANGE: side effect on qa.user (id %d): group id 41 removed" % FAKE_UID,
            "CHANGE: side effect on qa.user (id %d): group id 43 added" % FAKE_UID])

    def test_the_manager_group_itself_is_not_reported_as_a_side_effect(self):
        code, out, err = self.run_setup("add-user", "newbie.qa", "--apply")
        self.assertEqual(len(out.splitlines()), 1)
        self.assertNotIn("side effect", out)

    def test_no_side_effect_lines_for_a_user_that_was_not_written_nor_in_a_dry_run(self):
        self.world.write_side_effect_groups = {40: True}
        code, out, err = self.run_setup("add-user", "qa.user", "--apply")             # already a manager: no write
        self.assertEqual((code, self.changes(out)), (0, []), err)
        code, out, err = self.run_setup("add-user", "newbie.qa")                      # dry run
        self.assertEqual((code, self.changes(out)), (0, []), err)
        self.assertNoWrites()
        self.assertNotIn(40, self.groups_of(12))

    def test_a_malformed_reread_after_the_write_is_an_error_after_the_change_line(self):
        self.world.users_read_override = lambda n, ids, fields: [{"id": 12}] if n == 2 else UNSET   # no groups_id
        code, out, err = self.run_setup("add-user", "newbie.qa", "--apply")
        self.assertEqual(code, 2, err)
        self.assertEqual(out.splitlines(), ["CHANGE: added newbie.qa (id 12) to the dashboard manager group (id 2)"])
        self.assertEqual(len(self.fake.write_requests()), 1)
        self.assertIn("cannot read the groups of user id 12", err)
        self.assertNotIn("nothing changed", err)             # the write WAS sent: the message must not say otherwise

    def test_a_hook_that_undoes_an_add_is_an_error_not_a_silent_success(self):
        self.world.write_side_effect_groups = {MANAGER_ID: False}       # another module removes the right again
        code, out, err = self.run_setup("add-user", "newbie.qa", "--apply")
        self.assertEqual(code, 2, err)
        self.assertEqual(out.splitlines()[0], "CHANGE: added newbie.qa (id 12) to the dashboard manager group (id 2)")
        self.assertIn("is still outside the dashboard manager group", err)
        self.assertEqual(len(self.fake.write_requests()), 1)
        # round 3 (security r2, finding 2): the stdout record (the one the admin keeps for a revert) must not stand with a
        # lone CHANGE line: a NOT APPLIED line follows it, with the exact words
        self.assertEqual(out.splitlines(), [
            "CHANGE: added newbie.qa (id 12) to the dashboard manager group (id 2)",
            "NOT APPLIED: the write for newbie.qa (id 12) was sent but the user is still outside the dashboard manager "
            "group (a hook undid it): the CHANGE line above did not stay"])

    def test_a_hook_that_undoes_a_remove_is_an_error_not_a_silent_success(self):
        self.world.write_side_effect_groups = {MANAGER_ID: True}        # another module gives the right back
        code, out, err = self.run_setup("remove-user", "qa.user", "--apply")
        self.assertEqual(code, 2, err)
        self.assertIn("is still in the dashboard manager group", err)
        self.assertEqual(out.splitlines(), [
            "CHANGE: removed qa.user (id %d) from the dashboard manager group (id 2)" % FAKE_UID,
            "NOT APPLIED: the write for qa.user (id %d) was sent but the user is still in the dashboard manager "
            "group (a hook undid it): the CHANGE line above did not stay" % FAKE_UID])

    def test_the_not_applied_line_comes_after_the_side_effect_lines_and_before_the_error(self):
        # CHANGE (sent) -> SIDE EFFECT lines (real changes) -> NOT APPLIED (verdict on the manager group) -> error on stderr
        self.world.write_side_effect_groups = {MANAGER_ID: False, 40: True, 41: True}
        code, out, err = self.run_setup("add-user", "newbie.qa", "--apply")
        self.assertEqual(code, 2, err)
        lines = out.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("CHANGE: added newbie.qa"))
        self.assertEqual(lines[1:3], ["CHANGE: side effect on newbie.qa (id 12): group id 40 added",
                                      "CHANGE: side effect on newbie.qa (id 12): group id 41 added"])
        self.assertTrue(lines[3].startswith("NOT APPLIED: the write for newbie.qa (id 12) was sent"), lines[3])
        self.assertEqual(err.count("\n"), 1)                       # the one error line, on stderr

    def test_the_not_applied_line_is_for_the_failing_user_only_and_stops_the_command(self):
        # the first user (login order) is undone by the hook: no write for the second one, and one NOT APPLIED line
        self.world.write_side_effect_groups = {MANAGER_ID: False}
        code, out, err = self.run_setup("add-user", "second.qa", "newbie.qa", "--apply")
        self.assertEqual(code, 2, err)
        self.assertEqual([c[2][0] for c in self.fake.write_requests()], [[12]])
        self.assertEqual(sum(line.startswith("NOT APPLIED") for line in out.splitlines()), 1)
        self.assertNotIn("second.qa", out)

    def test_no_not_applied_line_when_the_write_stayed_or_nothing_was_written(self):
        for argv in (["add-user", "newbie.qa", "--apply"], ["add-user", "newbie.qa"], ["add-user", "qa.user", "--apply"],
                     ["remove-user", "qa.user", "--apply"], ["remove-user", "newbie.qa", "--apply"]):
            with self.subTest(argv=argv):
                self.world.write_side_effect_groups = {40: True}       # a side effect that is not the manager group
                code, out, err = self.run_setup(*argv)
                self.assertEqual(code, 0, err)
                self.assertNotIn("NOT APPLIED", out + err)


# ---------------------------------------------------------------------------- list-orphans

class ListOrphansTests(SetupCase):
    def test_list_orphans_prints_only_the_dashboards_of_the_group_without_a_creator_archived_ones_included(self):
        archived = self.world.add_dashboard("Gone and archived", self.s.group, creator=False, active=False)
        self.world.add_dashboard("Orphan elsewhere", self.s.other_group, creator=False)
        code, out, err = self.run_setup("list-orphans")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertIn("%d\tactive\tLeaver view v1" % self.s.orphan, lines)
        self.assertIn("%d\tactive\tKPI 1" % self.s.shipped, lines)       # shipped by a module: no creator either
        self.assertIn("%d\tarchived\tGone and archived" % archived, lines)
        self.assertEqual(len(lines), 3)
        self.assertNoWrites()

    def test_list_orphans_asks_for_the_group_with_active_test_false(self):
        self.run_setup("list-orphans")
        call = [c for c in self.fake.sequence() if (c[0], c[1]) == (DASH, "search_read")][0]
        self.assertEqual(call[2][0], [["dashboard_group_id", "=", self.s.group]])
        self.assertEqual(call[3]["context"], {"active_test": False})

    def test_list_orphans_without_the_group_says_so(self):
        del self.world.groups[self.s.group]
        code, out, err = self.run_setup("list-orphans")
        self.assertEqual(code, 0, err)
        self.assertIn("is missing: nothing to list", out)

    def test_list_orphans_refuses_duplicate_groups(self):
        self.world.add_group(GROUP)
        code, out, err = self.run_setup("list-orphans")
        self.assertEqual((code, out), (2, ""))

    def test_a_non_list_answer_is_an_error_and_nothing_is_printed(self):
        # round 2 (security r1): a non-list answer used to read as "no orphans" (rc 0)
        for label, raw in (("None", None), ("False", False), ("empty dict", {}), ("dict", {"a": 1}), ("text", "x"),
                           ("empty text", ""), ("number", 3)):
            with self.subTest(answer=label):
                self.world.dashboard_rows_override = raw
                code, out, err = self.run_setup("list-orphans")
                self.assertEqual((code, out), (2, ""), err)
                self.assertIn("unexpected shape", err)
        self.assertNoWrites()

    def test_a_row_is_an_orphan_only_when_the_creator_key_is_present_and_falsy(self):
        rows = [{"id": 1, "name": "no key", "active": True},
                {"id": 2, "name": "creator false", "create_uid": False, "active": True},
                {"id": 3, "name": "creator none", "create_uid": None, "active": True},
                {"id": 4, "name": "creator pair", "create_uid": [7, "QA"], "active": True},
                {"id": 5, "name": "creator zero", "create_uid": 0, "active": False},
                {"id": 6, "name": "creator number", "create_uid": 7, "active": True},
                {"id": 7, "name": "creator empty list", "create_uid": [], "active": True}]
        self.world.dashboard_rows_override = rows
        code, out, err = self.run_setup("list-orphans")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["2\tactive\tcreator false", "3\tactive\tcreator none",
                                            "5\tarchived\tcreator zero", "7\tactive\tcreator empty list"])


# ---------------------------------------------------------------------------- the group name is a translated field

class GroupLanguageTests(SetupCase):
    """Round 2 (security r1, finding 4), corrected in round 3 (security r2): an `=` search on a translated field compares
    the `lang` of the call context. `execute_kw` has NO user language today (en_US is compared), so the explicit en_US
    pin of every group lookup is defence in depth: it keeps a ko_KR administrator off a decoy / a duplicate if a client
    ever carries the user's language. The decoy tests below switch the fake to such a client
    (`context_lang_from_user`), the only way a dropped pin can make them red."""

    LOOKUP = {"context": {"active_test": False, "lang": "en_US"}}
    GROUP_COMMANDS = (["status"], ["init"], ["list-orphans"], ["remove-group"])      # the commands that look the group up

    def test_every_group_lookup_carries_the_stored_language_in_the_context(self):
        for argv in self.GROUP_COMMANDS:
            with self.subTest(command=argv[0]):
                self.fake.requests.clear()
                self.run_setup(*argv)
                lookups = [c for c in self.fake.sequence() if c[:2] == (DGROUP, "search")]
                self.assertEqual([c[2:] for c in lookups], [([[["name", "=", GROUP]]], self.LOOKUP)])

    def test_a_group_whose_translation_equals_the_name_is_not_the_group(self):
        self.world.users[ADMIN_UID]["lang"] = "ko_KR"
        self.world.context_lang_from_user = True          # a client that carries the user's language (see the class)
        self.world.add_group("Decoy", translations={"ko_KR": GROUP})
        code, out, err = self.run_setup("status")
        self.assertEqual((code, out.splitlines()[0]), (0, "group 'KPI (QA/QC)': id %d" % self.s.group), err)
        code, out, err = self.run_setup("init", "--apply")
        self.assertEqual(code, 0, err)
        self.assertIn("already exists: id %d" % self.s.group, out)
        code, out, err = self.run_setup("list-orphans")
        self.assertEqual(code, 0, err)
        self.assertIn("Leaver view v1", out)
        code, out, err = self.run_setup("remove-group", "--apply")            # the real group still holds dashboards
        self.assertEqual(code, 2)
        self.assertIn("still holds", err)
        self.assertNotIn("dashboard groups are named", err)
        self.assertNoWrites()

    def test_a_group_renamed_in_the_admins_language_is_still_found_so_init_creates_no_duplicate(self):
        self.world.users[ADMIN_UID]["lang"] = "ko_KR"
        self.world.context_lang_from_user = True          # a client that carries the user's language (see the class)
        self.world.groups[self.s.group]["translations"] = {"ko_KR": "KPI (품질)"}
        code, out, err = self.run_setup("init", "--apply")
        self.assertEqual(code, 0, err)
        self.assertIn("already exists: id %d" % self.s.group, out)
        self.assertNoWrites()
        self.assertEqual(self.named_groups(), [self.s.group])

    def test_a_decoy_alone_does_not_stop_init_from_creating_the_real_group(self):
        self.world.users[ADMIN_UID]["lang"] = "ko_KR"
        self.world.context_lang_from_user = True          # a client that carries the user's language (see the class)
        del self.world.groups[self.s.group]
        self.world.add_group("Decoy", translations={"ko_KR": GROUP})
        code, out, err = self.run_setup("init", "--apply")
        self.assertEqual(code, 0, err)
        self.assertIn("CHANGE: created dashboard group", out)
        self.assertEqual(len(self.named_groups()), 1)
        self.assertEqual(len(self.fake.write_requests()), 1)


# ---------------------------------------------------------------------------- remove-group

class RemoveGroupTests(SetupCase):
    def empty_the_group(self):
        for dashboard_id in [k for k, v in self.world.dashboards.items() if v["dashboard_group_id"] == self.s.group]:
            del self.world.dashboards[dashboard_id]

    def test_a_group_that_holds_dashboards_is_not_removed_with_or_without_apply(self):
        for apply in (["--apply"], []):
            with self.subTest(apply=apply):
                code, out, err = self.run_setup("remove-group", *apply)
                self.assertEqual((code, out), (2, ""), err)
                self.assertIn("still holds", err)
                self.assertNoWrites()
                self.assertIn(self.s.group, self.world.groups)

    def test_a_group_that_holds_only_an_archived_dashboard_is_not_removed(self):
        self.empty_the_group()
        self.world.add_dashboard("Only archived", self.s.group, creator=FAKE_UID, active=False)
        code, out, err = self.run_setup("remove-group", "--apply")
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("still holds 1 dashboard", err)
        self.assertNoWrites()

    def test_the_count_asks_about_the_exact_group_with_archived_ones_included(self):
        self.empty_the_group()
        self.run_setup("remove-group")
        call = [c for c in self.fake.sequence() if (c[0], c[1]) == (DASH, "search_count")][0]
        self.assertEqual(call[2], [[["dashboard_group_id", "=", self.s.group]]])
        self.assertEqual(call[3], {"context": {"active_test": False}})

    def test_an_empty_group_is_removed_with_apply_and_one_change_line(self):
        self.empty_the_group()
        other = dict(self.world.groups[self.s.other_group])
        code, out, err = self.run_setup("remove-group", "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.fake.write_requests(), [(DGROUP, "unlink", [[self.s.group]], {})])
        self.assertEqual(out.splitlines(), ["CHANGE: deleted dashboard group 'KPI (QA/QC)' id %d" % self.s.group])
        self.assertNotIn(self.s.group, self.world.groups)
        self.assertEqual(self.world.groups, {self.s.other_group: other})

    def test_an_empty_group_is_only_announced_without_apply(self):
        self.empty_the_group()
        code, out, err = self.run_setup("remove-group")
        self.assertEqual(code, 0, err)
        self.assertIn("dry run: would delete the empty dashboard group", out)
        self.assertEqual(self.changes(out), [])
        self.assertNoWrites()
        self.assertIn(self.s.group, self.world.groups)

    def test_a_missing_group_is_nothing_to_do(self):
        self.empty_the_group()
        del self.world.groups[self.s.group]
        code, out, err = self.run_setup("remove-group", "--apply")
        self.assertEqual(code, 0, err)
        self.assertIn("does not exist (nothing to do)", out)
        self.assertEqual(self.changes(out), [])
        self.assertNoWrites()

    def test_duplicate_groups_are_never_removed(self):
        self.empty_the_group()
        self.world.add_group(GROUP)
        code, out, err = self.run_setup("remove-group", "--apply")
        self.assertEqual((code, out), (2, ""))
        self.assertNoWrites()

    def test_only_the_exact_group_is_removed_a_lookalike_stays(self):
        self.empty_the_group()
        lookalike = self.world.add_group("KPI (QA/QC) archive")
        self.run_setup("remove-group", "--apply")
        self.assertIn(lookalike, self.world.groups)
        self.assertIn(self.s.other_group, self.world.groups)

    def test_a_lookalike_alone_is_not_removed(self):
        self.empty_the_group()
        del self.world.groups[self.s.group]
        lookalike = self.world.add_group("kpi (qa/qc)")
        code, out, err = self.run_setup("remove-group", "--apply")
        self.assertEqual(code, 0, err)
        self.assertNoWrites()
        self.assertIn(lookalike, self.world.groups)

    def test_a_malformed_count_answer_is_not_taken_for_an_empty_group(self):
        # fail closed (finding F1): None / False / [] / {} are falsy, so `if held:` reads them as "no dashboards"
        for label, raw in (("None", None), ("False", False), ("empty list", []), ("empty dict", {}), ("empty text", "")):
            with self.subTest(answer=label):
                self.world.groups.setdefault(self.s.group, {"name": GROUP})      # a wrongly deleted group comes back
                self.world.count_override = raw
                self.fake.requests.clear()
                code, out, err = self.run_setup("remove-group", "--apply")
                self.assertEqual(code, 2, err)
                self.assertEqual(self.fake.execute_calls(DGROUP, "unlink"), [])


if __name__ == "__main__":
    unittest.main()
