"""Command line, exit codes, instance guard and secret hygiene of scripts/publish_dashboard.py (fake Odoo, 127.0.0.1)."""
import io
import os
import unittest
from unittest import mock

from dashboard_fixtures import GROUP, Scenario, StreamCase, start_fake, use_qaqc_env, write_dashboard_file
from fake_odoo import FAKE_DB, FAKE_KEY, FAKE_LOGIN, FAKE_UID
from helpers import ExplodingOpener, RecordingOpener, ini_text, odoo_rpc, write_file

import publish_dashboard

POSIX_ONLY = unittest.skipIf(os.name == "nt", "POSIX mode bits only")


class CliCase(StreamCase):
    def setUp(self):
        super().setUp()
        self.fake = start_fake(self)
        self.s = Scenario(self.fake)
        use_qaqc_env(self.fake.url)
        self.file = write_dashboard_file(self.tmp)

    def run_pub(self, *argv, **kwargs):
        return self.collect(publish_dashboard.main, argv, **kwargs)


def factory_with(opener):
    return lambda config, confirm_instance=None: odoo_rpc.Client(config, confirm_instance=confirm_instance, opener=opener)


class InstanceGuardTests(StreamCase):
    """AC9, re-checked on the publisher: a non-local host needs the typed instance name, https only."""

    URL = "https://my-instance.example.com"

    def setUp(self):
        super().setUp()
        os.environ.update({"ODOO_KPI_URL": self.URL, "ODOO_KPI_DB": FAKE_DB, "ODOO_KPI_LOGIN": FAKE_LOGIN,
                           "ODOO_KPI_API_KEY": FAKE_KEY})
        self.file = write_dashboard_file(self.tmp)

    def commands(self):
        return (["list"], ["publish", "--dashboard", self.file, "--name", "x", "--apply"],
                ["archive", "--id", "5", "--apply"], ["delete-own", "--id", "5", "--apply"],
                ["publish", "--dashboard", self.file, "--replace", "5", "--confirm-replace", "5", "--apply"])

    def test_a_non_local_host_without_the_confirmation_never_reaches_the_network(self):
        for argv in self.commands():
            # round 3: the confirmation is the WHOLE host name. "my-instance.example.com" was in this list while the
            # first label was the rule (it is now the right answer, see below); the first label itself takes its place
            for confirm in ([], ["--confirm-instance", ""], ["--confirm-instance", "other"],
                            ["--confirm-instance", "my-instance"], ["--confirm-instance", "MY-INSTANCE"],
                            ["--confirm-instance", "my-instance.example"],
                            ["--confirm-instance", "my-instance.example.com.attacker.example"],
                            ["--confirm-instance", "my-instance.example.com:443"]):
                with self.subTest(argv=argv[0], confirm=confirm):
                    opener = ExplodingOpener()
                    code, out, err = self.collect(publish_dashboard.main, argv + confirm,
                                                  client_factory=factory_with(opener))
                    self.assertEqual(code, 2, err)
                    self.assertEqual(out, "")
                    self.assertIn("--confirm-instance", err)
                    self.assertEqual(opener.calls, 0)

    def test_a_look_alike_host_never_reaches_the_network_typed_with_the_label_or_the_legitimate_host(self):
        # round 3 (security r2): the API key goes to the URL; the first label alone must not unlock a look-alike host
        for url in ("https://my-instance.attacker.example", "https://my-instance.example.com.attacker.example"):
            os.environ["ODOO_KPI_URL"] = url
            for argv in self.commands():
                for typed in ("my-instance", "my-instance.example.com"):
                    with self.subTest(url=url, argv=argv[0], typed=typed):
                        opener = ExplodingOpener()
                        code, out, err = self.collect(publish_dashboard.main, argv + ["--confirm-instance", typed],
                                                      client_factory=factory_with(opener))
                        self.assertEqual(code, 2, err)
                        self.assertEqual(out, "")
                        self.assertIn("type the full host name", err)
                        self.assertNotIn(FAKE_KEY, out + err)
                        self.assertEqual(opener.calls, 0)

    def test_plain_http_to_a_non_local_host_is_refused_even_with_the_right_confirmation(self):
        os.environ["ODOO_KPI_URL"] = "http://my-instance.example.com"
        for typed in ("my-instance", "my-instance.example.com"):
            with self.subTest(typed=typed):
                opener = ExplodingOpener()
                code, out, err = self.collect(publish_dashboard.main, ["list", "--confirm-instance", typed],
                                              client_factory=factory_with(opener))
                self.assertEqual(code, 2, err)
                self.assertIn("https", err)
                self.assertEqual(opener.calls, 0)

    def test_the_right_confirmation_lets_the_request_through(self):
        for typed in ("my-instance.example.com", "MY-INSTANCE.EXAMPLE.COM", " my-instance.example.com "):
            with self.subTest(typed=typed):
                opener = RecordingOpener(False)          # the login answers `false`: stops right after the first request
                code, out, err = self.collect(publish_dashboard.main, ["list", "--confirm-instance", typed],
                                              client_factory=factory_with(opener))
                self.assertEqual(len(opener.requests), 1)
                self.assertEqual(code, 1)
                self.assertIn("login failed", err)

    def test_the_confirmation_option_is_passed_on_to_the_client(self):
        seen = []

        def factory(config, confirm_instance=None):
            seen.append(confirm_instance)
            return odoo_rpc.Client(config, confirm_instance=confirm_instance, opener=RecordingOpener(False))

        self.collect(publish_dashboard.main, ["list", "--confirm-instance", "my-instance.example.com"],
                     client_factory=factory)
        self.assertEqual(seen, ["my-instance.example.com"])


class ConfigTests(CliCase):
    def test_no_config_at_all_is_a_clear_refusal_without_a_request(self):
        for name in ("ODOO_KPI_URL", "ODOO_KPI_DB", "ODOO_KPI_LOGIN", "ODOO_KPI_API_KEY"):
            os.environ.pop(name)
        code, out, err = self.run_pub("list")
        self.assertEqual(code, 2, err)
        self.assertIn("no config", err)
        self.assertEqual(self.fake.requests, [])

    @POSIX_ONLY
    def test_a_config_file_open_to_others_is_refused(self):
        for name in ("ODOO_KPI_URL", "ODOO_KPI_DB", "ODOO_KPI_LOGIN", "ODOO_KPI_API_KEY"):
            os.environ.pop(name)
        path = write_file(self.tmp, ini_text(self.fake.url), 0o644)
        code, out, err = self.run_pub("list", "--config", path)
        self.assertEqual(code, 2, err)
        self.assertEqual(self.fake.requests, [])

    @POSIX_ONLY
    def test_a_private_config_file_given_with_the_option_is_used(self):
        for name in ("ODOO_KPI_URL", "ODOO_KPI_DB", "ODOO_KPI_LOGIN", "ODOO_KPI_API_KEY"):
            os.environ.pop(name)
        path = write_file(self.tmp, ini_text(self.fake.url), 0o600)
        code, out, err = self.run_pub("list", "--config", path)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.splitlines()), 2)


class ExitCodeAndSecretTests(CliCase):
    def test_a_failed_login_is_exit_1_and_names_no_secret(self):
        self.fake.mode = "login_false"
        code, out, err = self.run_pub("list")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("login failed", err)

    def test_server_errors_that_echo_the_key_never_print_it(self):
        for mode in ("error_echo", "error_echo_top", "http_500", "http_403_plain", "cf_1010", "cf_other", "non_json",
                     "json_list", "cf_502"):
            for argv in (["list"], ["archive", "--id", "%d" % self.s.own, "--apply"]):
                with self.subTest(mode=mode, command=argv[0]):
                    self.fake.mode = mode
                    code, out, err = self.run_pub(*argv)
                    self.assertEqual(code, 1, err)
                    self.assertEqual(out, "")
                    self.assertEqual(err.count("\n"), 1)
        self.fake.mode = "ok"
        self.assertEqual(self.fake.write_requests(), [])

    def test_an_odoo_error_whose_text_holds_the_key_is_redacted(self):
        # the key sits in the message of a refused write (here: the unlink of a dashboard)
        from fake_odoo import DocumentsError
        self.s.world.metadata_override[self.s.own] = DocumentsError("refused for key %s" % FAKE_KEY)
        code, out, err = self.run_pub("archive", "--id", str(self.s.own), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("***", err)

    def test_an_unexpected_exception_prints_one_line_and_never_its_text(self):
        def exploding(config, confirm_instance=None):
            raise RuntimeError("detail with %s inside" % FAKE_KEY)

        code, out, err = self.run_pub("list", client_factory=exploding)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "error: unexpected RuntimeError\n")

    def test_ctrl_c_is_exit_130(self):
        def interrupted(config, confirm_instance=None):
            raise KeyboardInterrupt()

        code, out, err = self.run_pub("list", client_factory=interrupted)
        self.assertEqual((code, out, err), (130, "", "interrupted\n"))

    def test_the_secret_is_in_no_output_of_a_whole_session(self):
        for argv in (["list"], ["publish", "--dashboard", self.file, "--name", "x", "--apply"],
                     ["publish", "--dashboard", self.file, "--name", "x"],
                     ["archive", "--id", str(self.s.foreign), "--apply"],
                     ["delete-own", "--id", str(self.s.own), "--apply"]):
            self.run_pub(*argv)
        combined = "".join(o + e for o, e in self.streams)
        self.assertNotIn(FAKE_KEY, combined)
        self.assertTrue(combined)

    def test_stdout_of_a_dry_run_is_empty_and_the_chatter_goes_to_stderr(self):
        for argv in (["publish", "--dashboard", self.file, "--name", "x"], ["archive", "--id", str(self.s.own)],
                     ["delete-own", "--id", str(self.s.own)]):
            with self.subTest(command=argv[0]):
                code, out, err = self.run_pub(*argv)
                self.assertEqual((code, out), (0, ""))
                self.assertIn("will ", err)

    def test_a_connection_that_cannot_be_made_is_exit_1(self):
        from helpers import closed_port
        use_qaqc_env("http://127.0.0.1:%d" % closed_port())
        code, out, err = self.run_pub("list")
        self.assertEqual(code, 1)
        self.assertIn("cannot reach", err)


class ArgumentTests(CliCase):
    def parse_fails(self, *argv):
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as caught:
            publish_dashboard.main(list(argv), io.StringIO(), io.StringIO())
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.fake.requests, [])

    def test_bad_ids_are_refused_by_the_parser_before_any_request(self):
        for command in ("archive", "delete-own"):
            for bad in ("0", "-3", "abc", "1.5", ""):
                with self.subTest(command=command, id=bad):
                    self.parse_fails(command, "--id", bad)

    def test_a_missing_id_or_dashboard_is_refused_by_the_parser(self):
        self.parse_fails("archive")
        self.parse_fails("delete-own")
        self.parse_fails("publish", "--name", "x")

    def test_bad_replace_ids_are_refused_by_the_parser(self):
        for bad in ("0", "-1", "x"):
            self.parse_fails("publish", "--dashboard", self.file, "--replace", bad)
            self.parse_fails("publish", "--dashboard", self.file, "--replace", "5", "--confirm-replace", bad)

    def test_unknown_commands_and_options_are_refused(self):
        self.parse_fails()
        self.parse_fails("destroy")
        self.parse_fails("list", "--apply")                 # `list` cannot write: it has no --apply at all
        self.parse_fails("archive", "--id", "5", "--force")

    def test_abbreviated_options_are_refused_by_the_parser_before_any_request(self):
        # round 2 (security r1): `--a` must not turn a dry run into a write, nor `--rep` / `--confirm-r` stand for the
        # guard options: every prefix is refused (allow_abbrev=False) whatever the command
        f = self.file
        cases = [["publish", "--dashboard", f, "--name", "x", flag] for flag in ("--a", "--ap", "--app", "--appl")]
        cases += [["archive", "--id", "5", flag] for flag in ("--a", "--ap", "--app", "--appl")]
        cases += [["delete-own", "--id", "5", flag] for flag in ("--a", "--ap", "--app", "--appl")]
        cases += [["publish", "--dashboard", f, "--replace", "5", "--confirm-replace", "5", "--a"],
                  ["publish", "--dashboard", f, "--rep", "5", "--confirm-replace", "5", "--apply"],
                  ["publish", "--dashboard", f, "--replace", "5", "--confirm-r", "5", "--apply"],
                  ["publish", "--dashboard", f, "--replace", "5", "--confirm-replace", "5", "--rep", "5"],
                  ["publish", "--dash", f, "--name", "x", "--apply"], ["publish", "--dashboard", f, "--nam", "x", "--apply"],
                  ["archive", "--i", "5", "--apply"], ["delete-own", "--i", "5", "--apply"],
                  ["archive", "--id", "5", "--confirm-i", "x"], ["list", "--confirm-i", "x"], ["list", "--confi", "x"],
                  ["publish", "--dashboard", f, "--name", "x", "--confirm-i", "x"],
                  ["list", "--conf", "x"], ["list", "--co", "x"]]
        for argv in cases:
            with self.subTest(argv=argv[-3:]):
                self.parse_fails(*argv)

    def test_the_main_parser_refuses_abbreviations_too(self):
        for flag in ("--he", "--hel"):
            with self.subTest(flag=flag):
                self.parse_fails(flag)

    def test_the_full_spellings_still_work(self):
        parser = publish_dashboard.build_parser()
        args = parser.parse_args(["publish", "--dashboard", "x", "--replace", "5", "--confirm-replace", "5", "--apply",
                                  "--confirm-instance", "i", "--config", "c.ini"])
        self.assertEqual((args.apply, args.replace, args.confirm_replace, args.confirm_instance, args.config),
                         (True, 5, 5, "i", "c.ini"))
        self.assertTrue(parser.parse_args(["archive", "--id", "5", "--apply"]).apply)
        self.assertTrue(parser.parse_args(["delete-own", "--id", "5", "--apply"]).apply)
        self.assertEqual(parser.parse_args(["publish", "--dashboard", "x", "--name", "n"]).name, "n")

    def test_every_command_offers_the_config_and_the_confirmation_options(self):
        for argv in (["list"], ["archive", "--id", "5"], ["delete-own", "--id", "5"],
                     ["publish", "--dashboard", "x", "--name", "n"]):
            with self.subTest(command=argv[0]):
                args = publish_dashboard.build_parser().parse_args(argv + ["--config", "c.ini", "--confirm-instance", "i"])
                self.assertEqual((args.config, args.confirm_instance), ("c.ini", "i"))

    def test_the_apply_flag_is_off_by_default_everywhere_it_exists(self):
        for argv in (["archive", "--id", "5"], ["delete-own", "--id", "5"], ["publish", "--dashboard", "x", "--name", "n"]):
            with self.subTest(command=argv[0]):
                self.assertFalse(publish_dashboard.build_parser().parse_args(argv).apply)

    def test_the_four_commands_are_exactly_the_documented_ones(self):
        self.assertEqual(sorted(publish_dashboard.COMMANDS), ["archive", "delete-own", "list", "publish"])
        self.assertEqual(publish_dashboard.GROUP_NAME, GROUP)


if __name__ == "__main__":
    unittest.main()
