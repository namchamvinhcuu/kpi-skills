"""scripts/kpi_catalogs.py: which source catalogs ship, and how a project name selects one.

Plan kpi-multi-project step 2a (AC4, AC5, D10-D12). A catalog is a trust root, so a project is chosen by NAME from the
files shipped in `catalogs/`; the name must be a member of `list_projects()` and the path is built from that listed entry.
Everything here runs against a temporary folder (`catalog_dir=` or `mock.patch.object(kpi_catalogs, "CATALOG_DIR", tmp)`,
D11) except the checks of the real shipped folder. The command-line tests run `main(argv)` IN PROCESS: a subprocess could
not see the patch.
"""
import fnmatch
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import reconcile_fixtures as rf
import scan_fixtures as sf
from helpers import REPO, IsolatedTestCase
from test_bind_cli import is_stdlib_module
from test_repo_hygiene import WORKFLOW_OUTPUT_PATTERNS, gitignore_patterns, is_ignored
from test_script_static import imported_names

import bind_kpi
import intake_xlsx
import kpi_catalogs
import odoo_rpc
import reconcile_kpi
import scan_excel

SHIPPED_DIR = os.path.join(str(REPO), "catalogs")
# Updated ON PURPOSE for 0.1.1: the move digest was 3328714b... (14566 bytes); the only change since is the two new `kind`
# alias texts of report_group ("Số lượng sản xuất" under produced, "Số lượng xuất hàng" under shipped).
DKSK_SHA256 = "2f63c92d2247895dd3718c5f0bd244421007602740f618f37ada6fa00a97cc72"
DKSK_BYTES = 14638
TYPED_TEXT = "zqxword"        # a text no message of the plugin may repeat: it stands for whatever the user typed


def can_symlink():
    with tempfile.TemporaryDirectory(prefix="kpi-link-") as folder:
        try:
            os.symlink(os.path.join(folder, "target"), os.path.join(folder, "link"))
        except (OSError, NotImplementedError, AttributeError):
            return False
    return True


SYMLINKS = unittest.skipUnless(can_symlink(), "this account cannot create symbolic links")


def touch(folder, name, data=b"{}"):
    with open(os.path.join(folder, name), "wb") as handle:
        handle.write(data)


class FolderCase(IsolatedTestCase):
    """A fresh temporary `catalogs` folder per test."""

    def setUp(self):
        super().setUp()
        self.catalogs = os.path.join(self.tmp, "catalogs")
        os.makedirs(self.catalogs)

    def fill(self, *names):
        for name in names:
            touch(self.catalogs, name + ".json")
        return self.catalogs


class CatalogErrorTests(unittest.TestCase):
    def test_it_is_its_own_exception_class_that_no_later_except_clause_swallows_by_accident(self):
        self.assertTrue(issubclass(kpi_catalogs.CatalogError, Exception))
        for other in (ValueError, OSError, KeyError, LookupError, TypeError):
            self.assertFalse(issubclass(kpi_catalogs.CatalogError, other), other)


class NameRuleTests(unittest.TestCase):
    def test_names_that_pass_the_rule(self):
        for name in ("dksk", "a", "0", "0abc", "a-b_c", "a" * 30, "x1", "mydashboard", "xbaseline", "alias", "dash",
                     "kpi", "console", "com0", "com10", "lpt0", "recon", "base"):
            with self.subTest(name=name):
                self.assertTrue(kpi_catalogs.valid_name(name))

    def test_the_rule_is_matched_against_the_whole_text_so_a_trailing_newline_fails(self):
        # `re.match` with a `$` would accept "dksk\n"; fullmatch must not
        for name in ("dksk\n", "dksk\n\n", "\ndksk", "dk\nsk", "dksk\r", "dksk\r\n", "dksk ", " dksk", "dk sk", "dksk\t"):
            with self.subTest(name=name):
                self.assertFalse(kpi_catalogs.valid_name(name))

    def test_names_the_rule_refuses(self):
        cases = {"empty": "", "uppercase": "DKSK", "mixed case": "Dksk", "dot": "dksk.json", "leading dot": ".dksk",
                 "parent folder": "../x", "dot dot": "..", "slash": "a/b", "backslash": "a\\b", "absolute": "/etc/passwd",
                 "drive": "C:\\x", "windows absolute": "\\\\host\\share", "NUL character": "a\x00b",
                 "only NUL": "\x00", "31 characters": "a" * 31, "100 characters": "a" * 100, "leading dash": "-a",
                 "leading underscore": "_a", "non ASCII letter": "dksé", "full width": "ｄｋｓｋ", "Kelvin sign": "\u212a",
                 "Arabic digit": "\u0663", "space inside": "a b", "colon": "a:b", "star": "a*", "question": "a?",
                 "tilde": "~", "percent": "%41", "dollar": "$HOME", "quote": "a'b", "semicolon": "a;b"}
        for label, name in cases.items():
            with self.subTest(case=label):
                self.assertFalse(kpi_catalogs.valid_name(name))

    def test_the_length_limit_is_thirty_characters(self):
        self.assertTrue(kpi_catalogs.valid_name("a" * 30))
        self.assertTrue(kpi_catalogs.valid_name("a" + "-" * 29))
        self.assertFalse(kpi_catalogs.valid_name("a" * 31))
        self.assertFalse(kpi_catalogs.valid_name("a" + "-" * 30))

    def test_a_value_that_is_not_text_is_refused_not_raised(self):
        for value in (None, 5, 0, 1.5, True, b"dksk", bytearray(b"dksk"), ["dksk"], ("dksk",), {"dksk"}, object()):
            with self.subTest(value=repr(value)):
                self.assertFalse(kpi_catalogs.valid_name(value))

    def test_windows_device_names_are_refused(self):
        names = ["con", "prn", "aux", "nul"] + ["com%d" % n for n in range(1, 10)] + ["lpt%d" % n for n in range(1, 10)]
        self.assertEqual(len(names), 22)
        for name in names:
            with self.subTest(name=name):
                self.assertFalse(kpi_catalogs.valid_name(name))
        self.assertEqual(set(names), set(kpi_catalogs.WINDOWS_DEVICE_NAMES))

    def test_names_that_start_like_a_gitignored_workflow_output_are_refused(self):
        for prefix in ("dashboard", "baseline", "kpi_spec", "reconcile"):
            for name in (prefix, prefix + "x", prefix + "-1", prefix + "_2024", prefix + "0"):
                with self.subTest(name=name):
                    self.assertFalse(kpi_catalogs.valid_name(name))
        self.assertEqual(set(kpi_catalogs.IGNORED_PREFIXES), {"dashboard", "baseline", "kpi_spec", "reconcile"})

    def test_a_prefix_is_only_refused_at_the_start(self):
        # `.gitignore` has `dashboard*.json`, not `*dashboard*.json`: a name that merely contains the word is fine
        for name in ("mydashboard", "my_baseline", "a-kpi_spec", "xreconcile"):
            with self.subTest(name=name):
                self.assertTrue(kpi_catalogs.valid_name(name))

    def test_names_containing_aliases_are_refused_anywhere(self):
        for name in ("aliases", "my_aliases", "aliasesx", "xaliasesx", "a-aliases-b", "dksk_aliases", "aliases1"):
            with self.subTest(name=name):
                self.assertFalse(kpi_catalogs.valid_name(name))


class ListProjectsTests(FolderCase):
    def test_it_returns_the_sorted_names_of_the_valid_catalog_files(self):
        self.fill("zz9", "beta", "alpha", "a-1_x", "0first")
        self.assertEqual(kpi_catalogs.list_projects(self.catalogs), ["0first", "a-1_x", "alpha", "beta", "zz9"])

    def test_the_order_does_not_depend_on_the_order_the_folder_is_read_in(self):
        self.fill("alpha", "beta", "gamma")
        for order in (["alpha.json", "beta.json", "gamma.json"], ["gamma.json", "beta.json", "alpha.json"],
                      ["beta.json", "gamma.json", "alpha.json"]):
            with self.subTest(order=order), mock.patch.object(os, "listdir", return_value=order):
                self.assertEqual(kpi_catalogs.list_projects(self.catalogs), ["alpha", "beta", "gamma"])

    def test_the_names_are_sorted_as_stems_not_as_file_names_regression(self):
        # Regression test for the step 2a finding: sorting `a-b.json`, `a.json`, `a_b.json` as FILE names gives
        # a-b, a, a_b (`-` sorts before `.`); the sorted STEMS are a, a-b, a_b
        self.fill("a", "a-b", "a_b")
        expected = ["a", "a-b", "a_b"]
        self.assertEqual(kpi_catalogs.list_projects(self.catalogs), expected)
        for order in (["a-b.json", "a.json", "a_b.json"], ["a_b.json", "a-b.json", "a.json"],
                      ["a_b.json", "a.json", "a-b.json"]):
            with self.subTest(order=order), mock.patch.object(os, "listdir", return_value=order):
                self.assertEqual(kpi_catalogs.list_projects(self.catalogs), expected)

    def test_everything_else_in_the_folder_is_ignored(self):
        self.fill("alpha")
        for name in ("notes.txt", ".json", "has.dot.json", "UPPER.json", "x" * 31 + ".json", "alpha.json.bak",
                     "alpha.JSON", "alpha.jsonx", "README.md", "alpha", "-lead.json", "a b.json", "dashboard.json",
                     "dashboard1.json", "baseline.json", "kpi_spec.json", "reconcile.json", "my_aliases.json",
                     "aliases.json", "con.json", "nul.json", "com1.json", "lpt9.json", "x.spec.json", "x.draft.json"):
            touch(self.catalogs, name)
        self.assertEqual(kpi_catalogs.list_projects(self.catalogs), ["alpha"])

    def test_a_file_name_with_a_newline_is_not_a_catalog(self):
        self.fill("alpha")
        try:
            touch(self.catalogs, "x\n.json")
            touch(self.catalogs, "alpha\n.json")
        except OSError:
            self.skipTest("this file system refuses a newline in a file name")
        self.assertEqual(kpi_catalogs.list_projects(self.catalogs), ["alpha"])

    def test_a_folder_named_like_a_catalog_is_not_a_catalog(self):
        self.fill("alpha")
        os.makedirs(os.path.join(self.catalogs, "x.json"))
        os.makedirs(os.path.join(self.catalogs, "y.json", "inner"))
        self.assertEqual(kpi_catalogs.list_projects(self.catalogs), ["alpha"])

    @SYMLINKS
    def test_a_symlink_is_not_a_catalog_whatever_it_points_at(self):
        self.fill("alpha")
        outside = os.path.join(self.tmp, "outside.json")
        touch(self.tmp, "outside.json")
        os.symlink(os.path.join(self.catalogs, "alpha.json"), os.path.join(self.catalogs, "linkfile.json"))
        os.symlink(outside, os.path.join(self.catalogs, "outside.json"))
        os.symlink(os.path.join(self.tmp, "nowhere.json"), os.path.join(self.catalogs, "dangling.json"))
        os.symlink(self.catalogs, os.path.join(self.catalogs, "linkdir.json"))
        self.assertEqual(kpi_catalogs.list_projects(self.catalogs), ["alpha"])

    def test_an_empty_folder_lists_nothing(self):
        self.assertEqual(kpi_catalogs.list_projects(self.catalogs), [])

    def test_a_missing_or_unreadable_folder_raises_catalog_error_never_os_error(self):
        not_a_folder = os.path.join(self.tmp, "file.txt")
        touch(self.tmp, "file.txt")
        for label, folder in (("missing", os.path.join(self.tmp, "nope")), ("a file", not_a_folder),
                              ("empty text", "")):
            with self.subTest(case=label):
                with self.assertRaises(kpi_catalogs.CatalogError) as raised:
                    kpi_catalogs.list_projects(folder)
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(raised.exception.__suppress_context__)
                self.assertNotIn(self.tmp, str(raised.exception))

    def test_a_permission_error_on_the_folder_is_a_catalog_error_too(self):
        with mock.patch.object(os, "listdir", side_effect=PermissionError(13, "denied", "/secret/path")):
            with self.assertRaises(kpi_catalogs.CatalogError) as raised:
                kpi_catalogs.list_projects(self.catalogs)
        self.assertNotIn("/secret/path", str(raised.exception))

    def test_the_default_folder_is_the_module_constant_read_at_call_time(self):
        self.fill("alpha", "beta")
        with mock.patch.object(kpi_catalogs, "CATALOG_DIR", self.catalogs):
            self.assertEqual(kpi_catalogs.list_projects(), ["alpha", "beta"])
        self.assertEqual(kpi_catalogs.list_projects(), kpi_catalogs.list_projects(SHIPPED_DIR))

    def test_an_explicit_folder_wins_over_the_constant(self):
        self.fill("alpha")
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        touch(other, "beta.json")
        with mock.patch.object(kpi_catalogs, "CATALOG_DIR", other):
            self.assertEqual(kpi_catalogs.list_projects(self.catalogs), ["alpha"])


class CatalogPathTests(FolderCase):
    def test_no_project_and_exactly_one_catalog_gives_that_file(self):
        self.fill("alpha")
        self.assertEqual(kpi_catalogs.catalog_path(catalog_dir=self.catalogs), os.path.join(self.catalogs, "alpha.json"))
        self.assertEqual(kpi_catalogs.catalog_path(None, self.catalogs), os.path.join(self.catalogs, "alpha.json"))

    def test_no_project_and_two_catalogs_is_refused_and_lists_the_names(self):
        self.fill("beta", "alpha")
        with self.assertRaises(kpi_catalogs.CatalogError) as raised:
            kpi_catalogs.catalog_path(catalog_dir=self.catalogs)
        self.assertIn("alpha, beta", str(raised.exception))
        self.assertIn("--project", str(raised.exception))

    def test_no_project_and_no_catalog_is_refused(self):
        with self.assertRaises(kpi_catalogs.CatalogError) as raised:
            kpi_catalogs.catalog_path(catalog_dir=self.catalogs)
        self.assertIn("no source catalog", str(raised.exception))

    def test_no_project_and_only_unusable_files_is_the_same_as_none(self):
        self.fill("dashboard", "Upper")
        touch(self.catalogs, "notes.txt")
        with self.assertRaises(kpi_catalogs.CatalogError):
            kpi_catalogs.catalog_path(catalog_dir=self.catalogs)

    def test_a_listed_project_gives_its_file(self):
        self.fill("alpha", "beta")
        self.assertEqual(kpi_catalogs.catalog_path("beta", self.catalogs), os.path.join(self.catalogs, "beta.json"))
        self.assertEqual(kpi_catalogs.catalog_path(project="alpha", catalog_dir=self.catalogs),
                         os.path.join(self.catalogs, "alpha.json"))

    def test_a_listed_project_does_not_need_to_be_the_only_one(self):
        self.fill("alpha")
        self.assertEqual(kpi_catalogs.catalog_path("alpha", self.catalogs), os.path.join(self.catalogs, "alpha.json"))

    def test_an_unknown_project_is_refused_and_the_installed_names_are_listed(self):
        self.fill("alpha", "beta")
        with self.assertRaises(kpi_catalogs.CatalogError) as raised:
            kpi_catalogs.catalog_path("gamma", self.catalogs)
        self.assertIn("alpha, beta", str(raised.exception))

    def test_membership_comes_first_a_typed_text_never_builds_a_path(self):
        # the files these names would reach exist: only the listing decides
        self.fill("alpha")
        touch(self.tmp, "x.json")
        touch(self.catalogs, "Upper.json")
        touch(self.catalogs, "dashboard.json")
        touch(self.catalogs, "has.dot.json")
        for name in ("../x", "..\\x", os.path.join(self.tmp, "x"), "Upper", "dashboard", "has.dot", "alpha/../alpha",
                     "./alpha", "alpha.json", "alpha\n", "alpha\x00", " alpha", "ALPHA", "alpha/", "a/../alpha"):
            with self.subTest(name=name):
                with self.assertRaises(kpi_catalogs.CatalogError):
                    kpi_catalogs.catalog_path(name, self.catalogs)

    @SYMLINKS
    def test_a_symlink_named_like_a_project_is_not_a_project(self):
        self.fill("alpha")
        os.symlink(os.path.join(self.catalogs, "alpha.json"), os.path.join(self.catalogs, "linked.json"))
        with self.assertRaises(kpi_catalogs.CatalogError):
            kpi_catalogs.catalog_path("linked", self.catalogs)

    def test_a_folder_named_like_a_project_is_not_a_project(self):
        self.fill("alpha")
        os.makedirs(os.path.join(self.catalogs, "folder.json"))
        with self.assertRaises(kpi_catalogs.CatalogError):
            kpi_catalogs.catalog_path("folder", self.catalogs)

    def test_a_value_that_is_not_text_is_refused_as_a_catalog_error(self):
        self.fill("alpha")
        for value in (5, 0, b"alpha", ["alpha"], ("alpha",), 1.5, True, object(), ""):
            with self.subTest(value=repr(value)):
                with self.assertRaises(kpi_catalogs.CatalogError):
                    kpi_catalogs.catalog_path(value, self.catalogs)

    def test_an_error_never_echoes_the_typed_value(self):
        self.fill("alpha", "beta")
        for typed in (TYPED_TEXT, TYPED_TEXT.upper(), "../" + TYPED_TEXT, TYPED_TEXT + "\n", "a/" + TYPED_TEXT, "a\\" + TYPED_TEXT,
                      "x" * 31 + TYPED_TEXT, TYPED_TEXT + "\x00", TYPED_TEXT + ".json", "." + TYPED_TEXT, "-" + TYPED_TEXT):
            with self.subTest(typed=repr(typed)):
                with self.assertRaises(kpi_catalogs.CatalogError) as raised:
                    kpi_catalogs.catalog_path(typed, self.catalogs)
                self.assertNotIn(TYPED_TEXT, str(raised.exception).lower())
                self.assertNotIn(TYPED_TEXT, repr(raised.exception).lower())
                self.assertIsNone(raised.exception.__cause__)

    def test_an_error_names_only_catalogs_that_can_be_chosen(self):
        self.fill("alpha")
        touch(self.catalogs, "dashboard.json")
        touch(self.catalogs, "Upper.json")
        with self.assertRaises(kpi_catalogs.CatalogError) as raised:
            kpi_catalogs.catalog_path("nope", self.catalogs)
        self.assertIn("installed: alpha)", str(raised.exception))
        self.assertNotIn("dashboard", str(raised.exception))
        self.assertNotIn("Upper", str(raised.exception))

    def test_an_unreadable_folder_is_a_catalog_error_for_every_way_of_asking(self):
        missing = os.path.join(self.tmp, "nope")
        for project in (None, "alpha", "../x", 5):
            with self.subTest(project=project):
                with self.assertRaises(kpi_catalogs.CatalogError):
                    kpi_catalogs.catalog_path(project, missing)

    def test_the_default_folder_is_the_module_constant_read_at_call_time(self):
        self.fill("alpha", "beta")
        with mock.patch.object(kpi_catalogs, "CATALOG_DIR", self.catalogs):
            self.assertEqual(kpi_catalogs.catalog_path("beta"), os.path.join(self.catalogs, "beta.json"))
            with self.assertRaises(kpi_catalogs.CatalogError) as raised:
                kpi_catalogs.catalog_path()
            self.assertIn("alpha, beta", str(raised.exception))
            with self.assertRaises(kpi_catalogs.CatalogError):
                kpi_catalogs.catalog_path("dksk")                   # a shipped name that this folder does not hold
        self.assertEqual(kpi_catalogs.catalog_path(), os.path.join(SHIPPED_DIR, "dksk.json"))

    def test_the_constant_points_at_the_catalogs_folder_next_to_scripts(self):
        self.assertEqual(os.path.realpath(kpi_catalogs.CATALOG_DIR), os.path.realpath(SHIPPED_DIR))
        self.assertTrue(os.path.isabs(kpi_catalogs.CATALOG_DIR))


class RealFolderTests(unittest.TestCase):
    """The folder that ships with the plugin. A second shipped catalog changes `test_dksk_is_the_only_catalog` on purpose."""

    def test_dksk_is_the_only_catalog(self):
        self.assertEqual(kpi_catalogs.list_projects(), ["dksk"])

    def test_every_file_in_the_folder_is_a_catalog_named_by_the_rule(self):
        entries = sorted(os.listdir(SHIPPED_DIR))
        self.assertTrue(entries)
        for entry in entries:
            with self.subTest(entry=entry):
                stem, extension = os.path.splitext(entry)
                self.assertEqual(extension, ".json")
                self.assertTrue(kpi_catalogs.valid_name(stem), stem)
                self.assertTrue(os.path.isfile(os.path.join(SHIPPED_DIR, entry)))
                self.assertFalse(os.path.islink(os.path.join(SHIPPED_DIR, entry)))
        # sorted by STEM: with `acme` and `acme-eu` the file names sort `acme-eu.json` before `acme.json`, the stems do not
        self.assertEqual(sorted(os.path.splitext(entry)[0] for entry in entries), kpi_catalogs.list_projects())

    def test_the_dksk_catalog_is_the_file_that_was_moved_from_scripts_byte_for_byte(self):
        """Migration pin: `catalogs/dksk.json` is the old `scripts/catalog.json` (sha256 taken before the move).
        A legitimate edit of the DKSK catalog updates DKSK_SHA256 ON PURPOSE, in the same change."""
        with open(os.path.join(SHIPPED_DIR, "dksk.json"), "rb") as handle:
            data = handle.read()
        self.assertEqual((len(data), hashlib.sha256(data).hexdigest()), (DKSK_BYTES, DKSK_SHA256))
        self.assertFalse(os.path.exists(os.path.join(str(REPO), "scripts", "catalog.json")))

    def test_every_shipped_catalog_is_committed_not_ignored_by_git(self):
        if not shutil.which("git") or not os.path.exists(os.path.join(str(REPO), ".git")):
            self.skipTest("needs git and a .git folder (the plugin may be checked out without one)")

        def ignored(path):
            done = subprocess.run(["git", "check-ignore", "--no-index", "-q", "--", path], cwd=str(REPO),
                                  capture_output=True, timeout=60)
            self.assertIn(done.returncode, (0, 1), done.stderr)
            return done.returncode == 0

        self.assertTrue(ignored("catalogs/dashboard.json"), "control: git must see the ignore rules")
        for name in kpi_catalogs.list_projects():
            with self.subTest(name=name):
                self.assertFalse(ignored("catalogs/%s.json" % name))

    def test_the_name_rule_refuses_one_name_per_gitignore_pattern_of_workflow_output(self):
        # drift guard: a new ignore pattern for workflow output is also a name that a catalog must not have
        probes = []
        for pattern in WORKFLOW_OUTPUT_PATTERNS:
            probe = pattern.replace("*", "x")
            self.assertTrue(probe.endswith(".json"), pattern)
            probes.append((pattern, probe[:-len(".json")]))
        self.assertEqual(len(probes), len(WORKFLOW_OUTPUT_PATTERNS))
        with open(os.path.join(str(REPO), ".gitignore"), encoding="utf-8") as handle:
            for pattern in gitignore_patterns(handle.read()):
                if pattern.endswith(".json") and "/" not in pattern and not pattern.startswith("!"):
                    probes.append((pattern, pattern.replace("*", "x")[:-len(".json")]))
        for pattern, probe in probes:
            with self.subTest(pattern=pattern, probe=probe):
                self.assertTrue(fnmatch.fnmatchcase(probe + ".json", pattern), "the probe must match its own pattern")
                self.assertFalse(kpi_catalogs.valid_name(probe))

    def test_no_name_the_rule_accepts_is_matched_by_a_gitignore_pattern(self):
        with open(os.path.join(str(REPO), ".gitignore"), encoding="utf-8") as handle:
            patterns = gitignore_patterns(handle.read())
        for name in ("dksk", "alpha", "a", "0", "a-b_c", "a" * 30, "mydashboard", "my_baseline", "xreconcile",
                     "kpi", "base", "dash", "alias", "spec", "draft", "config", "key"):
            with self.subTest(name=name):
                self.assertTrue(kpi_catalogs.valid_name(name))
                self.assertFalse(is_ignored(name + ".json", patterns))

    def test_the_module_imports_nothing_outside_the_standard_library(self):
        with open(os.path.join(str(REPO), "scripts", "kpi_catalogs.py"), encoding="utf-8") as handle:
            names = imported_names(handle.read())
        self.assertEqual({name for name in names if not is_stdlib_module(name)}, set())
        self.assertFalse({"socket", "urllib", "http", "ssl", "subprocess", "odoo_rpc", "bind_kpi", "intake_xlsx",
                          "validate_formulas", "scan_excel", "reconcile_kpi"} & names)

    def test_the_static_import_helper_would_see_a_sibling_import(self):
        self.assertEqual({n for n in imported_names("import os\nimport bind_kpi\nfrom odoo_rpc import x\n")
                          if not is_stdlib_module(n)}, {"bind_kpi", "odoo_rpc"})


def load_real():
    with open(os.path.join(SHIPPED_DIR, "dksk.json"), "rb") as handle:
        return handle.read()


class LoadCatalogTests(FolderCase):
    def setUp(self):
        super().setUp()
        self.real = load_real()
        touch(self.catalogs, "dksk.json", self.real)
        other = json.loads(self.real.decode("utf-8"))
        other["note"] = "the other project"
        touch(self.catalogs, "other.json", json.dumps(other).encode("utf-8"))

    def test_a_project_loads_the_shipped_catalog_of_that_name(self):
        self.assertEqual(bind_kpi.load_catalog(project="dksk"), json.loads(self.real.decode("utf-8")))

    def test_no_project_loads_the_only_catalog_that_ships(self):
        self.assertEqual(bind_kpi.load_catalog(), json.loads(self.real.decode("utf-8")))
        self.assertEqual(bind_kpi.load_catalog(None, None), bind_kpi.load_catalog(project="dksk"))

    def test_an_empty_path_means_no_explicit_path_and_selects_by_project(self):
        # `--catalog ""` is "not given": the guard is `if not path:` (a mutant `if path is None:` would open the file "")
        expected = bind_kpi.load_catalog(project="dksk")
        self.assertEqual(bind_kpi.load_catalog(""), expected)                     # one shipped catalog: the sole default
        self.assertEqual(bind_kpi.load_catalog("", project="dksk"), expected)
        self.assertEqual(bind_kpi.load_catalog(path="", project="dksk"), expected)

    def test_the_catalog_dir_keyword_reads_that_folder(self):
        self.assertEqual(bind_kpi.load_catalog(project="other", catalog_dir=self.catalogs)["note"], "the other project")
        self.assertNotEqual(bind_kpi.load_catalog(project="dksk", catalog_dir=self.catalogs)["note"], "the other project")

    def test_the_module_constant_is_read_when_the_function_runs(self):
        with mock.patch.object(kpi_catalogs, "CATALOG_DIR", self.catalogs):
            self.assertEqual(bind_kpi.load_catalog(project="other")["note"], "the other project")
            with self.assertRaises(bind_kpi.BindError):
                bind_kpi.load_catalog()                      # two catalogs and no project
        with self.assertRaises(bind_kpi.BindError):
            bind_kpi.load_catalog(project="other")           # the real folder has no such project

    def test_project_and_path_together_are_refused_even_when_both_are_good(self):
        good = os.path.join(self.catalogs, "other.json")
        for args, kwargs in (((good,), {"project": "dksk"}), ((), {"path": good, "project": "dksk"}),
                             ((good, "other"), {}), ((good,), {"project": ""})):
            with self.subTest(args=args, kwargs=sorted(kwargs)):
                with self.assertRaises(bind_kpi.BindError) as raised:
                    bind_kpi.load_catalog(*args, **kwargs)
                self.assertIn("--project and --catalog cannot be used together", str(raised.exception))

    def test_an_explicit_path_loads_that_file_and_ignores_the_catalog_folder(self):
        good = os.path.join(self.catalogs, "other.json")
        self.assertEqual(bind_kpi.load_catalog(good)["note"], "the other project")
        self.assertEqual(bind_kpi.load_catalog(path=good)["note"], "the other project")
        self.assertEqual(bind_kpi.load_catalog(good, catalog_dir=os.path.join(self.tmp, "nope"))["note"],
                         "the other project")
        with mock.patch.object(kpi_catalogs, "CATALOG_DIR", os.path.join(self.tmp, "nope")):
            self.assertEqual(bind_kpi.load_catalog(good)["note"], "the other project")

    def test_the_first_positional_parameter_is_the_path(self):
        self.assertEqual(bind_kpi.load_catalog(os.path.join(self.catalogs, "other.json"))["note"], "the other project")

    def test_a_catalog_error_becomes_a_bind_error_with_no_cause_and_no_echo(self):
        for typed in (TYPED_TEXT, TYPED_TEXT.upper(), "../" + TYPED_TEXT, TYPED_TEXT + "\n", "a/" + TYPED_TEXT):
            with self.subTest(typed=repr(typed)):
                with self.assertRaises(bind_kpi.BindError) as raised:
                    bind_kpi.load_catalog(project=typed, catalog_dir=self.catalogs)
                self.assertNotIsInstance(raised.exception, kpi_catalogs.CatalogError)
                self.assertNotIn(TYPED_TEXT, str(raised.exception).lower())
                self.assertIn("dksk, other", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(raised.exception.__suppress_context__)

    def test_two_catalogs_and_no_project_is_a_bind_error_that_lists_the_names(self):
        with self.assertRaises(bind_kpi.BindError) as raised:
            bind_kpi.load_catalog(catalog_dir=self.catalogs)
        self.assertIn("dksk, other", str(raised.exception))

    def test_an_unreadable_folder_is_a_bind_error(self):
        with self.assertRaises(bind_kpi.BindError):
            bind_kpi.load_catalog(catalog_dir=os.path.join(self.tmp, "nope"))
        with self.assertRaises(bind_kpi.BindError):
            bind_kpi.load_catalog(project="dksk", catalog_dir=os.path.join(self.tmp, "nope"))

    def test_a_bad_catalog_file_is_still_refused_by_the_shape_check(self):
        touch(self.catalogs, "broken.json", b'{"schema": 2, "sources": {}}')
        with self.assertRaises(bind_kpi.BindError) as raised:
            bind_kpi.load_catalog(project="broken", catalog_dir=self.catalogs)
        self.assertIn("unexpected shape", str(raised.exception))

    def test_the_old_module_constant_is_gone(self):
        self.assertFalse(hasattr(bind_kpi, "CATALOG_PATH"))


class CommandCase(FolderCase):
    """Each script run IN PROCESS with a folder of two valid catalogs (a subprocess would not see the patch)."""

    def setUp(self):
        super().setUp()
        touch(self.catalogs, "dksk.json", load_real())
        touch(self.catalogs, "other.json", load_real())
        patcher = mock.patch.object(kpi_catalogs, "CATALOG_DIR", self.catalogs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.baseline_path = os.path.join(self.work, "baseline.json")
        self.spec_path = os.path.join(self.work, "spec.json")
        self.dashboard_path = os.path.join(self.work, "dashboard.json")
        self.out_path = os.path.join(self.work, "result.json")

    def files(self):
        return sorted(os.listdir(self.work))

    def put(self, path, data):
        with open(path, "wb") as handle:
            handle.write(data)

    def bind_scene(self):
        baseline = rf.baseline_of(rf.typed_row(5, ["1", "2"]))
        spec = rf.spec_of([rf.list_binding(5, count=2)])
        baseline_bytes, spec_bytes, self.dashboard_bytes = rf.produce(baseline, spec)
        self.put(self.baseline_path, baseline_bytes)
        self.put(self.spec_path, spec_bytes)

    def scan_scene(self):
        self.put(self.baseline_path, intake_xlsx.dump_bytes(sf.twin()))

    def refusal(self, run, names="dksk, other", typed=None):
        """rc 2, an empty stdout, ONE `error:` line that lists the installed names and never the typed text."""
        rc, out, err = run
        self.assertEqual((rc, out), (2, ""))
        self.assertTrue(err.startswith("error: ") and len(err.splitlines()) == 1, err)
        self.assertIn(names, err)
        if typed:
            self.assertNotIn(typed, err.lower())

    def call(self, main, argv, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("socket.socket", side_effect=AssertionError("the network was reached")):
            rc = main(argv, out, err, **kwargs)
        return rc, out.getvalue(), err.getvalue()


class BindCommandTests(CommandCase):
    def argv(self, *extra):
        return ["--baseline", self.baseline_path, "--spec", self.spec_path, "--out", self.out_path] + list(extra)

    def setUp(self):
        super().setUp()
        self.bind_scene()
        self.before = self.files()

    def test_two_catalogs_and_no_project_is_refused_with_nothing_written(self):
        self.refusal(self.call(bind_kpi.main, self.argv()))
        self.assertEqual(self.files(), ["baseline.json", "spec.json"])

    def test_an_unknown_project_is_refused_with_the_list_and_without_the_typed_text(self):
        for typed in (TYPED_TEXT, TYPED_TEXT.upper(), "../" + TYPED_TEXT, TYPED_TEXT + "\n"):
            with self.subTest(typed=repr(typed)):
                self.refusal(self.call(bind_kpi.main, self.argv("--project", typed)), typed=TYPED_TEXT)
                self.assertEqual(self.files(), ["baseline.json", "spec.json"])

    def test_a_listed_project_binds_and_writes_exactly_what_the_binder_produces(self):
        rc, out, err = self.call(bind_kpi.main, self.argv("--project", "dksk"))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(json.loads(out)["sha256"], hashlib.sha256(self.dashboard_bytes).hexdigest())
        with open(self.out_path, "rb") as handle:
            self.assertEqual(handle.read(), self.dashboard_bytes)

    def test_the_other_listed_project_works_too(self):
        rc, out, err = self.call(bind_kpi.main, self.argv("--project", "other"))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.files(), ["baseline.json", "result.json", "spec.json"])

    def test_project_and_catalog_together_are_refused_with_nothing_written(self):
        catalog = os.path.join(self.work, "catalog.json")
        self.put(catalog, load_real())
        before = self.files()
        rc, out, err = self.call(bind_kpi.main, self.argv("--project", "dksk", "--catalog", catalog))
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("--project and --catalog cannot be used together", err)
        self.assertEqual(self.files(), before)

    def test_the_catalog_option_alone_is_unchanged_and_needs_no_project(self):
        catalog = os.path.join(self.work, "catalog.json")
        self.put(catalog, load_real())
        rc, out, err = self.call(bind_kpi.main, self.argv("--catalog", catalog))
        self.assertEqual((rc, err), (0, ""))
        with open(self.out_path, "rb") as handle:
            self.assertEqual(handle.read(), self.dashboard_bytes)


class ScanCommandTests(CommandCase):
    def argv(self, *extra):
        return ["--baseline", self.baseline_path, "--out", self.out_path] + list(extra)

    def setUp(self):
        super().setUp()
        self.scan_scene()
        self.before = self.files()

    def test_two_catalogs_and_no_project_is_refused_with_nothing_written(self):
        self.refusal(self.call(scan_excel.main, self.argv()))
        self.assertEqual(self.files(), ["baseline.json"])

    def test_the_default_draft_name_is_not_written_either(self):
        self.refusal(self.call(scan_excel.main, ["--baseline", self.baseline_path]))
        self.assertEqual(self.files(), ["baseline.json"])

    def test_an_unknown_project_is_refused_with_the_list_and_without_the_typed_text(self):
        for typed in (TYPED_TEXT, TYPED_TEXT.upper(), "../" + TYPED_TEXT, TYPED_TEXT + "\n"):
            with self.subTest(typed=repr(typed)):
                self.refusal(self.call(scan_excel.main, self.argv("--project", typed)), typed=TYPED_TEXT)
                self.assertEqual(self.files(), ["baseline.json"])

    def test_a_listed_project_scans_and_writes_the_draft(self):
        rc, out, err = self.call(scan_excel.main, self.argv("--project", "dksk"))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.files(), ["baseline.json", "result.json"])
        with open(self.out_path, "rb") as handle:
            self.assertEqual(len(json.loads(handle.read().decode("utf-8"))["bindings"]), 14)

    def test_the_project_name_does_not_change_the_draft(self):
        self.call(scan_excel.main, self.argv("--project", "dksk"))
        with open(self.out_path, "rb") as handle:
            first = handle.read()
        os.remove(self.out_path)
        self.call(scan_excel.main, self.argv("--project", "other"))
        with open(self.out_path, "rb") as handle:
            self.assertEqual(handle.read(), first)


class ReconcileCommandTests(CommandCase):
    """No Odoo is needed: a client factory that records calls stands in for the network, and `load_config` is patched."""

    def argv(self, *extra):
        return ["--baseline", self.baseline_path, "--spec", self.spec_path, "--dashboard", self.dashboard_path,
                "--year", "2026", "--out", self.out_path] + list(extra)

    def setUp(self):
        super().setUp()
        self.bind_scene()
        self.put(self.dashboard_path, self.dashboard_bytes)
        self.before = self.files()
        self.calls = []
        self.config_reads = []

    def factory(self, config, confirm_instance=None):
        self.calls.append(config)
        raise odoo_rpc.RpcError("stop here: the test needs no server")

    def reconcile(self, *extra):
        with mock.patch.object(odoo_rpc, "load_config", side_effect=lambda **kw: self.config_reads.append(kw) or object()):
            return self.call(reconcile_kpi.main, self.argv(*extra), client_factory=self.factory)

    def test_two_catalogs_and_no_project_is_refused_before_the_config_or_the_network(self):
        self.refusal(self.reconcile())
        self.assertEqual((self.calls, self.config_reads), ([], []))
        self.assertEqual(self.files(), ["baseline.json", "dashboard.json", "spec.json"])

    def test_an_unknown_project_is_refused_before_the_config_or_the_network(self):
        for typed in (TYPED_TEXT, TYPED_TEXT.upper(), "../" + TYPED_TEXT, TYPED_TEXT + "\n"):
            with self.subTest(typed=repr(typed)):
                self.refusal(self.reconcile("--project", typed), typed=TYPED_TEXT)
                self.assertEqual((self.calls, self.config_reads), ([], []))
                self.assertEqual(self.files(), ["baseline.json", "dashboard.json", "spec.json"])

    def test_a_listed_project_gets_as_far_as_the_client(self):
        rc, out, err = self.reconcile("--project", "dksk")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.config_reads), 1)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("stop here", err)

    def test_the_dashboard_must_be_what_the_chosen_catalog_produces(self):
        # `other` is a copy of the same catalog, so the bytes agree; a catalog that differs gives another dashboard
        other = json.loads(load_real().decode("utf-8"))
        other["sources"]["report_group"]["title"] = "A different title"
        touch(self.catalogs, "other.json", json.dumps(other).encode("utf-8"))
        rc, out, err = self.reconcile("--project", "other")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("the dashboard file is not what the spec produces", err)
        self.assertEqual((self.calls, self.config_reads), ([], []))

    def test_bound_document_takes_the_project_as_its_fourth_parameter(self):
        baseline, document, produced = reconcile_kpi.bound_document(self.baseline_path, self.spec_path,
                                                                    self.dashboard_path, "dksk")
        self.assertEqual(produced, hashlib.sha256(self.dashboard_bytes).hexdigest())
        with self.assertRaises(bind_kpi.BindError):
            reconcile_kpi.bound_document(self.baseline_path, self.spec_path, self.dashboard_path)   # two catalogs
        with self.assertRaises(bind_kpi.BindError):
            reconcile_kpi.bound_document(self.baseline_path, self.spec_path, self.dashboard_path, project="nope")


class SoleDefaultTests(IsolatedTestCase):
    """With the one catalog that ships, today's commands need no `--project` (the QA/QC flow does not change)."""

    def test_bind_without_a_project_is_the_same_as_with_dksk(self):
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        baseline = rf.baseline_of(rf.typed_row(5, ["1", "2"]))
        baseline_bytes, spec_bytes, dashboard_bytes = rf.produce(baseline, rf.spec_of([rf.list_binding(5, count=2)]))
        for name, data in (("baseline.json", baseline_bytes), ("spec.json", spec_bytes)):
            touch(work, name, data)
        for extra, out_name in (([], "plain.json"), (["--project", "dksk"], "named.json")):
            rc = bind_kpi.main(["--baseline", os.path.join(work, "baseline.json"), "--spec", os.path.join(work, "spec.json"),
                                "--out", os.path.join(work, out_name)] + extra, io.StringIO(), io.StringIO())
            self.assertEqual(rc, 0)
            with open(os.path.join(work, out_name), "rb") as handle:
                self.assertEqual(handle.read(), dashboard_bytes)


if __name__ == "__main__":
    unittest.main()
