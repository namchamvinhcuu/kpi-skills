"""Windows permission guard of scripts/odoo_rpc.py: SDDL (SIDs only) driven by injected strings and fake runners.

Everything runs on any OS: the SDDL text, the user SID and the subprocess runner are injected.
"""
import ntpath
import os
import subprocess
import sys
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


def read_through_guard(raw):
    """The whole Windows path of the guard on the bytes `icacls /save` would have written (injected runner)."""
    return odoo_rpc.check_private_file(
        CONFIG_PATH, os_name="nt", sddl=lambda p: odoo_rpc._read_sddl(p, run=FakeRun(raw)), user_sid=USER)


VI_NAME_LINE = "Th\u01b0 m\u1ee5c c\u1ea5u h\xecnh Nguy\u1ec5n V\u0103n An\\config.ini\r\n"   # invented, non-ASCII
VI_HEAVY_LINE = "Ng\u01b0\u1ee1ng_T\u1ec7p_C\u1ea5u_H\xecnh_Ng\u01b0\u1eddi_D\xf9ng_Vi\u1ec7t_Nam_\u01afu_Ti\xean_\u0110\u1eb7c\r\n" * 3


class BomlessIcaclsTests(IsolatedTestCase):
    """0.1.3: icacls /save without a BOM. Some Windows builds write the SDDL file as UTF-16 WITHOUT a byte order mark;
    0.1.2 read it as UTF-8 (a NUL after every character), never found `D:(` and refused with "could not read any
    permission entry"."""

    def test_utf16le_without_bom_decodes_to_the_same_text_as_the_bom_forms(self):
        # 0.1.3: icacls /save without a BOM
        bom_text = odoo_rpc._read_sddl("c.ini", run=FakeRun(CLEAN.encode("utf-16")))
        bomless = odoo_rpc._read_sddl("c.ini", run=FakeRun(CLEAN.encode("utf-16-le")))
        self.assertEqual(bomless, CLEAN)
        self.assertEqual(bomless, bom_text)

    def test_utf16be_without_bom_decodes_to_the_same_text_as_the_bom_forms(self):
        # 0.1.3: icacls /save without a BOM
        bom_text = odoo_rpc._read_sddl("c.ini", run=FakeRun(b"\xfe\xff" + CLEAN.encode("utf-16-be")))
        bomless = odoo_rpc._read_sddl("c.ini", run=FakeRun(CLEAN.encode("utf-16-be")))
        self.assertEqual(bomless, CLEAN)
        self.assertEqual(bomless, bom_text)

    def test_the_bom_forms_decode_to_exactly_the_text_without_a_stray_bom_character(self):
        # 0.1.3: icacls /save without a BOM (the BOM branch must stay: a BOM-less decode would leave U+FEFF in front)
        for label, raw in (("LE", CLEAN.encode("utf-16")), ("BE", b"\xfe\xff" + CLEAN.encode("utf-16-be")),
                           ("LE heavy non-ASCII", (VI_HEAVY_LINE + CLEAN).encode("utf-16")),
                           ("BE heavy non-ASCII", b"\xfe\xff" + (VI_HEAVY_LINE + CLEAN).encode("utf-16-be"))):
            with self.subTest(form=label):
                text = odoo_rpc._read_sddl("c.ini", run=FakeRun(raw))
                self.assertNotIn("\ufeff", text)
                self.assertTrue(text.endswith(CLEAN), repr(text[:20]))
                self.assertIsNone(odoo_rpc.check_sddl(text, USER))

    def test_the_whole_guard_accepts_a_bomless_clean_file(self):
        # 0.1.3: icacls /save without a BOM
        for label, raw in (("LE", CLEAN.encode("utf-16-le")), ("BE", CLEAN.encode("utf-16-be")),
                           ("LE CRLF", (CLEAN + "\r\n").encode("utf-16-le")),
                           ("BE CRLF", (CLEAN + "\r\n").encode("utf-16-be"))):
            with self.subTest(form=label):
                self.assertIsNone(read_through_guard(raw))

    def test_a_file_name_line_before_the_sddl_works_without_a_bom(self):
        # 0.1.3: icacls /save without a BOM
        text_in = "config.ini\r\n" + CLEAN + "\r\n"
        for label, raw in (("LE", text_in.encode("utf-16-le")), ("BE", text_in.encode("utf-16-be"))):
            with self.subTest(form=label):
                self.assertEqual(odoo_rpc._read_sddl("c.ini", run=FakeRun(raw)), text_in)
                self.assertIsNone(read_through_guard(raw))

    def test_a_windows_style_file_name_line_with_a_drive_and_backslashes_works_without_a_bom(self):
        # 0.1.3: icacls /save without a BOM (`D:\dir\file` is not a DACL: the scanner takes the real one)
        text_in = "D:\\Users\\qa\\config.ini\r\n" + CLEAN + "\r\n"
        self.assertIsNone(read_through_guard(text_in.encode("utf-16-le")))
        self.assertIsNone(read_through_guard(text_in.encode("utf-16-be")))

    def test_a_bomless_file_with_a_broad_allow_entry_is_still_refused(self):
        # 0.1.3: icacls /save without a BOM (the fail-closed rules apply to the decoded text)
        for label, sid in (("Everyone WD", "WD"), ("Users BU", "BU"), ("Authenticated AU", "AU"),
                           ("Domain Users DU", "DU"), ("Everyone SID", "S-1-1-0"), ("another user", "S-1-5-21-1-2-3-1002")):
            for enc in ("utf-16-le", "utf-16-be"):
                with self.subTest(principal=label, encoding=enc):
                    raw = sddl_of("A;;FA;;;SY", "A;;FA;;;BA", "A;;FA;;;%s" % sid).encode(enc)
                    with self.assertRaises(odoo_rpc.GuardError) as caught:
                        read_through_guard(raw)
                    self.assertIn(sid, str(caught.exception))
                    self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK))

    def test_a_bomless_broad_entry_is_refused_with_a_file_name_line_and_in_any_position(self):
        # 0.1.3: icacls /save without a BOM
        for aces in (("A;;FR;;;WD",), ("A;;FR;;;WD", "A;;FA;;;SY"), ("A;;FA;;;SY", "A;;FR;;;BU", "A;;FA;;;BA")):
            with self.subTest(aces=aces):
                raw = ("config.ini\r\n" + sddl_of(*aces) + "\r\n").encode("utf-16-le")
                with self.assertRaises(odoo_rpc.GuardError):
                    read_through_guard(raw)

    def test_the_reported_failure_a_bomless_utf16le_file_is_not_called_unreadable_regression(self):
        # 0.1.3: icacls /save without a BOM. Regression test for the 0.1.2 report: `init-config` failed with "could
        # not read any permission entry of the config file" because the UTF-16LE text was decoded as UTF-8.
        raw = CLEAN.encode("utf-16-le")
        text = odoo_rpc._read_sddl("c.ini", run=FakeRun(raw))
        try:
            odoo_rpc.check_sddl(text, USER)
        except odoo_rpc.GuardError as error:
            self.fail("a clean BOM-less UTF-16LE file was refused: %s" % error)
        self.assertNotIn("\x00", text)
        self.assertIn("D:PAI(", text)

    def test_plain_utf8_is_still_utf8(self):
        # 0.1.3: icacls /save without a BOM
        for label, text_in in (("ASCII", CLEAN), ("ASCII CRLF with name", "config.ini\r\n" + CLEAN + "\r\n"),
                               ("non-ASCII file name line", VI_NAME_LINE + CLEAN + "\r\n"),
                               ("heavy non-ASCII", VI_HEAVY_LINE + CLEAN)):
            with self.subTest(text=label):
                text = odoo_rpc._read_sddl("c.ini", run=FakeRun(text_in.encode("utf-8")))
                self.assertEqual(text, text_in)
                self.assertIsNone(odoo_rpc.check_sddl(text, USER))

    def test_utf8_ascii_alone_is_never_mistaken_for_utf16(self):
        # 0.1.3: icacls /save without a BOM (no NUL at all: neither threshold is met)
        self.assertEqual(odoo_rpc._decode_icacls(CLEAN.encode()), CLEAN)
        self.assertEqual(odoo_rpc._decode_icacls(("x" * 4).encode()), "xxxx")

    def test_a_bomless_file_whose_name_line_has_a_few_non_ascii_letters_still_decodes(self):
        # 0.1.3: icacls /save without a BOM. The SDDL dominates the byte pattern, so a Vietnamese path line is fine.
        text_in = VI_NAME_LINE + CLEAN + "\r\n"
        for enc in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=enc):
                self.assertEqual(odoo_rpc._read_sddl("c.ini", run=FakeRun(text_in.encode(enc))), text_in)
                self.assertIsNone(read_through_guard(text_in.encode(enc)))

    def test_a_bomless_file_with_a_long_non_ascii_first_line_is_read_from_its_ascii_tail(self):
        # Updated ON PURPOSE round 2 (round 1 pinned a false refusal here): only the last 128 bytes (the DACL line)
        # are measured, so a long Vietnamese first line no longer hides that the file is UTF-16.
        text_in = VI_HEAVY_LINE + CLEAN
        for enc in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=enc):
                self.assertEqual(odoo_rpc._read_sddl("c.ini", run=FakeRun(text_in.encode(enc))), text_in)
                self.assertIsNone(read_through_guard(text_in.encode(enc)))
        self.assertIsNone(read_through_guard(text_in.encode("utf-16")))        # control: the BOM form

    def test_a_bomless_file_whose_tail_is_mostly_non_ascii_still_fails_closed(self):
        # Updated ON PURPOSE round 2: what is still refused is a file whose last 128 bytes are not ASCII-dominated
        # (a short DACL line right after a long non-Latin name): refused as unreadable, never accepted.
        text_in = VI_HEAVY_LINE + "D:PAI(A;;FA;;;SY)"
        for enc in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=enc):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    read_through_guard(text_in.encode(enc))
                self.assertIn("unreadable icacls output", str(caught.exception))
                self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK))
        self.assertIsNone(read_through_guard(text_in.encode("utf-16")))        # control: the BOM form

    def test_a_mostly_non_ascii_bomless_file_with_a_broad_entry_is_never_accepted(self):
        # 0.1.3: icacls /save without a BOM (whatever branch decodes it, a broad entry must not pass)
        text_in = VI_HEAVY_LINE + sddl_of("A;;FA;;;SY", "A;;FR;;;WD")
        for enc in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=enc):
                with self.assertRaises(odoo_rpc.GuardError):
                    read_through_guard(text_in.encode(enc))

    def test_existing_odd_cases_keep_failing_the_same_way(self):
        # 0.1.3: icacls /save without a BOM (BOM and odd bytes, a lenient UTF-8 fallback, a bad BOM-less tail)
        run = FakeRun(b"\xff\xfeD")
        with self.assertRaises(odoo_rpc.GuardError) as caught:
            odoo_rpc._read_sddl("c.ini", run=run)
        self.assertIn("unreadable icacls output", str(caught.exception))
        self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK))
        self.assertIsNone(caught.exception.__cause__)
        text = odoo_rpc._read_sddl("c.ini", run=FakeRun(b"\xff\xff" + CLEAN.encode()))
        self.assertTrue(text.startswith("\ufffd\ufffd"), repr(text[:5]))
        self.assertTrue(text.endswith(CLEAN))
        self.assertIsNone(odoo_rpc.check_sddl(text, USER))

    def test_empty_and_one_to_three_bytes_are_never_accepted_and_never_crash(self):
        # 0.1.3: icacls /save without a BOM (the 4-byte minimum: shorter input is never guessed to be UTF-16)
        for raw in (b"", b"D", b"\x00", b"D:", b"D\x00", b"\x00D", b"D:(", b"D\x00:", b"\x00D\x00", b"\xff", b"\xfe\xff",
                    b"\xff\xfe"):
            with self.subTest(raw=raw):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    read_through_guard(raw)
                self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK))

    def test_a_truncated_bomless_file_of_odd_length_is_unreadable_not_half_read(self):
        # 0.1.3: icacls /save without a BOM
        for enc in ("utf-16-le", "utf-16-be"):
            full = CLEAN.encode(enc)
            for cut in (len(full) - 1, len(full) - 3, 41, 21, 5):
                with self.subTest(encoding=enc, length=cut):
                    run = FakeRun(full[:cut])
                    with self.assertRaises(odoo_rpc.GuardError) as caught:
                        odoo_rpc._read_sddl("c.ini", run=run)
                    self.assertIn("unreadable icacls output", str(caught.exception))
                    self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK))
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertFalse(os.path.exists(os.path.dirname(run.calls[0][0][3])))

    def test_a_bomless_file_cut_inside_an_entry_is_refused_at_every_even_cut_point(self):
        # 0.1.3: icacls /save without a BOM (a cut at an entry boundary is a shorter valid list: not asserted)
        for enc in ("utf-16-le", "utf-16-be"):
            for chars in range(2, len(CLEAN)):
                if CLEAN[chars - 1] == ")":
                    continue
                with self.subTest(encoding=enc, chars=chars):
                    with self.assertRaises(odoo_rpc.GuardError):
                        read_through_guard(CLEAN[:chars].encode(enc))

    def test_a_bomless_file_missing_its_first_byte_is_refused(self):
        # 0.1.3: icacls /save without a BOM (the byte pattern flips to the other endianness and the length is odd)
        for enc in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=enc):
                with self.assertRaises(odoo_rpc.GuardError):
                    read_through_guard(CLEAN.encode(enc)[1:])

    def test_a_nul_heavy_bomless_file_with_an_invalid_surrogate_is_unreadable(self):
        # 0.1.3: icacls /save without a BOM
        cases = {"LE high surrogate first": ("\ud800" + CLEAN, "utf-16-le"),
                 "LE high surrogate last": (CLEAN + "\ud800", "utf-16-le"),
                 "LE lone low surrogate": (CLEAN + "\udc00", "utf-16-le"),
                 "BE high surrogate first": ("\ud800" + CLEAN, "utf-16-be"),
                 "BE lone low surrogate in the middle": (CLEAN[:20] + "\udc00" + CLEAN[20:], "utf-16-be")}
        for label, (text_in, enc) in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    odoo_rpc._read_sddl("c.ini", run=FakeRun(text_in.encode(enc, "surrogatepass")))
                self.assertIn("unreadable icacls output", str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)

    def test_a_hostile_half_nul_buffer_is_not_decoded_as_utf16(self):
        # 0.1.3: icacls /save without a BOM (an attacker-shaped buffer: part UTF-16LE, part ASCII)
        # Updated ON PURPOSE round 2: not decoded as UTF-16 AND no longer read as UTF-8 either: the NUL refuses it.
        half = "D:PAI(A;;FA;;;SY)".encode("utf-16-le") + "(A;;FA;;;WD)".encode()
        with self.assertRaises(UnicodeDecodeError) as caught:
            odoo_rpc._decode_icacls(half)
        self.assertEqual(caught.exception.encoding, "utf-8")
        with self.assertRaises(odoo_rpc.GuardError) as refused:
            read_through_guard(half)
        self.assertIn("unreadable icacls output", str(refused.exception))
        every_other = bytes([65, 0] * 10 + [65, 65] * 10)
        with self.assertRaises(UnicodeDecodeError):
            odoo_rpc._decode_icacls(every_other)

    def test_a_bomless_utf16_text_is_not_decoded_as_utf8_and_vice_versa(self):
        # 0.1.3: icacls /save without a BOM (the decoded text differs per branch: no branch is a no-op)
        self.assertNotEqual(CLEAN.encode("utf-16-le").decode("utf-8", "replace"), CLEAN)
        self.assertNotEqual(CLEAN.encode("utf-16-le").decode("utf-16-be", "replace"), CLEAN)
        self.assertNotEqual(CLEAN.encode("utf-16-be").decode("utf-16-le", "replace"), CLEAN)
        self.assertEqual(odoo_rpc._decode_icacls(CLEAN.encode("utf-16-le")), CLEAN)
        self.assertEqual(odoo_rpc._decode_icacls(CLEAN.encode("utf-16-be")), CLEAN)

    def test_the_temp_folder_is_removed_after_a_bomless_read(self):
        # 0.1.3: icacls /save without a BOM
        run = FakeRun(CLEAN.encode("utf-16-le"))
        odoo_rpc._read_sddl("c.ini", run=run)
        self.assertTrue(run.folder_existed_during_call)
        self.assertFalse(os.path.exists(os.path.dirname(run.calls[0][0][3])))


def _bytes_with_nuls(pairs, odd_nuls, even_nuls):
    """`pairs` byte pairs; `odd_nuls` of the odd positions and `even_nuls` of the even positions are NUL, the rest are
    printable ASCII (even: A, odd: B), so each branch decodes to a different text."""
    raw = bytearray()
    for index in range(pairs):
        raw.append(0 if index < even_nuls else 0x41)
        raw.append(0 if index < odd_nuls else 0x42)
    return bytes(raw)


class DecodeIcaclsTests(unittest.TestCase):
    """0.1.3: icacls /save without a BOM: the byte-pattern decoder on its own."""

    def test_table_of_byte_patterns(self):
        # 0.1.3: icacls /save without a BOM
        table = [
            ("LE BOM + text", b"\xff\xfeD\x00:\x00", "D:"),
            ("BE BOM + text", b"\xfe\xff\x00D\x00:", "D:"),
            ("LE BOM only", b"\xff\xfe", ""),
            ("BE BOM only", b"\xfe\xff", ""),
            ("LE no BOM, 2 chars", b"D\x00:\x00", "D:"),
            ("BE no BOM, 2 chars", b"\x00D\x00:", "D:"),
            ("LE no BOM, 3 chars", b"D\x00:\x00(\x00", "D:("),
            ("BE no BOM, 3 chars", b"\x00D\x00:\x00(", "D:("),
            ("LE no BOM, one non-ASCII char among 20", ("D:PAI(A;;FA;;;SY)" + "\u1ec7" + "ab").encode("utf-16-le"),
             "D:PAI(A;;FA;;;SY)\u1ec7ab"),
            ("BE no BOM, one non-ASCII char among 20", ("D:PAI(A;;FA;;;SY)" + "\u1ec7" + "ab").encode("utf-16-be"),
             "D:PAI(A;;FA;;;SY)\u1ec7ab"),
            ("LE no BOM CRLF", "a\r\nb\r\n".encode("utf-16-le"), "a\r\nb\r\n"),
            ("BE no BOM CRLF", "a\r\nb\r\n".encode("utf-16-be"), "a\r\nb\r\n"),
            ("UTF-8 ASCII", b"D:PAI(A;;FA;;;SY)", "D:PAI(A;;FA;;;SY)"),
            ("UTF-8 four bytes", b"D:PA", "D:PA"),
            ("UTF-8 Vietnamese", "Nguy\u1ec5n V\u0103n An".encode("utf-8"), "Nguy\u1ec5n V\u0103n An"),
            ("UTF-8 with its own BOM", b"\xef\xbb\xbfD:PA", "\ufeffD:PA"),
            ("invalid UTF-8 replaced", b"\xff\xffD:PA", "\ufffd\ufffdD:PA"),
            ("empty", b"", ""),
            ("one byte", b"A", "A"),
            # Updated ON PURPOSE round 2: a NUL that no branch recognised is no longer read as UTF-8, it is refused
            # (UnicodeDecodeError, which `_read_sddl` turns into the GuardError "unreadable icacls output").
            ("two bytes, LE looking (below the 4-byte minimum)", b"A\x00", UnicodeDecodeError),
            ("two bytes, BE looking (below the 4-byte minimum)", b"\x00A", UnicodeDecodeError),
            ("three bytes, LE looking (below the 4-byte minimum)", b"A\x00B", UnicodeDecodeError),
            ("three bytes, BE looking (below the 4-byte minimum)", b"\x00A\x00", UnicodeDecodeError),
            ("all NUL four bytes", b"\x00\x00\x00\x00", UnicodeDecodeError),
            ("all NUL, 20 bytes", b"\x00" * 20, UnicodeDecodeError),
            ("half NUL mixed", bytes([65, 0] * 10 + [65, 65] * 10), UnicodeDecodeError),
            ("NUL at one position only", b"abc\x00defg", UnicodeDecodeError),
            ("four bytes LE, one char NUL-high and one not", b"A\x00BC", UnicodeDecodeError),
            ("UTF-8 text with a NUL first", b"\x00D:PAI(A;;FA;;;SY)", UnicodeDecodeError),
            ("UTF-8 text with a NUL last", b"D:PAI(A;;FA;;;SY)\x00", UnicodeDecodeError),
        ]
        self.assertGreaterEqual(len(table), 30)
        for label, raw, expected in table:
            with self.subTest(pattern=label):
                if expected is UnicodeDecodeError:
                    with self.assertRaises(UnicodeDecodeError):
                        odoo_rpc._decode_icacls(raw)
                else:
                    self.assertEqual(odoo_rpc._decode_icacls(raw), expected)

    def test_patterns_that_cannot_be_decoded_raise_unicode_decode_error(self):
        # 0.1.3: icacls /save without a BOM (the caller turns it into the GuardError)
        for label, raw in (("BOM + odd length", b"\xff\xfeD"), ("BE BOM + odd length", b"\xfe\xff\x00"),
                           ("LE no BOM odd length", "D:PA".encode("utf-16-le") + b"X"),
                           ("BE no BOM odd length", b"\x00D\x00:\x00"),
                           ("LE lone high surrogate", (CLEAN + "\ud800").encode("utf-16-le", "surrogatepass")),
                           ("BE lone high surrogate", (CLEAN + "\ud800").encode("utf-16-be", "surrogatepass"))):
            with self.subTest(pattern=label):
                with self.assertRaises(UnicodeDecodeError):
                    odoo_rpc._decode_icacls(raw)

    def test_the_thresholds_at_the_boundary_exactly_90_and_10_percent(self):
        # 0.1.3: icacls /save without a BOM. LE needs odd NULs >= 90 % and even NULs <= 10 %; BE is the mirror.
        # Updated ON PURPOSE round 2: every buffer is shorter than the 128-byte window (the tail is the whole buffer);
        # "utf8" now means "no branch": a buffer without a NUL is UTF-8, one WITH a NUL is refused ("reject").
        for pairs in (10, 20, 40):
            hi, lo = pairs * 9 // 10, pairs // 10
            cases = [(pairs, 0, "le"), (hi, 0, "le"), (hi - 1, 0, "reject"), (pairs, lo, "le"), (hi, lo, "le"),
                     (pairs, lo + 1, "reject"), (hi, lo + 1, "reject"), (hi - 1, lo, "reject"), (0, 0, "utf8"),
                     (pairs, pairs, "reject"), (pairs // 2, pairs // 2, "reject"),
                     (0, pairs, "be"), (0, hi, "be"), (0, hi - 1, "reject"), (lo, pairs, "be"), (lo, hi, "be"),
                     (lo + 1, pairs, "reject"), (lo + 1, hi, "reject"), (lo, hi - 1, "reject")]
            for odd_nuls, even_nuls, expected in cases:
                with self.subTest(pairs=pairs, odd_nuls=odd_nuls, even_nuls=even_nuls):
                    raw = _bytes_with_nuls(pairs, odd_nuls, even_nuls)
                    branches = {"le": raw.decode("utf-16-le"), "be": raw.decode("utf-16-be"),
                                "utf8": raw.decode("utf-8", "replace")}
                    if expected == "reject":
                        with self.assertRaises(UnicodeDecodeError):
                            odoo_rpc._decode_icacls(raw)
                        continue
                    for name, text in branches.items():
                        if name != expected:
                            self.assertNotEqual(text, branches[expected], name)
                    self.assertEqual(odoo_rpc._decode_icacls(raw), branches[expected])

    def test_the_length_floor_is_exactly_four_bytes(self):
        # 0.1.3: icacls /save without a BOM
        # Updated ON PURPOSE round 2: below 4 bytes nothing is guessed, so a NUL goes to the UTF-8 fallback, which now
        # refuses it; the `.encoding` of the error tells the UTF-8 refusal from a UTF-16 truncation (a floor of 3 or no
        # floor would have tried UTF-16 first: "utf-16-le" / a decoded text instead).
        for raw in (b"A\x00", b"\x00A", b"A\x00B", b"\x00A\x00"):
            with self.subTest(raw=raw):
                with self.assertRaises(UnicodeDecodeError) as caught:
                    odoo_rpc._decode_icacls(raw)
                self.assertEqual(caught.exception.encoding, "utf-8")
                self.assertIn("NUL", caught.exception.reason)
        self.assertEqual(odoo_rpc._decode_icacls(b"ABC"), "ABC")                           # 3 bytes without a NUL
        self.assertEqual(odoo_rpc._decode_icacls(b"A\x00B\x00"), "AB")                     # 4 bytes: LE
        self.assertEqual(odoo_rpc._decode_icacls(b"\x00A\x00B"), "AB")                     # 4 bytes: BE

    def test_the_bom_wins_over_the_byte_pattern(self):
        # 0.1.3: icacls /save without a BOM (a BOM is honoured even when the pattern would say the other endianness)
        self.assertEqual(odoo_rpc._decode_icacls(b"\xff\xfeA\x00B\x00"), "AB")
        self.assertEqual(odoo_rpc._decode_icacls(b"\xfe\xff\x00A\x00B"), "AB")
        self.assertEqual(odoo_rpc._decode_icacls(b"\xff\xfe" + b"\x00A" * 5), chr(0x4100) * 5)


def ascii_units(text, order):
    """UTF-16 text whose code units carry two ASCII characters each: encoded in `order` ("le" / "be") its bytes are
    exactly the ASCII characters of `text` (even length). E.g. U+3A44 is the two bytes `D:` in little endian."""
    pairs = zip(text[0::2], text[1::2])
    return "".join(chr(ord(a) | ord(b) << 8) if order == "le" else chr(ord(a) << 8 | ord(b)) for a, b in pairs)


def crafted_name(units, order):
    """A first line (file name) made of `units` code units that spell `D:(A;;FA;;;SY)` over and over in the byte stream."""
    fake = ("D:(A;;FA;;;SY)" * 40)[:2 * units]
    return ascii_units(fake, order)


BROAD_SHORT = sddl_of("A;;FA;;;SY", "A;;FA;;;WD")                                     # 62 bytes with a CRLF
BROAD_LONG = sddl_of("A;;FA;;;SY", "A;;FA;;;BA", "A;;FA;;;%s" % USER, "A;;FA;;;WD")   # 142 bytes: fills the window
ENCODINGS = (("le", "utf-16-le"), ("be", "utf-16-be"))


def with_bom(text, order):
    return text.encode("utf-16") if order == "le" else b"\xfe\xff" + text.encode("utf-16-be")


class CraftedNameAttackTests(IsolatedTestCase):
    """Round 2 (reviewer finding): real UTF-16 without a BOM whose FIRST line is dominated by code units whose two bytes
    are ASCII spells a fake `D:(A;;FA;;;SY)` line in the byte stream. Read as UTF-8 (round 1 did, because the whole file
    was measured) it hid a real wide-open entry. The same bytes with a BOM were always refused."""

    def test_a_crafted_name_with_a_broad_dacl_is_refused_with_and_without_a_bom(self):
        # Round 2: the attack, end to end, never accepted (a broad entry long enough to fill the window is named)
        for order, enc in ENCODINGS:
            for units in (8, 15, 16, 24, 42, 64):
                text_in = crafted_name(units, order) + "\r\n" + BROAD_LONG + "\r\n"
                with self.subTest(order=order, units=units, bom=False):
                    with self.assertRaises(odoo_rpc.GuardError) as caught:
                        read_through_guard(text_in.encode(enc))
                    self.assertIn("accessible by WD", str(caught.exception))
                with self.subTest(order=order, units=units, bom=True):
                    with self.assertRaises(odoo_rpc.GuardError) as caught:
                        read_through_guard(with_bom(text_in, order))
                    self.assertIn("accessible by WD", str(caught.exception))

    def test_a_crafted_name_with_a_short_broad_dacl_is_never_accepted(self):
        # Round 2: a DACL shorter than the window cannot be recognised behind a long name: refused, whatever the reason
        for order, enc in ENCODINGS:
            for units in (16, 24, 42, 64):
                with self.subTest(order=order, units=units):
                    text_in = crafted_name(units, order) + "\r\n" + BROAD_SHORT + "\r\n"
                    with self.assertRaises(odoo_rpc.GuardError):
                        read_through_guard(text_in.encode(enc))

    def test_the_crafted_bytes_do_spell_a_fake_line_so_the_attack_premise_holds(self):
        # Round 2: guards the guard: the fixture must really contain the fake `D:(...)` line in its byte stream
        for order, enc in ENCODINGS:
            raw = (crafted_name(16, order) + "\r\n" + BROAD_LONG).encode(enc)
            self.assertIn(b"D:(A;;FA;;;SY)D:(A;;FA;;;SY)", raw)
            self.assertIn(b"D:(A;;FA;;;SY)", raw.split(b"\r")[0])

    def test_a_crafted_name_with_a_clean_dacl_is_decoded_as_utf16_and_accepted(self):
        # Round 2: the tail rule measures the DACL line, so the long name no longer hides UTF-16; the fake line is absent
        for order, enc in ENCODINGS:
            for units in (15, 16, 24, 42, 64, 100):
                text_in = crafted_name(units, order) + "\r\n" + CLEAN + "\r\n"
                with self.subTest(order=order, units=units):
                    text = odoo_rpc._read_sddl("c.ini", run=FakeRun(text_in.encode(enc)))
                    self.assertEqual(text, text_in)
                    self.assertEqual(text.count("D:PAI("), 1)              # the real DACL only ...
                    self.assertNotIn("D:(", text)                          # ... the fake line is not in the text
                    self.assertNotIn("\x00", text)
                    self.assertNotIn("SY)D:(", text)
                    self.assertIsNone(read_through_guard(text_in.encode(enc)))

    def test_a_long_vietnamese_or_korean_first_line_with_a_clean_dacl_is_accepted_without_a_bom(self):
        # Round 2: invented names; both endiannesses; the name is far longer than the 128-byte window
        names = {"Vietnamese": VI_HEAVY_LINE * 3,
                 "Korean": ("\ud55c\uad6d\uc5b4_\uad6c\uc131_\ud30c\uc77c_\uacbd\ub85c_" * 12) + "\r\n",
                 "Japanese": ("\u69cb\u6210\u30d5\u30a1\u30a4\u30eb_" * 20) + "\r\n"}
        for label, name in names.items():
            for order, enc in ENCODINGS:
                with self.subTest(name=label, order=order):
                    self.assertGreater(len(name.encode("utf-16-le")), 200)
                    text_in = name + CLEAN + "\r\n"
                    self.assertEqual(odoo_rpc._read_sddl("c.ini", run=FakeRun(text_in.encode(enc))), text_in)
                    self.assertIsNone(read_through_guard(text_in.encode(enc)))

    def test_a_long_non_latin_first_line_with_a_broad_dacl_is_refused(self):
        # Round 2: the improvement never opens a way in: the same names with a wide-open entry are refused
        for order, enc in ENCODINGS:
            for name in (VI_HEAVY_LINE * 3, "\ud55c\uad6d\uc5b4_" * 40 + "\r\n"):
                with self.subTest(order=order, name=name[:4]):
                    with self.assertRaises(odoo_rpc.GuardError) as caught:
                        read_through_guard((name + BROAD_LONG).encode(enc))
                    self.assertIn("accessible by WD", str(caught.exception))

    def test_a_dacl_shorter_than_the_window_behind_a_non_latin_name_is_refused_as_unreadable(self):
        # Round 2: pins the real behaviour: fail closed (a false refusal), the BOM form of the same file is accepted
        for order, enc in ENCODINGS:
            for name in (VI_HEAVY_LINE, crafted_name(40, order) + "\r\n", "\ud55c\uad6d\uc5b4_" * 30 + "\r\n"):
                with self.subTest(order=order, name=name[:4]):
                    text_in = name + "D:PAI(A;;FA;;;SY)" + "\r\n"
                    with self.assertRaises(odoo_rpc.GuardError) as caught:
                        read_through_guard(text_in.encode(enc))
                    self.assertIn("unreadable icacls output", str(caught.exception))
                    self.assertIsNone(read_through_guard(with_bom(text_in, order)))

    def test_utf8_text_with_a_nul_anywhere_is_unreadable(self):
        # Round 2: UTF-8 never holds a NUL; one NUL in an otherwise UTF-8 file (not recognised as UTF-16) refuses it
        base = (VI_NAME_LINE + CLEAN + "\r\n").encode("utf-8")
        long_text = (VI_HEAVY_LINE * 2 + CLEAN).encode("utf-8")
        self.assertGreater(len(long_text), 300)
        cases = {"first byte": b"\x00" + base, "middle": base[:30] + b"\x00" + base[30:], "last byte": base + b"\x00",
                 "outside the tail window": b"\x00" + long_text, "plain ASCII": CLEAN.encode()[:20] + b"\x00" + CLEAN.encode()}
        for label, raw in cases.items():
            with self.subTest(nul=label):
                with self.assertRaises(odoo_rpc.GuardError) as caught:
                    odoo_rpc._read_sddl("c.ini", run=FakeRun(raw))
                self.assertIn("unreadable icacls output", str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                with self.assertRaises(UnicodeDecodeError) as raised:
                    odoo_rpc._decode_icacls(raw)
                self.assertEqual(raised.exception.encoding, "utf-8")


def poison_raw(poison, units, order, stray=b""):
    """`units` UTF-16 code units: `poison` units U+4141 (bytes AA, no NUL) first, then ASCII text; bytes in `order`."""
    body = ("D:PAI(A;;FA;;;SY)" * 40)[:units - poison]
    return (chr(0x4141) * poison + body).encode("utf-16-" + order) + stray


class TailWindowTests(unittest.TestCase):
    """Round 2: the byte pattern is measured on the last TAIL_BYTES (128) bytes only, the start kept even."""

    def test_the_window_is_128_bytes_so_the_unit_at_its_edge_decides(self):
        # Round 2: 7 poison units first. In a 64-unit window 58 odd NULs are needed (90 %): 6 poison units may sit in
        # the window, 7 may not. Total 130 bytes drops the first unit from the window, 128 bytes keeps all of it.
        self.assertEqual(odoo_rpc.TAIL_BYTES, 128)
        for order in ("le", "be"):
            for units, expected in ((63, "reject"), (64, "reject"), (65, "accept"), (66, "accept"), (67, "accept")):
                with self.subTest(order=order, units=units):
                    raw = poison_raw(7, units, order)
                    self.assertEqual(len(raw), 2 * units)
                    if expected == "reject":
                        with self.assertRaises(UnicodeDecodeError):
                            odoo_rpc._decode_icacls(raw)
                    else:
                        self.assertEqual(odoo_rpc._decode_icacls(raw), raw.decode("utf-16-" + order))

    def test_six_poison_units_in_a_whole_64_unit_buffer_are_still_utf16(self):
        # Round 2: the other side of the 90 % edge for the window of exactly 128 bytes
        for order in ("le", "be"):
            with self.subTest(order=order):
                raw = poison_raw(6, 64, order)
                self.assertEqual(odoo_rpc._decode_icacls(raw), raw.decode("utf-16-" + order))

    def test_odd_total_length_reports_the_endianness_of_the_utf16_it_recognised(self):
        # Round 2 (start parity): an odd total makes `len - 128` odd; the start must move to an even offset or the even
        # and odd positions swap and the OTHER endianness is tried. The error names the encoding that was tried.
        for order in ("le", "be"):
            for units in (63, 64, 65, 66, 70, 150, 151, 300):
                with self.subTest(order=order, units=units):
                    raw = poison_raw(0, units, order, stray=b"X")
                    self.assertEqual(len(raw) % 2, 1)
                    with self.assertRaises(UnicodeDecodeError) as caught:
                        odoo_rpc._decode_icacls(raw)
                    self.assertEqual(caught.exception.encoding, "utf-16-" + order)

    def test_odd_total_length_is_unreadable_through_read_sddl(self):
        # Round 2: never a crash, never half a DACL accepted
        for order, enc in ENCODINGS:
            for text in (CLEAN, "config.ini\r\n" + CLEAN + "\r\n", VI_HEAVY_LINE + CLEAN):
                with self.subTest(order=order, text=text[:8]):
                    raw = text.encode(enc) + b"X"
                    with self.assertRaises(odoo_rpc.GuardError) as caught:
                        odoo_rpc._read_sddl("c.ini", run=FakeRun(raw))
                    self.assertIn("unreadable icacls output", str(caught.exception))

    def test_the_window_edge_at_127_128_129_and_130_bytes(self):
        # Round 2: all-ASCII UTF-16 of 127 / 128 / 129 / 130 bytes (odd totals are a truncated unit: unreadable)
        for order in ("le", "be"):
            for length in (127, 128, 129, 130):
                with self.subTest(order=order, length=length):
                    raw = poison_raw(0, 65, order)[:length]
                    if length % 2:
                        with self.assertRaises(UnicodeDecodeError) as caught:
                            odoo_rpc._decode_icacls(raw)
                        self.assertEqual(caught.exception.encoding, "utf-16-" + order)
                    else:
                        self.assertEqual(odoo_rpc._decode_icacls(raw), raw.decode("utf-16-" + order))

    def test_the_thresholds_are_measured_on_the_tail_whatever_the_head_is(self):
        # Round 2: a 200-byte head without any NUL, then a 128-byte tail (64 units) with chosen NUL counts. 90 % of 64
        # is 57.6 -> 58 odd NULs needed; 10 % is 6.4 -> at most 6 even NULs.
        head = b"AB" * 100
        cases = [(64, 0, "le"), (58, 0, "le"), (58, 6, "le"), (64, 6, "le"), (57, 0, "reject"), (58, 7, "reject"),
                 (64, 7, "reject"), (57, 6, "reject"), (0, 64, "be"), (0, 58, "be"), (6, 58, "be"), (6, 64, "be"),
                 (0, 57, "reject"), (7, 58, "reject"), (7, 64, "reject"), (6, 57, "reject")]
        for odd_nuls, even_nuls, expected in cases:
            with self.subTest(odd_nuls=odd_nuls, even_nuls=even_nuls):
                raw = head + _bytes_with_nuls(64, odd_nuls, even_nuls)
                if expected == "reject":
                    with self.assertRaises(UnicodeDecodeError):
                        odoo_rpc._decode_icacls(raw)
                else:
                    self.assertEqual(odoo_rpc._decode_icacls(raw), raw.decode("utf-16-" + expected))

    def test_a_head_that_is_not_in_the_window_cannot_pull_the_decision_either_way(self):
        # Round 2: a NUL-free head with a UTF-8 tail stays UTF-8; ASCII UTF-8 with a CJK-like head is not UTF-16
        raw = ("\u4141" * 100).encode("utf-16-le") + CLEAN.encode()
        with self.assertRaises(UnicodeDecodeError) as caught:
            odoo_rpc._decode_icacls(b"\x00" + raw)
        self.assertEqual(caught.exception.encoding, "utf-8")
        text = odoo_rpc._decode_icacls(b"A" * 300 + CLEAN.encode())
        self.assertTrue(text.endswith(CLEAN))


STORE_EXE = ("C:\\Users\\qa\\AppData\\Local\\Microsoft\\WindowsApps\\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0"
             "\\python.exe")
NORMAL_EXE = "C:\\Users\\qa\\AppData\\Local\\Programs\\Python\\Python312\\python.exe"


class StorePythonHintTests(IsolatedTestCase):
    """Round 2: a hint when the running Python is the Microsoft Store build (it redirects %APPDATA%)."""

    def test_hint_table(self):
        store = odoo_rpc.STORE_PYTHON_HINT
        table = [
            ("WindowsApps + PythonSoftwareFoundation, backslashes", STORE_EXE, store),
            ("WindowsApps alone", "C:\\Users\\qa\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe", store),
            ("WindowsApps alone, forward slashes", "C:/Users/qa/AppData/Local/Microsoft/WindowsApps/python3.exe", store),
            ("mixed case", "c:\\USERS\\qa\\appdata\\local\\microsoft\\WINDOWSAPPS\\python3.exe", store),
            ("mixed slashes", "C:\\Users/qa\\AppData/Local\\Microsoft/WindowsApps\\python.exe", store),
            ("Program Files WindowsApps", "C:\\Program Files\\WindowsApps\\Foo.Python_1\\python.exe", store),
            ("PythonSoftwareFoundation alone, LocalCache path",
             "C:\\Users\\qa\\AppData\\Local\\Packages\\PythonSoftwareFoundation.Python.3.12_x\\LocalCache\\python.exe", store),
            ("PythonSoftwareFoundation lower case", "d:\\pythonsoftwarefoundation\\python.exe", store),
            ("root WindowsApps folder", "C:\\WindowsApps\\python.exe", store),
            ("python.org per-user build", NORMAL_EXE, ""),
            ("python.org all-users build", "C:\\Program Files\\Python312\\python.exe", ""),
            ("python.org short path", "C:\\Python312\\python.exe", ""),
            ("a POSIX path", "/usr/bin/python3", ""),
            ("a similar folder name", "C:\\Users\\qa\\WindowsApps2\\python.exe", ""),
            ("a longer folder name", "C:\\Users\\qa\\mywindowsapps\\python.exe", ""),
            ("an empty path", "", ""),
        ]
        self.assertGreaterEqual(len(table), 15)
        for label, executable, expected in table:
            with self.subTest(path=label):
                self.assertEqual(odoo_rpc._store_python_hint(executable), expected)

    def test_the_hint_names_the_store_build_and_the_way_out(self):
        for word in ("Microsoft Store", "%APPDATA%", "python.org", "py"):
            self.assertIn(word, odoo_rpc.STORE_PYTHON_HINT)
        self.assertTrue(odoo_rpc.STORE_PYTHON_HINT.startswith(" ("))

    def test_the_hint_is_hedged_it_does_not_state_the_redirect_as_a_verified_fact(self):
        # round 3: reviewer 🟡 (the redirect is not verified on a real machine; pin the hedge and the check command)
        self.assertIn("expected to redirect", odoo_rpc.STORE_PYTHON_HINT)
        self.assertIn("py -0p", odoo_rpc.STORE_PYTHON_HINT)
        self.assertNotIn("which redirects", odoo_rpc.STORE_PYTHON_HINT)

    def test_an_odd_sys_executable_never_raises_from_the_hint(self):
        # round 3: reviewer 🟡 (sys.executable can be None or empty when Python cannot tell; bytes or an object are
        # defensive): the helper returns a str and never raises, so it cannot mask the real GuardError
        odd = {"None": None, "empty": "", "bytes": b"x:\\windowsapps\\python.exe", "object": object(),
               "namespace": types.SimpleNamespace(path="x"), "an int": 0}
        for label, value in odd.items():
            with self.subTest(sys_executable=label):
                with mock.patch.object(odoo_rpc.sys, "executable", value):
                    hint = odoo_rpc._store_python_hint()
                self.assertIsInstance(hint, str)
                self.assertIn(hint, ("", odoo_rpc.STORE_PYTHON_HINT))
        for value in (None, "", 0):
            with self.subTest(sys_executable=value):
                with mock.patch.object(odoo_rpc.sys, "executable", value):
                    self.assertEqual(odoo_rpc._store_python_hint(), "")
        with mock.patch.object(odoo_rpc.sys, "executable", None):
            self.assertEqual(odoo_rpc._store_python_hint(None), "")
            self.assertEqual(odoo_rpc._store_python_hint(STORE_EXE), odoo_rpc.STORE_PYTHON_HINT)

    def test_a_missing_sys_executable_leaves_the_real_guard_error_unmasked(self):
        # round 3: reviewer 🟡 (an AttributeError / TypeError from the hint must not replace the refusal)
        for value in (None, "", b"", 0):
            for label, sddl_fn in (("no permission entry", lambda p: ""),
                                   ("icacls failed", lambda p: odoo_rpc._read_sddl(
                                       p, run=FakeRun(CLEAN.encode("utf-16"), returncode=5)))):
                with self.subTest(sys_executable=value, error=label):
                    with mock.patch.object(odoo_rpc.sys, "executable", value):
                        with self.assertRaises(odoo_rpc.GuardError) as caught:
                            self.run_guard(sddl_fn)
                    self.assertNotIn("Microsoft Store", str(caught.exception))
                    self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK), str(caught.exception))
        with mock.patch.object(odoo_rpc.sys, "executable", None):
            with self.assertRaises(odoo_rpc.GuardError) as caught:
                odoo_rpc.check_private_file(CONFIG_PATH, os_name="nt", sddl=lambda p: "", user_sid=USER)
        self.assertIn("could not read any permission entry", str(caught.exception))
        self.assertNotIn("Microsoft Store", str(caught.exception))

    def test_none_uses_the_running_interpreter_and_an_empty_string_does_not(self):
        with mock.patch.object(odoo_rpc.sys, "executable", STORE_EXE):
            self.assertEqual(odoo_rpc._store_python_hint(), odoo_rpc.STORE_PYTHON_HINT)
            self.assertEqual(odoo_rpc._store_python_hint(None), odoo_rpc.STORE_PYTHON_HINT)
            self.assertEqual(odoo_rpc._store_python_hint(""), "")
            self.assertEqual(odoo_rpc._store_python_hint(NORMAL_EXE), "")
        with mock.patch.object(odoo_rpc.sys, "executable", NORMAL_EXE):
            self.assertEqual(odoo_rpc._store_python_hint(), "")
            self.assertEqual(odoo_rpc._store_python_hint(STORE_EXE), odoo_rpc.STORE_PYTHON_HINT)
        with mock.patch.object(odoo_rpc.sys, "executable", ""):
            self.assertEqual(odoo_rpc._store_python_hint(), "")

    def failures(self):
        """{label: callable that raises a GuardError} for the 'could not read' family and for the other ones."""
        icacls_failed = FakeRun(CLEAN.encode("utf-16"), returncode=5)
        icacls_missing = FakeRun(raises=FileNotFoundError("icacls"))
        unreadable = FakeRun(b"\xff\xfeD")
        read = {
            "icacls failed": lambda p: odoo_rpc._read_sddl(p, run=icacls_failed),
            "icacls cannot run": lambda p: odoo_rpc._read_sddl(p, run=icacls_missing),
            "unreadable icacls output": lambda p: odoo_rpc._read_sddl(p, run=unreadable),
            "no permission entry": lambda p: "",
            "no permission entry (text without a DACL)": lambda p: "config.ini",
        }
        return read

    def run_guard(self, sddl_fn):
        return odoo_rpc.check_private_file(CONFIG_PATH, os_name="nt", sddl=sddl_fn, user_sid=USER)

    def test_the_hint_is_appended_to_the_could_not_read_errors_when_python_is_the_store_build(self):
        for label, sddl_fn in self.failures().items():
            with self.subTest(error=label):
                with mock.patch.object(odoo_rpc.sys, "executable", NORMAL_EXE):
                    with self.assertRaises(odoo_rpc.GuardError) as plain:
                        self.run_guard(sddl_fn)
                with mock.patch.object(odoo_rpc.sys, "executable", STORE_EXE):
                    with self.assertRaises(odoo_rpc.GuardError) as hinted:
                        self.run_guard(sddl_fn)
                self.assertEqual(str(hinted.exception), str(plain.exception) + odoo_rpc.STORE_PYTHON_HINT)
                self.assertTrue(str(hinted.exception).startswith(str(plain.exception)))
                self.assertNotIn("Microsoft Store", str(plain.exception))
                self.assertIsNone(hinted.exception.__cause__)
                self.assertTrue(hinted.exception.__suppress_context__)

    def test_a_normal_python_gets_the_error_unchanged(self):
        for label, sddl_fn in self.failures().items():
            with self.subTest(error=label):
                with mock.patch.object(odoo_rpc.sys, "executable", NORMAL_EXE):
                    with self.assertRaises(odoo_rpc.GuardError) as caught:
                        self.run_guard(sddl_fn)
                self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK), str(caught.exception))

    def test_a_real_refusal_never_gets_the_hint_even_on_the_store_build(self):
        refusals = {"Everyone": lambda p: sddl_of("A;;FA;;;SY", "A;;FR;;;WD"),
                    "Users": lambda p: sddl_of("A;;FA;;;SY", "A;;FA;;;BU"),
                    "another user": lambda p: sddl_of("A;;FA;;;SY", "A;;FA;;;S-1-5-21-1-2-3-1002")}
        with mock.patch.object(odoo_rpc.sys, "executable", STORE_EXE):
            for label, sddl_fn in refusals.items():
                with self.subTest(refusal=label):
                    with self.assertRaises(odoo_rpc.GuardError) as caught:
                        self.run_guard(sddl_fn)
                    self.assertIn("is accessible by", str(caught.exception))
                    self.assertNotIn("Microsoft Store", str(caught.exception))
                    self.assertTrue(str(caught.exception).endswith(odoo_rpc.WINDOWS_FALLBACK), str(caught.exception))

    def test_other_errors_get_no_hint_on_the_store_build(self):
        # the whoami failure mentions neither icacls nor a permission entry
        whoami = odoo_rpc.GuardError("cannot find the current user (whoami failed)" + odoo_rpc.WINDOWS_FALLBACK)
        with mock.patch.object(odoo_rpc.sys, "executable", STORE_EXE), \
                mock.patch.object(odoo_rpc, "_current_user_sid", side_effect=whoami):
            with self.assertRaises(odoo_rpc.GuardError) as caught:
                odoo_rpc.check_private_file(CONFIG_PATH, os_name="nt", sddl=lambda p: CLEAN)
        self.assertEqual(str(caught.exception), str(whoami))

    def test_a_clean_file_passes_on_the_store_build_too(self):
        with mock.patch.object(odoo_rpc.sys, "executable", STORE_EXE):
            self.assertIsNone(self.run_guard(lambda p: CLEAN))

    def test_the_posix_branch_never_gets_the_hint(self):
        stat = types.SimpleNamespace(st_mode=0o100644, st_uid=1000)
        with mock.patch.object(odoo_rpc.sys, "executable", STORE_EXE):
            with self.assertRaises(odoo_rpc.GuardError) as caught:
                odoo_rpc.check_private_file("/x/config.ini", os_name="posix", stat_fn=lambda p: stat, getuid=lambda: 1000)
        self.assertNotIn("Microsoft Store", str(caught.exception))


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
