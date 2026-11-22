"""Shared helpers of the tests (not a test module: the file name does not match `test*.py`)."""
import json
import os
import socket
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import kpi_catalogs  # noqa: E402
import odoo_rpc  # noqa: E402  (the script under test)
from fake_odoo import FAKE_DB, FAKE_KEY, FAKE_LOGIN  # noqa: E402

LOCAL_CONFIG_URL = "http://127.0.0.1:1"


def install_catalogs(case, *names):
    """Point `kpi_catalogs.CATALOG_DIR` (D11 seam, never a parameter) at a fresh tmp folder that holds one `<name>.json`
    per name for the rest of the test; returns the folder. `case` is an IsolatedTestCase (it owns `tmp`)."""
    folder = tempfile.mkdtemp(prefix="catalogs-", dir=case.tmp)
    for name in names:
        with open(os.path.join(folder, name + ".json"), "wb") as handle:
            handle.write(b"{}")
    patcher = mock.patch.object(kpi_catalogs, "CATALOG_DIR", folder)
    patcher.start()
    case.addCleanup(patcher.stop)
    return folder


def missing_catalogs(case):
    """`kpi_catalogs.CATALOG_DIR` at a folder that does not exist (an unreadable listing)."""
    folder = os.path.join(case.tmp, "no-such-catalogs")
    patcher = mock.patch.object(kpi_catalogs, "CATALOG_DIR", folder)
    patcher.start()
    case.addCleanup(patcher.stop)
    return folder


def make_config(url, key=FAKE_KEY, db=FAKE_DB, login=FAKE_LOGIN):
    return odoo_rpc.Config(url, db, login, key)


def closed_port():
    """A TCP port on 127.0.0.1 that nothing listens on (bound, read, released)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def ini_text(url, key=FAKE_KEY, db=FAKE_DB, login=FAKE_LOGIN):
    return "[odoo]\nurl = %s\ndb = %s\nlogin = %s\napi_key = %s\n" % (url, db, login, key)


def write_file(directory, text, mode=0o600, name="config.ini"):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    if os.name != "nt":
        os.chmod(path, mode)
    return path


def write_bytes(directory, data, mode=0o600, name="config.ini"):
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(data)
    if os.name != "nt":
        os.chmod(path, mode)
    return path


class FakeResponse:
    """What `urllib.request.urlopen` returns: `.read()` and a context manager."""

    def __init__(self, payload=None, raw=None):
        self._raw = raw if raw is not None else json.dumps(payload).encode("utf-8")

    def read(self, size=-1):
        return self._raw if size is None or size < 0 else self._raw[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class RecordingOpener:
    """An injected `opener`: records the `urllib.request.Request` and answers from a queue of JSON-RPC results."""

    def __init__(self, *results):
        self.results = list(results)
        self.requests = []
        self.timeouts = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        return FakeResponse({"jsonrpc": "2.0", "id": 1, "result": self.results.pop(0)})


class ExplodingOpener:
    """An injected `opener` that must never be called (the guard has to refuse first)."""

    def __init__(self):
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        raise AssertionError("the opener was called: the network was reached before the guard refused")


class IsolatedTestCase(unittest.TestCase):
    """Every test runs with a cleared, private environment: nothing under the real home is read or written,
    no proxy variable can redirect the 127.0.0.1 traffic, no real ODOO_KPI_* variable leaks in."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="kpi-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        base_env = {"HOME": self.home, "USERPROFILE": self.home,
                    "XDG_CONFIG_HOME": os.path.join(self.tmp, "xdg"),
                    "APPDATA": os.path.join(self.tmp, "appdata")}
        patcher = mock.patch.dict(os.environ, base_env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---- assertions shared by the safety tests
    def assertNoKey(self, text, key=FAKE_KEY):
        self.assertNotIn(key, text)

    def assertSafeRpcError(self, exc, key=FAKE_KEY):
        """The error text, its repr, its whole traceback and its exception chain carry no API key."""
        self.assertIsInstance(exc, odoo_rpc.RpcError)
        self.assertNotIn(key, str(exc))
        self.assertNotIn(key, repr(exc))
        self.assertIsNone(exc.__cause__, "a chained cause could print the key")
        self.assertTrue(exc.__suppress_context__ or exc.__context__ is None,
                        "an implicit context could print the key (`raise ... from None` is missing)")
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.assertNotIn(key, rendered)
