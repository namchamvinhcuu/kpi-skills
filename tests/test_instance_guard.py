"""check_instance / instance_name of scripts/odoo_rpc.py (URL guard + --confirm-instance)."""
import unittest

from helpers import IsolatedTestCase, odoo_rpc


class LocalHostTests(IsolatedTestCase):
    def test_local_hosts_pass_over_http_without_confirm(self):
        for url in ("http://localhost", "http://localhost:8069", "http://127.0.0.1:8069", "http://[::1]:8069",
                    "http://x.test", "http://dksk.dev.test:8097", "http://x.localhost:3000",
                    "http://LOCALHOST:8069", "http://User.TEST", "HTTP://LOCALHOST:8069"):
            with self.subTest(url=url):
                self.assertIsNone(odoo_rpc.check_instance(url))

    def test_dot_local_is_no_longer_a_local_suffix(self):
        self.assertEqual(odoo_rpc.LOCAL_SUFFIXES, (".test", ".localhost"))
        for url in ("http://x.local", "http://printer.local:8069", "http://x.LOCAL"):
            with self.subTest(url=url):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    odoo_rpc.check_instance(url, "x")
                self.assertIn("https", str(caught.exception))
        # over https it is an ordinary host: the typed WHOLE host name is needed (round 3: not the first label)
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc.check_instance("https://x.local")
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc.check_instance("https://x.local", "x")
        self.assertIsNone(odoo_rpc.check_instance("https://x.local", "x.local"))

    def test_the_local_exemption_is_exactly_the_loopback_names_and_the_two_suffixes(self):
        # pins the exemption itself: a widened rule (`.local`, any host that merely contains `localhost`, a prefix) would
        # let a credential go to an unconfirmed host
        self.assertEqual(odoo_rpc.LOCAL_HOSTS, frozenset({"localhost", "127.0.0.1", "::1"}))
        for url in ("https://x.local", "https://printer.local:8443", "https://localhost.example.com",
                    "https://mylocalhost", "https://localhost-evil.example.com", "https://evil-localhost.example.com",
                    "https://x.localhost.example.com", "https://127.0.0.2", "https://127.0.0.1.example.com",
                    "https://0.0.0.0", "https://[::2]", "https://test", "https://localtest.example.com"):
            with self.subTest(url=url):
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc.check_instance(url)

    def test_local_hosts_pass_over_https_without_confirm(self):
        for url in ("https://localhost", "https://127.0.0.1:8069", "https://x.test"):
            with self.subTest(url=url):
                self.assertIsNone(odoo_rpc.check_instance(url))


class NonLocalHostTests(IsolatedTestCase):
    def test_http_non_local_host_is_refused_even_with_the_right_confirm(self):
        for url, confirm in (("http://my-instance.example.com", None),
                             ("http://my-instance.example.com", "my-instance"),
                             ("http://my-instance.example.com", "my-instance.example.com"),
                             ("http://8.8.8.8:8069", "8.8.8.8")):
            with self.subTest(url=url, confirm=confirm):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    odoo_rpc.check_instance(url, confirm)
                self.assertIn("https", str(caught.exception))

    def test_https_non_local_host_is_refused_without_confirm(self):
        for confirm in (None, "", "   "):
            with self.subTest(confirm=confirm):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    odoo_rpc.check_instance("https://my-instance.example.com", confirm)
                message = str(caught.exception)
                self.assertIn("my-instance.example.com", message)            # names the host
                self.assertIn("--confirm-instance", message)                 # names the option
                self.assertNotIn("--confirm-instance my-instance", message)  # but is no ready-made flag
                self.assertIn("never fill it in yourself", message)
                self.assertIn("ask the user", message)
                self.assertIn("type the full host name", message)            # round 3: the whole host, not a label

    def test_https_non_local_host_is_refused_with_a_wrong_confirm(self):
        # "my-instance.example.com" was in this list while the first label was the rule: it is now THE right answer
        # (see the accepted test below); "my-instance" (the first label) moved to the dedicated test further down
        for confirm in ("other", "my", "my-instance-2", "example"):
            with self.subTest(confirm=confirm):
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc.check_instance("https://my-instance.example.com", confirm)

    def test_https_non_local_host_is_refused_with_the_first_label_alone(self):
        # round 3 (security r2): the first label was the old rule; any label of the host alone is now refused
        for confirm in ("my-instance", "MY-INSTANCE", "  my-instance  ", "my-instance.", "example", "com",
                        "example.com", "my-instance.example"):
            with self.subTest(confirm=repr(confirm)):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    odoo_rpc.check_instance("https://my-instance.example.com", confirm)
                self.assertIn("type the full host name", str(caught.exception))

    def test_a_confirm_that_only_looks_like_the_host_is_refused(self):
        # a label plus another domain, a prefix, a suffix, and the port / path / scheme / spacing / case variants of the
        # right host that are not the bare host name; each keeps the refusal it had under the first-label rule
        for confirm in ("my-instance.attacker.example", "my-instance.example.com.attacker.example",
                        "attacker.my-instance.example.com", "x.my-instance.example.com", "xmy-instance.example.com",
                        "my-instance.example.comx", "instance.example.com", "my-instance.example.co",
                        "my-instance.example.com.", ".my-instance.example.com", "my-instance.example.com:8443",
                        "my-instance.example.com/", "my-instance.example.com/web", "https://my-instance.example.com",
                        "//my-instance.example.com", "my-instance .example.com", "my instance.example.com",
                        "my-instance.example.com\x00", "my-instance.example.com​", "my-instance.examplе.com",
                        "my‑instance.example.com", "my-instance.example.com my-instance.example.com",
                        "my-instance.example.com,my-instance.example.com"):
            with self.subTest(confirm=repr(confirm)):
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc.check_instance("https://my-instance.example.com", confirm)

    def test_https_non_local_host_is_accepted_with_the_whole_host_name(self):
        for confirm in ("my-instance.example.com", "MY-INSTANCE.EXAMPLE.COM", "My-Instance.Example.Com",
                        "  my-instance.example.com  ", "\tmy-instance.example.com\n"):
            with self.subTest(confirm=repr(confirm)):
                self.assertIsNone(odoo_rpc.check_instance("https://my-instance.example.com", confirm))

    def test_confirm_with_an_uppercase_host_and_a_path_and_a_port(self):
        # the host name is compared, so the URL's own port / path / case do not enter the confirmation ...
        self.assertIsNone(odoo_rpc.check_instance("https://My-Instance.Example.com:8443/web", "my-instance.example.com"))
        # ... and typing them into the confirmation is refused
        for confirm in ("my-instance", "my-instance.example.com:8443", "my-instance.example.com/web",
                        "My-Instance.Example.com:8443/web"):
            with self.subTest(confirm=confirm):
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc.check_instance("https://My-Instance.Example.com:8443/web", confirm)

    def test_https_ip_address_needs_the_whole_address(self):
        url = "https://203.0.113.5:8443"
        for confirm in ("203", "203.0.113", "0.113.5", "203.0.113.5:8443", "203.0.113.50"):
            with self.subTest(confirm=confirm):
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc.check_instance(url, confirm)
        self.assertIsNone(odoo_rpc.check_instance(url, "203.0.113.5"))

    def test_https_ipv6_address_needs_the_whole_address(self):
        url = "https://[2001:db8::1]:8443"
        for confirm in ("2001", "2001:db8", "[2001:db8::1]", "2001:db8::2"):
            with self.subTest(confirm=confirm):
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc.check_instance(url, confirm)
        self.assertIsNone(odoo_rpc.check_instance(url, "2001:db8::1"))

    def test_an_idn_host_is_refused_in_unicode_and_needs_the_punycode_host(self):
        # the url must be ASCII (punycode): a unicode host never reaches the confirmation, and the typed confirmation
        # of a punycode host is its whole punycode name
        for url in ("https://bücher.example.com", "https://xn--bcher-kva.example.comé"):
            for confirm in ("bücher", "bücher.example.com", "xn--bcher-kva", "xn--bcher-kva.example.com"):
                with self.subTest(url=url, confirm=confirm):
                    with self.assertRaises(odoo_rpc.GuardError):
                        odoo_rpc.check_instance(url, confirm)
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc.check_instance("https://xn--bcher-kva.example.com", "xn--bcher-kva")
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc.check_instance("https://xn--bcher-kva.example.com", "bücher.example.com")
        self.assertIsNone(odoo_rpc.check_instance("https://xn--bcher-kva.example.com", "xn--bcher-kva.example.com"))


class LookAlikeConfirmTests(IsolatedTestCase):
    """Round 3 (security r2): the guard compared only the first DNS label, so a URL for a look-alike host passed for a
    human who typed the label she knows. The credential goes to the URL, so the confirmation must be the whole host."""
    LEGIT = "my-instance.example.com"
    ATTACK_URLS = ("https://my-instance.attacker.example", "https://my-instance.example.com.attacker.example",
                   "https://my-instance.example.com.evil", "https://my-instance.example.org",
                   "https://my-instance.example.com-evil.example", "https://my-instance-example.com")

    def test_typed_with_the_first_label_a_look_alike_url_is_refused(self):
        for url in self.ATTACK_URLS:
            with self.subTest(url=url):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    odoo_rpc.check_instance(url, "my-instance")
                self.assertIn("type the full host name", str(caught.exception))

    def test_typed_with_the_legitimate_host_a_look_alike_url_is_refused(self):
        for url in self.ATTACK_URLS:
            with self.subTest(url=url):
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc.check_instance(url, self.LEGIT)

    def test_typed_with_the_look_alike_host_itself_the_human_has_confirmed_what_she_reaches(self):
        # the control: the rule is "the typed name equals the host the request goes to", nothing is blacklisted
        for url in self.ATTACK_URLS:
            with self.subTest(url=url):
                host = url.split("//")[1]
                self.assertIsNone(odoo_rpc.check_instance(url, host))
                self.assertIsNone(odoo_rpc.check_instance(url, host.upper()))

    def test_the_legitimate_url_still_needs_the_whole_host_not_the_label(self):
        self.assertIsNone(odoo_rpc.check_instance("https://" + self.LEGIT, self.LEGIT))
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc.check_instance("https://" + self.LEGIT, "my-instance")

    def test_the_client_never_reaches_the_network_for_a_look_alike_url_typed_with_the_label_or_the_legit_host(self):
        from helpers import ExplodingOpener, make_config
        for url in self.ATTACK_URLS:
            for confirm in ("my-instance", self.LEGIT):
                with self.subTest(url=url, confirm=confirm):
                    opener = ExplodingOpener()
                    with self.assertRaises(odoo_rpc.GuardError):
                        odoo_rpc.Client(make_config(url), confirm_instance=confirm, opener=opener)
                    self.assertEqual(opener.calls, 0)

    def test_the_refusal_names_the_host_the_request_would_go_to(self):
        with self.assertRaises(odoo_rpc.GuardError) as caught:
            odoo_rpc.check_instance("https://my-instance.attacker.example", "my-instance")
        self.assertTrue(str(caught.exception).startswith("my-instance.attacker.example is not a local dev host"))


class LookAlikeHostTests(IsolatedTestCase):
    LOOK_ALIKES = (
        "https://localhost.example.com",
        "https://127.0.0.1.example.com",
        "https://evil.com/?x=.test",
        "https://evil.com/#.local",
        "https://localhost@evil.com",
        "https://evil.com@localhost.example.com",
        "https://notlocalhost",
        "https://mytest",
        "https://x.test.example.com",
        "https://x.local.example.com",
    )

    def test_look_alike_hosts_are_not_treated_as_local_over_https(self):
        for url in self.LOOK_ALIKES:
            with self.subTest(url=url):
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc.check_instance(url)

    def test_look_alike_hosts_are_not_treated_as_local_over_http(self):
        for url in self.LOOK_ALIKES:
            with self.subTest(url=url):
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc.check_instance(url.replace("https://", "http://"), "evil")


class HostSyntaxTests(IsolatedTestCase):
    BAD = (
        "http://evil.com\\.test/x", "http:\\\\localhost", "https://good#x", "https://good?x=1",
        "https://evil.com/?x=.test", "http://localhost/#.test", "http://local host", "http://localhost ",
        " http://localhost", "http://localhost\t", "http://localhost\n", "http://local\x00host", "http://local\x1bhost",
        "http://localhost\x7f", "http://localhost\x85", "http://localhost.", "https://my-instance.example.com.",
        "https://my_instance.example.com", "https://bücher.example.com", "http://localhost​", "https://",
        "http:///path", "my-instance.example.com", "//my-instance.example.com", "ftp://x.example.com",
        "file:///etc/passwd", "https://user@my-instance.example.com", "https://user:pw@my-instance.example.com",
        "https://-bad.example.com", "https://a..example.com", "", None,
    )

    @staticmethod
    def unlocking_confirms(url):
        """What a human could type for `url` under the whole-host rule: the label she knows, the legitimate host, and
        the host name of the url itself when it can be parsed (so the confirmation is NOT what makes the url fail)."""
        import urllib.parse
        typed = ["my-instance", "my-instance.example.com"]
        try:
            host = urllib.parse.urlparse(url or "").hostname
        except ValueError:
            host = None
        if host:
            typed += [host, host.lower()]
        return typed

    def test_bad_urls_are_refused_with_a_guard_error_even_with_the_confirm(self):
        for url in self.BAD:
            for confirm in self.unlocking_confirms(url):
                with self.subTest(url=url, confirm=confirm):
                    with self.assertRaises(odoo_rpc.GuardError):
                        odoo_rpc.check_instance(url, confirm)

    def test_bad_urls_never_reach_the_network_through_the_client(self):
        from helpers import ExplodingOpener, make_config
        for url in self.BAD:
            for confirm in self.unlocking_confirms(url):
                with self.subTest(url=url, confirm=confirm):
                    opener = ExplodingOpener()
                    with self.assertRaises(odoo_rpc.GuardError):
                        odoo_rpc.Client(make_config(url), confirm_instance=confirm, opener=opener)
                    self.assertEqual(opener.calls, 0)

    def test_the_refusal_text_does_not_echo_the_url(self):
        with self.assertRaises(odoo_rpc.GuardError) as caught:
            odoo_rpc.check_instance("https://good.example.com/path?token=hunter2pw")
        self.assertNotIn("hunter2pw", str(caught.exception))

    def test_a_trailing_dot_cannot_smuggle_a_local_name(self):
        for url in ("http://localhost.", "http://x.test.", "http://x.localhost."):
            with self.subTest(url=url):
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc.check_instance(url)

    def test_valid_hosts_pass_the_syntax_check(self):
        for url in ("http://[::1]:8069", "http://127.0.0.1:8069", "https://my-instance.example.com",
                    "https://MY-INSTANCE.EXAMPLE.COM", "http://a-b.test", "http://x1.localhost"):
            with self.subTest(url=url):
                self.assertIsNone(odoo_rpc._url_problem(url))

    def test_url_problem_returns_a_reason_text_not_none_for_bad_urls(self):
        for url in self.BAD:
            with self.subTest(url=url):
                self.assertIsInstance(odoo_rpc._url_problem(url), str)

    def test_uppercase_host_is_compared_in_lower_case(self):
        self.assertIsNone(odoo_rpc.check_instance("https://MY-Instance.EXAMPLE.com", "my-instance.example.com"))
        self.assertIsNone(odoo_rpc.check_instance("https://my-instance.example.com", "MY-Instance.EXAMPLE.com"))
        self.assertIsNone(odoo_rpc.check_instance("http://LOCALHOST"))
        self.assertIsNone(odoo_rpc.check_instance("http://X.TEST"))


class AsciiAndUnparsableUrlTests(IsolatedTestCase):
    KELVIN = "https://dKsd.example.com"   # urlparse lower-cases U+212A to the ASCII letter `k`: host `dksd`

    def test_urlparse_really_folds_the_kelvin_sign_to_ascii(self):
        # the premise of the attack: without the ASCII rule the host would equal the instance name `dksd`
        import urllib.parse
        self.assertEqual(urllib.parse.urlparse(self.KELVIN).hostname.lower(), "dksd.example.com")
        self.assertEqual(odoo_rpc.instance_name(self.KELVIN), "dksd")

    def test_the_kelvin_sign_url_is_refused_by_check_instance(self):
        with self.assertRaises(odoo_rpc.GuardError) as caught:
            odoo_rpc.check_instance(self.KELVIN)
        self.assertIn("ASCII", str(caught.exception))

    def test_the_confirm_for_the_folded_name_does_not_unlock_the_kelvin_sign_url(self):
        for confirm in ("dksd", "DKSD", "dKsd", "dksd.example.com", "DKSD.EXAMPLE.COM",
                        "dKsd.example.com"):
            # `dksd.example.com` is the folded WHOLE host (what the guard would compare after urlparse): the premise of
            # the attack under the whole-host rule; the first label `dksd` was the premise under the old rule
            with self.subTest(confirm=confirm):
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc.check_instance(self.KELVIN, confirm)

    def test_the_same_url_in_ascii_is_unlocked_by_the_same_confirm(self):
        # the control: only the ASCII rule makes the difference
        self.assertIsNone(odoo_rpc.check_instance("https://dksd.example.com", "dksd.example.com"))
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc.check_instance("https://dksd.example.com", "dksd")

    def test_non_ascii_urls_are_refused_even_for_local_looking_hosts(self):
        # the first two fold to `k.test` / `dksd.localhost`: local over plain http unless the ASCII rule refuses them
        for url in ("http://K.test", "http://dKsd.localhost", "http://x.test․", "http://localhost/é",
                    "http://127.0.0.1:8069/ü"):
            with self.subTest(url=url):
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc.check_instance(url)

    def test_the_client_never_reaches_the_network_for_the_kelvin_sign_url(self):
        from helpers import ExplodingOpener, make_config
        for confirm in ("dksd", "dksd.example.com"):
            with self.subTest(confirm=confirm):
                opener = ExplodingOpener()
                with self.assertRaises(odoo_rpc.GuardError):
                    odoo_rpc.Client(make_config(self.KELVIN), confirm_instance=confirm, opener=opener)
                self.assertEqual(opener.calls, 0)

    def test_main_refuses_the_kelvin_sign_url_with_exit_2_even_with_the_folded_confirm(self):
        import io
        import os
        os.environ.update({"ODOO_KPI_URL": self.KELVIN, "ODOO_KPI_DB": "db", "ODOO_KPI_LOGIN": "u",
                           "ODOO_KPI_API_KEY": "k"})
        for confirm in ("dksd", "dksd.example.com"):
            with self.subTest(confirm=confirm):
                out, err = io.StringIO(), io.StringIO()
                code = odoo_rpc.main(["ping", "--confirm-instance", confirm], out, err)
                self.assertEqual(code, 2)
                self.assertEqual(out.getvalue(), "")
                self.assertIn("ASCII", err.getvalue())

    def test_a_url_urlparse_rejects_is_a_guard_error_never_a_raw_value_error(self):
        for url in ("http://[::1]x", "http://[::1", "http://::1]", "http://[]", "https://[::1", "http://a]"):
            for confirm in ("my-instance", "my-instance.example.com"):
                with self.subTest(url=url, confirm=confirm):
                    with self.assertRaises(odoo_rpc.GuardError):
                        odoo_rpc.check_instance(url, confirm)
                    self.assertIsInstance(odoo_rpc._url_problem(url), str)

    def test_urlparse_really_raises_value_error_on_those_urls(self):
        # the premise: without the `except ValueError` the exceptions below would escape as ValueError
        import urllib.parse
        for url in ("http://[::1]x", "http://[::1"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    urllib.parse.urlparse(url)

    def test_the_client_never_reaches_the_network_for_an_unparsable_url(self):
        from helpers import ExplodingOpener, make_config
        for url in ("http://[::1]x", "http://[::1"):
            for confirm in ("my-instance", "my-instance.example.com"):
                with self.subTest(url=url, confirm=confirm):
                    opener = ExplodingOpener()
                    with self.assertRaises(odoo_rpc.GuardError):
                        odoo_rpc.Client(make_config(url), confirm_instance=confirm, opener=opener)
                    self.assertEqual(opener.calls, 0)

    def test_main_returns_2_with_one_line_for_an_unparsable_url(self):
        import io
        import os
        for url in ("http://[::1]x", "http://[::1"):
            with self.subTest(url=url):
                os.environ.update({"ODOO_KPI_URL": url, "ODOO_KPI_DB": "db", "ODOO_KPI_LOGIN": "u",
                                   "ODOO_KPI_API_KEY": "k"})
                out, err = io.StringIO(), io.StringIO()
                code = odoo_rpc.main(["ping"], out, err)
                self.assertEqual(code, 2)
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(err.getvalue().count("\n"), 1)
                self.assertNotIn("Traceback", err.getvalue())
                self.assertNotIn("unexpected", err.getvalue())


class InstanceNameTests(unittest.TestCase):
    def test_instance_name_is_the_first_label_of_a_host(self):
        self.assertEqual(odoo_rpc.instance_name("https://my-instance.example.com"), "my-instance")
        self.assertEqual(odoo_rpc.instance_name("https://My-Instance.Example.com:8443/web"), "my-instance")
        self.assertEqual(odoo_rpc.instance_name("https://single"), "single")

    def test_instance_name_is_the_whole_address_for_an_ip(self):
        self.assertEqual(odoo_rpc.instance_name("https://10.1.2.3:8443"), "10.1.2.3")
        self.assertEqual(odoo_rpc.instance_name("http://127.0.0.1:8069"), "127.0.0.1")
        self.assertEqual(odoo_rpc.instance_name("http://[::1]:8069"), "::1")

    def test_instance_name_of_localhost(self):
        self.assertEqual(odoo_rpc.instance_name("http://localhost:8069"), "localhost")

    def test_instance_name_of_a_host_that_starts_with_digits(self):
        self.assertEqual(odoo_rpc.instance_name("https://127.0.0.1.example.com"), "127")


if __name__ == "__main__":
    unittest.main()
