"""Hardening of Client._post / login / _clean in scripts/odoo_rpc.py (round 2 of the step 5 review)."""
import http.client
import re
import unittest
from unittest import mock

from fake_odoo import FAKE_KEY, FAKE_UID, FakeOdoo
from helpers import FakeResponse, IsolatedTestCase, RecordingOpener, make_config, odoo_rpc

CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def client_with(*payloads, raw=None):
    """A client whose injected opener answers with these JSON payloads (or one raw byte string)."""
    queue = list(payloads)

    def opener(request, timeout=None):
        return FakeResponse(raw=raw) if raw is not None else FakeResponse(queue.pop(0))

    return odoo_rpc.Client(make_config("http://127.0.0.1:1"), opener=opener)


def client_raising(exc):
    def opener(request, timeout=None):
        raise exc

    return odoo_rpc.Client(make_config("http://127.0.0.1:1"), opener=opener)


class RedirectTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.fake, self.other = FakeOdoo(), FakeOdoo()
        for server in (self.fake, self.other):
            server.start()
            self.addCleanup(server.stop)
        self.fake.mode = "redirect"

    def test_redirects_are_never_followed_by_the_default_opener(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                self.fake.requests.clear()
                self.fake.redirect_code = code
                self.fake.redirect_to = "/jsonrpc2"
                client = odoo_rpc.Client(make_config(self.fake.url), timeout=5)
                with self.assertRaises(odoo_rpc.RpcError) as caught:
                    client.login()
                self.assertIn("HTTP %d" % code, str(caught.exception))
                self.assertEqual(len(self.fake.requests), 1, "a redirect was followed")
                self.assertSafeRpcError(caught.exception)

    def test_a_redirect_to_another_server_sends_nothing_there(self):
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                self.fake.requests.clear()
                self.other.requests.clear()
                self.fake.redirect_code = code
                self.fake.redirect_to = self.other.url + "/jsonrpc"
                client = odoo_rpc.Client(make_config(self.fake.url), timeout=5)
                with self.assertRaises(odoo_rpc.RpcError):
                    client.execute("res.partner", "search_count")
                self.assertEqual(len(self.fake.requests), 1)
                self.assertEqual(self.other.requests, [])
                for server in (self.fake, self.other):
                    for request in server.requests[1:]:
                        self.assertNotIn(FAKE_KEY, request["raw"].decode("utf-8"))

    def test_the_first_and_only_request_is_the_one_the_client_meant_to_send(self):
        self.fake.redirect_code = 307
        client = odoo_rpc.Client(make_config(self.fake.url), timeout=5)
        with self.assertRaises(odoo_rpc.RpcError):
            client.version()
        self.assertEqual(self.fake.requests[0]["path"], "/jsonrpc")
        self.assertEqual(self.fake.requests[0]["body"]["params"]["method"], "version")

    def test_a_server_that_does_not_redirect_still_works_with_the_default_opener(self):
        self.fake.mode = "ok"
        client = odoo_rpc.Client(make_config(self.fake.url), timeout=5)
        self.assertEqual(client.login(), FAKE_UID)


class ErrorShapeTests(IsolatedTestCase):
    def test_error_that_is_no_object_gives_one_clean_line(self):
        for error in (None, "boom", 7, ["x"], True, 1.5):
            with self.subTest(error=error):
                with self.assertRaises(odoo_rpc.RpcError) as caught:
                    client_with({"error": error}).version()
                self.assertNotIn("\n", str(caught.exception))
                self.assertSafeRpcError(caught.exception)

    def test_error_string_that_echoes_the_key_is_redacted(self):
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            client_with({"error": "bad key %s" % FAKE_KEY}).version()
        self.assertIn("***", str(caught.exception))
        self.assertSafeRpcError(caught.exception)

    def test_error_data_that_is_no_object_falls_back_to_the_message(self):
        for data in ("a string", ["x"], 3, None, True):
            with self.subTest(data=data):
                with self.assertRaises(odoo_rpc.RpcError) as caught:
                    client_with({"error": {"data": data, "message": "plain message"}}).version()
                self.assertIn("plain message", str(caught.exception))
                self.assertSafeRpcError(caught.exception)

    def test_error_without_any_text_still_gives_rpc_error(self):
        for payload in ({"error": {}}, {"error": {"data": {}}}, {"error": {"data": {"name": "X"}}}):
            with self.subTest(payload=payload):
                with self.assertRaises(odoo_rpc.RpcError):
                    client_with(payload).version()

    def test_data_name_and_message_are_shown_and_redacted(self):
        payload = {"error": {"data": {"name": "odoo.exceptions.AccessError", "message": "no %s here" % FAKE_KEY}}}
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            client_with(payload).version()
        self.assertIn("odoo.exceptions.AccessError", str(caught.exception))
        self.assertIn("no *** here", str(caught.exception))

    def test_json_documents_that_are_no_object_give_rpc_error(self):
        for raw in (b"null", b"[]", b'"x"', b"42", b"true", b"[1, 2, 3]"):
            with self.subTest(raw=raw):
                with self.assertRaises(odoo_rpc.RpcError) as caught:
                    client_with(raw=raw).version()
                self.assertIn("not a JSON-RPC object", str(caught.exception))
                self.assertSafeRpcError(caught.exception)

    def test_an_object_without_result_gives_none_not_a_crash(self):
        self.assertIsNone(client_with({"jsonrpc": "2.0", "id": 1}).version())

    def test_non_utf8_answer_gives_a_clean_rpc_error(self):
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            client_with(raw=b"\xff\xfe\xfa not utf-8").version()
        self.assertSafeRpcError(caught.exception)


class TransportFailureTests(IsolatedTestCase):
    def test_http_client_exceptions_become_rpc_errors(self):
        errors = (http.client.BadStatusLine("garbage line"), http.client.IncompleteRead(b"abc", 10),
                  http.client.RemoteDisconnected("Remote end closed connection without response"),
                  http.client.CannotSendRequest(), http.client.ResponseNotReady(), http.client.LineTooLong("header"),
                  http.client.HTTPException("generic"))
        for exc in errors:
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(odoo_rpc.RpcError) as caught:
                    client_raising(exc).version()
                self.assertIn("cannot reach the server", str(caught.exception))
                self.assertSafeRpcError(caught.exception)

    def test_http_client_exception_text_with_the_key_is_redacted(self):
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            client_raising(http.client.BadStatusLine("HTTP/1.1 key=%s" % FAKE_KEY)).version()
        self.assertIn("***", str(caught.exception))
        self.assertSafeRpcError(caught.exception)

    def test_a_very_deep_json_answer_becomes_an_rpc_error_not_a_recursion_crash(self):
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            client_with(raw=b"[" * 100000).version()
        self.assertIn("JSON", str(caught.exception))
        self.assertSafeRpcError(caught.exception)

    def test_a_very_deep_json_answer_over_real_http(self):
        with FakeOdoo() as fake:
            fake.mode = "deep_json"
            with self.assertRaises(odoo_rpc.RpcError) as caught:
                odoo_rpc.Client(make_config(fake.url), timeout=10).version()
        self.assertSafeRpcError(caught.exception)


class ControlCharacterTests(IsolatedTestCase):
    def assertCleanLine(self, exc):
        text = str(exc)
        self.assertIsNone(CONTROL.search(text), repr(text))
        self.assertNotIn("\x1b", text)
        self.assertNotIn(FAKE_KEY, text)

    def test_ansi_and_control_characters_are_removed_from_server_errors(self):
        nasty = "\x1b[31mRED\x1b[0m line1\nline2\r\x00nul\x07bell\x7fdel\x9bcsi key=%s" % FAKE_KEY
        for label, payload in (("data.message", {"error": {"data": {"name": "E", "message": nasty}}}),
                               ("data.name", {"error": {"data": {"name": nasty, "message": "m"}}}),
                               ("error.message", {"error": {"message": nasty}}),
                               ("error string", {"error": nasty})):
            with self.subTest(where=label):
                with self.assertRaises(odoo_rpc.RpcError) as caught:
                    client_with(payload).version()
                self.assertCleanLine(caught.exception)
                self.assertIn("RED", str(caught.exception))
                self.assertIn("***", str(caught.exception))

    def test_control_characters_are_removed_from_network_errors(self):
        import urllib.error
        for exc in (urllib.error.URLError("refused \x1b[2J\x1b[H wiped"), OSError("bad \x1b]0;title\x07 thing"),
                    http.client.BadStatusLine("x\x1b[1my")):
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(odoo_rpc.RpcError) as caught:
                    client_raising(exc).version()
                self.assertCleanLine(caught.exception)

    def test_each_control_character_range_boundary_is_replaced(self):
        for code in (0x00, 0x09, 0x0a, 0x1b, 0x1f, 0x7f, 0x80, 0x85, 0x9f):
            with self.subTest(code=hex(code)):
                with self.assertRaises(odoo_rpc.RpcError) as caught:
                    client_with({"error": "a%sb" % chr(code)}).version()
                self.assertEqual(str(caught.exception), "error: a b")

    def test_printable_characters_next_to_the_control_ranges_are_kept(self):
        for char in (" ", "~", " ", "¡", "é", "한"):
            with self.subTest(char=repr(char)):
                with self.assertRaises(odoo_rpc.RpcError) as caught:
                    client_with({"error": "a%sb" % char}).version()
                self.assertEqual(str(caught.exception), "error: a%sb" % char)

    def test_http_status_errors_are_clean_lines_too(self):
        with FakeOdoo() as fake:
            fake.mode = "http_500"
            with self.assertRaises(odoo_rpc.RpcError) as caught:
                odoo_rpc.Client(make_config(fake.url), timeout=5).version()
        self.assertCleanLine(caught.exception)


class ResponseSizeTests(IsolatedTestCase):
    BODY = b'{"result": 1}'  # 13 bytes

    def test_an_answer_over_the_cap_is_refused(self):
        with mock.patch.object(odoo_rpc, "MAX_RESPONSE", len(self.BODY) - 1):
            with self.assertRaises(odoo_rpc.RpcError) as caught:
                client_with(raw=self.BODY).version()
        self.assertIn("too large", str(caught.exception))
        self.assertSafeRpcError(caught.exception)

    def test_an_answer_exactly_at_the_cap_is_accepted(self):
        with mock.patch.object(odoo_rpc, "MAX_RESPONSE", len(self.BODY)):
            self.assertEqual(client_with(raw=self.BODY).version(), 1)

    def test_an_answer_one_byte_over_the_cap_is_refused(self):
        with mock.patch.object(odoo_rpc, "MAX_RESPONSE", len(self.BODY)):
            with self.assertRaises(odoo_rpc.RpcError):
                client_with(raw=self.BODY + b" ").version()

    def test_the_client_never_reads_more_than_cap_plus_one(self):
        asked = []

        class Probe(FakeResponse):
            def read(self, size=-1):
                asked.append(size)
                return super().read(size)

        client = odoo_rpc.Client(make_config("http://127.0.0.1:1"),
                                 opener=lambda request, timeout=None: Probe(raw=b'{"result": 1}'))
        with mock.patch.object(odoo_rpc, "MAX_RESPONSE", 100):
            client.version()
        self.assertEqual(asked, [101])

    def test_the_cap_over_real_http(self):
        with FakeOdoo() as fake:
            with mock.patch.object(odoo_rpc, "MAX_RESPONSE", 10):
                with self.assertRaises(odoo_rpc.RpcError) as caught:
                    odoo_rpc.Client(make_config(fake.url), timeout=5).version()
        self.assertIn("too large", str(caught.exception))

    def test_the_default_cap_is_large_enough_for_snapshots_and_finite(self):
        self.assertGreaterEqual(odoo_rpc.MAX_RESPONSE, 8 * 1024 * 1024)
        self.assertLessEqual(odoo_rpc.MAX_RESPONSE, 1024 * 1024 * 1024)


class LoginResultTests(IsolatedTestCase):
    def test_only_a_positive_int_is_a_uid(self):
        for good in (1, 7, 2 ** 31):
            with self.subTest(uid=good):
                client = client_with({"result": good})
                self.assertEqual(client.login(), good)
                self.assertEqual(client.uid, good)

    def test_everything_else_is_a_failed_login(self):
        for bad in ("abc", "7", True, False, 0, -1, None, 1.5, 1.0, [7], {"uid": 7}, ""):
            with self.subTest(result=bad):
                client = client_with({"result": bad})
                with self.assertRaises(odoo_rpc.RpcError) as caught:
                    client.login()
                self.assertIn("login failed", str(caught.exception))
                self.assertIsNone(client.uid)
                self.assertSafeRpcError(caught.exception)

    def test_execute_does_not_send_execute_kw_after_an_odd_login_answer(self):
        opener = RecordingOpener("abc")
        client = odoo_rpc.Client(make_config("http://127.0.0.1:1"), opener=opener)
        with self.assertRaises(odoo_rpc.RpcError):
            client.execute("res.partner", "search_count")
        self.assertEqual(len(opener.requests), 1)

    def test_over_real_http_a_string_uid_is_refused(self):
        with FakeOdoo() as fake:
            for bad in ("abc", True, 0, -1, None, 1.5):
                with self.subTest(result=bad):
                    fake.overrides["login"] = bad
                    with self.assertRaises(odoo_rpc.RpcError):
                        odoo_rpc.Client(make_config(fake.url), timeout=5).login()


if __name__ == "__main__":
    unittest.main()
