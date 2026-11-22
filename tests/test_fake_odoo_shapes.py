"""The fake Odoo must refuse every call shape the REAL Odoo 17 refuses (step 9, security round 1).

Why: the round-1 fake modelled `res.users.has_group` with ids (`[[uid], xmlid]`) while the real method is an @api.model
method (`has_group(self, group_ext_id)`, odoo/odoo/addons/base/models/res_users.py:1085) that the server evaluates for the
authenticated uid: a gate that the real server turns into a TypeError looked tested. These tests pin the real shapes of
every method the fake models (the signatures are read from models.py / ir_model.py / res_users.py of Odoo 17):

    has_group(group_ext_id)                                    @api.model
    search(domain, offset=0, limit=None, order=None)           @api.model
    search_read(domain=None, fields=None, offset=0, limit=None, order=None, **read_kwargs)
    search_count(domain, limit=None)
    read(ids, fields=None, load='_classic_read')               recordset: the ids come first
    get_metadata(ids)   write(ids, vals)   unlink(ids)         recordset: the ids come first
    create(vals)                                               @api.model_create_multi
    check_object_reference(module, xml_id, raise_on_access_error=False)    @api.model
    read_group(domain, fields, groupby, offset=0, limit=None, orderby=False, lazy=True)    @api.model (models.py:2682)
    fields_get(allfields=None, attributes=None)                @api.model (models.py:3443)
"""
import unittest

from dashboard_fixtures import (ADMIN_LOGIN, ADMIN_SECRET, ADMIN_UID, GROUP, MANAGER, PLAIN_UID, SYSTEM, Scenario,
                                StreamCase, start_fake)
from fake_odoo import FAKE_KEY, FAKE_TZ_OFFSETS, FAKE_UID, REAL_API
from helpers import make_config, odoo_rpc

DASH, DGROUP, USERS, DATA = "spreadsheet.dashboard", "spreadsheet.dashboard.group", "res.users", "ir.model.data"
EVENTS = "t.events"                      # a KPI-like data model the tests add to the world
NO_ARCHIVED = {"context": {"active_test": False}}


class ShapeCase(StreamCase):
    SECRETS = (FAKE_KEY, ADMIN_SECRET)

    def setUp(self):
        super().setUp()
        self.fake = start_fake(self)
        self.s = Scenario(self.fake)
        self.world = self.s.world
        self.client = odoo_rpc.Client(make_config(self.fake.url))             # the qa.user, uid FAKE_UID
        self.client.login()

    def call(self, model, method, args, kwargs=None):
        return self.client.execute(model, method, args, kwargs)

    def assertRefused(self, model, method, args, kwargs=None, text="TypeError"):
        before = self.world.snapshot()
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            self.call(model, method, args, kwargs)
        self.assertIn(text, str(caught.exception))
        self.assertEqual(self.world.snapshot(), before, "a refused call must not change anything")


class HasGroupShapeTests(ShapeCase):
    def test_the_real_shape_is_one_xmlid_and_answers_for_the_authenticated_user(self):
        self.assertIs(self.call(USERS, "has_group", [MANAGER]), True)           # qa.user is a dashboard manager
        self.assertIs(self.call(USERS, "has_group", [SYSTEM]), False)           # but not a system administrator

    def test_the_answer_follows_the_authenticated_uid_not_a_uid_in_the_arguments(self):
        admin_fake = start_fake(self, login=ADMIN_LOGIN, key=ADMIN_SECRET, uid=ADMIN_UID)   # the fake serves one login
        Scenario(admin_fake)
        admin = odoo_rpc.Client(make_config(admin_fake.url, key=ADMIN_SECRET, login=ADMIN_LOGIN))
        self.assertIs(admin.execute(USERS, "has_group", [SYSTEM]), True)
        self.assertIs(self.call(USERS, "has_group", [SYSTEM]), False)          # same xmlid, another authenticated uid

    def test_the_old_shape_with_ids_is_rejected_like_the_real_server_does(self):
        self.assertRefused(USERS, "has_group", [[FAKE_UID], MANAGER],
                           text="has_group() takes 2 positional arguments but 3 were given")
        self.assertRefused(USERS, "has_group", [[ADMIN_UID], SYSTEM], text="takes 2 positional arguments but 3 were given")

    def test_every_other_wrong_shape_of_has_group_is_rejected(self):
        for label, args, text in (("no argument", [], "TypeError"), ("two xmlids", [MANAGER, SYSTEM], "TypeError"),
                                  ("uid then xmlid", [FAKE_UID, MANAGER], "TypeError"),
                                  ("ids only", [[FAKE_UID]], "must be text"), ("a number", [3], "must be text")):
            with self.subTest(shape=label):
                self.assertRefused(USERS, "has_group", args, text=text)
        self.assertRefused(USERS, "has_group", [MANAGER], {"uid": FAKE_UID})


class WrongShapeTests(ShapeCase):
    """A good call is answered; a call with a wrong arity / shape is a TypeError that changes nothing."""

    def setUp(self):
        super().setUp()
        self.domain = [["dashboard_group_id", "=", self.s.group]]

    def test_search(self):
        self.assertEqual(self.call(DGROUP, "search", [[["name", "=", GROUP]]], NO_ARCHIVED), [self.s.group])
        self.assertEqual(self.call(DGROUP, "search", [[["name", "=", GROUP]], 0, 5, "id"]), [self.s.group])
        for label, args, kwargs in (("no domain", [], None), ("too many", [[], 0, 5, "id", "extra"], None),
                                    ("unknown keyword", [[]], {"fields": ["name"]})):
            with self.subTest(shape=label):
                self.assertRefused(DGROUP, "search", args, kwargs)

    def test_search_read(self):
        rows = self.call(DASH, "search_read", [self.domain, ["name"]], {"order": "id", **NO_ARCHIVED})
        self.assertTrue(rows)
        with self.subTest(shape="too many"):
            self.assertRefused(DASH, "search_read", [self.domain, ["name"], 0, 5, "id", "extra"])
        with self.subTest(shape="same argument twice"):
            self.assertRefused(DASH, "search_read", [self.domain, ["name"]], {"fields": ["name"]})

    def test_search_count(self):
        self.assertEqual(self.call(DASH, "search_count", [self.domain], NO_ARCHIVED), 6)
        for label, args in (("no domain", []), ("too many", [self.domain, 5, "extra"])):
            with self.subTest(shape=label):
                self.assertRefused(DASH, "search_count", args)
        self.assertRefused(DASH, "search_count", [self.domain], {"order": "id"})

    def test_read(self):
        self.assertEqual(self.call(DASH, "read", [[self.s.own], ["name"]])[0]["name"], "Quality v1")
        self.assertEqual(self.call(USERS, "read", [[FAKE_UID], ["groups_id"]], NO_ARCHIVED)[0]["groups_id"], [1, 2])
        for label, args in (("no ids", []), ("ids missing, fields first", [["name"]]), ("ids are text", [["x"], ["name"]]),
                            ("ids is a dict", [{"id": 1}, ["name"]]), ("bool id", [[True], ["name"]]),
                            ("too many", [[self.s.own], ["name"], "_classic_read", "extra"])):
            with self.subTest(shape=label):
                self.assertRefused(DASH, "read", args)
        self.assertRefused(DASH, "read", [[self.s.own], ["name"]], {"fields": ["name"]})

    def test_get_metadata(self):
        self.assertEqual(self.call(DASH, "get_metadata", [[self.s.own]], NO_ARCHIVED)[0]["id"], self.s.own)
        for label, args in (("no ids", []), ("ids are text", [["x"]]), ("an extra argument", [[self.s.own], "extra"])):
            with self.subTest(shape=label):
                self.assertRefused(DASH, "get_metadata", args)
        self.assertRefused(DASH, "get_metadata", [[self.s.own]], {"fields": ["name"]})

    def test_create(self):
        self.assertIsInstance(self.call(DGROUP, "create", [{"name": "Fresh group"}]), int)
        for label, args in (("no values", []), ("two dicts as two arguments", [{"name": "a"}, {"name": "b"}])):
            with self.subTest(shape=label):
                self.assertRefused(DGROUP, "create", args)

    def test_write(self):
        self.assertIs(self.call(DASH, "write", [[self.s.own], {"active": True}]), True)
        self.assertIs(self.call(USERS, "write", [[PLAIN_UID], {"groups_id": [[4, 2]]}]), True)
        for label, args in (("values without ids", [{"active": False}]), ("ids without values", [[self.s.own]]),
                            ("no argument", []), ("too many", [[self.s.own], {"active": False}, {"active": True}]),
                            ("ids are text", [["x"], {"active": False}])):
            with self.subTest(shape=label):
                self.assertRefused(DASH, "write", args)

    def test_unlink(self):
        self.assertIs(self.call(DASH, "unlink", [[self.s.archived_own]]), True)
        for label, args in (("no ids", []), ("a bare dict", [{"id": self.s.own}]), ("an extra argument", [[self.s.own], "x"])):
            with self.subTest(shape=label):
                self.assertRefused(DASH, "unlink", args)
        self.assertIn(self.s.own, self.world.dashboards)

    def test_check_object_reference(self):
        self.assertEqual(self.call(DATA, "check_object_reference", ["base", "group_user"]), ["res.groups", 1])
        self.assertEqual(self.call(DATA, "check_object_reference", ["base", "group_user", False]), ["res.groups", 1])
        for label, args in (("one argument", ["base.group_user"]), ("no argument", []),
                            ("too many", ["base", "group_user", False, "extra"])):
            with self.subTest(shape=label):
                self.assertRefused(DATA, "check_object_reference", args)

    def test_read_group(self):
        self.world.add_model(EVENTS, {"stamp": "datetime", "kind": "char", "qty": "float"})
        self.world.add_row(EVENTS, stamp="2026-04-01 03:00:00", kind="a", qty=2.0)
        good = [[["kind", "=", "a"]], ["qty"], ["stamp:month"]]
        groups = self.call(EVENTS, "read_group", good, {"lazy": False, "context": {"tz": "Asia/Seoul", "lang": "en_US"}})
        self.assertEqual(groups[0]["__range"], {"stamp:month": {"from": "2026-03-31 15:00:00",
                                                                "to": "2026-04-30 15:00:00"}})
        self.assertEqual((groups[0]["__count"], groups[0]["qty"]), (1, 2.0))
        # every positional parameter of the real signature, in its order
        self.assertEqual(self.call(EVENTS, "read_group", [[], ["qty"], ["stamp:month"], 0, None, False, False]),
                         self.call(EVENTS, "read_group", [[], ["qty"], ["stamp:month"]], {"lazy": False}))
        for label, args, kwargs in (("no argument", [], None), ("no groupby", [[], ["qty"]], None),
                                    ("too many", [[], ["qty"], ["stamp:month"], 0, None, False, False, "extra"], None),
                                    ("unknown keyword", [[], ["qty"], ["stamp:month"]], {"order": "id"}),
                                    ("same argument twice", [[], ["qty"], ["stamp:month"]],
                                     {"groupby": ["stamp:month"]}),
                                    ("fields given twice",[[], ["qty"], ["stamp:month"]], {"fields": ["qty"]})):
            with self.subTest(shape=label):
                self.assertRefused(EVENTS, "read_group", args, kwargs)

    def test_fields_get(self):
        selection = [[name, name] for name in FAKE_TZ_OFFSETS]
        self.assertEqual(self.call(USERS, "fields_get", [["tz"]], {"attributes": ["selection"]}),
                         {"tz": {"selection": selection}})
        self.assertEqual(self.call(USERS, "fields_get", [["tz"], ["selection"]]), {"tz": {"selection": selection}})
        for label, args, kwargs in (("too many", [["tz"], ["selection"], "extra"], None),
                                    ("unknown keyword", [["tz"]], {"fields": ["tz"]}),
                                    ("same argument twice", [["tz"]], {"allfields": ["tz"]}),
                                    ("same attributes twice", [["tz"], ["selection"]], {"attributes": ["selection"]})):
            with self.subTest(shape=label):
                self.assertRefused(USERS, "fields_get", args, kwargs)

    def test_every_modelled_method_has_a_real_signature_entry(self):
        self.assertEqual(sorted(REAL_API), sorted({"has_group", "search", "search_read", "search_count", "read",
                                                   "get_metadata", "create", "write", "unlink",
                                                   "check_object_reference", "read_group", "fields_get"}))


class LanguageOfTheLookupTests(ShapeCase):
    """`spreadsheet.dashboard.group.name` is translate=True: an `=` search compares the language of the call."""

    def test_the_lookup_language_decides_which_text_is_compared(self):
        decoy = self.world.add_group("Decoy", translations={"ko_KR": GROUP})
        found = lambda lang: self.call(DGROUP, "search", [[["name", "=", GROUP]]], {"context": {"lang": lang}})
        self.assertEqual(found("en_US"), [self.s.group])                       # the stored name only
        self.assertEqual(found("ko_KR"), sorted([self.s.group, decoy]))        # the translation, else the stored name

    def test_without_a_lang_in_the_context_en_us_is_compared_whatever_the_users_own_language_is(self):
        # security r2: `/jsonrpc` `execute_kw` builds `Environment(cr, uid, {})`, so there is no `env.lang` and the search
        # compares the stored en_US text for a ko_KR user too (the user's language is NOT applied on this transport)
        decoy = self.world.add_group("Decoy", translations={"ko_KR": GROUP})
        for user_lang in ("ko_KR", "vi_VN", "en_US"):
            with self.subTest(user_lang=user_lang):
                self.world.users[FAKE_UID]["lang"] = user_lang
                for kwargs in ({}, {"context": {}}, {"context": {"lang": False}}, {"context": {"active_test": False}}):
                    self.assertEqual(self.call(DGROUP, "search", [[["name", "=", GROUP]]], kwargs), [self.s.group])
                    self.assertNotIn(decoy, self.call(DGROUP, "search", [[["name", "=", GROUP]]], kwargs))
                self.assertEqual(self.call(DGROUP, "search", [[["name", "=", GROUP]]], {"context": {"lang": "en_US"}}),
                                 [self.s.group])

    def test_the_real_transport_is_the_default_and_the_user_language_client_is_opt_in(self):
        decoy = self.world.add_group("Decoy", translations={"ko_KR": GROUP})
        self.world.users[FAKE_UID]["lang"] = "ko_KR"
        self.assertFalse(self.world.context_lang_from_user)                      # execute_kw: empty context
        self.assertEqual(self.call(DGROUP, "search", [[["name", "=", GROUP]]]), [self.s.group])
        self.world.context_lang_from_user = True                                 # a client that carries the user's lang
        self.assertEqual(self.call(DGROUP, "search", [[["name", "=", GROUP]]]), sorted([self.s.group, decoy]))
        self.assertEqual(self.call(DGROUP, "search", [[["name", "=", GROUP]]], {"context": {"lang": "en_US"}}),
                         [self.s.group])                                         # an explicit lang still wins

    def test_an_explicit_lang_in_the_context_compares_that_translation(self):
        # the only way to reach the translation: the caller names the language, whatever the user's own is
        decoy = self.world.add_group("Decoy", translations={"ko_KR": GROUP})
        self.world.users[FAKE_UID]["lang"] = "en_US"
        self.assertEqual(self.call(DGROUP, "search", [[["name", "=", GROUP]]], {"context": {"lang": "ko_KR"}}),
                         sorted([self.s.group, decoy]))
        self.assertEqual(self.call(DGROUP, "search", [[["name", "=", GROUP]]], {"context": {"lang": "vi_VN"}}),
                         [self.s.group])                 # a language without a translation falls back to the stored text


if __name__ == "__main__":
    unittest.main()
