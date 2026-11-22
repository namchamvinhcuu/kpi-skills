"""A fake Odoo `/jsonrpc` endpoint on 127.0.0.1 (ephemeral port, own thread) for the tests.

Standard library only. It records every request (path, headers, parsed body) and answers according to `mode`:

  ok              normal answers (common.version, common.login, object.execute_kw)
  login_false     common.login answers `false` (wrong credentials as Odoo reports them)
  error_echo      a JSON-RPC error whose data.message ECHOES the API key (the one in the request, else its own)
  error_echo_top  a JSON-RPC error without `data`, whose top-level message echoes the API key
  cf_1010         HTTP 403 with the body `error code: 1010` (Cloudflare edge)
  cf_other        HTTP 403 with the body `error code: 1020`
  http_403_plain  HTTP 403 with an ordinary body
  http_500        HTTP 500 whose body echoes the API key
  non_json        HTTP 200 with an HTML body
  json_list       HTTP 200 with the valid JSON document `[]` (not a JSON-RPC object)
  cf_502          HTTP 502 with the body `error code: 502`
  redirect        `redirect_code` with `Location: redirect_to` (a client that follows it sends a 2nd request)
  deep_json       HTTP 200 with 100000 `[` (json.loads raises RecursionError)

`overrides` (dict) changes the ok answers: `server_version`, `version_result` (the whole version answer), `login`.

No real instance, no real credential: everything here is a placeholder.
"""
import base64
import copy
import datetime
import inspect
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAKE_DB = "fake_db"
FAKE_LOGIN = "qa.user"
FAKE_KEY = "FAKE-KEY-0123abcd-not-a-real-secret"
FAKE_UID = 7


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass

    def _send(self, status, body, content_type="application/json"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        fake = self.server.fake
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = None
        headers = {name.lower(): value for name, value in self.headers.items()}
        with fake.lock:
            fake.requests.append({"path": self.path, "headers": headers, "body": body, "raw": raw})
        if self.path != "/jsonrpc":
            return self._send(404, "not found", "text/plain")
        self._answer(fake, body or {})

    def _answer(self, fake, body):
        params = body.get("params") or {}
        service, method, args = params.get("service"), params.get("method"), params.get("args") or []
        # the key found in the request (login / execute_kw), else the key this fake server was configured with
        key_seen = args[2] if len(args) > 2 and isinstance(args[2], str) else fake.key
        mode = fake.mode

        if mode == "cf_1010":
            return self._send(403, "error code: 1010", "text/plain")
        if mode == "cf_other":
            return self._send(403, "error code: 1020", "text/plain")
        if mode == "cf_502":
            return self._send(502, "error code: 502", "text/plain")
        if mode == "redirect":
            self.send_response(fake.redirect_code)
            self.send_header("Location", fake.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "deep_json":
            return self._send(200, "[" * 100000)
        if mode == "http_403_plain":
            return self._send(403, "Forbidden", "text/plain")
        if mode == "http_500":
            return self._send(500, "Internal Server Error key=%s" % key_seen, "text/plain")
        if mode == "non_json":
            return self._send(200, "<html><body>not json</body></html>", "text/html")
        if mode == "json_list":
            return self._send(200, "[]")
        if mode == "error_echo":
            return self._send(200, json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
                "code": 200, "message": "Odoo Server Error",
                "data": {"name": "odoo.exceptions.AccessDenied", "message": "rejected key %s" % key_seen}}}))
        if mode == "error_echo_top":
            return self._send(200, json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
                "code": 200, "message": "rejected key %s" % key_seen}}))

        # mode == "ok" or "login_false"
        if (service, method) == ("common", "version"):
            result = {"server_version": fake.overrides.get("server_version", "17.0-fake"),
                      "server_serie": "17.0", "protocol_version": 1}
            result = fake.overrides.get("version_result", result)
        elif (service, method) == ("common", "login"):
            good = mode == "ok" and args == [fake.db, fake.login, fake.key]
            result = fake.uid if good else False
            if "login" in fake.overrides:
                result = fake.overrides["login"]
        elif (service, method) == ("object", "execute_kw"):
            if not (len(args) >= 7 and args[0] == fake.db and args[1] == fake.uid and args[2] == fake.key):
                return self._send(200, json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
                    "code": 100, "message": "Odoo Server Error",
                    "data": {"name": "odoo.exceptions.AccessDenied", "message": "Access Denied"}}}))
            if args[3] == "documents.document":
                result = fake.documents_call(args[4], args[5])
            elif fake.world is not None and fake.world.handles(args[3]):
                result = fake.world.call(args[3], args[4], args[5], args[6], args[1])
            else:
                result = {"model": args[3], "method": args[4], "args": args[5], "kwargs": args[6]}
            if isinstance(result, DocumentsError):
                return self._send(200, json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
                    "code": 200, "message": "Odoo Server Error",
                    "data": {"name": result.name, "message": result.message.replace("{key}", key_seen)}}}))
        else:
            result = None
        return self._send(200, json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}))


class DocumentsError:
    """What `FakeOdoo.documents_call` returns to make the server answer with an Odoo error (`{key}` = the API key)."""

    def __init__(self, message, name="odoo.exceptions.UserError"):
        self.message = message
        self.name = name


WORLD_MODELS = frozenset({"spreadsheet.dashboard", "spreadsheet.dashboard.group", "res.users", "ir.model.data"})
WRITE_METHODS = ("create", "write", "unlink")
UNSET = object()                         # "no override": None / False / 0 are answers a test may want to send

# The REAL Odoo 17 signature of every method the world models, with how `api.call_kw` dispatches it (checked against
# odoo/odoo/api.py `_call_kw_model` / `_call_kw_model_create` / `_call_kw_multi`): "model" = `method(recs, *args)` (an
# @api.model method: the first RPC argument is the first real argument), "multi" = a recordset method (the first RPC
# argument is the list of ids, the rest are the arguments), "create" = @api.model_create_multi (one dict or a list).
# `context` is split off the keyword arguments before the call. A call that does not fit is answered with a TypeError
# like the real server, so the fake can never accept a shape that Odoo rejects (step 9: `has_group` with ids).
REAL_API = {
    "search": ("model", lambda self, domain, offset=0, limit=None, order=None: None),                    # models.py:1606
    "search_read": ("model", lambda self, domain=None, fields=None, offset=0, limit=None, order=None,
                    **read_kwargs: None),                                                                # models.py:5765
    "search_count": ("model", lambda self, domain, limit=None: None),                                    # models.py:1588
    "read": ("multi", lambda self, fields=None, load="_classic_read": None),                             # models.py:3563
    "get_metadata": ("multi", lambda self: None),                                                        # models.py:3995
    "create": ("create", lambda self, vals_list: None),                                                  # models.py:4561
    "write": ("multi", lambda self, vals: None),                                                         # models.py:4316
    "unlink": ("multi", lambda self: None),                                                              # models.py:4215
    "has_group": ("model", lambda self, group_ext_id: None),                          # res_users.py:1085 (@api.model)
    "check_object_reference": ("model", lambda self, module, xml_id, raise_on_access_error=False: None),  # ir_model:2217
    "read_group": ("model", lambda self, domain, fields, groupby, offset=0, limit=None, orderby=False,
                   lazy=True: None),                                                                     # models.py:2682
    "fields_get": ("model", lambda self, allfields=None, attributes=None: None),                         # models.py:3443
}


def _tz_rule(winter, summer, first_summer_month, last_summer_month):
    """The offset (hours east of UTC) in force at the START of a local month: `summer` for the months
    first..last (inclusive), else `winter`."""
    return lambda month: summer if first_summer_month <= month <= last_summer_month else winter


# The time zones the fake knows, as {name: offset at the start of a local month -> hours east of UTC}. SMALL and FIXED on
# purpose (stdlib only, Python 3.8 has no `zoneinfo`): a month starts at LOCAL midnight, so only the offset at that
# instant matters. America/Los_Angeles: daylight time lasts from the 2nd Sunday of March to the 1st Sunday of November,
# so the 1st of March is still standard time (-8) and the 1st of November is still daylight time (-7) in every year.
# Europe/Berlin: daylight time lasts from the last Sunday of March to the last Sunday of October: the 1st of April
# .. 1st of October are +2, the 1st of November and the 1st of March are +1. A name that is not here is NOT a zone of
# the fake: like the real server (`pytz.all_timezones_set`) a read_group then ignores it and groups in UTC.
FAKE_TZ_OFFSETS = {
    "UTC": lambda month: 0,
    "Asia/Seoul": lambda month: 9,
    "Asia/Ho_Chi_Minh": lambda month: 7,
    "Pacific/Kiritimati": lambda month: 14,
    "Etc/GMT+12": lambda month: -12,                 # POSIX sign: GMT+12 is twelve hours WEST of UTC
    "America/Los_Angeles": _tz_rule(-8, -7, 4, 11),
    "Europe/Berlin": _tz_rule(1, 2, 4, 10),
}
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")
DATE_FORMATS = {"date": "%Y-%m-%d", "datetime": "%Y-%m-%d %H:%M:%S"}
NUMERIC_KINDS = ("float", "integer")


def local_month_start_utc(tz, year, month):
    """The UTC instant (naive datetime) of the start of the local month `year`-`month` in the zone `tz`; a `tz` that is
    not a zone of the fake (or empty) gives the UTC month start, as the real server does for an unknown name."""
    rule = FAKE_TZ_OFFSETS.get(tz)
    return datetime.datetime(year, month, 1) - datetime.timedelta(hours=rule(month) if rule else 0)


def local_month_of(tz, instant):
    """(year, month) of the local month that holds the UTC instant `instant`: the latest local month start <= it."""
    candidates = [(y, m) for y in (instant.year - 1, instant.year, instant.year + 1) for m in range(1, 13)]
    return max(c for c in candidates if local_month_start_utc(tz, *c) <= instant)


def shape_problem(model, method, args, kwargs):
    """None when `execute_kw(model, method, args, kwargs)` fits the real signature, else the TypeError text Odoo gives."""
    kind, stub = REAL_API[method]
    args, kw = list(args), {k: v for k, v in (kwargs or {}).items() if k != "context"}
    where = "%s.%s()" % (model, method)
    if kind == "multi":
        ids = args[0] if args else None
        if not (isinstance(ids, list) and all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
            return "%s: the first argument of a recordset method is the list of record ids" % where
        args = args[1:]
    positional = [p for p in inspect.signature(stub).parameters.values() if p.kind is p.POSITIONAL_OR_KEYWORD]
    try:
        inspect.signature(stub).bind(None, *args, **kw)
    except TypeError as exc:
        high, low, given = len(positional), len([p for p in positional if p.default is p.empty]), len(args) + 1
        if given > high:
            takes = str(high) if low == high else "from %d to %d" % (low, high)
            return "%s takes %s positional argument%s but %d were given" % (where, takes, "" if high == 1 else "s", given)
        return "%s %s" % (where, exc)
    return None


class DashboardWorld:
    """Plain-dict model of the part of Odoo the publisher and the instance setup talk to (opt in with
    `FakeOdoo.enable_world()`; without it those models keep answering the echo of the request).

    Models: `spreadsheet.dashboard` (create / write / unlink / search / search_read / search_count / read /
    get_metadata, `active` field, `active_test` honoured), `spreadsheet.dashboard.group` (`name` is translate=True: a
    search compares the `lang` of the call context, else en_US because `execute_kw` has an empty context, whatever
    the user's own language is; see `add_group(translations=)`),
    `res.users` (has_group, search_read, search_count, read of `groups_id`, write of `groups_id` commands 3 / 4),
    `ir.model.data` (only `check_object_reference`: every other method answers an AccessError, a QA/QC user cannot read
    that table).

    Every call is first checked against the REAL Odoo 17 signature (`REAL_API`, `shape_problem`): `has_group` is an
    @api.model method (`args == [xmlid]`, evaluated for the AUTHENTICATED uid), the recordset methods (`read`, `write`,
    `unlink`, `get_metadata`) take the list of ids first; a wrong arity / shape is answered with a TypeError.

    The world does NOT enforce ACL or ir.rule: the guards under test are the scripts' own, so a script that forgets one
    must still be seen writing. `create_uid` of a created record is the uid of the caller.

    Knobs (all optional): `ref_override` {xmlid: raw answer of check_object_reference}, `has_group_override`
    {xmlid: raw answer}, `metadata_override` {dashboard id: raw answer | DocumentsError}, `read_override` {dashboard id:
    raw answer | DocumentsError}, `count_override` (raw answer of every dashboard search_count), `create_answer`
    (raw answer of `spreadsheet.dashboard` create, the record is still made), `group_search_override` (raw answer of a
    group `search`), `users_search_override` (raw answer of a `res.users` search_read), `users_read_override` (raw
    answer of a `res.users` read, or a callable `(call number from 1, ids, fields) -> raw answer | UNSET`),
    `dashboard_rows_override` (raw answer of every dashboard search_read), `group_create_answer` (raw answer of a group
    create, the group is still made), `write_side_effect_groups` {group id: True (added) | False (removed)}: what the
    project's `res.users.write` hooks also do to a user whose `groups_id` is written, `context_lang_from_user` (default
    False = the real transport: a search without `lang` compares en_US; True = a client that carries the user's own
    language). `UNSET` = no override.

    KPI data models (reconcile): `add_model(model, {field: kind})` + `add_row(model, **values)` make a plain table (kinds
    `char`, `integer`, `float`, `date` 'YYYY-MM-DD', `datetime` 'YYYY-MM-DD HH:MM:SS' in UTC; an unset field reads
    False like Odoo) that answers `search_read` (domain terms `=`, `!=`, `in`, `<`, `<=`, `>`, `>=`; `limit`; a
    datetime compared with a date-only text is completed like `expression.py`: `00:00:00`, or `23:59:59` for `>` and
    `<=`) and `read_group(domain, fields, ["<date field>:month"], lazy=False, context={"tz": ...})`: one group per
    LOCAL month with data, ascending, holding `__count`, the summed numeric fields, `__range` {"<field>:month":
    {"from", "to"}} (a datetime field: the UTC instants of the local month start and of the next one, from the table
    `FAKE_TZ_OFFSETS`; a tz that is not in the table is ignored, as the real server does) and `__domain`. A row whose
    date is unset forms one group whose `__range` entry is False. `null_sum_answer` is what a sum over only unset
    values reads (Odoo: False); `answer_override` {(model, method): raw answer | DocumentsError} replaces one answer. Every other method, grouping, `order` or `lazy=True` is refused as not modelled.
    `res.users` also answers `read` of `tz` (`add_user(tz=)`, False when unset) and `fields_get(["tz"],
    attributes=["selection"])` (the zone names of `tz_selection`, default the keys of `FAKE_TZ_OFFSETS`;
    `fields_get_override` = a raw answer). The calls that reached the world are in `log`; every HTTP request is in
    `FakeOdoo.requests`.
    """

    GROUP_USER, MANAGER, SYSTEM = "base.group_user", "spreadsheet_dashboard.group_dashboard_manager", "base.group_system"

    def __init__(self):
        self.res_groups = {self.GROUP_USER: 1, self.MANAGER: 2, self.SYSTEM: 3}
        self.users, self.groups, self.dashboards = {}, {}, {}
        self.models = {}                 # KPI data models: {model: {"schema": {field: kind}, "rows": {id: {field: value}}}}
        self.tz_selection = [[name, name] for name in FAKE_TZ_OFFSETS]
        self.fields_get_override = UNSET
        self.answer_override = {}        # {(data model, method): raw answer | DocumentsError} after the shape check
        self.null_sum_answer = False
        self._next = {"group": 10, "dashboard": 100, "row": 1000}
        self.ref_override, self.has_group_override = {}, {}
        self.metadata_override, self.read_override = {}, {}
        self.count_override = self.create_answer = self.group_search_override = self.users_search_override = UNSET
        self.users_read_override = self.dashboard_rows_override = self.group_create_answer = UNSET
        self.write_side_effect_groups = {}
        # False = the real `/jsonrpc` transport (empty context, en_US). True models a client that carries the user's own
        # language (the web client does, `execute_kw` does not): used only to show the scripts' explicit en_US pin holds
        self.context_lang_from_user = False
        self.users_reads = 0            # number of `res.users` read calls so far (for a callable users_read_override)
        self.log = []                    # (model, method, args, kwargs) of every call that reached the world

    # ---- building the scenario
    def add_user(self, uid, login, name=None, share=False, active=True, groups=(), lang="en_US", template=False,
                 tz=False):
        """`template=True` is Odoo's `base.default_user`: a group LINKED on it is copied to every other active internal
        user (`Users.write` -> `_apply_groups_to_existing_employees`, res_users.py), so a write on it is a mass write.
        `tz` is the user's time zone name; Odoo reads an unset one as False."""
        self.users[uid] = {"login": login, "name": name or login.title(), "share": share, "active": active,
                           "groups": {self.res_groups[g] for g in groups}, "lang": lang, "template": template,
                           "tz": tz}
        return uid

    def handles(self, model):
        """Whether this world answers the model (the fixed ones and the KPI data models made by `add_model`)."""
        return model in WORLD_MODELS or model in self.models

    def add_model(self, model, schema):
        """A KPI data model: `schema` {field: kind}, kinds char / integer / float / date / datetime."""
        bad = {name: kind for name, kind in schema.items() if kind not in ("char", "integer", "float") + tuple(DATE_FORMATS)}
        if bad:
            raise ValueError("kinds not modelled: %s" % bad)
        self.models[model] = {"schema": dict(schema), "rows": {}}

    def add_row(self, model, **values):
        """One record (an unset field reads False); a date / datetime must be written in the Odoo text format."""
        table = self.models[model]
        unknown = sorted(set(values) - set(table["schema"]))
        if unknown:
            raise ValueError("fields not in the model: %s" % unknown)
        record = {name: False for name in table["schema"]}
        for name, value in values.items():
            kind = table["schema"][name]
            if kind in DATE_FORMATS and value is not False:
                datetime.datetime.strptime(value, DATE_FORMATS[kind])
            record[name] = value
        record_id = self._take("row")
        table["rows"][record_id] = record
        return record_id

    def add_group(self, name, group_id=None, translations=None):
        """`name` is the stored (en_US) value; `translations` {lang: text} are the other languages' values."""
        group_id = group_id or self._take("group")
        self.groups[group_id] = {"name": name, "translations": dict(translations or {})}
        return group_id

    def add_dashboard(self, name, group_id, creator=False, xmlids=(), active=True, data="seed", dashboard_id=None):
        dashboard_id = dashboard_id or self._take("dashboard")
        self.dashboards[dashboard_id] = {"name": name, "dashboard_group_id": group_id, "create_uid": creator,
                                         "xmlids": list(xmlids), "active": active, "spreadsheet_binary_data": data,
                                         "group_ids": [1]}
        return dashboard_id

    def _take(self, kind):
        self._next[kind] += 1
        return self._next[kind]

    def snapshot(self):
        return copy.deepcopy((self.users, self.groups, self.dashboards))

    def metadata_row(self, dashboard_id):
        """The honest `get_metadata` row of one dashboard (a test copies it and breaks one key)."""
        record = self.dashboards[dashboard_id]
        xmlids = [{"xmlid": x, "noupdate": True} for x in record["xmlids"]]
        return {"id": dashboard_id, "create_uid": self._m2o_user(record["create_uid"]),
                "create_date": "2026-01-01 00:00:00", "write_uid": self._m2o_user(record["create_uid"]),
                "write_date": "2026-01-01 00:00:00", "xmlid": xmlids[-1]["xmlid"] if xmlids else False,
                "noupdate": bool(xmlids), "xmlids": xmlids[::-1]}

    # ---- helpers
    def _m2o_user(self, uid):
        return [uid, self.users[uid]["name"] if uid in self.users else "User %s" % uid] if uid else False

    def _value(self, model, record_id, record, field, lang=None):
        if field == "id":
            return record_id
        if model == "spreadsheet.dashboard.group" and field == "name":      # COALESCE(name->>lang, name->>'en_US')
            return record.get("translations", {}).get(lang, record[field])
        if model == "res.users" and field == "groups_id":
            return sorted(record["groups"])
        return record[field]

    def _matches(self, model, record_id, record, domain, lang=None):
        for leaf in domain:
            field, op, wanted = leaf
            value = self._value(model, record_id, record, field, lang)
            if op == "=":
                ok = value == wanted
            elif op == "!=":
                ok = value != wanted
            elif op == "in":
                ok = bool(set(value) & set(wanted)) if isinstance(value, list) else value in wanted
            elif op in ("ilike", "=ilike", "like"):
                ok = (str(wanted).lower() in str(value).lower()) if op == "ilike" else (
                    str(value).lower() == str(wanted).lower() if op == "=ilike" else str(wanted) in str(value))
            else:
                raise ValueError("operator %s is not modelled" % op)
            if not ok:
                return False
        return True

    def _search(self, model, records, domain, kwargs, uid=None):
        context = (kwargs or {}).get("context") or {}
        active_test = context.get("active_test", True)
        # `/jsonrpc` `object.execute_kw` builds the environment with an EMPTY context (odoo/service/model.py `execute_cr`:
        # `Environment(cr, uid, {})`), so `env.lang` is None and a translated field is compared in en_US whatever the
        # user's own language is; only an explicit `lang` in the call context changes that (security r2, step 9)
        lang = context.get("lang") or (self.users.get(uid, {}).get("lang", "en_US") if self.context_lang_from_user
                                       else "en_US")
        return [rid for rid, rec in sorted(records.items())
                if (not active_test or rec.get("active", True)) and self._matches(model, rid, rec, domain, lang)]

    def _read_dashboard(self, rid, fields):
        record, row = self.dashboards[rid], {"id": rid}
        for field in fields:
            if field == "dashboard_group_id":
                gid = record["dashboard_group_id"]
                row[field] = [gid, self.groups[gid]["name"]] if gid in self.groups else False
            elif field == "create_uid":
                row[field] = self._m2o_user(record["create_uid"])
            elif field in ("name", "active", "spreadsheet_binary_data"):
                row[field] = record[field]
            else:
                raise ValueError("field %s is not modelled" % field)
        return row

    # ---- the RPC entry point
    def call(self, model, method, args, kwargs, uid):
        self.log.append((model, method, copy.deepcopy(args), copy.deepcopy(kwargs)))
        problem = shape_problem(model, method, args, kwargs) if method in REAL_API else None
        if problem:
            return DocumentsError(problem, "builtins.TypeError")
        try:
            if model in self.models:
                return self._data_model(model, method, args, kwargs or {})
            return getattr(self, "_" + model.replace(".", "_"))(method, args, kwargs or {}, uid)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            return DocumentsError("fake world refuses %s.%s: %s" % (model, method, exc))

    # ---- the KPI data models (reconcile)
    @staticmethod
    def _arguments(method, args, kwargs):
        """{parameter: value} of a call bound against the REAL signature, defaults filled in (`shape_problem` has
        already refused a call that does not fit)."""
        stub = REAL_API[method][1]
        bound = inspect.signature(stub).bind(None, *args, **{k: v for k, v in kwargs.items() if k != "context"})
        bound.apply_defaults()
        given = dict(bound.arguments)
        del given["self"]
        return given

    @staticmethod
    def _data_matches(schema, model, record, domain):
        for leaf in domain:
            if not (isinstance(leaf, (list, tuple)) and len(leaf) == 3 and isinstance(leaf[0], str)):
                raise ValueError("the domain term %r is not modelled" % (leaf,))
            field, op, wanted = leaf
            if field not in schema:
                raise ValueError("Invalid field %r on model %r" % (field, model))
            value = record[field]
            if schema[field] == "datetime" and isinstance(wanted, str) and len(wanted) == 10:
                wanted += " 23:59:59" if op in (">", "<=") else " 00:00:00"      # odoo/osv/expression.py:1389
            if op == "=":
                ok = value == wanted
            elif op == "!=":
                ok = value != wanted
            elif op == "in":
                ok = value in wanted
            elif op in ("<", "<=", ">", ">="):
                numbers = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (value, wanted))
                texts = isinstance(value, str) and isinstance(wanted, str)
                if value is False:
                    ok = False                                          # NULL never satisfies an inequality
                elif not (numbers or texts):
                    raise ValueError("cannot compare %r with %r" % (value, wanted))
                else:
                    ok = {"<": value < wanted, "<=": value <= wanted, ">": value > wanted, ">=": value >= wanted}[op]
            else:
                raise ValueError("operator %s is not modelled" % op)
            if not ok:
                return False
        return True

    def _data_ids(self, model, domain):
        table = self.models[model]
        return [rid for rid, record in sorted(table["rows"].items())
                if self._data_matches(table["schema"], model, record, domain)]

    def _data_model(self, model, method, args, kwargs):
        table = self.models[model]
        if (model, method) in self.answer_override:          # a raw answer or a DocumentsError (`{key}` = the API key)
            return self.answer_override[(model, method)]
        if method == "search_read":
            given = self._arguments(method, args, kwargs)
            if given["offset"] or given["order"] not in (None, False, "id"):
                raise ValueError("offset and order are not modelled")
            ids = self._data_ids(model, given["domain"] or [])
            if given["limit"]:
                ids = ids[:given["limit"]]
            fields = given["fields"] or sorted(table["schema"])
            for name in fields:
                if name not in table["schema"]:
                    raise ValueError("Invalid field %r on model %r" % (name, model))
            return [{"id": rid, **{name: table["rows"][rid][name] for name in fields}} for rid in ids]
        if method == "read_group":
            return self._read_group(model, args, kwargs)
        return DocumentsError("unknown method %s" % method)

    def _read_group(self, model, args, kwargs):
        """`read_group(domain, fields, ["<date field>:month"], lazy=False)` as Odoo 17 answers it (models.py:2506-2532)."""
        table = self.models[model]
        schema = table["schema"]
        given = self._arguments("read_group", args, kwargs)
        if given["lazy"] is not False or given["offset"] or given["limit"] or given["orderby"]:
            raise ValueError("only lazy=False without offset, limit and orderby is modelled")
        groupby = given["groupby"]
        spec = groupby[0] if isinstance(groupby, list) and len(groupby) == 1 and isinstance(groupby[0], str) else ""
        name, _, granularity = spec.partition(":")
        if granularity != "month" or schema.get(name) not in DATE_FORMATS:
            raise ValueError("only one '<date field>:month' grouping is modelled")
        measures = []
        for field in given["fields"]:
            if field == "__count":
                continue
            if schema.get(field) not in NUMERIC_KINDS:
                raise ValueError("Invalid field %r on model %r" % (field, model))
            measures.append(field)
        kind, fmt = schema[name], DATE_FORMATS[schema[name]]
        tz = (kwargs.get("context") or {}).get("tz") if kind == "datetime" else None
        buckets, undated = {}, []
        for rid in self._data_ids(model, given["domain"]):
            record = table["rows"][rid]
            if record[name] is False:
                undated.append(record)
                continue
            moment = datetime.datetime.strptime(record[name], fmt)
            key = local_month_of(tz, moment) if kind == "datetime" else (moment.year, moment.month)
            buckets.setdefault(key, []).append(record)
        groups = []
        for (year, month), records in sorted(buckets.items()):
            following = (year + (month == 12), month % 12 + 1)
            if kind == "datetime":
                start, end = local_month_start_utc(tz, year, month), local_month_start_utc(tz, *following)
            else:
                start, end = datetime.datetime(year, month, 1), datetime.datetime(*following, 1)
            groups.append(self._group_row(spec, "%s %d" % (MONTH_NAMES[month - 1], year),
                                          {"from": start.strftime(fmt), "to": end.strftime(fmt)}, records, measures))
        if undated:
            groups.append(self._group_row(spec, False, False, undated, measures))
        return groups

    def _group_row(self, spec, label, bounds, records, measures):
        row = {"__count": len(records), spec: label, "__range": {spec: bounds}, "__domain": [],
               "__context": {"group_by": []}}
        for measure in measures:
            values = [r[measure] for r in records if r[measure] is not False and r[measure] is not None]
            row[measure] = sum(values) if values else self.null_sum_answer
        return row

    def _ir_model_data(self, method, args, kwargs, uid):
        if method != "check_object_reference":
            return DocumentsError("You are not allowed to access 'Records Data' (ir.model.data) records.",
                                  "odoo.exceptions.AccessError")
        xmlid = "%s.%s" % (args[0], args[1])
        if xmlid in self.ref_override:
            return self.ref_override[xmlid]
        return ["res.groups", self.res_groups[xmlid]]

    def _res_users(self, method, args, kwargs, uid):
        if method == "has_group":              # @api.model: args == [xmlid], answered for the authenticated uid
            xmlid = args[0]
            if not isinstance(xmlid, str):      # the real method calls `.split('.')` on it
                raise TypeError("the group external id must be text, not %s" % type(xmlid).__name__)
            if xmlid in self.has_group_override:
                return self.has_group_override[xmlid]
            return self.res_groups[xmlid] in self.users[uid]["groups"]
        if method == "fields_get":             # @api.model: only the selection of `tz` is modelled
            if self.fields_get_override is not UNSET:
                return self.fields_get_override
            given = self._arguments(method, args, kwargs)
            attributes = given["attributes"]
            answer = {}
            for name in given["allfields"] or ["tz"]:
                if name != "tz":
                    raise ValueError("field %s is not modelled" % name)
                info = {"type": "selection", "string": "Timezone", "selection": [list(p) for p in self.tz_selection]}
                answer[name] = {k: v for k, v in info.items() if not attributes or k in attributes}
            return answer
        if method == "search_read" and self.users_search_override is not UNSET:
            return self.users_search_override
        if method in ("search_read", "search_count"):
            ids = self._search("res.users", self.users, args[0], kwargs, uid)
            if method == "search_count":
                return len(ids)
            rows = []
            for rid in ids:
                rows.append({"id": rid, **{f: self._value("res.users", rid, self.users[rid], f) for f in args[1]}})
            return rows
        if method == "read":
            self.users_reads += 1
            fields = args[1] if len(args) > 1 else ["login"]
            override = self.users_read_override
            if callable(override):
                override = override(self.users_reads, args[0], fields)
            if override is not UNSET:
                return override
            return [{"id": rid, **{f: self._value("res.users", rid, self.users[rid], f) for f in fields}}
                    for rid in args[0] if rid in self.users]
        if method == "write":
            for rid in args[0]:
                groups = self.users[rid]["groups"]
                for command in args[1]["groups_id"]:
                    if command[0] == 4:
                        groups.add(command[1])
                        if self.users[rid].get("template"):          # copied to every other active internal user
                            for other_id, other in self.users.items():
                                if other_id != rid and other["active"] and not other["share"] and \
                                        self.res_groups[self.GROUP_USER] in other["groups"]:
                                    other["groups"].add(command[1])
                    elif command[0] == 3:
                        groups.discard(command[1])
                    else:
                        raise ValueError("command %s is not modelled" % command[0])
                for gid, added in self.write_side_effect_groups.items():     # what the project's write hooks add / remove
                    (groups.add if added else groups.discard)(gid)
            return True
        return DocumentsError("unknown method %s" % method)

    def _spreadsheet_dashboard_group(self, method, args, kwargs, uid):
        if method == "search":
            if self.group_search_override is not UNSET:
                return self.group_search_override
            return self._search("spreadsheet.dashboard.group", self.groups, args[0], kwargs, uid)
        if method == "create":
            created = self.add_group(args[0]["name"])
            return created if self.group_create_answer is UNSET else self.group_create_answer
        if method == "unlink":
            for rid in args[0]:
                del self.groups[rid]
            return True
        return DocumentsError("unknown method %s" % method)

    def _spreadsheet_dashboard(self, method, args, kwargs, uid):
        if method in ("search_read", "search", "search_count"):
            if method == "search_read" and self.dashboard_rows_override is not UNSET:
                return self.dashboard_rows_override
            ids = self._search("spreadsheet.dashboard", self.dashboards, args[0], kwargs, uid)
            if method == "search_count":
                return len(ids) if self.count_override is UNSET else self.count_override
            return ids if method == "search" else [self._read_dashboard(rid, args[1]) for rid in ids]
        if method == "read":
            if len(args[0]) == 1 and args[0][0] in self.read_override:
                return self.read_override[args[0][0]]
            return [self._read_dashboard(rid, args[1]) for rid in args[0] if rid in self.dashboards]
        if method == "get_metadata":
            if len(args[0]) == 1 and args[0][0] in self.metadata_override:
                return self.metadata_override[args[0][0]]
            for rid in args[0]:
                if rid not in self.dashboards:
                    return DocumentsError("Record does not exist or has been deleted.", "odoo.exceptions.MissingError")
            return [self.metadata_row(rid) for rid in args[0]]
        if method == "create":
            vals = args[0]
            rid = self.add_dashboard(vals["name"], vals["dashboard_group_id"], creator=uid,
                                     data=vals.get("spreadsheet_binary_data", ""))
            self.dashboards[rid]["group_ids"] = vals.get("group_ids")
            return rid if self.create_answer is UNSET else self.create_answer
        if method == "write":
            for rid in args[0]:
                self.dashboards[rid].update(args[1])
            return True
        if method == "unlink":
            for rid in args[0]:
                del self.dashboards[rid]
            return True
        return DocumentsError("unknown method %s" % method)


class FakeOdoo:
    """Start with `start()` (returns the base url), stop with `stop()`. Usable as a context manager.

    `documents.document` (the model of the baseline intake) is simulated on top of the modes above:

      documents            dict id -> {"name", "handler", "mimetype", "spreadsheet_snapshot"} (use `add_document`)
      clone_mode           answer of `clone_xlsx_into_spreadsheet`: int | dict (`{"res_id": n}`) | dict_id | garbage
                           | zero | error (an Odoo error)
      clone_snapshot       bytes (or text sent verbatim) the clone's snapshot will carry
      snapshot_after_polls the clone's snapshot is empty for that many reads of `spreadsheet_snapshot`, then appears
      unlink_mode          delete (True, record gone) | false (answers False) | error | error_echo (error text holds
                           the API key)
                           (also bool | str_int | negative | source (the xlsx id itself) | none | list)
      read_error_ids       ids whose `read` answers an Odoo error
      read_error_on_polls  0-based indexes of the snapshot reads of a document that answer an Odoo error
      clone_handler        `handler` of the clone as the FIRST read (the freshness check) sees it; anything but
                           "spreadsheet" makes the script refuse the clone ("not a fresh clone")
      clone_poll_handler   `handler` of the clone from the SECOND read on (the polls); None keeps `clone_handler`
      clone_mode "existing"  the clone call answers `existing_clone_id`, the id of ANOTHER document that already
                           exists (nothing is created, `cloned` stays empty)
      cloned               [(xlsx id, clone id)], unlinked: [[ids]] as the server received them

    A document made by the clone call is FRESH: its first `read` answers no snapshot whatever `snapshot_after_polls`
    says and is not counted as a poll (that read is the script's freshness check of the clone); the later reads are
    the polls. A document added with `add_document` has no such first read.
    """

    def __init__(self, db=FAKE_DB, login=FAKE_LOGIN, key=FAKE_KEY, uid=FAKE_UID):
        self.db, self.login, self.key, self.uid = db, login, key, uid
        self.documents = {}
        self.next_document_id = 900
        self.clone_mode = "int"
        self.clone_snapshot = None
        self.snapshot_after_polls = 0
        self.unlink_mode = "delete"
        self.read_error_ids = set()
        self.read_error_on_polls = set()   # 0-based indexes of the snapshot reads (per document) that fail
        self.clone_handler = "spreadsheet"
        self.clone_poll_handler = None
        self.existing_clone_id = None
        self.cloned = []
        self.unlinked = []
        self._ready_after = {}
        self._polls = {}
        self._first_read_done = set()
        self.mode = "ok"
        self.redirect_code = 302
        self.redirect_to = "/jsonrpc2"   # a path on this server, or a full URL of another server
        self.overrides = {}              # server_version, version_result (whole answer), login (answer of common.login)
        self.requests = []
        self.world = None                # a DashboardWorld after `enable_world()`
        self.lock = threading.Lock()
        self._server = None
        self._thread = None

    def enable_world(self):
        """Turn on the dashboard / users / ir.model.data models and return their `DashboardWorld`."""
        self.world = DashboardWorld()
        return self.world

    def start(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02},
                                        daemon=True)
        self._thread.start()
        return self.url

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(5)
            self._server = None

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self._server.server_address[1]

    @staticmethod
    def _packed(snapshot):
        """bytes -> base64 text (as Odoo's Binary field is read over RPC); text is kept verbatim; None -> False."""
        if snapshot is None:
            return False
        return base64.b64encode(snapshot).decode("ascii") if isinstance(snapshot, bytes) else snapshot

    def add_document(self, doc_id, name="doc", handler="spreadsheet", mimetype="application/o-spreadsheet",
                     snapshot=None, ready_after_polls=None):
        """`ready_after_polls=N`: the snapshot reads answer no snapshot N times, then the snapshot (like the clone's
        `snapshot_after_polls`, for a document that was not made by the clone call). The first `read` of such a document
        is not counted as a poll (see the class docstring: it is the freshness read of a clone)."""
        self.documents[doc_id] = {"name": name, "handler": handler, "mimetype": mimetype,
                                  "spreadsheet_snapshot": self._packed(snapshot)}
        if ready_after_polls is not None:
            self._ready_after[doc_id] = ready_after_polls

    def documents_call(self, method, call_args):
        """The answer (or a `DocumentsError`) of one `documents.document` method."""
        if method == "read":
            ids, fields = call_args[0], call_args[1]
            rows = []
            for doc_id in ids:
                if doc_id in self.read_error_ids:
                    return DocumentsError("cannot read document %s" % doc_id)
                if doc_id not in self.documents:
                    continue
                row = {"id": doc_id}
                fresh_read = doc_id in self._ready_after and doc_id not in self._first_read_done
                self._first_read_done.add(doc_id)
                for field in fields:
                    if field == "handler" and not fresh_read and doc_id in self._ready_after \
                            and self.clone_poll_handler is not None:
                        row[field] = self.clone_poll_handler
                    elif field == "spreadsheet_snapshot" and fresh_read:
                        row[field] = False
                    elif field == "spreadsheet_snapshot":
                        seen = self._polls.get(doc_id, 0)
                        self._polls[doc_id] = seen + 1
                        if seen in self.read_error_on_polls:
                            return DocumentsError("transient failure on poll %d" % seen)
                        ready = seen >= self._ready_after.get(doc_id, 0)
                        row[field] = self.documents[doc_id][field] if ready else False
                    else:
                        row[field] = self.documents[doc_id].get(field, False)
                rows.append(row)
            return rows
        if method == "clone_xlsx_into_spreadsheet":
            if self.clone_mode == "error":
                return DocumentsError("cannot clone the document")
            if self.clone_mode == "existing":
                return self.existing_clone_id
            new_id = self.next_document_id
            self.next_document_id += 1
            self.add_document(new_id, name="clone", handler=self.clone_handler, snapshot=self.clone_snapshot)
            self._ready_after[new_id] = self.snapshot_after_polls
            self.cloned.append((call_args[0][0], new_id))
            return {"int": new_id, "dict": {"res_id": new_id}, "dict_id": {"id": new_id},
                    "garbage": "not-an-id", "zero": 0, "bool": True, "str_int": str(new_id), "negative": -new_id,
                    "source": call_args[0][0], "none": None, "list": [new_id]}[self.clone_mode]
        if method == "unlink":
            self.unlinked.append(list(call_args[0]))
            if self.unlink_mode == "error":
                return DocumentsError("you cannot delete this document")
            if self.unlink_mode == "error_echo":
                return DocumentsError("refused for key {key}")
            if self.unlink_mode == "false":
                return False
            for doc_id in call_args[0]:
                self.documents.pop(doc_id, None)
            return True
        return DocumentsError("unknown method %s" % method)

    def execute_calls(self, model, method):
        """`args` (the positional list) of every recorded `execute_kw` for one model/method."""
        return [body["params"]["args"][5] for body in self.calls("object", "execute_kw")
                if body["params"]["args"][3] == model and body["params"]["args"][4] == method]

    def sequence(self):
        """[(model, method, args, kwargs)] of every recorded `execute_kw`, in arrival order (also refused calls)."""
        return [(a[3], a[4], a[5], a[6]) for a in (b["params"]["args"] for b in self.calls("object", "execute_kw"))]

    def write_requests(self, model=None):
        """The `create` / `write` / `unlink` requests received (optionally of one model): what a dry run must not send."""
        return [call for call in self.sequence() if call[1] in WRITE_METHODS and model in (None, call[0])]

    def calls(self, service, method):
        """Parsed bodies of the recorded requests for one service/method."""
        with self.lock:
            return [r["body"] for r in self.requests
                    if r["body"] and r["body"]["params"]["service"] == service
                    and r["body"]["params"]["method"] == method]

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc_info):
        self.stop()
