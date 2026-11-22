"""The KPI data models of the fake Odoo (`DashboardWorld.add_model`): the reconcile tests stand on them, so they are pinned
here against what Odoo 17 does (`models.py` read_group 2506-2532, `expression.py:1389`), without any HTTP.

The time zone table is a SMALL FIXED one (stdlib only, Python 3.8 has no `zoneinfo`); the expected instants below are
written out by hand, never computed with the table under test.
"""
import datetime
import unittest

from fake_odoo import FAKE_TZ_OFFSETS, DashboardWorld, DocumentsError, local_month_of, local_month_start_utc

EVENTS = "t.events"
SCHEMA = {"stamp": "datetime", "day": "date", "form": "char", "qty": "float", "n": "integer"}
GROUP = ["stamp:month"]


def at(text):
    return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


class WorldCase(unittest.TestCase):
    def setUp(self):
        self.world = DashboardWorld()
        self.world.add_model(EVENTS, SCHEMA)

    def call(self, method, args, kwargs=None, model=EVENTS):
        return self.world.call(model, method, args, kwargs or {}, 7)

    def groups(self, rows, tz=None, domain=(), fields=("qty",), **extra):
        for row in rows:
            self.world.add_row(EVENTS, **row)
        context = {"lang": "en_US"}
        if tz is not None:
            context["tz"] = tz
        return self.call("read_group", [list(domain), list(fields), GROUP], dict({"lazy": False, "context": context},
                                                                                 **extra))


class TimeZoneTableTests(unittest.TestCase):
    def test_the_utc_instant_of_a_local_month_start_in_every_zone(self):
        for tz, year, month, expected in (
                ("UTC", 2026, 4, "2026-04-01 00:00:00"),
                ("Asia/Seoul", 2026, 4, "2026-03-31 15:00:00"),                # the example of the Plan
                ("Asia/Ho_Chi_Minh", 2026, 4, "2026-03-31 17:00:00"),
                ("Pacific/Kiritimati", 2026, 4, "2026-03-31 10:00:00"),         # +14, the furthest east
                ("Etc/GMT+12", 2026, 4, "2026-04-01 12:00:00"),                 # -12, the furthest west
                ("America/Los_Angeles", 2026, 4, "2026-04-01 07:00:00"),        # daylight time (-7)
                ("America/Los_Angeles", 2026, 3, "2026-03-01 08:00:00"),        # the 1st of March is still -8
                ("America/Los_Angeles", 2026, 11, "2026-11-01 07:00:00"),       # the 1st of November is still -7
                ("America/Los_Angeles", 2026, 12, "2026-12-01 08:00:00"),
                ("America/Los_Angeles", 2026, 1, "2026-01-01 08:00:00"),
                ("Europe/Berlin", 2026, 3, "2026-02-28 23:00:00"),              # +1
                ("Europe/Berlin", 2026, 4, "2026-03-31 22:00:00"),              # +2
                ("Europe/Berlin", 2026, 10, "2026-09-30 22:00:00"),
                ("Europe/Berlin", 2026, 11, "2026-10-31 23:00:00"),
                ("Asia/Seoul", 2026, 1, "2025-12-31 15:00:00")):                # across the year boundary
            with self.subTest(tz=tz, month=month):
                self.assertEqual(local_month_start_utc(tz, year, month), at(expected))

    def test_a_name_that_is_not_a_zone_of_the_fake_is_utc(self):
        for tz in ("Mars/Phobos", "", None, "asia/seoul", "UTC+9"):
            with self.subTest(tz=tz):
                self.assertEqual(local_month_start_utc(tz, 2026, 4), at("2026-04-01 00:00:00"))

    def test_the_local_month_of_an_instant_next_to_a_boundary(self):
        for tz, instant, expected in (
                ("Asia/Seoul", "2026-03-31 14:59:59", (2026, 3)), ("Asia/Seoul", "2026-03-31 15:00:00", (2026, 4)),
                ("America/Los_Angeles", "2026-04-01 06:59:59", (2026, 3)),
                ("America/Los_Angeles", "2026-04-01 07:00:00", (2026, 4)),
                ("America/Los_Angeles", "2026-11-01 06:59:59", (2026, 10)),
                ("America/Los_Angeles", "2026-11-01 07:00:00", (2026, 11)),
                ("America/Los_Angeles", "2026-12-01 07:59:59", (2026, 11)),
                ("America/Los_Angeles", "2026-12-01 08:00:00", (2026, 12)),
                ("Asia/Seoul", "2025-12-31 15:00:00", (2026, 1)), ("Asia/Seoul", "2026-12-31 14:59:59", (2026, 12)),
                ("Etc/GMT+12", "2026-01-01 11:59:59", (2025, 12)), ("UTC", "2026-01-01 00:00:00", (2026, 1))):
            with self.subTest(tz=tz, instant=instant):
                self.assertEqual(local_month_of(tz, at(instant)), expected)

    def test_the_table_holds_the_zones_the_tests_name(self):
        self.assertEqual(sorted(FAKE_TZ_OFFSETS), sorted(["UTC", "Asia/Seoul", "Asia/Ho_Chi_Minh", "Pacific/Kiritimati",
                                                          "Etc/GMT+12", "America/Los_Angeles", "Europe/Berlin"]))


class ReadGroupTests(WorldCase):
    def test_a_group_carries_the_count_the_sums_and_the_range_of_the_local_month(self):
        groups = self.groups([{"stamp": "2026-03-31 20:00:00", "form": "a", "qty": 2.5},
                              {"stamp": "2026-04-10 01:00:00", "form": "a", "qty": 1.5},
                              {"stamp": "2026-06-02 01:00:00", "form": "a", "qty": 4.0}], tz="Asia/Seoul")
        self.assertEqual([g["__count"] for g in groups], [2, 1])
        self.assertEqual([g["qty"] for g in groups], [4.0, 4.0])
        self.assertEqual(groups[0]["__range"], {"stamp:month": {"from": "2026-03-31 15:00:00",
                                                                "to": "2026-04-30 15:00:00"}})
        self.assertEqual(groups[1]["__range"]["stamp:month"]["from"], "2026-05-31 15:00:00")
        self.assertEqual(groups[0]["stamp:month"], "April 2026")
        self.assertEqual(set(groups[0]), {"__count", "stamp:month", "__range", "__domain", "__context", "qty"})

    def test_the_same_row_lands_in_another_month_in_another_zone(self):
        row = {"stamp": "2026-03-31 20:00:00", "qty": 1.0}
        self.assertEqual(self.groups([row], tz="Asia/Seoul")[0]["__range"]["stamp:month"]["from"],
                         "2026-03-31 15:00:00")                                         # April
        self.world.models[EVENTS]["rows"].clear()
        self.assertEqual(self.groups([row], tz="America/Los_Angeles")[0]["__range"]["stamp:month"]["from"],
                         "2026-03-01 08:00:00")                                         # March

    def test_a_zone_that_is_not_in_the_table_or_no_zone_groups_in_utc_like_the_real_server(self):
        row = {"stamp": "2026-03-31 20:00:00", "qty": 1.0}
        for tz in ("Mars/Phobos", None, ""):
            with self.subTest(tz=tz):
                self.world.models[EVENTS]["rows"].clear()
                group = self.groups([row], tz=tz)[0]
                self.assertEqual(group["__range"]["stamp:month"], {"from": "2026-03-01 00:00:00",
                                                                   "to": "2026-04-01 00:00:00"})

    def test_a_missing_context_is_utc(self):
        self.world.add_row(EVENTS, stamp="2026-03-31 20:00:00", qty=1.0)
        groups = self.call("read_group", [[], ["qty"], GROUP], {"lazy": False})
        self.assertEqual(groups[0]["__range"]["stamp:month"]["from"], "2026-03-01 00:00:00")

    def test_a_date_field_has_no_zone_and_a_date_range(self):
        self.world.add_row(EVENTS, day="2026-04-15", qty=1.0)
        groups = self.call("read_group", [[], ["qty"], ["day:month"]],
                           {"lazy": False, "context": {"tz": "Asia/Seoul"}})
        self.assertEqual(groups[0]["__range"], {"day:month": {"from": "2026-04-01", "to": "2026-05-01"}})

    def test_a_sum_over_only_unset_values_reads_false_or_what_the_knob_says(self):
        rows = [{"stamp": "2026-04-10 01:00:00", "form": "a"}, {"stamp": "2026-04-11 01:00:00", "form": "a"}]
        group = self.groups(rows, tz="UTC")[0]
        self.assertEqual((group["__count"], group["qty"]), (2, False))
        self.world.null_sum_answer = None
        self.assertIsNone(self.call("read_group", [[], ["qty"], GROUP], {"lazy": False})[0]["qty"])

    def test_unset_values_do_not_count_in_a_sum_that_has_others(self):
        group = self.groups([{"stamp": "2026-04-10 01:00:00", "qty": 2.0}, {"stamp": "2026-04-11 01:00:00"}],
                            tz="UTC")[0]
        self.assertEqual((group["__count"], group["qty"]), (2, 2.0))

    def test_a_row_without_a_date_forms_one_group_with_a_false_range(self):
        groups = self.groups([{"stamp": "2026-04-10 01:00:00", "qty": 1.0}, {"qty": 5.0}, {"qty": 6.0}], tz="UTC")
        self.assertEqual(len(groups), 2)
        self.assertEqual((groups[1]["stamp:month"], groups[1]["__range"], groups[1]["__count"], groups[1]["qty"]),
                         (False, {"stamp:month": False}, 2, 11.0))

    def test_the_domain_filters_before_the_grouping(self):
        groups = self.groups([{"stamp": "2026-04-10 01:00:00", "form": "a", "qty": 1.0},
                              {"stamp": "2026-04-11 01:00:00", "form": "b", "qty": 8.0}],
                             domain=[["form", "=", "a"]], tz="UTC")
        self.assertEqual([(g["__count"], g["qty"]) for g in groups], [(1, 1.0)])

    def test_the_count_only_call_and_a_field_that_does_not_exist(self):
        self.world.add_row(EVENTS, stamp="2026-04-10 01:00:00")
        self.assertEqual(self.call("read_group", [[], ["__count"], GROUP], {"lazy": False})[0]["__count"], 1)
        refused = self.call("read_group", [[], ["no_such"], GROUP], {"lazy": False})
        self.assertIsInstance(refused, DocumentsError)
        self.assertIn("Invalid field", refused.message)

    def test_what_is_not_modelled_is_refused_never_guessed(self):
        self.world.add_row(EVENTS, stamp="2026-04-10 01:00:00")
        for label, args, kwargs in (("lazy", [[], ["qty"], GROUP], {}),
                                    ("two groupings", [[], ["qty"], ["stamp:month", "form"]], {"lazy": False}),
                                    ("day granularity", [[], ["qty"], ["stamp:day"]], {"lazy": False}),
                                    ("a char field", [[], ["qty"], ["form:month"]], {"lazy": False}),
                                    ("a limit", [[], ["qty"], GROUP, 0, 5], {"lazy": False}),
                                    ("an order", [[], ["qty"], GROUP, 0, None, "stamp"], {"lazy": False}),
                                    ("a text measure", [[], ["form"], GROUP], {"lazy": False})):
            with self.subTest(case=label):
                self.assertIsInstance(self.call("read_group", args, kwargs), DocumentsError)

    def test_a_wrong_shape_is_a_type_error_like_the_real_server(self):
        refused = self.call("read_group", [[], ["qty"]], {"lazy": False})
        self.assertEqual(refused.name, "builtins.TypeError")


class SearchReadTests(WorldCase):
    def rows(self, domain, **kwargs):
        return self.call("search_read", [domain, ["form", "n"]], kwargs)

    def setUp(self):
        super().setUp()
        for index, day in enumerate(("2025-12-31", "2026-01-01", "2026-06-15", "2026-12-31", "2027-01-01")):
            self.world.add_row(EVENTS, day=day, form="f%d" % index, n=index)
        self.world.add_row(EVENTS, form="undated", n=99)

    def forms(self, domain, **kwargs):
        return [row["form"] for row in self.rows(domain, **kwargs)]

    def test_the_year_range_on_a_date_field_includes_both_ends(self):
        year = [["day", ">=", "2026-01-01"], ["day", "<=", "2026-12-31"]]
        self.assertEqual(self.forms(year), ["f1", "f2", "f3"])

    def test_every_operator(self):
        self.assertEqual(self.forms([["n", ">", 3]]), ["f4", "undated"])
        self.assertEqual(self.forms([["n", ">=", 3]]), ["f3", "f4", "undated"])
        self.assertEqual(self.forms([["n", "<", 1]]), ["f0"])
        self.assertEqual(self.forms([["n", "<=", 1]]), ["f0", "f1"])
        self.assertEqual(self.forms([["form", "=", "f2"]]), ["f2"])
        self.assertEqual(self.forms([["form", "!=", "f2"], ["n", "<", 4]]), ["f0", "f1", "f3"])
        self.assertEqual(self.forms([["form", "in", ["f0", "f4"]]]), ["f0", "f4"])

    def test_an_unset_value_never_satisfies_an_inequality(self):
        self.assertNotIn("undated", self.forms([["day", ">=", "1900-01-01"]]))
        self.assertNotIn("undated", self.forms([["day", "<=", "2999-12-31"]]))
        self.assertEqual(self.forms([["day", "=", False]]), ["undated"])

    def test_a_datetime_compared_with_a_date_only_text_is_completed_like_odoo(self):
        for stamp in ("2026-01-01 00:00:00", "2026-12-31 23:59:59", "2025-12-31 23:59:59", "2027-01-01 00:00:00"):
            self.world.add_row(EVENTS, stamp=stamp, form=stamp)
        year = [["stamp", ">=", "2026-01-01"], ["stamp", "<=", "2026-12-31"]]
        self.assertEqual(self.forms(year), ["2026-01-01 00:00:00", "2026-12-31 23:59:59"])
        self.assertEqual(self.forms([["stamp", ">", "2026-12-31"]]), ["2027-01-01 00:00:00"])      # > adds 23:59:59
        self.assertEqual(self.forms([["stamp", "<", "2026-01-01"]]), ["2025-12-31 23:59:59"])      # < adds 00:00:00

    def test_limit_and_the_fields_asked_for(self):
        rows = self.rows([], limit=2)
        self.assertEqual(rows, [{"id": rows[0]["id"], "form": "f0", "n": 0}, {"id": rows[1]["id"], "form": "f1", "n": 1}])
        self.assertEqual(len(self.rows([])), 6)
        self.assertEqual(len(self.rows([], limit=0)), 6)                  # Odoo: a falsy limit is no limit

    def test_what_is_not_modelled_is_refused(self):
        for label, answer in (("operator", self.rows([["n", "like", 3]])), ("field", self.rows([["nope", "=", 1]])),
                              ("text against number", self.rows([["n", ">", "3"]])),
                              ("offset", self.call("search_read", [[], ["n"], 2])),
                              ("order", self.call("search_read", [[], ["n"]], {"order": "n desc"})),
                              ("a field to read", self.call("search_read", [[], ["nope"]])),
                              ("a bad term", self.rows(["|", ["n", "=", 1]])),
                              ("another method", self.call("write", [[1], {"n": 1}]))):
            with self.subTest(case=label):
                self.assertIsInstance(answer, DocumentsError)

    def test_a_row_is_validated_when_it_is_added(self):
        with self.assertRaises(ValueError):
            self.world.add_row(EVENTS, nope=1)
        with self.assertRaises(ValueError):
            self.world.add_row(EVENTS, stamp="2026-04-10")                 # a datetime needs the time part
        with self.assertRaises(ValueError):
            self.world.add_row(EVENTS, day="2026-04-10 00:00:00")
        with self.assertRaises(ValueError):
            self.world.add_model("t.bad", {"x": "blob"})


class UsersTzTests(unittest.TestCase):
    def setUp(self):
        self.world = DashboardWorld()
        self.world.add_user(7, "someone", tz="Asia/Seoul", groups=("base.group_user",))
        self.world.add_user(8, "else", groups=("base.group_user",))

    def test_read_of_tz_and_an_unset_tz_is_false(self):
        self.assertEqual(self.world.call("res.users", "read", [[7], ["tz"]], {}, 7), [{"id": 7, "tz": "Asia/Seoul"}])
        self.assertEqual(self.world.call("res.users", "read", [[8], ["tz"]], {}, 7), [{"id": 8, "tz": False}])

    def test_fields_get_gives_the_selection_of_the_zone_names(self):
        answer = self.world.call("res.users", "fields_get", [["tz"]], {"attributes": ["selection"]}, 7)
        self.assertEqual(answer, {"tz": {"selection": [[name, name] for name in FAKE_TZ_OFFSETS]}})
        self.world.tz_selection = [["Asia/Seoul", "Seoul"]]
        self.assertEqual(self.world.call("res.users", "fields_get", [["tz"]], {"attributes": ["selection"]}, 7),
                         {"tz": {"selection": [["Asia/Seoul", "Seoul"]]}})

    def test_fields_get_of_another_field_is_not_modelled(self):
        self.assertIsInstance(self.world.call("res.users", "fields_get", [["login"]], {}, 7), DocumentsError)

    def test_the_log_records_every_call_that_reached_the_world(self):
        self.world.call("res.users", "read", [[7], ["tz"]], {}, 7)
        self.world.add_model(EVENTS, SCHEMA)
        self.world.call(EVENTS, "search_read", [[]], {}, 7)
        self.assertEqual([(model, method) for model, method, _, _ in self.world.log],
                         [("res.users", "read"), (EVENTS, "search_read")])


if __name__ == "__main__":
    unittest.main()
