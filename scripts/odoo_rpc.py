"""JSON-RPC client of the Odoo KPI skills (standard library only, Python 3.8+).

Safety rules of this client (each one has a test):
- credentials come from a private config file or from environment variables, never from the command line;
- the config file must be private to the current user: POSIX = mode without group/other bits and owned by the user;
  Windows = its DACL (read as SDDL, SIDs only, so the check does not depend on the display language) holds allow
  entries only for the current user, SYSTEM and Administrators;
- a plain-http URL is refused unless the host is local; any other host needs `--confirm-instance <host>` typed by the
  user (the FULL host name);
- `init-config` writes the config file for the human only: a terminal is required, Claude Code's environment is refused,
  the API key is typed hidden, and the file is written through a private temp file that is checked before it replaces
  the target;
- every request carries its own honest User-Agent (the default `Python-urllib` is blocked by Cloudflare, error 1010);
- redirects are never followed, the answer size is capped, and the API key is never printed: error texts are cleaned
  (key redacted, control characters removed) and chained exceptions are dropped.
"""
import argparse
import configparser
import csv
import getpass
import http.client
import io
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import warnings

import kpi_catalogs

VERSION = "0.1.2"
USER_AGENT = "odoo-kpi-skills/" + VERSION
ENV_NAMES = {"url": "ODOO_KPI_URL", "db": "ODOO_KPI_DB", "login": "ODOO_KPI_LOGIN",
             "api_key": "ODOO_KPI_API_KEY"}  # secret-allow: names of environment variables, not a value
CONFIG_ENV = "ODOO_KPI_CONFIG"
PROJECT_ENV = "ODOO_KPI_PROJECT"     # optional, not part of the four credentials: which shipped source catalog to use
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
LOCAL_SUFFIXES = (".test", ".localhost")        # `.local` is mDNS (spoofable on a LAN): not trusted
MAX_RESPONSE = 64 * 1024 * 1024                  # step 6 reads large spreadsheet snapshots
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*|[0-9a-f:.]+")
_BAD_URL_CHARS = re.compile(r"[\\\s\x00-\x1f\x7f?#]")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# a version shape (major.minor first, at most 40 characters): a 40-hex API key does not fit
_VERSION_TEXT = re.compile(r"(?=.{1,40}\Z)\d+\.\d+[0-9A-Za-z.+_ ~-]*")
# SDDL SIDs allowed on the config file besides the current user: SYSTEM, Administrators, Owner Rights, Creator Owner
WINDOWS_ALLOWED_SIDS = frozenset({"SY", "BA", "OW", "CO", "S-1-5-18", "S-1-5-32-544", "S-1-3-4", "S-1-3-0"})
_ALLOW_ACE_TYPES = frozenset({"A", "OA", "XA", "ZA"})
_DENY_OR_AUDIT_ACE_TYPES = frozenset({"D", "OD", "XD", "AU", "OU", "XU", "AL", "ML", "RA", "SP", "TL"})
WINDOWS_FALLBACK = "; use the ODOO_KPI_* environment variables instead of a config file"


class ConfigError(Exception):
    """The configuration is missing or unusable."""


class GuardError(Exception):
    """A safety guard refused the operation."""


class RpcError(Exception):
    """The server (or the network) did not answer as expected."""


class Config:
    """URL, database, login and API key of one instance. `repr` / `str` never show the key. `project` is the RAW optional
    project text of the config (never validated here, never printed: `project_status` validates it when it is used)."""

    __slots__ = ("url", "db", "login", "api_key", "source", "project")

    def __init__(self, url, db, login, api_key, source="file", project=None):
        self.url, self.db, self.login, self.api_key, self.source = url, db, login, api_key, source
        self.project = project

    def __repr__(self):
        return "Config(url=%r, db=%r, login=%r, api_key=***)" % (self.url, self.db, self.login)

    __str__ = __repr__


# ---------------------------------------------------------------- config location and privacy

def config_path(env=None, os_name=None):
    """Where the config file lives: ODOO_KPI_CONFIG, else %APPDATA%\\odoo-kpi (Windows) or ~/.config/odoo-kpi."""
    env = os.environ if env is None else env
    os_name = os.name if os_name is None else os_name
    if env.get(CONFIG_ENV):
        return env[CONFIG_ENV]
    if os_name == "nt":
        base = env.get("APPDATA") or ntpath.join(env.get("USERPROFILE", "~"), "AppData", "Roaming")
        return ntpath.join(base, "odoo-kpi", "config.ini")
    base = env.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "odoo-kpi", "config.ini")


def _system32(name, env=None):
    """Absolute path of a Windows system tool: never found through the current directory or PATH."""
    env = os.environ if env is None else env
    return ntpath.join(env.get("SystemRoot") or env.get("windir") or "C:\\Windows", "System32", name)


def _read_sddl(path, run=None):
    """DACL of `path` as an SDDL string through `icacls /save` (SIDs only, whatever the display language)."""
    run = run or subprocess.run
    folder = tempfile.mkdtemp(prefix="odoo-kpi-")
    target = os.path.join(folder, "acl.txt")
    try:
        done = run([_system32("icacls.exe"), os.path.abspath(path), "/save", target],
                   capture_output=True, timeout=15)
        if done.returncode != 0:
            raise GuardError("cannot read the permissions of the config file (icacls failed)" + WINDOWS_FALLBACK)
        with open(target, "rb") as handle:
            raw = handle.read()
    except (OSError, subprocess.SubprocessError):
        raise GuardError("cannot read the permissions of the config file (icacls failed)" + WINDOWS_FALLBACK) from None
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    try:
        return raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", "replace")
    except UnicodeDecodeError:
        raise GuardError("cannot read the permissions of the config file (unreadable icacls output)"
                         + WINDOWS_FALLBACK) from None


def _current_user_sid(run=None):
    """SID of the current Windows user (`whoami /user`, parsed as ASCII: locale independent)."""
    run = run or subprocess.run
    try:
        done = run([_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        raise GuardError("cannot find the current user (whoami failed)" + WINDOWS_FALLBACK) from None
    sid = None
    if done.returncode == 0:
        # `"DOMAIN\user","S-1-5-21-...-1001"`: read as latin-1 (never fails, the SID is ASCII) and take CSV field 2,
        # so an account whose name looks like a SID is not mistaken for the SID
        rows = list(csv.reader((done.stdout or b"").decode("latin-1").splitlines()))
        if rows and len(rows[0]) >= 2 and re.fullmatch(r"S-1-\d+(?:-\d+)+", rows[0][1].strip()):
            sid = rows[0][1].strip()
    if not sid:
        raise GuardError("cannot find the current user (whoami failed)" + WINDOWS_FALLBACK)
    return sid


def _dacl_entries(sddl):
    """ACE strings of the DACL: the LAST `D:` + optional flags (P, AI, AR) that is followed by `(`, then every
    `(`...`)` group at depth 1, up to the end of that line. A file-name line such as `D:\\dir\\config.ini` is not a
    DACL (a backslash follows its `D:`), and `O:..G:..D:..` on one line is fine.

    Anything this scanner does not understand (a conditional ACE with nested parentheses, a quote, unbalanced
    parentheses, text after the ACE list that is not another SDDL section) raises GuardError: fail closed.
    """
    if '"' in (sddl or ""):
        # a quote only occurs in conditional / resource-attribute entries, which this check refuses by design; it also
        # stops a quoted fake `D:(...)` inside a SACL string from being read as the DACL
        raise GuardError("the config file has a permission entry this check does not understand" + WINDOWS_FALLBACK)
    found = list(re.finditer(r"D:(?:NO_ACCESS_CONTROL|P|AI|AR)*(?=\()", sddl or ""))
    if not found:
        return []
    text = re.match(r"[^\r\n]*", sddl[found[-1].end():]).group(0)
    entries, i = [], text.index("(")
    while i < len(text) and text[i] == "(":
        depth, j = 1, i + 1
        while j < len(text) and depth:
            if text[j] == "(":
                raise GuardError("the config file has a permission entry this check does not understand"
                                 + WINDOWS_FALLBACK)
            depth -= text[j] == ")"
            j += 1
        if depth:
            raise GuardError("the permissions of the config file could not be read (unbalanced entry)"
                             + WINDOWS_FALLBACK)
        entries.append(text[i + 1:j - 1])
        i = j
    rest = text[i:].strip()
    if rest and not rest.startswith(("S:", "O:", "G:")):
        raise GuardError("the config file has a permission entry this check does not understand" + WINDOWS_FALLBACK)
    return entries


def check_sddl(sddl, user_sid):
    """Raise GuardError unless every allow entry of the DACL belongs to the user, SYSTEM or Administrators."""
    entries = _dacl_entries(sddl)
    if not entries:
        raise GuardError("could not read any permission entry of the config file" + WINDOWS_FALLBACK)
    allowed = WINDOWS_ALLOWED_SIDS | {user_sid.upper()}
    for entry in entries:
        fields = entry.split(";")
        ace_type = fields[0].strip().upper()
        if ace_type in _DENY_OR_AUDIT_ACE_TYPES:
            continue
        if ace_type not in _ALLOW_ACE_TYPES or len(fields) < 6:
            raise GuardError("the config file has a permission entry this check does not understand"
                             + WINDOWS_FALLBACK)
        if fields[5].strip().upper() not in allowed:
            raise GuardError("the config file is accessible by %s: only you, SYSTEM and Administrators may have access "
                             "(remove the other entries)%s" % (fields[5].strip(), WINDOWS_FALLBACK))


def check_private_file(path, os_name=None, sddl=None, user_sid=None, stat_fn=os.stat, getuid=None):
    """Raise GuardError unless `path` is private to the current user."""
    os_name = os.name if os_name is None else os_name
    if os_name == "nt":
        text = sddl(path) if sddl else _read_sddl(path)
        check_sddl(text, user_sid or _current_user_sid())
        return
    info = stat_fn(path)
    if info.st_mode & 0o077:
        raise GuardError("the config file must not be accessible by group or others: run chmod 600 on it")
    uid = getuid if getuid is not None else getattr(os, "getuid", None)
    if uid is not None and info.st_uid != uid():
        raise GuardError("the config file is not owned by the current user")


def _url_problem(url):
    """Why `url` cannot be a base URL, or None."""
    if not (url or "").isascii():
        return "the url must contain ASCII characters only (write an international host name in punycode)"
    if _BAD_URL_CHARS.search(url or ""):
        return "the url must not contain backslashes, spaces, control characters, '?' or '#'"
    try:
        parts = urllib.parse.urlparse(url or "")
        host = parts.hostname
    except ValueError:
        return "the url is not valid"
    if parts.scheme not in ("http", "https") or not host:
        return "the url must look like https://host (scheme and host are required)"
    if parts.username or parts.password:
        return "the url must not contain a user name or password"
    if not _HOST_RE.fullmatch(parts.hostname.lower()):
        return "the host name of the url is not valid"
    return None


def _validated_url(url):
    problem = _url_problem(url)
    if problem:
        raise ConfigError(problem)
    return url.rstrip("/")


def load_config(path=None, env=None, os_name=None, sddl=None, user_sid=None):
    """Config from the 4 environment variables when all are set, else from the private config file ([odoo] section).
    The optional project: `ODOO_KPI_PROJECT` first, else the file's `project` key (the file is opened only in file mode)."""
    env = os.environ if env is None else env
    project = None
    if all(env.get(name) for name in ENV_NAMES.values()):
        values, source = {key: env[name] for key, name in ENV_NAMES.items()}, "env"
    else:
        path = path or config_path(env, os_name)
        if not os.path.isfile(path):
            raise ConfigError("no config: create %s (see the README) or set the %s variables"
                              % (path, ", ".join(ENV_NAMES.values())))
        check_private_file(path, os_name=os_name, sddl=sddl, user_sid=user_sid)
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path, encoding="utf-8-sig")
        except (configparser.Error, UnicodeDecodeError):
            raise ConfigError("the config file is not valid INI / UTF-8") from None
        if not parser.has_section("odoo"):
            raise ConfigError("the config file needs an [odoo] section")
        values = {key: parser.get("odoo", key, fallback="").strip() for key in ENV_NAMES}
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise ConfigError("the config file misses: %s" % ", ".join(missing))
        source = "file"
        project = parser.get("odoo", "project", fallback="").strip()
    project = (env.get(PROJECT_ENV) or "").strip() or project or None
    return Config(_validated_url(values["url"]), values["db"], values["login"], values["api_key"], source, project)


def project_status(project):
    """One static line for `check-config` about the project of the user's config: never an exception, and never the
    typed text unless it is the name of an installed catalog (membership first, see kpi_catalogs)."""
    try:
        names = kpi_catalogs.list_projects()
    except kpi_catalogs.CatalogError:
        return "catalogs: unreadable"
    installed = ", ".join(names) if names else "none"
    if project is None:
        return "project: not set (installed: %s)" % installed
    if project in names:
        return "project: %s" % project
    return "project: invalid (installed: %s)" % installed


# ---------------------------------------------------------------- instance guard

def instance_name(url):
    """First DNS label of the host (`my-instance` for my-instance.example.com); the whole host for an IP / localhost."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if re.fullmatch(r"[\d.]+|[0-9a-f:]+", host):
        return host
    return host.split(".")[0]


def check_instance(url, confirm_instance=None):
    """A valid host; https and the full host name typed by the user for every non-local host."""
    problem = _url_problem(url)
    if problem:
        raise GuardError(problem)
    parts = urllib.parse.urlparse(url)
    host = parts.hostname.lower()
    if host in LOCAL_HOSTS or host.endswith(LOCAL_SUFFIXES):
        return
    if parts.scheme != "https":
        raise GuardError("%s is not a local host: only https is allowed" % host)
    # the WHOLE host name must be typed: matching only its first label let `my-instance.attacker.example` pass for a
    # human who typed `my-instance`
    if (confirm_instance or "").strip().lower() != host:
        raise GuardError("%s is not a local dev host: ask the user to type the full host name (for example "
                         "my-instance.example.com) and pass it as --confirm-instance; never fill it in yourself" % host)


# ---------------------------------------------------------------- client

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """/jsonrpc never legitimately redirects: refuse, so nothing is ever sent to or fetched from another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_DEFAULT_OPENER = urllib.request.build_opener(_NoRedirect)


class Client:
    def __init__(self, config, confirm_instance=None, timeout=60, opener=None):
        check_instance(config.url, confirm_instance)
        self.config = config
        self.timeout = timeout
        self._open = opener or _DEFAULT_OPENER.open
        self.uid = None

    def _clean(self, text):
        text = str(text)
        if self.config.api_key:
            text = text.replace(self.config.api_key, "***")
        return _CONTROL_CHARS.sub(" ", text)[:500]

    def _post(self, service, method, args):
        body = json.dumps({"jsonrpc": "2.0", "method": "call", "id": 1,
                           "params": {"service": service, "method": method, "args": list(args)}}).encode("utf-8")
        request = urllib.request.Request(
            self.config.url + "/jsonrpc", body, {"Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with self._open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise RpcError("the server answer is too large")
            data = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            snippet = exc.read(200).decode("utf-8", "replace") if hasattr(exc, "read") else ""
            edge = re.search(r"error code: (\d+)", snippet)
            if edge and edge.group(1) == "1010":
                raise RpcError("HTTP %s, Cloudflare error 1010: the client signature is blocked; ask the instance "
                               "owner to allow it" % exc.code) from None
            if edge:
                raise RpcError("HTTP %s, Cloudflare error %s: the edge rejected this request"
                               % (exc.code, edge.group(1))) from None
            raise RpcError(self._clean("HTTP %s from the server" % exc.code)) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise RpcError(self._clean("cannot reach the server: %s" % getattr(exc, "reason", exc))) from None
        except (ValueError, RecursionError):
            raise RpcError("the server did not answer with JSON") from None
        if not isinstance(data, dict):
            raise RpcError("the server answer is not a JSON-RPC object")
        if "error" in data:
            error = data["error"]
            if isinstance(error, dict):
                detail = error.get("data") if isinstance(error.get("data"), dict) else {}
                text = "%s: %s" % (detail.get("name", "error"), detail.get("message") or error.get("message", ""))
            else:
                text = "error: %s" % (error,)
            raise RpcError(self._clean(text))
        return data.get("result")

    def version(self):
        return self._post("common", "version", [])

    def login(self):
        uid = self._post("common", "login", [self.config.db, self.config.login, self.config.api_key])
        if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
            raise RpcError("login failed: check the login, the API key and the database name")
        self.uid = uid
        return uid

    def execute(self, model, method, args=None, kwargs=None):
        if self.uid is None:
            self.login()
        return self._post("object", "execute_kw", [self.config.db, self.uid, self.config.api_key, model, method,
                                                   args or [], kwargs or {}])


# ---------------------------------------------------------------- init-config (the human, in their own terminal)

INIT_FIELD_LIMIT = 256
INIT_KEY_LIMIT = 1024
AGENT_ENV = "CLAUDECODE"                         # set by Claude Code in the environment of its tools


def _hidden_prompt(prompt):
    """The API key typed without echo; refuses when the terminal cannot hide it (getpass would fall back to echo)."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise GuardError("this terminal cannot hide what you type: use the %s variables instead of init-config"
                             % ", ".join(ENV_NAMES.values())) from None


def _field(value, name, limit):
    value = (value or "").strip()
    if not value or len(value) > limit or _CONTROL_CHARS.search(value):
        raise ConfigError("%s must be a non-empty single-line text of at most %d characters" % (name, limit))
    return value


def _not_a_key(value, name, written=False):
    """An Odoo API key is 40 hex characters: pasted at the wrong prompt it would be shown on the screen and stored as
    a database name or login. Refuse with a STATIC text (never the value), and tell the user what to do. `written` =
    the config file already exists at this stage (the host is asked after the write): say so, so the text stays true."""
    if re.fullmatch(r"[0-9A-Fa-f]{40}", value):
        raise ConfigError("what you typed as %s looks like an API key: %s. If it was a key, delete it in Odoo "
                          "(Preferences > Account Security) and create a new one, because it was shown on the screen"
                          % (name, "the config file was already written, so run init-config again after creating a "
                                   "new key" if written else "nothing was written"))
    return value


def _ask_project(ask):
    """The optional project prompt of `init-config`, asked ONLY when several source catalogs ship (fewer: no prompt, so
    today's prompt sequence is unchanged; an unreadable catalogs folder counts as fewer). Validated here, BEFORE anything
    is written: a pasted key gets the usual "revoke it" text, any other value must be an installed catalog."""
    try:
        names = kpi_catalogs.list_projects()
    except kpi_catalogs.CatalogError:
        return None
    if len(names) < 2:
        return None
    text = _not_a_key((ask("Project (%s; Enter = none): " % ", ".join(names)) or "").strip(), "the project")
    if text and text not in names:
        raise ConfigError("that project is not one of the installed source catalogs (installed: %s)" % ", ".join(names))
    return text or None


def _write_private(path, text, os_name, sddl, user_sid):
    """Write `text` to `path` through a private temp file in the same folder: the temp file is checked with the same
    privacy rule as a config file BEFORE it replaces the target, and the target is never opened for writing (so an old
    wide-open file never holds the key, not even for a moment)."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, mode=0o700, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".odoo-kpi-config-", suffix=".tmp")
    try:
        # the privacy check runs on the still EMPTY temp file: a folder whose inherited ACL is too wide (Windows) is
        # refused before the key is written, so the key never sits on disk under a wide ACL, not even briefly; the
        # descriptor is closed first so that the temp file can be deleted on Windows too
        try:
            check_private_file(temp, os_name=os_name, sddl=sddl, user_sid=user_sid)
        except BaseException:
            os.close(handle)
            raise
        with os.fdopen(handle, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def init_config(path=None, env=None, stdin=None, stdout=None, ask=input, ask_secret=None, os_name=None, sddl=None,
                user_sid=None, client_factory=None):
    """Create the private config file by asking the HUMAN for URL, database, login and API key. Returns the exit code.

    Refused (nothing written) unless stdin and stdout are terminals and the environment is not Claude Code's: an agent
    can neither type the key nor run this for the user. Every input is read from `env` / `stdin` / `stdout` / `ask` /
    `ask_secret` as given, so a test gives the same answer inside and outside Claude Code."""
    env = os.environ if env is None else env
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    ask_secret = ask_secret or _hidden_prompt
    if AGENT_ENV in env:
        raise GuardError("init-config is for you to run in your own terminal window, not inside Claude Code: open a "
                         "normal terminal and run it there")
    if not (hasattr(stdin, "isatty") and stdin.isatty() and hasattr(stdout, "isatty") and stdout.isatty()):
        raise GuardError("init-config needs a terminal: run it yourself in a terminal window")
    target = path or config_path(env, os_name)
    print("The config file will be: %s" % target, file=stdout)
    if any(name in env for name in ENV_NAMES.values()):
        print("Note: ODOO_KPI_* environment variables are set here. While all four are set they are used INSTEAD of "
              "this file.", file=stdout)
    if os.path.lexists(target):
        if os.path.islink(target) or not os.path.isfile(target):
            raise GuardError("the config path is a link or not a regular file: refusing to touch it")
        if ask("A config file exists. Type yes to replace it: ").strip() != "yes":
            print("Cancelled: nothing was written.", file=stdout)
            return 1
    url = _validated_url(_not_a_key(_field(ask("Odoo address (for example https://my-instance.example.com): "),
                                           "the address", INIT_FIELD_LIMIT), "the address"))
    db = _not_a_key(_field(ask("Database name: "), "the database name", INIT_FIELD_LIMIT), "the database name")
    login = _not_a_key(_field(ask("Your Odoo login: "), "the login", INIT_FIELD_LIMIT), "the login")
    project = _ask_project(ask)
    key = _field(ask_secret("Your API key (typing is hidden): "), "the API key", INIT_KEY_LIMIT)
    parser = configparser.ConfigParser(interpolation=None)
    values = dict(zip(ENV_NAMES, (url, db, login, key)))       # the INI keys are the ENV_NAMES keys
    if project:
        values["project"] = project
    parser.read_dict({"odoo": values})
    buffer = io.StringIO()
    parser.write(buffer)
    _write_private(target, buffer.getvalue(), os_name, sddl, user_sid)
    print("Config written (private to your user).", file=stdout)
    host = ask("To test the connection, type the FULL host name of the server (Enter skips the test): ").strip()
    if host:
        _not_a_key(host, "the host name", written=True)
    if not host:
        print("Connection test skipped. Test it later with: odoo_rpc.py ping --confirm-instance <host>", file=stdout)
        return 0
    config = load_config(path=target, env={}, os_name=os_name, sddl=sddl, user_sid=user_sid)
    client = (client_factory or Client)(config, confirm_instance=host)
    print("login: ok (uid %d)" % client.login(), file=stdout)
    return 0


# ---------------------------------------------------------------- command line

def main(argv=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="odoo_rpc.py", description="Odoo KPI skills: connection check")
    parser.add_argument("command", choices=["ping", "config-path", "check-config", "init-config"])
    parser.add_argument("--config", help="path of the config file (default: see config-path)")
    parser.add_argument("--confirm-instance", help="the full host name typed by the user, required for non-local hosts")
    args = parser.parse_args(argv)
    if args.command == "config-path":
        print(args.config or config_path(), file=out)
        return 0
    if args.command == "init-config":
        try:
            return init_config(path=args.config, stdout=out)
        except (ConfigError, GuardError) as exc:
            print("error: %s" % exc, file=err)
            return 2
        except RpcError as exc:
            print("error: %s" % exc, file=err)
            return 1
        except Exception as exc:  # noqa: BLE001 - last resort: one line, no traceback, never the exception text
            print("error: unexpected %s" % type(exc).__name__, file=err)
            return 1
    try:
        config = load_config(path=args.config)
        if args.command == "check-config":
            print("config: %s" % (config.source if config.source == "env" else "file, private (permissions ok)"),
                  file=out)
            print(project_status(config.project), file=out)       # a bad optional project never fails check-config
            return 0
        client = Client(config, confirm_instance=args.confirm_instance)
        info = client.version()
        uid = client.login()
    except (ConfigError, GuardError) as exc:
        print("error: %s" % exc, file=err)
        return 2
    except RpcError as exc:
        print("error: %s" % exc, file=err)
        return 1
    except Exception as exc:  # noqa: BLE001 - last resort: one line, no traceback, never the exception text
        print("error: unexpected %s" % type(exc).__name__, file=err)
        return 1
    shown = info.get("server_version") if isinstance(info, dict) else None
    shown = (shown if isinstance(shown, str) and _VERSION_TEXT.fullmatch(shown) and config.api_key not in shown
             else "unknown")
    print("instance: %s" % instance_name(config.url), file=out)
    print("config: %s" % config.source, file=out)
    print("server_version: %s" % shown, file=out)
    print("login: ok (uid %d)" % uid, file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
