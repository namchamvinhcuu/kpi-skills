"""Plan kpi-multi-project step 4 / AC6: the TEXT of the multi-project feature (pages, skills, command, READMEs) keeps what
the Plan promises.

Like `test_docs_contract.py` these checks are keyword level (squashed lower-case text, no markdown marks): rewording a
sentence keeps them green, dropping or inverting a promise turns them red with a message that names it. What is new here:

* every catalog `catalogs/<p>.json` is PAIRED with a page `skills/kpi-source-catalog/projects/<p>.md` that names every
  source of the catalog and lists no source the catalog does not have (`pairing_problems`, a function, so that planting
  each defect in a temporary folder proves the check goes RED: a green scan of the shipped tree proves nothing alone);
* the generic `kpi-source-catalog` skill names nothing that belongs to a project: the forbidden words are DERIVED from the
  shipped catalogs (project names, source keys, models, selector names / values, measures);
* step 0 of `kpi-builder` and the `/kpi` command: the four `check-config` project lines are taken from the REAL
  `odoo_rpc.project_status` (not retyped here), every scan / bind / reconcile command template carries
  `--project "<project>"`, the name never comes from the Excel;
* the five refusal texts of the catalog code are provoked for real (through `kpi_catalogs` / `bind_kpi.load_catalog` with the
  `CATALOG_DIR` seam) and must be in the troubleshooting list of the skill; the Vietnamese README table pins the first two
  and the two `check-config` refusals;
* both READMEs: `ODOO_KPI_PROJECT` (the name comes from `odoo_rpc.PROJECT_ENV`), the `project =` key, the Projects section
  and its five-point checklist, and nothing of the old `scripts/catalog.json` layout.

Nothing is run against a network; only `git check-ignore` is called (and the test is skipped without git).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from helpers import REPO, IsolatedTestCase, install_catalogs, missing_catalogs, odoo_rpc
from test_docs_contract import (CATALOG_SKILL, COMMAND, NEG, PARSERS, README, README_VI, SKILL, DocCase, commands, gap,
                                hard_rules, known_options, option_value, parse_frontmatter, read, says, section,
                                squash, subparsers_of)
from test_repo_hygiene import FORBIDDEN_TEXT, HEX40, has_term

import bind_kpi  # noqa: E402  (found through the sys.path entry that `helpers` adds)
import kpi_catalogs  # noqa: E402

CATALOGS = REPO / "catalogs"
PAGES = REPO / "skills" / "kpi-source-catalog" / "projects"
PROJECT_SCRIPTS = ("scan_excel", "bind_kpi", "reconcile_kpi")
GIT_ENV = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)


# ---------------------------------------------------------------- the pairing check (a function, proven RED below)

def stems(folder, extension):
    """Sorted names of the regular `<name><extension>` files of `folder`."""
    return sorted(os.path.splitext(entry)[0] for entry in os.listdir(str(folder))
                  if entry.endswith(extension) and os.path.isfile(os.path.join(str(folder), entry)))


def source_keys(catalog_path):
    """The source keys of one catalog file; raises ValueError for a file that is not a catalog with a `sources` object."""
    with open(str(catalog_path), encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        raise ValueError("no sources object")
    return list(data["sources"])


def names_word(text, word):
    """True when `word` stands in `text` as a whole identifier (`production_group_x` does not name `production_group`)."""
    return re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(word), text) is not None


def first_column_identifiers(text):
    """The backticked identifiers in the FIRST column of every markdown table of `text` (header and rule rows skipped)."""
    found = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        first = line.strip().strip("|").split("|")[0]
        if re.fullmatch(r"\s*:?-{3,}:?\s*", first):
            continue
        found += re.findall(r"`([a-z][a-z0-9_]*)`", first)
    return found


def pairing_problems(catalog_dir, page_dir):
    """What is wrong between a catalogs folder and a project pages folder ([] = paired and complete)."""
    problems = []
    catalogs, pages = set(stems(catalog_dir, ".json")), set(stems(page_dir, ".md"))
    for name in sorted(catalogs - pages):
        problems.append("catalogs/%s.json has no page projects/%s.md" % (name, name))
    for name in sorted(pages - catalogs):
        problems.append("projects/%s.md has no catalog catalogs/%s.json" % (name, name))
    for name in sorted(catalogs & pages):
        try:
            keys = source_keys(os.path.join(str(catalog_dir), name + ".json"))
        except (OSError, ValueError) as exc:
            problems.append("catalogs/%s.json cannot be read as a catalog (%s)" % (name, type(exc).__name__))
            continue
        text = read(os.path.join(str(page_dir), name + ".md"))
        problems += ["projects/%s.md does not name the source %r" % (name, key) for key in keys if not names_word(text, key)]
        problems += ["projects/%s.md lists %r in a table but catalogs/%s.json has no such source" % (name, ident, name)
                     for ident in first_column_identifiers(text) if ident not in keys]
    return problems


def git_ignores(root, relative):
    """True / False from `git check-ignore --no-index`; None when git cannot answer (missing, not a repository)."""
    try:
        done = subprocess.run(["git", "check-ignore", "--no-index", "-q", "--", relative], cwd=str(root), env=GIT_ENV,
                              capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {0: True, 1: False}.get(done.returncode)


def forbidden_words(catalog_dir):
    """Every project-specific word of the catalogs in `catalog_dir`: project names, source keys, models, the names
    (with an underscore) and code-like values of the selectors, the measures (with an underscore)."""
    words = set()
    for name in stems(catalog_dir, ".json"):
        words.add(name)
        with open(os.path.join(str(catalog_dir), name + ".json"), encoding="utf-8") as handle:
            sources = json.load(handle)["sources"]
        for key, source in sources.items():
            words.add(key)
            words.add(source["model"])
            for selector, rule in list(source.get("selectors", {}).items()) + list(source.get("optional_filters", {}).items()):
                if "_" in selector and not selector.startswith("_"):
                    words.add(selector)
                for value in rule.get("values", []):
                    if isinstance(value, str) and ("_" in value or re.search(r"[A-Z]", value)):
                        words.add(value)
            for measure in source.get("measures", []):
                if isinstance(measure, str) and "_" in measure and not measure.startswith("_"):
                    words.add(measure)
    return words


def word_hits(text, words):
    """The words (case-insensitive, whole identifiers) of `words` that `text` holds."""
    return sorted(word for word in words if re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(word), text, re.I))


class IsolatedDocCase(IsolatedTestCase, DocCase):
    """A doc test that may redirect the catalogs folder (`install_catalogs` needs `self.tmp`)."""


def project_lines(case):
    """The four `check-config` project line forms, from the REAL `odoo_rpc.project_status` -> {form: fixed prefix}."""
    status = odoo_rpc.project_status
    install_catalogs(case, "alpha")
    one_not_set = status(None)
    install_catalogs(case, "alpha", "beta")
    two_not_set, member, invalid = status(None), status("alpha"), status("zzz")
    missing_catalogs(case)
    unreadable = status("alpha")
    fixed = lambda line: line.split(" (installed:")[0]          # noqa: E731
    return {"member": member.replace("alpha", "<name>"), "not set": fixed(two_not_set), "not set (one)": fixed(one_not_set),
            "invalid": fixed(invalid), "unreadable": unreadable}


def refusals(case):
    """The five refusal texts of the catalog code, provoked for real -> {key: (fixed part, source file)}."""
    found = {}

    def provoke(key, source, call):
        try:
            call()
        except (kpi_catalogs.CatalogError, bind_kpi.BindError) as exc:
            found[key] = (str(exc).split(" (installed:")[0], source)
        else:
            raise AssertionError("%s was not refused" % key)

    install_catalogs(case, "alpha", "beta")
    provoke("several", "kpi_catalogs", lambda: kpi_catalogs.catalog_path(None))
    provoke("not installed", "kpi_catalogs", lambda: kpi_catalogs.catalog_path("zzz"))
    provoke("both flags", "bind_kpi", lambda: bind_kpi.load_catalog(path="x.json", project="alpha"))
    install_catalogs(case)
    provoke("none", "kpi_catalogs", lambda: kpi_catalogs.catalog_path(None))
    missing_catalogs(case)
    provoke("unreadable", "kpi_catalogs", lambda: kpi_catalogs.catalog_path(None))
    return found


# ---------------------------------------------------------------- (1) pairing: the shipped tree and the checker itself

class ShippedPairingTests(DocCase):
    def test_every_catalog_has_a_page_and_every_page_a_catalog_and_the_page_names_each_source(self):
        self.assertGreaterEqual(len(stems(CATALOGS, ".json")), 1)
        self.assertEqual(pairing_problems(CATALOGS, PAGES), [])

    def test_the_two_folders_hold_the_same_project_names(self):
        self.assertEqual(stems(CATALOGS, ".json"), stems(PAGES, ".md"))
        self.assertEqual(sorted(os.listdir(str(PAGES))), [name + ".md" for name in stems(CATALOGS, ".json")],
                         "the pages folder holds something that is not a `<project>.md` page")

    def test_each_page_names_every_source_key_of_its_catalog_as_a_word(self):
        checked = 0
        for name in stems(CATALOGS, ".json"):
            text = read(PAGES / (name + ".md"))
            for key in source_keys(CATALOGS / (name + ".json")):
                with self.subTest(project=name, source=key):
                    self.assertTrue(names_word(text, key), "projects/%s.md does not name the source %r" % (name, key))
                checked += 1
        self.assertGreaterEqual(checked, 6)

    def test_the_first_column_of_each_page_table_lists_only_sources_of_the_catalog(self):
        for name in stems(CATALOGS, ".json"):
            keys = set(source_keys(CATALOGS / (name + ".json")))
            listed = first_column_identifiers(read(PAGES / (name + ".md")))
            with self.subTest(project=name):
                self.assertTrue(listed, "the page has no table with a backticked source in its first column")
                self.assertEqual(sorted(set(listed) - keys), [])
                self.assertEqual(sorted(keys - set(listed)), [], "a source of the catalog is in no table of the page")

    def test_the_catalog_names_and_page_names_pass_the_name_rule_and_no_file_is_ignored_by_git(self):
        for name in stems(CATALOGS, ".json"):
            self.assertTrue(kpi_catalogs.valid_name(name), name)
            for relative in ("catalogs/%s.json" % name, "skills/kpi-source-catalog/projects/%s.md" % name):
                with self.subTest(path=relative):
                    ignored = git_ignores(REPO, relative)
                    if ignored is None:
                        self.skipTest("git cannot answer here (not installed, or the folder is not a repository)")
                    self.assertFalse(ignored, "%s is ignored by .gitignore: it would miss the commit" % relative)


class PairingCheckerIsRedTests(DocCase):
    """The checker flags each defect planted in a temporary pair of folders (so the green scan above means something)."""

    def world(self, catalogs, pages, extra_catalog_files=(), extra_page_files=()):
        folder = tempfile.mkdtemp(prefix="pairing-")
        self.addCleanup(shutil.rmtree, folder, True)
        cat, pag = os.path.join(folder, "catalogs"), os.path.join(folder, "projects")
        os.makedirs(cat)
        os.makedirs(pag)
        for name, keys in catalogs.items():
            with open(os.path.join(cat, name + ".json"), "w", encoding="utf-8") as handle:
                json.dump({"schema": 1, "sources": {key: {} for key in keys}}, handle)
        for name, text in pages.items():
            with open(os.path.join(pag, name + ".md"), "w", encoding="utf-8") as handle:
                handle.write(text)
        for entry in extra_catalog_files:
            open(os.path.join(cat, entry), "w").close()
        for entry in extra_page_files:
            open(os.path.join(pag, entry), "w").close()
        return cat, pag

    GOOD = "# alpha\n\n| source | what |\n|---|---|\n| `line_month` | a |\n| `qc_log` | b |\n\nmore about `line_month`\n"

    def test_a_complete_pair_is_green(self):
        cat, pag = self.world({"alpha": ["line_month", "qc_log"]}, {"alpha": self.GOOD})
        self.assertEqual(pairing_problems(cat, pag), [])

    def test_a_catalog_without_a_page_is_red(self):
        cat, pag = self.world({"alpha": ["line_month", "qc_log"], "beta": ["x_y"]}, {"alpha": self.GOOD})
        self.assertEqual(pairing_problems(cat, pag), ["catalogs/beta.json has no page projects/beta.md"])

    def test_a_page_without_a_catalog_is_red(self):
        cat, pag = self.world({"alpha": ["line_month", "qc_log"]}, {"alpha": self.GOOD, "gamma": "# g\n"})
        self.assertEqual(pairing_problems(cat, pag), ["projects/gamma.md has no catalog catalogs/gamma.json"])

    def test_a_source_the_page_does_not_name_is_red(self):
        cat, pag = self.world({"alpha": ["line_month", "qc_log", "third_one"]}, {"alpha": self.GOOD})
        self.assertEqual(pairing_problems(cat, pag), ["projects/alpha.md does not name the source 'third_one'"])

    def test_a_longer_identifier_does_not_count_as_naming_the_source(self):
        page = self.GOOD.replace("`qc_log`", "`qc_log_extra`")
        cat, pag = self.world({"alpha": ["line_month", "qc_log"]}, {"alpha": page})
        problems = pairing_problems(cat, pag)
        self.assertIn("projects/alpha.md does not name the source 'qc_log'", problems)
        self.assertIn("projects/alpha.md lists 'qc_log_extra' in a table but catalogs/alpha.json has no such source", problems)

    def test_a_stray_source_in_a_table_is_red_but_prose_and_other_columns_are_not_checked(self):
        page = self.GOOD + "| `stray_one` | not in the catalog |\n"
        cat, pag = self.world({"alpha": ["line_month", "qc_log"]}, {"alpha": page})
        self.assertEqual(pairing_problems(cat, pag),
                         ["projects/alpha.md lists 'stray_one' in a table but catalogs/alpha.json has no such source"])
        quiet = self.GOOD + "| `line_month` | mentions `stray_two` in the second column |\n\nand `stray_three` in prose\n"
        cat, pag = self.world({"alpha": ["line_month", "qc_log"]}, {"alpha": quiet})
        self.assertEqual(pairing_problems(cat, pag), [])

    def test_a_catalog_that_is_not_a_catalog_is_reported_not_crashed_on(self):
        cat, pag = self.world({"alpha": ["line_month", "qc_log"]}, {"alpha": self.GOOD})
        with open(os.path.join(cat, "alpha.json"), "w", encoding="utf-8") as handle:
            handle.write("[1, 2]")
        self.assertEqual(pairing_problems(cat, pag), ["catalogs/alpha.json cannot be read as a catalog (ValueError)"])
        with open(os.path.join(cat, "alpha.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(pairing_problems(cat, pag), ["catalogs/alpha.json cannot be read as a catalog (JSONDecodeError)"])

    def test_other_file_kinds_are_not_catalogs_or_pages(self):
        cat, pag = self.world({"alpha": ["line_month", "qc_log"]}, {"alpha": self.GOOD},
                              extra_catalog_files=("notes.txt", "alpha.json.bak"), extra_page_files=("alpha.txt", "alpha.md.orig"))
        self.assertEqual(pairing_problems(cat, pag), [])

    def test_the_table_reader_skips_header_and_rule_rows_and_reads_only_backticked_identifiers(self):
        text = "| source | x |\n|:---|---:|\n| `a_b` / `c_d` | y |\n| plain | z |\n| `Not Snake` | w |\n"
        self.assertEqual(first_column_identifiers(text), ["a_b", "c_d"])

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_the_git_check_reports_an_ignored_catalog_and_a_normal_one(self):
        folder = tempfile.mkdtemp(prefix="gitcheck-")
        self.addCleanup(shutil.rmtree, folder, True)
        subprocess.run(["git", "init", "-q", folder], env=GIT_ENV, check=True, capture_output=True, timeout=30)
        with open(os.path.join(folder, ".gitignore"), "w", encoding="utf-8") as handle:
            handle.write("baseline*.json\n*aliases*.json\n")
        self.assertTrue(git_ignores(folder, "catalogs/baseline-x.json"))
        self.assertTrue(git_ignores(folder, "catalogs/my-aliases.json"))
        self.assertFalse(git_ignores(folder, "catalogs/alpha.json"))
        self.assertFalse(git_ignores(folder, "skills/kpi-source-catalog/projects/alpha.md"))

    def test_the_git_check_answers_none_outside_a_repository(self):
        folder = tempfile.mkdtemp(prefix="nogit-")
        self.addCleanup(shutil.rmtree, folder, True)
        if git_ignores(folder, "x.json") is not None:
            self.skipTest("the temporary folder is inside a git repository on this machine")
        self.assertIsNone(git_ignores(folder, "x.json"))


# ---------------------------------------------------------------- (2) the generic skill names no project

class GenericSkillTests(DocCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(CATALOG_SKILL)
        cls.words = forbidden_words(CATALOGS)

    def test_the_forbidden_words_are_derived_from_every_shipped_catalog(self):
        self.assertGreater(len(self.words), 20)
        for name in stems(CATALOGS, ".json"):
            self.assertIn(name, self.words)
            with open(str(CATALOGS / (name + ".json")), encoding="utf-8") as handle:
                sources = json.load(handle)["sources"]
            for key, source in sources.items():
                self.assertIn(key, self.words)
                self.assertIn(source["model"], self.words)

    def test_the_generic_skill_holds_no_source_key_model_group_code_or_project_name(self):
        self.assertEqual(word_hits(self.text, self.words), [])

    def test_the_check_is_not_vacuous_the_project_page_does_hold_those_words(self):
        for name in stems(CATALOGS, ".json"):
            page_hits = word_hits(read(PAGES / (name + ".md")), self.words)
            with self.subTest(project=name):
                self.assertGreaterEqual(len(page_hits), len(source_keys(CATALOGS / (name + ".json"))))

    def test_the_word_check_flags_each_kind_of_word_and_only_whole_identifiers(self):
        folder = tempfile.mkdtemp(prefix="forbid-")
        self.addCleanup(shutil.rmtree, folder, True)
        catalog = {"schema": 1, "sources": {"qc_log": {"model": "acme.qc.log", "selectors": {"group_code": {"values": ["AB_CD", "plain"]}},
                                                        "measures": ["ng_qty", "__count"]}}}
        with open(os.path.join(folder, "acme.json"), "w", encoding="utf-8") as handle:
            json.dump(catalog, handle)
        words = forbidden_words(folder)
        self.assertEqual(words, {"acme", "qc_log", "acme.qc.log", "group_code", "AB_CD", "ng_qty"})
        for text, hit in (("see qc_log", "qc_log"), ("the ACME page", "acme"), ("model acme.qc.log here", "acme.qc.log"),
                          ("group AB_CD", "AB_CD"), ("`group_code`", "group_code"), ("ng_qty column", "ng_qty")):
            with self.subTest(text=text):
                self.assertIn(hit, word_hits(text, words))
        self.assertEqual(word_hits("qc_logs and xqc_log and acmes and plain and __count", words), [])

    def test_the_frontmatter_name_is_kpi_source_catalog_with_a_description(self):
        fields, problems = parse_frontmatter(self.text)
        self.assertEqual((fields.get("name"), problems), ("kpi-source-catalog", []))
        self.assertTrue(fields.get("description"))

    def test_it_points_at_the_project_page_and_the_project_never_comes_from_the_excel(self):
        self.assertEqual(says(self.text, r"projects/<project>\.md", r"check-config", r"the user's answer",
                              r"project name comes from check-config or from the user's answer",
                              NEG + gap(30) + r"text of the excel"), [])
        # the pointer and the rule sit in the "Which project?" paragraph itself, not only in a later paragraph
        paragraph = re.search(r"\*\*Which project\?\*\*.*?(?:\n\s*\n|\Z)", self.text, re.S)
        self.assertIsNotNone(paragraph, "the generic skill has no `Which project?` paragraph")
        self.assertEqual(says(paragraph.group(0), r"projects/<project>\.md", r"check-config",
                              r"project name comes from check-config or from the user's answer",
                              NEG + gap(30) + r"text of the excel"), [])

    def test_a_users_answer_counts_only_when_it_equals_a_name_check_config_printed_character_for_character(self):
        paragraph = re.search(r"\*\*Which project\?\*\*.*?(?:\n\s*\n|\Z)", self.text, re.S)
        self.assertIsNotNone(paragraph, "the generic skill has no `Which project?` paragraph")
        for where, text in (("paragraph", paragraph.group(0)), ("skill", self.text)):
            with self.subTest(where=where):
                self.assertEqual(says(text, r"an answer counts only when it equals, character for character, one of the names check-config printed",
                                      NEG + gap(30) + r"text of the excel or of a document"), [])

    def test_it_says_what_a_catalog_holds_without_naming_a_project(self):
        self.assertEqual(says(self.text, r"list sources", r"pivot sources", r"aliases", r"readability", r"--project",
                              r"(?:per-project|per project) pages"), [])


# ---------------------------------------------------------------- (3) step 0 of the skill and the command

class StepZeroContractTests(IsolatedDocCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(SKILL)
        cls.step0 = section(cls.text, r"preflight")[1]
        cls.flat0 = squash(cls.step0)

    def test_the_four_check_config_lines_come_from_the_real_code_and_are_all_in_step_zero(self):
        forms = project_lines(self)
        self.assertEqual(forms["member"], "project: <name>")
        self.assertEqual(forms["not set"], "project: not set")
        self.assertEqual(forms["not set"], forms["not set (one)"])
        self.assertEqual(forms["invalid"], "project: invalid")
        self.assertEqual(forms["unreadable"], "catalogs: unreadable")
        self.assertEqual(len(set(forms.values())), 4)
        for form, prefix in forms.items():
            with self.subTest(form=form):
                self.assertIn(prefix, self.flat0)
        for prefix in (forms["not set"], forms["invalid"]):
            self.assertRegex(self.flat0, re.escape(prefix) + r" \(installed: ", "the list of installed names is part of the line")
        self.assertRegex(self.flat0, r"only these four forms exist")

    def test_one_catalog_and_not_set_passes_nothing(self):
        self.assertEqual(says(self.step0, r"one catalog installed" + gap(40) + r"not set" + gap(20) + r"pass nothing"), [])

    def test_a_named_project_with_two_or_more_catalogs_is_confirmed_with_the_user_first(self):
        self.assertEqual(says(self.step0, r"project: <name> and two or more catalogs installed" + gap(250) +
                              r"ask them to confirm before you use it", r"tell the user the name you read"), [])
        self.assertIn(odoo_rpc.PROJECT_ENV.lower(), self.flat0)               # the silent override is the reason
        self.assertRegex(self.flat0, r"overrides the file")

    def test_not_set_with_several_or_invalid_asks_the_user_and_says_to_add_the_project_line(self):
        self.assertEqual(says(self.step0, r"not set with two or more installed, or invalid: ask the user which of the printed names is theirs",
                              r"add the line project = <name> to their config file", r"init-config again",
                              NEG + r" read the config file yourself"), [])

    def test_an_unreadable_catalogs_folder_stops_the_work(self):
        self.assertEqual(says(self.step0, r"catalogs: unreadable" + gap(80) + r"stop and tell the user"), [])

    def test_the_name_comes_only_from_check_config_or_the_user_never_from_the_excel_a_document_or_a_design_file(self):
        self.assertEqual(says(self.step0, r"project name comes only from check-config or from the user's answer",
                              r"never from the excel" + gap(80) + r"design file", r"\(hard rule 7\)"), [])
        self.assertNotRegex(self.flat0, r"project name comes from the excel")
        rule7 = [rule for rule in hard_rules(self.text) if rule.startswith("7.")]
        self.assertEqual(says(rule7[0], r"file and document names", r"the text of a design document"), [])

    def test_a_typed_name_is_used_only_when_it_equals_a_printed_name_character_for_character_and_no_other_text_reaches_a_command_or_path(self):
        # Review finding: a typed answer that merely looks like a name must never be spliced into a command or a file path
        self.assertEqual(says(self.step0,
                              r"use a name the user typed only when it equals, character for character, one of the printed names",
                              r"otherwise ask again",
                              NEG + r" put any other text into a command or a file path"), [])
        self.assertNotRegex(self.flat0, r"close enough|approximately|ignoring case|case-insensitive")

    def test_the_step_tells_which_three_scripts_get_the_project_and_that_the_name_is_the_same_all_session(self):
        self.assertEqual(says(self.step0, r'pass --project "<project>" to scan_excel\.py, bind_kpi\.py and reconcile_kpi\.py',
                              r"the same name for the whole session"), [])

    def test_the_three_scripts_of_the_skill_accept_the_project_option(self):
        for script in PROJECT_SCRIPTS:
            parser = PARSERS[script]()
            options = {option for action in parser._actions for option in action.option_strings}
            with self.subTest(script=script):
                self.assertIn("--project", options)
        for script in set(PARSERS) - set(PROJECT_SCRIPTS):
            parser = PARSERS[script]()
            options = {option for each in [parser] + list(subparsers_of(parser).values())
                       for action in each._actions for option in action.option_strings}
            with self.subTest(script=script):
                self.assertNotIn("--project", options, "the skill would have to pass it there too")

    def test_every_scan_bind_and_reconcile_command_template_carries_the_quoted_project_placeholder(self):
        found = {}
        for position, script, root, words in commands(self.text):
            if script in PROJECT_SCRIPTS:
                found.setdefault(script, []).append(words)
                with self.subTest(script=script, at=position):
                    self.assertEqual(option_value(words, "--project"), "<project>")
                    self.assertNotIn("--catalog", words)
        self.assertEqual(sorted(found), sorted(PROJECT_SCRIPTS), "a script has no command template in the skill")
        lines = [line for line in self.text.splitlines()
                 if re.search(r"python\s+\S*scripts/(?:%s)\.py" % "|".join(PROJECT_SCRIPTS), line)]
        self.assertGreaterEqual(len(lines), 3)
        for line in lines:
            with self.subTest(line=line[:60]):
                self.assertIn('--project "<project>"', line)
                self.assertNotIn("--catalog", line)

    def test_no_other_command_template_passes_a_project_and_no_doc_command_passes_a_catalog_or_a_literal_project(self):
        for script_path in (SKILL, COMMAND, README, README_VI):
            for position, script, root, words in commands(read(script_path)):
                with self.subTest(file=script_path.name, script=script):
                    self.assertNotIn("--catalog", words)
                    if "--project" in words:
                        self.assertIn(script, PROJECT_SCRIPTS)
                        value = option_value(words, "--project")
                        self.assertTrue(value.startswith("<") and value.endswith(">"), "a literal project %r" % value)

    def test_the_command_mentions_the_project_line_confirms_it_and_never_takes_it_from_the_excel(self):
        body = read(COMMAND).split("\n---\n", 1)[1]
        self.assertEqual(says(body, r"project: line of check-config", r"which source catalog",
                              r"several installed" + gap(20) + r"confirm it with the user", r"never take it from the excel"), [])
        flat = squash(body)
        self.assertLess(flat.index("preflight"), flat.index("project: line"))
        self.assertLess(flat.index("project: line"), flat.index("intake"))

    def test_troubleshooting_names_the_project_refusals_the_plain_python_rule_and_never_guess(self):
        body = section(self.text, r"troubleshooting")[1]
        self.assertEqual(says(body, r"no module named 'odoo_rpc' \(or 'kpi_catalogs'\)", r"never guess, never take it from the excel",
                              r"plugin folder is damaged", r"hard rule 7: you never pass --catalog"), [])


# ---------------------------------------------------------------- (4) the refusal texts

class RefusalLiteralTests(IsolatedDocCase):
    DOC_FORM = {"none": "no source catalog is installed"}                # the skill shortens this one (a true prefix)

    @classmethod
    def setUpClass(cls):
        cls.skill = squash(section(read(SKILL), r"troubleshooting")[1])
        cls.vi = read(README_VI)

    def test_the_five_texts_are_provoked_for_real_and_are_what_the_code_says(self):
        found = refusals(self)
        self.assertEqual(sorted(found), ["both flags", "none", "not installed", "several", "unreadable"])
        self.assertEqual(found["several"][0], "several source catalogs are installed: pass --project <name>")
        self.assertEqual(found["not installed"][0], "that project is not one of the installed source catalogs")
        self.assertEqual(found["both flags"][0], "--project and --catalog cannot be used together")
        self.assertEqual(found["none"][0], "no source catalog is installed in the catalogs folder")
        self.assertEqual(found["unreadable"][0], "the catalogs folder cannot be read")
        for key, (literal, module) in found.items():
            with self.subTest(refusal=key):
                self.assertIn(literal, read(REPO / "scripts" / (module + ".py")))

    def test_each_refusal_is_in_the_troubleshooting_list_of_the_skill(self):
        for key, (literal, _) in refusals(self).items():
            shown = self.DOC_FORM.get(key, literal)
            with self.subTest(refusal=key):
                self.assertTrue(literal.startswith(shown), "the skill may only shorten the END of a message")
                self.assertGreaterEqual(len(shown.split()), 5)
                self.assertIn(shown.lower(), self.skill)

    def test_the_vietnamese_table_pins_the_first_two_refusals_and_the_two_check_config_lines(self):
        found, forms = refusals(self), project_lines(self)
        table = section(self.vi, r"lỗi thường gặp")
        self.assertIsNotNone(table)
        rows = [line for line in table[1].splitlines() if line.startswith("|")]
        wanted = [found["several"][0], found["not installed"][0], forms["invalid"], forms["unreadable"]]
        for literal in wanted:
            matching = [row for row in rows if literal in row]
            with self.subTest(literal=literal):
                self.assertEqual(len(matching), 1, "README.vi.md error table: %r listed %d times" % (literal, len(matching)))
                cells = [cell.strip() for cell in matching[0].strip().strip("|").split("|")]
                self.assertEqual(len(cells), 3, matching[0])
                self.assertTrue(all(cells), "an empty cell in the row of %r" % literal)
                self.assertIn(literal, cells[0])
                self.assertIn(literal.lower(), squash(self.vi))
        self.assertIn("(installed: ...)", [row for row in rows if forms["invalid"] in row][0])

    def test_the_vietnamese_rows_tell_the_user_what_to_do(self):
        rows = [line for line in section(self.vi, r"lỗi thường gặp")[1].splitlines()
                if line.startswith("|") and ("--project" in line or "project: invalid" in line)]
        self.assertEqual(len(rows), 2)
        flat = squash(" ".join(rows))
        for pattern in (r"project = <tên>", r"cho claude biết tên dự án", r"sửa dòng project =", r"cài lại plugin"):
            with self.subTest(pattern=pattern):
                self.assertRegex(flat, pattern)


# ---------------------------------------------------------------- (5) the READMEs

class ReadmeProjectTests(IsolatedDocCase):
    @classmethod
    def setUpClass(cls):
        cls.en, cls.vi = read(README), read(README_VI)
        found = section(cls.en, r"^projects \(one source catalog per odoo project\)")
        cls.projects = found[1] if found else None

    def test_the_english_readme_documents_the_project_variable_and_the_project_key(self):
        self.assertIn(odoo_rpc.PROJECT_ENV, self.en)
        flat = squash(self.en)
        self.assertRegex(flat, re.escape(odoo_rpc.PROJECT_ENV.lower()) + r" is optional and is not one of the four" + gap(20) +
                         r"overrides the file's project")
        config_block = re.search(r"\[odoo\]\n(?:    .*\n)+", self.en)
        self.assertIsNotNone(config_block)
        self.assertIn("    project = ", config_block.group(0))
        self.assertNotIn(odoo_rpc.PROJECT_ENV, [line.split("=")[0].strip() for line in config_block.group(0).splitlines()])

    def test_the_english_readme_shows_the_four_check_config_project_lines_from_the_real_code(self):
        forms = project_lines(self)
        flat = squash(self.en)
        for form, prefix in forms.items():
            with self.subTest(form=form):
                self.assertIn(prefix, flat)
        self.assertIn("a bad project never makes it fail", flat)

    def test_the_projects_section_exists_and_says_the_flag_the_default_and_the_choice_by_name(self):
        self.assertIsNotNone(self.projects, "README.md has no `Projects (one source catalog per Odoo project)` section")
        self.assertEqual(says(self.projects, r"closed list per project", r"catalogs/<project>\.json",
                              r"scan_excel\.py, bind_kpi\.py and reconcile_kpi\.py take --project <name>",
                              r"exactly one catalog is installed it is used without the flag",
                              r"several are installed the flag is required", r"init-config asks for the project once",
                              r"chosen by name from the files that ship with the plugin" + gap(10) + r"never by a path",
                              r"user-supplied catalog folder does not exist on purpose"), [])

    def checklist(self):
        self.assertIsNotNone(self.projects)
        items = [item for item in re.split(r"(?m)^(?=\d+\.\s)", self.projects) if re.match(r"\d+\.\s", item)]
        return items

    def test_the_checklist_has_five_points_in_this_order(self):
        items = self.checklist()
        self.assertEqual(len(items), 5)
        self.assertEqual([item.split(".")[0] for item in items], ["1", "2", "3", "4", "5"])
        expected = ((r"models or sql views" + gap(60) + r"exist in that project's odoo",),
                    (r"catalogs/<project>\.json", r"schema 1", r"no customer data"),
                    (r"skills/kpi-source-catalog/projects/<project>\.md", r"a test requires it to name every source"),
                    (r"tests with a synthetic fixture", r"second_catalog_fixtures\.py", r"never real customer data"),
                    (r"ask the owner of the repository", r"public with the plugin", r"cleared for publication"))
        for number, (item, patterns) in enumerate(zip(items, expected), 1):
            with self.subTest(point=number):
                self.assertEqual(says(item, *patterns), [])

    def test_point_two_states_the_real_name_rule_and_the_workflow_output_prefixes(self):
        point = squash(self.checklist()[1])
        self.assertIn(kpi_catalogs.NAME_RULE.pattern, point)
        for prefix in kpi_catalogs.IGNORED_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertIn(prefix, point)
        self.assertIn("aliases", point)
        self.assertRegex(point, r"windows device name")
        named = [device for device in sorted(kpi_catalogs.WINDOWS_DEVICE_NAMES) if re.search(r"\b%s\b" % device, point)]
        self.assertGreaterEqual(len(named), 2, "point 2 should show a few real device names, found %s" % named)
        self.assertTrue(os.path.isfile(str(REPO / "tests" / "second_catalog_fixtures.py")), "point 4 names a file that is gone")

    def test_the_vietnamese_readme_explains_the_optional_project_line(self):
        flat = squash(self.vi)
        self.assertIn(odoo_rpc.PROJECT_ENV, self.vi)
        self.assertEqual(says(self.vi, r"dòng project = <tên> trong file là tùy chọn", r"chỉ cần khi plugin có nhiều danh mục dự án",
                              r"init-config hỏi thêm một ô \"project\"",
                              odoo_rpc.PROJECT_ENV.lower() + r" nếu có sẽ ghi đè dòng này"), [])
        self.assertLess(flat.index("api_key"), flat.index("dòng project = <tên>"))

    def test_no_doc_names_the_old_catalog_layout_or_the_removed_constant(self):
        pages = [path for path in PAGES.iterdir() if path.suffix == ".md"]
        for path in (SKILL, CATALOG_SKILL, COMMAND, README, README_VI, *pages):
            text = read(path)
            with self.subTest(file=path.name):
                self.assertIsNone(re.search(r"(?<![\w-])catalog\.json", text), "names the old scripts/catalog.json")
                self.assertNotIn("CATALOG_PATH", text)
                self.assertNotIn("scripts/catalogs", text)
                self.assertNotIn("scripts/catalog", text)


# ---------------------------------------------------------------- the pages and the generic skill obey the docs contract too

class ProjectPageContractTests(DocCase):
    DOCS = [CATALOG_SKILL] + sorted(path for path in PAGES.iterdir() if path.suffix == ".md")

    def test_every_option_and_script_named_in_the_generic_skill_and_the_pages_exists(self):
        known = known_options() | {"--help"}
        for path in self.DOCS:
            text = read(path)
            with self.subTest(file=path.name):
                for option in sorted(set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", text))):
                    self.assertIn(option, known, "%s mentions %s: no script accepts it" % (path.name, option))
                for name in {m.rstrip(".") for m in re.findall(r"scripts/([A-Za-z0-9_.\-]+)", text)} - {""}:
                    self.assertTrue(os.path.isfile(str(REPO / "scripts" / name)), "%s names scripts/%s: no such file" % (path.name, name))
                for name in re.findall(r"(?<![\w/.-])([a-z][a-z0-9_]*\.py)\b", text):
                    self.assertTrue(os.path.isfile(str(REPO / "scripts" / name)), "%s names %s: no such script" % (path.name, name))

    def test_the_pages_hold_no_real_company_term_address_or_hash(self):
        for path in self.DOCS:
            raw = path.read_bytes()
            with self.subTest(file=path.name):
                self.assertEqual([term for term in FORBIDDEN_TEXT if has_term(raw, term)], [])
                self.assertIsNone(HEX40.search(raw.decode("utf-8")))
                self.assertEqual(re.findall(r"https?://[^\s)>]+", raw.decode("utf-8")), [])

    def test_a_page_points_back_to_the_generic_skill_that_exists(self):
        for path in self.DOCS[1:]:
            with self.subTest(file=path.name):
                self.assertIn("../SKILL.md", read(path))
                self.assertTrue((path.parent / ".." / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
