"""Credential handling, instance guard, exit codes and secret hygiene of scripts/setup_instance.py (fake Odoo only)."""
import io
import os
import unittest
from unittest import mock

from dashboard_fixtures import (ADMIN_LOGIN, ADMIN_SECRET, ADMIN_UID, Scenario, StreamCase, start_fake, use_admin_env,
                                use_qaqc_env)
from fake_odoo import FAKE_DB, FAKE_KEY, FAKE_LOGIN, FAKE_UID
from helpers import ExplodingOpener, RecordingOpener, ini_text, odoo_rpc, write_file

import setup_instance

POSIX_ONLY = unittest.skipIf(os.name == "nt", "POSIX mode bits only")
ADMIN_VARS = ("ODOO_KPI_ADMIN_URL", "ODOO_KPI_ADMIN_DB", "ODOO_KPI_ADMIN_LOGIN", "ODOO_KPI_ADMIN_SECRET")
QAQC_VARS = ("ODOO_KPI_URL", "ODOO_KPI_DB", "ODOO_KPI_LOGIN", "ODOO_KPI_API_KEY")


class NoTerminalCase(StreamCase):
    SECRETS = (ADMIN_SECRET, FAKE_KEY)

    def setUp(self):
        super().setUp()
        patcher = mock.patch("sys.stdin", io.StringIO())
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_setup(self, *argv, **kwargs):
        return self.collect(setup_instance.main, argv, **kwargs)


def factory_with(opener):
    return lambda config, confirm_instance=None: odoo_rpc.Client(config, confirm_instance=confirm_instance, opener=opener)


class AdminConfigUnitTests(StreamCase):
    """`admin_config`: environment first, prompts only for what is missing, never a file, never a secret in a message."""

    FULL = {"ODOO_KPI_ADMIN_URL": "http://127.0.0.1:8069/", "ODOO_KPI_ADMIN_DB": "somedb",
            "ODOO_KPI_ADMIN_LOGIN": "boss", "ODOO_KPI_ADMIN_SECRET": ADMIN_SECRET}

    def no_prompt(self, *args):
        raise AssertionError("a prompt was shown")

    def test_a_complete_environment_is_used_without_any_prompt(self):
        config = setup_instance.admin_config(env=dict(self.FULL), interactive=True, ask=self.no_prompt,
                                             ask_secret=self.no_prompt)
        self.assertEqual((config.url, config.db, config.login, config.api_key, config.source),
                         ("http://127.0.0.1:8069", "somedb", "boss", ADMIN_SECRET, "admin"))

    def test_values_are_stripped(self):
        env = {k: "  %s \n" % v for k, v in self.FULL.items()}
        config = setup_instance.admin_config(env=env, interactive=False)
        self.assertEqual((config.db, config.login, config.api_key), ("somedb", "boss", ADMIN_SECRET))

    def test_the_qaqc_variables_are_never_read(self):
        env = dict(self.FULL, ODOO_KPI_URL="http://qa.invalid", ODOO_KPI_DB="qadb", ODOO_KPI_LOGIN="qa",
                   ODOO_KPI_API_KEY=FAKE_KEY)
        config = setup_instance.admin_config(env=env, interactive=False)
        self.assertEqual((config.db, config.login, config.api_key), ("somedb", "boss", ADMIN_SECRET))
        for name in ADMIN_VARS:
            partial = {k: v for k, v in env.items() if k != name}
            with self.subTest(missing=name), self.assertRaises(setup_instance.SetupError):
                setup_instance.admin_config(env=partial, interactive=False)

    def test_a_missing_value_without_a_terminal_names_the_variable_and_leaks_no_value(self):
        for name in ADMIN_VARS:
            for blank in (None, "", "   "):
                with self.subTest(missing=name, blank=blank):
                    env = dict(self.FULL)
                    if blank is None:
                        del env[name]
                    else:
                        env[name] = blank
                    with self.assertRaises(setup_instance.SetupError) as caught:
                        setup_instance.admin_config(env=env, interactive=False)
                    text = str(caught.exception)
                    self.assertIn(name, text)
                    for value in ("somedb", "boss", ADMIN_SECRET, "8069"):
                        self.assertNotIn(value, text)
                    self.assertIn("never put them in the QA/QC config file", text)

    def test_with_a_terminal_only_the_missing_values_are_asked_and_the_secret_is_not_echoed(self):
        asked, secret_asked = [], []
        env = {k: v for k, v in self.FULL.items() if k != "ODOO_KPI_ADMIN_SECRET"}
        config = setup_instance.admin_config(
            env=env, interactive=True, ask=lambda text: asked.append(text) or "unused",
            ask_secret=lambda text: secret_asked.append(text) or "  typed-secret  ")
        self.assertEqual(asked, [])
        self.assertEqual(len(secret_asked), 1)
        self.assertIn("not shown", secret_asked[0])
        self.assertEqual(config.api_key, "typed-secret")

    def test_with_a_terminal_everything_missing_is_asked_in_order(self):
        asked, secret_asked = [], []
        answers = iter(["http://127.0.0.1:1/", " dbx ", " boss "])
        config = setup_instance.admin_config(
            env={}, interactive=True, ask=lambda text: asked.append(text) or next(answers),
            ask_secret=lambda text: secret_asked.append(text) or "s3cret")
        self.assertEqual([a.split()[1] for a in asked], ["url:", "db:", "login:"])
        self.assertEqual(len(secret_asked), 1)
        self.assertEqual((config.url, config.db, config.login, config.api_key),
                         ("http://127.0.0.1:1", "dbx", "boss", "s3cret"))
        self.assertNotIn("s3cret", "".join(asked))

    def test_an_empty_answer_is_refused(self):
        for answers, secret in ((["", "d", "l"], "x"), (["u", "  ", "l"], "x"), (["u", "d", "l"], "   ")):
            with self.subTest(answers=answers, secret=secret):
                feed = iter(answers)
                with self.assertRaises(setup_instance.SetupError) as caught:
                    setup_instance.admin_config(env={}, interactive=True, ask=lambda text: next(feed),
                                                ask_secret=lambda text: secret)
                self.assertIn("is empty", str(caught.exception))

    def test_the_secret_comes_from_the_secret_prompt_never_from_the_plain_one(self):
        env = {k: v for k, v in self.FULL.items() if k != "ODOO_KPI_ADMIN_SECRET"}
        with self.assertRaises(AssertionError):
            setup_instance.admin_config(env=env, interactive=True, ask=self.no_prompt, ask_secret=self.no_prompt)


class CredentialSourceTests(NoTerminalCase):
    def setUp(self):
        super().setUp()
        self.admin = start_fake(self, login=ADMIN_LOGIN, key=ADMIN_SECRET, uid=ADMIN_UID)
        self.admin_scenario = Scenario(self.admin)
        self.qaqc = start_fake(self)
        Scenario(self.qaqc)

    def test_a_qaqc_environment_alone_is_not_an_administrator_credential(self):
        use_qaqc_env(self.qaqc.url)
        code, out, err = self.run_setup("init", "--apply")
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("ODOO_KPI_ADMIN_URL is not set", err)
        self.assertEqual(self.qaqc.requests, [])
        self.assertEqual(self.admin.requests, [])

    @POSIX_ONLY
    def test_the_qaqc_config_file_is_never_read(self):
        path = write_file(self.tmp, ini_text(self.qaqc.url), 0o600)
        os.environ["ODOO_KPI_CONFIG"] = path
        with mock.patch.object(odoo_rpc, "load_config", side_effect=AssertionError("QA/QC config read")), \
                mock.patch.object(odoo_rpc, "config_path", side_effect=AssertionError("config path asked")):
            code, out, err = self.run_setup("status")
        self.assertEqual(code, 2, err)
        self.assertEqual(self.qaqc.requests, [])

    def test_with_both_credentials_present_only_the_admin_server_is_called_with_the_admin_secret(self):
        use_qaqc_env(self.qaqc.url)
        use_admin_env(self.admin.url)
        code, out, err = self.run_setup("status")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.qaqc.requests, [])
        login = self.admin.calls("common", "login")[0]["params"]["args"]
        self.assertEqual(login, [FAKE_DB, ADMIN_LOGIN, ADMIN_SECRET])
        for request in self.admin.requests:
            self.assertNotIn(FAKE_KEY, request["raw"].decode("utf-8"))

    def test_each_missing_admin_variable_is_refused_before_any_request(self):
        use_admin_env(self.admin.url)
        for name in ADMIN_VARS:
            with self.subTest(missing=name):
                saved = os.environ.pop(name)
                code, out, err = self.run_setup("status")
                os.environ[name] = saved
                self.assertEqual((code, out), (2, ""), err)
                self.assertIn(name, err)
                self.assertEqual(self.admin.requests, [])

    def test_main_prompts_for_the_missing_values_when_there_is_a_terminal(self):
        use_admin_env(self.admin.url)
        del os.environ["ODOO_KPI_ADMIN_SECRET"]
        asked = []
        defaults = (None, None, lambda text: asked.append(text) or "unused", lambda text: ADMIN_SECRET)
        tty = mock.Mock()
        tty.isatty.return_value = True
        with mock.patch("sys.stdin", tty), mock.patch.object(setup_instance.admin_config, "__defaults__", defaults):
            code, out, err = self.run_setup("status")
        self.assertEqual(code, 0, err)
        self.assertEqual(asked, [])
        self.assertIn("group 'KPI (QA/QC)'", out)

    def test_an_interrupted_prompt_is_exit_130(self):
        def interrupted(text):
            raise EOFError()

        tty = mock.Mock()
        tty.isatty.return_value = True
        defaults = (None, None, interrupted, interrupted)
        with mock.patch("sys.stdin", tty), mock.patch.object(setup_instance.admin_config, "__defaults__", defaults):
            code, out, err = self.run_setup("status")
        self.assertEqual((code, out, err), (130, "", "interrupted\n"))

    def test_nothing_is_persisted_and_the_environment_is_not_changed(self):
        use_admin_env(self.admin.url)
        before_env = dict(os.environ)
        for argv in (["status"], ["init", "--apply"], ["add-user", "plain.user", "--apply"]):
            self.run_setup(*argv)
        self.assertEqual(dict(os.environ), before_env)
        for folder in (self.home, os.path.join(self.tmp, "xdg"), os.path.join(self.tmp, "appdata")):
            found = [os.path.join(root, f) for root, _, files in os.walk(folder) for f in files]
            self.assertEqual(found, [])
        self.assertEqual(sorted(os.listdir(self.tmp)), ["home"])


class InstanceGuardTests(NoTerminalCase):
    """AC9 for the admin tool: a non-local host needs the instance name typed by the human, and https."""

    URL = "https://my-instance.example.com"

    def setUp(self):
        super().setUp()
        use_admin_env(self.URL)

    def commands(self):
        return (["status"], ["init", "--apply"], ["add-user", "x", "--apply"], ["remove-user", "x", "--apply"],
                ["list-orphans"], ["remove-group", "--apply"])

    def test_a_non_local_host_without_the_confirmation_never_reaches_the_network(self):
        for argv in self.commands():
            # round 3: the confirmation is the WHOLE host name. "my-instance.example.com" was in this list while the
            # first label was the rule (it is now the right answer, see below); the first label itself takes its place
            for confirm in ([], ["--confirm-instance", ""], ["--confirm-instance", "other"],
                            ["--confirm-instance", "my-instance"], ["--confirm-instance", "MY-INSTANCE"],
                            ["--confirm-instance", "my-instance.example"],
                            ["--confirm-instance", "my-instance.example.com.attacker.example"],
                            ["--confirm-instance", "my-instance.example.com:443"]):
                with self.subTest(command=argv[0], confirm=confirm):
                    opener = ExplodingOpener()
                    code, out, err = self.run_setup(*argv, *confirm, client_factory=factory_with(opener))
                    self.assertEqual((code, out), (2, ""), err)
                    self.assertIn("--confirm-instance", err)
                    self.assertEqual(opener.calls, 0)

    def test_a_look_alike_host_never_receives_the_admin_secret_typed_with_the_label_or_the_legitimate_host(self):
        # round 3 (security r2): the administrator password goes to the URL of the environment; the first label (or the
        # legitimate host) must not unlock a look-alike host
        for url in ("https://my-instance.attacker.example", "https://my-instance.example.com.attacker.example"):
            use_admin_env(url)
            for argv in self.commands():
                for typed in ("my-instance", "my-instance.example.com"):
                    with self.subTest(url=url, command=argv[0], typed=typed):
                        opener = ExplodingOpener()
                        code, out, err = self.run_setup(*argv, "--confirm-instance", typed,
                                                        client_factory=factory_with(opener))
                        self.assertEqual((code, out), (2, ""), err)
                        self.assertIn("type the full host name", err)
                        self.assertNotIn(ADMIN_SECRET, out + err)
                        self.assertEqual(opener.calls, 0)

    def test_plain_http_to_a_non_local_host_is_refused_even_with_the_right_confirmation(self):
        use_admin_env("http://my-instance.example.com")
        for typed in ("my-instance", "my-instance.example.com"):
            with self.subTest(typed=typed):
                opener = ExplodingOpener()
                code, out, err = self.run_setup("status", "--confirm-instance", typed,
                                                client_factory=factory_with(opener))
                self.assertEqual(code, 2, err)
                self.assertIn("https", err)
                self.assertEqual(opener.calls, 0)

    def test_the_right_confirmation_lets_the_request_through(self):
        for typed in ("my-instance.example.com", "MY-INSTANCE.EXAMPLE.COM", " my-instance.example.com "):
            with self.subTest(typed=typed):
                opener = RecordingOpener(False)
                code, out, err = self.run_setup("status", "--confirm-instance", typed, client_factory=factory_with(opener))
                self.assertEqual(len(opener.requests), 1)
                self.assertEqual(code, 1)
                self.assertIn("login failed", err)

    def test_the_confirmation_is_passed_on_to_the_client(self):
        seen = []

        def factory(config, confirm_instance=None):
            seen.append(confirm_instance)
            return odoo_rpc.Client(config, confirm_instance=confirm_instance, opener=RecordingOpener(False))

        self.run_setup("status", "--confirm-instance", "my-instance.example.com", client_factory=factory)
        self.assertEqual(seen, ["my-instance.example.com"])

    def test_a_bad_admin_url_is_refused_by_the_guard_not_sent(self):
        for url in ("http://localhost.", "https://good.example.com/?token=x", "http://[::1", "ftp://x.example.com"):
            for typed in ("good", "good.example.com"):
                with self.subTest(url=url, typed=typed):
                    use_admin_env(url)
                    opener = ExplodingOpener()
                    code, out, err = self.run_setup("status", "--confirm-instance", typed,
                                                    client_factory=factory_with(opener))
                    self.assertEqual((code, out), (2, ""), err)
                    self.assertEqual(opener.calls, 0)


class ExitCodeAndSecretTests(NoTerminalCase):
    def setUp(self):
        super().setUp()
        self.fake = start_fake(self, login=ADMIN_LOGIN, key=ADMIN_SECRET, uid=ADMIN_UID)
        self.s = Scenario(self.fake)
        use_admin_env(self.fake.url)

    def test_server_errors_that_echo_the_secret_never_print_it(self):
        for mode in ("error_echo", "error_echo_top", "http_500", "http_403_plain", "cf_1010", "cf_other", "non_json",
                     "json_list", "cf_502", "login_false"):
            for argv in (["status"], ["init", "--apply"], ["add-user", "plain.user", "--apply"]):
                with self.subTest(mode=mode, command=argv[0]):
                    self.fake.mode = mode
                    code, out, err = self.run_setup(*argv)
                    self.assertEqual(code, 1, err)
                    self.assertEqual(out, "")
                    self.assertEqual(err.count("\n"), 1)
        self.assertEqual(self.fake.write_requests(), [])

    def test_a_role_check_that_fails_in_the_server_echoes_no_secret(self):
        from fake_odoo import DocumentsError
        self.s.world.has_group_override = {"base.group_system": DocumentsError("denied for key {key}")}
        code, out, err = self.run_setup("status")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("***", err)

    def test_an_unexpected_exception_prints_one_line_and_never_its_text(self):
        def exploding(config, confirm_instance=None):
            raise RuntimeError("detail with %s and %s" % (ADMIN_SECRET, config.api_key))

        code, out, err = self.run_setup("status", client_factory=exploding)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "error: unexpected RuntimeError\n")

    def test_ctrl_c_is_exit_130(self):
        def interrupted(config, confirm_instance=None):
            raise KeyboardInterrupt()

        self.assertEqual(self.run_setup("status", client_factory=interrupted), (130, "", "interrupted\n"))

    def test_an_unreachable_server_is_exit_1(self):
        from helpers import closed_port
        use_admin_env("http://127.0.0.1:%d" % closed_port())
        code, out, err = self.run_setup("status")
        self.assertEqual(code, 1)
        self.assertIn("cannot reach", err)

    def test_the_secret_is_in_no_output_of_a_whole_session(self):
        for argv in (["status"], ["init"], ["init", "--apply"], ["add-user", "plain.user"],
                     ["add-user", "plain.user", "--apply"], ["remove-user", "plain.user", "--apply"], ["list-orphans"],
                     ["remove-group"], ["remove-group", "--apply"], ["add-user", "nobody", "--apply"],
                     ["add-user", "portal.user", "--apply"]):
            self.run_setup(*argv)
        combined = "".join(o + e for o, e in self.streams)
        self.assertTrue(combined)
        self.assertNotIn(ADMIN_SECRET, combined)
        self.assertNotIn(ADMIN_SECRET[:12], combined)

    def test_the_secret_never_appears_in_the_admin_config_repr(self):
        config = setup_instance.admin_config(env={k: v for k, v in zip(ADMIN_VARS, (self.fake.url, "d", "l", ADMIN_SECRET))})
        self.assertNotIn(ADMIN_SECRET, repr(config) + str(config))


class ArgumentTests(NoTerminalCase):
    def setUp(self):
        super().setUp()
        self.fake = start_fake(self, login=ADMIN_LOGIN, key=ADMIN_SECRET, uid=ADMIN_UID)
        Scenario(self.fake)
        use_admin_env(self.fake.url)

    def parse_fails(self, *argv):
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as caught:
            setup_instance.main(list(argv), io.StringIO(), io.StringIO())
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.fake.requests, [])

    def test_unknown_commands_and_missing_logins_are_refused_by_the_parser(self):
        self.parse_fails()
        self.parse_fails("destroy")
        self.parse_fails("add-user")
        self.parse_fails("remove-user", "--apply")

    def test_abbreviated_options_are_refused_by_the_parser_before_any_request(self):
        # round 2 (security r1): `--a` must not turn a dry run into a write (allow_abbrev=False), whatever the command
        cases = []
        for command in (["init"], ["add-user", "x"], ["remove-user", "x"], ["remove-group"]):
            cases += [command + [flag] for flag in ("--a", "--ap", "--app", "--appl")]
        for command in (["status"], ["init"], ["add-user", "x"], ["remove-user", "x"], ["list-orphans"], ["remove-group"]):
            cases += [command + ["--confirm-i", "i"], command + ["--confi", "i"], command + ["--c", "i"]]
        for argv in cases:
            with self.subTest(argv=argv):
                self.parse_fails(*argv)

    def test_the_main_parser_refuses_abbreviations_too(self):
        for flag in ("--he", "--hel"):
            with self.subTest(flag=flag):
                self.parse_fails(flag)

    def test_the_full_spellings_still_work(self):
        parser = setup_instance.build_parser()
        for argv in (["init"], ["add-user", "x"], ["remove-user", "x"], ["remove-group"]):
            with self.subTest(command=argv[0]):
                self.assertTrue(parser.parse_args(argv + ["--apply", "--confirm-instance", "i"]).apply)

    def test_read_only_commands_have_no_apply_flag(self):
        for command in ("status", "list-orphans"):
            with self.subTest(command=command):
                self.parse_fails(command, "--apply")

    def test_the_apply_flag_is_off_by_default_everywhere_it_exists(self):
        parser = setup_instance.build_parser()
        for argv in (["init"], ["add-user", "x"], ["remove-user", "x"], ["remove-group"]):
            with self.subTest(command=argv[0]):
                self.assertFalse(parser.parse_args(argv).apply)

    def test_every_command_offers_the_confirmation_option(self):
        parser = setup_instance.build_parser()
        for argv in (["status"], ["init"], ["add-user", "x"], ["remove-user", "x"], ["list-orphans"], ["remove-group"]):
            with self.subTest(command=argv[0]):
                self.assertEqual(parser.parse_args(argv + ["--confirm-instance", "i"]).confirm_instance, "i")

    def test_there_is_no_option_to_give_the_secret_on_the_command_line(self):
        for flag in ("--secret", "--password", "--api-key", "--login", "--url"):
            with self.subTest(flag=flag):
                self.parse_fails("status", flag, "x")

    def test_the_commands_are_exactly_the_documented_ones(self):
        self.assertEqual(sorted(setup_instance.COMMANDS),
                         ["add-user", "init", "list-orphans", "remove-group", "remove-user", "status"])


if __name__ == "__main__":
    unittest.main()
