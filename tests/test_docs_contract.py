"""Plan kpi-skill-improvements step 5 / AC6: the TEXT that tells Claude how to use the scripts keeps its safety rules.

The skill text, the `/kpi` command and the READMEs are not code, so a careless edit can drop a safety sentence and no
other test notices. These checks are keyword level on purpose: they lower-case the text, drop the markdown marks and
collapse white space, and look for a rule by its words (`never ... init-config`, `--dashboard` + `always`), not for a
sentence. Rewording a rule keeps them green; deleting or inverting it turns them red with a message that names it.

Nothing is run: the options shown in the text are parsed by the REAL argparse parser of each script (dummy values,
no network), and every script path / error message named in the text is looked up in `scripts/`.
"""
import argparse
import contextlib
import io
import json
import os
import re
import shlex
import tempfile
import unittest

from helpers import REPO
from test_repo_hygiene import FORBIDDEN_TEXT, HEX40, SECRET_INI, SECRET_QUOTED, has_term, scan_tree

import bind_kpi  # noqa: F401  (every script below is imported for its real `build_parser`)
import intake_xlsx
import odoo_rpc
import publish_dashboard
import reconcile_kpi
import scan_excel
import setup_instance
import validate_formulas

SKILL = REPO / "skills" / "kpi-builder" / "SKILL.md"
CATALOG_SKILL = REPO / "skills" / "kpi-source-catalog" / "SKILL.md"
COMMAND = REPO / "commands" / "kpi.md"
README, README_VI = REPO / "README.md", REPO / "README.vi.md"
DOCS = (SKILL, CATALOG_SKILL, COMMAND, README, README_VI)
SCRIPTS = REPO / "scripts"
PLUGIN_ROOTS = ("${CLAUDE_PLUGIN_ROOT}/", "<plugin_folder>/")      # the variable Claude Code sets / the human's own path

PARSERS = {"intake_xlsx": intake_xlsx.build_parser, "scan_excel": scan_excel.build_parser,
           "bind_kpi": bind_kpi.build_parser, "reconcile_kpi": reconcile_kpi.build_parser,
           "publish_dashboard": publish_dashboard.build_parser, "setup_instance": setup_instance.build_parser,
           "validate_formulas": validate_formulas.build_parser}


def read(path):
    with open(str(path), encoding="utf-8") as handle:
        return handle.read()


class DocCase(unittest.TestCase):
    """`assertIn` that never dumps a whole document into the failure message."""

    def assertIn(self, member, container, msg=None):
        if member not in container:
            where = "a text of %d characters" % len(container) if isinstance(container, str) else repr(container)[:200]
            self.fail(msg or "%r not found in %s" % (member, where))


# ---------------------------------------------------------------- text helpers

def squash(text):
    """Lower case, no markdown marks (backticks, bold), white space collapsed: what the keyword checks look at."""
    return re.sub(r"\s+", " ", text.replace("`", "").replace("**", "")).strip().lower()


NEG = r"(?:never|do not|don't|must not|may not|cannot)"


def gap(size):
    """Up to `size` characters that do not cross the end of a sentence (a full stop followed by a space)."""
    return r"(?:(?!\.\s).){0,%d}" % size


def says(text, *patterns):
    """The patterns that do NOT match `text` (squashed); [] means the text says all of them."""
    flat = squash(text)
    return [pattern for pattern in patterns if not re.search(pattern, flat)]


def sections(text):
    """[(title, level, start, body)] of every markdown heading; a body runs to the next heading of the same or a
    higher level."""
    marks = [(m.start(), len(m.group(1)), m.group(2).strip(), m.end()) for m in re.finditer(r"(?m)^(#{1,6})\s+(.*)$", text)]
    found = []
    for index, (start, level, title, body_start) in enumerate(marks):
        end = next((later[0] for later in marks[index + 1:] if later[1] <= level), len(text))
        found.append((title, level, start, text[body_start:end]))
    return found


def section(text, title_pattern):
    """(start, body) of the first section whose title matches `title_pattern` (case-insensitive), else None."""
    for title, _, start, body in sections(text):
        if re.search(title_pattern, title, re.I):
            return start, body
    return None


def hard_rules(text):
    """The numbered items of the `Hard rules` section of the skill."""
    found = section(text, r"^hard rules")
    if found is None:
        return []
    return [item for item in re.split(r"(?m)^(?=\d+\.\s)", found[1]) if re.match(r"\d+\.\s", item)]


def parse_frontmatter(text):
    """({key: raw value}, [problems]) of the `---` block at the start of `text`; ({}, [problem]) when there is none.
    The problems are what a strict YAML reader would choke on in a plain (unquoted) value."""
    match = re.match(r"---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not match:
        return {}, ["no --- frontmatter block at the start"]
    fields, problems = {}, []
    for line in match.group(1).splitlines():
        pair = re.match(r"([A-Za-z][\w-]*):[ \t]*(.*)$", line)
        if not pair:
            problems.append("line %r is not `key: value`" % line[:40])
            continue
        key, value = pair.group(1), pair.group(2).strip()
        fields[key] = value
        if value and value[0] not in "\"'":
            if ": " in value or value.endswith(":"):
                problems.append("%s: a plain value holding a colon and a space is not valid YAML (quote it)" % key)
            if " #" in value:
                problems.append("%s: a plain value holding ` #` is cut there by YAML (quote it)" % key)
    return fields, problems


# ---------------------------------------------------------------- commands shown in the text

COMMAND_LINE = re.compile(r'python\s+"?(?:[^"\n`]{0,60}?/)?scripts/([a-z_]+)\.py"?([^\n`]*)')
ROOT_LINE = re.compile(r'python\s+"?((?:[^"\n`]{0,60}?/)?)scripts/([a-z_]+)\.py')


def commands(text):
    """[(position, script, root, [argument tokens])] for every `python <path>/scripts/<name>.py ...` in `text`.
    Placeholders in angle brackets stay ONE token; a trailing `# comment` and the `[ ]` of an optional part go."""
    found = []
    for match in COMMAND_LINE.finditer(text):
        tail = match.group(2).split(" #")[0]
        tail = re.sub(r"<[^<>]*>", lambda m: m.group(0).replace(" ", "_"), tail)
        try:
            words = shlex.split(tail)
        except ValueError:
            words = tail.split()
        root = ROOT_LINE.match(text, match.start()).group(1).replace(" ", "_")
        found.append((match.start(), match.group(1), root, [w.strip("[]") for w in words if w.strip("[]")]))
    return found


def first_command(text, script):
    return next((entry for entry in commands(text) if entry[1] == script), None)


def option_value(words, option):
    """The word after `option` in `words`, or None."""
    return words[words.index(option) + 1] if option in words and words.index(option) + 1 < len(words) else None


def subparsers_of(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


def dummy_for(action):
    if action.choices:
        return str(list(action.choices)[0])
    for candidate in ("1", "2026"):
        try:
            if action.type is not None:
                action.type(candidate)
            return candidate if action.type is not None else "dummy"
        except Exception:                         # a type that refuses this candidate: try the next one
            continue
    return "dummy"


def rpc_help():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            odoo_rpc.main(["--help"], io.StringIO(), io.StringIO())
        except SystemExit:
            pass
    return out.getvalue()


def command_problems(script, words):
    """Why the real parser of `script` would not accept `words` ([] when it does)."""
    if script == "odoo_rpc":
        text = rpc_help()
        choices = re.search(r"\{([^}]*)\}", text)
        names = choices.group(1).split(",") if choices else []
        problems = []
        positional = [w for w in words if not w.startswith("--")]
        if not positional or positional[0] not in names:
            problems.append("odoo_rpc.py command %r is not one of %s" % (positional[:1], names))
        problems += ["odoo_rpc.py does not accept %s" % w for w in words if w.startswith("--") and w not in text]
        return problems
    if script not in PARSERS:
        return ["%s.py is not a script this test knows" % script]
    parser, argv, items = PARSERS[script](), [], list(words)
    target, subs = parser, subparsers_of(parser)
    if subs:
        if not items or items[0] not in subs:
            return ["%s.py: the first word must be one of %s, got %r" % (script, sorted(subs), items[:1])]
        argv.append(items.pop(0))
        target = subs[argv[0]]
    actions = {option: action for action in target._actions for option in action.option_strings}
    index = 0
    while index < len(items):
        word = items[index]
        if word.startswith("--"):
            action = actions.get(word)
            if action is None:
                return ["%s.py does not accept %s" % (script, word)]
            if action.nargs == 0:
                argv.append(word)
                index += 1
            else:
                if index + 1 >= len(items) or items[index + 1].startswith("--"):
                    return ["%s.py: %s needs a value" % (script, word)]
                argv += [word, dummy_for(action)]
                index += 2
        else:
            argv.append(word)                     # a positional (the dashboard file of `validate_formulas.py check`)
            index += 1
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            parser.parse_args(argv)
    except SystemExit:
        last = err.getvalue().strip().splitlines()
        return ["%s.py %s is refused by its parser: %s" % (script, argv, last[-1] if last else "?")]
    return []


def known_options():
    """Every `--option` any script of the plugin accepts."""
    known = set(re.findall(r"--[a-z][a-z-]*", rpc_help()))
    for build in PARSERS.values():
        parser = build()
        for each in [parser] + list(subparsers_of(parser).values()):
            known.update(option for action in each._actions for option in action.option_strings)
    return known


# ---------------------------------------------------------------- frontmatter helpers (self tests)

class DocHelperTests(DocCase):
    """The helpers find what they claim to find (a helper that matched nothing would make every check below pass)."""

    def test_squash_drops_marks_and_white_space(self):
        self.assertEqual(squash("**Never**  run\n`init-config`\tNOW"), "never run init-config now")

    def test_says_reports_the_patterns_that_are_missing(self):
        self.assertEqual(says("Never run `x`", NEG + gap(10) + "run"), [])
        self.assertEqual(says("Run `x`", NEG + gap(10) + "run"), [NEG + gap(10) + "run"])

    def test_gap_stops_at_the_end_of_a_sentence_but_not_at_a_dot_inside_a_name(self):
        self.assertTrue(says("never run odoo_rpc.py init-config", "never" + gap(40) + "init-config") == [])
        self.assertTrue(says("never. Then init-config", "never" + gap(40) + "init-config") != [])

    def test_sections_nest_by_heading_level(self):
        text = "# a\n## b\nbody b\n### c\nbody c\n## d\nbody d\n"
        self.assertEqual([title for title, *_ in sections(text)], ["a", "b", "c", "d"])
        self.assertEqual(section(text, "^b$")[1].strip().splitlines(), ["body b", "### c", "body c"])
        self.assertEqual(section(text, "^c$")[1].strip(), "body c")
        self.assertIsNone(section(text, "^zzz"))

    def test_hard_rules_are_the_numbered_items_only(self):
        text = "## Hard rules\n\n1. one\n   more\n2. two\n\n## Workflow\n3. not a rule\n"
        self.assertEqual([item.strip() for item in hard_rules(text)], ["1. one\n   more", "2. two"])

    def test_commands_keep_placeholders_whole_and_drop_comments_and_brackets(self):
        text = ('x `python "${CLAUDE_PLUGIN_ROOT}/scripts/scan_excel.py" --baseline <a folder>/b.json '
                '[--aliases <a folder>/m.json]` y\n    python scripts/setup_instance.py init --apply   # note\n')
        self.assertEqual([(s, r, w) for _, s, r, w in commands(text)],
                         [("scan_excel", "${CLAUDE_PLUGIN_ROOT}/", ["--baseline", "<a_folder>/b.json", "--aliases",
                                                                   "<a_folder>/m.json"]),
                          ("setup_instance", "", ["init", "--apply"])])

    def test_the_parser_check_accepts_a_real_command_and_refuses_the_rest(self):
        self.assertEqual(command_problems("scan_excel", ["--baseline", "<x>"]), [])
        self.assertEqual(command_problems("publish_dashboard", ["publish", "--dashboard", "<d>", "--name", "<n>"]), [])
        self.assertEqual(command_problems("validate_formulas", ["check", "<d>.json"]), [])
        self.assertEqual(command_problems("odoo_rpc", ["ping", "--confirm-instance", "<h>"]), [])
        for script, words in (("scan_excel", ["--baseline", "<x>", "--nonexistent", "1"]),
                              ("scan_excel", ["--aliases", "<x>"]),                    # --baseline is required
                              ("scan_excel", ["--basline", "<x>"]),
                              ("reconcile_kpi", ["--baseline", "b", "--spec", "s", "--dashboard", "d"]),   # no --year
                              ("publish_dashboard", ["--dashboard", "<d>"]),                    # no subcommand
                              ("publish_dashboard", ["publish", "--dashbord", "<d>"]),
                              ("odoo_rpc", ["init-cfg"]), ("odoo_rpc", ["ping", "--nope"]),
                              ("no_such_script", [])):
            with self.subTest(script=script, words=words):
                self.assertTrue(command_problems(script, words))

    def test_the_known_options_cover_the_scripts_and_not_a_made_up_one(self):
        known = known_options()
        for option in ("--baseline", "--spec", "--dashboard", "--year", "--tz", "--aliases", "--apply", "--replace",
                       "--confirm-replace", "--confirm-instance", "--xlsx-document", "--config", "--catalog"):
            self.assertIn(option, known)
        self.assertNotIn("--nonexistent", known)

    def test_frontmatter_parser(self):
        fields, problems = parse_frontmatter("---\ndescription: A b c\nargument-hint: <x> [y]\n---\nbody\n")
        self.assertEqual((fields, problems), ({"description": "A b c", "argument-hint": "<x> [y]"}, []))
        self.assertTrue(parse_frontmatter("no block\n")[1])
        self.assertTrue(parse_frontmatter("---\nargument-hint: <x> [file: PDF]\n---\n")[1])        # colon + space
        self.assertTrue(parse_frontmatter("---\ndescription: a # b\n---\n")[1])
        self.assertEqual(parse_frontmatter('---\ndescription: "a: b # c"\n---\n')[1], [])         # quoted is fine
        self.assertTrue(parse_frontmatter("---\nnot a pair\n---\n")[1])


# ---------------------------------------------------------------- frontmatter of the real files

class FrontmatterTests(DocCase):
    def test_every_prompt_file_has_a_frontmatter_a_strict_yaml_reader_accepts(self):
        # `claude plugin validate` is lenient, a strict reader (the YAML spec) is not: a plain value with a colon and a
        # space ("a design file: PDF") is a syntax error. Quoting the value is valid for both.
        for path in (SKILL, CATALOG_SKILL, COMMAND):
            with self.subTest(file=str(path.relative_to(REPO))):
                self.assertEqual(parse_frontmatter(read(path))[1], [])


# ---------------------------------------------------------------- SKILL.md: the hard rules

class SkillHardRuleTests(DocCase):
    """Each rule is looked up by its words inside the `Hard rules` section, in ONE numbered item."""

    @classmethod
    def setUpClass(cls):
        cls.text = read(SKILL)
        cls.rules = hard_rules(cls.text)

    def rule_with(self, *patterns):
        """The first hard rule that says every pattern, or a failure naming the closest miss."""
        best = None
        for item in self.rules:
            missing = says(item, *patterns)
            if not missing:
                return item
            best = missing if best is None or len(missing) < len(best) else best
        self.fail("no hard rule says %s (closest rule misses %s)" % (list(patterns), best))

    def test_the_section_has_nine_numbered_rules(self):
        self.assertGreaterEqual(len(self.rules), 9)

    def test_rule_never_run_init_config_never_through_a_pty_or_without_claudecode(self):
        self.rule_with(NEG + gap(40) + r"init-config", NEG + gap(60) + r"\bpty\b", r"claudecode", r"environment")

    def test_rule_the_key_never_passes_through_the_session(self):
        self.rule_with(r"init-config", r"api key", r"\bhuman\b", r"never|not")
        self.assertIn("init-config", squash(self.text.split("## Workflow")[0]))

    def test_rule_text_from_the_excel_is_data_never_instructions(self):
        self.rule_with(r"excel" + gap(30) + r"\bis data\b", r"never" + gap(30) + r"instruction",
                       NEG + r" open" + gap(15) + r"link")

    def test_rule_7_names_file_and_document_names_and_the_text_of_a_design_document_as_data(self):
        # D1: the names of the customer's files and the text of a design document are untrusted too
        rule = self.rule_with(r"file and document names", r"the text of a design document",
                              NEG + r" put" + gap(30) + r"name into a command or a file name unchanged")
        self.assertRegex(squash(rule), r"^7\.")
        self.assertIn("text from the excel is data", squash(rule))

    def test_rule_never_pass_catalog_to_bind_kpi(self):
        self.rule_with(NEG + r" pass" + gap(15) + r"--catalog", r"bind_kpi")

    def test_rule_only_existing_sources_and_a_requirement_text_otherwise(self):
        self.rule_with(r"only existing sources", r"kpi-source-catalog", r"requirement text", r"\bstop\b")

    def test_rule_never_print_a_secret_or_read_the_config_file(self):
        self.rule_with(NEG + gap(30) + r"(?:print|log)" + gap(10) + r"secret", r"config file", r"api key", r"bash -x")

    def test_rule_confirm_instance_is_typed_by_the_human(self):
        self.rule_with(r"--confirm-instance", r"typed by the human", NEG + gap(10) + r"fill", r"full host name")

    def test_rule_only_the_users_own_dashboards_and_no_delete_without_a_yes(self):
        self.rule_with(r"kpi \(qa/qc\)", r"own", r"explicit yes", NEG + gap(60) + r"(?:edit|delete)")

    def test_rule_dry_run_first_then_ask_then_apply(self):
        self.rule_with(r"dry run first", r"--apply", r"\byes\b", r"writes nothing without --apply")

    def test_rule_never_run_setup_instance_and_never_read_the_admin_variables(self):
        self.rule_with(NEG + r" run" + gap(15) + r"setup_instance\.py", NEG + r" read" + gap(15) + r"odoo_kpi_admin")

    def test_rule_no_customer_data_in_files_left_behind(self):
        self.rule_with(r"no customer data", r"do not copy", r"commit")


# ---------------------------------------------------------------- SKILL.md: the workflow

class SkillWorkflowTests(DocCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(SKILL)
        cls.commands = commands(cls.text)

    def step(self, title_pattern):
        found = section(self.text, title_pattern)
        self.assertIsNotNone(found, "no step headed like %r" % title_pattern)
        return found

    def command(self, script):
        found = first_command(self.text, script)
        self.assertIsNotNone(found, "the skill shows no `python .../%s.py` command" % script)
        return found

    # -- order
    def test_headings_run_intake_scan_bind_publish(self):
        starts = [self.step(r"^\S+\s+intake")[0], self.step(r"^\S+\s+scan")[0], self.step(r"^\S+\s+bind")[0],
                  self.step(r"^\S+\s+publish")[0]]
        self.assertEqual(starts, sorted(starts), "the steps must come in the order intake, scan, bind, publish")

    def test_commands_run_in_the_order_check_intake_scan_bind_validate_reconcile_publish(self):
        order = ["odoo_rpc", "intake_xlsx", "scan_excel", "bind_kpi", "validate_formulas", "reconcile_kpi",
                 "publish_dashboard"]
        positions = [self.command(script)[0] for script in order]
        self.assertEqual(positions, sorted(positions),
                         "the commands must be shown in the order %s" % " < ".join(order))

    def test_reconcile_is_taught_after_bind_and_before_publish_in_the_text_too(self):
        bind_start, bind_body = self.step(r"^\S+\s+bind")
        publish_start = self.step(r"^\S+\s+publish")[0]
        self.assertIn("reconcile_kpi.py", bind_body)
        self.assertLess(bind_start, bind_start + bind_body.index("reconcile_kpi.py"))
        self.assertLess(bind_start + bind_body.index("reconcile_kpi.py"), publish_start)

    def test_the_interview_sends_the_user_to_the_scan_before_asking_cell_by_cell(self):
        body = self.step(r"interview")[1]
        self.assertEqual(says(body, r"intake" + gap(40) + r"first", r"scan"), [])

    # -- scan
    def test_the_scan_step_names_the_script_and_the_offline_proposal(self):
        body = self.step(r"^\S+\s+scan")[1]
        self.assertEqual(says(body, r"scan_excel\.py", r"offline", r"propos", r"\byes\b"), [])

    def test_unmatched_rows_are_never_bound_by_guessing(self):
        body = self.step(r"^\S+\s+scan")[1]
        self.assertEqual(says(body, r"(?:could not|cannot|not) match|unmatched", NEG + gap(30) + r"(?:bind|use)" +
                              gap(30) + r"guess", r"requirement text"), [],
                         "the scan step must say a row that is not matched is never bound by guessing")

    def test_the_scan_step_names_the_users_own_aliases_file_not_the_plugin_folder(self):
        body = self.step(r"^\S+\s+scan")[1]
        self.assertEqual(says(body, r"--aliases", r"own aliases file", NEG + gap(30) + r"plugin folder"), [])

    def test_the_scan_step_says_warnings_are_for_the_designer_or_user_to_decide(self):
        body = self.step(r"^\S+\s+scan")[1]
        self.assertEqual(says(body, r"warning", r"designer or the user decides|ask"), [])

    def test_the_scan_step_says_where_the_dry_run_line_is_a_second_run_needs_force_and_a_title_warning_exists(self):
        body = self.step(r"^\S+\s+scan")[1]
        self.assertEqual(says(body, r"bind dry run: ok" + gap(120) + r"comes just before the line saying where the draft "
                                    r"was written",
                              r"scan again with --force",
                              r"block title that was not recognised" + gap(80) + r"source came from the label alone"), [])

    def test_the_scan_step_says_never_pass_out_to_the_scan_it_refuses_the_inputs_and_any_spec_file_and_force_is_for_its_own_draft(self):
        # fix-r2 T2: the old sentence ("never --force over a spec or an aliases file: --out is refused when it is one of
        # the inputs") let `--out kpi.spec.json --force` through, the new one forbids --out altogether
        body = self.step(r"^\S+\s+scan")[1]
        self.assertEqual(says(body, NEG + r" pass --out to the scan" + gap(60) + r"default draft name is enough",
                              r"it refuses the baseline, the aliases file and any \.spec\.json file",
                              r"--force is only for replacing its own draft"), [])
        flat = squash(body)
        self.assertNotIn("never --force over a spec", flat)
        self.assertNotIn("--out is refused when it is one of the inputs", flat)
        self.assertLess(flat.index("scan again with --force"), flat.index("never pass --out to the scan"))
        self.assertLess(flat.index("never pass --out to the scan"), flat.index("--force is only for replacing"))
        # the scan command template of the skill itself never carries --out (nor --force)
        words = self.command("scan_excel")[3]
        self.assertNotIn("--out", words)
        self.assertNotIn("--force", words)

    def test_the_scan_step_claim_is_what_the_script_does(self):
        # the doc says the scan refuses the baseline, the aliases file and any .spec.json file: check each on the real script
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        baseline = os.path.join(folder.name, "baseline.json")
        spec = os.path.join(folder.name, "kpi.spec.json")
        aliases = os.path.join(folder.name, "my_aliases.json")
        with open(baseline, "w", encoding="utf-8") as handle:
            handle.write('{"x": 1}')
        for path in (spec, aliases):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")
        for out_name, extra in ((baseline, []), (aliases, ["--aliases", aliases]), (spec, [])):
            for force in ([], ["--force"]):
                with self.subTest(out=os.path.basename(out_name), force=bool(force)):
                    err = io.StringIO()
                    rc = scan_excel.main(["--baseline", baseline, "--out", out_name] + extra + force, io.StringIO(), err)
                    self.assertEqual(rc, 2)
                    self.assertTrue(err.getvalue().startswith("error: --out "), err.getvalue())
        with open(baseline, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), '{"x": 1}')
        with open(spec, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{}")

    # -- the spec name and the quoting of paths (D1)
    def test_the_spec_name_is_a_slug_the_agent_makes_from_the_users_words_never_a_document_name(self):
        body = self.step(r"^\S+\s+write the kpi spec")[1]
        self.assertEqual(says(body, r"<kpi slug>\.spec\.json", r"slug is a name you make from the user's words",
                              r"only letters, digits, _ and -", r"at most 40 characters",
                              r"never copied from a document or file name", r"untrusted", r"in doubt use kpi",
                              r"in every command put each path in double quotes"), [])

    def test_the_slug_rule_names_the_windows_device_names_and_keeps_the_fallback(self):
        # fix-r2 T1 (a file called con.spec.json / nul.json is a device on Windows)
        body = self.step(r"^\S+\s+write the kpi spec")[1]
        flat = squash(body)
        self.assertRegex(flat, r"not a windows device name \(con, nul, aux, prn, com1, lpt1 \.\.\.\)")
        self.assertRegex(flat, r"when in doubt use kpi")
        for device in ("con", "nul", "aux", "prn", "com1", "lpt1"):
            with self.subTest(device=device):
                self.assertRegex(flat, r"\b%s\b" % device)
        # the device-name clause belongs to the slug sentence, before the quoting sentence of the commands
        self.assertLess(flat.index("slug is a name you make"), flat.index("windows device name"))
        self.assertLess(flat.index("windows device name"), flat.index("when in doubt use kpi"))
        self.assertLess(flat.index("when in doubt use kpi"), flat.index("in every command put each path"))

    # ---- fix-r2 T1: a tab or dashboard name with a shell metacharacter never reaches a command
    SHELL_CHARACTERS = ["$", "\\", "&", "|", ";", "<", ">", "(", ")", "%", "^", "!"]    # the backticked ones
    SHELL_WORDS = ("double quote", "single quote", "backtick")                         # the three written as words

    def name_rule(self):
        """(raw sentence group, start in the step body) of the rule about sheet / dashboard names in the intake step."""
        start, body = self.step(r"^\S+\s+intake")
        match = re.search(r"A sheet \(tab\) name or a dashboard name.*?never from a document\)\.", body, re.S)
        self.assertIsNotNone(match, "the intake step has no rule about sheet (tab) / dashboard names")
        return match.group(0), body, match.start()

    def test_the_intake_step_lists_the_shell_characters_a_tab_or_dashboard_name_must_not_contain(self):
        rule, _, _ = self.name_rule()
        listed = re.findall(r"`([^`]+)`", rule)
        self.assertEqual(sorted(listed), sorted(self.SHELL_CHARACTERS), "the backticked characters of the rule")
        self.assertEqual(len(listed), len(set(listed)), "a character is listed twice")
        flat = squash(rule)
        for word in self.SHELL_WORDS:
            with self.subTest(word=word):
                self.assertEqual(flat.count("the " + word), 1, "%r must be named once" % word)
        self.assertRegex(flat, r"must not contain any of these characters")
        self.assertRegex(flat, r"a shell would expand it or end the quoting")

    def test_each_shell_character_is_listed_once_in_the_whole_skill_text(self):
        for character in self.SHELL_CHARACTERS:
            with self.subTest(character=character):
                self.assertEqual(self.text.count("`%s`" % character), 1)
        for word in self.SHELL_WORDS:
            with self.subTest(word=word):
                self.assertEqual(squash(self.text).count("the " + word + ","), 1)

    def test_the_rule_says_do_not_run_the_command_with_such_a_name_ask_the_user_to_rename_or_retype(self):
        rule, _, _ = self.name_rule()
        self.assertEqual(says(rule, r"then do not run the command with it",
                              r"ask the user to rename the tab in their excel",
                              r"(?:or )?to retype the dashboard name in their own words",
                              r"name a dashboard from the user's words, never from a document"), [])
        flat = squash(rule)
        self.assertLess(flat.index("must not contain"), flat.index("then do not run the command with it"))
        self.assertLess(flat.index("then do not run the command with it"), flat.index("ask the user to rename the tab"))
        self.assertLess(flat.index("ask the user to rename the tab"), flat.index("retype the dashboard name"))

    def test_the_name_rule_sits_in_the_intake_step_after_the_command_template_and_before_the_script_is_run(self):
        # the brief: "before The script creates a temporary spreadsheet copy"; the template itself is the code block above
        rule, body, at = self.name_rule()
        self.assertLess(at, body.index("The script creates a temporary spreadsheet copy"))
        self.assertLess(body.index("intake_xlsx.py"), at)
        # and before every later command: the scan, and the publish command that takes the dashboard name
        absolute = self.text.index(rule)
        self.assertLess(absolute, self.command("scan_excel")[0])
        self.assertLess(absolute, self.command("publish_dashboard")[0])
        self.assertLess(absolute, self.step(r"^\S+\s+scan")[0])
        self.assertEqual(self.text.count("A sheet (tab) name or a dashboard name"), 1)

    def test_the_rule_names_both_the_tab_and_the_dashboard_and_the_intake_template_uses_a_quoted_tab(self):
        rule, _, _ = self.name_rule()
        flat = squash(rule)
        self.assertIn("sheet (tab) name", flat)
        self.assertIn("dashboard name", flat)
        line = [item for item in self.command_lines() if "intake_xlsx.py" in item][0]
        self.assertIn('--sheet "<tab>"', line)

    def command_lines(self):
        return [line for line in self.text.splitlines() if re.search(r"python\s+\S*scripts/[a-z_]+\.py", line)]

    def test_every_command_template_of_the_skill_quotes_the_script_and_every_work_folder_path(self):
        lines = self.command_lines()
        self.assertGreaterEqual(len(lines), 8)
        placeholders = 0
        for line in lines:
            with self.subTest(line=line[:70]):
                self.assertRegex(line, r'python\s+"(?:\$\{CLAUDE_PLUGIN_ROOT\}|<plugin folder>)/scripts/',
                                 "the script path of a command must be in double quotes")
                outside = re.sub(r'"[^"\n]*"', "", line)             # what is left when the quoted parts are removed
                for loose in ("<folder>", "<kpi slug>", "<tab>", "<name>"):
                    self.assertNotIn(loose, outside, "%s must stay inside double quotes" % loose)
                placeholders += line.count("<folder>")
        self.assertGreaterEqual(placeholders, 11)                    # intake 1, scan 2, bind 3, validate 1, reconcile 3, publish 1

    def test_the_spec_path_placeholder_holds_a_space_so_the_quotes_are_needed_in_the_bind_and_reconcile_commands(self):
        for script in ("bind_kpi", "reconcile_kpi"):
            words = self.command(script)[3]
            spec = option_value(words, "--spec")
            self.assertIn("<kpi_slug>", spec)                          # `commands()` keeps a placeholder whole (space -> _)
        for line in self.command_lines():
            if "<kpi slug>" in line:
                self.assertIn('"<folder>/<kpi slug>.spec.json"', line)

    # -- reconcile
    def test_reconcile_is_always_run_with_the_dashboard_that_gets_published(self):
        body = self.step(r"^\S+\s+bind")[1]
        self.assertEqual(says(body, r"always" + gap(120) + r"--dashboard", r"exact file" + gap(60) + r"publish",
                              r"--dashboard" + gap(30) + r"required"), [],
                         "the bind step must say reconcile is ALWAYS run with `--dashboard` = the file passed to publish")

    def test_the_reconcile_command_carries_its_required_options(self):
        words = self.command("reconcile_kpi")[3]
        for option in ("--baseline", "--spec", "--dashboard", "--year", "--confirm-instance"):
            self.assertIn(option, words, "the reconcile command of the skill lacks %s" % option)

    def test_one_dashboard_file_flows_from_bind_to_reconcile_to_publish(self):
        bound = option_value(self.command("bind_kpi")[3], "--out")
        self.assertIsNotNone(bound)
        self.assertEqual(option_value(self.command("reconcile_kpi")[3], "--dashboard"), bound,
                         "reconcile must read the file bind wrote")
        self.assertEqual(option_value(self.command("publish_dashboard")[3], "--dashboard"), bound,
                         "publish must read the file bind wrote")

    def test_one_baseline_and_one_spec_flow_through_the_steps(self):
        baseline = option_value(self.command("intake_xlsx")[3], "--out")
        self.assertIsNotNone(baseline)
        for script in ("scan_excel", "bind_kpi", "reconcile_kpi"):
            self.assertEqual(option_value(self.command(script)[3], "--baseline"), baseline, script)
        spec = option_value(self.command("bind_kpi")[3], "--spec")
        self.assertEqual(option_value(self.command("reconcile_kpi")[3], "--spec"), spec)
        self.assertTrue(spec.endswith(".spec.json"), spec)                        # the reuse convention

    def test_differ_is_not_an_error_it_is_a_question(self):
        body = self.step(r"^\S+\s+bind")[1]
        self.assertEqual(says(body, r"differ\b" + gap(100) + r"not an error" + gap(60) + r"\bask\b"), [],
                         "the bind step must say `differ` is NOT an error: ask which side is right")

    def test_an_error_cell_stops_the_publish(self):
        body = self.step(r"^\S+\s+bind")[1]
        self.assertEqual(says(body, r"\berror\b" + gap(100) + r"\bstop\b" + gap(20) + r"do not publish"), [])

    def test_many_differences_are_a_reason_to_ask_never_to_change_the_spec_silently(self):
        body = self.step(r"^\S+\s+bind")[1]
        self.assertEqual(says(body, r"many differ" + gap(80) + r"ask", NEG + gap(30) + r"change the spec silently"), [])

    def test_the_time_zone_is_asked_for_never_guessed(self):
        flat = squash(self.step(r"^\S+\s+bind")[1])
        near = [flat[max(0, m.start() - 200):m.end() + 200] for m in re.finditer(r"--tz", flat)]
        self.assertTrue(near, "the bind step must mention --tz")
        self.assertTrue(any(re.search(NEG + r" guess", chunk) and "time zone" in chunk and "ask" in chunk
                            for chunk in near), "the text near --tz must say: ask the user, never guess a time zone")

    # -- reuse
    def reuse(self):
        match = re.search(r"(?m)^\*{0,2}Reuse\.?\*{0,2}.*(?:\n(?!\s*\n).*)*", self.text)
        self.assertIsNotNone(match, "the skill has no `Reuse` paragraph")
        return match.group(0)

    def test_reuse_names_the_v2_spec_file_and_the_sha_refresh(self):
        self.assertEqual(says(self.reuse(), r"<kpi slug>\.spec\.json", r"<kpi slug>\.v2\.spec\.json",
                              r"baseline_sha256", r"new intake's sha256|sha256 of the new", r"sha256 differs",
                              r"label shift", r"intake"), [])
        self.assertNotIn("<excel name>", self.text, "the spec name is made from the user's words, never an Excel name")

    def test_reuse_runs_the_scan_on_the_new_baseline_compares_shows_and_refreshes_only_after_a_yes(self):
        # D1: the old spec is compared with what the scan finds in the NEW baseline before anything is refreshed
        reuse = squash(self.reuse())
        self.assertEqual(says(self.reuse(), r"run the scan on the new baseline",
                              r"compare" + gap(40) + r"bindings and labels" + gap(40) + r"old spec",
                              r"show the user what is the same and what moved or is new",
                              r"refresh only after a yes" + gap(30) + r"cells that moved",
                              r"<kpi slug>\.v2\.spec\.json"), [])
        scan_at, show_at = reuse.index("run the scan on the new baseline"), reuse.index("show the user what is the same")
        yes_at, v2_at = reuse.index("refresh only after a yes"), reuse.index(".v2.spec.json")
        self.assertLess(reuse.index("run the intake again"), scan_at)
        self.assertLess(scan_at, show_at)
        self.assertLess(show_at, yes_at)
        self.assertLess(yes_at, v2_at)                                  # the refresh comes after the comparison and the yes

    def test_reuse_also_compares_the_label_cells_text_and_the_typed_or_blank_state_of_the_bound_cells(self):
        # fix-r2 T3: the scan resolves only list rows, so a moved label of a QMS / derived / keep_typed cell and a
        # bound cell that went blank (or typed) are invisible to it: the agent compares them in the two baselines
        reuse = squash(self.reuse())
        self.assertEqual(says(self.reuse(), r"also compare, in the old and the new baseline,",
                              r"text of every cell listed in the spec's labels",
                              r"typed or blank state of every bound cell",
                              r"the scan resolves only list rows",
                              r"qms, derived and keep_typed cells" + gap(40) + r"not covered"), [])
        scan_at = reuse.index("run the scan on the new baseline")
        extra_at, show_at = reuse.index("also compare"), reuse.index("show the user what is the same")
        yes_at = reuse.index("refresh only after a yes")
        names_at = reuse.index("cells that moved")
        self.assertLess(scan_at, extra_at)                              # after the scan comparison ...
        self.assertLess(extra_at, show_at)                              # ... and before anything is shown
        self.assertLess(show_at, yes_at)
        self.assertLess(yes_at, names_at)                               # the yes is the one that names the moved cells
        self.assertLess(names_at, reuse.index(".v2.spec.json"))
        self.assertEqual(reuse.count("also compare"), 1)

    def test_reuse_asks_for_a_yes_that_names_the_moved_cells_never_a_bare_yes(self):
        reuse = squash(self.reuse())
        self.assertRegex(reuse, r"refresh only after a yes that names the cells that moved")
        self.assertNotIn("refresh only after the user's yes", reuse)   # the old, weaker wording

    def test_reuse_is_looked_for_before_interviewing_again(self):
        self.assertEqual(says(self.reuse(), r"work folder", r"instead of interviewing"), [])

    def test_reuse_never_refreshes_the_hash_past_a_label_shift_silently(self):
        self.assertEqual(says(self.reuse(), r"(?:never|not) refresh" + gap(100) + r"label shift" + gap(80) +
                              r"(?:silent|without telling)"), [],
                         "the Reuse paragraph must forbid refreshing the hash past a `label shift` without telling the user")

    def test_reuse_says_what_to_do_when_a_row_moved(self):
        self.assertEqual(says(self.reuse(), r"row moved" + gap(60) + r"label shift", r"show the user which row",
                              r"decide together"), [])

    # -- publish
    def test_the_publish_command_is_a_dry_run_and_apply_comes_after_a_yes(self):
        words = self.command("publish_dashboard")[3]
        self.assertEqual(words[0], "publish")
        self.assertNotIn("--apply", words, "the first publish command must be the dry run")
        body = self.step(r"^\S+\s+publish")[1]
        self.assertEqual(says(body, r"\byes\b" + gap(40) + r"--apply", r"every internal user", r"dry run|plan"), [])

    def test_no_command_of_the_skill_applies_anything_directly(self):
        for _, script, _, words in self.commands:
            self.assertNotIn("--apply", words, "%s.py is shown with --apply" % script)

    # -- time zone / init-config in the preflight and the error table
    def test_the_preflight_sends_the_user_to_their_own_terminal_for_init_config(self):
        body = self.step(r"preflight")[1]
        self.assertEqual(says(body, r"init-config", r"own terminal|own terminal window", r"not this session",
                              r"never run it|you never run"), [])
        entry = first_command(body, "odoo_rpc")
        self.assertEqual(entry[3], ["check-config"], "the first preflight command must stay the harmless check-config")

    def test_init_config_is_never_shown_as_a_command_claude_runs(self):
        for position, script, root, words in self.commands:
            if script == "odoo_rpc" and "init-config" in words:
                self.assertEqual(root.replace("_", " "), "<plugin folder>/",
                                 "init-config may only be shown as the HUMAN's own command (their plugin folder path)")
                context = squash(self.text[max(0, position - 300):position + 160])
                self.assertRegex(context, r"(?:user|own terminal)", "init-config must be shown as the user's command")

    def test_the_troubleshooting_list_covers_the_new_errors_and_points_to_rule_9(self):
        body = self.step(r"troubleshooting")[1]
        self.assertEqual(says(body, r"the dashboard file is not what the spec produces", r"--tz",
                              r"no header row with the 12 months in order was found", r"sha256 differs", r"reuse",
                              r"init-config" + gap(120) + r"hard rule 9", r"looks like an api key"), [])


# ---------------------------------------------------------------- SKILL.md / READMEs: scripts, options, messages

class ScriptReferenceTests(DocCase):
    TEXTS = (SKILL, CATALOG_SKILL, COMMAND, README, README_VI)

    def test_every_scripts_path_named_in_the_docs_exists(self):
        for path in self.TEXTS:
            text = read(path)
            names = sorted({m.rstrip(".") for m in re.findall(r"scripts/([A-Za-z0-9_.\-]+)", text)} - {""})
            self.assertTrue(names or path == COMMAND, "%s names no script at all" % path.name)
            for name in names:
                with self.subTest(file=path.name, name=name):
                    self.assertTrue(os.path.isfile(str(SCRIPTS / name)), "%s names scripts/%s: no such file" %
                                    (path.name, name))

    def test_every_bare_python_script_name_in_the_skill_and_readmes_exists(self):
        for path in (SKILL, CATALOG_SKILL, README, README_VI):
            names = {m for m in re.findall(r"(?<![\w/.-])([a-z][a-z0-9_]*\.py)\b", read(path)) if not m.startswith("test_")}
            for name in sorted(names):
                with self.subTest(file=path.name, name=name):
                    self.assertTrue(os.path.isfile(str(SCRIPTS / name)), "%s names %s: no such script" % (path.name, name))

    def test_the_skill_names_the_scripts_of_this_plan(self):
        flat = read(SKILL)
        for script in ("scan_excel.py", "reconcile_kpi.py", "odoo_rpc.py", "intake_xlsx.py", "bind_kpi.py",
                       "publish_dashboard.py", "validate_formulas.py"):
            self.assertIn(script, flat)

    def test_every_command_in_the_docs_starts_from_the_plugin_folder(self):
        for position, script, root, words in commands(read(SKILL)):
            self.assertIn(root, PLUGIN_ROOTS, "%s.py is not run from ${CLAUDE_PLUGIN_ROOT}" % script)
        for path in (README, README_VI):                    # the READMEs speak to the human: a relative path or the folder
            for position, script, root, words in commands(read(path)):
                self.assertIn(root.replace("_", " "), ("", "<plugin folder>/", "<thư mục plugin>/"),
                              "%s: %s.py" % (path.name, script))

    def test_every_command_shown_in_the_skill_and_the_readmes_is_accepted_by_the_real_parser(self):
        checked = 0
        for path in (SKILL, README, README_VI):
            for position, script, root, words in commands(read(path)):
                with self.subTest(file=path.name, script=script, words=words):
                    self.assertEqual(command_problems(script, words), [])
                checked += 1
        self.assertGreaterEqual(checked, 12)

    def test_every_option_named_anywhere_in_the_skill_and_the_command_exists_in_some_script(self):
        known = known_options() | {"--help"}
        for path in (SKILL, COMMAND, README, README_VI):
            options = sorted(set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", read(path))))
            for option in options:
                with self.subTest(file=path.name, option=option):
                    self.assertIn(option, known, "%s mentions %s: no script accepts it" % (path.name, option))

    def test_the_options_of_this_plan_are_accepted_by_the_script_the_skill_names_them_for(self):
        expected = {"scan_excel": ("--baseline", "--aliases"),
                    "reconcile_kpi": ("--baseline", "--spec", "--dashboard", "--year", "--tz", "--confirm-instance"),
                    "bind_kpi": ("--baseline", "--spec", "--out"),
                    "publish_dashboard": ("--dashboard", "--name", "--apply", "--replace", "--confirm-replace",
                                          "--confirm-instance")}
        text = read(SKILL)
        for script, options in expected.items():
            parser = PARSERS[script]()
            actions = [parser] + list(subparsers_of(parser).values())
            accepted = {option for each in actions for action in each._actions for option in action.option_strings}
            for option in options:
                with self.subTest(script=script, option=option):
                    self.assertIn(option, accepted)
                    self.assertIn(option, text, "the skill never mentions %s" % option)

    def test_confirm_instance_in_a_command_is_always_a_placeholder_never_a_host(self):
        for path in (SKILL, README, README_VI):
            for position, script, root, words in commands(read(path)):
                value = option_value(words, "--confirm-instance")
                if value is not None:
                    self.assertTrue(value.startswith("<") and value.endswith(">"), "%s: %s.py shows a literal host %r" %
                                    (path.name, script, value))

    def test_the_documented_error_messages_are_the_ones_the_scripts_print(self):
        # (docs literal, script, the pieces of the real message): a reworded script message must reach the docs
        table = (("the dashboard file is not what the spec produces", "reconcile_kpi", ()),
                 ("no header row with the 12 months in order was found", "scan_excel", ()),
                 ("has typed numbers but no catalog source has an alias for it", "scan_excel",
                  ("has typed numbers but", "no catalog source has an alias for it")),
                 ("the baseline changed since the spec was written (sha256 differs)", "bind_kpi", ()),
                 ("looks like an API key", "odoo_rpc", ()),
                 ("your Odoo user has no time zone", "reconcile_kpi", ()),
                 ("is not one the server knows", "reconcile_kpi", ()),
                 ("is for you to run in your own terminal", "odoo_rpc", ()),
                 ("needs a terminal", "odoo_rpc", ()),
                 ("is not a local dev host: ask the user to type the full host name", "odoo_rpc", ()),
                 ("no snapshot after 600 seconds", "intake_xlsx", ("no snapshot after",)),
                 ("typed numbers left outside the bindings", "bind_kpi", ()),
                 ("label shift", "bind_kpi", ()))
        sources = {name: read(SCRIPTS / (name + ".py")) for name in {row[1] for row in table}}
        for literal, script, pieces in table:
            with self.subTest(message=literal):
                for piece in pieces or (literal,):
                    self.assertIn(piece, sources[script], "%s.py no longer prints %r" % (script, piece))
                self.assertTrue(literal.lower() in squash(read(README_VI)),
                                "README.vi.md (the error table) does not list the error %r" % literal)

    def test_the_new_errors_are_in_both_the_skill_and_the_vietnamese_readme(self):
        for literal in ("the dashboard file is not what the spec produces",
                        "no header row with the 12 months in order was found", "looks like an API key",
                        "sha256 differs", "no time zone"):
            for path in (SKILL, README_VI):
                with self.subTest(file=path.name, message=literal):
                    self.assertIn(literal.lower(), squash(read(path)))


# ---------------------------------------------------------------- commands/kpi.md

class KpiCommandTests(DocCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(COMMAND)
        cls.fields, cls.problems = parse_frontmatter(cls.text)
        cls.body = cls.text.split("\n---\n", 1)[1]

    def test_the_file_is_the_command_named_kpi_and_has_a_frontmatter(self):
        self.assertTrue(COMMAND.is_file())
        self.assertEqual(COMMAND.name, "kpi.md")
        self.assertEqual(self.text.split("\n", 1)[0], "---")

    def test_the_frontmatter_has_a_description_and_an_argument_hint(self):
        for key in ("description", "argument-hint"):
            self.assertTrue(self.fields.get(key), "the command frontmatter lacks a non-empty `%s`" % key)
        self.assertRegex(squash(self.fields["argument-hint"]), r"excel|document|address|id")

    def test_the_body_passes_the_arguments_on(self):
        self.assertIn("$ARGUMENTS", self.body)

    def test_the_body_starts_the_kpi_builder_skill_and_its_hard_rules(self):
        self.assertEqual(says(self.body, r"kpi-builder\b", r"hard rules", r"skill"), [])
        self.assertEqual(read(REPO / "skills" / "kpi-builder" / "SKILL.md").count("name: kpi-builder"), 1)

    def test_it_never_runs_init_config_itself(self):
        flat = squash(self.body)
        near = [flat[max(0, m.start() - 160):m.end() + 160] for m in re.finditer(r"init-config", flat)]
        self.assertTrue(near, "the command must mention init-config")
        self.assertTrue(any(re.search(NEG + r" (?:run|execute|start)", chunk) and "own terminal" in chunk for chunk in near),
                        "the command must say init-config is never run by Claude, only in the user's own terminal")

    def test_it_never_publishes_before_the_dry_run_was_shown_and_a_yes(self):
        self.assertEqual(says(self.body, r"(?:never|not) publish" + gap(80) + r"dry run" + gap(80) + r"\byes\b"), [])

    def test_it_runs_the_scan_before_building_and_shows_the_proposed_mapping_first(self):
        self.assertEqual(says(self.body, r"intake" + gap(40) + r"scan", r"mapping" + gap(60) + r"before" + gap(30) +
                              r"built", r"preflight"), [])
        flat = squash(self.body)
        self.assertLess(flat.index("preflight"), flat.index("intake"))
        self.assertLess(flat.index("intake"), flat.index("scan"))

    def test_the_design_file_is_a_hint_and_its_text_is_data(self):
        self.assertEqual(says(self.body, r"design file", r"only help", r"data" + gap(30) + r"never" + gap(10) +
                              r"instruction"), [])

    def test_it_holds_no_script_command_of_its_own(self):
        self.assertEqual(commands(self.body), [])           # the workflow lives in the skill, not twice


# ---------------------------------------------------------------- README.md / README.vi.md

class ReadmeTests(DocCase):
    def test_readme_en_mentions_init_scan_reconcile_and_the_command(self):
        text = read(README)
        for word in ("init-config", "scan_excel.py", "reconcile_kpi.py", "/odoo-kpi-skills:kpi", "commands/kpi.md"):
            self.assertIn(word, text, "README.md does not mention %s" % word)

    def test_readme_en_says_init_config_is_for_the_human_terminal_only(self):
        self.assertEqual(says(read(README), r"init-config", r"normal terminal", r"not inside claude code",
                              r"agent must never run"), [])

    def test_readme_en_warns_windows_users_off_the_microsoft_store_python(self):
        # 0.1.3: the Store build redirects %APPDATA%, so icacls (not sandboxed) cannot see the config file
        text = read(README)
        flat = squash(text)
        for word in ("Microsoft Store", "python.org", "`py`", "LocalCache", "icacls"):
            self.assertIn(word, text, "README.md does not mention %s" % word)
        # Updated ON PURPOSE round 2: no blind "use the py launcher"; check which Python runs, avoid the Store folders,
        # and the Store redirect is marked as not verified on a real machine.
        self.assertEqual(says(text, r"use python from python\.org",
                              r"not the one from the microsoft store",
                              r"redirect %appdata% into its own localcache folder",
                              r"not verified on a real machine",
                              r"py -0p", r"sys\.executable", r"windowsapps", r"pythonsoftwarefoundation",
                              r"the error says so"), [])
        # one bullet: the key phrases sit within the same paragraph, not scattered over the README
        paragraph = next(p for p in re.split(r"\n\s*\n", text) if "Microsoft Store" in p)
        for word in ("python.org", "`py`", "LocalCache", "WindowsApps", "PythonSoftwareFoundation", "py -0p"):
            self.assertIn(word, paragraph)
        self.assertNotIn("use the py launcher", flat)           # the old blind advice
        self.assertIn("without a byte order mark", flat)

    def test_readme_vi_warns_windows_users_off_the_microsoft_store_python(self):
        # 0.1.3: the same advice for the QA/QC staff, in Vietnamese
        text = read(README_VI)
        for word in ("Microsoft Store", "python.org", "`py`", "icacls"):
            self.assertIn(word, text, "README.vi.md does not mention %s" % word)
        flat = squash(text)
        self.assertRegex(flat, r"không dùng python cài từ microsoft store",
                         "README.vi.md must tell Windows users not to use the Microsoft Store Python")
        # Updated ON PURPOSE round 2: how to check the running Python, the folders to avoid, "not verified yet"
        self.assertRegex(flat, r"chưa kiểm chứng")
        paragraph = next(p for p in re.split(r"\n\s*\n", text) if "Microsoft Store" in p)
        for word in ("python.org", "`py`", "icacls", "py -0p", "sys.executable", "WindowsApps", "PythonSoftwareFoundation"):
            self.assertIn(word, paragraph)

    def test_readme_vi_mentions_init_config_and_the_command(self):
        text = read(README_VI)
        for word in ("init-config", "/odoo-kpi-skills:kpi", "label shift", "my_aliases.json"):
            self.assertIn(word, text, "README.vi.md does not mention %s" % word)

    def test_readme_vi_tells_the_human_claude_never_runs_init_config_nor_through_a_fake_terminal(self):
        flat = squash(read(README_VI))
        self.assertRegex(flat, r"claude không bao giờ", "README.vi.md must say Claude never runs init-config")
        self.assertRegex(flat, r"terminal giả", "README.vi.md must say init-config is not driven through a fake terminal")

    def test_readme_vi_explains_the_scan_and_the_reconcile_in_words(self):
        flat = squash(read(README_VI))
        for word in ("quét", "đối chiếu", "không khớp", "cảnh báo", "lệch", "không phải lỗi"):
            self.assertIn(word, flat)

    def test_readme_vi_names_the_scan_and_reconcile_scripts_like_the_english_readme(self):
        # the plan lists the two script names for both READMEs (a maintainer reading the Vietnamese one finds the files)
        text = read(README_VI)
        for word in ("scan_excel.py", "reconcile_kpi.py"):
            with self.subTest(script=word):
                self.assertIn(word, text, "README.vi.md never names scripts/%s (README.md does)" % word)

    def test_readme_vi_error_table_lists_the_new_messages_with_a_meaning_and_a_fix(self):
        text = read(README_VI)
        table = section(text, r"lỗi thường gặp")
        self.assertIsNotNone(table)
        rows = [line for line in table[1].splitlines() if line.startswith("|")]
        for message in ("the dashboard file is not what the spec produces",
                        "no header row with the 12 months in order was found", "looks like an API key"):
            with self.subTest(message=message):
                matching = [row for row in rows if message in row]
                self.assertEqual(len(matching), 1, "README.vi.md error table: %r listed %d times" % (message, len(matching)))
                cells = [cell.strip() for cell in matching[0].strip().strip("|").split("|")]
                self.assertEqual(len(cells), 3, matching[0])
                self.assertTrue(all(cells), "an empty cell in the row of %r" % message)
                self.assertIn(message, cells[0])

    def test_readme_vi_table_has_a_uniform_shape(self):
        table = section(read(README_VI), r"lỗi thường gặp")[1]
        rows = [line for line in table.splitlines() if line.startswith("|")]
        self.assertGreaterEqual(len(rows), 15)
        self.assertEqual({row.count("|") for row in rows}, {4}, "every row of the error table has three cells")

    def test_readme_vi_reuse_paragraph_names_the_slug_compares_first_and_never_overwrites_a_spec(self):
        text = read(README_VI)
        match = re.search(r"(?m)^\*\*Tháng sau.*(?:\n(?!\s*\n).*)*", text)
        self.assertIsNotNone(match, "README.vi.md has no reuse paragraph")
        flat = squash(match.group(0))
        for pattern in (r"<tên kpi>\.spec\.json", r"tên do claude tự đặt từ lời bạn", r"chỉ gồm chữ, số, _ và -",
                        r"không lấy nguyên từ tên file excel", r"so với spec cũ", r"cho bạn xem chỗ nào giống",
                        r"chỗ nào đã dịch hoặc mới",
                        r"chỉ khi bạn đồng ý, và lời đồng ý nêu rõ các ô đã dịch, mới làm mới spec", r"--force",
                        r"không chỉ định --out",
                        r"quét từ chối ghi đè file nhãn, file excel đã đọc và mọi file \.spec\.json"):
            with self.subTest(pattern=pattern):
                self.assertRegex(flat, pattern)
        self.assertLess(flat.index("so với spec cũ"), flat.index("chỉ khi bạn đồng ý, và lời đồng ý nêu rõ các ô đã dịch"))
        self.assertLess(flat.index("chỉ khi bạn đồng ý"), flat.index("--force"))
        self.assertNotIn("<tên excel>", flat)
        self.assertNotIn("không bao giờ ghi đè lên file spec", flat)       # the old sentence: it promised more than the scan did

    def reuse_paragraph_vi(self):
        match = re.search(r"(?m)^\*\*Tháng sau.*(?:\n(?!\s*\n).*)*", read(README_VI))
        self.assertIsNotNone(match, "README.vi.md has no reuse paragraph")
        return match.group(0)

    def test_readme_vi_reuse_paragraph_says_label_text_and_which_cells_hold_a_number_or_are_blank_are_compared(self):
        # fix-r2 T3
        flat = squash(self.reuse_paragraph_vi())
        for pattern in (r"so với spec cũ \(kể cả chữ ở các ô nhãn và ô nào có số hay để trống\)",
                        r"cho bạn xem chỗ nào giống, chỗ nào đã dịch hoặc mới",
                        r"lời đồng ý nêu rõ các ô đã dịch"):
            with self.subTest(pattern=pattern):
                self.assertRegex(flat, pattern)
        self.assertLess(flat.index("kể cả chữ ở các ô nhãn"), flat.index("cho bạn xem"))
        self.assertLess(flat.index("cho bạn xem"), flat.index("lời đồng ý nêu rõ các ô đã dịch"))
        self.assertLess(flat.index("lời đồng ý nêu rõ các ô đã dịch"), flat.index("mới làm mới spec"))

    def test_readme_vi_reuse_paragraph_says_a_second_scan_uses_force_without_out_and_what_the_scan_refuses(self):
        # fix-r2 T2 (the three things the scan refuses are the real ones: see SpecNameOutTests)
        flat = squash(self.reuse_paragraph_vi())
        self.assertRegex(flat, r"quét lại sau khi thêm nhãn mới vào my_aliases\.json" + gap(60) + r"--force")
        self.assertRegex(flat, r"--force \(file nháp đã có\) và không chỉ định --out")
        self.assertRegex(flat, r"quét từ chối ghi đè file nhãn, file excel đã đọc và mọi file \.spec\.json")
        self.assertEqual(flat.count("--force"), 1)
        self.assertEqual(flat.count("--out"), 1)

    def test_readme_vi_reuse_paragraph_says_tab_and_dashboard_names_with_special_characters_are_not_put_in_a_command(self):
        # fix-r2 T1 (the character list of README.vi.md is a subset of SKILL.md's: dấu nháy = both quotes)
        raw = self.reuse_paragraph_vi()
        flat = squash(raw)
        self.assertRegex(flat, r"tên tab excel hoặc tên dashboard có ký tự đặc biệt")
        self.assertRegex(flat, r"claude không đưa vào lệnh mà nhờ bạn đổi tên tab hoặc gõ lại tên dashboard")
        sentence = raw[raw.index("Tên tab Excel"):]
        listed = re.findall(r"`([^`]+)`", sentence)
        self.assertEqual(sorted(listed), sorted(["$", "&", ";", "|", "(", ")", "%", "^", "!"]))
        self.assertIn("dấu nháy", sentence)
        self.assertIn("dấu gạch ngược", sentence)
        self.assertLess(sentence.index("dấu nháy"), sentence.index("nhờ bạn đổi tên tab"))
        for character in listed:                                                    # nothing README.vi.md lists is absent
            self.assertIn("`%s`" % character, read(SKILL), "SKILL.md does not list %s" % character)

    def test_readme_vi_example_summary_line_is_marked_as_an_example_and_has_the_real_shape(self):
        zero = reconcile_kpi.summary_line({"replaced": 0, "typed": 0, "equal": 0, "rounded": 0, "differ": 0, "error": 0,
                                           "untyped": 0, "had_text": 0, "formula_left": 0})
        shape = re.escape(zero[:zero.index("), ") + 1]).replace("0", r"\d+")           # up to `0 error)`
        found = re.search("`(%s)[^`]*`\\s*\\(ví dụ\\)" % shape, read(README_VI))
        self.assertIsNotNone(found, "the README.vi.md summary line must have the real shape and be followed by (ví dụ)")
        numbers = [int(n) for n in re.findall(r"\d+", found.group(1))]
        replaced, typed, equal, rounded, differ, error = numbers
        self.assertEqual(typed, equal + rounded + differ + error)        # an example that could really be printed
        self.assertLessEqual(typed, replaced)

    def test_readme_vi_limits_say_the_customers_figures_go_through_the_ai_providers_session(self):
        found = section(read(README_VI), r"giới hạn cần biết")
        self.assertIsNotNone(found)
        flat = squash(found[1])
        for pattern in (r"số liệu của khách", r"phiên làm việc với claude", r"nhà cung cấp mô hình ai",
                        r"chỉ dùng cho dữ liệu mà công ty cho phép"):
            with self.subTest(pattern=pattern):
                self.assertRegex(flat, pattern)

    def test_the_command_says_confirm_instance_is_typed_by_the_user_even_when_the_host_is_in_the_pasted_address(self):
        body = read(COMMAND).split("\n---\n", 1)[1]
        self.assertEqual(says(body, r"--confirm-instance is typed by the user",
                              r"even when the host name is visible in the" + gap(30) + r"address",
                              r"ask them to type it", NEG + r" copy it from the address"), [])

    def test_readme_en_still_lists_the_plugin_parts_table_with_the_new_scripts(self):
        text = read(README)
        for word in ("`scripts/scan_excel.py`", "`scripts/reconcile_kpi.py`", "`commands/kpi.md`"):
            self.assertIn(word, text)

    def test_the_two_readmes_agree_on_the_command_name(self):
        for path in (README, README_VI):
            self.assertIn("/odoo-kpi-skills:kpi", read(path))
        self.assertEqual(json.loads(read(REPO / ".claude-plugin" / "plugin.json"))["name"], "odoo-kpi-skills")


# ---------------------------------------------------------------- 0.1.1: a document that is already a spreadsheet

class SpreadsheetDocumentDocsTests(DocCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(SKILL)

    def body(self, title_pattern):
        found = section(self.text, title_pattern)
        self.assertIsNotNone(found, "no step headed like %r" % title_pattern)
        return found[1]

    def test_step_1_accepts_an_uploaded_xlsx_or_a_document_that_is_already_a_spreadsheet(self):
        body = self.body(r"^1\.\s+find the excel")
        self.assertEqual(says(body, r"spreadsheet_id=", r"already a spreadsheet", r"uploaded \.xlsx",
                              r"same --xlsx-document <n> command handles both",
                              r"never the host name"), [])
        self.assertNotIn("(not already a spreadsheet)", body)

    def test_step_3_says_a_spreadsheet_document_is_neither_copied_nor_deleted_and_document_id_never_waits(self):
        body = self.body(r"^3\.\s+intake")
        self.assertEqual(says(body, r"already a spreadsheet the script makes no copy and deletes nothing",
                              r"file > snapshot", r"--document-id <id> \(a spreadsheet that already holds a snapshot, "
                              r"it never waits\)"), [])
        self.assertEqual(says(body, r"deletes its copy"), [])                # the uploaded-xlsx path still deletes its copy

    def test_the_version_is_the_same_in_the_manifest_the_client_and_the_readme(self):
        version = json.loads(read(REPO / ".claude-plugin" / "plugin.json"))["version"]
        self.assertEqual(odoo_rpc.VERSION, version)
        self.assertEqual(odoo_rpc.USER_AGENT, "odoo-kpi-skills/" + version)
        self.assertIn("Status: **%s," % version, read(README))


# ---------------------------------------------------------------- 0.1.2: the `live data:` line of reconcile

class LiveDataLineDocsTests(DocCase):
    """The skill must tell Claude to read the `live data:` line before saying an instance has no data (the 0.1.1
    incident: the first 50 differ lines were all `-`, the agent concluded "no data" for a year that had numbers)."""

    @classmethod
    def setUpClass(cls):
        cls.text = read(SKILL)
        found = section(cls.text, r"^5\.")
        cls.step5 = found[1] if found else ""

    def test_the_bind_step_is_where_the_paragraph_lives_and_it_exists(self):
        self.assertTrue(self.step5, "no step 5 section in SKILL.md")
        self.assertIn("live data:", self.step5)

    def test_the_skill_says_the_differ_list_is_cut_after_50_lines_and_never_to_conclude_from_the_first_lines(self):
        self.assertEqual(says(self.step5, r"cut after 50 lines", r"never conclude from the first lines"), [])

    def test_the_skill_says_to_read_the_live_data_line_that_also_covers_months_the_excel_left_empty(self):
        self.assertEqual(says(self.step5, r"read the line live data:", r"live source has any number for the year",
                              r"months the excel left empty"), [])

    def test_the_skill_allows_no_data_only_when_the_line_says_all_zero_and_asks_to_check_the_year_anyway(self):
        # Updated ON PURPOSE for 0.1.2 round 2: the all-zero condition is "every cell that reads the data of the year"
        self.assertEqual(says(self.step5, r"the instance has no data for this year\" only when that line says every "
                                          r"cell that reads the data of the year is zero or -",
                              r"even then check the year with the user"), [])

    def test_the_skill_says_previous_year_cells_and_derived_cells_do_not_count_as_data_of_the_year(self):
        self.assertEqual(says(self.step5, r"previous-year cells and derived cells such as ratios do not count as data "
                                          r"of the year"), [])

    def test_the_live_line_wording_of_the_script_is_the_one_the_skill_names(self):
        # the skill quotes `live data: ...` and "every cell that reads the data of the year is zero"
        zero = reconcile_kpi.live_line({"counts": {"replaced": 1}, "live_nonzero": []}, 2026)
        has = reconcile_kpi.live_line({"counts": {"replaced": 1}, "live_nonzero": ["D5"]}, 2026)
        for line in (zero, has):
            self.assertTrue(line.startswith("live data: "))
        self.assertIn("every replaced cell that reads the data of year 2026 is zero or '-'", zero)
        self.assertIn("HAS data", has)
        self.assertIn("read a non-zero number of year 2026 (D5)", has)


# ---------------------------------------------------------------- manifest

class ManifestTests(DocCase):
    PLUGIN = REPO / ".claude-plugin" / "plugin.json"
    MARKET = REPO / ".claude-plugin" / "marketplace.json"

    def test_the_plugin_manifest_is_valid_and_holds_no_secret(self):
        text = read(self.PLUGIN)
        data = json.loads(text)
        self.assertEqual(data["name"], "odoo-kpi-skills")
        self.assertRegex(data["version"], r"^\d+\.\d+\.\d+")
        self.assertTrue(data["description"])
        for line in text.splitlines():
            self.assertIsNone(SECRET_QUOTED.search(line) or SECRET_INI.search(line), line)
        self.assertIsNone(HEX40.search(text))
        for key in data:
            self.assertNotRegex(key.lower(), r"secret|password|api_?key|token")

    def test_the_marketplace_lists_the_same_plugin_from_this_folder(self):
        data = json.loads(read(self.MARKET))
        self.assertEqual([entry["name"] for entry in data["plugins"]], ["odoo-kpi-skills"])
        self.assertEqual(data["plugins"][0]["source"], "./")
        self.assertIsNone(HEX40.search(read(self.MARKET)))

    def test_the_plugin_still_has_exactly_its_two_skills_and_the_command(self):
        skills = sorted(entry for entry in os.listdir(str(REPO / "skills")) if (REPO / "skills" / entry).is_dir())
        self.assertEqual(skills, ["kpi-builder", "kpi-source-catalog"])
        for name in skills:
            fields, problems = parse_frontmatter(read(REPO / "skills" / name / "SKILL.md"))
            self.assertEqual(fields.get("name"), name)
            self.assertTrue(fields.get("description"))
        self.assertIn("kpi.md", os.listdir(str(REPO / "commands")))

    def test_no_manifest_hides_a_skill_or_a_command_list_that_would_drop_the_new_files(self):
        data = json.loads(read(self.PLUGIN))
        for key in ("skills", "commands"):
            if key in data:                                  # an explicit list must still name them
                self.assertTrue(any("kpi" in str(item) for item in ([data[key]] if isinstance(data[key], str)
                                                                    else data[key])))


# ---------------------------------------------------------------- the public tree

class PublicDocsTests(DocCase):
    def test_the_hygiene_scan_of_the_whole_tree_is_green(self):
        self.assertEqual(scan_tree(REPO), [])

    def test_the_docs_hold_no_real_instance_label_company_domain_or_login(self):
        for path in DOCS:
            raw = path.read_bytes()
            with self.subTest(file=path.name):
                self.assertEqual([term for term in FORBIDDEN_TEXT if has_term(raw, term)], [])
                self.assertIsNone(HEX40.search(raw.decode("utf-8")))

    def test_every_address_in_the_docs_is_a_placeholder_or_an_example_domain(self):
        for path in DOCS:
            for host in re.findall(r"https?://([A-Za-z0-9._<>{}-]+)", read(path)):
                with self.subTest(file=path.name, host=host):
                    self.assertTrue(host.startswith("<") or host.endswith("example.com") or host in ("localhost", "127.0.0.1"),
                                    "%s names a real-looking host %r" % (path.name, host))

    def test_no_doc_shows_a_secret_looking_assignment(self):
        # the same two patterns as the hygiene scan, on the docs alone (a placeholder in angle brackets is fine)
        for path in DOCS:
            for number, line in enumerate(read(path).splitlines(), 1):
                if "secret-allow" not in line:
                    self.assertIsNone(SECRET_QUOTED.search(line) or SECRET_INI.search(line),
                                      "%s line %d" % (path.name, number))


if __name__ == "__main__":
    unittest.main()
