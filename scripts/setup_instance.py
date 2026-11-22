#!/usr/bin/env python3
"""One-time instance setup for the Odoo ADMINISTRATOR (never for a QA/QC user).

    python scripts/setup_instance.py status
    python scripts/setup_instance.py init [--apply]                       create the dashboard group "KPI (QA/QC)"
    python scripts/setup_instance.py add-user LOGIN [LOGIN ...] [--apply]      let users publish dashboards (decision D2)
    python scripts/setup_instance.py remove-user LOGIN [LOGIN ...] [--apply]   take that right away again
    python scripts/setup_instance.py list-orphans                         dashboards whose creator no longer exists
    python scripts/setup_instance.py remove-group [--apply]               delete the group when it holds no dashboard
    (nothing is written without --apply; every change made is printed on stdout as a `CHANGE:` line, keep them)

The administrator's credential comes from the environment (ODOO_KPI_ADMIN_URL, ODOO_KPI_ADMIN_DB,
ODOO_KPI_ADMIN_LOGIN, ODOO_KPI_ADMIN_SECRET: a password or an API key) or, for what is missing, from an interactive
prompt (the secret is not echoed). It is never read from the QA/QC config file, never written anywhere, and a login
that is not a system administrator AND a dashboard manager is refused (so a QA/QC config cannot run this tool).
A non-local host needs `--confirm-instance <full host name>` typed by the human, like every script of this plugin.
Standard library only.
"""
import argparse
import getpass
import os
import sys

import intake_xlsx
import odoo_rpc
from publish_dashboard import DASHBOARD_MODEL, GROUP_LOOKUP, GROUP_MODEL, GROUP_NAME, NO_ARCHIVED

ADMIN_ENV = {"url": "ODOO_KPI_ADMIN_URL", "db": "ODOO_KPI_ADMIN_DB", "login": "ODOO_KPI_ADMIN_LOGIN",
             "secret": "ODOO_KPI_ADMIN_SECRET"}  # secret-allow: the NAME of an environment variable
MANAGER_XMLID = ("spreadsheet_dashboard", "group_dashboard_manager")
MANAGER_GROUP = "spreadsheet_dashboard.group_dashboard_manager"
MAX_LOGINS = 50


class SetupError(Exception):
    """A refusal or a problem the administrator can act on (message is safe to print)."""


def admin_config(env=None, interactive=None, ask=input, ask_secret=getpass.getpass):
    """The administrator's `odoo_rpc.Config`: environment first, then prompts for what is missing (never a file)."""
    env = os.environ if env is None else env
    interactive = sys.stdin.isatty() if interactive is None else interactive
    values = {}
    for key, name in ADMIN_ENV.items():
        value = (env.get(name) or "").strip()
        if not value:
            if not interactive:
                raise SetupError("%s is not set and there is no terminal to ask: set ODOO_KPI_ADMIN_URL, _DB, _LOGIN "
                                 "and _SECRET (never put them in the QA/QC config file)" % name)
            value = (ask_secret("Administrator password or API key (not shown): ") if key == "secret"
                     else ask("Administrator %s: " % key)).strip()
        if not value:
            raise SetupError("the administrator %s is empty" % key)
        values[key] = value
    return odoo_rpc.Config(values["url"].rstrip("/"), values["db"], values["login"], values["secret"], source="admin")


def require_admin(client):
    """Refuse unless this login is a system administrator and a dashboard manager."""
    for xmlid, what in (("base.group_system", "an Odoo administrator (Settings)"),
                        (MANAGER_GROUP, "a spreadsheet dashboard manager")):
        # `has_group` is an @api.model method: the server evaluates it for the authenticated uid (no ids are passed)
        if client.execute("res.users", "has_group", [xmlid]) is not True:
            raise SetupError("this login is not %s: this tool is for the Odoo administrator only" % what)


def manager_group_id(client):
    result = client.execute("ir.model.data", "check_object_reference", list(MANAGER_XMLID))
    if not (isinstance(result, list) and len(result) == 2 and isinstance(result[1], int) and result[1] > 0
            and not isinstance(result[1], bool)):
        raise SetupError("cannot resolve the dashboard manager group %s" % MANAGER_GROUP)
    return result[1]


def group_ids(client):
    ids = client.execute(GROUP_MODEL, "search", [[["name", "=", GROUP_NAME]]], GROUP_LOOKUP)
    if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise SetupError("the server answered the group lookup in an unexpected shape")
    return ids


def one_group(client):
    ids = group_ids(client)
    if len(ids) > 1:
        raise SetupError("%d dashboard groups are named %r: remove the duplicates by hand first" % (len(ids), GROUP_NAME))
    return ids[0] if ids else None


def users_by_login(client, logins, allow_archived=False):
    """{login: user id} for exactly the given logins (every one must exist and be an internal user).

    Archived users are found only when `allow_archived` (revoking a leaver's right is legitimate). Granting is never
    allowed on them: Odoo's template user `default` is an archived internal user, and a group written on it is copied
    to EVERY internal user (and the copy is not undone by removing it again)."""
    if not logins or len(logins) > MAX_LOGINS or len(set(logins)) != len(logins):
        raise SetupError("give between 1 and %d different logins" % MAX_LOGINS)
    found = {}
    for login in logins:
        rows = client.execute("res.users", "search_read", [[["login", "=", login]], ["login", "share"]],
                              NO_ARCHIVED if allow_archived else {})
        if not (isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)):
            raise SetupError("no single %suser with the login %r" % ("" if allow_archived else "active ",
                                                                    intake_xlsx._short(login, 40)))
        if rows[0].get("share") is not False:     # missing / None is not "internal": refuse
            raise SetupError("%r is a portal / external user (or the server did not say): only internal users can "
                             "publish" % login)
        uid = rows[0].get("id")
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
            raise SetupError("the server answered the lookup of %r in an unexpected shape" % intake_xlsx._short(login, 40))
        found[login] = uid
    return found


def cmd_status(client, args, out):
    group = one_group(client)
    print("group %r: %s" % (GROUP_NAME, "missing" if group is None else "id %d" % group), file=out)
    mgr = manager_group_id(client)
    members = client.execute("res.users", "search_count", [[["groups_id", "in", [mgr]]]], NO_ARCHIVED)
    print("users in the dashboard manager group: %s" % members, file=out)
    return 0


def cmd_init(client, args, out):
    group = one_group(client)
    if group is not None:
        print("group %r already exists: id %d (nothing to do)" % (GROUP_NAME, group), file=out)
        return 0
    if not args.apply:
        print("dry run: would create the dashboard group %r, add --apply" % GROUP_NAME, file=out)
        return 0
    group = client.execute(GROUP_MODEL, "create", [{"name": GROUP_NAME}])
    if not isinstance(group, int) or isinstance(group, bool) or group <= 0:
        raise SetupError("the server did not return the id of the new group: check it in Odoo before running init again")
    print("CHANGE: created dashboard group %r id %s" % (GROUP_NAME, group), file=out)
    return 0


def _groups_of(client, uid):
    """The set of group ids of one user, read now (refuse an answer of an unexpected shape)."""
    rows = client.execute("res.users", "read", [[uid], ["groups_id"]], NO_ARCHIVED)
    groups = rows[0].get("groups_id") if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict) else None
    if not isinstance(groups, list) or not all(isinstance(g, int) and not isinstance(g, bool) for g in groups):
        raise SetupError("cannot read the groups of user id %d (unexpected answer): check that user in Odoo" % uid)
    return set(groups)


def _change_members(client, args, out, add):
    mgr = manager_group_id(client)
    users = users_by_login(client, args.logins, allow_archived=not add)
    before = {uid: _groups_of(client, uid) for uid in users.values()}     # every read before the first write
    for login, uid in sorted(users.items()):
        verb = "add" if add else "remove"
        if (mgr in before[uid]) == add:
            print("%s is already %s the dashboard manager group: no change" % (login, "in" if add else "outside"), file=out)
            continue
        if not args.apply:
            print("dry run: would %s %s %s the dashboard manager group" % (verb, login, "to" if add else "from"), file=out)
            continue
        client.execute("res.users", "write", [[uid], {"groups_id": [(4 if add else 3, mgr)]}])
        print("CHANGE: %s %s (id %d) %s the dashboard manager group (id %d)"
              % ("added" if add else "removed", login, uid, "to" if add else "from", mgr), file=out)
        # other modules hook `res.users.write` and may change more than the payload says: report it, never hide it
        after = _groups_of(client, uid)
        for gid in sorted((after ^ before[uid]) - {mgr}):
            print("CHANGE: side effect on %s (id %d): group id %d %s" % (login, uid, gid,
                  "added" if gid in after else "removed"), file=out)
        if (mgr in after) != add:                 # a hook undid the write: the CHANGE line above must not stand alone
            print("NOT APPLIED: the write for %s (id %d) was sent but the user is still %s the dashboard manager group "
                  "(a hook undid it): the CHANGE line above did not stay" % (login, uid, "outside" if add else "in"),
                  file=out)
            raise SetupError("the write for %s was sent but the user is still %s the dashboard manager group: check "
                             "that user in Odoo" % (login, "outside" if add else "in"))
    return 0


def cmd_add_user(client, args, out):
    return _change_members(client, args, out, True)


def cmd_remove_user(client, args, out):
    return _change_members(client, args, out, False)


def cmd_list_orphans(client, args, out):
    group = one_group(client)
    if group is None:
        print("group %r is missing: nothing to list" % GROUP_NAME, file=out)
        return 0
    rows = client.execute(DASHBOARD_MODEL, "search_read", [[["dashboard_group_id", "=", group]],
                          ["name", "create_uid", "active"]], {"context": {"active_test": False}, "order": "id"})
    if not isinstance(rows, list):
        raise SetupError("the server answered the dashboard list in an unexpected shape")
    for row in rows:
        if isinstance(row, dict) and "create_uid" in row and not row["create_uid"]:
            print("%d\t%s\t%s" % (row["id"], "active" if row.get("active", True) else "archived",
                                  intake_xlsx._short(row.get("name") or "")), file=out)
    return 0


def cmd_remove_group(client, args, out):
    group = one_group(client)
    if group is None:
        print("group %r does not exist (nothing to do)" % GROUP_NAME, file=out)
        return 0
    held = client.execute(DASHBOARD_MODEL, "search_count", [[["dashboard_group_id", "=", group]]], NO_ARCHIVED)
    if not isinstance(held, int) or isinstance(held, bool):
        raise SetupError("cannot tell whether the group holds dashboards (unexpected answer): nothing deleted")
    if held:
        raise SetupError("the group still holds %s dashboard(s): remove them first (publish_dashboard.py delete-own, or "
                         "by hand in Odoo)" % held)
    if not args.apply:
        print("dry run: would delete the empty dashboard group %r (id %d), add --apply" % (GROUP_NAME, group), file=out)
        return 0
    client.execute(GROUP_MODEL, "unlink", [[group]])
    print("CHANGE: deleted dashboard group %r id %d" % (GROUP_NAME, group), file=out)
    return 0


COMMANDS = {"status": cmd_status, "init": cmd_init, "add-user": cmd_add_user, "remove-user": cmd_remove_user,
            "list-orphans": cmd_list_orphans, "remove-group": cmd_remove_group}


def build_parser():
    parser = argparse.ArgumentParser(prog="setup_instance.py", description=__doc__.split("\n\n")[0], allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        cmd = sub.add_parser(name, allow_abbrev=False)
        cmd.add_argument("--confirm-instance", help="the full host name typed by the human (non-local hosts)")
        if name in ("init", "add-user", "remove-user", "remove-group"):
            cmd.add_argument("--apply", action="store_true", help="really write (the default is a dry run)")
        if name in ("add-user", "remove-user"):
            cmd.add_argument("logins", nargs="+", metavar="LOGIN")
    return parser


def run(args, out, err, client_factory=odoo_rpc.Client, config=None):
    config = config or admin_config()
    client = client_factory(config, confirm_instance=args.confirm_instance)
    client.login()
    require_admin(client)
    return COMMANDS[args.command](client, args, out)


def main(argv=None, out=None, err=None, **kwargs):
    out, err = out or sys.stdout, err or sys.stderr
    for stream in (out, err):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (ValueError, OSError):
                pass
    args = build_parser().parse_args(argv)
    try:
        return run(args, out, err, **kwargs)
    except (SetupError, odoo_rpc.ConfigError, odoo_rpc.GuardError) as exc:
        print("error: %s" % exc, file=err)
        return 2
    except odoo_rpc.RpcError as exc:
        print("error: %s" % exc, file=err)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("interrupted", file=err)
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort: one line, no traceback, never the exception text
        print("error: unexpected %s" % type(exc).__name__, file=err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
