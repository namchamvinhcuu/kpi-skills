"""Command line `main(argv, out, err)` of scripts/odoo_rpc.py."""
import io
import os
import unittest
from unittest import mock

from fake_odoo import FAKE_DB, FAKE_KEY, FAKE_LOGIN, FAKE_UID, FakeOdoo
from helpers import IsolatedTestCase, closed_port, ini_text, odoo_rpc, write_bytes, write_file

POSIX_ONLY = unittest.skipIf(os.name == "nt", "POSIX mode bits only; Windows uses the icacls guard")


class CliTestCase(IsolatedTestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = odoo_rpc.main(list(argv), out, err)
        return code, out.getvalue(), err.getvalue()

    def set_env_config(self, url, key=FAKE_KEY):
        os.environ.update({"ODOO_KPI_URL": url, "ODOO_KPI_DB": FAKE_DB, "ODOO_KPI_LOGIN": FAKE_LOGIN,
                           "ODOO_KPI_API_KEY": key})


class ConfigPathCommandTests(CliTestCase):
    def test_config_path_prints_the_default_path_and_returns_0(self):
        code, out, err = self.run_main("config-path")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), odoo_rpc.config_path())
        self.assertEqual(err, "")

    def test_config_path_honours_the_environment_override(self):
        os.environ["ODOO_KPI_CONFIG"] = os.path.join(self.tmp, "mine.ini")
        code, out, _ = self.run_main("config-path")
        self.assertEqual((code, out.strip()), (0, os.path.join(self.tmp, "mine.ini")))

    def test_config_path_prints_the_given_config_option(self):
        code, out, _ = self.run_main("config-path", "--config", "/some/where.ini")
        self.assertEqual((code, out.strip()), (0, "/some/where.ini"))

    def test_config_path_does_not_need_or_read_a_config(self):
        code, out, err = self.run_main("config-path")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertFalse(os.path.exists(out.strip()))


class PingTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeOdoo()
        self.fake.start()
        self.addCleanup(self.fake.stop)

    @POSIX_ONLY
    def test_ping_against_the_fake_server_returns_0_and_prints_no_secret(self):
        text = ini_text(self.fake.url)
        path = write_file(self.tmp, text, 0o600)

        code, out, err = self.run_main("ping", "--config", path)

        self.assertEqual(code, 0, err)
        self.assertIn("instance: 127.0.0.1", out)
        self.assertIn("server_version: 17.0-fake", out)
        self.assertIn("login: ok (uid %d)" % FAKE_UID, out)
        combined = out + err
        self.assertNotIn(FAKE_KEY, combined)
        self.assertNotIn(text, combined)
        for line in text.splitlines():
            if line.startswith("api_key"):
                self.assertNotIn(line, combined)
        self.assertEqual(err, "")

    def test_ping_from_environment_variables_returns_0_and_prints_no_secret(self):
        self.set_env_config(self.fake.url)
        code, out, err = self.run_main("ping")
        self.assertEqual(code, 0, err)
        self.assertNotIn(FAKE_KEY, out + err)
        self.assertEqual(self.fake.requests[0]["headers"]["user-agent"], "odoo-kpi-skills/" + odoo_rpc.VERSION)

    def test_ping_sends_version_then_login_only(self):
        self.set_env_config(self.fake.url)
        self.run_main("ping")
        self.assertEqual([r["body"]["params"]["method"] for r in self.fake.requests], ["version", "login"])

    def test_login_failure_returns_1_and_prints_no_secret(self):
        self.fake.mode = "login_false"
        self.set_env_config(self.fake.url)
        code, out, err = self.run_main("ping")
        self.assertEqual(code, 1)
        self.assertIn("login failed", err)
        self.assertNotIn(FAKE_KEY, out + err)
        self.assertNotIn("login: ok", out)

    def test_server_error_echoing_the_key_returns_1_and_prints_no_secret(self):
        self.fake.mode = "error_echo"
        self.set_env_config(self.fake.url)
        code, out, err = self.run_main("ping")
        self.assertEqual(code, 1)
        self.assertIn("***", err)
        self.assertNotIn(FAKE_KEY, out + err)

    def test_cloudflare_block_returns_1_and_mentions_1010(self):
        self.fake.mode = "cf_1010"
        self.set_env_config(self.fake.url)
        code, out, err = self.run_main("ping")
        self.assertEqual(code, 1)
        self.assertIn("1010", err)

    def test_valid_json_that_is_no_object_returns_1_not_a_traceback(self):
        self.fake.mode = "json_list"
        self.set_env_config(self.fake.url)
        code, out, err = self.run_main("ping")
        self.assertEqual(code, 1)
        self.assertNotIn(FAKE_KEY, out + err)

    def test_unreachable_server_returns_1(self):
        self.set_env_config("http://127.0.0.1:%d" % closed_port())
        code, out, err = self.run_main("ping")
        self.assertEqual(code, 1)
        self.assertIn("cannot reach", err)
        self.assertNotIn(FAKE_KEY, out + err)


class PingOutputTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeOdoo()
        self.fake.start()
        self.addCleanup(self.fake.stop)
        self.set_env_config(self.fake.url)

    def test_ping_prints_exactly_four_lines_in_order_with_config_env(self):
        code, out, err = self.run_main("ping")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["instance: 127.0.0.1", "config: env", "server_version: 17.0-fake",
                                            "login: ok (uid %d)" % FAKE_UID])

    @POSIX_ONLY
    def test_ping_prints_config_file_when_the_config_comes_from_a_file(self):
        path = write_file(self.tmp, ini_text(self.fake.url), 0o600)
        for name in ("ODOO_KPI_URL", "ODOO_KPI_DB", "ODOO_KPI_LOGIN", "ODOO_KPI_API_KEY"):
            os.environ.pop(name, None)
        code, out, err = self.run_main("ping", "--config", path)
        self.assertEqual(code, 0, err)
        self.assertIn("config: file", out.splitlines())

    def test_a_plain_version_string_is_shown(self):
        for version in ("17.0", "17.0+e", "17.0-20260528", "17.0 beta_1", "17.0.1~rc1"):
            with self.subTest(version=version):
                self.fake.overrides["server_version"] = version
                code, out, err = self.run_main("ping")
                self.assertIn("server_version: %s" % version, out.splitlines())

    def test_a_version_must_start_with_major_dot_minor(self):
        # "9" alone, letters first, a dot without minor, a leading space: none is a version shape
        for version in ("9", "17", "17.", ".17", "v17.0", "abc", " 17.0", "17-0", "-17.0", "~17.0", "17.x"):
            with self.subTest(version=version):
                self.fake.overrides["server_version"] = version
                code, out, err = self.run_main("ping")
                self.assertEqual(code, 0, err)
                self.assertIn("server_version: unknown", out.splitlines())

    def test_a_version_with_characters_outside_the_alphabet_prints_unknown(self):
        for version in ("saas~17.2", "17.0/x", "17.0:y", "17.0(e)"):
            with self.subTest(version=version):
                self.fake.overrides["server_version"] = version
                self.assertIn("server_version: unknown", self.run_main("ping")[1].splitlines())

    def test_a_version_equal_to_a_realistic_forty_hex_key_prints_unknown(self):
        hex_key = "0123456789abcdef0123456789abcdef01234567"  # secret-allow: fixture, 40 hex like an Odoo API key
        with FakeOdoo(key=hex_key) as fake:
            fake.overrides["server_version"] = hex_key
            self.set_env_config(fake.url, key=hex_key)
            code, out, err = self.run_main("ping")
        self.assertEqual(code, 0, err)
        self.assertNotIn(hex_key, out + err)
        self.assertIn("server_version: unknown", out.splitlines())

    def test_a_key_shaped_version_prints_unknown_even_when_it_is_not_the_configured_key(self):
        # the shape alone must refuse it (the configured key is different, so the substring check cannot help)
        for length in (39, 40):
            for text in ("0123456789abcdef" * 3, "a1b2c3d4e5f60718" * 3):
                version = text[:length]
                with self.subTest(version=version):
                    self.fake.overrides["server_version"] = version
                    code, out, err = self.run_main("ping")
                    self.assertEqual(code, 0, err)
                    self.assertIn("server_version: unknown", out.splitlines())
                    self.assertNotIn(version, out + err)

    def test_a_version_that_contains_the_configured_key_prints_unknown(self):
        # a SHORT key so that "17.0 <key>" still fits the shape (<= 40 characters): only the key check can refuse it
        short_key = "k3y-s3cret"  # secret-allow: fixture
        for version in ("17.0 " + short_key, "17.0-" + short_key, "17.0" + short_key, "17.0 x" + short_key + "y"):
            with self.subTest(version=version):
                with FakeOdoo(key=short_key) as fake:
                    fake.overrides["server_version"] = version
                    self.set_env_config(fake.url, key=short_key)
                    code, out, err = self.run_main("ping")
                self.assertEqual(code, 0, err)
                self.assertNotIn(short_key, out + err)
                self.assertIn("server_version: unknown", out.splitlines())

    def test_a_version_that_is_the_key_itself_prints_unknown(self):
        # the key "17.0" makes the version "17.0" key-bearing: the check is a substring test, not a shape test
        with FakeOdoo(key="17.0") as fake:  # secret-allow: fixture
            fake.overrides["server_version"] = "17.0"
            self.set_env_config(fake.url, key="17.0")
            code, out, err = self.run_main("ping")
        self.assertEqual(code, 0, err)
        self.assertIn("server_version: unknown", out.splitlines())

    def test_a_version_that_only_resembles_the_key_is_still_shown(self):
        # key "k3y-s3cret": "17.0 k3y-s3cre" (one character short) does not contain it
        short_key = "k3y-s3cret"  # secret-allow: fixture
        with FakeOdoo(key=short_key) as fake:
            fake.overrides["server_version"] = "17.0 k3y-s3cre"
            self.set_env_config(fake.url, key=short_key)
            code, out, err = self.run_main("ping")
        self.assertEqual(code, 0, err)
        self.assertIn("server_version: 17.0 k3y-s3cre", out.splitlines())

    def test_a_forty_character_version_is_shown_and_a_forty_first_is_not(self):
        shown, refused = "9." + "9" * 38, "9." + "9" * 39
        self.assertEqual((len(shown), len(refused)), (40, 41))
        self.fake.overrides["server_version"] = shown
        self.assertIn("server_version: " + shown, self.run_main("ping")[1].splitlines())
        self.fake.overrides["server_version"] = refused
        self.assertIn("server_version: unknown", self.run_main("ping")[1].splitlines())

    def test_a_version_followed_by_forty_more_characters_prints_unknown(self):
        version = "17.0" + "a" * 40
        self.fake.overrides["server_version"] = version
        code, out, err = self.run_main("ping")
        self.assertEqual(code, 0, err)
        self.assertIn("server_version: unknown", out.splitlines())
        self.assertNotIn(version, out + err)

    def test_an_empty_version_prints_unknown(self):
        self.fake.overrides["server_version"] = ""
        self.assertIn("server_version: unknown", self.run_main("ping")[1].splitlines())

    def test_hostile_versions_print_unknown_and_never_the_key_or_escapes(self):
        hostile = ("\x1b[31m17.0\x1b[0m", "17.0\n", "17.0\nlogin: ok (uid 1)", "key " + FAKE_KEY, FAKE_KEY, "",
                   "17.0\x00", "<script>", "17.0;rm", "한국어", "17.0‮", "a" * 500)
        for version in hostile:
            with self.subTest(version=version):
                self.fake.overrides["server_version"] = version
                code, out, err = self.run_main("ping")
                self.assertEqual(code, 0, err)
                self.assertIn("server_version: unknown", out.splitlines())
                self.assertNotIn(FAKE_KEY, out + err)
                self.assertNotIn("\x1b", out + err)
                self.assertEqual(len(out.splitlines()), 4)

    def test_a_version_that_is_not_a_string_prints_unknown(self):
        for version in (17.0, 17, None, ["17.0"], {"a": 1}, True):
            with self.subTest(version=version):
                self.fake.overrides["server_version"] = version
                self.assertIn("server_version: unknown", self.run_main("ping")[1].splitlines())

    def test_a_version_answer_that_is_not_an_object_prints_unknown(self):
        for answer in ("17.0", ["17.0"], 17, None):
            with self.subTest(answer=answer):
                self.fake.overrides["version_result"] = answer
                code, out, err = self.run_main("ping")
                self.assertEqual(code, 0, err)
                self.assertIn("server_version: unknown", out.splitlines())

    def test_the_uid_is_printed_as_an_int(self):
        self.fake.overrides["login"] = 4242
        self.assertIn("login: ok (uid 4242)", self.run_main("ping")[1].splitlines())

    def test_an_odd_uid_is_a_login_failure_not_a_printed_value(self):
        for bad in ("7; rm -rf", "abc", True, 0, -1, 1.5):
            with self.subTest(uid=bad):
                self.fake.overrides["login"] = bad
                code, out, err = self.run_main("ping")
                self.assertEqual(code, 1)
                self.assertNotIn("login: ok", out)
                self.assertIn("login failed", err)


class UnexpectedErrorTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.set_env_config("http://127.0.0.1:1")

    def test_an_unexpected_exception_gives_one_line_no_traceback_and_no_key(self):
        boom = RuntimeError("internal state: key=%s" % FAKE_KEY)
        with mock.patch.object(odoo_rpc.Client, "version", side_effect=boom):
            code, out, err = self.run_main("ping")
        self.assertEqual(code, 1)
        self.assertEqual(err, "error: unexpected RuntimeError\n")
        self.assertEqual(out, "")
        self.assertNotIn(FAKE_KEY, out + err)
        self.assertNotIn("Traceback", out + err)

    def test_the_same_for_other_exception_types_and_for_the_login_step(self):
        cases = ((ValueError("k=%s" % FAKE_KEY), "version", "ValueError"),
                 (KeyError(FAKE_KEY), "login", "KeyError"), (ZeroDivisionError(), "version", "ZeroDivisionError"),
                 (AttributeError("x %s" % FAKE_KEY), "login", "AttributeError"))
        for exc, where, name in cases:
            with self.subTest(exc=name, where=where):
                fake_version = {"server_version": "17.0"}
                with mock.patch.object(odoo_rpc.Client, "version", return_value=fake_version), \
                        mock.patch.object(odoo_rpc.Client, where, side_effect=exc):
                    code, out, err = self.run_main("ping")
                self.assertEqual((code, err), (1, "error: unexpected %s\n" % name))
                self.assertNotIn(FAKE_KEY, out + err)

    def test_known_errors_keep_their_own_exit_codes(self):
        with mock.patch.object(odoo_rpc.Client, "version", side_effect=odoo_rpc.RpcError("rpc text")):
            self.assertEqual(self.run_main("ping")[0], 1)
        with mock.patch.object(odoo_rpc.Client, "version", side_effect=odoo_rpc.GuardError("guard text")):
            code, out, err = self.run_main("ping")
        self.assertEqual((code, err), (2, "error: guard text\n"))

    def test_keyboard_interrupt_is_not_swallowed(self):
        with mock.patch.object(odoo_rpc.Client, "version", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_main("ping")


class CheckConfigCommandTests(CliTestCase):
    def patch_network(self):
        resolver = mock.patch("socket.getaddrinfo", side_effect=OSError("network blocked in tests"))
        connector = mock.patch("socket.socket.connect", side_effect=OSError("network blocked in tests"))
        self.getaddrinfo, self.connect = resolver.start(), connector.start()
        self.addCleanup(resolver.stop)
        self.addCleanup(connector.stop)

    def test_env_config_prints_config_env_and_returns_0(self):
        self.patch_network()
        self.set_env_config("https://my-instance.example.com")
        code, out, err = self.run_main("check-config")
        # plan kpi-multi-project step 3: a second static line says which project is set (one shipped catalog: not set)
        self.assertEqual((code, out, err), (0, "config: env\nproject: not set (installed: dksk)\n", ""))
        self.getaddrinfo.assert_not_called()
        self.connect.assert_not_called()

    @POSIX_ONLY
    def test_private_file_prints_permissions_ok_and_returns_0(self):
        self.patch_network()
        text = ini_text("https://my-instance.example.com")
        path = write_file(self.tmp, text, 0o600)
        code, out, err = self.run_main("check-config", "--config", path)
        self.assertEqual((code, out, err), (0, "config: file, private (permissions ok)\n"
                                               "project: not set (installed: dksk)\n", ""))
        self.assertNotIn(FAKE_KEY, out + err)
        self.getaddrinfo.assert_not_called()

    @POSIX_ONLY
    def test_world_readable_file_returns_2(self):
        path = write_file(self.tmp, ini_text("https://my-instance.example.com"), 0o644)
        code, out, err = self.run_main("check-config", "--config", path)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("chmod 600", err)
        self.assertNotIn(FAKE_KEY, out + err)

    def test_missing_file_returns_2_naming_the_path(self):
        path = os.path.join(self.tmp, "nope.ini")
        code, out, err = self.run_main("check-config", "--config", path)
        self.assertEqual(code, 2)
        self.assertIn(path, err)
        self.assertEqual(out, "")

    @POSIX_ONLY
    def test_bad_content_returns_2_without_echoing_it(self):
        for label, data in (("no section", b"api_key = " + FAKE_KEY.encode() + b"\n"),  # secret-allow: fixture
                            ("invalid utf-8", b"[odoo]\nurl = \xff\xfe\n"),
                            ("missing key", b"[odoo]\nurl = https://my-instance.example.com\n"),
                            ("bad url", ini_text("my-instance.example.com").encode())):
            with self.subTest(content=label):
                path = write_bytes(self.tmp, data, 0o600, name="bad.ini")
                code, out, err = self.run_main("check-config", "--config", path)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertNotIn(FAKE_KEY, out + err)

    def test_a_bad_url_in_the_environment_returns_2(self):
        self.set_env_config("https://good#x")
        code, out, err = self.run_main("check-config")
        self.assertEqual((code, out), (2, ""))

    @POSIX_ONLY
    def test_a_bom_config_file_is_accepted(self):
        path = write_bytes(self.tmp, b"\xef\xbb\xbf" + ini_text("https://my-instance.example.com").encode())
        code, out, err = self.run_main("check-config", "--config", path)
        self.assertEqual((code, err), (0, ""))

    def test_check_config_does_not_need_a_confirmed_instance(self):
        self.set_env_config("https://my-instance.example.com")
        self.assertEqual(self.run_main("check-config")[0], 0)


class NonLocalHostTests(CliTestCase):
    URL = "https://my-instance.example.com"

    def patch_network(self):
        """Any attempt to resolve or connect becomes visible: a mock records it (and fails the connection)."""
        resolver = mock.patch("socket.getaddrinfo", side_effect=OSError("network blocked in tests"))
        connector = mock.patch("socket.socket.connect", side_effect=OSError("network blocked in tests"))
        self.getaddrinfo = resolver.start()
        self.connect = connector.start()
        self.addCleanup(resolver.stop)
        self.addCleanup(connector.stop)

    def test_non_local_host_without_confirm_returns_2_and_opens_no_socket(self):
        self.patch_network()
        self.set_env_config(self.URL)
        code, out, err = self.run_main("ping")
        self.assertEqual(code, 2)
        self.assertIn("my-instance", err)
        self.assertIn("--confirm-instance", err)
        self.assertNotIn(FAKE_KEY, out + err)
        self.getaddrinfo.assert_not_called()
        self.connect.assert_not_called()

    def test_wrong_confirm_returns_2_and_opens_no_socket(self):
        self.patch_network()
        self.set_env_config(self.URL)
        code, out, err = self.run_main("ping", "--confirm-instance", "somebody-else")
        self.assertEqual(code, 2)
        self.getaddrinfo.assert_not_called()
        self.connect.assert_not_called()

    def test_plain_http_non_local_returns_2_even_with_the_right_confirm(self):
        self.patch_network()
        self.set_env_config("http://my-instance.example.com")
        code, out, err = self.run_main("ping", "--confirm-instance", "my-instance.example.com")
        self.assertEqual(code, 2)
        self.assertIn("https", err)
        self.getaddrinfo.assert_not_called()

    def test_the_first_label_alone_returns_2_and_opens_no_socket(self):
        # round 3 (security r2): the confirmation is the whole host name; the first label no longer unlocks the host
        self.patch_network()
        self.set_env_config(self.URL)
        code, out, err = self.run_main("ping", "--confirm-instance", "my-instance")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("type the full host name", err)
        self.getaddrinfo.assert_not_called()
        self.connect.assert_not_called()

    def test_a_look_alike_url_is_refused_with_the_first_label_and_with_the_legitimate_host(self):
        self.patch_network()
        for url in ("https://my-instance.attacker.example", "https://my-instance.example.com.attacker.example"):
            for typed in ("my-instance", "my-instance.example.com"):
                with self.subTest(url=url, typed=typed):
                    self.set_env_config(url)
                    code, out, err = self.run_main("ping", "--confirm-instance", typed)
                    self.assertEqual((code, out), (2, ""))
                    self.assertIn("type the full host name", err)
                    self.assertNotIn(FAKE_KEY, out + err)
                    self.getaddrinfo.assert_not_called()
                    self.connect.assert_not_called()

    def test_right_confirm_passes_the_guard_and_then_fails_on_the_blocked_network(self):
        self.patch_network()
        self.set_env_config(self.URL)
        code, out, err = self.run_main("ping", "--confirm-instance", "My-Instance.Example.com")
        self.assertEqual(code, 1)  # past the guard: the (blocked) network is what failed
        self.assertIn("cannot reach", err)
        self.assertNotIn(FAKE_KEY, out + err)
        self.assertTrue(self.getaddrinfo.called)


class BadConfigTests(CliTestCase):
    def test_missing_config_file_returns_2_and_names_the_path(self):
        path = os.path.join(self.tmp, "nope.ini")
        code, out, err = self.run_main("ping", "--config", path)
        self.assertEqual(code, 2)
        self.assertIn(path, err)
        self.assertEqual(out, "")

    def test_default_location_without_a_file_returns_2(self):
        code, out, err = self.run_main("ping")
        self.assertEqual(code, 2)
        self.assertIn(odoo_rpc.config_path(), err)

    @POSIX_ONLY
    def test_world_readable_config_returns_2_and_prints_no_secret(self):
        path = write_file(self.tmp, ini_text("http://127.0.0.1:1"), 0o644)
        code, out, err = self.run_main("ping", "--config", path)
        self.assertEqual(code, 2)
        self.assertIn("chmod 600", err)
        self.assertNotIn(FAKE_KEY, out + err)

    @POSIX_ONLY
    def test_config_with_a_missing_key_returns_2(self):
        path = write_file(self.tmp, "[odoo]\nurl = http://127.0.0.1:1\ndb = d\nlogin = l\n", 0o600)
        code, out, err = self.run_main("ping", "--config", path)
        self.assertEqual(code, 2)
        self.assertIn("api_key", err)

    def test_url_without_scheme_in_the_environment_returns_2(self):
        self.set_env_config("my-instance.example.com")
        code, out, err = self.run_main("ping")
        self.assertEqual(code, 2)
        self.assertNotIn(FAKE_KEY, out + err)

    def test_the_parser_lists_init_config_and_the_old_commands(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            with self.assertRaises(SystemExit) as caught:
                odoo_rpc.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        for command in ("ping", "config-path", "check-config", "init-config"):
            self.assertIn(command, out.getvalue())

    def test_init_config_inside_claude_code_exits_2_and_writes_nothing(self):
        # the real environment of this very command: the guard must refuse it (Plan kpi-skill-improvements AC5)
        target = os.path.join(self.tmp, "cfg", "config.ini")
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            code, out, err = self.run_main("init-config", "--config", target)
        self.assertEqual(code, 2)
        self.assertIn("Claude Code", err)
        self.assertEqual(out, "")
        self.assertFalse(os.path.exists(os.path.dirname(target)))

    def test_init_config_without_a_terminal_exits_2_and_writes_nothing(self):
        target = os.path.join(self.tmp, "cfg", "config.ini")
        code, out, err = self.run_main("init-config", "--config", target)      # `out` is a StringIO: not a terminal
        self.assertEqual(code, 2)
        self.assertIn("terminal", err)
        self.assertFalse(os.path.exists(os.path.dirname(target)))

    def test_unknown_command_exits_with_usage_error(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as caught:
                self.run_main("frobnicate")
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
