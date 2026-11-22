"""Client of scripts/odoo_rpc.py against a fake Odoo (real HTTP on 127.0.0.1) and against injected openers."""
import urllib.error

from fake_odoo import FAKE_DB, FAKE_KEY, FAKE_LOGIN, FAKE_UID, FakeOdoo
from helpers import (ExplodingOpener, IsolatedTestCase, RecordingOpener, closed_port, make_config, odoo_rpc)


class FakeServerTestCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeOdoo()
        self.fake.start()
        self.addCleanup(self.fake.stop)
        self.config = make_config(self.fake.url)
        self.client = odoo_rpc.Client(self.config, timeout=5)


class UserAgentTests(FakeServerTestCase):
    def test_every_request_carries_own_user_agent(self):
        self.client.version()
        self.client.login()
        self.client.execute("res.partner", "search_count")

        self.assertEqual(len(self.fake.requests), 3)
        for request in self.fake.requests:
            agent = request["headers"]["user-agent"]
            self.assertEqual(agent, "odoo-kpi-skills/" + odoo_rpc.VERSION)
            self.assertFalse(agent.lower().startswith("python-urllib"), agent)
            self.assertRegex(agent, r"^odoo-kpi-skills/\d+\.\d+\.\d+$")
            self.assertEqual(request["headers"]["content-type"], "application/json")

    def test_requests_go_to_jsonrpc_with_a_call_envelope(self):
        self.client.version()
        request = self.fake.requests[0]
        self.assertEqual(request["path"], "/jsonrpc")
        self.assertEqual(request["body"]["jsonrpc"], "2.0")
        self.assertEqual(request["body"]["method"], "call")
        self.assertEqual(request["body"]["params"], {"service": "common", "method": "version", "args": []})

    def test_version_returns_server_info(self):
        self.assertEqual(self.client.version()["server_version"], "17.0-fake")


class LoginAndExecuteTests(FakeServerTestCase):
    def test_login_ok_returns_uid_and_remembers_it(self):
        uid = self.client.login()
        self.assertEqual(uid, FAKE_UID)
        self.assertEqual(self.client.uid, FAKE_UID)
        body = self.fake.calls("common", "login")[0]
        self.assertEqual(body["params"]["args"], [FAKE_DB, FAKE_LOGIN, FAKE_KEY])

    def test_login_false_raises_rpc_error_without_the_key(self):
        self.fake.mode = "login_false"
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            self.client.login()
        self.assertIn("login failed", str(caught.exception))
        self.assertSafeRpcError(caught.exception)
        self.assertIsNone(self.client.uid)

    def test_wrong_key_is_rejected_by_the_server_and_not_echoed(self):
        client = odoo_rpc.Client(make_config(self.fake.url, key="another-WRONG-key-value"), timeout=5)
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            client.login()
        self.assertSafeRpcError(caught.exception, key="another-WRONG-key-value")

    def test_execute_logs_in_lazily_and_sends_everything_the_server_needs(self):
        self.assertEqual(self.fake.requests, [])

        result = self.client.execute("res.partner", "search_read", [[["id", ">", 0]]], {"limit": 3})

        self.assertEqual([r["body"]["params"]["method"] for r in self.fake.requests], ["login", "execute_kw"])
        sent = self.fake.calls("object", "execute_kw")[0]["params"]
        self.assertEqual(sent["service"], "object")
        self.assertEqual(sent["args"], [FAKE_DB, FAKE_UID, FAKE_KEY, "res.partner", "search_read",
                                        [[["id", ">", 0]]], {"limit": 3}])
        self.assertEqual(result, {"model": "res.partner", "method": "search_read",
                                  "args": [[["id", ">", 0]]], "kwargs": {"limit": 3}})

    def test_execute_logs_in_only_once_and_defaults_args_and_kwargs(self):
        self.client.execute("res.partner", "search_count")
        self.client.execute("res.users", "search_count")

        self.assertEqual(len(self.fake.calls("common", "login")), 1)
        calls = self.fake.calls("object", "execute_kw")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["params"]["args"][5:], [[], {}])

    def test_execute_with_a_failed_login_never_sends_execute_kw(self):
        self.fake.mode = "login_false"
        with self.assertRaises(odoo_rpc.RpcError):
            self.client.execute("res.partner", "search_count")
        self.assertEqual(self.fake.calls("object", "execute_kw"), [])


class SecretSafeErrorTests(FakeServerTestCase):
    def test_server_error_that_echoes_the_key_is_redacted(self):
        for mode in ("error_echo", "error_echo_top"):
            with self.subTest(mode=mode):
                self.fake.mode = mode
                with self.assertRaises(odoo_rpc.RpcError) as caught:
                    self.client.login()
                self.assertIn("***", str(caught.exception))
                self.assertIn("rejected key", str(caught.exception))
                self.assertSafeRpcError(caught.exception)

    def test_server_error_on_execute_kw_that_echoes_the_key_is_redacted(self):
        self.client.uid = FAKE_UID
        self.fake.mode = "error_echo"
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            self.client.execute("res.partner", "search_count")
        self.assertIn("***", str(caught.exception))
        self.assertSafeRpcError(caught.exception)

    def test_http_500_whose_body_echoes_the_key_gives_a_clean_rpc_error(self):
        self.fake.mode = "http_500"
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            self.client.login()
        self.assertIn("HTTP 500", str(caught.exception))
        self.assertSafeRpcError(caught.exception)

    def test_non_json_answer_gives_a_clean_rpc_error(self):
        self.fake.mode = "non_json"
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            self.client.login()
        self.assertIn("JSON", str(caught.exception))
        self.assertSafeRpcError(caught.exception)

    def test_unreachable_port_gives_a_clean_rpc_error(self):
        client = odoo_rpc.Client(make_config("http://127.0.0.1:%d" % closed_port()), timeout=3)
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            client.login()
        self.assertIn("cannot reach the server", str(caught.exception))
        self.assertSafeRpcError(caught.exception)

    def test_network_error_whose_text_holds_the_key_is_redacted(self):
        def opener(request, timeout=None):
            raise urllib.error.URLError("proxy said no for key %s" % FAKE_KEY)

        client = odoo_rpc.Client(make_config("http://127.0.0.1:1"), opener=opener)
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            client.version()
        self.assertIn("***", str(caught.exception))
        self.assertSafeRpcError(caught.exception)

    def test_os_error_whose_text_holds_the_key_is_redacted(self):
        def opener(request, timeout=None):
            raise OSError("socket closed, key %s" % FAKE_KEY)

        client = odoo_rpc.Client(make_config("http://127.0.0.1:1"), opener=opener)
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            client.version()
        self.assertIn("***", str(caught.exception))
        self.assertSafeRpcError(caught.exception)

    def test_error_text_is_truncated_to_500_characters(self):
        long_message = "x" * 3000

        class LongErrorOpener:
            def __call__(self, request, timeout=None):
                from helpers import FakeResponse
                return FakeResponse({"error": {"data": {"name": "E", "message": long_message}}})

        client = odoo_rpc.Client(make_config("http://127.0.0.1:1"), opener=LongErrorOpener())
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            client.version()
        self.assertLessEqual(len(str(caught.exception)), 500)

    def test_valid_json_that_is_not_an_object_gives_rpc_error_not_a_crash(self):
        # Boundary of the "non-JSON answer gives RpcError" promise: a proxy may answer valid JSON that is no object.
        self.fake.mode = "json_list"
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            self.client.version()
        self.assertSafeRpcError(caught.exception)


    def test_other_valid_json_documents_that_are_no_object_give_rpc_error_not_a_crash(self):
        from helpers import FakeResponse
        for payload in (None, "ok", 42, ["a"], True):
            with self.subTest(payload=payload):
                client = odoo_rpc.Client(make_config("http://127.0.0.1:1"),
                                         opener=lambda request, timeout=None, p=payload: FakeResponse(p))
                with self.assertRaises(odoo_rpc.RpcError):
                    client.version()


    def test_error_member_that_is_no_object_gives_rpc_error_not_a_crash(self):
        from helpers import FakeResponse
        for error in (None, "boom", 7, ["x"]):
            with self.subTest(error=error):
                client = odoo_rpc.Client(make_config("http://127.0.0.1:1"),
                                         opener=lambda request, timeout=None, e=error: FakeResponse({"error": e}))
                with self.assertRaises(odoo_rpc.RpcError):
                    client.version()


class CloudflareTests(FakeServerTestCase):
    def test_http_403_error_1010_mentions_cloudflare_and_1010(self):
        self.fake.mode = "cf_1010"
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            self.client.version()
        message = str(caught.exception)
        self.assertIn("Cloudflare", message)
        self.assertIn("1010", message)
        self.assertIn("blocked", message)
        self.assertIn("HTTP 403", message)
        self.assertSafeRpcError(caught.exception)

    def test_other_edge_error_code_is_reported_with_its_own_number(self):
        self.fake.mode = "cf_other"
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            self.client.version()
        self.assertIn("Cloudflare", str(caught.exception))
        self.assertIn("1020", str(caught.exception))
        self.assertNotIn("1010", str(caught.exception))
        self.assertNotIn("signature", str(caught.exception))

    def test_edge_502_is_reported_with_its_own_number_and_does_not_claim_1010(self):
        self.fake.mode = "cf_502"
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            self.client.version()
        self.assertIn("502", str(caught.exception))
        self.assertIn("Cloudflare", str(caught.exception))
        self.assertNotIn("1010", str(caught.exception))
        self.assertNotIn("signature", str(caught.exception))
        self.assertSafeRpcError(caught.exception)

    def test_plain_403_is_not_reported_as_cloudflare(self):
        self.fake.mode = "http_403_plain"
        with self.assertRaises(odoo_rpc.RpcError) as caught:
            self.client.version()
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertNotIn("Cloudflare", str(caught.exception))
        self.assertSafeRpcError(caught.exception)


class ConstructorGuardTests(IsolatedTestCase):
    def test_guard_runs_before_any_network_call_for_a_non_local_url_without_confirm(self):
        opener = ExplodingOpener()
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc.Client(make_config("https://my-instance.example.com"), opener=opener)
        self.assertEqual(opener.calls, 0)

    def test_guard_refuses_plain_http_non_local_even_with_the_right_confirm(self):
        opener = ExplodingOpener()
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc.Client(make_config("http://my-instance.example.com"),
                            confirm_instance="my-instance.example.com", opener=opener)
        self.assertEqual(opener.calls, 0)

    def test_guard_refuses_a_wrong_confirm(self):
        opener = ExplodingOpener()
        with self.assertRaises(odoo_rpc.GuardError) as caught:
            odoo_rpc.Client(make_config("https://my-instance.example.com"), confirm_instance="other",
                            opener=opener)
        self.assertIn("my-instance", str(caught.exception))
        self.assertEqual(opener.calls, 0)

    def test_guard_refuses_the_first_label_alone_through_the_constructor(self):
        # round 3 (security r2): the confirmation is the WHOLE host name, the first label alone no longer unlocks it
        for url in ("https://my-instance.example.com", "https://my-instance.attacker.example",
                    "https://my-instance.example.com.attacker.example"):
            with self.subTest(url=url):
                opener = ExplodingOpener()
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    odoo_rpc.Client(make_config(url), confirm_instance="my-instance", opener=opener)
                self.assertIn("type the full host name", str(caught.exception))
                self.assertEqual(opener.calls, 0)

    def test_guard_refuses_the_legitimate_host_typed_for_a_look_alike_url(self):
        for url in ("https://my-instance.attacker.example", "https://my-instance.example.com.attacker.example"):
            with self.subTest(url=url):
                opener = ExplodingOpener()
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc.Client(make_config(url), confirm_instance="my-instance.example.com", opener=opener)
                self.assertEqual(opener.calls, 0)

    def test_with_confirm_it_works_through_the_injected_opener(self):
        opener = RecordingOpener({"server_version": "17.0-fake"}, FAKE_UID)
        client = odoo_rpc.Client(make_config("https://my-instance.example.com"),
                                 confirm_instance="my-instance.example.com", timeout=12, opener=opener)

        self.assertEqual(opener.requests, [])  # the constructor itself does not touch the network
        self.assertEqual(client.version(), {"server_version": "17.0-fake"})
        self.assertEqual(client.login(), FAKE_UID)

        self.assertEqual(len(opener.requests), 2)
        for request in opener.requests:
            self.assertTrue(request.full_url.endswith("/jsonrpc"))
            self.assertEqual(request.full_url, "https://my-instance.example.com/jsonrpc")
            headers = {name.lower(): value for name, value in request.header_items()}
            self.assertEqual(headers["user-agent"], "odoo-kpi-skills/" + odoo_rpc.VERSION)
            self.assertEqual(request.get_method(), "POST")
            self.assertIn('"service": "common"', request.data.decode("utf-8"))
        self.assertEqual(opener.timeouts, [12, 12])

    def test_confirm_is_accepted_case_insensitively_with_spaces(self):
        opener = RecordingOpener({"server_version": "17.0-fake"})
        client = odoo_rpc.Client(make_config("https://my-instance.example.com"),
                                 confirm_instance="  My-Instance.Example.COM ", opener=opener)
        client.version()
        self.assertEqual(len(opener.requests), 1)

    def test_local_url_needs_no_confirm(self):
        opener = RecordingOpener({"server_version": "17.0-fake"})
        client = odoo_rpc.Client(make_config("http://localhost:8069"), opener=opener)
        client.version()
        self.assertEqual(opener.requests[0].full_url, "http://localhost:8069/jsonrpc")


if __name__ == "__main__":
    import unittest
    unittest.main()
