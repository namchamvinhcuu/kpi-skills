"""Scenario builders and base classes for the publisher / instance-setup tests (not a test module).

Everything is invented: the fake users, the group names and the dashboard names are placeholders, the server is the
in-process `FakeOdoo` on 127.0.0.1, the credentials are the constants below.
"""
import io
import json
import os

from fake_odoo import FAKE_DB, FAKE_KEY, FAKE_LOGIN, FAKE_UID, FakeOdoo
from helpers import IsolatedTestCase, odoo_rpc  # noqa: F401  (also puts scripts/ on sys.path)

GROUP = "KPI (QA/QC)"
MANAGER = "spreadsheet_dashboard.group_dashboard_manager"
SYSTEM = "base.group_system"
USER = "base.group_user"
COLLEAGUE_UID = 8
ADMIN_UID, ADMIN_LOGIN, ADMIN_SECRET = 2, "admin.user", "ADMIN-SECRET-9f8e7d-not-a-real-secret"
PORTAL_UID, PLAIN_UID, OTHER_UID = 9, 10, 11
# Odoo's own archived INTERNAL users: `base.default_user` (login `default`, the template of every new user: a group
# written on it is copied to EVERY internal user, res_users.py `_apply_groups_to_existing_employees`) and OdooBot
TEMPLATE_UID, TEMPLATE_LOGIN, ODOOBOT_UID, ODOOBOT_LOGIN = 5, "default", 1, "__system__"
DASHBOARD_BYTES = json.dumps({"sheets": [{"id": "sheet1", "name": "Quality", "cells": {"A1": {"content": "hello"}}}]},
                             sort_keys=True).encode("utf-8")


class Scenario:
    """The users, groups and dashboards every test starts from (ids are read from the attributes, never hard coded).

    Users: FAKE_UID `qa.user` (the publisher: internal user, dashboard manager = decision D2), COLLEAGUE_UID,
    ADMIN_UID `admin.user` (system administrator + dashboard manager), PORTAL_UID (share user), PLAIN_UID (internal,
    no manager right), OTHER_UID `alice` and `ALICE` lookalikes are added by the tests that need them.
    """

    def __init__(self, fake):
        self.fake = fake
        self.world = world = fake.enable_world()
        world.add_user(FAKE_UID, FAKE_LOGIN, "QA User", groups=(USER, MANAGER))
        world.add_user(COLLEAGUE_UID, "colleague.qa", "Colleague", groups=(USER, MANAGER))
        world.add_user(ADMIN_UID, ADMIN_LOGIN, "Admin", groups=(USER, MANAGER, SYSTEM))
        world.add_user(PORTAL_UID, "portal.user", "Portal", share=True, groups=(USER,))
        world.add_user(PLAIN_UID, "plain.user", "Plain", groups=(USER,))
        # archived internal users that exist in every Odoo database (share False, in base.group_user): never granted
        world.add_user(TEMPLATE_UID, TEMPLATE_LOGIN, "Default User (Template)", active=False, groups=(USER,),
                       template=True)
        world.add_user(ODOOBOT_UID, ODOOBOT_LOGIN, "OdooBot", active=False, groups=(USER, SYSTEM))
        self.group = world.add_group(GROUP)
        self.other_group = world.add_group("Finance")
        self.own = world.add_dashboard("Quality v1", self.group, creator=FAKE_UID)
        self.archived_own = world.add_dashboard("Quality v2", self.group, creator=FAKE_UID, active=False)
        self.foreign = world.add_dashboard("Colleague view v1", self.group, creator=COLLEAGUE_UID)
        self.shipped = world.add_dashboard("KPI 1", self.group, creator=False, xmlids=["dksk_kpi.kpi_1"])
        self.shipped_mine = world.add_dashboard("KPI 2", self.group, creator=FAKE_UID, xmlids=["dksk_kpi.kpi_2"])
        self.orphan = world.add_dashboard("Leaver view v1", self.group, creator=False)
        self.elsewhere_mine = world.add_dashboard("Mine elsewhere", self.other_group, creator=FAKE_UID)
        self.elsewhere_shipped = world.add_dashboard("Sales", self.other_group, xmlids=["sales.dashboard"])
        self.own_ids = [self.own, self.archived_own]
        self.foreign_ids = [self.foreign, self.shipped, self.shipped_mine, self.orphan, self.elsewhere_mine,
                            self.elsewhere_shipped]

    def sequence(self):
        return self.fake.sequence()


class StreamCase(IsolatedTestCase):
    """Collects every (stdout, stderr) pair of a test and checks, at the end of EVERY test, that the secret never
    reached them and that no traceback was printed. Subclasses set `SECRETS` (a tuple of the secret texts)."""

    SECRETS = (FAKE_KEY,)

    def setUp(self):
        super().setUp()
        self.streams = []
        self.addCleanup(self.check_streams)

    def check_streams(self):
        for out, err in self.streams:
            for secret in self.SECRETS:
                self.assertNotIn(secret, out)
                self.assertNotIn(secret, err)
            self.assertNotIn("Traceback", out + err)

    def collect(self, runner, argv, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        try:
            code = runner(list(argv), out, err, **kwargs)
        except KeyboardInterrupt:
            self.fail("KeyboardInterrupt escaped main(); stderr so far: %s" % err.getvalue())
        self.streams.append((out.getvalue(), err.getvalue()))
        return code, out.getvalue(), err.getvalue()


def start_fake(case, **kwargs):
    fake = FakeOdoo(**kwargs)
    fake.start()
    case.addCleanup(fake.stop)
    return fake


def use_qaqc_env(url):
    os.environ.update({"ODOO_KPI_URL": url, "ODOO_KPI_DB": FAKE_DB, "ODOO_KPI_LOGIN": FAKE_LOGIN,
                       "ODOO_KPI_API_KEY": FAKE_KEY})


def use_admin_env(url, login=ADMIN_LOGIN, secret=ADMIN_SECRET):
    os.environ.update({"ODOO_KPI_ADMIN_URL": url, "ODOO_KPI_ADMIN_DB": FAKE_DB, "ODOO_KPI_ADMIN_LOGIN": login,
                       "ODOO_KPI_ADMIN_SECRET": secret})


def write_dashboard_file(directory, data=DASHBOARD_BYTES, name="dashboard.json"):
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(data)
    return path
