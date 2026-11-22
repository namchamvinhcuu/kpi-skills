"""Windows permission guard of scripts/odoo_rpc.py: SDDL (SIDs only) driven by injected strings and fake runners.

Everything runs on any OS: the SDDL text, the user SID and the subprocess runner are injected.
"""
import ntpath
import os
import subprocess
import types
import unittest
from unittest import mock

from fake_odoo import FAKE_KEY
from helpers import IsolatedTestCase, ini_text, odoo_rpc, write_file

CONFIG_PATH = "C:\\Users\\qa\\AppData\\Roaming\\odoo-kpi\\config.ini"
USER = "S-1-5-21-1-2-3-1001"
CLEAN = "D:PAI(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;%s)" % USER
HINT = "ODOO_KPI_"


def sddl_of(*aces, flags="PAI"):
    return "D:" + flags + "".join("(%s)" % ace for ace in aces)


def check(text, user=USER, path=CONFIG_PATH):
    return odoo_rpc.check_private_file(path, os_name="nt", sddl=lambda p: text, user_sid=user)


class RefusedAllowEntryTests(IsolatedTestCase):
    def test_allow_for_broad_or_foreign_principals_is_refused(self):
        cases = {"WD (Everyone alias)": "WD", "BU (Users)": "BU", "AU (Authenticated Users)": "AU",
                 "DU (Domain Users)": "DU", "S-1-1-0 (Everyone)": "S-1-1-0", "S-1-5-11 (Authenticated)": "S-1-5-11",
                 "a random SID": "S-1-5-21-9-9-9-500", "another user": "S-1-5-21-1-2-3-1002",
                 "a longer SID of the same prefix": USER + "0", "Guests": "BG", "Network": "NU"}
        for label, sid in cases.items():
            with self.subTest(principal=label):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    check(sddl_of("A;;FA;;;SY", "A;;FA;;;%s" % sid))
                self.assertIn(sid, str(caught.exception))

    def test_a_broad_entry_is_refused_wherever_it_sits_in_the_list(self):
        for aces in (("A;;FR;;;WD",), ("A;;FR;;;WD", "A;;FA;;;SY", "A;;FA;;;BA"),
                     ("A;;FA;;;SY", "A;;FA;;;BA", "A;;FR;;;WD")):
            with self.subTest(aces=aces):
                with self.assertRaises(odoo_rpc.GuardError):
                    check(sddl_of(*aces))

    def test_every_allow_type_is_checked(self):
        for ace_type in ("A", "OA", "XA", "ZA", "a", "oa"):
            with self.subTest(ace_type=ace_type):
                with self.assertRaises(odoo_rpc.GuardError):
                    check(sddl_of("%s;;FA;;;WD" % ace_type))

    def test_sid_comparison_is_case_insensitive_for_the_refusal_too(self):
        for sid in ("wd", "Wd", "bu"):
            with self.subTest(sid=sid):
                with self.assertRaises(odoo_rpc.GuardError):
                    check(sddl_of("A;;FA;;;SY", "A;;FA;;;" + sid))

    def test_deny_for_one_principal_does_not_excuse_an_allow_for_another(self):
        with self.assertRaises(odoo_rpc.GuardError):
            check(sddl_of("D;;FA;;;WD", "A;;FA;;;SY", "A;;FR;;;BU"))

    def test_deny_and_allow_for_the_same_principal_is_refused(self):
        with self.assertRaises(odoo_rpc.GuardError):
            check(sddl_of("D;;FW;;;WD", "A;;FR;;;WD", "A;;FA;;;SY"))

    def test_the_refusal_names_the_principal_and_gives_the_environment_variable_hint(self):
        # Spec of the redesign: every GuardError text of the Windows path ends with the environment-variable hint.
        with self.assertRaises(odoo_rpc.GuardError) as caught:
            check(sddl_of("A;;FA;;;SY", "A;;FR;;;WD"))
        self.assertIn("WD", str(caught.exception))
        self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK), str(caught.exception))
        self.assertIn("(remove the other entries)" + odoo_rpc.WINDOWS_FALLBACK, str(caught.exception))

    def test_every_foreign_sid_refusal_ends_with_the_hint(self):
        for sid in ("WD", "BU", "S-1-5-21-9-9-9-500", USER + "0"):
            with self.subTest(sid=sid):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    check(sddl_of("A;;FA;;;SY", "A;;FA;;;" + sid))
                self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK), str(caught.exception))


class UnreadableOrUnknownAclTests(IsolatedTestCase):
    def test_garbage_empty_and_aclless_texts_are_refused(self):
        for text in ("garbage text with rc 0", "", None, "   ", "D:", "D:P", "D:PAI", "O:BAG:SY",
                     "S:(AU;SA;FA;;;WD)", "config.ini\r\nSuccessfully processed 1 files"):
            with self.subTest(text=text):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    check(text)
                self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK))

    def test_unknown_ace_type_is_refused_even_for_an_allowed_sid(self):
        for ace_type in ("X", "ZZ", "Q", "", "AD"):
            with self.subTest(ace_type=ace_type):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    check(sddl_of("A;;FA;;;SY", "%s;;FA;;;SY" % ace_type))
                self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK))

    def test_ace_with_fewer_than_six_fields_is_refused(self):
        for ace in ("A;;FA;;SY", "A;;FA", "A", "A;;FA;;"):
            with self.subTest(ace=ace):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    check(sddl_of("A;;FA;;;SY", ace))
                self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK))

    def test_ace_with_an_empty_sid_field_is_refused(self):
        with self.assertRaises(odoo_rpc.GuardError):
            check(sddl_of("A;;FA;;;"))


class AcceptedAclTests(IsolatedTestCase):
    def test_clean_dacl_is_accepted(self):
        self.assertIsNone(check(CLEAN))

    def test_each_allowed_principal_is_accepted(self):
        for sid in ("SY", "BA", "OW", "CO", "S-1-5-18", "S-1-5-32-544", "S-1-3-4", "S-1-3-0", USER):
            with self.subTest(sid=sid):
                self.assertIsNone(check(sddl_of("A;;FA;;;%s" % sid)))

    def test_user_sid_in_lower_case_is_accepted(self):
        self.assertIsNone(check(sddl_of("A;;FA;;;SY", "A;;FA;;;" + USER.lower())))
        self.assertIsNone(check(sddl_of("A;;FA;;;" + USER), user=USER.lower()))

    def test_inherited_flags_are_accepted(self):
        self.assertIsNone(check(sddl_of("A;OICIID;FA;;;SY", "A;OICIID;FA;;;BA", "A;OICIID;FA;;;" + USER)))
        self.assertIsNone(check("D:AI(A;ID;FA;;;SY)", ))

    def test_every_dacl_flag_form_is_accepted(self):
        for flags in ("", "P", "PAI", "AI", "PAR", "AR"):
            with self.subTest(flags=flags):
                self.assertIsNone(check(sddl_of("A;;FA;;;SY", flags=flags)))

    def test_deny_entries_are_ignored(self):
        for ace_type in ("D", "OD", "XD"):
            with self.subTest(ace_type=ace_type):
                self.assertIsNone(check(sddl_of("%s;;FA;;;WD" % ace_type, "A;;FA;;;SY")))

    def test_deny_only_dacl_has_entries_and_is_accepted(self):
        self.assertIsNone(check(sddl_of("D;;FA;;;WD")))

    def test_audit_and_other_non_allow_ace_types_are_ignored(self):
        for ace_type in ("AU", "OU", "XU", "AL", "ML", "RA", "SP", "TL"):
            with self.subTest(ace_type=ace_type):
                self.assertIsNone(check(sddl_of("A;;FA;;;SY", "%s;;FA;;;WD" % ace_type)))

    def test_the_audit_section_is_not_read_as_dacl(self):
        text = CLEAN + "S:(AU;SA;FA;;;WD)(AU;FA;FA;;;BU)(A;;FA;;;WD)"
        self.assertIsNone(check(text))

    def test_the_icacls_save_layout_with_a_file_name_line_is_accepted(self):
        text = "config.ini\r\n" + CLEAN + "\r\n\r\n"
        self.assertIsNone(check(text))

    def test_owner_and_group_prefix_before_the_dacl_is_accepted(self):
        self.assertIsNone(check("O:BAG:SY" + CLEAN))

    def test_check_sddl_can_be_called_directly(self):
        self.assertIsNone(odoo_rpc.check_sddl(CLEAN, USER))
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc.check_sddl(sddl_of("A;;FA;;;WD"), USER)

    def test_allowed_sids_constant_is_exactly_the_documented_set(self):
        self.assertEqual(odoo_rpc.WINDOWS_ALLOWED_SIDS,
                         {"SY", "BA", "OW", "CO", "S-1-5-18", "S-1-5-32-544", "S-1-3-4", "S-1-3-0"})
        self.assertTrue(odoo_rpc.WINDOWS_FALLBACK.strip("; ").startswith("use the ODOO_KPI_"))


class DaclScannerRefusalTests(IsolatedTestCase):
    """Security review r2 N1 / N2: what the scanner does not understand is refused (fail closed), never skipped."""

    def assertRefused(self, text, fragment=None):
        with self.assertRaises(odoo_rpc.GuardError) as caught:
            check(text)
        self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK), str(caught.exception))
        if fragment:
            self.assertIn(fragment, str(caught.exception))

    def test_a_conditional_ace_with_a_nested_parenthesis_is_refused_n1(self):
        # regression N1: a regex of innermost `(...)` groups read `(AU)` as an audit entry and accepted this
        self.assertRefused("D:AI(A;ID;FA;;;SY)(XA;;FR;;;WD;(Member_of{SID(AU)}))", "does not understand")

    def test_nested_parentheses_are_refused_wherever_they_sit(self):
        for text in ("D:AI(XA;;FR;;;WD;(Member_of{SID(AU)}))",
                     "D:AI(XA;;FA;;;SY;(Member_of{SID(WD)}))",        # even for an allowed SID
                     "D:PAI(A;;FA;;;SY)(A;;FA;;;BA)(XA;;FA;;;BA;(x))",
                     "D:PAI((A;;FA;;;SY))"):
            with self.subTest(text=text):
                self.assertRefused(text)

    def test_a_quote_inside_the_dacl_is_refused_n2(self):
        # regression N2: the `"S:"` made a section scan stop early and hid the broad entry that follows
        self.assertRefused('D:AI(A;ID;FA;;;SY)(XA;;FA;;;SY;(@Resource.a=="S:"))(A;;FR;;;WD)', "does not understand")

    def test_a_bare_quote_without_nesting_is_refused(self):
        for text in ('D:PAI(A;;FA;;;SY)(A";;FA;;;WD)', 'D:PAI(XA;;FA;;;SY;"x")', 'D:PAI(A;;FA;;;S"Y)'):
            with self.subTest(text=text):
                self.assertRefused(text)

    def test_unbalanced_parentheses_are_refused(self):
        for text in ("D:PAI(A;;FA;;;SY", "D:PAI(A;;FA;;;SY)(A;;FA;;;BA", "D:PAI(A;;FA;;;SY))", "D:PAI(A;;FA;;;SY)(",
                     "D:PAI(A;;FA;;;SY))(A;;FR;;;WD)"):
            with self.subTest(text=text):
                self.assertRefused(text)

    def test_the_unbalanced_message_says_so(self):
        self.assertRefused("D:PAI(A;;FA;;;SY", "unbalanced")

    def test_text_after_the_ace_list_that_is_not_a_section_is_refused(self):
        for text in ("D:PAI(A;;FA;;;SY)junk", "D:PAI(A;;FA;;;SY)X:foo", "D:PAI(A;;FA;;;SY)D:", "D:PAI(A;;FA;;;SY)s:(AU)",
                     "D:PAI(A;;FA;;;SY)(A;;FA;;;BA)garbage(A;;FR;;;WD)"):
            with self.subTest(text=text):
                self.assertRefused(text, "does not understand")

    def test_ace_groups_separated_by_a_space_are_refused_not_half_read(self):
        # only the first group is adjacent: the rest is unexplained trailing text
        self.assertRefused("D:PAI(A;;FA;;;SY) (A;;FR;;;WD)")

    def test_an_empty_ace_is_refused(self):
        self.assertRefused("D:PAI()")
        self.assertRefused("D:PAI(A;;FA;;;SY)()")

    def test_a_quoted_dacl_look_alike_inside_a_sacl_string_is_refused(self):
        # a resource attribute ACE carrying the text `D:(...)`: the scanner meets a quote / trailing text, never accepts
        self.assertRefused('D:PAI(A;;FR;;;WD)S:AI(RA;;;;;WD;("A",TS,0,"D:(A;;FA;;;SY)"))')

    def test_a_quoted_fake_dacl_with_a_section_tail_is_refused(self):
        # regression (observation 1 of round 3): the quoted `D:(A;;FA;;;SY)S:` was read as the DACL, hiding `(A;;FR;;;WD)`
        for tail in ("S:", "O:", "G:"):
            with self.subTest(tail=tail):
                text = 'D:PAI(A;;FR;;;WD)S:AI(RA;;;;;WD;("A",TS,0,"D:(A;;FA;;;SY)%s"))' % tail
                self.assertRefused(text, "does not understand")

    def test_a_bare_quote_anywhere_in_the_text_is_refused(self):
        positions = {"in the name line": 'D:\\cfg\\"config.ini\r\n' + CLEAN,
                     "before the DACL": 'x"y ' + CLEAN,
                     "in an owner section before the DACL": 'O:"BA"G:SY' + CLEAN[0:],
                     "after the ACE list": CLEAN + 'S:"x"',
                     "on a later line": CLEAN + '\r\nnote "x"\r\n',
                     "alone": '"'}
        for label, text in positions.items():
            with self.subTest(position=label):
                self.assertRefused(text, "does not understand")

    def test_a_quote_is_refused_through_read_sddl_too(self):
        for label, text in (("name line", 'D:\\cfg\\"c.ini\r\n' + CLEAN + "\r\n"),
                            ("after the ACE list", CLEAN + "\r\nS:\"x\"\r\n"),
                            ("fake DACL in a SACL string",
                             'D:PAI(A;;FR;;;WD)S:AI(RA;;;;;WD;("A",TS,0,"D:(A;;FA;;;SY)S:"))')):
            with self.subTest(position=label):
                decoded = odoo_rpc._read_sddl("c.ini", run=FakeRun(text.encode("utf-16")))
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    odoo_rpc.check_sddl(decoded, USER)
                self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK))

    def test_the_same_texts_without_the_quote_are_accepted_controls(self):
        for text in ('D:\\cfg\\c.ini\r\n' + CLEAN + "\r\n", 'x y ' + CLEAN, 'O:BAG:SY' + CLEAN,
                     CLEAN + 'S:x', CLEAN + '\r\nnote x\r\n'):
            with self.subTest(text=text):
                self.assertIsNone(check(text))
                decoded = odoo_rpc._read_sddl("c.ini", run=FakeRun(text.encode("utf-16")))
                self.assertIsNone(odoo_rpc.check_sddl(decoded, USER))

    def test_the_quote_refusal_happens_in_the_scanner_itself(self):
        with self.assertRaises(odoo_rpc.GuardError) as caught:
            odoo_rpc._dacl_entries('D:PAI(A;;FA;;;SY)S:"x"')
        self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK))

    def test_a_name_line_only_and_no_dacl_is_refused(self):
        for text in ("D:\\cfg\\config.ini\r\n", "D:\\cfg\\config.ini", "D:\r\nSuccessfully processed 1 files",
                     "D:NO_ACCESS_CONTROL", "D:P\r\n"):
            with self.subTest(text=text):
                self.assertRefused(text, "could not read any permission entry")

    def test_the_last_dacl_line_wins_when_it_is_broad(self):
        self.assertRefused(CLEAN + "\r\n" + sddl_of("A;;FA;;;SY", "A;;FR;;;WD"))

    def test_a_broad_entry_after_junk_on_the_same_line_is_still_refused(self):
        self.assertRefused("\ufeffjunk\x00 " + sddl_of("A;;FA;;;SY", "A;;FR;;;WD"))

    def test_a_lower_case_allow_type_for_a_broad_sid_is_refused(self):
        for ace_type in ("a", "oa", "xa", "za"):
            with self.subTest(ace_type=ace_type):
                self.assertRefused(sddl_of("%s;;FA;;;WD" % ace_type), "WD")

    def test_an_unknown_lower_case_type_is_refused(self):
        self.assertRefused(sddl_of("q;;FA;;;SY"))


class DaclScannerAcceptanceTests(IsolatedTestCase):
    def test_a_file_name_line_on_drive_d_before_the_real_dacl_is_accepted(self):
        text = "D:\\cfg\\config.ini\r\n" + CLEAN + "\r\n\r\n"
        self.assertIsNone(check(text))
        self.assertIsNone(check("D:\\cfg\\config.ini\n" + CLEAN))

    def test_the_drive_d_name_line_is_not_taken_for_the_dacl_end_to_end(self):
        raw = ("D:\\cfg\\config.ini\r\n" + CLEAN + "\r\n").encode("utf-16")
        text = odoo_rpc._read_sddl("D:\\cfg\\config.ini", run=FakeRun(raw))
        self.assertIsNone(odoo_rpc.check_sddl(text, USER))

    def test_the_drive_d_name_line_does_not_hide_a_broad_entry_in_the_real_dacl(self):
        with self.assertRaises(odoo_rpc.GuardError):
            check("D:\\cfg\\config.ini\r\n" + sddl_of("A;;FA;;;SY", "A;;FR;;;WD"))

    def test_owner_group_and_dacl_on_one_line_are_accepted(self):
        for text in ("O:BAG:SYD:PAI(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;%s)" % USER,
                     "O:BAG:SYD:PAI(A;;FA;;;SY)",
                     "O:%sG:SYD:AI(A;ID;FA;;;SY)" % USER,
                     "O:BAG:SYD:PAI(A;;FA;;;SY)S:AI(AU;SA;FA;;;WD)"):
            with self.subTest(text=text):
                self.assertIsNone(check(text))

    def test_a_sacl_before_the_dacl_on_one_line_is_accepted(self):
        self.assertIsNone(check("S:(AU;SA;FA;;;WD)" + CLEAN))

    def test_a_broad_entry_in_the_owner_group_dacl_line_is_refused(self):
        with self.assertRaises(odoo_rpc.GuardError):
            check("O:BAG:SYD:PAI(A;;FA;;;SY)(A;;FR;;;WD)")

    def test_garbage_prefix_bytes_before_the_dacl_on_the_same_line_are_accepted(self):
        for prefix in ("\ufeff", "garbage ", "\x00\x01\x02", "\ufffd\ufffd", "config.ini "):
            with self.subTest(prefix=prefix):
                self.assertIsNone(check(prefix + CLEAN))

    def test_a_lower_case_allow_type_for_an_allowed_sid_is_accepted(self):
        for ace_type, sid in (("a", "SY"), ("oa", "BA"), ("a", USER), ("A", USER.lower())):
            with self.subTest(ace_type=ace_type, sid=sid):
                self.assertIsNone(check(sddl_of("%s;;FA;;;%s" % (ace_type, sid))))

    def test_the_audit_section_after_the_dacl_is_not_read_as_dacl(self):
        for tail in ("S:(AU;SA;FA;;;WD)", "S:AI(AU;SA;FA;;;WD)(AU;FA;FA;;;BU)", "S:PAI(ML;;NW;;;LW)", "O:BA", "G:SY",
                     "  S:(AU;SA;FA;;;WD)"):
            with self.subTest(tail=tail):
                self.assertIsNone(check("D:PAI(A;;FA;;;SY)" + tail))
        self.assertIsNone(check("D:PAI(A;;FA;;;SY)S:(AU;SA;FA;;;WD)"))

    def test_the_last_dacl_line_wins_when_it_is_clean(self):
        self.assertIsNone(check(sddl_of("A;;FR;;;WD") + "\r\n" + CLEAN))
        self.assertIsNone(check(sddl_of("A;;FR;;;WD") + "\n" + CLEAN + "\n"))

    def test_trailing_whitespace_and_line_ends_are_accepted(self):
        for tail in ("", " ", "  \t", "\r\n", "\n", "\r\n\r\n", "   \r\n"):
            with self.subTest(tail=repr(tail)):
                self.assertIsNone(check(CLEAN + tail))

    def test_the_dacl_ends_at_the_end_of_its_line(self):
        # spec: "up to the end of that line": a status line after the DACL line is not part of the ACE list
        for tail in ("\r\nSuccessfully processed 1 files; Failed processing 0 files\r\n", "\nsomething else\n",
                     "\r\n\r\njunk"):
            with self.subTest(tail=repr(tail)):
                self.assertIsNone(check(CLEAN + tail))

    def test_the_no_access_control_flag_is_a_flag(self):
        self.assertIsNone(check("D:NO_ACCESS_CONTROL(A;;FA;;;SY)"))

    def test_a_flag_free_dacl_is_accepted(self):
        self.assertIsNone(check("D:(A;;FA;;;SY)"))

    def test_the_scanner_returns_the_ace_strings_in_order(self):
        self.assertEqual(odoo_rpc._dacl_entries(CLEAN), ["A;;FA;;;SY", "A;;FA;;;BA", "A;;FA;;;" + USER])
        self.assertEqual(odoo_rpc._dacl_entries("O:BAG:SY" + CLEAN + "S:(AU;SA;FA;;;WD)"),
                         ["A;;FA;;;SY", "A;;FA;;;BA", "A;;FA;;;" + USER])
        self.assertEqual(odoo_rpc._dacl_entries("D:\\x\\y.ini\r\nD:PAI(A;;FA;;;SY)"), ["A;;FA;;;SY"])
        self.assertEqual(odoo_rpc._dacl_entries("D:(A;;FR;;;WD)\r\nD:(A;;FA;;;SY)"), ["A;;FA;;;SY"])

    def test_the_scanner_finds_no_entries_in_text_without_a_dacl(self):
        for text in ("", None, "garbage", "D:", "D:\\x\\y.ini", "O:BAG:SY", "S:(AU;SA;FA;;;WD)"):
            with self.subTest(text=text):
                self.assertEqual(odoo_rpc._dacl_entries(text), [])


class CheckPrivateFileWiringTests(IsolatedTestCase):
    def test_injected_sddl_and_sid_spawn_no_process(self):
        with mock.patch.object(odoo_rpc.subprocess, "run") as run:
            check(CLEAN)
        run.assert_not_called()

    def test_the_sddl_callable_receives_the_path(self):
        seen = []
        odoo_rpc.check_private_file(CONFIG_PATH, os_name="nt", sddl=lambda p: seen.append(p) or CLEAN, user_sid=USER)
        self.assertEqual(seen, [CONFIG_PATH])

    def test_without_injection_the_real_readers_are_used(self):
        with mock.patch.object(odoo_rpc, "_read_sddl", return_value=CLEAN) as read, \
                mock.patch.object(odoo_rpc, "_current_user_sid", return_value=USER) as who:
            odoo_rpc.check_private_file(CONFIG_PATH, os_name="nt")
        read.assert_called_once_with(CONFIG_PATH)
        who.assert_called_once_with()

    def test_guard_error_from_the_sddl_reader_propagates(self):
        def failing(path):
            raise odoo_rpc.GuardError("cannot read the permissions" + odoo_rpc.WINDOWS_FALLBACK)

        with self.assertRaises(odoo_rpc.GuardError) as caught:
            odoo_rpc.check_private_file(CONFIG_PATH, os_name="nt", sddl=failing, user_sid=USER)
        self.assertIn("cannot read the permissions", str(caught.exception))

    def test_guard_error_from_the_user_lookup_propagates(self):
        with mock.patch.object(odoo_rpc, "_current_user_sid", side_effect=odoo_rpc.GuardError("who failed")):
            with self.assertRaises(odoo_rpc.GuardError):
                odoo_rpc.check_private_file(CONFIG_PATH, os_name="nt", sddl=lambda p: CLEAN)

    def test_load_config_refuses_a_broad_entry_and_never_reads_the_file(self):
        path = write_file(self.tmp, ini_text("https://my-instance.example.com"))
        with mock.patch("configparser.ConfigParser.read") as read:
            with self.assertRaises(odoo_rpc.GuardError) as caught:
                odoo_rpc.load_config(path=path, env={}, os_name="nt", user_sid=USER,
                                     sddl=lambda p: sddl_of("A;;FA;;;SY", "A;;FR;;;WD"))
        read.assert_not_called()
        self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_load_config_accepts_a_clean_dacl(self):
        path = write_file(self.tmp, ini_text("https://my-instance.example.com"))
        config = odoo_rpc.load_config(path=path, env={}, os_name="nt", user_sid=USER, sddl=lambda p: CLEAN)
        self.assertEqual(config.api_key, FAKE_KEY)

    def test_windows_branch_does_not_look_at_mode_bits(self):
        path = write_file(self.tmp, ini_text("https://my-instance.example.com"), 0o666)
        config = odoo_rpc.load_config(path=path, env={}, os_name="nt", user_sid=USER, sddl=lambda p: CLEAN)
        self.assertEqual(config.db, "fake_db")


class System32Tests(unittest.TestCase):
    def test_path_is_absolute_and_honours_systemroot(self):
        path = odoo_rpc._system32("icacls.exe", env={"SystemRoot": "C:\\TestWin"})
        self.assertEqual(path, "C:\\TestWin\\System32\\icacls.exe")
        self.assertTrue(ntpath.isabs(path))

    def test_windir_is_the_second_choice_and_c_windows_the_last(self):
        self.assertEqual(odoo_rpc._system32("whoami.exe", env={"windir": "D:\\W"}), "D:\\W\\System32\\whoami.exe")
        self.assertEqual(odoo_rpc._system32("whoami.exe", env={}), "C:\\Windows\\System32\\whoami.exe")
        self.assertEqual(odoo_rpc._system32("whoami.exe", env={"SystemRoot": "", "windir": "D:\\W"}),
                         "D:\\W\\System32\\whoami.exe")

    def test_systemroot_wins_over_windir(self):
        path = odoo_rpc._system32("icacls.exe", env={"SystemRoot": "C:\\A", "windir": "C:\\B"})
        self.assertEqual(path, "C:\\A\\System32\\icacls.exe")

    def test_never_the_bare_name(self):
        for env in ({}, {"SystemRoot": "C:\\Windows"}, {"PATH": "C:\\evil"}):
            with self.subTest(env=env):
                path = odoo_rpc._system32("icacls.exe", env=env)
                self.assertNotEqual(path, "icacls.exe")
                self.assertTrue(path.endswith("System32\\icacls.exe"))

    def test_the_process_environment_is_the_default(self):
        with mock.patch.dict(os.environ, {"SystemRoot": "C:\\FromEnviron"}):
            self.assertEqual(odoo_rpc._system32("x.exe"), "C:\\FromEnviron\\System32\\x.exe")


class FakeRun:
    """Stands in for subprocess.run: records argv/kwargs, writes the /save target, returns a result object."""

    def __init__(self, content=None, returncode=0, raises=None):
        self.content, self.returncode, self.raises = content, returncode, raises
        self.calls = []
        self.folder_existed_during_call = None

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.raises is not None:
            raise self.raises
        target = argv[3]
        self.folder_existed_during_call = os.path.isdir(os.path.dirname(target))
        if self.content is not None:
            with open(target, "wb") as handle:
                handle.write(self.content)
        return types.SimpleNamespace(returncode=self.returncode, stdout=b"", stderr=b"")


class ReadSddlTests(IsolatedTestCase):
    def test_argv_shape_absolute_icacls_and_save_target(self):
        os.environ["SystemRoot"] = "C:\\TestWin"
        run = FakeRun(CLEAN.encode("utf-16"))
        text = odoo_rpc._read_sddl("relative/config.ini", run=run)

        argv, kwargs = run.calls[0]
        self.assertEqual(argv[0], "C:\\TestWin\\System32\\icacls.exe")
        self.assertTrue(argv[0].endswith("System32\\icacls.exe"))
        self.assertNotEqual(argv[0], "icacls")
        self.assertEqual(argv[1], os.path.abspath("relative/config.ini"))
        self.assertEqual(argv[2], "/save")
        self.assertTrue(os.path.isabs(argv[3]))
        self.assertEqual(len(argv), 4)
        self.assertTrue(kwargs.get("capture_output"))
        self.assertTrue(0 < kwargs.get("timeout", 0) <= 60)
        self.assertEqual(text, CLEAN)

    def test_default_systemroot_gives_c_windows(self):
        os.environ.pop("SystemRoot", None)
        os.environ.pop("windir", None)
        run = FakeRun(CLEAN.encode("utf-16"))
        odoo_rpc._read_sddl("c.ini", run=run)
        self.assertEqual(run.calls[0][0][0], "C:\\Windows\\System32\\icacls.exe")

    def test_utf16_with_bom_little_and_big_endian_and_utf8_are_decoded(self):
        for label, raw in (("utf-16 LE with BOM", CLEAN.encode("utf-16")),
                           ("utf-16 BE with BOM", b"\xfe\xff" + CLEAN.encode("utf-16-be")),
                           ("utf-8", CLEAN.encode("utf-8")),
                           ("utf-8 file name line first", ("config.ini\r\n" + CLEAN + "\r\n").encode("utf-8")),
                           ("utf-16 with a file name line", ("config.ini\r\n" + CLEAN + "\r\n").encode("utf-16"))):
            with self.subTest(encoding=label):
                text = odoo_rpc._read_sddl("c.ini", run=FakeRun(raw))
                self.assertIn("D:PAI(A;;FA;;;SY)", text)
                self.assertIsNone(odoo_rpc.check_sddl(text, USER))

    def test_the_decoded_text_of_a_broad_acl_is_refused_end_to_end(self):
        raw = sddl_of("A;;FA;;;SY", "A;;FR;;;WD").encode("utf-16")
        text = odoo_rpc._read_sddl("c.ini", run=FakeRun(raw))
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc.check_sddl(text, USER)

    def test_the_temp_folder_is_removed_after_success(self):
        run = FakeRun(CLEAN.encode("utf-16"))
        odoo_rpc._read_sddl("c.ini", run=run)
        self.assertTrue(run.folder_existed_during_call)
        self.assertFalse(os.path.exists(os.path.dirname(run.calls[0][0][3])))

    def test_failures_become_guard_errors_with_the_hint_and_remove_the_temp_folder(self):
        failures = {
            "non-zero return code": FakeRun(CLEAN.encode("utf-16"), returncode=5),
            "OSError": FakeRun(raises=OSError("The system cannot find the file specified")),
            "FileNotFoundError": FakeRun(raises=FileNotFoundError("icacls")),
            "timeout": FakeRun(raises=subprocess.TimeoutExpired("icacls", 15)),
            "called process error": FakeRun(raises=subprocess.CalledProcessError(1, "icacls")),
            "missing output file": FakeRun(content=None),
        }
        for label, run in failures.items():
            with self.subTest(failure=label):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    odoo_rpc._read_sddl("c.ini", run=run)
                self.assertIn(HINT, str(caught.exception))
                self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK))
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__ or caught.exception.__context__ is None)
                self.assertFalse(os.path.exists(os.path.dirname(run.calls[0][0][3])), "temp folder left behind")

    def test_undecodable_output_becomes_a_guard_error_with_the_hint(self):
        run = FakeRun(b"\xff\xfeD")  # a UTF-16 BOM followed by an odd number of bytes
        with self.assertRaises(odoo_rpc.GuardError) as caught:
            odoo_rpc._read_sddl("c.ini", run=run)
        self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__ or caught.exception.__context__ is None,
                        "the UnicodeDecodeError (it quotes the bytes) would be printed in the traceback")
        self.assertFalse(os.path.exists(os.path.dirname(run.calls[0][0][3])))

    def test_invalid_utf8_without_bom_is_decoded_leniently_and_then_refused_or_accepted_on_content(self):
        text = odoo_rpc._read_sddl("c.ini", run=FakeRun(b"\xff\xff" + CLEAN.encode("utf-8")))
        self.assertIsNone(odoo_rpc.check_sddl(text, USER))

    def test_every_call_uses_a_private_new_temp_folder(self):
        first, second = FakeRun(CLEAN.encode("utf-16")), FakeRun(CLEAN.encode("utf-16"))
        odoo_rpc._read_sddl("c.ini", run=first)
        odoo_rpc._read_sddl("c.ini", run=second)
        self.assertNotEqual(first.calls[0][0][3], second.calls[0][0][3])

    def test_the_real_runner_fails_closed_when_icacls_cannot_run(self):
        # On Linux the absolute Windows path does not exist; on Windows the path to check does not exist.
        with self.assertRaises(odoo_rpc.GuardError) as caught:
            odoo_rpc._read_sddl(os.path.join(self.tmp, "no-such-file.ini"))
        self.assertIn(HINT, str(caught.exception))


class CurrentUserSidTests(IsolatedTestCase):
    @staticmethod
    def run_returning(stdout, returncode=0):
        calls = []

        def run(argv, **kwargs):
            calls.append((list(argv), kwargs))
            return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=b"")

        run.calls = calls
        return run

    def test_parses_the_sid_from_csv_bytes(self):
        run = self.run_returning(b'"DOMAIN\\user","S-1-5-21-1-2-3-1001"\r\n')
        self.assertEqual(odoo_rpc._current_user_sid(run=run), "S-1-5-21-1-2-3-1001")

    def test_argv_is_the_absolute_system32_whoami(self):
        os.environ["SystemRoot"] = "C:\\TestWin"
        run = self.run_returning(b'"PC\\qa","S-1-5-21-1-2-3-1001"\r\n')
        odoo_rpc._current_user_sid(run=run)
        argv, kwargs = run.calls[0]
        self.assertEqual(argv[0], "C:\\TestWin\\System32\\whoami.exe")
        self.assertTrue(ntpath.isabs(argv[0]))
        self.assertIn("/user", argv)
        self.assertTrue(kwargs.get("capture_output"))
        self.assertTrue(0 < kwargs.get("timeout", 0) <= 60)

    def test_non_ascii_account_names_in_legacy_code_pages_do_not_break_the_parse(self):
        # cp1258 (Vietnamese): letters that exist in the code page, plus a combining tilde (U+0303, also in it)
        names = {"cp949": "PC\\홍길동", "cp1258": "PC\\Nguyêñ Văn A",
                 "cp932": "PC\\山田", "cp1252": "PC\\José"}
        for codepage, account in names.items():
            with self.subTest(codepage=codepage):
                stdout = ('"%s","S-1-5-21-1-2-3-1001"\r\n' % account).encode(codepage)
                self.assertEqual(odoo_rpc._current_user_sid(run=self.run_returning(stdout)), "S-1-5-21-1-2-3-1001")

    def test_a_sid_looking_account_name_does_not_replace_the_real_sid_n7(self):
        # regression N7: a `re.search` of the first SID-shaped text returned the account NAME's text as the user SID
        stdout = b'"DOMAIN\\S-1-5-21-9-9-9-500","S-1-5-21-1-2-3-1001"\r\n'
        self.assertEqual(odoo_rpc._current_user_sid(run=self.run_returning(stdout)), "S-1-5-21-1-2-3-1001")

    def test_the_sid_is_taken_from_field_two_whatever_the_first_field_holds(self):
        for first in ('S-1-5-21-9-9-9-500', 'PC\\S-1-5-18', 'S-1-1-0\\x', 'a,b\\c', 'PC\\S-1-5-21-9-9-9-500,x'):
            with self.subTest(first=first):
                stdout = ('"%s","S-1-5-21-1-2-3-1001"\r\n' % first).encode("latin-1")
                self.assertEqual(odoo_rpc._current_user_sid(run=self.run_returning(stdout)), "S-1-5-21-1-2-3-1001")

    def test_only_the_first_row_is_read(self):
        stdout = b'"PC\\a","S-1-5-21-1-2-3-1001"\r\n"PC\\b","S-1-5-21-9-9-9-500"\r\n'
        self.assertEqual(odoo_rpc._current_user_sid(run=self.run_returning(stdout)), "S-1-5-21-1-2-3-1001")
        # a bad first row is not rescued by a good second row
        stdout = b'"PC\\a","nope"\r\n"PC\\b","S-1-5-21-1-2-3-1001"\r\n'
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc._current_user_sid(run=self.run_returning(stdout))

    def test_a_non_ascii_account_name_in_a_code_page_whose_trail_byte_is_a_backslash_or_a_quote(self):
        # cp932: 0x83 0x5C (ソ) and 0x95 0x5C (表) end in a backslash byte
        stdout = ('"PC\\ソ表","S-1-5-21-1-2-3-1001"\r\n').encode("cp932")
        self.assertIn(b"\x5c", stdout.split(b",")[0][4:])
        self.assertEqual(odoo_rpc._current_user_sid(run=self.run_returning(stdout)), "S-1-5-21-1-2-3-1001")

    def test_a_non_ascii_name_that_would_not_decode_as_utf8_is_read_through_latin1(self):
        stdout = b'"PC\\\xff\xfe\x80","S-1-5-21-1-2-3-1001"\r\n'
        self.assertEqual(odoo_rpc._current_user_sid(run=self.run_returning(stdout)), "S-1-5-21-1-2-3-1001")

    def test_a_row_with_fewer_than_two_fields_is_refused(self):
        for stdout in (b'"S-1-5-21-1-2-3-1001"\r\n', b"S-1-5-21-1-2-3-1001\r\n", b'"PC\\qa"\r\n', b"\r\n", b",",
                       b'"S-1-5-21-1-2-3-1001\r\n'):
            with self.subTest(stdout=stdout):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    odoo_rpc._current_user_sid(run=self.run_returning(stdout))
                self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK))

    def test_a_second_field_that_is_not_a_sid_is_refused(self):
        for second in ("nope", "", "S-1-x", "S-1-5", "S-2-5-21-1-2-3-1001", "s-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1001x",
                       "xS-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1001 S-1-5-18", "S-1-5-21-1--2", "S-1-5-21-1-2-3-"):
            with self.subTest(second=second):
                stdout = ('"PC\\qa","%s"\r\n' % second).encode("latin-1")
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    odoo_rpc._current_user_sid(run=self.run_returning(stdout))
                self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK))

    def test_a_sid_in_the_first_field_only_is_refused(self):
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc._current_user_sid(run=self.run_returning(b'"S-1-5-21-1-2-3-1001","nope"\r\n'))

    def test_a_non_zero_return_code_is_refused_even_with_a_valid_row(self):
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc._current_user_sid(run=self.run_returning(b'"PC\\qa","S-1-5-21-1-2-3-1001"\r\n', returncode=3))

    def test_empty_and_whitespace_only_output_is_refused_with_the_hint(self):
        for stdout in (b"", b"  \r\n", None):
            with self.subTest(stdout=stdout):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    odoo_rpc._current_user_sid(run=self.run_returning(stdout))
                self.assertIn(HINT, str(caught.exception))

    def test_domain_user_sid_with_many_parts(self):
        stdout = b'"CORP\\qa","S-1-5-21-3623811015-3361044348-30300820-1013"\r\n'
        self.assertEqual(odoo_rpc._current_user_sid(run=self.run_returning(stdout)),
                         "S-1-5-21-3623811015-3361044348-30300820-1013")

    def test_failures_become_guard_errors_with_the_hint(self):
        def raising(exc):
            def run(argv, **kwargs):
                raise exc
            return run

        cases = {
            "non-zero return code": self.run_returning(b'"PC\\qa","S-1-5-21-1-2-3-1001"', returncode=1),
            "no SID in the output": self.run_returning(b"INFO: nothing useful\r\n"),
            "empty output": self.run_returning(b""),
            "None output": self.run_returning(None),
            "a SID-like text that is not a SID": self.run_returning(b'"PC\\qa","S-1-x"'),
            "OSError": raising(OSError("no whoami")),
            "FileNotFoundError": raising(FileNotFoundError("whoami")),
            "timeout": raising(subprocess.TimeoutExpired("whoami", 15)),
        }
        for label, run in cases.items():
            with self.subTest(failure=label):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    odoo_rpc._current_user_sid(run=run)
                self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK))
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__ or caught.exception.__context__ is None,
                                "the underlying error would be printed in the traceback")

    def test_the_real_runner_fails_closed_when_whoami_cannot_run(self):
        if os.name == "nt":  # a real Windows runner has whoami: the answer must then be a SID
            self.assertRegex(odoo_rpc._current_user_sid(), r"^S-1-\d+(-\d+)+$")
            return
        with self.assertRaises(odoo_rpc.GuardError):
            odoo_rpc._current_user_sid()


if __name__ == "__main__":
    unittest.main()
