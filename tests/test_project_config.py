"""The optional `project` of the user's config: `Config.project`, `load_config` precedence, `project_status`, `check-config`.

Plan kpi-multi-project step 3 (AC4, D7, D9, D11, D12). The project is the RAW optional text of `[odoo] project` /
`ODOO_KPI_PROJECT`: it is never validated by `load_config` (a bad optional key must not break `ping`, `check-config` or
`reconcile`), and it is printed only when it is the name of an installed catalog (membership first). The folder of the
catalogs is redirected with the D11 seam (`kpi_catalogs.CATALOG_DIR`), so every command here runs `main(argv)` IN PROCESS.
`init-config` has its own tests in test_init_config.py.
"""
import ast
import builtins
import configparser
import io
import os
import unittest
from unittest import mock

from fake_odoo import FAKE_DB, FAKE_KEY, FAKE_LOGIN, FakeOdoo
from helpers import IsolatedTestCase, ini_text, install_catalogs, missing_catalogs, odoo_rpc, write_file
from test_cli import CliTestCase
from test_config import USER_SID, _clean_sddl

import kpi_catalogs  # noqa: E402  (found through the sys.path entry that `helpers` adds)

POSIX_ONLY = unittest.skipIf(os.name == "nt", "POSIX mode bits only; Windows uses the icacls guard")
URL = "https://my-instance.example.com"
SECRET_WORD = "SECRETWORD"            # secret-allow: a marker for whatever a user typed as a project, not a secret
ENV = {"ODOO_KPI_URL": "https://env.example.com", "ODOO_KPI_DB": "env_db", "ODOO_KPI_LOGIN": "env.login",
       "ODOO_KPI_API_KEY": "env-key"}  # secret-allow: fixture values, not real
HOSTILE = {
    "control characters": "\x1b[2J" + SECRET_WORD + "\x07",
    "a NUL inside": "a\x00" + SECRET_WORD,
    "10 KB": SECRET_WORD + "a" * 10240,
    "parent folder": "../" + SECRET_WORD,
    "absolute path": "/etc/" + SECRET_WORD,
    "newline inside": "alpha\n" + SECRET_WORD,
    "interpolation": "%(" + SECRET_WORD + ")s",
    "upper case": SECRET_WORD,
    "non ASCII": "K" + SECRET_WORD,
}


def ini_with_project(project_line, url=URL):
    return ini_text(url) + project_line + "\n"


def load_nt(path, env):
    """load_config through the Windows branch with a clean injected SDDL (platform independent)."""
    return odoo_rpc.load_config(path=path, env=env, os_name="nt", sddl=_clean_sddl, user_sid=USER_SID)


class ConfigSlotTests(unittest.TestCase):
    def test_project_is_the_last_optional_slot_and_defaults_to_none(self):
        config = odoo_rpc.Config(URL, "db", "login", FAKE_KEY)
        self.assertIsNone(config.project)
        self.assertEqual(config.source, "file")
        self.assertEqual(odoo_rpc.Config.__slots__[-1], "project")
        self.assertEqual(odoo_rpc.Config.__slots__[:5], ("url", "db", "login", "api_key", "source"))

    def test_the_five_positional_arguments_keep_their_meaning(self):
        config = odoo_rpc.Config(URL, "db", "login", FAKE_KEY, "env")
        self.assertEqual((config.url, config.db, config.login, config.api_key, config.source, config.project),
                         (URL, "db", "login", FAKE_KEY, "env", None))

    def test_project_is_a_last_positional_and_a_keyword(self):
        self.assertEqual(odoo_rpc.Config(URL, "db", "login", FAKE_KEY, "file", "beta").project, "beta")
        self.assertEqual(odoo_rpc.Config(URL, "db", "login", FAKE_KEY, project="beta").project, "beta")
        self.assertEqual(odoo_rpc.Config(URL, "db", "login", FAKE_KEY, source="env", project="beta").source, "env")

    def test_the_project_is_in_no_text_form_of_the_config(self):
        config = odoo_rpc.Config(URL, "db", "login", FAKE_KEY, "file", SECRET_WORD)
        for text in (repr(config), str(config), "%s" % config, "%r" % config, "{}".format(config), repr([config]),
                     repr({"c": config}), f"{config}"):
            self.assertNotIn(SECRET_WORD, text)
            self.assertNotIn("project", text)
            self.assertNotIn(FAKE_KEY, text)
        self.assertEqual(repr(config), repr(odoo_rpc.Config(URL, "db", "login", FAKE_KEY)))

    def test_the_project_variable_is_not_one_of_the_four_credentials(self):
        self.assertEqual(odoo_rpc.PROJECT_ENV, "ODOO_KPI_PROJECT")
        self.assertNotIn(odoo_rpc.PROJECT_ENV, odoo_rpc.ENV_NAMES.values())
        self.assertEqual(set(odoo_rpc.ENV_NAMES), {"url", "db", "login", "api_key"})
        self.assertEqual(len(odoo_rpc.ENV_NAMES), 4)


class EnvCredentialsModeTests(IsolatedTestCase):
    """All four credentials from the environment: the file is NOT opened and the project comes from ODOO_KPI_PROJECT only."""

    def assertFileUntouched(self, run):
        """`run()` must not look at the config file at all: no existence test, no privacy check, no read, no open."""
        with mock.patch.object(odoo_rpc, "check_private_file", side_effect=AssertionError("privacy check ran")) as check, \
                mock.patch.object(odoo_rpc.os.path, "isfile", side_effect=AssertionError("isfile ran")) as isfile, \
                mock.patch.object(configparser.ConfigParser, "read", side_effect=AssertionError("read ran")) as read, \
                mock.patch.object(builtins, "open", side_effect=AssertionError("open ran")) as opened:
            result = run()
        for spy in (check, isfile, read, opened):
            spy.assert_not_called()
        return result

    def test_the_project_comes_from_the_environment_and_the_file_is_not_opened(self):
        missing = os.path.join(self.tmp, "does-not-exist.ini")
        config = self.assertFileUntouched(
            lambda: odoo_rpc.load_config(path=missing, env=dict(ENV, ODOO_KPI_PROJECT="beta")))
        self.assertEqual((config.source, config.project), ("env", "beta"))

    def test_an_unset_project_is_none_and_the_file_is_still_not_opened(self):
        missing = os.path.join(self.tmp, "does-not-exist.ini")
        config = self.assertFileUntouched(lambda: odoo_rpc.load_config(path=missing, env=dict(ENV)))
        self.assertEqual((config.source, config.project), ("env", None))

    def test_an_empty_or_blank_project_variable_is_none(self):
        for value in ("", " ", "\t", "  \t "):
            with self.subTest(value=repr(value)):
                self.assertIsNone(odoo_rpc.load_config(path="/nowhere", env=dict(ENV, ODOO_KPI_PROJECT=value)).project)

    def test_the_project_of_an_existing_file_is_not_read_in_env_mode(self):
        path = write_file(self.tmp, ini_with_project("project = alpha"), 0o600)
        config = self.assertFileUntouched(lambda: odoo_rpc.load_config(path=path, env=dict(ENV)))
        self.assertIsNone(config.project)

    def test_a_world_readable_file_is_not_refused_in_env_mode_because_it_is_not_looked_at(self):
        path = write_file(self.tmp, ini_with_project("project = alpha"), 0o666)
        config = odoo_rpc.load_config(path=path, env=dict(ENV, ODOO_KPI_PROJECT="beta"))
        self.assertEqual((config.source, config.project), ("env", "beta"))

    def test_the_environment_project_is_stripped(self):
        self.assertEqual(odoo_rpc.load_config(path="/x", env=dict(ENV, ODOO_KPI_PROJECT="  beta \t")).project, "beta")

    def test_the_process_environment_is_the_default_source_of_the_project(self):
        os.environ.update(ENV)
        os.environ["ODOO_KPI_PROJECT"] = "beta"
        self.assertEqual(odoo_rpc.load_config(path="/nowhere").project, "beta")

    def test_the_project_variable_alone_does_not_make_env_mode(self):
        missing = os.path.join(self.tmp, "does-not-exist.ini")
        with self.assertRaises(odoo_rpc.ConfigError) as caught:
            odoo_rpc.load_config(path=missing, env={"ODOO_KPI_PROJECT": "beta"})
        self.assertIn(missing, str(caught.exception))

    def test_three_credentials_plus_the_project_variable_still_read_the_file(self):
        path = write_file(self.tmp, ini_with_project("project = alpha", url="https://file.example.com"), 0o600)
        env = {name: value for name, value in ENV.items() if name != "ODOO_KPI_LOGIN"}
        env["ODOO_KPI_PROJECT"] = "beta"
        config = load_nt(path, env)
        self.assertEqual((config.source, config.url, config.project), ("file", "https://file.example.com", "beta"))


class FileModeTests(IsolatedTestCase):
    def load(self, project_line, env=None):
        path = write_file(self.tmp, ini_with_project(project_line) if project_line is not None else ini_text(URL))
        return load_nt(path, env or {})

    def test_the_project_of_the_file_is_used(self):
        config = self.load("project = alpha")
        self.assertEqual((config.source, config.project), ("file", "alpha"))
        self.assertEqual((config.url, config.db, config.login, config.api_key), (URL, FAKE_DB, FAKE_LOGIN, FAKE_KEY))

    def test_the_project_of_the_file_is_stripped(self):
        self.assertEqual(self.load("project =   alpha   ").project, "alpha")

    def test_no_project_key_empty_or_blank_value_is_none(self):
        self.assertIsNone(self.load(None).project)
        self.assertIsNone(self.load("project =").project)
        self.assertIsNone(self.load("project =    ").project)

    def test_the_environment_project_overrides_the_file(self):
        config = self.load("project = alpha", env={"ODOO_KPI_PROJECT": "beta"})
        self.assertEqual(config.project, "beta")

    def test_a_blank_environment_project_leaves_the_file_value(self):
        for value in ("", "   "):
            with self.subTest(value=repr(value)):
                self.assertEqual(self.load("project = alpha", env={"ODOO_KPI_PROJECT": value}).project, "alpha")

    def test_the_environment_project_is_used_when_the_file_has_none(self):
        self.assertEqual(self.load(None, env={"ODOO_KPI_PROJECT": "beta"}).project, "beta")
        self.assertEqual(self.load("project =", env={"ODOO_KPI_PROJECT": "beta"}).project, "beta")

    def test_the_key_of_another_section_is_ignored(self):
        path = write_file(self.tmp, ini_text(URL) + "[other]\nproject = alpha\n")
        self.assertIsNone(load_nt(path, {}).project)

    def test_the_project_key_does_not_count_as_a_missing_credential(self):
        self.assertEqual(self.load("project = alpha").login, FAKE_LOGIN)
        broken = write_file(self.tmp, "[odoo]\nproject = alpha\n", name="broken.ini")
        with self.assertRaises(odoo_rpc.ConfigError) as caught:
            load_nt(broken, {})
        self.assertIn("misses", str(caught.exception))
        self.assertNotIn("project", str(caught.exception))

    @POSIX_ONLY
    def test_the_default_privacy_check_still_runs_before_the_project_is_read(self):
        path = write_file(self.tmp, ini_with_project("project = alpha"), 0o644)
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc.load_config(path=path, env={"ODOO_KPI_PROJECT": "beta"})

    def test_a_hostile_file_value_is_stored_raw_and_never_raises(self):
        for label, value in HOSTILE.items():
            if "\n" in value:
                continue                                    # a newline cannot sit inside one INI value (see below)
            with self.subTest(case=label):
                config = self.load("project = " + value)
                self.assertEqual(config.project, value.strip())
                self.assertEqual(config.url, URL)

    def test_a_hostile_environment_value_is_stored_raw_and_never_raises(self):
        for label, value in HOSTILE.items():
            for mode in ("env credentials", "file"):
                with self.subTest(case=label, mode=mode):
                    if mode == "env credentials":
                        config = odoo_rpc.load_config(path="/x", env=dict(ENV, ODOO_KPI_PROJECT=value))
                    else:
                        config = self.load(None, env={"ODOO_KPI_PROJECT": value})
                    self.assertEqual(config.project, value.strip())

    def test_a_trailing_newline_of_the_environment_value_is_stripped_like_any_blank(self):
        # documented: the text is stripped before it is stored, so "alpha\n" is the project "alpha"
        self.assertEqual(odoo_rpc.load_config(path="/x", env=dict(ENV, ODOO_KPI_PROJECT="alpha\n")).project, "alpha")

    def test_load_config_never_looks_at_the_catalogs(self):
        # validated at USE time (membership), never here: an unreadable or exploding listing changes nothing
        with mock.patch.object(kpi_catalogs, "list_projects", side_effect=AssertionError("listing used")) as listing, \
                mock.patch.object(kpi_catalogs, "catalog_path", side_effect=AssertionError("path used")) as path_of:
            self.assertEqual(self.load("project = " + SECRET_WORD).project, SECRET_WORD)
            self.assertEqual(odoo_rpc.load_config(path="/x", env=dict(ENV, ODOO_KPI_PROJECT=SECRET_WORD)).project,
                             SECRET_WORD)
        listing.assert_not_called()
        path_of.assert_not_called()


class ProjectStatusTests(IsolatedTestCase):
    def test_not_set_lists_the_installed_names_sorted(self):
        install_catalogs(self, "zeta", "alpha", "mid")
        self.assertEqual(odoo_rpc.project_status(None), "project: not set (installed: alpha, mid, zeta)")

    def test_a_member_is_printed(self):
        install_catalogs(self, "alpha", "beta")
        self.assertEqual(odoo_rpc.project_status("alpha"), "project: alpha")
        self.assertEqual(odoo_rpc.project_status("beta"), "project: beta")

    def test_one_catalog(self):
        install_catalogs(self, "dksk")
        self.assertEqual(odoo_rpc.project_status(None), "project: not set (installed: dksk)")
        self.assertEqual(odoo_rpc.project_status("dksk"), "project: dksk")
        self.assertEqual(odoo_rpc.project_status("other"), "project: invalid (installed: dksk)")

    def test_a_non_member_is_invalid_and_the_typed_text_is_never_echoed(self):
        install_catalogs(self, "alpha", "beta")
        values = list(HOSTILE.values()) + ["gamma", "ALPHA", "Alpha", " alpha", "alpha ", "alpha\n", "alpha.json",
                                           "", "alpha,beta", "al", "*"]
        for value in values:
            with self.subTest(value=repr(value)[:40]):
                line = odoo_rpc.project_status(value)
                self.assertEqual(line, "project: invalid (installed: alpha, beta)")
                self.assertNotIn(SECRET_WORD, line)
        for text in ("alpha", "beta"):
            self.assertEqual(odoo_rpc.project_status(text), "project: " + text)

    def test_membership_is_exact_not_stripped_or_case_folded_here(self):
        install_catalogs(self, "alpha")
        for value in ("alpha ", "ALPHA", "alpha\n", "\talpha"):
            self.assertEqual(odoo_rpc.project_status(value), "project: invalid (installed: alpha)")

    def test_an_empty_folder_says_none(self):
        install_catalogs(self)
        self.assertEqual(odoo_rpc.project_status(None), "project: not set (installed: none)")
        self.assertEqual(odoo_rpc.project_status(SECRET_WORD), "project: invalid (installed: none)")

    def test_an_unreadable_folder_is_a_static_line_whatever_the_project(self):
        missing_catalogs(self)
        for project in (None, "alpha", SECRET_WORD, ""):
            with self.subTest(project=project):
                self.assertEqual(odoo_rpc.project_status(project), "catalogs: unreadable")

    def test_a_catalogs_path_that_is_a_file_is_unreadable_not_an_exception(self):
        path = os.path.join(self.tmp, "afile")
        with open(path, "w") as handle:
            handle.write("x")
        with mock.patch.object(kpi_catalogs, "CATALOG_DIR", path):
            self.assertEqual(odoo_rpc.project_status("alpha"), "catalogs: unreadable")

    def test_the_names_come_from_the_valid_files_of_the_folder_only(self):
        folder = install_catalogs(self, "alpha", "beta", "DKSK", "bad name", "dashboard1", "con", "x.aliases")
        for name in ("notes.txt", "alpha.json.bak", "gamma"):
            with open(os.path.join(folder, name), "w") as handle:
                handle.write("{}")
        os.makedirs(os.path.join(folder, "adir.json"))
        try:
            os.symlink(os.path.join(folder, "alpha.json"), os.path.join(folder, "linked.json"))
        except (OSError, NotImplementedError, AttributeError):
            pass                                              # this account cannot link: the other cases still hold
        self.assertEqual(odoo_rpc.project_status(None), "project: not set (installed: alpha, beta)")
        for refused in ("DKSK", "bad name", "dashboard1", "con", "x.aliases", "gamma", "adir", "linked", "notes"):
            self.assertEqual(odoo_rpc.project_status(refused), "project: invalid (installed: alpha, beta)")

    def test_it_follows_the_folder_at_call_time(self):
        install_catalogs(self, "alpha")
        self.assertEqual(odoo_rpc.project_status(None), "project: not set (installed: alpha)")
        install_catalogs(self, "beta", "gamma")
        self.assertEqual(odoo_rpc.project_status(None), "project: not set (installed: beta, gamma)")

    def test_it_never_raises_for_a_catalog_error(self):
        with mock.patch.object(kpi_catalogs, "list_projects", side_effect=kpi_catalogs.CatalogError("x")):
            self.assertEqual(odoo_rpc.project_status("alpha"), "catalogs: unreadable")


class CheckConfigProjectTests(CliTestCase):
    """`check-config` end to end, in process, with two installed catalogs unless a test says otherwise."""

    def setUp(self):
        super().setUp()
        install_catalogs(self, "alpha", "beta")

    def file_config(self, project_line, mode=0o600):
        return write_file(self.tmp, ini_with_project(project_line) if project_line else ini_text(URL), mode)

    def check(self, *argv, expect_out):
        code, out, err = self.run_main("check-config", *argv)
        self.assertEqual((code, out, err), (0, expect_out, ""))
        for text in (out, err):
            self.assertNotIn(SECRET_WORD, text)
            self.assertNotIn(FAKE_KEY, text)
        return out

    @POSIX_ONLY
    def test_a_member_in_the_file(self):
        path = self.file_config("project = beta")
        self.check("--config", path, expect_out="config: file, private (permissions ok)\nproject: beta\n")

    @POSIX_ONLY
    def test_not_set_lists_the_installed_names(self):
        path = self.file_config(None)
        self.check("--config", path, expect_out="config: file, private (permissions ok)\n"
                                                "project: not set (installed: alpha, beta)\n")

    @POSIX_ONLY
    def test_an_unknown_project_is_invalid_with_rc_0_and_no_echo(self):
        path = self.file_config("project = " + SECRET_WORD.lower())
        self.check("--config", path, expect_out="config: file, private (permissions ok)\n"
                                                "project: invalid (installed: alpha, beta)\n")

    @POSIX_ONLY
    def test_a_hostile_value_in_the_file_is_one_static_line(self):
        for label, value in HOSTILE.items():
            if "\n" in value:
                continue
            with self.subTest(case=label):
                path = self.file_config("project = " + value)
                out = self.check("--config", path, expect_out="config: file, private (permissions ok)\n"
                                                              "project: invalid (installed: alpha, beta)\n")
                self.assertEqual(len(out), len("config: file, private (permissions ok)\n"
                                               "project: invalid (installed: alpha, beta)\n"))
                self.assertFalse(any(ord(ch) < 32 for ch in out.replace("\n", "")))

    def test_a_hostile_environment_value_is_one_static_line(self):
        self.set_env_config(URL)
        for label, value in HOSTILE.items():
            if "\x00" in value:
                continue                                    # a real process environment cannot hold a NUL (os refuses)
            with self.subTest(case=label):
                os.environ["ODOO_KPI_PROJECT"] = value
                self.check(expect_out="config: env\nproject: invalid (installed: alpha, beta)\n")

    def test_env_mode_reads_the_project_from_the_environment_only(self):
        self.set_env_config(URL)
        self.check(expect_out="config: env\nproject: not set (installed: alpha, beta)\n")
        os.environ["ODOO_KPI_PROJECT"] = "beta"
        self.check(expect_out="config: env\nproject: beta\n")

    def test_env_mode_does_not_open_the_file_named_by_config(self):
        self.set_env_config(URL)
        os.environ["ODOO_KPI_PROJECT"] = "alpha"
        missing = os.path.join(self.tmp, "does-not-exist.ini")
        self.check("--config", missing, expect_out="config: env\nproject: alpha\n")

    @POSIX_ONLY
    def test_env_mode_ignores_the_project_of_a_file_even_one_that_would_be_refused(self):
        self.set_env_config(URL)
        path = self.file_config("project = beta", mode=0o666)
        self.check("--config", path, expect_out="config: env\nproject: not set (installed: alpha, beta)\n")

    @POSIX_ONLY
    def test_file_mode_the_environment_project_overrides_the_file(self):
        path = self.file_config("project = alpha")
        os.environ["ODOO_KPI_PROJECT"] = "beta"
        self.check("--config", path, expect_out="config: file, private (permissions ok)\nproject: beta\n")

    @POSIX_ONLY
    def test_file_mode_a_bad_environment_value_overrides_a_good_file_value(self):
        path = self.file_config("project = alpha")
        os.environ["ODOO_KPI_PROJECT"] = SECRET_WORD
        self.check("--config", path, expect_out="config: file, private (permissions ok)\n"
                                                "project: invalid (installed: alpha, beta)\n")

    @POSIX_ONLY
    def test_an_unreadable_catalogs_folder_is_a_static_line_never_rc_2(self):
        missing_catalogs(self)
        path = self.file_config("project = " + SECRET_WORD)
        self.check("--config", path, expect_out="config: file, private (permissions ok)\ncatalogs: unreadable\n")
        self.set_env_config(URL)
        self.check(expect_out="config: env\ncatalogs: unreadable\n")

    def test_an_empty_catalogs_folder_says_none(self):
        install_catalogs(self)
        self.set_env_config(URL)
        self.check(expect_out="config: env\nproject: not set (installed: none)\n")

    def test_one_catalog(self):
        install_catalogs(self, "dksk")
        self.set_env_config(URL)
        self.check(expect_out="config: env\nproject: not set (installed: dksk)\n")
        os.environ["ODOO_KPI_PROJECT"] = "dksk"
        self.check(expect_out="config: env\nproject: dksk\n")

    def test_a_bad_config_still_fails_before_any_project_line(self):
        os.environ.update({"ODOO_KPI_URL": "https://good#x", "ODOO_KPI_DB": "d", "ODOO_KPI_LOGIN": "l",
                           "ODOO_KPI_API_KEY": "k", "ODOO_KPI_PROJECT": "alpha"})   # secret-allow: fixture
        code, out, err = self.run_main("check-config")
        self.assertEqual((code, out), (2, ""))
        self.assertNotIn("project", out)


class PingIgnoresTheProjectTests(CliTestCase):
    """A bad `project` never breaks `ping`: its output is the one of a config without a project."""

    def setUp(self):
        super().setUp()
        self.fake = FakeOdoo()
        self.fake.start()
        self.addCleanup(self.fake.stop)

    def ping(self, project_line, env=None):
        path = write_file(self.tmp, ini_with_project(project_line, url=self.fake.url) if project_line
                          else ini_text(self.fake.url))
        with mock.patch.dict(os.environ, env or {}):
            return self.run_main("ping", "--config", path)

    @POSIX_ONLY
    def test_ping_output_is_the_same_with_a_hostile_project_in_the_file(self):
        install_catalogs(self, "alpha", "beta")
        reference = self.ping(None)
        self.assertEqual(reference[0], 0, reference)
        for label, value in HOSTILE.items():
            if "\n" in value:
                continue
            with self.subTest(case=label):
                got = self.ping("project = " + value)
                self.assertEqual(got, reference)
                self.assertNotIn(SECRET_WORD, got[1] + got[2])

    @POSIX_ONLY
    def test_ping_output_is_the_same_with_a_bad_environment_project_and_an_unreadable_catalogs_folder(self):
        reference = self.ping(None)
        missing_catalogs(self)
        got = self.ping("project = " + SECRET_WORD, env={"ODOO_KPI_PROJECT": SECRET_WORD})
        self.assertEqual(got, reference)

    @POSIX_ONLY
    def test_ping_does_not_look_at_the_catalogs_at_all(self):
        with mock.patch.object(kpi_catalogs, "list_projects", side_effect=AssertionError("listing used")) as listing:
            code, out, err = self.ping("project = " + SECRET_WORD)
        self.assertEqual(code, 0, err)
        listing.assert_not_called()
        self.assertNotIn("project", out)

    def test_ping_in_env_mode_with_a_hostile_project(self):
        self.set_env_config(self.fake.url)
        os.environ["ODOO_KPI_PROJECT"] = HOSTILE["control characters"]
        code, out, err = self.run_main("ping")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[:1], ["instance: 127.0.0.1"])
        self.assertNotIn(SECRET_WORD, out + err)


class StaticImportTests(unittest.TestCase):
    def test_odoo_rpc_uses_the_module_not_a_copy_of_its_constant(self):
        # D11 rule (a): `from kpi_catalogs import CATALOG_DIR` would bind a copy that a patch cannot reach
        with open(odoo_rpc.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual((node.module or "").split(".")[0], "kpi_catalogs", "from kpi_catalogs import ...")
        self.assertTrue(any(isinstance(node, ast.Import) and any(a.name == "kpi_catalogs" for a in node.names)
                            for node in ast.walk(tree)))


if __name__ == "__main__":
    unittest.main()
