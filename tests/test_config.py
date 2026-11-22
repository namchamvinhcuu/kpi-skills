"""load_config / config_path / check_private_file (POSIX part) of scripts/odoo_rpc.py."""
import os
import types
import unittest

from fake_odoo import FAKE_DB, FAKE_KEY, FAKE_LOGIN
from helpers import IsolatedTestCase, ini_text, odoo_rpc, write_bytes, write_file

URL = "https://my-instance.example.com"


USER_SID = "S-1-5-21-1-2-3-1001"


def _clean_sddl(path):
    """Stands in for the SDDL reader: a clean DACL, so the parsing tests do not depend on the platform of the runner."""
    return "D:PAI(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;%s)" % USER_SID


def load_parsing(path, **extra):
    """load_config through the Windows branch with a clean injected SDDL (platform independent)."""
    return odoo_rpc.load_config(path=path, env={}, os_name="nt", sddl=_clean_sddl, user_sid=USER_SID, **extra)


class FilePermissionTests(IsolatedTestCase):
    @unittest.skipIf(os.name == "nt", "POSIX mode bits only; Windows uses the icacls guard (test_windows_guard.py)")
    def test_mode_0600_is_accepted(self):
        path = write_file(self.tmp, ini_text(URL), 0o600)
        config = odoo_rpc.load_config(path=path, env={})
        self.assertEqual((config.url, config.db, config.login, config.api_key), (URL, FAKE_DB, FAKE_LOGIN, FAKE_KEY))

    @unittest.skipIf(os.name == "nt", "POSIX mode bits only; Windows uses the icacls guard (test_windows_guard.py)")
    def test_mode_0400_is_accepted(self):
        path = write_file(self.tmp, ini_text(URL), 0o400)
        self.assertEqual(odoo_rpc.load_config(path=path, env={}).db, FAKE_DB)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits only; Windows uses the icacls guard (test_windows_guard.py)")
    def test_group_or_other_accessible_files_are_refused(self):
        for mode in (0o644, 0o660, 0o640, 0o604, 0o602, 0o666, 0o777):
            with self.subTest(mode=oct(mode)):
                path = write_file(self.tmp, ini_text(URL), mode, name="c%o.ini" % mode)
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    odoo_rpc.load_config(path=path, env={})
                self.assertIn("chmod 600", str(caught.exception))
                self.assertNotIn(FAKE_KEY, str(caught.exception))

    @unittest.skipIf(os.name == "nt", "POSIX mode bits only; Windows uses the icacls guard (test_windows_guard.py)")
    def test_refused_file_is_not_parsed(self):
        # A world-readable file with invalid INI must give the permission error, not the parse error.
        path = write_file(self.tmp, "this is not ini", 0o644)
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc.load_config(path=path, env={})

    def test_mode_bits_with_an_injected_stat(self):
        def stat_of(mode):
            return lambda path: types.SimpleNamespace(st_mode=0o100000 | mode, st_uid=1000)

        for mode in (0o600, 0o400):
            with self.subTest(mode=oct(mode)):
                self.assertIsNone(odoo_rpc.check_private_file("c", os_name="posix", stat_fn=stat_of(mode),
                                                              getuid=lambda: 1000))
        for mode in (0o644, 0o660, 0o640, 0o604, 0o602, 0o666):
            with self.subTest(mode=oct(mode)):
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc.check_private_file("c", os_name="posix", stat_fn=stat_of(mode), getuid=lambda: 1000)

    def test_file_owned_by_someone_else_is_refused(self):
        info = types.SimpleNamespace(st_mode=0o100600, st_uid=1001)
        with self.assertRaises(odoo_rpc.GuardError) as caught:
            odoo_rpc.check_private_file("c", os_name="posix", stat_fn=lambda p: info, getuid=lambda: 1000)
        self.assertIn("not owned", str(caught.exception))


class FileContentTests(IsolatedTestCase):
    def test_missing_file_names_the_path(self):
        path = os.path.join(self.tmp, "nope", "config.ini")
        with self.assertRaises(odoo_rpc.ConfigError) as caught:
            odoo_rpc.load_config(path=path, env={})
        self.assertIn(path, str(caught.exception))
        self.assertIn("ODOO_KPI_URL", str(caught.exception))

    def test_default_path_is_used_when_no_path_is_given(self):
        env = {"ODOO_KPI_CONFIG": os.path.join(self.tmp, "absent.ini")}
        with self.assertRaises(odoo_rpc.ConfigError) as caught:
            odoo_rpc.load_config(env=env)
        self.assertIn(env["ODOO_KPI_CONFIG"], str(caught.exception))

    def test_missing_odoo_section(self):
        path = write_file(self.tmp, "[other]\nurl = %s\n" % URL)
        with self.assertRaises(odoo_rpc.ConfigError) as caught:
            load_parsing(path)
        self.assertIn("[odoo]", str(caught.exception))

    def test_a_missing_key_is_listed_by_name(self):
        for dropped in ("url", "db", "login", "api_key"):
            with self.subTest(missing=dropped):
                lines = [line for line in ini_text(URL).splitlines() if not line.startswith(dropped + " ")]
                path = write_file(self.tmp, "\n".join(lines) + "\n", name="m-%s.ini" % dropped)
                with self.assertRaises(odoo_rpc.ConfigError) as caught:
                    load_parsing(path)
                self.assertIn(dropped, str(caught.exception))
                self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_an_empty_value_counts_as_missing_and_all_missing_keys_are_listed(self):
        path = write_file(self.tmp, "[odoo]\nurl = %s\ndb =\nlogin =   \napi_key =\n" % URL)
        with self.assertRaises(odoo_rpc.ConfigError) as caught:
            load_parsing(path)
        message = str(caught.exception)
        for key in ("db", "login", "api_key"):
            self.assertIn(key, message)

    def test_invalid_ini_never_echoes_the_file_content(self):
        path = write_file(self.tmp, "api_key = %s\nno section header\n" % FAKE_KEY)
        with self.assertRaises(odoo_rpc.ConfigError) as caught:
            load_parsing(path)
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_percent_signs_in_values_are_kept_literally(self):
        path = write_file(self.tmp, ini_text(URL, key="ab%cd%%ef"))
        self.assertEqual(load_parsing(path).api_key, "ab%cd%%ef")

    def test_values_are_stripped(self):
        path = write_file(self.tmp, "[odoo]\nurl =   %s  \ndb =  d \nlogin =  l \napi_key =  k \n" % URL)
        config = load_parsing(path)
        self.assertEqual((config.url, config.db, config.login, config.api_key), (URL, "d", "l", "k"))

    def test_a_utf8_bom_is_ignored(self):
        path = write_bytes(self.tmp, b"\xef\xbb\xbf" + ini_text(URL).encode("utf-8"))
        config = load_parsing(path)
        self.assertEqual((config.url, config.db, config.login, config.api_key), (URL, FAKE_DB, FAKE_LOGIN, FAKE_KEY))

    def test_non_ascii_values_are_read_as_utf8(self):
        path = write_bytes(self.tmp, ini_text(URL, login="Nguyễn.Văn").encode("utf-8"))
        self.assertEqual(load_parsing(path).login, "Nguyễn.Văn")

    def test_invalid_utf8_gives_config_error_without_echoing_the_content(self):
        content = b"[odoo]\napi_key = " + FAKE_KEY.encode() + b"\n\xff\xfe\xfa broken\n"  # secret-allow: fixture
        path = write_bytes(self.tmp, content)
        with self.assertRaises(odoo_rpc.ConfigError) as caught:
            load_parsing(path)
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertIn("UTF-8", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_a_file_config_reports_source_file(self):
        self.assertEqual(load_parsing(write_file(self.tmp, ini_text(URL))).source, "file")


class UrlValidationTests(IsolatedTestCase):
    def load_url(self, url):
        return load_parsing(write_file(self.tmp, ini_text(url), name="u.ini"))

    def test_url_without_scheme_is_refused(self):
        for url in ("my-instance.example.com", "//my-instance.example.com", "my-instance.example.com:8069"):
            with self.subTest(url=url):
                with self.assertRaises(odoo_rpc.ConfigError):
                    self.load_url(url)

    def test_other_schemes_and_empty_host_are_refused(self):
        for url in ("ftp://x.example.com", "file:///etc/passwd", "https://", "http:///path"):
            with self.subTest(url=url):
                with self.assertRaises(odoo_rpc.ConfigError):
                    self.load_url(url)

    def test_url_with_user_and_password_is_refused_and_not_echoed(self):
        for url in ("https://alice:hunter2pw@my-instance.example.com", "https://alice@my-instance.example.com",
                    "https://:hunter2pw@my-instance.example.com"):
            with self.subTest(url=url):
                with self.assertRaises(odoo_rpc.ConfigError) as caught:
                    self.load_url(url)
                self.assertNotIn("hunter2pw", str(caught.exception))

    BAD_URLS = (
        "http://evil.com\\.test/x", "http:\\\\localhost", "https://good#x", "https://good?x=1",
        "https://evil.com/?x=.test", "https://evil.com/#.local", "https://good.example.com/a#frag",
        "http://local host", "http://localhost\t", "http://localhost\x0b", "http://local\x00host", "http://local\x1bhost",
        "http://localhost\x7f", "http://localhost\x85", "https://my-instance.example.com ",
        "http://localhost.", "https://my-instance.example.com.", "https://my_instance.example.com",
        "https://bücher.example.com", "https://my-instance.exämple.com", "http://localhost​",
        "https://-bad.example.com", "https://bad-.example.com", "https://a..b.example.com",
        "https://" + "a" * 64 + ".example.com",
    )

    def test_malformed_urls_are_refused_with_a_config_error(self):
        for url in self.BAD_URLS:
            with self.subTest(url=url):
                with self.assertRaises(odoo_rpc.ConfigError):
                    odoo_rpc._validated_url(url)

    def test_malformed_urls_are_refused_when_they_come_from_a_file(self):
        for url in ("https://good#x", "http://evil.com\\.test/x", "http://localhost."):
            with self.subTest(url=url):
                with self.assertRaises(odoo_rpc.ConfigError):
                    self.load_url(url)

    # Kelvin sign U+212A: urlparse lower-cases it to the ASCII letter `k`, so `d<K>sd` would pass as `dksd`
    NON_ASCII_URLS = ("https://dKsd.example.com", "https://dksd.example.com/é", "https://dksd.examplé.com",
                      "https://ｄksd.example.com", "https://dksd.example.com․", "https://dksı.example.com")
    UNPARSABLE_URLS = ("http://[::1]x", "http://[::1", "http://::1]", "http://[]", "https://[::1", "http://a]")

    def test_a_url_with_a_non_ascii_character_is_refused_with_a_config_error(self):
        for url in self.NON_ASCII_URLS:
            with self.subTest(url=url):
                with self.assertRaises(odoo_rpc.ConfigError) as caught:
                    odoo_rpc._validated_url(url)
                self.assertIn("ASCII", str(caught.exception))
                self.assertNotIn(url, str(caught.exception))

    def test_the_kelvin_sign_url_is_refused_from_a_file_and_from_the_environment(self):
        url = "https://dKsd.example.com"
        with self.assertRaises(odoo_rpc.ConfigError):
            self.load_url(url)
        env = dict(EnvironmentVariableTests.ENV, ODOO_KPI_URL=url)
        with self.assertRaises(odoo_rpc.ConfigError):
            odoo_rpc.load_config(path="/nowhere", env=env)

    def test_a_url_urlparse_cannot_parse_is_a_config_error_never_a_raw_value_error(self):
        for url in self.UNPARSABLE_URLS:
            with self.subTest(url=url):
                with self.assertRaises(odoo_rpc.ConfigError):
                    odoo_rpc._validated_url(url)
                with self.assertRaises(odoo_rpc.ConfigError):
                    self.load_url(url)
                with self.assertRaises(odoo_rpc.ConfigError):
                    odoo_rpc.load_config(path="/nowhere", env=dict(EnvironmentVariableTests.ENV, ODOO_KPI_URL=url))

    def test_ipv4_ipv6_and_normal_hosts_are_accepted(self):
        for url in ("http://[::1]:8069", "http://127.0.0.1:8069", "https://203.0.113.5", "http://localhost:8069",
                    "https://my-instance.example.com", "https://MY-Instance.Example.com", "http://dksk.dev.test:8097",
                    "https://a.b.c.d.example.com:8443/odoo", "https://x1.example.com"):
            with self.subTest(url=url):
                self.assertEqual(odoo_rpc._validated_url(url), url)

    def test_the_error_text_of_a_refused_url_does_not_echo_it(self):
        with self.assertRaises(odoo_rpc.ConfigError) as caught:
            odoo_rpc._validated_url("https://good.example.com/path?token=hunter2pw")
        self.assertNotIn("hunter2pw", str(caught.exception))

    def test_trailing_slash_is_stripped(self):
        self.assertEqual(self.load_url(URL + "/").url, URL)
        self.assertEqual(self.load_url(URL + "///").url, URL)
        self.assertEqual(self.load_url(URL).url, URL)

    def test_port_and_path_prefix_are_kept(self):
        self.assertEqual(self.load_url("http://localhost:8069").url, "http://localhost:8069")
        self.assertEqual(self.load_url("https://x.example.com/odoo/").url, "https://x.example.com/odoo")


class EnvironmentVariableTests(IsolatedTestCase):
    ENV = {"ODOO_KPI_URL": URL + "/", "ODOO_KPI_DB": "env_db", "ODOO_KPI_LOGIN": "env.login",
           "ODOO_KPI_API_KEY": "env-key-value"}  # secret-allow: test placeholder, not a real key

    def test_four_env_vars_are_used_without_reading_any_file(self):
        missing = os.path.join(self.tmp, "does-not-exist.ini")
        config = odoo_rpc.load_config(path=missing, env=dict(self.ENV))
        self.assertEqual((config.url, config.db, config.login, config.api_key),
                         (URL, "env_db", "env.login", "env-key-value"))

    def test_four_env_vars_do_not_run_the_permission_guard(self):
        def exploding_sddl(path):
            raise AssertionError("the permission guard must not run when the env vars carry the config")

        config = odoo_rpc.load_config(path="/nowhere", env=dict(self.ENV), os_name="nt", sddl=exploding_sddl,
                                      user_sid=USER_SID)
        self.assertEqual(config.db, "env_db")
        self.assertEqual(config.source, "env")

    def test_env_vars_win_over_an_existing_file(self):
        path = write_file(self.tmp, ini_text("https://file.example.com", db="file_db"))
        config = odoo_rpc.load_config(path=path, env=dict(self.ENV))
        self.assertEqual(config.db, "env_db")

    def test_the_url_from_the_env_is_validated_too(self):
        env = dict(self.ENV, ODOO_KPI_URL="no-scheme.example.com")
        with self.assertRaises(odoo_rpc.ConfigError):
            odoo_rpc.load_config(path="/nowhere", env=env)

    def test_three_env_vars_fall_back_to_the_file(self):
        path = write_file(self.tmp, ini_text("https://file.example.com", db="file_db", login="file.login",
                                             key="file-key"))
        for dropped in self.ENV:
            with self.subTest(unset=dropped):
                env = {name: value for name, value in self.ENV.items() if name != dropped}
                config = load_parsing_env(path, env)
                self.assertEqual((config.url, config.db, config.login, config.api_key),
                                 ("https://file.example.com", "file_db", "file.login", "file-key"))

    def test_an_empty_env_var_counts_as_unset(self):
        env = dict(self.ENV, ODOO_KPI_API_KEY="")
        missing = os.path.join(self.tmp, "does-not-exist.ini")
        with self.assertRaises(odoo_rpc.ConfigError) as caught:
            odoo_rpc.load_config(path=missing, env=env)
        self.assertIn(missing, str(caught.exception))

    def test_the_process_environment_is_the_default_source(self):
        os.environ.update(self.ENV)
        self.assertEqual(odoo_rpc.load_config(path="/nowhere").db, "env_db")


def load_parsing_env(path, env):
    return odoo_rpc.load_config(path=path, env=env, os_name="nt", sddl=_clean_sddl, user_sid=USER_SID)


class SecretInReprTests(unittest.TestCase):
    def test_repr_and_str_do_not_contain_the_key(self):
        config = odoo_rpc.Config(URL, "db", "login", FAKE_KEY)
        for text in (repr(config), str(config), "%s" % config, "%r" % config, "{}".format(config),
                     repr([config]), repr({"c": config}), f"{config}"):
            self.assertNotIn(FAKE_KEY, text)
        self.assertIn("api_key=***", repr(config))
        self.assertIn("db", repr(config))
        self.assertIn(URL, repr(config))

    def test_a_loaded_config_does_not_show_the_key(self):
        config = odoo_rpc.Config(URL, "db", "login", FAKE_KEY)
        self.assertEqual(config.api_key, FAKE_KEY)  # the key is still usable, only hidden from the text forms
        self.assertFalse(hasattr(config, "__dict__"))

    def test_source_defaults_to_file_and_never_shows_in_a_way_that_leaks_the_key(self):
        self.assertEqual(odoo_rpc.Config(URL, "db", "login", FAKE_KEY).source, "file")
        config = odoo_rpc.Config(URL, "db", "login", FAKE_KEY, "env")
        self.assertEqual(config.source, "env")
        self.assertNotIn(FAKE_KEY, repr(config) + str(config))


class ConfigPathTests(IsolatedTestCase):
    def test_odoo_kpi_config_wins_on_every_os(self):
        env = {"ODOO_KPI_CONFIG": "/custom/place.ini", "APPDATA": "C:\\Roaming", "XDG_CONFIG_HOME": "/xdg"}
        self.assertEqual(odoo_rpc.config_path(env=env, os_name="nt"), "/custom/place.ini")
        self.assertEqual(odoo_rpc.config_path(env=env, os_name="posix"), "/custom/place.ini")

    def test_windows_uses_appdata_and_a_backslash_path(self):
        env = {"APPDATA": "C:\\Users\\qa\\AppData\\Roaming"}
        path = odoo_rpc.config_path(env=env, os_name="nt")
        self.assertEqual(path, "C:\\Users\\qa\\AppData\\Roaming\\odoo-kpi\\config.ini")
        self.assertTrue(path.endswith("odoo-kpi\\config.ini"))

    def test_windows_without_appdata_falls_back_to_the_user_profile(self):
        path = odoo_rpc.config_path(env={"USERPROFILE": "C:\\Users\\qa"}, os_name="nt")
        self.assertEqual(path, "C:\\Users\\qa\\AppData\\Roaming\\odoo-kpi\\config.ini")

    def test_posix_uses_xdg_config_home(self):
        path = odoo_rpc.config_path(env={"XDG_CONFIG_HOME": "/x/cfg"}, os_name="posix")
        self.assertEqual(path, os.path.join("/x/cfg", "odoo-kpi", "config.ini"))

    def test_posix_default_is_dot_config_under_home(self):
        for env in ({}, {"XDG_CONFIG_HOME": ""}):
            with self.subTest(env=env):
                path = odoo_rpc.config_path(env=env, os_name="posix")
                self.assertEqual(path, os.path.join(self.home, ".config", "odoo-kpi", "config.ini"))

    def test_the_process_environment_is_the_default(self):
        os.environ["ODOO_KPI_CONFIG"] = "/from/os/environ.ini"
        self.assertEqual(odoo_rpc.config_path(), "/from/os/environ.ini")


if __name__ == "__main__":
    unittest.main()
