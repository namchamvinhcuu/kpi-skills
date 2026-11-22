#!/usr/bin/env python3
"""Publisher: put a dashboard JSON on the Odoo instance as the QA/QC user's OWN `spreadsheet.dashboard`.

    python scripts/publish_dashboard.py list
    python scripts/publish_dashboard.py publish --dashboard dashboard.json --name "My KPI" [--apply]
    python scripts/publish_dashboard.py publish --dashboard dashboard.json --replace ID --confirm-replace ID [--apply]
    python scripts/publish_dashboard.py archive --id ID [--apply]
    python scripts/publish_dashboard.py delete-own --id ID [--apply]
    (every command also takes --config PATH and --confirm-instance NAME; nothing is written without --apply)

Ownership. This plugin only ever creates, changes, archives or deletes dashboards that are its user's OWN. A dashboard
is own when ALL of these hold, read from the server just before every write: (1) its group (`dashboard_group_id`) is
the ONE group named exactly "KPI (QA/QC)" (0 or more than 1 such groups: refuse); (2) `get_metadata()` returns no
external id (a dashboard shipped by a module has one; the metadata call reads `ir.model.data` with sudo, so it works
for a user who cannot read that table); (3) its creator is this login. A colleague's dashboard and every shipped
dashboard are therefore foreign. The guard is SOFT: a user in the dashboard-manager group can still write anything
through other tools (decision D2 of the Plan). The publisher never writes a dashboard group (an admin creates it with
setup_instance.py) and never writes a record it did not create.

Versions: `publish` creates a NEW dashboard named "<name> v<N>" (N = 1 + the highest version of your own dashboards of
that name, archived ones included), so a re-publish never replaces what people edited by hand. `--replace ID` rewrites
the data of one of your own dashboards in place and needs `--confirm-replace ID` with the same id: the snapshot and the
revisions people saved on it are lost.
The final line of a successful `publish --apply` on stdout is the created id; everything else goes to stderr.
Standard library only.
"""
import argparse
import base64
import re
import sys

import bind_kpi
import intake_xlsx
import odoo_rpc
import validate_formulas

GROUP_NAME = "KPI (QA/QC)"
GROUP_MODEL, DASHBOARD_MODEL = "spreadsheet.dashboard.group", "spreadsheet.dashboard"
NAME_LIMIT = 100
NO_ARCHIVED = {"context": {"active_test": False}}
# the group name is a translated field: say which language the `=` compares (the JSON-RPC context is empty by default,
# so en_US is what a search would use anyway; stated here as defence in depth)
GROUP_LOOKUP = {"context": {"active_test": False, "lang": "en_US"}}
VERSION_SUFFIX = re.compile(r" v(\d+)$")


class PublishError(Exception):
    """A refusal or a problem the user can act on (message is safe to print)."""


# ---------------------------------------------------------------- ownership

def find_group(client):
    """The id of THE dashboard group named exactly GROUP_NAME; refuse when it is missing or ambiguous."""
    ids = client.execute(GROUP_MODEL, "search", [[["name", "=", GROUP_NAME]]], GROUP_LOOKUP)
    if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], int) or isinstance(ids[0], bool):
        count = len(ids) if isinstance(ids, list) else "?"
        raise PublishError("expected exactly one dashboard group named %r but found %s: ask the Odoo administrator to "
                           "run setup_instance.py (or to remove the duplicate)" % (GROUP_NAME, count))
    return ids[0]


def _creator_id(value):
    """The user id out of a many2one value as JSON-RPC returns it ([id, name]) or False."""
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], int) and not isinstance(value[0], bool):
        return value[0]
    return None


def foreign_reason(client, group_id, dashboard_id):
    """Why `dashboard_id` is NOT the user's own dashboard, or None when it is (all three rules, read now)."""
    rows = client.execute(DASHBOARD_MODEL, "read", [[dashboard_id], ["dashboard_group_id", "name"]], NO_ARCHIVED)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return "it does not exist (or you cannot see it)"
    group = rows[0].get("dashboard_group_id")
    if _creator_id(group) != group_id:
        return "it is not in the group %r" % GROUP_NAME
    meta = client.execute(DASHBOARD_MODEL, "get_metadata", [[dashboard_id]], NO_ARCHIVED)
    if not isinstance(meta, list) or len(meta) != 1 or not isinstance(meta[0], dict):
        return "its metadata could not be read"
    info = meta[0]
    if info.get("xmlids") != [] or info.get("xmlid"):
        return "it ships with a module (it has an external id)"
    if _creator_id(info.get("create_uid")) != client.uid:
        return "it was created by someone else"
    return None


def require_own(client, group_id, dashboard_id):
    reason = foreign_reason(client, group_id, dashboard_id)
    if reason:
        raise PublishError("dashboard %d is not yours to change: %s" % (dashboard_id, reason))


def own_dashboards(client, group_id):
    """[{id, name, active}] of the user's own dashboards (archived ones included), oldest first."""
    rows = client.execute(DASHBOARD_MODEL, "search_read", [[["dashboard_group_id", "=", group_id]],
                          ["name", "active"]], {"context": {"active_test": False}, "order": "id"})
    if not isinstance(rows, list):
        raise PublishError("the server answered the dashboard list in an unexpected shape")
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), int)
            and foreign_reason(client, group_id, row["id"]) is None]


def next_version(own, name):
    """1 + the highest `<name> vN` among the own dashboards (a plain `<name>` counts as v0)."""
    highest = -1
    for row in own:
        title = row.get("name") or ""
        match = VERSION_SUFFIX.search(title)
        if title == name:
            highest = max(highest, 0)
        elif match and title[:match.start()] == name:
            highest = max(highest, int(match.group(1)))
    return highest + 1 if highest >= 0 else 1


# ---------------------------------------------------------------- commands

def load_dashboard(path):
    data, document = bind_kpi._read_json(path, "the dashboard file")
    if not isinstance(document, dict) or not isinstance(document.get("sheets"), list) or not document["sheets"]:
        raise PublishError("the dashboard file is not a spreadsheet JSON (no sheets)")
    problems = validate_formulas.unknown_function_problems(document, validate_formulas.load_frozen())
    if problems:
        raise PublishError("; ".join(problems))
    # send what was validated: a UTF-8 BOM is accepted by the reader above but Odoo's json.loads refuses it
    return base64.b64encode(data.decode("utf-8-sig").encode("utf-8")).decode("ascii")


def user_group_id(client):
    """The id of `base.group_user` (the shipped dashboards are visible to it), as a user may resolve it."""
    result = client.execute("ir.model.data", "check_object_reference", ["base", "group_user"])
    if not (isinstance(result, list) and len(result) == 2 and isinstance(result[1], int) and result[1] > 0
            and not isinstance(result[1], bool)):
        raise PublishError("cannot resolve the internal-user group: ask the Odoo administrator")
    return result[1]


def cmd_list(client, args, out, err):
    group_id = find_group(client)
    for row in own_dashboards(client, group_id):
        print("%d\t%s\t%s" % (row["id"], "active" if row.get("active", True) else "archived",
                              intake_xlsx._short(row.get("name") or "")), file=out)
    return 0


def cmd_publish(client, args, out, err):
    payload = load_dashboard(args.dashboard)
    group_id = find_group(client)
    if args.replace is not None:
        if args.confirm_replace != args.replace:
            raise PublishError("--replace %d needs --confirm-replace %d: the snapshot and the revisions saved on that "
                               "dashboard are lost" % (args.replace, args.replace))
        require_own(client, group_id, args.replace)
        print("will rewrite dashboard %d in place (its saved revisions are lost)" % args.replace, file=err)
        if args.apply:
            client.execute(DASHBOARD_MODEL, "write", [[args.replace], {"spreadsheet_binary_data": payload}])
            print(args.replace, file=out)
        else:
            print("dry run: nothing written, add --apply", file=err)
        return 0
    name = bind_kpi._text(args.name, "--name", NAME_LIMIT - 6)
    full_name = "%s v%d" % (name, next_version(own_dashboards(client, group_id), name))
    print("will create the dashboard %r in the group %r (visible to EVERY internal user; the text of the Excel design "
          "area is copied as it is)" % (full_name, GROUP_NAME), file=err)
    if not args.apply:
        print("dry run: nothing written, add --apply", file=err)
        return 0
    created = client.execute(DASHBOARD_MODEL, "create", [{
        "name": full_name, "dashboard_group_id": group_id, "group_ids": [(6, 0, [user_group_id(client)])],
        "spreadsheet_binary_data": payload}])
    if not isinstance(created, int) or isinstance(created, bool) or created <= 0:
        raise PublishError("the server did not return the id of the new dashboard")
    print(created, file=out)
    return 0


def cmd_archive(client, args, out, err):
    group_id = find_group(client)
    require_own(client, group_id, args.id)
    print("will archive dashboard %d" % args.id, file=err)
    if args.apply:
        client.execute(DASHBOARD_MODEL, "write", [[args.id], {"active": False}])
    else:
        print("dry run: nothing written, add --apply", file=err)
    return 0


def cmd_delete_own(client, args, out, err):
    group_id = find_group(client)
    require_own(client, group_id, args.id)
    print("will DELETE dashboard %d (it cannot be restored)" % args.id, file=err)
    if args.apply:
        client.execute(DASHBOARD_MODEL, "unlink", [[args.id]])
    else:
        print("dry run: nothing deleted, add --apply", file=err)
    return 0


COMMANDS = {"list": cmd_list, "publish": cmd_publish, "archive": cmd_archive, "delete-own": cmd_delete_own}


def _positive(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive id")
    return value


def build_parser():
    parser = argparse.ArgumentParser(prog="publish_dashboard.py", description=__doc__.split("\n\n")[0],
                                     allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        cmd = sub.add_parser(name, allow_abbrev=False)
        cmd.add_argument("--config", help="path of the config file (default: see odoo_rpc.py config-path)")
        cmd.add_argument("--confirm-instance", help="the full host name typed by the user (non-local hosts)")
        if name != "list":
            cmd.add_argument("--apply", action="store_true", help="really write (the default is a dry run)")
        if name == "publish":
            cmd.add_argument("--dashboard", required=True, help="dashboard JSON written by bind_kpi.py")
            cmd.add_argument("--name", help="dashboard name (a version suffix is added)")
            cmd.add_argument("--replace", type=_positive, help="rewrite this own dashboard in place")
            cmd.add_argument("--confirm-replace", type=_positive, help="the same id again")
        if name in ("archive", "delete-own"):
            cmd.add_argument("--id", required=True, type=_positive)
    return parser


def run(args, out, err, client_factory=odoo_rpc.Client):
    if args.command == "publish" and (args.replace is None) == (args.name is None):
        raise PublishError("give --name (a new version) or --replace ID (rewrite in place), not both and not neither")
    client = client_factory(odoo_rpc.load_config(path=args.config), confirm_instance=args.confirm_instance)
    client.login()
    return COMMANDS[args.command](client, args, out, err)


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
    except (PublishError, odoo_rpc.ConfigError, odoo_rpc.GuardError, bind_kpi.BindError, intake_xlsx.IntakeError,
            validate_formulas.ValidateError) as exc:
        print("error: %s" % exc, file=err)
        return 2
    except odoo_rpc.RpcError as exc:
        print("error: %s" % exc, file=err)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=err)
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort: one line, no traceback, never the exception text
        print("error: unexpected %s" % type(exc).__name__, file=err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
