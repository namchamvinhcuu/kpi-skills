"""The plugin repository is PUBLIC: no customer data, no Excel, no secret, no real instance name.

`scan_tree` walks a tree (skipping `.git`, `__pycache__` and the top-level `tests` folder, which holds fixtures)
and returns a list of `(relative_path, reason)`. The repository test asserts the list is empty; the scanner tests
prove, on temporary trees, that each kind of violation IS caught and that a clean tree is NOT flagged.

A line may carry the marker `secret-allow` (with a reason) to allow a password/api_key/secret assignment.
"""
import fnmatch
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__"}
SKIP_TOP_LEVEL = {"tests"}
FORBIDDEN_FILE_SUFFIXES = (".xlsx", ".osheet.json")
# built from fragments on purpose: this file is part of the public tree and must not spell the words it forbids
COMPANY_DOMAIN, UAT_LABEL, DEV_LOGIN = "autonsi" + ".cloud", "nam" + "-dksk", "namcham" + "vinhcuu"
FORBIDDEN_TEXT = (COMPANY_DOMAIN, UAT_LABEL, DEV_LOGIN)
HEX40 = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{40}(?![0-9A-Fa-f])")
SECRET_WORD = r"(?:password|passwd|api_?key|secret)\w*"
# a quoted literal assigned to a secret-looking name:  password = "x"   "api_key": 'x'   secret='x'
SECRET_QUOTED = re.compile(r"(?i)" + SECRET_WORD + r"""["']?\s*[:=]\s*["'][^"'\s][^"']*["']""")
# an INI style line:  api_key = abc   (a placeholder in angle brackets does not count)
SECRET_INI = re.compile(r"(?i)^\s*" + SECRET_WORD + r"""\s*[:=]\s*(?!<)[^\s<"']\S*""")
ALLOW_MARKER = "secret-allow"


# files the workflow writes next to the user's data: `.gitignore` must keep them out of the repository
WORKFLOW_OUTPUT_PATTERNS = ("baseline*.json", "dashboard*.json", "kpi_spec*.json",
                            # Plan kpi-skill-improvements R1: the reconcile report (live customer numbers), the reusable
                            # specs (`<excel>.spec.json`, `<excel>.v2.spec.json`), the scan drafts and the user's aliases
                            "reconcile*.json", "*.spec.json", "*.draft.json", "aliases*.json",
                            # combined review G1: the user's own aliases file may be named `my_aliases.json`
                            "*aliases*.json")
# combined review G1: the customer's design documents and spreadsheets (never part of the public repository)
DESIGN_FILE_PATTERNS = ("*.xls*", "*.pdf", "*.pptx", "*.docx")


# The ONE public spelling of the owner login: the repository slug named by the install line of the READMEs
# (`/plugin marketplace add <slug>`). Built from fragments like the words above.
REPO_SLUG = "namcham" + "vinhcuu" + "/kpi-" + "skills"
# An allowed occurrence of the slug: either right after `github.com/`, or not glued to a word character, a `/`, a `.`,
# a backslash or a `-` on its left (no `x<slug>`, no `/home/<slug>`, no `a.<slug>`); and not followed by a word
# character or a `-` (no `<slug>-x`, no `<slug>x`). Case-insensitive like the rest of the scan.
ALLOWED_SLUG = re.compile(r"(?:(?<=github\.com/)|(?<![\w/.\\-]))" + re.escape(REPO_SLUG) + r"(?![\w-])", re.IGNORECASE)


def is_utf8_text(raw):
    """True for real text: valid UTF-8 without a NUL byte (UTF-16 text and binary data are not)."""
    if b"\x00" in raw:
        return False
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def has_term(raw, term):
    """Whether the bytes `raw` hold `term` (lower case) in any case: as UTF-8 / ASCII text, as UTF-16 text, or inside
    binary data (bytes that are no valid text are decoded with replacement, the term's own bytes survive).
    The ONLY exemption: in real UTF-8 text every allowed occurrence of `REPO_SLUG` is blanked first - by a single space,
    never by nothing, so the two sides of it cannot join into a word. UTF-16 text and binary data get none."""
    lowered = raw.lower()
    text = raw.decode("utf-8", "replace")
    if is_utf8_text(raw):
        text = ALLOWED_SLUG.sub(" ", text)
    return term in text.lower() or any(
        term.encode(encoding) in lowered for encoding in ("utf-16-le", "utf-16-be"))


def name_has_term(rel, term):
    """Whether a relative path (folder and file names) holds `term` in any case. No slug exemption here: the slug is
    allowed in the TEXT of a page, never in a path."""
    return term in rel.lower()


def scan_words(folder, root):
    """[(path relative to `root`, term)] for every file under `folder` (any depth, hidden files included) that holds a
    forbidden word. `__pycache__` is skipped: it only holds what the sources already say."""
    folder, root = Path(folder), Path(root)
    findings = []
    for path in sorted(folder.rglob("*")):
        if path.is_file() and "__pycache__" not in path.relative_to(folder).parts:
            raw = path.read_bytes()
            rel = path.relative_to(root).as_posix()
            findings += [(rel, term) for term in FORBIDDEN_TEXT if has_term(raw, term) or name_has_term(rel, term)]
    return findings


def forbidden_files(folder, root):
    """Relative paths of the files under `folder` (any depth) of a forbidden type: `scan_tree` skips `tests/`, so an
    Excel or a spreadsheet JSON put there would pass it."""
    folder, root = Path(folder), Path(root)
    return sorted(path.relative_to(root).as_posix() for path in folder.rglob("*")
                  if path.is_file() and path.name.lower().endswith(FORBIDDEN_FILE_SUFFIXES))


def workflow_outputs(root, patterns=None):
    """Relative paths of the files under `root` named like an output of the workflow (case-insensitive, any depth,
    `tests/` included); `patterns` replaces the list of names (the design documents of `DESIGN_FILE_PATTERNS`)."""
    root = Path(root)
    found = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if any(fnmatch.fnmatch(name.lower(), pattern) for pattern in (patterns or WORKFLOW_OUTPUT_PATTERNS)):
                found.append((Path(current) / name).relative_to(root).as_posix())
    return sorted(found)


def gitignore_patterns(text):
    """The plain patterns of a .gitignore (no comment, no blank line); a pattern that is not a bare file name glob is
    kept as it is (the callers only ask about bare names)."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def is_ignored(name, patterns):
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns if "/" not in pattern
               and not pattern.startswith("!"))


def scan_tree(root, skip_top_level=SKIP_TOP_LEVEL):
    root = Path(root)
    findings = []
    for current, dirs, files in os.walk(root):
        here = Path(current)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (here == root and d in skip_top_level))
        for name in sorted(files):
            path = here / name
            rel = path.relative_to(root).as_posix()
            findings += [(rel, "forbidden text in name: " + term) for term in FORBIDDEN_TEXT if name_has_term(rel, term)]
            if name.lower().endswith(FORBIDDEN_FILE_SUFFIXES):
                findings.append((rel, "forbidden file type"))
                continue
            raw = path.read_bytes()
            text = raw.decode("utf-8", "replace")
            for term in FORBIDDEN_TEXT:
                if has_term(raw, term):
                    findings.append((rel, "forbidden text: " + term))
            if HEX40.search(text):
                findings.append((rel, "40-hex-digit token"))
            for number, line in enumerate(text.splitlines(), 1):
                if ALLOW_MARKER in line:
                    continue
                if SECRET_QUOTED.search(line) or SECRET_INI.search(line):
                    findings.append((rel, "secret-looking assignment at line %d" % number))
    return findings


class RepositoryHygieneTests(unittest.TestCase):
    def test_the_repository_holds_no_forbidden_file_or_text(self):
        self.assertEqual(scan_tree(REPO), [])

    def test_the_scan_really_visits_the_plugin_files(self):
        # guards against a scanner that silently walks nothing
        visited = []
        for current, dirs, files in os.walk(REPO):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (Path(current) == REPO and d in SKIP_TOP_LEVEL)]
            visited += [Path(current, f).relative_to(REPO).as_posix() for f in files]
        self.assertIn("scripts/odoo_rpc.py", visited)
        self.assertIn("README.md", visited)
        self.assertFalse([f for f in visited if f.startswith("tests/")])
        self.assertFalse([f for f in visited if f.startswith(".git/")])

    def test_the_script_carries_the_allow_marker_only_where_needed(self):
        script = (REPO / "scripts" / "odoo_rpc.py").read_text(encoding="utf-8")
        marked = [line for line in script.splitlines() if ALLOW_MARKER in line]
        self.assertLessEqual(len(marked), 2)
        for line in marked:
            self.assertIn("ODOO_KPI_API_KEY", line)  # the marker covers the env var NAME only


class ScannerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="kpi-hygiene-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def put(self, rel, text="", binary=None):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(binary if binary is not None else text.encode("utf-8"))
        return path

    def reasons(self):
        return sorted(reason for _, reason in scan_tree(self.root))

    def test_clean_tree_has_no_findings(self):
        self.put("README.md", "Config:\n\n    api_key = <an API key created in Odoo>\n    url = https://your-instance.example.com\n")
        self.put("scripts/tool.py", 'VERSION = "0.1.0"\nif parts.password:\n    pass\ntext = "reset your password"\n')
        self.put("data.json", '{"sha": "abc123", "n": 12345678901234567890}')
        self.assertEqual(scan_tree(self.root), [])

    def test_empty_tree_has_no_findings(self):
        self.assertEqual(scan_tree(self.root), [])

    def test_xlsx_file_is_caught_in_any_case_and_folder(self):
        self.put("docs/KPI.xlsx", binary=b"PK\x03\x04")
        self.put("UPPER.XLSX", binary=b"PK")
        self.assertEqual(sorted(path for path, _ in scan_tree(self.root)), ["UPPER.XLSX", "docs/KPI.xlsx"])

    def test_osheet_json_file_is_caught(self):
        self.put("out/dash.osheet.json", "{}")
        self.assertEqual(scan_tree(self.root), [("out/dash.osheet.json", "forbidden file type")])

    def test_plain_json_is_not_confused_with_osheet_json(self):
        self.put("plugin.json", "{}")
        self.assertEqual(scan_tree(self.root), [])

    def test_real_instance_names_are_caught_case_insensitively(self):
        for text in ("see https://acme." + COMPANY_DOMAIN + "/web", "host " + COMPANY_DOMAIN.upper(),
                     "instance " + UAT_LABEL.title(), "/home/" + DEV_LOGIN + "/x"):
            with self.subTest(text=text):
                self.put("README.md", text)
                self.assertEqual(len(scan_tree(self.root)), 1)
                self.assertIn("forbidden text", self.reasons()[0])

    def test_forty_hex_digit_token_is_caught(self):
        self.put("notes.txt", "token: " + "0123456789abcdef" * 2 + "01234567")
        self.assertEqual(self.reasons(), ["40-hex-digit token"])

    def test_uppercase_forty_hex_digit_token_is_caught(self):
        self.put("notes.txt", "ABCDEF0123" * 4)
        self.assertEqual(self.reasons(), ["40-hex-digit token"])

    def test_hex_runs_of_other_lengths_are_not_flagged(self):
        self.put("notes.txt", "a" * 39 + "\n" + "b" * 41 + "\n" + "0123456789abcdef" * 2 + "\n")
        self.assertEqual(scan_tree(self.root), [])

    def test_secret_assignments_are_caught(self):
        for line in ('password = "hunter2"', "api_key = 'abcDEF'", 'config = {"api_key": "abc"}',  # secret-allow: fixture
                     "secret='xyz'", 'PASSWORD: "pw"', "api_key = abcdef", "  password: swordfish",
                     'db_password = "x"', 'client_secret = "x"'):
            with self.subTest(line=line):
                self.put("mod.py", line + "\n")
                findings = scan_tree(self.root)
                self.assertEqual(len(findings), 1, line)
                self.assertIn("secret-looking assignment at line 1", findings[0][1])

    def test_secret_assignment_reports_its_line_number(self):
        self.put("mod.py", "a = 1\nb = 2\npassword = \"x\"\n")  # secret-allow: scanner fixture, not a secret
        self.assertEqual(scan_tree(self.root), [("mod.py", "secret-looking assignment at line 3")])

    def test_allow_marker_silences_only_its_own_line(self):
        self.put("mod.py", 'NAMES = {"api_key": "ODOO_KPI_API_KEY"}  # secret-allow: env var name\n'
                           'password = "still-flagged"\n')
        self.assertEqual(scan_tree(self.root), [("mod.py", "secret-looking assignment at line 2")])

    def test_empty_or_placeholder_values_are_not_flagged(self):
        self.put("mod.py", 'password = ""\napi_key = <your key>\nif x.password:\n'
                           '    raise ConfigError("no password given")\n')
        self.assertEqual(scan_tree(self.root), [])

    def test_tests_folder_and_git_and_pycache_are_skipped(self):
        self.put("tests/fixture.xlsx", binary=b"PK")
        self.put("tests/t.py", 'password = "x"\n')  # secret-allow: scanner fixture, not a secret
        self.put(".git/config", 'url = https://x.' + COMPANY_DOMAIN + '\n')
        self.put("scripts/__pycache__/m.cpython-312.pyc", binary=UAT_LABEL.encode())
        self.assertEqual(scan_tree(self.root), [])

    def test_a_nested_folder_called_tests_is_not_skipped(self):
        self.put("scripts/tests/leak.xlsx", binary=b"PK")
        self.assertEqual(scan_tree(self.root), [("scripts/tests/leak.xlsx", "forbidden file type")])

    def test_binary_content_does_not_crash_the_scanner(self):
        self.put("blob.bin", binary=bytes(range(256)) * 4)
        self.assertEqual(scan_tree(self.root), [])

    def test_several_violations_are_all_reported(self):
        self.put("a.xlsx", binary=b"PK")
        self.put("b.txt", UAT_LABEL + "\n" + "f" * 40 + "\npassword = 'x'\n")  # secret-allow: scanner fixture
        self.assertEqual(self.reasons(), ["40-hex-digit token", "forbidden file type", "forbidden text: " + UAT_LABEL,
                                          "secret-looking assignment at line 3"])

    def test_the_tests_folder_does_not_spell_the_forbidden_words_either(self):
        # the secret / hex scan skips tests/ (fixtures are full of placeholders) but the identifiers must not ship there
        self.assertEqual(scan_words(REPO / "tests", REPO), [])

    def test_the_tests_folder_holds_no_excel_or_spreadsheet_json_either(self):
        # the tests build their spreadsheets in Python; a real customer file must not hide in the folder the tree scan skips
        self.assertEqual(forbidden_files(REPO / "tests", REPO), [])
        with tempfile.TemporaryDirectory(prefix="kpi-hygiene-files-") as tmp:
            root = Path(tmp)
            for rel in ("tests/a.xlsx", "tests/deep/B.XLSX", "tests/x/dash.osheet.json", "tests/ok.json",
                        "tests/plain.py", "tests/xlsx.txt"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_bytes(b"PK")
            self.assertEqual(forbidden_files(root / "tests", root),
                             ["tests/a.xlsx", "tests/deep/B.XLSX", "tests/x/dash.osheet.json"])

    def test_the_tests_folder_scan_really_reads_the_test_files(self):
        # not vacuous: the scan visits this very file (which holds the fragments, not the words) and the fixtures
        visited = [p for p in (REPO / "tests").rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        self.assertGreater(len(visited), 20)
        self.assertIn(Path(__file__).resolve(), [p.resolve() for p in visited])
        self.assertTrue(any(p.suffix == ".js" for p in visited))      # not only Python files


class ForbiddenWordScanTests(unittest.TestCase):
    """The scan of `tests/` (and of the rest of the tree) for the words of the denylist, on temporary trees."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="kpi-hygiene-words-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / "tests"

    def put(self, rel, data):
        path = self.folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def found(self):
        return scan_words(self.folder, self.root)

    def test_the_denylist_is_built_from_fragments_and_is_not_vacuous(self):
        self.assertEqual(len(FORBIDDEN_TEXT), 3)
        for term in FORBIDDEN_TEXT:
            # the scan lower-cases the text: a term with a capital letter could never match
            self.assertEqual(term, term.lower())
            self.assertTrue(term.isascii() and len(term) >= 8, term)
        source = Path(__file__).read_text(encoding="utf-8")
        for term in FORBIDDEN_TEXT:
            self.assertNotIn(term, source.lower())           # this file does not spell what it forbids

    def test_every_word_is_found_in_any_kind_of_file_and_folder(self):
        for index, term in enumerate(FORBIDDEN_TEXT):
            for name in ("t.py", "data.json", "notes.txt", "client.js", "deep/er/x.md", ".hidden", "noext", "a b/c d.csv"):
                with self.subTest(term=term, name=name):
                    path = self.put(name, "line one\nsee %s here\n" % term)
                    self.assertEqual(self.found(), [(path.relative_to(self.root).as_posix(), term)])
                    path.unlink()

    def test_the_match_ignores_case_and_line_endings_and_position(self):
        term = FORBIDDEN_TEXT[0]
        for label, text in (("upper", term.upper()), ("title", term.title()), ("mixed", term[:3].upper() + term[3:]),
                            ("start", term + " first"), ("end without a newline", "last " + term),
                            ("crlf", "a\r\nb " + term + "\r\nc"), ("alone", term), ("glued", "x" + term + "y"),
                            ("in a url", "https://host." + term + "/path"), ("tabs", "\t" + term + "\t")):
            with self.subTest(label=label):
                path = self.put("t.txt", text)
                self.assertEqual([t for _, t in self.found()], [term])
                path.unlink()
        # the Kelvin sign lower-cases to `k`: a spelling with it is the same word
        label = FORBIDDEN_TEXT[1]
        self.assertIn("k", label)
        self.put("t.txt", label.replace("k", "\N{KELVIN SIGN}").upper())
        self.assertEqual([t for _, t in self.found()], [label])

    def test_the_words_are_found_inside_binary_data_and_utf16_text(self):
        term = FORBIDDEN_TEXT[2]
        for label, data in (("binary", bytes(range(256)) + term.encode() + bytes(range(255, 0, -1))),
                            ("binary upper", b"\x00\xff" + term.upper().encode() + b"\x01"),
                            ("utf-16 le", term.encode("utf-16-le")), ("utf-16 le upper", term.upper().encode("utf-16-le")),
                            ("utf-16 be", term.encode("utf-16-be")), ("utf-16 with bom", term.encode("utf-16")),
                            ("zip like", b"PK\x03\x04" + b"\x00" * 30 + term.encode() + b"PK\x05\x06")):
            with self.subTest(label=label):
                path = self.put("fixture.bin", data)
                self.assertEqual([t for _, t in self.found()], [term])
                path.unlink()

    def test_all_the_words_of_one_file_are_reported_and_clean_files_are_not(self):
        self.put("clean.py", "VALUE = 1  # nothing to see\nname = 'dksk'\nvendor = 'autonsi'\nsuffix = '.cloud'\n")
        self.put("dirty.txt", " ".join(FORBIDDEN_TEXT))
        self.assertEqual(sorted(self.found()), sorted(("tests/dirty.txt", term) for term in FORBIDDEN_TEXT))

    def test_a_word_split_by_a_line_break_or_a_quote_is_not_a_word(self):
        # the fragments of the denylist itself: the scan cannot (and must not) flag a joined-by-code spelling
        company, label, login = FORBIDDEN_TEXT
        text = 'A = "%s" + "%s"\nB = "%s"\n  "%s"\nC = "%s" "%s"\n' % (
            company[:7], company[7:], label[:3], label[3:], login[:6], login[6:])
        self.put("fragments.py", text)
        self.assertEqual(self.found(), [])

    def test_this_very_file_is_clean_for_the_scan(self):
        self.put("test_repo_hygiene.py", Path(__file__).read_bytes())
        self.assertEqual(self.found(), [])

    def test_pycache_is_skipped_and_other_cache_like_names_are_not(self):
        self.put("__pycache__/m.cpython-312.pyc", FORBIDDEN_TEXT[1].encode())
        self.assertEqual(self.found(), [])
        self.put("not__pycache__/m.txt", FORBIDDEN_TEXT[1])
        self.put("pycache/m.txt", FORBIDDEN_TEXT[1])
        self.assertEqual(sorted(path for path, _ in self.found()), ["tests/not__pycache__/m.txt", "tests/pycache/m.txt"])

    def test_the_tree_scan_finds_the_words_in_binary_and_utf16_files_too(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "a.bin").write_bytes(b"\x00" + FORBIDDEN_TEXT[0].upper().encode() + b"\x00")
        (self.root / "docs" / "b.txt").write_bytes(FORBIDDEN_TEXT[1].encode("utf-16-le"))
        self.assertEqual(sorted(scan_tree(self.root)), [("docs/a.bin", "forbidden text: " + FORBIDDEN_TEXT[0]),
                                                       ("docs/b.txt", "forbidden text: " + FORBIDDEN_TEXT[1])])


class GitignoreTests(unittest.TestCase):
    """Combined security review M1: the files the workflow writes (the baseline holds the customer's cell text) are
    ignored by git, and nothing of that kind sits in the tree."""
    GITIGNORE = (REPO / ".gitignore").read_text(encoding="utf-8")
    OUTPUT_NAMES = ("baseline.json", "baseline_2026.json", "baseline-v2.json", "baseline.final.json", "dashboard.json",
                    "dashboard_v3.json", "dashboard-qaqc.json", "kpi_spec.json", "kpi_spec_a.json", "kpi_spec-1.json",
                    # appended (a test slices this tuple at [:3] and [3:]): the names of the reconcile / scan / reuse files
                    "reconcile_report.json", "reconcile.json", "report.spec.json", "report.v2.spec.json",
                    "kpi_spec.draft.json", "report.draft.json", "aliases.json", "aliases_vi.json",
                    # combined review G1 (appended again): an aliases file the user named freely
                    "my_aliases.json", "user-aliases-2.json", "x.aliases.json")
    SHIPPED_NAMES = ("catalog.json", "client_functions.json", "plugin.json", "marketplace.json", "README.md",
                     "run-tests.sh", "SKILL.md", "bind_kpi.py", "dashboard_fixtures.py", "test_publish_dashboard.py",
                     "baseline.py", "my_baseline.json", "spec_baseline.json", "kpi.json", "kpi_specs.txt",
                     # decoys for the new patterns: a script, a test and a fixture whose names merely CONTAIN the words
                     "reconcile_kpi.py", "test_reconcile_kpi.py", "reconcile_fixtures.py", "scan_excel.py",
                     "spec.json", "draft.md", "my_aliases.py", "aliases.py", "spec_json.py",
                     # decoys for `*aliases*.json` and the design-file patterns (G1): other types, other words
                     "my_aliases.txt", "aliases.md", "docs.md", "pdf_notes.md", "xls_notes.md", "docx_reader.py",
                     "pptx.md", "readme.pdf.md", "notes.xs", "scan_aliases.py", "test_catalog_aliases.py")
    DESIGN_NAMES = ("design.pdf", "Slides.pptx", "notes.docx", "book.xls", "book.xlsx", "book.xlsm", "book.xlsb",
                    "customer report.pdf", "a.b.c.docx", "design.v2.pdf")

    def test_the_gitignore_names_the_workflow_outputs_and_the_older_entries_stay(self):
        lines = [line.strip() for line in self.GITIGNORE.splitlines()]
        for pattern in WORKFLOW_OUTPUT_PATTERNS:
            self.assertEqual(lines.count(pattern), 1, pattern)
        for pattern in ("*.xlsx", "*.osheet.json", "config.ini", "config.ini.*", ".env", "*.key", "*.pem", "*.pyc",
                        "__pycache__/"):
            self.assertIn(pattern, lines)

    def test_every_workflow_output_name_is_ignored_and_no_shipped_name_is(self):
        patterns = gitignore_patterns(self.GITIGNORE)
        self.assertEqual([n for n in self.OUTPUT_NAMES if not is_ignored(n, patterns)], [])
        self.assertEqual([n for n in self.SHIPPED_NAMES if is_ignored(n, patterns)], [])

    def test_git_agrees_with_the_patterns_when_the_repository_has_a_git_folder(self):
        if not shutil.which("git") or not (REPO / ".git").exists():
            self.skipTest("needs git and a .git folder (the plugin may be checked out without one)")
        names = ["scripts/" + n for n in self.OUTPUT_NAMES[:3]] + ["x/" + n for n in self.OUTPUT_NAMES[3:]]
        for name, expected in [(n, True) for n in names] + [("scripts/" + n, False) for n in
                                                           ("catalog.json", "client_functions.json")]:
            with self.subTest(name=name):
                done = subprocess.run(["git", "check-ignore", "--no-index", "-q", name], cwd=str(REPO),
                                      capture_output=True, timeout=60)
                self.assertEqual(done.returncode, 0 if expected else 1, done.stderr)

    def test_no_workflow_output_sits_in_the_repository_tree(self):
        self.assertEqual(workflow_outputs(REPO), [])

    def test_the_new_output_patterns_are_appended_to_the_three_older_ones_and_all_are_in_the_gitignore(self):
        self.assertEqual(WORKFLOW_OUTPUT_PATTERNS[:3], ("baseline*.json", "dashboard*.json", "kpi_spec*.json"))
        self.assertEqual(WORKFLOW_OUTPUT_PATTERNS[3:], ("reconcile*.json", "*.spec.json", "*.draft.json",
                                                        "aliases*.json", "*aliases*.json"))
        self.assertEqual(self.OUTPUT_NAMES[:3], ("baseline.json", "baseline_2026.json", "baseline-v2.json"))
        lines = [line.strip() for line in self.GITIGNORE.splitlines()]
        for pattern in WORKFLOW_OUTPUT_PATTERNS:
            self.assertEqual(lines.count(pattern), 1, pattern)

    def test_each_new_pattern_ignores_its_own_names_and_only_them(self):
        patterns = gitignore_patterns(self.GITIGNORE)
        for name in ("reconcile_report.json", "reconcile-2026.json", "kpi.spec.json", "kpi.v2.spec.json",
                     "kpi_spec.draft.json", "x.draft.json", "aliases.json", "aliases_my.json",
                     "my_aliases.json"):                                # G1: ignored on purpose since the review
            with self.subTest(name=name):
                self.assertTrue(is_ignored(name, patterns))
        for name in ("reconcile_kpi.py", "spec.json", "draft.md", "aliases.py", "my_aliases.txt", "catalog.json",
                     "specs.json", "kpi.spec.txt", "docs.md", "bind_kpi.py", "README.md"):
            with self.subTest(name=name):
                self.assertFalse(is_ignored(name, patterns))

    def test_the_aliases_pattern_catches_the_word_anywhere_in_a_json_name_and_nothing_else(self):
        patterns = gitignore_patterns(self.GITIGNORE)
        self.assertEqual(len([p for p in patterns if p == "*aliases*.json"]), 1)
        for name in ("my_aliases.json", "user-aliases-2.json", "x.aliases.json", "aliases_vi.json", "MYaliases.json",
                     "customer aliases.json"):
            with self.subTest(ignored=name):
                self.assertTrue(is_ignored(name, patterns))
        for name in ("aliases.py", "my_aliases.txt", "aliases.md", "scan_aliases.py", "test_catalog_aliases.py",
                     "alias.json", "my_alias.json", "aliases", "catalog.json"):
            with self.subTest(kept=name):
                self.assertFalse(is_ignored(name, patterns))

    def test_the_design_file_patterns_are_in_the_gitignore_once_each_and_ignore_their_own_names_only(self):
        lines = [line.strip() for line in self.GITIGNORE.splitlines()]
        self.assertEqual(DESIGN_FILE_PATTERNS, ("*.xls*", "*.pdf", "*.pptx", "*.docx"))
        for pattern in DESIGN_FILE_PATTERNS:
            self.assertEqual(lines.count(pattern), 1, pattern)
        patterns = gitignore_patterns(self.GITIGNORE)
        self.assertEqual([name for name in self.DESIGN_NAMES if not is_ignored(name, patterns)], [])
        self.assertEqual([name for name in self.SHIPPED_NAMES if is_ignored(name, patterns)], [])
        for name in ("design.pdf.md", "pdf", "docx", "pptx.py", "book.xl", "book.xs", "xls", "notes.doc", "deck.ppt"):
            with self.subTest(kept=name):
                self.assertFalse(is_ignored(name, patterns))

    def test_each_design_pattern_matches_only_its_own_extension(self):
        patterns = gitignore_patterns(self.GITIGNORE)
        for pattern, names, others in (("*.pdf", ("a.pdf", "b.v2.pdf"), ("a.pdfx", "a.pdf.md")),
                                       ("*.pptx", ("a.pptx",), ("a.ppt", "a.pptx.md")),
                                       ("*.docx", ("a.docx",), ("a.doc", "a.docx.md")),
                                       ("*.xls*", ("a.xls", "a.xlsx", "a.xlsm", "a.xlsb", "a.xlsx.bak2"), ("a.xl", "xls"))):
            for name in names:
                with self.subTest(pattern=pattern, ignored=name):
                    self.assertTrue(fnmatch.fnmatchcase(name, pattern))
                    self.assertTrue(is_ignored(name, patterns))
            for name in others:
                with self.subTest(pattern=pattern, kept=name):
                    self.assertFalse(fnmatch.fnmatchcase(name, pattern))

    def test_git_agrees_about_the_new_design_and_aliases_names(self):
        if not shutil.which("git") or not (REPO / ".git").exists():
            self.skipTest("needs git and a .git folder (the plugin may be checked out without one)")
        for name, expected in ([("work/" + n, True) for n in self.DESIGN_NAMES]
                               + [("work/my_aliases.json", True), ("work/x.aliases.json", True),
                                  ("work/aliases.py", False), ("work/my_aliases.txt", False), ("work/docs.md", False),
                                  ("work/readme.pdf.md", False)]):
            with self.subTest(name=name):
                done = subprocess.run(["git", "check-ignore", "--no-index", "-q", name], cwd=str(REPO),
                                      capture_output=True, timeout=60)
                self.assertEqual(done.returncode, 0 if expected else 1, done.stderr)

    def test_the_repository_itself_holds_no_design_document_spreadsheet_or_aliases_file(self):
        # the patterns above hide such files from git; this proves the tree (tests/ included) never carried one
        self.assertEqual(workflow_outputs(REPO, DESIGN_FILE_PATTERNS), [])
        self.assertEqual([name for name in workflow_outputs(REPO) if "aliases" in name.lower()], [])
        self.assertEqual(scan_tree(REPO), [])

    def test_the_design_file_finder_finds_every_type_at_any_depth_and_in_any_case_and_only_those(self):
        with tempfile.TemporaryDirectory(prefix="kpi-hygiene-design-") as tmp:
            root = Path(tmp)
            for rel in ("a.pdf", "x/B.PPTX", "tests/c.docx", "d.xlsm", "e.XLS", "__pycache__/f.pdf", "notes.md",
                        "design.pdf.md", "scripts/bind_kpi.py", "catalog.json", "my_aliases.json"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("x")
            self.assertEqual(workflow_outputs(root, DESIGN_FILE_PATTERNS),
                             ["a.pdf", "d.xlsm", "e.XLS", "tests/c.docx", "x/B.PPTX"])
            self.assertEqual(workflow_outputs(root), ["my_aliases.json"])

    def test_the_output_finder_knows_the_new_names_too(self):
        with tempfile.TemporaryDirectory(prefix="kpi-hygiene-new-") as tmp:
            root = Path(tmp)
            for rel in ("reconcile_report.json", "work/Report.V2.SPEC.JSON", "tests/kpi_spec.draft.json",
                        "x/aliases_vi.json", "scripts/reconcile_kpi.py", "tests/reconcile_fixtures.py", "catalog.json",
                        "notes/spec.json", "__pycache__/aliases.json"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("{}")
            self.assertEqual(workflow_outputs(root), ["reconcile_report.json", "tests/kpi_spec.draft.json",
                                                      "work/Report.V2.SPEC.JSON", "x/aliases_vi.json"])

    def test_the_output_finder_finds_them_at_any_depth_in_any_case_and_only_them(self):
        with tempfile.TemporaryDirectory(prefix="kpi-hygiene-out-") as tmp:
            root = Path(tmp)
            for rel in ("baseline.json", "a/b/Dashboard_v2.JSON", "tests/kpi_spec-1.json", ".hid/baseline.json",
                        "catalog.json", "scripts/client_functions.json", "baselines.txt", "mybaseline.json",
                        "__pycache__/baseline.json", "dashboard_fixtures.py"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("{}")
            self.assertEqual(workflow_outputs(root), [".hid/baseline.json", "a/b/Dashboard_v2.JSON", "baseline.json",
                                                      "tests/kpi_spec-1.json"])


class RepoSlugExemptionTests(unittest.TestCase):
    """The READMEs name the GitHub repository in the install line, so the slug (the owner login + the repository name)
    is the ONLY allowed spelling of the login, and only in UTF-8 text. Everything else stays forbidden.
    The slug is always built from `REPO_SLUG` / fragments here: this folder must not spell it either."""
    SLUG = REPO_SLUG
    LOGIN = DEV_LOGIN
    LOGIN_REASON = "forbidden text: " + DEV_LOGIN

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="kpi-hygiene-slug-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def put(self, rel, data):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def reasons(self):
        return sorted(reason for _, reason in scan_tree(self.root))

    def clear(self):
        for path in sorted(self.root.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()

    def assert_flagged(self, label, data, reason=None):
        with self.subTest(label=label):
            self.clear()
            self.put("README.md", data)
            self.assertIn(reason or self.LOGIN_REASON, self.reasons())

    def assert_allowed(self, label, data):
        with self.subTest(label=label):
            self.clear()
            self.put("README.md", data)
            self.assertEqual(scan_tree(self.root), [])
            self.assertEqual(scan_words(self.root, self.root), [])

    # ------------------------------------------------------------------ what is allowed

    def test_the_slug_is_fragment_built_and_the_source_does_not_spell_it(self):
        self.assertEqual(REPO_SLUG, REPO_SLUG.lower())
        self.assertTrue(REPO_SLUG.startswith(DEV_LOGIN + "/") and REPO_SLUG.endswith("/kpi-" + "skills"))
        self.assertNotIn(REPO_SLUG, Path(__file__).read_text(encoding="utf-8").lower())
        self.assertEqual(scan_words(REPO / "tests", REPO), [])

    def test_the_install_line_and_the_usual_spellings_of_the_slug_are_allowed(self):
        slug = self.SLUG
        for label, text in (("install line", "    /plugin marketplace add %s\n    /plugin install odoo-kpi-skills@odoo-kpi\n" % slug),
                            ("indented fence", "   ```\n   /plugin marketplace add %s\n   ```\n" % slug),
                            ("backticks", "the repo `%s` holds it" % slug),
                            ("double quotes", 'repo = "%s"' % slug), ("single quotes", "repo = '%s'" % slug),
                            ("parentheses", "(%s)" % slug), ("brackets", "[%s]" % slug),
                            ("https url", "see https://github.com/%s for it" % slug),
                            ("https url with .git", "git clone https://github.com/%s.git\n" % slug),
                            ("url with a path", "https://github.com/%s/issues/1" % slug),
                            ("upper case url", "HTTPS://GITHUB.COM/%s" % slug.upper()),
                            ("ssh url", "git@github.com:%s.git" % slug),
                            ("start of the file", "%s is the repository" % slug),
                            ("end of the file, no newline", "the repository is " + slug),
                            ("alone", slug), ("alone with a newline", slug + "\n"),
                            ("crlf", "a\r\n/plugin marketplace add %s\r\nb\r\n" % slug),
                            ("crlf start and end", slug + "\r\n" + slug),
                            ("upper case", slug.upper()), ("title case", slug.title()),
                            ("mixed case", slug[:5].upper() + slug[5:]),
                            ("comma and dot after it", "%s, %s." % (slug, slug)), ("colon after it", slug + ": the repo"),
                            ("tab around it", "\t" + slug + "\t"),
                            ("twice", "%s and %s" % (slug, slug)), ("in a table cell", "| %s |" % slug)):
            self.assert_allowed(label, text)

    def test_an_allowed_slug_is_blanked_by_a_single_space_so_its_sides_cannot_join(self):
        # nothing in the real denylist can span the slug (its neighbours are never word characters), so a made-up term
        # proves the rule: with an empty replacement "(" + slug + ")" would read "()"
        raw = ("x(" + self.SLUG + ")y").encode("utf-8")
        self.assertFalse(has_term(raw, "()"))
        self.assertFalse(has_term(raw, "x()y"))
        self.assertTrue(has_term(raw, "x( )y"))
        self.assertFalse(has_term(raw, self.SLUG[:6]))            # the slug itself is gone from the searched text
        self.assertTrue(has_term(raw, "x("))                      # the rest of the text is still searched

    def test_the_exemption_is_exactly_the_slug_regex(self):
        self.assertIsNotNone(ALLOWED_SLUG.fullmatch(self.SLUG))
        self.assertIsNotNone(ALLOWED_SLUG.fullmatch(self.SLUG.upper()))
        self.assertIsNone(ALLOWED_SLUG.fullmatch(self.LOGIN + "/other"))
        self.assertTrue(ALLOWED_SLUG.flags & re.IGNORECASE)

    # ------------------------------------------------------------------ what is still forbidden

    def test_the_bare_login_and_the_login_with_another_repository_are_flagged(self):
        login, slug = self.LOGIN, self.SLUG
        for label, text in (("bare", "owner: " + login), ("bare alone", login), ("bare upper", login.upper()),
                            ("other repository", login + "/other-repo"),
                            ("other repository upper", login.upper() + "/OTHER"),
                            ("the repository name only partly", login + "/kpi-" + "skill"),
                            ("another repository named skills", login + "/skills"),
                            ("kpi-skills of somebody else", "other/kpi-" + "skills " + login),
                            ("url of another repository", "https://github.com/" + login + "/dotfiles"),
                            ("profile url", "https://github.com/" + login),
                            ("login then slash then the slug", login + "/" + slug),
                            ("a slug hiding a second bare login", slug + " " + login)):
            self.assert_flagged(label, text)

    def test_a_slug_followed_by_a_word_character_or_a_hyphen_is_flagged(self):
        slug = self.SLUG
        for label, text in (("hyphen word", slug + "-x"), ("letter", slug + "x"), ("digit", slug + "2"),
                            ("underscore", slug + "_x"), ("lone hyphen", slug + "-"), ("hyphen then digits", slug + "-1"),
                            ("upper case suffix", slug.upper() + "X"),
                            ("suffix on a url", "https://github.com/" + slug + "-fork")):
            self.assert_flagged(label, text)

    def test_a_slug_glued_to_something_on_its_left_is_flagged(self):
        login, slug = self.LOGIN, self.SLUG
        for label, text in (("letter", "x" + slug), ("digit", "9" + slug), ("underscore", "_" + slug),
                            ("hyphen", "-" + slug), ("dot", "a." + slug), ("slash", "/" + slug),
                            ("home folder", "/home/" + slug), ("home folder, split slug", "/home/" + login + "/kpi-" + "skills"),
                            ("tilde folder", "~/" + slug), ("windows folder", "C:\\Users\\" + slug),
                            ("backslash", "\\" + slug), ("scheme without a host", "https://" + slug),
                            ("another host", "https://gitlab.com/" + slug), ("example host", "https://example.org/" + slug),
                            ("another dot com host", "https://x.com/" + slug),
                            ("a path that merely ends in a slash", "https://github.com.evil/" + slug),
                            ("github without its dot", "https://githubxcom/" + slug),
                            ("two slugs glued by a slash", slug + "/" + slug),
                            ("upper case, glued", "X" + slug.upper())):
            self.assert_flagged(label, text)

    def test_the_other_forbidden_words_stay_flagged_next_to_and_around_a_slug(self):
        slug, label_uat, company = self.SLUG, UAT_LABEL, COMPANY_DOMAIN
        uat_reason, company_reason = "forbidden text: " + label_uat, "forbidden text: " + company
        for label, text, reason in (("uat label beside a slug", label_uat + " " + slug, uat_reason),
                                    ("uat label after a slug", slug + " " + label_uat, uat_reason),
                                    ("company domain beside a slug", "https://x." + company + " " + slug, company_reason),
                                    ("company domain right after a slug", slug + "." + company, company_reason),
                                    ("company domain right before a slug", company + " " + slug, company_reason),
                                    ("uat label split by a slug", "nam" + slug + "-dksk", self.LOGIN_REASON),
                                    ("company split by a slug", "autonsi" + slug + ".cloud", self.LOGIN_REASON),
                                    ("uat label, space-joined", "nam " + slug + " -dksk", None)):
            if reason is None:
                # nothing of the label is left once the slug is blanked: the slug alone is allowed there
                self.assert_allowed(label, text)
            else:
                self.assert_flagged(label, text, reason)

    def test_one_bad_occurrence_is_enough_when_good_ones_are_present(self):
        slug = self.SLUG
        for label, text in (("good then glued", "add %s then /home/%s" % (slug, slug)),
                            ("glued then good", "/home/%s then add %s" % (slug, slug)),
                            ("good then suffixed", "%s %s-x" % (slug, slug)),
                            ("good then bare login", "%s and %s" % (slug, self.LOGIN))):
            self.assert_flagged(label, text)

    def test_the_slug_inside_utf16_text_is_flagged(self):
        slug = self.SLUG
        for label, data in (("utf-16 le", slug.encode("utf-16-le")), ("utf-16 be", slug.encode("utf-16-be")),
                            ("utf-16 with bom", slug.encode("utf-16")),
                            ("utf-16 le, install line", ("/plugin marketplace add " + slug + "\n").encode("utf-16-le")),
                            ("utf-16 upper", slug.upper().encode("utf-16-le"))):
            with self.subTest(label=label):
                self.clear()
                self.put("README.md", data)
                self.assertIn(self.LOGIN_REASON, self.reasons())
                self.assertEqual([term for _, term in scan_words(self.root, self.root)], [self.LOGIN])

    def test_the_slug_inside_binary_data_is_flagged(self):
        slug = self.SLUG.encode("utf-8")
        for label, data in (("nul bytes around it", b"\x00" + slug + b"\x00"), ("zip like", b"PK\x03\x04" + b"\x00" * 30 + slug),
                            ("invalid utf-8 byte, no nul", b"\xff " + slug + b" \xfe"),
                            ("all byte values", bytes(range(1, 256)) + b" " + slug + b" ")):
            with self.subTest(label=label):
                self.clear()
                self.put("blob.bin", data)
                self.assertIn(self.LOGIN_REASON, self.reasons())
                self.assertEqual([term for _, term in scan_words(self.root, self.root)], [self.LOGIN])

    def test_a_valid_utf8_page_with_non_ascii_text_still_gets_the_exemption(self):
        # the Vietnamese README: the exemption is for real UTF-8 text, accents included
        self.assert_allowed("vietnamese", "3. Cài plugin:\n   /plugin marketplace add %s\n   Xong rồi.\n" % self.SLUG)

    def test_the_slug_in_a_file_or_folder_name_is_flagged(self):
        login, slug = self.LOGIN, self.SLUG
        for label, rel in (("login as a file name", login + ".txt"), ("slug as one file name", slug.replace("/", "_")),
                           ("slug as two folders", slug + "/page.md"), ("slug as the folder of a file", "docs/" + slug + "/a.md"),
                           ("upper case file name", login.upper() + ".md"),
                           ("login inside a long name", "notes-for-" + login + "-v2.txt")):
            with self.subTest(label=label):
                self.clear()
                self.put(rel, "nothing to see\n")
                self.assertTrue([r for r in self.reasons() if r.startswith("forbidden text in name: ")], rel)
                self.assertEqual([term for _, term in scan_words(self.root, self.root)], [login])

    def test_a_clean_name_is_not_flagged_by_the_name_scan(self):
        self.put("docs/kpi-" + "skills-notes.md", "x\n")
        self.put("namcham-notes.txt", "x\n")                 # only part of the login
        self.assertEqual(scan_tree(self.root), [])
        self.assertEqual(scan_words(self.root, self.root), [])

    def test_the_tests_folder_scan_keeps_flagging_the_login_outside_the_slug(self):
        folder = self.root / "tests"
        folder.mkdir()
        (folder / "a.py").write_text('X = "ok %s"\nY = "%s"\n' % (self.SLUG, self.LOGIN), encoding="utf-8")
        self.assertEqual(scan_words(folder, self.root), [("tests/a.py", self.LOGIN)])


class ReadmeInstallLineTests(unittest.TestCase):
    """The shipped READMEs carry the real install line, not a placeholder."""
    FILES = ("README.md", "README.vi.md")
    INSTALL = re.compile(r"(?m)^[ \t]*/plugin marketplace add " + re.escape(REPO_SLUG) + r"[ \t]*$")

    def test_each_readme_has_the_install_line_with_the_slug(self):
        for name in self.FILES:
            with self.subTest(file=name):
                text = (REPO / name).read_text(encoding="utf-8")
                self.assertEqual(len(self.INSTALL.findall(text)), 1, name)
                self.assertIn("/plugin marketplace add " + REPO_SLUG, text)

    def test_no_owner_or_repo_placeholder_is_left_in_a_readme(self):
        for name in self.FILES:
            with self.subTest(file=name):
                text = (REPO / name).read_text(encoding="utf-8").lower()
                for placeholder in ("<" + "owner" + ">", "<" + "repo" + ">"):
                    self.assertNotIn(placeholder, text)

    def test_the_readmes_pass_the_hygiene_scan_only_through_the_slug_exemption(self):
        for name in self.FILES:
            with self.subTest(file=name):
                raw = (REPO / name).read_bytes()
                self.assertTrue(is_utf8_text(raw))
                self.assertEqual([term for term in FORBIDDEN_TEXT if has_term(raw, term)], [])
                # control: the same page with its slug cut down to the bare login is refused
                broken = raw.replace(REPO_SLUG.encode(), DEV_LOGIN.encode())
                self.assertNotEqual(broken, raw)
                self.assertEqual([term for term in FORBIDDEN_TEXT if has_term(broken, term)], [DEV_LOGIN])


if __name__ == "__main__":
    unittest.main()
