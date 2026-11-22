"""AC1 of the multi-project plan: the old plugin identity never comes back, and the bare customer word stays in its place.

`scan_old_identity(root)` walks a tree (EVERY file, as bytes, case-insensitive; it skips only the `.git` and
`__pycache__` folders) and returns `[(relative_path, reason)]`. Two tiers:

* tier (i): the old identity tokens (the old env prefix, the old config folder, the old temp-file prefix, the old
  plugin / marketplace name) are forbidden in every file CONTENT and in every file / folder NAME, `tests/` included,
  this file included. The one exception: the two Odoo xml ids of the shipped dashboards in `tests/dashboard_fixtures.py`
  (an Odoo module name, customer data, not the plugin identity), matched as exact strings in that file only.
* tier (ii): the bare customer word (any case, content or path) is allowed only under the directories in
  `ALLOWED_DIRS` (a folder that is not there yet still counts the day it appears) and in the files of `ALLOWED_FILES`
  (granted per file, no line check).

This file is part of the public tree: it must not spell the words it forbids, so every word below is built from
fragments joined at runtime. The scanner is a function so that the unit tests prove it RED by planting each word in a
temporary tree (a green scan of the real tree proves nothing on its own).
"""
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import odoo_rpc

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__"}

# built from fragments on purpose (see the module docstring); matched in lower case
WORD = "dk" + "sk"
OLD_TOKENS = (WORD + "_" + "kpi", WORD + "-" + "kpi", WORD + "-" + "config")
# the Odoo xml ids that are customer data: granted as EXACT strings, in this one file only
XMLID_FILE = "tests/dashboard_fixtures.py"
XMLIDS = (WORD + "_kpi" + ".kpi_1", WORD + "_kpi" + ".kpi_2")
XMLID_RE = re.compile(rb"(?:" + b"|".join(re.escape(x.encode()) for x in XMLIDS) + rb")(?![0-9A-Za-z_])")

# tier (ii): where the bare word may live (the project NAME, not the old identity)
ALLOWED_DIRS = (("catalogs",), ("skills", "kpi-source-catalog", "projects"), ("tests",))
# `scripts/catalog.json` was granted here until it moved to `catalogs/` (Plan kpi-multi-project step 2a)
ALLOWED_FILES = ("scripts/validate_formulas.py",)

KELVIN = chr(0x212A)       # the Kelvin sign: Unicode lower-cases it to the letter k

REASON_TOKEN = "old identity token"
REASON_WORD = "bare customer word outside the allowed places"


def has_term(raw, term):
    """Whether the bytes `raw` hold `term` (lower case) in any case: as UTF-8 / ASCII text, as UTF-16 text (either byte
    order, with or without BOM), or inside binary data (invalid bytes are decoded with replacement, the term survives).
    The decoded text is lower-cased by Unicode rules, so the Kelvin sign counts as `k`."""
    lowered = raw.lower()
    return term in raw.decode("utf-8", "replace").lower() or any(
        term.encode(encoding) in lowered for encoding in ("utf-16-le", "utf-16-be"))


def word_allowed_here(rel):
    parts = tuple(rel.split("/"))
    return rel in ALLOWED_FILES or any(parts[:len(folder)] == folder and len(parts) > len(folder)
                                       for folder in ALLOWED_DIRS)


def path_findings(rel):
    found = {(rel, REASON_TOKEN + " (in the path)")} if any(t in rel.lower() for t in OLD_TOKENS) else set()
    if WORD in rel.lower() and not word_allowed_here(rel):
        found.add((rel, REASON_WORD + " (in the path)"))
    return found


def content_findings(rel, raw):
    if rel == XMLID_FILE:
        raw = XMLID_RE.sub(b"", raw)
    found = set()
    if any(has_term(raw, token) for token in OLD_TOKENS):
        found.add((rel, REASON_TOKEN))
    if has_term(raw, WORD) and not word_allowed_here(rel):
        found.add((rel, REASON_WORD))
    return found


def iter_tree(root):
    """Relative posix paths of every file and folder under `root` (any depth, hidden ones included), `.git` and
    `__pycache__` skipped. A folder is `(rel, False)`, a file `(rel, True)`."""
    root = Path(root)
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        here = Path(current)
        for name in dirs:
            yield (here / name).relative_to(root).as_posix(), False
        for name in sorted(files):
            yield (here / name).relative_to(root).as_posix(), True


def scan_old_identity(root):
    """[(path relative to `root`, reason)], sorted; empty when the tree keeps the old identity out and the bare word
    inside its allowed places."""
    root = Path(root)
    found = set()
    for rel, is_file in iter_tree(root):
        found |= path_findings(rel)
        if is_file and (root / rel).is_file():           # a broken link has a name but no content
            found |= content_findings(rel, (root / rel).read_bytes())
    return sorted(found)


def encodings(text):
    """(file name, bytes) for each way of storing `text` the scan must read."""
    data = text.encode("utf-8")
    return {"a.py": data, "data.json": data, "notes.md": data, "noext": data, ".hidden": data,
            "deep/er/nest/x.txt": data,
            "u16le.txt": text.encode("utf-16-le"), "u16le_bom.txt": b"\xff\xfe" + text.encode("utf-16-le"),
            "u16be.txt": text.encode("utf-16-be"), "blob.bin": b"\x00\xff\xfe" + data + b"\x00\x80"}


class TreeCase(unittest.TestCase):
    def fresh(self):
        tmp = tempfile.TemporaryDirectory(prefix="odoo-kpi-identity-")
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    @staticmethod
    def put(root, rel, content=b""):
        path = Path(root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    @staticmethod
    def reasons(root, rel):
        return sorted(reason for path, reason in scan_old_identity(root) if path == rel)


class RepositoryIdentityTests(TreeCase):
    def test_the_tree_holds_no_old_identity_token_and_no_stray_customer_word(self):
        self.assertEqual(scan_old_identity(REPO), [])

    def test_the_scan_really_visits_the_files_and_this_file_is_not_exempt(self):
        visited = {rel for rel, is_file in iter_tree(REPO) if is_file}
        for rel in ("scripts/odoo_rpc.py", "README.md", "tests/dashboard_fixtures.py", "tests/test_old_identity.py"):
            self.assertIn(rel, visited)
        self.assertFalse([rel for rel in visited if rel.startswith(".git/") or "__pycache__" in rel])
        # no exemption for this file: it spells none of the words, so the scan of the real tree is clean with it in
        self.assertFalse(content_findings("tests/test_old_identity.py", (REPO / "tests" / "test_old_identity.py").read_bytes()))

    def test_every_grant_is_still_needed(self):
        # a stale grant is a hole: each allowed FILE must exist and still hold the word, and the xml id exception must
        # still be used (steps that move or delete these files edit this list on purpose)
        for rel in ALLOWED_FILES:
            raw = (REPO / rel).read_bytes()
            self.assertTrue(has_term(raw, WORD), rel + " no longer holds the word: drop its grant")
        raw = (REPO / XMLID_FILE).read_bytes()
        for xmlid in XMLIDS:
            self.assertIn(xmlid.encode(), raw)

    def test_the_new_identity_is_present_where_it_must_be(self):
        plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["name"], "odoo-kpi-skills")
        self.assertEqual(market["name"], "odoo-kpi")
        self.assertEqual([entry["name"] for entry in market["plugins"]], ["odoo-kpi-skills"])
        self.assertTrue(odoo_rpc.USER_AGENT.startswith("odoo-kpi-skills/"), odoo_rpc.USER_AGENT)
        self.assertTrue(odoo_rpc.ENV_NAMES)
        for key, name in odoo_rpc.ENV_NAMES.items():
            self.assertTrue(name.startswith("ODOO_KPI_"), (key, name))


class ScannerTierOneTests(TreeCase):
    def test_every_token_is_caught_in_every_file_kind_case_and_folder(self):
        variants = (str.lower, str.upper, str.swapcase)
        for token in OLD_TOKENS:
            for variant in variants:
                for folder in ("scripts", "tests", "catalogs", "skills/kpi-source-catalog/projects", ""):
                    for name, content in encodings("x = '%s'\n" % variant(token)).items():
                        with self.subTest(token=variant(token), folder=folder, file=name):
                            root = self.fresh()
                            rel = (folder + "/" + name).lstrip("/")
                            self.put(root, rel, content)
                            self.assertIn(REASON_TOKEN, self.reasons(root, rel))

    def test_every_real_shape_of_the_old_identity_is_caught(self):
        # built here independently of OLD_TOKENS, so that dropping a token from that list cannot go unnoticed
        old = "DK" + "SK"
        shapes = {"env prefix": old + "_KPI_URL", "admin env": old + "_KPI_ADMIN_SECRET", "config override": old + "_KPI_CONFIG",
                  "config folder": "~/.config/" + old.lower() + "-kpi/config.ini",
                  "windows config folder": "%APPDATA%\\" + old.lower() + "-kpi\\config.ini",
                  "temp file prefix": "." + old.lower() + "-config-abcd1234.tmp",
                  "plugin name": old.lower() + "-kpi-skills", "marketplace name": old.lower() + "-kpi",
                  "command": "/" + old.lower() + "-kpi-skills:kpi", "user agent": old.lower() + "-kpi-skills/0.1.0",
                  "odoo module style name": old.lower() + "_kpi_admin"}
        for label, text in shapes.items():
            with self.subTest(label):
                root = self.fresh()
                self.put(root, "scripts/x.py", "VALUE = %r\n" % text)
                self.assertIn(REASON_TOKEN, self.reasons(root, "scripts/x.py"))

    def test_a_token_alone_in_a_utf16_file_is_caught_in_each_byte_order(self):
        # the bytes of one order sit inside the other order's bytes only when a character follows / precedes the word:
        # a file that holds nothing else proves that BOTH encodings are really searched
        for token in OLD_TOKENS:
            for label, content in (("le", token.encode("utf-16-le")), ("be", token.encode("utf-16-be")),
                                   ("le+bom", b"\xff\xfe" + token.encode("utf-16-le")),
                                   ("be+bom", b"\xfe\xff" + token.encode("utf-16-be"))):
                with self.subTest(token=token, order=label):
                    root = self.fresh()
                    self.put(root, "scripts/u.txt", content)
                    self.assertIn(REASON_TOKEN, self.reasons(root, "scripts/u.txt"))

    def test_a_token_in_a_file_name_or_folder_name_is_reported_too(self):
        for token in OLD_TOKENS:
            for rel in ("docs/%s-notes.md" % token, "docs/%s/clean.txt" % token, "tests/%s.json" % token,
                        "a/b/.%s" % token, "catalogs/%s/x.json" % token, "%s" % token.upper()):
                with self.subTest(rel=rel):
                    root = self.fresh()
                    self.put(root, rel, "nothing to see\n")
                    self.assertIn(REASON_TOKEN + " (in the path)", self.reasons(root, rel))

    def test_a_folder_name_alone_is_reported_even_without_files(self):
        root = self.fresh()
        (root / "docs" / OLD_TOKENS[1]).mkdir(parents=True)
        self.assertIn(("docs/" + OLD_TOKENS[1], REASON_TOKEN + " (in the path)"), scan_old_identity(root))

    def test_the_kelvin_sign_does_not_hide_a_token(self):
        kelvin = "d" + KELVIN + "s" + KELVIN + "-kpi"           # lower-cases to the old plugin name
        root = self.fresh()
        self.put(root, "scripts/x.py", "NAME = '%s'\n" % kelvin)
        self.assertIn(REASON_TOKEN, self.reasons(root, "scripts/x.py"))

    def test_the_two_xml_ids_are_granted_in_the_fixture_file_only_and_only_as_exact_strings(self):
        ok = 'ids = ["%s", "%s"]\n' % XMLIDS
        cases = {
            "the granted file with exactly the two ids": (XMLID_FILE, ok, False),
            "the same ids in another test file": ("tests/other.py", ok, True),
            "the same ids at the repo root": ("fixtures.py", ok, True),
            "the same ids in a nested copy of the file name": ("sub/" + XMLID_FILE, ok, True),
            "the granted file with a third id": (XMLID_FILE, ok + 'x = "%s.kpi_3"\n' % (WORD + "_kpi"), True),
            "the granted file with a longer id": (XMLID_FILE, 'x = "%s0"\n' % XMLIDS[0], True),
            "the granted file with the upper-case id": (XMLID_FILE, 'x = "%s"\n' % XMLIDS[0].upper(), True),
            "the granted file with an extra old env name": (XMLID_FILE, ok + 'k = "%s_URL"\n' % (WORD + "_kpi").upper(), True),
            "the granted file with the old plugin name": (XMLID_FILE, ok + 'k = "%s-skills"\n' % OLD_TOKENS[1], True),
            "the granted file with the ids in UTF-16": (XMLID_FILE, ok.encode("utf-16-le"), True),
        }
        for label, (rel, content, red) in cases.items():
            with self.subTest(label):
                root = self.fresh()
                self.put(root, rel, content)
                self.assertEqual(REASON_TOKEN in self.reasons(root, rel), red)


class ScannerTierTwoTests(TreeCase):
    def test_the_bare_word_is_allowed_in_the_listed_places_even_when_they_are_new(self):
        root = self.fresh()
        text = "project = '%s'\n" % WORD
        for rel in ("tests/test_x.py", "tests/sub/dir/fixture.json", "catalogs/%s.json" % WORD,
                    "skills/kpi-source-catalog/projects/%s.md" % WORD,
                    "scripts/validate_formulas.py", "tests/%s_fixture.json" % WORD, "catalogs/%s/a.json" % WORD):
            self.put(root, rel, text)
        self.assertEqual(scan_old_identity(root), [])

    def test_the_bare_word_is_caught_everywhere_else_in_every_file_kind_and_case(self):
        for variant in (str.lower, str.upper, str.title, str.swapcase):
            for folder in ("scripts", "skills", "commands", ".claude-plugin", "docs/deep", ""):
                for name, content in encodings("note %s\n" % variant(WORD)).items():
                    with self.subTest(word=variant(WORD), folder=folder, file=name):
                        root = self.fresh()
                        rel = (folder + "/" + name).lstrip("/")
                        self.put(root, rel, content)
                        self.assertIn(REASON_WORD, self.reasons(root, rel))

    def test_the_bare_word_alone_in_a_utf16_file_is_caught_in_each_byte_order(self):
        for label, content in (("le", WORD.encode("utf-16-le")), ("be", WORD.encode("utf-16-be")),
                               ("le+bom", b"\xff\xfe" + WORD.encode("utf-16-le")),
                               ("be+bom", b"\xfe\xff" + WORD.encode("utf-16-be"))):
            with self.subTest(order=label):
                root = self.fresh()
                self.put(root, "scripts/u.txt", content)
                self.assertIn(REASON_WORD, self.reasons(root, "scripts/u.txt"))

    def test_the_grants_stop_at_the_segment_boundary(self):
        # each path looks like an allowed one and is not: a prefix match without a boundary would let it through
        for rel in ("testsx/a.py", "tests_extra/a.py", "Tests/a.py", "nested/tests/a.py", "catalogs-old/a.json",
                    "catalogs.json", "skills/kpi-source-catalog/projects-old/p.json", "skills/kpi-source-catalog/p.json",
                    "skills/other/projects/p.json", "scripts/catalog.json", "scripts/catalog.json.bak", "scripts/other/catalog.json",
                    "docs/scripts/catalog.json", "catalog.json", "scripts/validate_formulas.py.orig",
                    "scripts/validate_formulas_v2.py", "tests"):
            with self.subTest(rel=rel):
                root = self.fresh()
                self.put(root, rel, "project = '%s'\n" % WORD)
                self.assertIn(REASON_WORD, self.reasons(root, rel))

    def test_a_file_or_folder_name_with_the_word_is_reported_outside_the_allowed_places(self):
        for rel in ("docs/%s-notes.md" % WORD, "scripts/%s/clean.txt" % WORD, "%s" % WORD.upper(), "a/b/.%s" % WORD,
                    "commands/Notes %s.md" % WORD):
            with self.subTest(rel=rel):
                root = self.fresh()
                self.put(root, rel, "nothing to see\n")
                self.assertIn(REASON_WORD + " (in the path)", self.reasons(root, rel))

    def test_a_clean_file_in_a_clean_place_and_an_empty_tree_are_not_flagged(self):
        root = self.fresh()
        self.assertEqual(scan_old_identity(root), [])
        self.put(root, "scripts/tool.py", "VERSION = '0.1.0'\nNAME = 'odoo-kpi-skills'\nENV = 'ODOO_KPI_URL'\n")
        self.put(root, "README.md", "# Odoo KPI skills\n")
        self.assertEqual(scan_old_identity(root), [])

    def test_the_bare_word_hidden_by_a_kelvin_sign_is_still_caught(self):
        root = self.fresh()
        self.put(root, "README.md", "customer d" + KELVIN + "s" + KELVIN + " here\n")
        self.assertIn(REASON_WORD, self.reasons(root, "README.md"))


class ScannerSkipTests(TreeCase):
    def test_pycache_and_git_are_skipped_wherever_they_are(self):
        root = self.fresh()
        text = "x = '%s' and '%s'\n" % (OLD_TOKENS[0], WORD)
        for rel in ("__pycache__/m.pyc", "scripts/__pycache__/m.pyc", "tests/__pycache__/%s.pyc" % OLD_TOKENS[1],
                    ".git/config", ".git/hooks/%s" % OLD_TOKENS[2], "sub/.git/config"):
            self.put(root, rel, text)
        self.assertEqual(scan_old_identity(root), [])

    def test_a_folder_that_only_looks_like_a_skipped_one_is_scanned(self):
        for rel in ("pycache/m.py", "__pycache__x/m.py", "_git/config", ".github/workflows/ci.yml", ".gitignore"):
            with self.subTest(rel=rel):
                root = self.fresh()
                self.put(root, rel, "x = '%s'\n" % OLD_TOKENS[0])
                self.assertIn(REASON_TOKEN, self.reasons(root, rel))

    def test_the_findings_are_a_sorted_list_of_path_reason_pairs(self):
        root = self.fresh()
        self.put(root, "b.py", "x = '%s'\n" % OLD_TOKENS[0])
        self.put(root, "a.md", "y = '%s'\n" % WORD)
        findings = scan_old_identity(root)
        self.assertIsInstance(findings, list)
        self.assertEqual(findings, sorted(findings))
        self.assertEqual([path for path, _ in findings], ["a.md", "b.py", "b.py"])
        self.assertTrue(all(isinstance(path, str) and isinstance(reason, str) for path, reason in findings))


class ScannerOnACopyOfTheRealTreeTests(TreeCase):
    """The same scan on a copy of the real layout: planting into the copy proves the real paths are covered."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="odoo-kpi-identity-copy-")
        cls.copy = Path(cls.tmp.name) / "plugin"
        shutil.copytree(REPO, cls.copy, ignore=shutil.ignore_patterns(".git", "__pycache__"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def plant(self, rel, text):
        path = self.copy / rel
        original = path.read_bytes() if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((original or b"") + text.encode("utf-8"))
        self.addCleanup(self.restore, path, original)

    @staticmethod
    def restore(path, original):
        if original is None:
            path.unlink()
        else:
            path.write_bytes(original)

    def test_the_copy_starts_clean(self):
        self.assertEqual(scan_old_identity(self.copy), [])

    def test_a_token_planted_in_each_kind_of_real_file_turns_the_scan_red(self):
        for rel in ("scripts/odoo_rpc.py", "scripts/setup_instance.py", "README.md", "README.vi.md",
                    "commands/kpi.md", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
                    ".gitignore", "run-tests.sh", "tests/helpers.py", "tests/dashboard_fixtures.py",
                    "catalogs/dksk.json", "scripts/validate_formulas.py"):
            with self.subTest(rel=rel):
                self.plant(rel, "\n%s_URL\n" % OLD_TOKENS[0].upper())
                self.assertIn(REASON_TOKEN, self.reasons(self.copy, rel))

    def test_the_bare_word_planted_outside_the_allowed_places_turns_the_scan_red_and_inside_does_not(self):
        for rel in ("scripts/odoo_rpc.py", "README.md", "commands/kpi.md", ".claude-plugin/plugin.json"):
            with self.subTest(rel=rel):
                self.plant(rel, "\n%s\n" % WORD.upper())
                self.assertIn(REASON_WORD, self.reasons(self.copy, rel))
        for rel in ("tests/helpers.py", "catalogs/dksk.json", "scripts/validate_formulas.py"):
            with self.subTest(rel=rel):
                self.plant(rel, "\n%s\n" % WORD.upper())
                self.assertEqual(self.reasons(self.copy, rel), [])


if __name__ == "__main__":
    unittest.main()
